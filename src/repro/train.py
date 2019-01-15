from bisect import bisect_right
from datetime import datetime
import argparse
import pprint
import random

import numpy

from ignite.engine import Events, create_supervised_trainer, create_supervised_evaluator
from ignite.handlers.timing import Timer
from ignite.metrics import Accuracy, Loss

from kleio.core.io.resolve_config import merge_configs
from kleio.core.utils import unflatten

import mahler.client as mahler

import torch
import torch.nn.functional as F
import torch.optim

import tqdm

import yaml

from repro.dataset.base import build_dataset
from repro.model.base import build_model, load_checkpoint, save_checkpoint
from repro.optimizer.base import build_optimizer


TIME_BUFFER = 60 * 5  # 5 minutes


def update(config, arguments):
    pairs = [argument.split("=") for argument in arguments]
    kwargs = unflatten(dict((pair[0], eval(pair[1])) for pair in pairs))
    return merge_configs(config, kwargs)


def update_lr(lr, optimizer, epoch):
    if epoch < 1:
        new_lr = lr / 10
    elif epoch < 60:
        new_lr = lr
    elif epoch < 120:
        new_lr = lr / 10
    else:
        new_lr = lr / 100

    for param_group in optimizer.param_groups:
        param_group['lr'] = new_lr


class MultiStepLR(torch.optim.lr_scheduler.MultiStepLR):
    def __init__(self, optimizer, milestones, gamma=0.1, div_first_epoch=False, last_epoch=-1):
        self.div_first_epoch = div_first_epoch
        super(MultiStepLR, self).__init__(optimizer, milestones, gamma=0.1, last_epoch=-1)

    def get_lr(self):
        if self.last_epoch < 1 and self.div_first_epoch:
            return [base_lr * self.gamma for base_lr in self.base_lrs]

        return [base_lr * self.gamma ** bisect_right(self.milestones, self.last_epoch)
                for base_lr in self.base_lrs]


def build_config(**kwargs):
    with open(kwargs.pop('config'), 'r') as f:
        config = yaml.load(f)

    if 'updates' in kwargs:
        if isinstance(kwargs['updates'], str):
            kwargs['updates'] = [kwargs['updates']]

        update(config, kwargs.pop('updates'))

    config.update(kwargs)

    return config


def build_experiment(**config):

    seeds = {'model': config.get('model_seed'),
             'sampler': config.get('sampler_seed')}

    if seeds['model'] is None:
        raise ValueError("model_seed must be defined")

    if seeds['sampler'] is None:
        raise ValueError("sampler_seed must be defined")

    device = torch.device('cpu')
    if torch.cuda.is_available():
        print("\n\nUsing GPU\n\n")
        device = torch.device('cuda')
    else:
        raise RuntimeError("GPU not available")

    print("\n\nConfiguration\n")
    pprint.pprint(config)
    print("\n\n")

    seed(int(seeds['sampler']))

    dataset = build_dataset(**config['data'])
    input_size = dataset['input_size']
    num_classes = dataset['num_classes']

    print("\n\nDatasets\n")
    pprint.pprint(dataset)
    print("\n\n")

    # Note: model is not loaded here for resumed trials
    seed(int(seeds['model']))
    model = build_model(input_size=input_size, num_classes=num_classes, **config['model'])

    print("\n\nModel\n")
    pprint.pprint(model)
    print("\n\n")

    lr_scheduler_config = config['optimizer'].pop("lr_scheduler", None)

    optimizer = build_optimizer(model=model, **config['optimizer'])

    if lr_scheduler_config:
        milestones = lr_scheduler_config['milestones']
        div_first_epoch = milestones[0]
        milestones = milestones[1:]
        # TODO: Adapt last_epoch if resuming
        lr_scheduler = MultiStepLR(
            optimizer, milestones, gamma=0.1, div_first_epoch=div_first_epoch, last_epoch=-1)
    else:
        lr_scheduler = None

    print("\n\nOptimizer\n")
    pprint.pprint(optimizer)
    print("\n\n")

    return dataset, model, optimizer, lr_scheduler, device, seeds


