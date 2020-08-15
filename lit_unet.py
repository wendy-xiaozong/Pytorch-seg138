from typing import Union, List, Dict, Optional
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
from pytorch_lightning.metrics.functional import dice_score, to_onehot


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
        self.max_queue_length = 1000
        self.patch_size = 96
        # Number of patches to extract from each volume. A small number of patches ensures a large variability
        # in the queue, but training will be slower.
        self.samples_per_volume = 10
        self.val_times = 0
        self.num_workers = 0
        if not self.hparams.cedar:
            self.num_workers = 0

        if not COMPUTECANADA:
            self.num_workers = 8
            self.subjects, self.visual_img_path_list, self.visual_label_path_list = get_processed_subjects(
                whether_use_cropped_and_resample_img=True
            )
            random.seed(42)
            random.shuffle(self.subjects)  # shuffle it to pick the val set
            num_subjects = len(self.subjects)
            num_training_subjects = int(num_subjects * 0.97)  # （5074+359+21） * 0.9 used for training
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
        num_training_subjects = int(num_subjects * 0.97)  # （5074+359+21） * 0.9 used for training
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
            # A small number of patches ensures a large variability in the queue,
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

        val_imageDataset = torchio.ImagesDataset(self.validation_subjects)

        # patches_validation_set = torchio.Queue(
        #     subjects_dataset=val_imageDataset,
        #     max_length=self.max_queue_length,
        #     samples_per_volume=self.samples_per_volume,
        #     sampler=torchio.sampler.UniformSampler(self.patch_size),
        #     num_workers=self.num_workers,
        #     shuffle_subjects=False,
        #     shuffle_patches=True,
        #     verbose=True,
        # )

        # the batch_size here only could be 1 because we only could handle one image to aggregate
        val_loader = DataLoader(val_imageDataset, batch_size=1)
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
        optimizer = torch.optim.Adam(self.parameters(), lr=self.hparams.learning_rate)
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
        # print(f"inputs shape: {inputs.shape}")
        pred = self(inputs)
        # diceloss = DiceLoss(include_background=True, to_onehot_y=True)
        # loss = diceloss.forward(input=probs, target=targets)
        # dice, iou, _, _ = get_score(batch_preds, batch_targets, include_background=True)
        # gdloss = GeneralizedDiceLoss(include_background=True, to_onehot_y=True)
        # loss = gdloss.forward(input=batch_preds, target=batch_targets)
        # if batch_idx != 0 and ((self.current_epoch >= 1 and dice.item() < 0.5) or batch_idx % 100 == 0):
        #     input = inputs.chunk(inputs.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     target = targets.chunk(targets.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     prob = probs.chunk(probs.size()[0], 0)[0]  # split into 1 in the dimension 0
        #     ＃　really have problem in there, need to fix it
        #     dice_score, _, _, _ = get_score(torch.unsqueeze(prob, 0), torch.unsqueeze(target, 0))
        #     log_all_info(self, input, target, prob, batch_idx, "training", dice_score.item())
        # loss = F.binary_cross_entropy_with_logits(logits, targets)
        # del inputs  # Just a Try
        diceloss = DiceLoss(include_background=True, to_onehot_y=True)
        loss = diceloss.forward(input=pred, target=targets)
        # gdloss = GeneralizedDiceLoss(include_background=True, to_onehot_y=True)
        # loss = gdloss.forward(input=batch_preds, target=batch_targets)
        return {
            'loss': loss,
            # we cannot compute the matrixs on the patches, because they do not contain all the 138 segmentations
            # So they would return 0 on some of the classes, making the matrixs not accurate
            # 'log': {'train_loss': loss, 'train_dice': dice, 'train_IoU': iou},
            'log': {'train_loss': loss},
            # 'progress_bar': {'train_loss': loss, 'train_dice': dice}
            # 'progress_bar': {'train_loss': loss}
        }

    # It supports only need when using DP or DDP2, I should not need it because I am using ddp
    # but I have some problem with the dice score, So I am just trying ...
    # def training_step_end(self, outputs) -> Dict[str, Union[Tensor, Dict[str, Tensor]]]:
    #     print(f"outputs shape: {outputs['train_step_preds'].shape}")
    #     batch_preds = torch.stack([x['train_step_preds'] for x in outputs])
    #     batch_targets = torch.stack([x['train_step_target'] for x in outputs])
        # dice, iou, _, _ = get_score(batch_preds, batch_targets, include_background=True)
        # dice = dice_score(pred=batch_preds, target=batch_targets, bg=True)

    def compute_from_aggregating(self, input, target, if_path: bool, type_as_tensor=None):
        if if_path:
            cur_img_subject = torchio.Subject(
                img=torchio.Image(input, type=torchio.INTENSITY)
            )
            cur_label_subject = torchio.Subject(
                img=torchio.Image(target, type=torchio.LABEL)
            )
        else:
            cur_img_subject = torchio.Subject(
                img=torchio.Image(tensor=input.squeeze().cpu().detach(), type=torchio.INTENSITY)
            )
            cur_label_subject = torchio.Subject(
                img=torchio.Image(tensor=target.squeeze().cpu().detach(), type=torchio.LABEL)
            )

        transform = get_val_transform()
        preprocessed_img = transform(cur_img_subject)
        preprocessed_label = transform(cur_label_subject)

        patch_overlap = 20  # is there any constrain?
        grid_sampler = torchio.inference.GridSampler(
            preprocessed_img,
            self.patch_size,
            patch_overlap,
        )

        patch_loader = torch.utils.data.DataLoader(grid_sampler)
        aggregator = torchio.inference.GridAggregator(grid_sampler)

        for patches_batch in patch_loader:
            input_tensor = patches_batch['img'][torchio.DATA]
            if not if_path:
                input_tensor = input_tensor.type_as(input)
            else:
                input_tensor = input_tensor.type_as(type_as_tensor[0]['val_step_dice'])
            locations = patches_batch[torchio.LOCATION]
            preds = self(input_tensor)  # use cuda
            labels = preds.argmax(dim=torchio.CHANNELS_DIMENSION, keepdim=True)  # use cuda
            aggregator.add_batch(labels, locations)
        output_tensor = aggregator.get_output_tensor()  # not using cuda!!!!

        if if_path:
            return preprocessed_img.img.data, output_tensor, preprocessed_label.img.data
        else:
            return output_tensor, preprocessed_label.img.data

    def validation_step(self, batch, batch_id):
        input, target = self.prepare_batch(batch)
        output_tensor, target_tensor = self.compute_from_aggregating(input, target, False)  # in CPU

        # pred = self(inputs)
        # gdloss = GeneralizedDiceLoss(include_background=True, to_onehot_y=True)
        # loss = gdloss.forward(input=probs, target=targets)

        diceloss = DiceLoss(include_background=True, to_onehot_y=True)
        output_tensor = to_onehot(output_tensor, num_classes=139)
        loss = diceloss.forward(input=output_tensor, target=target_tensor.unsqueeze(dim=1))  # all in CPU
        loss_cuda = loss.type_as(input)
        output_tensor_cuda = output_tensor.type_as(input)
        target_tensor_cuda = target_tensor.type_as(input)
        del output_tensor, target_tensor, loss, input, target
        dice, iou, sensitivity, specificity = get_score(output_tensor_cuda, target_tensor_cuda)
        return {'val_step_loss': loss_cuda,
                'val_step_dice': dice,
                'val_step_IoU': iou,
                "val_step_sensitivity": sensitivity,
                "val_step_specificity": specificity}

    # def validation_step_end(self, outputs) -> Dict[str, Tensor]:
    #     batch_preds = torch.stack([x['val_step_preds'] for x in outputs])
    #     batch_targets = torch.stack([x['val_step_target'] for x in outputs])

    # Called at the end of the validation epoch with the outputs of all validation steps.
    def validation_epoch_end(self, outputs):
        # visualization part
        cur_img_path = self.visual_img_path_list[self.val_times % len(self.visual_img_path_list)]
        cur_label_path = self.visual_label_path_list[self.val_times % len(self.visual_label_path_list)]

        img, output_tensor, target_tensor = self.compute_from_aggregating(cur_img_path, cur_label_path,
                                                                          if_path=True, type_as_tensor=outputs)
        # print(f"validation_epoch_end_output_tensor: {output_tensor.requires_grad}")
        # print(f"validation_epoch_end_target_tensor: {target_tensor.requires_grad}")
        output_tensor_cuda = output_tensor.type_as(outputs[0]['val_step_dice'])
        target_tensor_cuda = target_tensor.type_as(outputs[0]['val_step_dice'])
        del output_tensor, target_tensor
        # using CUDA
        dice, iou, sensitivity, specificity = get_score(pred=output_tensor_cuda, target=target_tensor_cuda)
        log_all_info(self,
                     img,
                     target_tensor_cuda,
                     output_tensor_cuda,
                     dice,
                     self.val_times)
        self.val_times += 1

        # torch.stack: Concatenates sequence of tensors along a new dimension.
        avg_loss = torch.stack([x['val_step_loss'] for x in outputs]).mean()
        avg_val_dice = torch.stack([x['val_step_dice'] for x in outputs]).mean()
        tensorboard_logs = {
            "val_loss": outputs[0]['val_step_loss'],  # to compare with train
            "val_dice": outputs[0]['val_step_dice'],
            "val_IoU": outputs[0]['val_step_IoU'],
            "val_sensitivity": outputs[0]['val_step_sensitivity'],
            "val_specificity": outputs[0]['val_step_specificity']
        }
        return {"loss": avg_loss, "val_loss": avg_loss, "val_dice": avg_val_dice, 'log': tensorboard_logs,
                'progress_bar': {'val_dice': avg_val_dice}}

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
        parser.add_argument("--learning_rate", type=float, default=1e-3, help='Learning rate')
        # parser.add_argument("--normalization", type=str, default='Group', help='the way of normalization')
        parser.add_argument("--down_sample", type=str, default="max", help="the way to down sample")
        parser.add_argument("--loss", type=str, default="BCEWL", help='Loss Function')
        return parser
