from typing import Union, List
import pytorch_lightning as pl
from torchio import DATA
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR, MultiStepLR
from data.get_subjects import get_subjects, get_processed_subjects
from data.const import CC359_DATASET_DIR, NFBS_DATASET_DIR, ADNI_DATASET_DIR_1, COMPUTECANADA
from data.transform import get_train_transforms, get_val_transform, get_test_transform
from argparse import ArgumentParser
from data.const import SIZE
from model.unet.unet import UNet
from utils.matrix import get_score
from utils.loss import dice_loss
from torch.optim.lr_scheduler import MultiStepLR
import nibabel as nib
import numpy as np
import torch.nn.functional as F
from postprocess.visualize import log_all_info
from torch import Tensor
from monai.losses import GeneralizedDiceLoss, DiceLoss
import torchio
import torch
import random


class Lightning_Unet(pl.LightningModule):
    def __init__(self, hparams):
        super(Lightning_Unet, self).__init__()
        self.hparams = hparams

        self.out_classes = 139
        self.deepth = 4
        self.kernal_size = 5  # whether this affect the model to learn?
        self.module_type = 'Unet'
        self.downsampling_type = 'max'
        self.normalization = 'InstanceNorm3d'

        self.unet = UNet(
            in_channels=1,
            out_classes=self.out_classes,
            num_encoding_blocks=self.deepth,
            out_channels_first_layer=32,
            kernal_size=self.kernal_size,
            normalization=self.normalization,
            module_type=self.module_type,
            downsampling_type=self.downsampling_type,
            dropout=0,
        )

        # torchio parameters
        # ?need to try to find the suitable value
        self.max_queue_length = 10
        self.patch_size = 96
        # Number of patches to extract from each volume. A small number of patches ensures a large variability
        # in the queue, but training will be slower.
        self.samples_per_volume = 10
        self.val_times = 0
        self.num_workers = 0
        # if not self.hparams.cedar:
        #     self.num_workers = 20

        if not COMPUTECANADA:
            self.num_workers = 8
            self.subjects, self.visual_img_path_list, self.visual_label_path_list = get_processed_subjects(
                whether_use_cropped_and_resample_img=True
            )
            random.seed(42)
            random.shuffle(self.subjects)  # shuffle it to pick the val set
            num_subjects = len(self.subjects)
            num_training_subjects = int(num_subjects * 0.95)  # （5074+359+21） * 0.9 used for training
            self.training_subjects = self.subjects[:num_training_subjects]
            self.validation_subjects = self.subjects[num_training_subjects:]

    def forward(self, x: Tensor) -> Tensor:
        return self.unet(x)

    # Called at the beginning of fit and test. This is a good hook when you need to build models dynamically or
    # adjust something about them. This hook is called on every process when using DDP.
    def setup(self, stage):
        self.subjects, self.visual_img_path_list, self.visual_label_path_list = get_processed_subjects(
            whether_use_cropped_and_resample_img=True
        )
        random.seed(42)
        random.shuffle(self.subjects)  # shuffle it to pick the val set
        num_subjects = len(self.subjects)
        num_training_subjects = int(num_subjects * 0.95)  # （5074+359+21） * 0.9 used for training
        self.training_subjects = self.subjects[:num_training_subjects]
        self.validation_subjects = self.subjects[num_training_subjects:]

    def train_dataloader(self) -> DataLoader:
        training_transform = get_train_transforms()
        train_imageDataset = torchio.ImagesDataset(self.training_subjects, transform=training_transform)

        patches_training_set = torchio.Queue(
            subjects_dataset=train_imageDataset,
            # Maximum number of patches that can be stored in the queue.
            # Using a large number means that the queue needs to be filled less often,
            # but more CPU memory is needed to store the patches.
            max_length=self.max_queue_length,
            # Number of patches to extract from each volume.
            # A small number of patches ensures a large variability in the queue,   ??? how to understand this??
            # but training will be slower.
            samples_per_volume=self.samples_per_volume,
            #  A sampler used to extract patches from the volumes.
            sampler=torchio.sampler.UniformSampler(self.patch_size),
            num_workers=self.num_workers,
            # If True, the subjects dataset is shuffled at the beginning of each epoch,
            # i.e. when all patches from all subjects have been processed
            shuffle_subjects=False,
            # If True, patches are shuffled after filling the queue.
            shuffle_patches=True,
            verbose=True,
        )

        training_loader = DataLoader(patches_training_set,
                                     batch_size=self.hparams.batch_size)

        print('Training set:', len(train_imageDataset), 'subjects')
        return training_loader

    def val_dataloader(self) -> DataLoader:
        val_transform = get_val_transform()
        val_imageDataset = torchio.ImagesDataset(self.validation_subjects, transform=val_transform)

        patches_validation_set = torchio.Queue(
            subjects_dataset=val_imageDataset,
            max_length=self.max_queue_length,
            samples_per_volume=self.samples_per_volume,
            sampler=torchio.sampler.UniformSampler(self.patch_size),
            num_workers=self.num_workers,
            shuffle_subjects=False,
            shuffle_patches=True,
            verbose=True,
        )

        val_loader = DataLoader(patches_validation_set,
                                batch_size=self.hparams.batch_size * 2)
        print('Validation set:', len(val_loader), 'subjects')
        return val_loader

    def test_dataloader(self):
        test_transform = get_test_transform()
        # using all the data to test
        test_imageDataset = torchio.ImagesDataset(self.subjects, transform=test_transform)
        test_loader = DataLoader(test_imageDataset,
                                 batch_size=1)  # always one because using different label size
        print('Testing set:', len(test_imageDataset), 'subjects')
        return test_loader

    # need to adding more things
    def configure_optimizers(self):
        # Setting up the optimizer
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.hparams.learning_rate)
        # scheduler = MultiStepLR(optimizer, milestones=[1, 10], gamma=0.1)
        return optimizer

    def prepare_batch(self, batch):
        inputs, targets = batch["img"][DATA], batch["label"][DATA]
        if torch.isnan(inputs).any():
            print("there is nan in input data!")
            inputs[inputs != inputs] = 0
        if torch.isnan(targets).any():
            print("there is nan in targets data!")
            targets[targets != targets] = 0
        return inputs, targets

    def training_step(self, batch, batch_idx):
        inputs, targets = self.prepare_batch(batch)
        probs = self(inputs)
        dice, iou, _, _ = get_score(probs, targets, include_background=True)
        # gdloss = GeneralizedDiceLoss(include_background=True, to_onehot_y=True)
        # loss = gdloss.forward(input=probs, target=targets)
        diceloss = DiceLoss(include_background=True, to_onehot_y=True)
        loss = diceloss.forward(input=probs, target=targets)
        # if batch_idx != 0 and ((self.current_epoch >= 1 and dice.item() < 0.5) or batch_idx % 100 == 0):
        #     input = inputs.chunk(inputs.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     target = targets.chunk(targets.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     prob = probs.chunk(probs.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     ＃　really have problem in there, need to fix it
        #     dice_score, _, _, _ = get_score(torch.unsqueeze(prob, 0), torch.unsqueeze(target, 0))
        #     log_all_info(self, input, target, prob, batch_idx, "training", dice_score.item())
        # loss = F.binary_cross_entropy_with_logits(logits, targets)
        tensorboard_logs = {"train_loss": loss, "train_IoU": iou, "train_dice": dice}
        return {'loss': loss, "log": tensorboard_logs}

    def validation_step(self, batch, batch_id):
        inputs, targets = self.prepare_batch(batch)
        probs = self(inputs)
        # gdloss = GeneralizedDiceLoss(include_background=True, to_onehot_y=True)
        # loss = gdloss.forward(input=probs, target=targets)
        diceloss = DiceLoss(include_background=True, to_onehot_y=True)
        loss = diceloss.forward(input=probs, target=targets)
        dice, iou, sensitivity, specificity = get_score(probs, targets)
        return {'val_step_loss': loss,
                'val_step_dice': dice,
                'val_step_IoU': iou,
                "val_step_sensitivity": sensitivity,
                "val_step_specificity": specificity
                }

    # Called at the end of the validation epoch with the outputs of all validation steps.
    def validation_epoch_end(self, outputs):
        # visualization part
        cur_img_path = self.visual_img_path_list[self.val_times % len(self.visual_img_path_list)]
        cur_label_path = self.visual_label_path_list[self.val_times % len(self.visual_label_path_list)]

        cur_img_subject = torchio.Subject(
            img=torchio.Image(cur_img_path, type=torchio.INTENSITY)
        )
        cur_label_subject = torchio.Subject(
            img=torchio.Image(cur_label_path, type=torchio.LABEL)
        )

        transform = get_val_transform()
        preprocessed_img = transform(cur_img_subject)
        preprocessed_label = transform(cur_label_subject)

        patch_overlap = 10  # is there any constrain?
        grid_sampler = torchio.inference.GridSampler(
            preprocessed_img,
            self.patch_size,
            patch_overlap,
        )

        patch_loader = torch.utils.data.DataLoader(grid_sampler)
        aggregator = torchio.inference.GridAggregator(grid_sampler)

        for patches_batch in patch_loader:
            input_tensor = patches_batch['img'][torchio.DATA]
            input_tensor = input_tensor.type_as(outputs[0]['val_step_loss'])
            locations = patches_batch[torchio.LOCATION]
            preds = self(input_tensor)
            labels = preds.argmax(dim=torchio.CHANNELS_DIMENSION, keepdim=True)
            aggregator.add_batch(labels, locations)
        output_tensor = aggregator.get_output_tensor()

        dice, _, _, _ = get_score(pred=output_tensor, target=preprocessed_label.img.data)

        log_all_info(self,
                     preprocessed_img.img.data,
                     preprocessed_label.img.data,
                     output_tensor,
                     dice,
                     self.val_times)

        self.val_times += 1

        # torch.stack: Concatenates sequence of tensors along a new dimension.
        avg_loss = torch.stack([x['val_step_loss'] for x in outputs]).mean()
        avg_val_dice = torch.stack([x['val_step_dice'] for x in outputs]).mean()
        tensorboard_logs = {
            "val_loss": outputs[0]['val_step_loss'],  # the outputs is a dict wrapped in a list
            "val_dice": outputs[0]['val_step_dice'],
            "val_IoU": outputs[0]['val_step_IoU'],
            "val_sensitivity": outputs[0]['val_step_sensitivity'],
            "val_specificity": outputs[0]['val_step_specificity']
        }
        return {"loss": avg_loss, "val_loss": avg_loss, "val_dice": avg_val_dice, 'log': tensorboard_logs}

    def test_step(self, batch, batch_idx):
        inputs, targets = self.prepare_batch(batch)
        # print(f"training input range: {torch.min(inputs)} - {torch.max(inputs)}")
        logits = self(inputs)
        logits = F.interpolate(logits, size=logits.size()[2:])
        probs = torch.sigmoid(logits)
        dice, iou, _, _ = get_score(probs, targets)
        # if batch_idx != 0 and batch_idx % 50 == 0:  # save total about 10 picture
        #     input = inputs.chunk(inputs.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     target = targets.chunk(targets.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     logit = probs.chunk(logits.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     # need to add the dice score here
        #     log_all_info(self, input, target, logit, batch_idx, "testing", 0.5)
        # loss = F.binary_cross_entropy_with_logits(logits, targets)
        loss = dice_loss(probs, targets)
        dice, iou, sensitivity, specificity = get_score(probs, targets)
        return {'test_step_loss': loss,
                'test_step_dice': dice,
                'test_step_IoU': iou,
                'test_step_sensitivity': sensitivity,
                'test_step_specificity': specificity
                }

    def test_epoch_end(self, outputs):
        # torch.stack: Concatenates sequence of tensors along a new dimension.
        avg_loss = torch.stack([x['test_step_loss'] for x in outputs]).mean()
        avg_dice = torch.stack([x['test_step_dice'] for x in outputs]).mean()
        avg_IoU = torch.stack([x['test_step_IoU'] for x in outputs]).mean()
        avg_sensitivity = torch.stack([x['test_step_sensitivity'] for x in outputs]).mean()
        avg_specificity = torch.stack([x['test_step_specificity'] for x in outputs]).mean()
        tensorboard_logs = {
            "avg_test_loss": avg_loss.item(),  # the outputs is a dict wrapped in a list
            "avg_test_dice": avg_dice.item(),
            "avg_test_IoU": avg_IoU.item(),
            "avg_test_sensitivity": avg_sensitivity.item(),
            "avg_test_specificity": avg_specificity.item(),
        }
        return {'log': tensorboard_logs}

    @staticmethod
    def add_model_specific_args(parent_parser: ArgumentParser) -> ArgumentParser:
        """
        parameters defined here will be available to the model through self.hparams
        """
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument("--batch_size", type=int, default=2, help='Batch size', dest='batch_size')
        # From the generalizedDiceLoss paper
        parser.add_argument("--learning_rate", type=float, default=1e-4, help='Learning rate')
        # parser.add_argument("--normalization", type=str, default='Group', help='the way of normalization')
        parser.add_argument("--down_sample", type=str, default="max", help="the way to down sample")
        parser.add_argument("--loss", type=str, default="BCEWL", help='Loss Function')
        return parser