def main(argv=None):
    parser = argparse.ArgumentParser(description='Script to train a model')
    parser.add_argument('--config', help='Path to yaml configuration file for the trial')
    parser.add_argument('--model-seed', type=int, help='Seed for model\'s initialization')
    parser.add_argument('--sampler-seed', type=int, help='Seed for data sampling order')
    # parser.add_argument('--epochs', type=int, required=True, help='number of epochs to train.')

    parser.add_argument('--updates', nargs='+', default=[], metavar='updates',
                        help='Values to update in the configuration file')

    args = parser.parse_args(argv)

    config = build_config(**vars(args))

    train(**config)


def train(data, model, optimizer, model_seed=1, sampler_seed=1, max_epochs=200,
          compute_error_rates=('train', 'valid')):

    # Create client inside function otherwise MongoDB does not play nicely with multiprocessing
    mahler_client = mahler.Client()

    dataset, model, optimizer, lr_scheduler, device, seeds = build_experiment(
        data=data, model=model, optimizer=optimizer,
        model_seed=model_seed, sampler_seed=sampler_seed)

    timer = Timer(average=True)

    trainer = create_supervised_trainer(
        model, optimizer, torch.nn.functional.cross_entropy, device=device)
    evaluator = create_supervised_evaluator(
        model, metrics={'accuracy': Accuracy(),
                        'nll': Loss(F.cross_entropy)},
        device=device)

    timer.attach(trainer, start=Events.STARTED, step=Events.EPOCH_COMPLETED)

    all_stats = []

    @trainer.on(Events.STARTED)
    def trainer_load_checkpoint(engine):
        engine.state.last_checkpoint = datetime.utcnow()
        metadata = load_checkpoint(mahler_client, model, optimizer, lr_scheduler)
        if metadata:
            print('Resuming from epoch {}'.format(metadata['epoch']))
            engine.state.epoch = metadata['epoch']
            engine.state.iteration = metadata['iteration']
            for epoch_stats in metadata['all_stats']:
                all_stats.append(epoch_stats)
        else:
            engine.state.epoch = 0
            engine.state.iteration = 0
            engine.state.output = 0.0
            trainer_save_checkpoint(engine)

    @trainer.on(Events.EPOCH_STARTED)
    def trainer_seeding(engine):
        if lr_scheduler:
            lr_scheduler.step()
        seed(seeds['sampler'] + engine.state.epoch)
        model.train()

    @trainer.on(Events.EPOCH_COMPLETED)
    def trainer_save_checkpoint(engine):
        model.eval()

        stats = dict(epoch=engine.state.epoch)

        for name in ['train', 'valid', 'test']:
            if name not in compute_error_rates:
                continue

            loader = dataset[name]
            metrics = evaluator.run(loader).metrics
            stats[name] = dict(
                loss=metrics['nll'],
                error_rate=1. - metrics['accuracy'])

        all_stats.append(stats)

        print("Epoch {:>4} Iteration {:>8} Loss {:>12} Time {:>6}".format(
            engine.state.epoch, engine.state.iteration, engine.state.output, timer.value()))

        # TODO: Checkpoint lr_scheduler as well
        if (datetime.utcnow() - engine.state.last_checkpoint).total_seconds() > TIME_BUFFER:
            print('Checkpointing epoch {}'.format(engine.state.epoch))
            save_checkpoint(mahler_client,
                            model, optimizer, lr_scheduler,
                            epoch=engine.state.epoch,
                            iteration=engine.state.iteration,
                            all_stats=all_stats)
            engine.state.last_checkpoint = datetime.utcnow()

    print("Training")
    trainer.run(dataset['train'], max_epochs=max_epochs)

    # Remove checkpoint to avoid cluttering the FS.
    clear_checkpoint(mahler_client)

    return {'last': all_stats[-1], 'all': tuple(all_stats)}


def seed(seed):
    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = False

    # random.seed(seed)
    # numpy.random.seed(seed)
    # torch.manual_seed(seed)
    # torch.cuda.manual_seed_all(seed)


if __name__ == "__main__":
    main()
