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

# ---------------------------------------------------------------------------
# Recording constants
# ---------------------------------------------------------------------------
DEFAULT_DURATION = 15.0           # seconds
BUCKET_NAME = "krak"
INDEX_FILE_NAME = "search_index.json"
FOCUSRITE_SAMPLE_RATE = 48000
LANXI_FALLBACK_SAMPLE_RATE = 51200
STWINMA2_SAMPLE_RATE = 192_000
STWINMA2_BLOCK_SIZE = 2048
LANXI_CHUNK_DURATION = 2.0        # seconds per LAN-XI chunk in manual-stop mode


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
