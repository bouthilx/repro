import asyncio
import argparse
import os

from sgdad.utils.commandline import execute


EXPERIMENT = "benchmark"

orion_template = 'orion hunt -n {experiment};{dataset};{model};{version} --config /configs/orion.core/orion_config.yaml'

kleio_template = 'kleio run --allow-host-changes --config /config/kleio.core/kleio_config.yaml --tags {experiment};{dataset};{model};{version}'

script_template = (
    "python3.6 /repos/sgd-space/src/sgdad/train.py --config={file_path} "
    "--model-seed 1 --sampler-seed 1 --epochs 300")

commandline_template = "{orion} {kleio} {script}"

# orion save -n $experiment.$dataset.$model \
#     kleio run --tags $experiment;$dataset;$model \
#         python src/sgdad/train.py --config=${file_path} --model-seed 1 --sampler-seed 1 --epochs 10


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Script to train a model')
    parser.add_argument('--configs', default='configs', help='Root folder for configs')
    parser.add_argument('--version', required=True, help='Version of the execution')
    parser.add_argument('--datasets', nargs="*", help='Datasets to save executions for')
    parser.add_argument('--models', nargs="*", help='Models to save executions for')
    # TODO remove print_only, and turn it into a test for kleio, if not using
    # kleio to register this execution, then print-only
    parser.add_argument('--print-only', action='store_true',
                        help='Print executions but do not execute.')

    return parser.parse_args(argv)


def get_instances(configs_root, datasets, models, experiment):
    possible_datasets = [dataset for dataset
                         in os.listdir(os.path.join(configs_root, experiment))
                         if os.path.isdir(os.path.join(configs_root, experiment, dataset))]

    for dataset in possible_datasets:

        if datasets and dataset not in datasets:
            continue

        possible_models = [model[:-5] for model 
                           in os.listdir(os.path.join(configs_root, experiment, dataset))
                           if model.split(".")[-1] == "yaml"]

        for model in possible_models:

            if models and model not in models:
                continue

            file_path = os.path.join(configs_root, experiment, dataset, model + ".yaml")

            yield dataset, model, file_path


def main(argv=None):

    args = parse_args(argv)

    iterator = get_instances(args.configs, args.datasets, args.models, "0.benchmark")
    futures = []
    for dataset, model, file_path in iterator:
        orion = orion_template.format(
            experiment=EXPERIMENT, dataset=dataset, model=model, version=args.version)
        kleio = kleio_template.format(
            experiment=EXPERIMENT, dataset=dataset, model=model, version=args.version)
        script = script_template.format(
            file_path=file_path, data_wrapper_level=data_wrapper_level)
        commandline = commandline_template.format(kleio=kleio, script=script)
        futures.append(execute(commandline, print_only=args.print_only))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(*futures))
    loop.close()


if __name__ == "__main__":
    main()