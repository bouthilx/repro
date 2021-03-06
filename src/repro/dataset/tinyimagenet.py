from collections import OrderedDict
import csv
import os
import urllib
import zipfile
import shutil
import time

from filelock import FileLock, Timeout

from PIL import Image

import h5py

import numpy

from tqdm import tqdm

import torch
from torchvision import datasets, transforms
import torchvision.transforms.functional as F

from repro.dataset.tensorhdf5 import HDF5Dataset

# download-url: http://cs231n.stanford.edu/tiny-imagenet-200.zip

# Train: 100000
# Val:    10000
# Train:  10000

DIRNAME = 'tiny-imagenet-200'
ZIP_FILENAME = 'tiny-imagenet-200.zip'
TRAIN_FILENAME = 'tinyimagenet_train.h5'
VAL_FILENAME = 'tinyimagenet_val.h5'
# TEST_FILENAME = 'tinyimagenet_test.h5'


def get_zipfile_path(data_path):
    return os.path.join(data_path, ZIP_FILENAME)


def get_dirpath(data_path):
    return os.path.join(data_path, DIRNAME)


def all_hdf5_exists(data_path):
    return all(os.path.exists(os.path.join(data_path, filename))
               for filename in [TRAIN_FILENAME, VAL_FILENAME])


def build_dataset(data_path, timeout=10 * 60):
    if all_hdf5_exists(data_path):
        return

    try:
        with FileLock(os.path.join(data_path, DIRNAME + ".lock"), timeout=timeout):
            download(data_path)
            unzip(data_path)
            create_hdf5(data_path)
    except Timeout:
        print("Another process holds the lock since more than {} seconds. "
              "Will try to load the dataset.").format(timeout)
    finally:
        clean(data_path)


def download(data_path):
    if os.path.exists(get_zipfile_path(data_path)):
        print("Zip file already downloaded")
        return

    # download 
    url = 'http://cs231n.stanford.edu/tiny-imagenet-200.zip'
    u = urllib.request.urlopen(url)
    with open(get_zipfile_path(data_path), 'wb') as f:
        file_size = int(dict(u.getheaders())['Content-Length']) / (10.0**6)
        print("Downloading: {} ({}MB)".format(get_zipfile_path(data_path), file_size))

        file_size_dl = 0
        block_sz = 8192
        pbar = tqdm(total=file_size, desc='TinyImageNet')
        while True:
            buffer = u.read(block_sz)
            if not buffer:
                break
            f.write(buffer)
            pbar.update(len(buffer) / (10.0 ** 6))

        pbar.close()


def unzip(data_path):
    print("Unzipping files...")
    with zipfile.ZipFile(get_zipfile_path(data_path), 'r') as zip_ref:
        zip_ref.extractall(data_path)
    print("Done")


def clean(data_path):
    print("Deleting unzipped files...")
    shutil.rmtree(get_dirpath(data_path))


def create_hdf5(data_path):
    create_hdf5_train(
        get_dirpath(data_path), os.path.join(data_path, 'tinyimagenet_train.h5'))

    create_hdf5_val(
        get_dirpath(data_path), os.path.join(data_path, 'tinyimagenet_val.h5'))


def create_train_loader(dirpath):
    dataset = datasets.ImageFolder(
        os.path.join(dirpath, 'train'),
        transforms.Compose([transforms.ToTensor()]))

    dataloader = torch.utils.data.DataLoader(
        dataset=dataset, batch_size=1, num_workers=1)

    for batch in dataloader:
        yield batch


def create_hdf5_file(dirpath, file_path, n, dataloader):

    f = h5py.File(file_path, 'w', libver='latest')

    data = f.create_dataset(
        "data", (n, 64, 64, 3),
        chunks=(1, 64, 64, 3),
        dtype=numpy.uint8)
        # compression='lzf')
    labels = f.create_dataset("labels", (n, ), dtype=numpy.uint8)

    f.swmr_mode = True

    for index, (x, y) in enumerate(tqdm(dataloader, total=n, desc='HDF5')):
        x = numpy.array(x * 255, dtype=numpy.uint8)
        data[index] = numpy.moveaxis(x, 1, -1)
        labels[index] = y

    f.close()


def create_hdf5_train(dirpath, file_path):
    return create_hdf5_file(dirpath, file_path, 100000, create_train_loader(dirpath))


def create_hdf5_val(dirpath, file_path):
    return create_hdf5_file(dirpath, file_path, 10000, create_val_loader(dirpath))


def create_val_loader(dirpath):

    train_dataset = datasets.ImageFolder(
        os.path.join(dirpath, 'train'),
        transforms.Compose([transforms.ToTensor()]))

    with open(os.path.join(dirpath, 'val', 'val_annotations.txt'), 'r') as f:
        csv_reader = csv.reader(f, delimiter='\t')

        for index, row in enumerate(csv_reader):
            filename = row[0]
            class_id = row[1]

            image_path = os.path.join(dirpath, 'val', 'images', filename)
            with open(image_path, 'rb') as f:
                img = Image.open(f)
                img = img.convert('RGB')
                x = F.to_tensor(img).unsqueeze(0)

            yield x, train_dataset.class_to_idx[class_id]


def build(batch_size, data_path, num_workers):
    normalize = transforms.Normalize(mean=[0.4194, 0.3898, 0.3454],
                                     std=[0.303, 0.291, 0.293])

    build_dataset(data_path)

    dataset = HDF5Dataset(
        os.path.join(data_path, TRAIN_FILENAME),
        transforms.Compose([
            # data is stored as uint8
            transforms.ToPILImage(),
            transforms.RandomCrop(64, padding=8),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ]),
        transforms.Lambda(lambda x: int(x)))

    train_loader = torch.utils.data.DataLoader(
        dataset=dataset, batch_size=batch_size, num_workers=num_workers,
        pin_memory=True, shuffle=True)

    dataset = HDF5Dataset(
        os.path.join(data_path, VAL_FILENAME),
        transforms.Compose([
            # data is stored as uint8
            transforms.ToPILImage(),
            transforms.ToTensor(),
            normalize,
        ]),
        transforms.Lambda(lambda x: int(x)))

    sampler = torch.utils.data.sampler.SubsetRandomSampler(range(int(len(dataset) / 2)))

    valid_loader = torch.utils.data.DataLoader(
        dataset=dataset, batch_size=batch_size, num_workers=num_workers, pin_memory=True,
        sampler=sampler, shuffle=False)

    sampler = torch.utils.data.sampler.SubsetRandomSampler(range(int(len(dataset) / 2), len(dataset)))

    test_loader = torch.utils.data.DataLoader(
        dataset=dataset, batch_size=batch_size, num_workers=num_workers, pin_memory=True,
        sampler=sampler, shuffle=False)

    return OrderedDict(train=train_loader, valid=valid_loader, test=test_loader,
                       input_size=(3, 64, 64), num_classes=200)


if __name__ == "__main__":
    from repro.utils.cov import ExpectationMeter, CovarianceMeter
    for num_workers in range(1, 9): # range(5, 6):  # 1, 9):
        print("\n-*- {} -*-\n".format(num_workers))
        datasets = build(128, "/Tmp/data", num_workers)
        std = CovarianceMeter()
        topmax = 0
        for x, y in tqdm(datasets['train'], desc='train'):
            flattened = x.permute(0, 2, 3, 1).contiguous().view(-1, 3)
            std.add(flattened, n=flattened.size(0))
            topmax = max(topmax, y.max())
        print(topmax)
        print(std.expectation_meter.value())
        print(std.value())
