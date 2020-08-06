from pathlib import Path
import os
import sys
import re

TMP = os.environ.get("SLURM_TMPDIR")  # run in compute canada, also in a job
ACT = os.environ.get("SLURM_ACCOUNT")  # run in compute canada, but not in a job

COMPUTECANADA = False
SIZE = 64

if TMP or ACT:  # running inside Compute Canada
    COMPUTECANADA = True
    SIZE = 128


# DATASET_DIR = Path("/project/6005889/U-Net_MRI-Data")
#
# CC359_DATASET_DIR = DATASET_DIR / "CalgaryCampinas359/Original"
# CC359_LABEL_DIR = DATASET_DIR / "CalgaryCampinas359/Skull-stripping-masks/STAPLE"
# CC359_MANUAL_LABEL_DIR = DATASET_DIR / "CalgaryCampinas359/Skull-stripping-masks/Manual"
#
# NFBS_DATASET_DIR = DATASET_DIR / "NFBS/NFBS_Dataset"
#
# ADNI_DATASET_DIR_1 = "/project/6005889/U-Net_MRI-Data/ADNI"
# ADNI_DATASET_DIR_2 = "/project/6005889/U-Net_MRI-Data/ADNI/ADNI"
#
# ADNI_LABEL = "brain_extraction"

if COMPUTECANADA:
    DATA_ROOT = Path(str(TMP)).resolve() / "work"
    # DATA_ROOT = Path("/project/6005889/U-Net_MRI-Data")
    processed_img_folder = DATA_ROOT / "img"
    processed_label_folder = DATA_ROOT / "label"
    cropped_img_folder = DATA_ROOT / "cropped_img"
    cropped_label_folder = DATA_ROOT / "cropped_label"
else:
    DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "Data"
    processed_folder = DATA_ROOT / "processed_ADNI"
    processed_img_folder = processed_folder / "img"
    processed_label_folder = processed_folder / "label"
    cropped_folder = DATA_ROOT / "cropped_resample"
    cropped_img_folder = cropped_folder / "img"
    cropped_label_folder = cropped_folder / "label"

CC359_DATASET_DIR = DATA_ROOT / "CalgaryCampinas359/Original"
CC359_LABEL_DIR = DATA_ROOT / "CalgaryCampinas359/Skull-stripping-masks/STAPLE"
CC359_MANUAL_LABEL_DIR = DATA_ROOT / "CalgaryCampinas359/Skull-stripping-masks/Manual"
NFBS_DATASET_DIR = DATA_ROOT / "NFBS/NFBS_Dataset"

ADNI_DATASET_DIR_1 = DATA_ROOT / "ADNI"
ADNI_DATASET_DIR_2 = DATA_ROOT / "ADNI/ADNI"

colors_path = Path(__file__).resolve().parent.parent.parent / "GIFNiftyNet.ctbl"

if COMPUTECANADA:
    ADNI_LABEL = ADNI_DATASET_DIR_1 / "cross-sectional_seg138"
else:
    ADNI_LABEL = DATA_ROOT / "label"