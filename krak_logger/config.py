"""Paths, environment loading and constants shared across the application."""

import os
import sys

import dotenv

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
# SCRIPT_DIR is the directory holding krak_logger_gui.py (the KRAK-logger
# project root), both when running from source and from a PyInstaller bundle.
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))   # mobile cookie crusher/
MODELS_DIR = os.path.join(BASE_DIR, "models")
FEATURE_EXTRACTION_DIR = os.path.join(SCRIPT_DIR, "feature_extraction")

TEMP_DIR = "temp_files"
CN0582_SETTINGS_FILE = os.path.join(SCRIPT_DIR, "cn0582_settings.json")

# ---------------------------------------------------------------------------
# Recording constants
# ---------------------------------------------------------------------------
DEFAULT_DURATION = 15.0           # seconds
BUCKET_NAME = "krak"
INDEX_FILE_NAME = "search_index.json"
FOCUSRITE_SAMPLE_RATE = 48000
STWINMA2_SAMPLE_RATE = 192_000
STWINMA2_BLOCK_SIZE = 2048

# ---------------------------------------------------------------------------
# CN0582 (EVAL-CN0582-USBZ, AD7768-4)
# ---------------------------------------------------------------------------
# Fixed by the board: 256 kSPS on four simultaneous 24-bit channels.
CN0582_SAMPLE_RATE = 256_000
# Longest fixed-duration clip the driver accepts; beyond this the logger uses
# manual-stop mode, which streams gaplessly until stopped.
CN0582_MAX_CLIP_S = 15.0
# 8.192 MB/s on the wire, and ~1 GB of RAM per captured minute once decoded to
# float64 -- warn before a manual-stop recording runs past this.
CN0582_LONG_RECORD_WARN_S = 60.0
# Front-end defaults, overridable per deployment via .env (see cn0582_daq).
CN0582_DEFAULT_GAINS = (1, 1, 1, 1)      # LTC6910 codes: 1 2 5 10 20 50 100
CN0582_DEFAULT_COUPLING = (1, 1, 1, 1)   # 1 = AC-coupled
# Current-source bit mapping: GUI channel index -> device bit index.
# CN0582 command bytes use bit0=CH0 .. bit3=CH3.
CN0582_CURRENT_SOURCE_BIT_FOR_CHANNEL = (0, 1, 2, 3)
CN0582_DEFAULT_IEPE_MASK = 0x01          # CH0 mic power on by default; others off
# AI1 carries the load cell, which changes far too slowly to be worth 256 kSPS on
# disk. It is the ONLY channel that may be stored at a reduced rate -- the others
# are audio and must keep every sample. Change this if the load cell is rewired.
CN0582_LOADCELL_CHANNEL = 1
# Rate the load-cell channel is stored at by default.
CN0582_LOADCELL_STORE_RATE = 32000


def find_env_file():
    """Find .env file in multiple possible locations for development and executable compatibility"""
    # Get the directory where the script/executable is located
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = SCRIPT_DIR

    # Try multiple locations in priority order
    possible_paths = [
        os.path.join(exe_dir, '.env'),           # Same directory as exe/script
        os.path.join(os.getcwd(), '.env'),       # Current working directory
        '.env'                                   # Relative to current dir (fallback)
    ]

    for path in possible_paths:
        if os.path.exists(path):
            print(f"Found .env file at: {path}")
            return path

    print("No .env file found, using default environment variables")
    return '.env'  # Fallback for dotenv.load_dotenv()


def load_env():
    """Load environment variables from the detected .env file."""
    dotenv.load_dotenv(find_env_file())


def ensure_temp_dir():
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
