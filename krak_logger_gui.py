from HelpFunctions.lanxi import LanXI
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scipy.io.wavfile as wav
from scipy.signal import butter, sosfilt
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import threading
import queue
import time
import dotenv
import datetime
import sounddevice as sd
import os
import io
import json
import subprocess
import shutil
from minio import Minio
from minio.error import S3Error
from minio.commonconfig import CopySource
import atexit
import signal
import tkinter.font as tkfont
from sklearn.base import BaseEstimator, ClassifierMixin
from scipy.signal import find_peaks
from collections import defaultdict
from signal_processor import SignalProcessor

from mcu_protocol import MCUProtocol
from mcu_tabs import MCUController, _ts, scale_fonts, _F, _parse_field as _mcu_parse_field

# Global variables
recording = False
DURATION = 15  # Default duration (can be adjusted)
OUTPUT_WAV_FILE = "recorded_audio.wav"
OUTPUT_PARQUET_FILE = "recorded_data.parquet"
BUCKET_NAME = "krak" # Replace with your bucket name
INDEX_FILE_NAME = "search_index.json"
parameter_entries = {}
TEMP_DIR = "temp_files"
loaded_df = None  # For storing loaded sample data
current_sample_name = None  # For storing current sample name
excel_metadata_df = None  # For storing Excel metadata
excel_file_path = None  # For storing current Excel file path
loaded_excel_metadata = {}  # For storing currently loaded metadata from Excel
sort_by = "time"  # Default sort by time
sort_order = "desc"  # Default sort newest first
recorded_data = None
recorded_time_axis = None
recorded_loadcell = None        # (time_axis_s, raw_values) from MCU, or None
FOCUSRITE_SAMPLE_RATE = 48000
_audio_device_map: dict = {}    # display name -> sd device index; "LAN-XI" -> None
audio_source_var = None         # tk.StringVar, assigned during UI init
audio_source_combo = None       # ttk.Combobox, assigned during UI init
focusrite_sensitivity_var = None  # tk.StringVar V/FS, assigned during UI init
loadcell_enable_var = None      # tk.BooleanVar, assigned during UI init
hp_filter_var = None            # tk.BooleanVar, 1 kHz high-pass on playback
hp5k_filter_var = None          # tk.BooleanVar, 5 kHz high-pass on playback
STWINMA2_SAMPLE_RATE  = 192_000
STWINMA2_BLOCK_SIZE   = 2048
LANXI_CHUNK_DURATION  = 2.0     # seconds per LAN-XI chunk in manual-stop mode
recorded_stwinma2     = None    # float64 ndarray in [-1, 1] from float32 ch0, or None
recorded_stwin_offset = None    # seconds between gate-open and first captured sample
stwinma2_enable_var   = None    # tk.BooleanVar
stwinma2_device_var   = None    # tk.StringVar
stwinma2_device_combo = None    # ttk.Combobox
stop_indefinite       = threading.Event()   # set to end a manual-stop recording
manual_stop_var       = None    # tk.BooleanVar

# Smart .env path detection for both development and executable
def find_env_file():
    """Find .env file in multiple possible locations for development and executable compatibility"""
    # Get the directory where the script/executable is located
    if getattr(sys, 'frozen', False):
        # Running as executable
        exe_dir = os.path.dirname(sys.executable)
    else:
        # Running as script
        exe_dir = os.path.dirname(os.path.abspath(__file__))

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

# IP of Lan-XI
import sys  # Add sys import for executable detection
env_path = find_env_file()
dotenv.load_dotenv(env_path)
try:
    ip = os.getenv("BKDAQ_IP")
    Lanxi = LanXI(ip)
    Lanxi.setup_stream()
    SAMPLE_RATE = Lanxi.sample_rate
    NUM_SAMPLES = SAMPLE_RATE * DURATION
    LANXI_AVAILABLE = True
    atexit.register(Lanxi.close_stream)
    signal.signal(signal.SIGINT, lambda _s, _f: (Lanxi.close_stream(), sys.exit(0)))
except Exception as _lanxi_err:
    print(f"LAN-XI not available: {_lanxi_err}")
    Lanxi = None
    SAMPLE_RATE = 51200
    NUM_SAMPLES = SAMPLE_RATE * DURATION
    LANXI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Load cell data collection from MCU ADCSTREAM (200 Hz)
# ---------------------------------------------------------------------------

class LoadCellCollector:
    """Collects load cell data from MCU via LC_LOGGING at 200 Hz.

    Protocol (PC -> MCU): LC_LOGGING START <duration_s>
    Protocol (MCU -> PC): LC:START, LC:<value> x N, LC:END
    stop() blocks until LC:END is received so all buffered samples are captured.
    """

    def __init__(self, protocol):
        self._proto = protocol
        self._samples = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._duration = 0.0

    def start(self, duration_s):
        self._duration = duration_s
        with self._lock:
            self._samples = []
        self._done.clear()
        self._proto.add_callback(self._on_line)
        self._proto.send(f"LC_LOGGING START {int(round(duration_s))}")

    def stop(self):
        """Wait for LC:END from MCU, then return (time_axis_s, raw_values).

        Blocks until the MCU signals LC:END (all samples transmitted) or
        until a timeout expires (fallback for old firmware without LC:END support).
        """
        # MCU flushes its UART buffer after sending LC:END; give it generous time.
        timeout = self._duration + 10.0
        self._done.wait(timeout=timeout)
        self._proto.remove_callback(self._on_line)
        with self._lock:
            data = list(self._samples)
        if not data:
            return None, None
        values = np.array(data, dtype=float)
        times = np.arange(len(values)) / 200.0
        return times, values

    def _on_line(self, line):
        if line.strip() == "LC:START":
            with self._lock:
                self._samples = []  # reset in case of re-start
        elif line.strip() == "LC:END":
            self._done.set()
        elif line.startswith("LC:"):
            try:
                with self._lock:
                    self._samples.append(float(line[3:].strip()))
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Disk-backed audio buffer — avoids accumulating 96kHz frames in RAM
# ---------------------------------------------------------------------------

class _DiskBuffer:
    """Streams audio frames to a temp file via a background writer thread.

    The callback path only enqueues a memoryview copy; the background thread
    does all I/O so the audio callback stays non-blocking.

    dtype: numpy dtype used for on-disk storage (np.int16 or np.float32).
    """

    def __init__(self, path, dtype=np.int16):
        self._path    = path
        self._dtype   = dtype
        self._bps     = np.dtype(dtype).itemsize   # bytes per sample
        self._q       = queue.Queue()
        self._fh      = open(path, 'wb')
        self._count   = 0          # samples written (updated by writer thread)
        self._thread  = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()

    # Called from audio callback — must be fast
    def push(self, chunk):
        self._q.put(chunk.tobytes())

    @property
    def sample_count(self):
        return self._count

    def _writer(self):
        while True:
            item = self._q.get()
            if item is None:          # sentinel → shut down
                break
            self._fh.write(item)
            self._count += len(item) // self._bps

    def finish(self):
        """Stop the writer thread and flush/close the file."""
        self._q.put(None)
        self._thread.join()
        self._fh.close()

    def read_float(self):
        """Return all recorded samples as a float64 array in [-1, 1], then delete the file."""
        raw = np.fromfile(self._path, dtype=self._dtype).astype(np.float64)
        if self._dtype == np.int16:
            raw /= 32768.0
        try:
            os.remove(self._path)
        except OSError:
            pass
        return raw

    def discard(self):
        """Finish without reading; delete the temp file."""
        self.finish()
        try:
            os.remove(self._path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Audio device enumeration for Focusrite / sounddevice
# ---------------------------------------------------------------------------

def list_audio_input_devices():
    """Return list of (index, name) for all input-capable audio devices."""
    try:
        sd._terminate()
        sd._initialize()
        return [(i, d['name']) for i, d in enumerate(sd.query_devices())
                if d['max_input_channels'] > 0]
    except Exception:
        return []


def refresh_audio_devices():
    """Repopulate the audio source combobox with LAN-XI + STWINMA2 + detected input devices."""
    global _audio_device_map
    _audio_device_map = {"LAN-XI": None, "STWINMA2 Ch0 (192 kHz)": "__stwinma2__"}

    # Identify all STWINMA2 device indices (MME + WASAPI instances of the same
    # physical device) so every duplicate is excluded from the generic dropdown.
    _stwin_indices = set()
    try:
        _keywords = ('stwin', 'steval', 'stm32')
        for i, d in enumerate(sd.query_devices()):
            if d['max_input_channels'] >= 1:
                if any(k in d['name'].lower() for k in _keywords):
                    _stwin_indices.add(i)
    except Exception:
        pass

    choices = ["LAN-XI", "STWINMA2 Ch0 (192 kHz)"]
    stwin_choices = ["Auto-detect"]
    for idx, name in list_audio_input_devices():
        key = f"{name} [{idx}]"
        _audio_device_map[key] = idx
        stwin_choices.append(key)          # all input devices selectable as STWINMA2 target
        if idx not in _stwin_indices:      # exclude all STWINMA2 instances from generic dropdown
            choices.append(key)

    if audio_source_combo is not None:
        audio_source_combo.configure(values=choices)
        if audio_source_var is not None and audio_source_var.get() not in choices:
            audio_source_var.set("LAN-XI")
    if stwinma2_device_combo is not None:
        stwinma2_device_combo.configure(values=stwin_choices)
        if stwinma2_device_var is not None and stwinma2_device_var.get() not in stwin_choices:
            stwinma2_device_var.set("Auto-detect")


def _find_stwinma2_device():
    """Return sounddevice index for STWINMA2, or None if not found.

    Prefers the WASAPI host-API instance of the device (supports 192 kHz on
    Windows).  Falls back to the last name-matched entry in the device list,
    which on Windows is also WASAPI (MME/DS come first, WASAPI last).
    """
    sel = stwinma2_device_var.get() if stwinma2_device_var else "Auto-detect"
    if sel and sel != "Auto-detect":
        idx = _audio_device_map.get(sel)
        if idx is not None:
            print(f"STWINMA2: using explicit selection '{sel}' → device {idx}")
            return idx
    try:
        devices  = sd.query_devices()
        hostapis = sd.query_hostapis()
        _kw = ('stwin', 'steval', 'stm32')

        # Collect all name-matched input devices with their host-API name
        candidates = []
        for i, d in enumerate(devices):
            if d['max_input_channels'] >= 1:
                n = d['name'].lower()
                if any(k in n for k in _kw):
                    ha = hostapis[d['hostapi']]['name']
                    candidates.append((i, d['name'], ha))
                    print(f"STWINMA2 candidate [{i}] {d['name']}  hostapi={ha}")

        # Priority 1: WASAPI instance
        for i, name, ha in candidates:
            if 'wasapi' in ha.lower():
                print(f"STWINMA2: auto-selected [{i}] {name} (WASAPI)")
                return i

        # Priority 2: last in list (Windows orders MME→DS→WASAPI, so last ≈ WASAPI)
        if candidates:
            i, name, ha = candidates[-1]
            print(f"STWINMA2: auto-selected [{i}] {name} (last candidate, hostapi={ha})")
            return i

    except Exception as e:
        print(f"STWINMA2 device search error: {e}")
    return None

def select_excel_file():
    global excel_metadata_df, excel_file_path
    # Start from user's home directory or Documents folder for easier navigation
    initial_dir = os.path.expanduser("~")
    if os.path.exists(os.path.join(initial_dir, "Documents")):
        initial_dir = os.path.join(initial_dir, "Documents")

    file_path = filedialog.askopenfilename(
        title="Select Excel Metadata File",
        filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
        initialdir=initial_dir
    )
    if file_path:
        try:
            excel_metadata_df = pd.read_excel(file_path)
            excel_file_path = file_path
            excel_file_label.config(text=f"Excel file: {os.path.basename(file_path)}")
            messagebox.showinfo("Success", f"Excel file loaded successfully.\nColumns: {list(excel_metadata_df.columns)}")
            # Clear any previously loaded metadata
            clear_excel_metadata_display()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load Excel file: {str(e)}")
            excel_metadata_df = None
            excel_file_path = None
            excel_file_label.config(text="No Excel file selected")

def load_metadata_from_excel():
    global loaded_excel_metadata
    if excel_metadata_df is None:
        messagebox.showwarning("No Excel File", "Please select an Excel file first.")
        return

    ref_number = ref_number_entry.get().strip()
    if not ref_number:
        messagebox.showwarning("Invalid Input", "Please enter a reference number.")
        return

    try:
        # Convert ref_number to appropriate type for comparison
        try:
            ref_num = int(ref_number)
        except ValueError:
            ref_num = ref_number

        # Find the row with matching reference number (first column)
        ref_col = excel_metadata_df.columns[0]
        matching_rows = excel_metadata_df[excel_metadata_df[ref_col] == ref_num]

        if matching_rows.empty:
            # Try string comparison if int comparison failed
            matching_rows = excel_metadata_df[excel_metadata_df[ref_col].astype(str) == str(ref_number)]

        if matching_rows.empty:
            messagebox.showwarning("Not Found", f"Reference number '{ref_number}' not found in Excel file.")
            return

        # Get the first matching row
        row = matching_rows.iloc[0]

        # Load metadata from all columns except the first (reference) column
        loaded_excel_metadata.clear()
        for col in excel_metadata_df.columns[1:]:
            value = row[col]
            if pd.notna(value):  # Only add non-empty values
                loaded_excel_metadata[col] = str(value)

        # Add automatic timestamp and date
        current_time = datetime.datetime.now()
        loaded_excel_metadata['Date'] = current_time.strftime("%Y-%m-%d")
        loaded_excel_metadata['Timestamp'] = current_time.strftime("%Y-%m-%d %H:%M:%S")

        # Display loaded metadata for verification
        display_excel_metadata()

    except Exception as e:
        messagebox.showerror("Error", f"Failed to load metadata: {str(e)}")

def display_excel_metadata():
    excel_metadata_text.delete("1.0", tk.END)
    for key, value in loaded_excel_metadata.items():
        excel_metadata_text.insert(tk.END, f"{key}: {value}\n")
    _refresh_edit_field_combo()

def _refresh_edit_field_combo():
    fields = [k for k in loaded_excel_metadata if k not in ('Date', 'Timestamp')]
    edit_field_combo.configure(values=fields)
    if fields and edit_field_var.get() not in fields:
        edit_field_var.set(fields[0])
        edit_value_var.set(loaded_excel_metadata.get(fields[0], ""))

def _on_edit_field_selected(event=None):
    key = edit_field_var.get()
    edit_value_var.set(loaded_excel_metadata.get(key, ""))

def _apply_metadata_edit():
    key = edit_field_var.get()
    if not key:
        return
    loaded_excel_metadata[key] = edit_value_var.get()
    display_excel_metadata()
    edit_field_combo.focus()

def advance_to_next_reference():
    """Move ref_number_entry to the next row in the Excel file and reload metadata."""
    if excel_metadata_df is None:
        return
    ref_col = excel_metadata_df.columns[0]
    current = ref_number_entry.get().strip()
    if not current:
        return
    try:
        current_val = int(current)
    except ValueError:
        current_val = current
    # Find the row index of the current reference
    matches = excel_metadata_df.index[excel_metadata_df[ref_col] == current_val].tolist()
    if not matches:
        matches = excel_metadata_df.index[
            excel_metadata_df[ref_col].astype(str) == str(current)
        ].tolist()
    if not matches:
        return
    next_idx = matches[0] + 1
    if next_idx >= len(excel_metadata_df):
        return   # already at last reference
    next_ref = str(excel_metadata_df.iloc[next_idx][ref_col])
    ref_number_entry.delete(0, tk.END)
    ref_number_entry.insert(0, next_ref)
    load_metadata_from_excel()
    messagebox.showinfo("Next Reference", f"Reference advanced to: {next_ref}")


def clear_excel_metadata_display():
    global loaded_excel_metadata
    loaded_excel_metadata.clear()
    excel_metadata_text.delete("1.0", tk.END)
    ref_number_entry.delete(0, tk.END)
    # Clear additional metadata entries
    for widget in additional_metadata_frame.winfo_children():
        widget.destroy()

def add_additional_metadata_field():
    frame = tk.Frame(additional_metadata_frame)
    frame.pack(fill=tk.X, padx=5, pady=2)

    tk.Label(frame, text="Key:").pack(side=tk.LEFT)
    key_entry = tk.Entry(frame, width=10)
    key_entry.pack(side=tk.LEFT, padx=(2, 5))

    tk.Label(frame, text="Value:").pack(side=tk.LEFT)
    value_entry = tk.Entry(frame, width=15)
    value_entry.pack(side=tk.LEFT, padx=(2, 5))

    remove_btn = tk.Button(frame, text="Remove", command=lambda: frame.destroy())
    remove_btn.pack(side=tk.LEFT, padx=(5, 0))






def get_all_metadata():
    """Combine Excel metadata with additional metadata fields"""
    all_metadata = loaded_excel_metadata.copy()

    # Add additional metadata from manual entry fields
    for frame in additional_metadata_frame.winfo_children():
        entries = [w for w in frame.winfo_children() if isinstance(w, tk.Entry)]
        if len(entries) >= 2:
            key = entries[0].get().strip()
            value = entries[1].get().strip()
            if key and value:
                all_metadata[key] = value

    return all_metadata

def update_parameters():
    global parameter_entries
    for widget in parameter_frame.winfo_children():
        widget.destroy()

    parameters = measurement_entry.get().split(',')
    parameter_entries.clear()
    for param in parameters:
        param = param.strip()
        if param:
            frame = tk.Frame(parameter_frame)
            frame.pack(fill=tk.X, padx=5, pady=2)
            tk.Label(frame, text=param + ":").pack(side=tk.LEFT)
            entry = tk.Entry(frame)
            entry.pack(side=tk.LEFT, expand=True, fill=tk.X)
            parameter_entries[param] = entry


def generate_filenames():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # Check if we have Excel metadata with ID field
    if loaded_excel_metadata and 'ID' in loaded_excel_metadata:
        id_value = loaded_excel_metadata['ID']
        base_name = f"{timestamp}-({id_value})"
    else:
        # Fallback to timestamp if no Excel metadata
        base_name = f"recording_{timestamp}"

    wav_filename = f"{base_name}.wav"
    parquet_filename = f"{base_name}.parquet"
    return wav_filename, parquet_filename


def ensure_temp_dir():
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)


def record_data(on_daq_ready=None):
    global recording, NUM_SAMPLES, DURATION, OUTPUT_WAV_FILE, OUTPUT_PARQUET_FILE
    global recorded_data, recorded_time_axis, recorded_loadcell, recorded_stwinma2, recorded_stwin_offset

    source      = audio_source_var.get() if audio_source_var is not None else "LAN-XI"
    manual_mode = manual_stop_var is not None and manual_stop_var.get()

    if source == "LAN-XI" and not LANXI_AVAILABLE:
        messagebox.showerror("LAN-XI Not Available",
                             "No LAN-XI device connected. Check BKDAQ_IP in .env and restart.")
        return

    if not manual_mode:
        try:
            DURATION = float(duration_entry.get())
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter a valid number for duration.")
            return

    OUTPUT_WAV_FILE, OUTPUT_PARQUET_FILE = generate_filenames()
    ensure_temp_dir()
    OUTPUT_WAV_FILE     = os.path.join(TEMP_DIR, OUTPUT_WAV_FILE)
    OUTPUT_PARQUET_FILE = os.path.join(TEMP_DIR, OUTPUT_PARQUET_FILE)

    recording    = True
    _lc_duration = 86400.0 if manual_mode else DURATION   # effectively indefinite for manual stop
    lc_collector = None
    lc_enabled   = loadcell_enable_var is not None and loadcell_enable_var.get()

    # --- STWINMA2 simultaneous capture setup (skipped when STWINMA2 is the primary source) ---
    stwin_enabled    = (stwinma2_enable_var is not None and stwinma2_enable_var.get()
                        and source != "STWINMA2 Ch0 (192 kHz)")
    stwin_buf        = None
    stwin_stream     = None
    stwin_gate       = threading.Event()   # set() when LAN-XI on_ready fires
    stwin_gate_t     = [None]              # wall-clock time gate was opened
    stwin_first_t    = [None]              # wall-clock time of first captured sample

    def _stwin_callback(indata, frames_count, time_info, status):
        if status:
            print(f"STWINMA2: {status}")
        if stwin_gate.is_set() and stwin_buf is not None:
            if stwin_first_t[0] is None:
                stwin_first_t[0] = time.perf_counter()
            stwin_buf.push(indata[:, 0].copy())

    if stwin_enabled:
        _sidx = _find_stwinma2_device()
        if _sidx is None:
            messagebox.showwarning("STWINMA2 Not Found",
                                   "Could not find a USB audio input (STWINMA2). "
                                   "Recording without STWINMA2.")
            stwin_enabled = False
        else:
            try:
                _stwin_raw_path = os.path.join(TEMP_DIR, f"_stwin_{int(time.time()*1000)}.raw")
                stwin_buf    = _DiskBuffer(_stwin_raw_path, dtype=np.float32)
                stwin_stream = sd.InputStream(
                    device=_sidx, samplerate=STWINMA2_SAMPLE_RATE,
                    channels=1, dtype='float32', blocksize=STWINMA2_BLOCK_SIZE,
                    callback=_stwin_callback,
                )
                # Start early so hardware is warm before LAN-XI is ready.
                # The gate keeps the buffer closed until on_ready fires.
                stwin_stream.start()
            except Exception as stwin_err:
                messagebox.showwarning("STWINMA2 Error",
                                       f"Failed to open STWINMA2 stream:\n{stwin_err}\n"
                                       "Recording without STWINMA2.")
                stwin_enabled = False
                stwin_stream  = None
                if stwin_buf is not None:
                    stwin_buf.discard()
                    stwin_buf = None

    time_axis = None
    data      = None

    try:
        # ── LAN-XI ──────────────────────────────────────────────────────────────
        if source == "LAN-XI":
            if manual_mode:
                # Record in fixed-length chunks until stop_indefinite is set
                all_times   = []
                all_ch      = [[], [], [], []]
                t_offset    = 0.0
                first_chunk = True

                while not stop_indefinite.is_set():
                    def _ready_first(orig=on_daq_ready):
                        nonlocal lc_collector
                        stwin_gate_t[0] = time.perf_counter()
                        stwin_gate.set()   # open buffer gate — hardware already warm
                        if lc_enabled and mcu_protocol is not None and mcu_protocol.connected:
                            lc_collector = LoadCellCollector(mcu_protocol)
                            lc_collector.start(_lc_duration)
                        if orig is not None:
                            orig()
                    try:
                        t_c, d_c = Lanxi.SampleChannels(
                            LANXI_CHUNK_DURATION,
                            on_ready=_ready_first if first_chunk else None,
                        )
                    except Exception as _chunk_err:
                        if not stop_indefinite.is_set():
                            recording = False
                            messagebox.showerror("Recording Error",
                                                 f"LAN-XI error:\n\n{_chunk_err}")
                            return
                        break
                    all_times.append(t_c + t_offset)
                    for i in range(4):
                        all_ch[i].append(d_c[i])
                    t_offset += LANXI_CHUNK_DURATION
                    first_chunk = False

                if not all_times:
                    recording = False
                    return
                time_axis = np.concatenate(all_times)
                data      = [np.concatenate(all_ch[i]) for i in range(4)]
                DURATION  = float(time_axis[-1]) if len(time_axis) else 0.0

            else:
                try:
                    def _daq_ready_wrapper(orig=on_daq_ready):
                        nonlocal lc_collector
                        stwin_gate_t[0] = time.perf_counter()
                        stwin_gate.set()   # open buffer gate — hardware already warm
                        if lc_enabled and mcu_protocol is not None and mcu_protocol.connected:
                            lc_collector = LoadCellCollector(mcu_protocol)
                            lc_collector.start(DURATION)
                        if orig is not None:
                            orig()
                    time_axis, data = Lanxi.SampleChannels(DURATION, on_ready=_daq_ready_wrapper)
                except ConnectionRefusedError:
                    recording = False
                    messagebox.showerror("Connection Error",
                                       "Failed to connect to LAN-XI device.\n\n"
                                       "The device may be busy from a previous recording.\n"
                                       "Attempting to reset the connection...")
                    try:
                        Lanxi.reset_stream()
                        time_axis, data = Lanxi.SampleChannels(DURATION)
                    except Exception as retry_error:
                        messagebox.showerror("Connection Failed",
                                           f"Could not establish connection after reset.\n\n"
                                           f"Error: {retry_error}\n\n"
                                           f"Try restarting the application or power cycle the LAN-XI device.")
                        return
                except Exception as e:
                    recording = False
                    messagebox.showerror("Recording Error", f"An error occurred during recording:\n\n{e}")
                    return

        # ── STWINMA2-only ────────────────────────────────────────────────────────
        elif source == "STWINMA2 Ch0 (192 kHz)":
            stwin_idx_only = _find_stwinma2_device()
            if stwin_idx_only is None:
                recording = False
                messagebox.showerror("STWINMA2 Not Found",
                                     "Could not find a USB audio input (STWINMA2).\n"
                                     "Check connection and click Refresh.")
                return

            _so_raw_path = os.path.join(TEMP_DIR, f"_stwin_only_{int(time.time()*1000)}.raw")
            so_buf = _DiskBuffer(_so_raw_path, dtype=np.float32)

            def _stwin_only_cb(indata, fc, ti, status):
                if status:
                    print(f"STWINMA2: {status}")
                so_buf.push(indata[:, 0].copy())

            try:
                with sd.InputStream(device=stwin_idx_only, samplerate=STWINMA2_SAMPLE_RATE,
                                    channels=1, dtype='float32', blocksize=STWINMA2_BLOCK_SIZE,
                                    callback=_stwin_only_cb):
                    if lc_enabled and mcu_protocol is not None and mcu_protocol.connected:
                        lc_collector = LoadCellCollector(mcu_protocol)
                        lc_collector.start(_lc_duration)
                    if on_daq_ready is not None:
                        on_daq_ready()
                    if manual_mode:
                        stop_indefinite.wait()
                    else:
                        n_expected = int(STWINMA2_SAMPLE_RATE * DURATION)
                        while so_buf.sample_count < n_expected:
                            time.sleep(0.05)

                so_buf.finish()
                raw = so_buf.read_float()
                if not manual_mode and len(raw) > int(STWINMA2_SAMPLE_RATE * DURATION):
                    raw = raw[:int(STWINMA2_SAMPLE_RATE * DURATION)]

                n_samples  = len(raw)
                actual_dur = n_samples / STWINMA2_SAMPLE_RATE
                if manual_mode:
                    DURATION = actual_dur
                time_axis = np.linspace(0, actual_dur, n_samples, endpoint=False)
                data = [raw,
                        np.full(n_samples, np.nan),
                        np.full(n_samples, np.nan),
                        np.full(n_samples, np.nan)]
            except Exception as e:
                so_buf.discard()
                recording = False
                messagebox.showerror("Recording Error", f"STWINMA2 recording failed:\n\n{e}")
                return

        # ── Focusrite / generic sounddevice ──────────────────────────────────────
        else:
            device_idx = _audio_device_map.get(source)
            try:
                sensitivity = float(focusrite_sensitivity_var.get()) if focusrite_sensitivity_var else 1.0
            except (ValueError, tk.TclError):
                sensitivity = 1.0

            sr = FOCUSRITE_SAMPLE_RATE

            if manual_mode:
                fo_frames  = []
                fo_lock    = threading.Lock()

                def _fo_cb(indata, fc, ti, status):
                    if status:
                        print(f"Audio: {status}")
                    with fo_lock:
                        fo_frames.append(indata[:, 0].copy())

                try:
                    with sd.InputStream(device=device_idx, samplerate=sr, channels=1,
                                        dtype='float32', callback=_fo_cb):
                        if lc_enabled and mcu_protocol is not None and mcu_protocol.connected:
                            lc_collector = LoadCellCollector(mcu_protocol)
                            lc_collector.start(_lc_duration)
                        if on_daq_ready is not None:
                            on_daq_ready()
                        stop_indefinite.wait()

                    with fo_lock:
                        _all_fo = fo_frames[:]
                    raw_fo    = np.concatenate(_all_fo) if _all_fo else np.array([], dtype=np.float32)
                    ai0       = raw_fo.astype(float) * sensitivity
                    n_samples = len(ai0)
                    DURATION  = n_samples / sr
                    time_axis = np.linspace(0, DURATION, n_samples, endpoint=False)
                    data = [ai0,
                            np.full(n_samples, np.nan),
                            np.full(n_samples, np.nan),
                            np.full(n_samples, np.nan)]
                except Exception as e:
                    recording = False
                    messagebox.showerror("Recording Error", f"Recording failed:\n\n{e}")
                    return
            else:
                n_samples = int(sr * DURATION)
                try:
                    audio_raw = sd.rec(n_samples, samplerate=sr, channels=1,
                                       device=device_idx, dtype="float32")
                    if lc_enabled and mcu_protocol is not None and mcu_protocol.connected:
                        lc_collector = LoadCellCollector(mcu_protocol)
                        lc_collector.start(DURATION)
                    if on_daq_ready is not None:
                        on_daq_ready()
                    sd.wait()
                    ai0 = audio_raw[:, 0].astype(float) * sensitivity
                    time_axis = np.linspace(0, DURATION, n_samples, endpoint=False)
                    data = [ai0,
                            np.full(n_samples, np.nan),
                            np.full(n_samples, np.nan),
                            np.full(n_samples, np.nan)]
                except Exception as e:
                    recording = False
                    messagebox.showerror("Recording Error", f"Focusrite recording failed:\n\n{e}")
                    return

    finally:
        # Stop simultaneous STWINMA2 stream
        if stwin_stream is not None:
            try:
                stwin_stream.stop()
                stwin_stream.close()
            except Exception:
                pass

        # Collect simultaneous STWINMA2 data from disk buffer
        if stwin_buf is not None:
            stwin_buf.finish()
            if stwin_enabled and stwin_buf.sample_count > 0:
                recorded_stwinma2 = stwin_buf.read_float()
                if stwin_gate_t[0] is not None and stwin_first_t[0] is not None:
                    recorded_stwin_offset = stwin_first_t[0] - stwin_gate_t[0]
                    print(f"STwin capture lag after gate: {recorded_stwin_offset*1000:.1f} ms")
                else:
                    recorded_stwin_offset = None
            else:
                stwin_buf.discard()
                recorded_stwinma2 = None
            stwin_buf = None
        else:
            recorded_stwinma2 = None

        # Always stop LC_LOGGING stream, even on error
        if lc_collector is not None:
            lc_t, lc_v = lc_collector.stop()
            recorded_loadcell = (lc_t, lc_v) if lc_t is not None else None
        else:
            recorded_loadcell = None

        # Reset record button when a manual-stop recording finishes
        if manual_mode:
            root.after(0, lambda: record_button.config(
                text="Start Recording", bg="SystemButtonFace", fg="black",
                command=start_recording, state="normal"))

    if time_axis is None or data is None:
        return

    # Resample load cell onto audio time axis for plotting and storage
    lc_resampled = None
    if recorded_loadcell is not None:
        lc_t, lc_v = recorded_loadcell
        lc_resampled = np.interp(time_axis, lc_t, lc_v,
                                  left=float("nan"), right=float("nan"))

    update_plot(time_axis, data, lc_resampled, stwinma2=recorded_stwinma2)

    recorded_data = data
    recorded_time_axis = time_axis
    recording = False

    # Write WAV from AI0
    if source == "LAN-XI":
        _sr, max_v = SAMPLE_RATE, 10.0
    elif source == "STWINMA2 Ch0 (192 kHz)":
        _sr, max_v = STWINMA2_SAMPLE_RATE, 1.0
    else:
        _sr = FOCUSRITE_SAMPLE_RATE
        try:
            sensitivity = float(focusrite_sensitivity_var.get()) if focusrite_sensitivity_var else 1.0
        except (ValueError, tk.TclError):
            sensitivity = 1.0
        max_v = max(sensitivity, 1e-9)
    audio_int16 = np.nan_to_num(data[0] / max_v * 32767, nan=0).astype(np.int16)
    wav.write(OUTPUT_WAV_FILE, _sr, audio_int16)

    # Write STWINMA2 Ch0 WAV at native 192 kHz (int16, normalised)
    stwin_wav_path = OUTPUT_WAV_FILE.replace('.wav', '_stwinma2.wav')
    if recorded_stwinma2 is not None:
        stwin_int16 = (np.clip(recorded_stwinma2, -1.0, 1.0) * 32767).astype(np.int16)
        wav.write(stwin_wav_path, STWINMA2_SAMPLE_RATE, stwin_int16)
    elif source == "STWINMA2 Ch0 (192 kHz)":
        # standalone source — data[0] is already the normalised ch0 signal
        stwin_int16 = (np.clip(data[0], -1.0, 1.0) * 32767).astype(np.int16)
        wav.write(stwin_wav_path, STWINMA2_SAMPLE_RATE, stwin_int16)

    # Save parquet immediately so it is available locally before MinIO upload
    save_to_parquet(lc_resampled)

    # Make upload button red to indicate data needs to be uploaded
    upload_button.config(bg="red", fg="white")

def save_to_parquet(lc_resampled=None):
    if recorded_data is not None:
        source = audio_source_var.get() if audio_source_var is not None else "LAN-XI"
        if source == "LAN-XI":
            _sr = SAMPLE_RATE
        elif source == "STWINMA2 Ch0 (192 kHz)":
            _sr = STWINMA2_SAMPLE_RATE
        else:
            _sr = FOCUSRITE_SAMPLE_RATE

        metadata = get_all_metadata()
        metadata.update({
            "Sample Rate (Hz)": _sr,
            "Audio Source": source,
        })

        for param, entry in parameter_entries.items():
            metadata[param] = entry.get()

        df_dict = {
            "Time (s)": recorded_time_axis,
            "AI0 (V)": recorded_data[0],
            "AI1 (V)": recorded_data[1],
            "AI2 (V)": recorded_data[2],
            "AI3 (mV)": recorded_data[3] * 1000,
        }
        if lc_resampled is not None:
            df_dict["Load Cell (mV)"] = lc_resampled

        # STwin runs at 192 kHz vs LAN-XI at ~51 kHz, so AI04 has ~3.75x more rows.
        # LAN-XI columns are NaN-padded to match — ~75 % of those rows will be NaN.
        # Parquet snappy compresses NaN runs well, but the file is still larger.
        if recorded_stwinma2 is not None:
            n_stwin = len(recorded_stwinma2)
            n_lanxi = len(recorded_time_axis)
            if n_stwin > n_lanxi:
                pad = n_stwin - n_lanxi
                for k in list(df_dict.keys()):
                    df_dict[k] = np.concatenate([
                        np.asarray(df_dict[k], dtype=np.float64),
                        np.full(pad, np.nan),
                    ])
            stwin_time = np.linspace(0, n_stwin / STWINMA2_SAMPLE_RATE,
                                     n_stwin, endpoint=False)
            df_dict["AI04 Time (s)"] = stwin_time
            df_dict["AI04 (norm)"]   = recorded_stwinma2
            metadata["STWINMA2 Sample Rate (Hz)"] = STWINMA2_SAMPLE_RATE
            if recorded_stwin_offset is not None:
                metadata["STWINMA2 Start Offset (s)"] = round(recorded_stwin_offset, 6)

        df = pd.DataFrame(df_dict)
        df.attrs.update(metadata)
        df.to_parquet(OUTPUT_PARQUET_FILE, index=False)
        print(f"Data saved as {OUTPUT_PARQUET_FILE} with metadata")
    else:
        messagebox.showwarning("No Data", "No recorded data to save.")

def upload_to_minio():
    # check if file exists
    if not os.path.exists(OUTPUT_PARQUET_FILE) or not os.path.exists(OUTPUT_WAV_FILE):
        save_to_parquet()

    parquet_basename = os.path.basename(OUTPUT_PARQUET_FILE)
    wav_basename = os.path.basename(OUTPUT_WAV_FILE)

    def upload_worker():
        try:
            # Disable upload button during operation
            root.after(0, lambda: upload_button.config(text="Uploading...", state="disabled"))

            minio_client = create_minio_client()

            # Get file sizes for verification
            parquet_size = os.path.getsize(OUTPUT_PARQUET_FILE)
            wav_size = os.path.getsize(OUTPUT_WAV_FILE)
            stwin_wav_path     = OUTPUT_WAV_FILE.replace('.wav', '_stwinma2.wav')
            stwin_wav_basename = os.path.basename(stwin_wav_path)
            has_stwin_wav      = os.path.exists(stwin_wav_path)

            # Direct upload of files
            root.after(0, lambda: upload_button.config(text="Uploading parquet..."))
            minio_client.fput_object(BUCKET_NAME, parquet_basename, OUTPUT_PARQUET_FILE)

            root.after(0, lambda: upload_button.config(text="Uploading WAV..."))
            minio_client.fput_object(BUCKET_NAME, wav_basename, OUTPUT_WAV_FILE)

            if has_stwin_wav:
                root.after(0, lambda: upload_button.config(text="Uploading STWINMA2 WAV..."))
                minio_client.fput_object(BUCKET_NAME, stwin_wav_basename, stwin_wav_path)

            # Verify uploaded files
            root.after(0, lambda: upload_button.config(text="Verifying upload..."))
            final_parquet_obj = minio_client.stat_object(BUCKET_NAME, parquet_basename)
            final_wav_obj = minio_client.stat_object(BUCKET_NAME, wav_basename)

            if final_parquet_obj.size != parquet_size:
                raise Exception("Parquet file verification failed - size mismatch")

            if final_wav_obj.size != wav_size:
                raise Exception("WAV file verification failed - size mismatch")

            print(f"Upload verification successful")

            # Update search index
            def success_update():
                upload_button.config(text="Upload to MinIO", state="normal", bg="SystemButtonFace", fg="black")

                # Update search index with metadata
                metadata = get_all_metadata()
                if metadata and os.path.exists(OUTPUT_PARQUET_FILE):
                    try:
                        df = pd.read_parquet(OUTPUT_PARQUET_FILE)
                        update_search_index_on_server(parquet_basename, metadata, df)
                    except Exception as e:
                        print(f"Warning: Search index update failed: {e}")

                extra = f"\nSTWINMA2 WAV: {stwin_wav_basename}" if has_stwin_wav else ""
                messagebox.showinfo("Upload Complete",
                                  f"Files uploaded to MinIO successfully!\n\n"
                                  f"Parquet: {parquet_basename}\n"
                                  f"WAV: {wav_basename}{extra}")

                # Advance to the next reference number and load its metadata
                advance_to_next_reference()

                # Get the uploaded file base name (without extension)
                uploaded_file_base = os.path.splitext(parquet_basename)[0]

                # Refresh file list after upload
                refresh_file_list()

                # Auto-select the uploaded file
                select_uploaded_file(uploaded_file_base)

            root.after(0, success_update)

        except Exception as e:
            error_msg = str(e)
            print(f"Upload error: {error_msg}")

            def error_update():
                upload_button.config(text="Upload to MinIO", state="normal")
                messagebox.showerror("Upload Failed",
                                   f"Failed to upload files: {error_msg}")

            root.after(0, error_update)

    # Run upload in separate thread
    threading.Thread(target=upload_worker, daemon=True).start()


def save_to_disk():
    """Copy the current recording files to a user-chosen folder."""
    if not os.path.exists(OUTPUT_PARQUET_FILE) or not os.path.exists(OUTPUT_WAV_FILE):
        save_to_parquet()

    dest_dir = filedialog.askdirectory(title="Choose folder to save recording")
    if not dest_dir:
        return

    copied = []
    failed = []
    for src in [OUTPUT_PARQUET_FILE, OUTPUT_WAV_FILE,
                OUTPUT_WAV_FILE.replace('.wav', '_stwinma2.wav')]:
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(dest_dir, os.path.basename(src)))
                copied.append(os.path.basename(src))
            except Exception as e:
                failed.append(f"{os.path.basename(src)}: {e}")

    if failed:
        messagebox.showerror("Save to Disk", "Some files could not be copied:\n" + "\n".join(failed))
    else:
        upload_button.config(bg="SystemButtonFace", fg="black")
        messagebox.showinfo("Save to Disk",
                            f"Saved {len(copied)} file(s) to:\n{dest_dir}\n\n" +
                            "\n".join(copied))


def _prepare_audio(signal_float, sample_rate):
    """Normalise to float32 [-1, 1] and optionally apply high-pass filter(s)."""
    audio = signal_float.astype(np.float32)
    if hp_filter_var is not None and hp_filter_var.get():
        sos = butter(4, 1000, btype="highpass", fs=sample_rate, output="sos")
        audio = sosfilt(sos, audio).astype(np.float32)
    if hp5k_filter_var is not None and hp5k_filter_var.get():
        sos = butter(4, 5000, btype="highpass", fs=sample_rate, output="sos")
        audio = sosfilt(sos, audio).astype(np.float32)
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak
    return audio

def play_recorded_audio():
    """Play the recorded audio file from AI0"""
    try:
        if os.path.exists(OUTPUT_WAV_FILE):
            sample_rate, raw = wav.read(OUTPUT_WAV_FILE)
            audio = _prepare_audio(raw.astype(np.float32), sample_rate)
            sd.play(audio, sample_rate)
        else:
            messagebox.showwarning("No Audio", "No recorded audio file found. Please record audio first.")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play audio: {str(e)}")

def stop_audio():
    """Stop any active sounddevice playback."""
    sd.stop()

def play_ai2_audio():
    """Play the recorded audio from AI2 (accelerometer channel)"""
    try:
        if recorded_data is not None and len(recorded_data) > 2:
            audio = _prepare_audio(recorded_data[2] / 10.0, SAMPLE_RATE)
            sd.play(audio, SAMPLE_RATE)
        else:
            messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play AI2 audio: {str(e)}")

def play_ai3_audio():
    """Play the recorded audio from AI3 (HBK 4518 CCLD microphone channel)"""
    try:
        if recorded_data is not None and len(recorded_data) > 3:
            audio = _prepare_audio(recorded_data[3] / 1.0, SAMPLE_RATE)
            sd.play(audio, SAMPLE_RATE)
        else:
            messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play AI3 audio: {str(e)}")

def play_ai4_audio():
    """Play the recorded STwin AI04 channel at 192 kHz (falls back to 48 kHz if unsupported)."""
    try:
        stwin_wav = OUTPUT_WAV_FILE.replace('.wav', '_stwinma2.wav')
        if recorded_stwinma2 is not None:
            audio = _prepare_audio(recorded_stwinma2, STWINMA2_SAMPLE_RATE)
        elif os.path.exists(stwin_wav):
            sr, raw = wav.read(stwin_wav)
            audio = _prepare_audio(raw.astype(np.float32), sr)
        else:
            messagebox.showwarning("No Audio", "No AI04 data recorded yet."); return
        try:
            sd.play(audio, STWINMA2_SAMPLE_RATE)
        except Exception:
            # Device doesn't support 192 kHz — resample to 48 kHz for playback
            from scipy.signal import resample as _rs
            n_out = int(len(audio) * 48000 / STWINMA2_SAMPLE_RATE)
            sd.play(_rs(audio, n_out).astype(np.float32), 48000)
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play AI04 audio: {e}")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play AI3 audio: {str(e)}")

def start_recording():
    if recording:
        return
    if manual_stop_var is not None and manual_stop_var.get():
        stop_indefinite.clear()
        record_button.config(text="⏹  Stop", bg="red", fg="white",
                             command=_stop_manual_recording)
    threading.Thread(target=record_data, daemon=True).start()

def _stop_manual_recording():
    stop_indefinite.set()
    record_button.config(text="Stopping…", state="disabled")

def start_linked_recording(on_daq_ready):
    """Start a DAQ-linked recording. on_daq_ready() fires when data starts flowing
    (LAN-XI: streaming socket connect; Focusrite: just before sd.rec())."""
    source = audio_source_var.get() if audio_source_var is not None else "LAN-XI"
    if source == "LAN-XI" and not LANXI_AVAILABLE:
        messagebox.showerror("LAN-XI Not Available",
                             "No LAN-XI device connected. Check BKDAQ_IP in .env and restart.")
        return
    if not recording:
        threading.Thread(target=record_data, args=(on_daq_ready,), daemon=True).start()




def update_plot(time_axis, data, loadcell=None, title="Recorded Data", stwinma2=None):
    def _has_data(arr):
        if arr is None:
            return False
        a = np.asarray(arr, dtype=float)
        return a.size > 0 and not np.all(np.isnan(a))

    channels = []
    if _has_data(data[0]):
        channels.append(("AI0 (V)",           np.asarray(data[0], dtype=float),           "b"))
    if _has_data(data[1]):
        channels.append(("AI1 (V)",           np.asarray(data[1], dtype=float),           "r"))
    if _has_data(data[2]):
        channels.append(("AI2 (V)",           np.asarray(data[2], dtype=float),           "g"))
    if _has_data(data[3]):
        channels.append(("AI3 (mV)",          np.asarray(data[3], dtype=float) * 1000,   "m"))
    if _has_data(loadcell):
        channels.append(("Load Cell (mV)",    np.asarray(loadcell, dtype=float),          "saddlebrown"))
    if _has_data(stwinma2):
        # Resampled to match time_axis length for display
        from scipy.signal import resample as _resample
        _s = np.asarray(stwinma2, dtype=float)
        if len(_s) != len(time_axis):
            _s = _resample(_s, len(time_axis))
        channels.append(("AI04",               _s,                                         "darkcyan"))

    if not channels:
        return

    n = len(channels)
    fig.clear()
    axes = fig.subplots(n, 1, sharex=True)
    if n == 1:
        axes = [axes]

    for i, (ax, (label, values, color)) in enumerate(zip(axes, channels)):
        ax.plot(time_axis, values, color=color)
        ax.set_ylabel(label, color=color)
        ax.tick_params(axis="y", labelcolor=color)
        if i == 0:
            ax.set_title(title)
    axes[-1].set_xlabel("Time (s)")

    fig.tight_layout(pad=1.5)
    fig.canvas.draw()



def list_s3_files(sort_by="name", sort_order="asc"):
    """List MinIO files with sorting options

    Args:
        sort_by: "name" or "time"
        sort_order: "asc" (1-9) or "desc" (9-1)
    """
    try:
        minio_client = create_minio_client()
        objects = minio_client.list_objects(BUCKET_NAME)

        # Create a dictionary to store file info
        files_info = {}
        for obj in objects:
            if obj.object_name.endswith('.parquet'):  # Only process parquet files
                base_name = os.path.splitext(os.path.basename(obj.object_name))[0]
                if base_name not in files_info:
                    files_info[base_name] = {
                        'name': base_name,
                        'last_modified': obj.last_modified
                    }

        # Sort based on criteria
        if sort_by == "time":
            sorted_files = sorted(files_info.values(),
                                key=lambda x: x['last_modified'],
                                reverse=(sort_order == "desc"))
        else:  # sort by name
            sorted_files = sorted(files_info.values(),
                                key=lambda x: x['name'],
                                reverse=(sort_order == "desc"))

        return [file_info['name'] for file_info in sorted_files]

    except S3Error as e:
        print(f"MinIO Error listing files: {e}")
        return []
    except Exception as e:
        print(f"Error listing files: {e}")
        return []


def refresh_file_list():
    """Refresh the file listbox with current S3 files"""
    global sort_by, sort_order
    file_listbox.delete(0, tk.END)
    s3_files = list_s3_files(sort_by, sort_order)
    for file in s3_files:
        file_listbox.insert(tk.END, file)

def change_sort_criteria():
    """Handle sort criteria changes and refresh the list"""
    global sort_by, sort_order
    sort_by = sort_by_var.get()
    sort_order = sort_order_var.get()
    refresh_file_list()

def select_uploaded_file(file_base_name):
    """Select and highlight the uploaded file in the listbox"""
    for i in range(file_listbox.size()):
        if file_listbox.get(i) == file_base_name:
            file_listbox.selection_clear(0, tk.END)
            file_listbox.selection_set(i)
            file_listbox.see(i)  # Scroll to make it visible
            dropdown_var.set(file_base_name)
            break




def load_sample():
    try:
        base_name = dropdown_var.get()
        parquet_key = f"{base_name}.parquet"
        minio_client = create_minio_client()

        # Get object from MinIO
        response = minio_client.get_object(BUCKET_NAME, parquet_key)
        df = pd.read_parquet(io.BytesIO(response.read()))

        lc_col     = df["Load Cell (mV)"].values       if "Load Cell (mV)"       in df.columns else None
        stwin_col  = (df["AI04 (norm)"].values         if "AI04 (norm)"          in df.columns else
                      df["STWINMA2 Ch0 (norm)"].values if "STWINMA2 Ch0 (norm)"  in df.columns else None)
        update_plot(df["Time (s)"], [df["AI0 (V)"], df["AI1 (V)"], df["AI2 (V)"], df["AI3 (mV)"] / 1000],
                    loadcell=lc_col, title=base_name, stwinma2=stwin_col)
        metadata_text.delete("1.0", tk.END)
        for key, val in df.attrs.items():
            metadata_text.insert(tk.END, f"{key}: {val}\n")

        # Store the loaded dataframe globally for metadata updates
        global loaded_df, current_sample_name
        loaded_df = df
        current_sample_name = base_name
    except S3Error as e:
        messagebox.showerror("Load Error", f"MinIO Error: {e}")
    except Exception as e:
        messagebox.showerror("Load Error", str(e))






def create_minio_client():
    """Create and return MinIO client"""
    env_path = find_env_file()
    dotenv.load_dotenv(env_path)
    endpoint = os.getenv("MINIO_ENDPOINT")

    # Parse endpoint URL properly
    secure = False
    if endpoint.startswith("https://"):
        endpoint = endpoint[8:]
        secure = True
    elif endpoint.startswith("http://"):
        endpoint = endpoint[7:]
        secure = False


    return Minio(
        endpoint,
        access_key=os.getenv("MINIO_ACCESS_KEY"),
        secret_key=os.getenv("MINIO_SECRET_KEY"),
        secure=secure,
        region=os.getenv("MINIO_REGION", "us-east-1")  # Default region
    )


def update_search_index_on_server(parquet_filename, metadata_dict, dataframe):
    """
    Update the search index on the server with new metadata for an uploaded file

    Args:
        parquet_filename: Full filename (with .parquet extension)
        metadata_dict: Dictionary of metadata
        dataframe: The pandas DataFrame for file info

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        minio_client = create_minio_client()

        # Download existing index or create new one
        try:
            response = minio_client.get_object(BUCKET_NAME, INDEX_FILE_NAME)
            index_data = json.loads(response.read().decode('utf-8'))
        except S3Error as e:
            if "NoSuchKey" in str(e):
                # Create new index if it doesn't exist
                index_data = {
                    "index_info": {
                        "created_at": datetime.datetime.now().isoformat(),
                        "total_files": 0,
                        "index_version": "1.0"
                    },
                    "files": {}
                }
            else:
                raise e

        # Get file statistics
        try:
            stat = minio_client.stat_object(BUCKET_NAME, parquet_filename)
            file_info = {
                'filename': parquet_filename,
                'size_bytes': stat.size,
                'last_modified': stat.last_modified.isoformat() if stat.last_modified else None,
                'rows': len(dataframe),
                'columns': len(dataframe.columns),
                'column_names': list(dataframe.columns)
            }
        except Exception as file_error:
            print(f"Warning: Could not get file stats: {file_error}")
            file_info = {
                'filename': parquet_filename,
                'size_bytes': 'Unknown',
                'last_modified': datetime.datetime.now().isoformat(),
                'rows': len(dataframe) if dataframe is not None else 'Unknown',
                'columns': len(dataframe.columns) if dataframe is not None else 'Unknown',
                'column_names': list(dataframe.columns) if dataframe is not None else 'Unknown'
            }

        # Prepare the file metadata
        file_metadata = dict(metadata_dict) if metadata_dict else {}
        file_metadata['_file_info'] = file_info

        # Update the index
        index_data['files'][parquet_filename] = file_metadata
        index_data['index_info']['total_files'] = len(index_data['files'])
        index_data['index_info']['last_updated'] = datetime.datetime.now().isoformat()

        # Upload updated index
        json_bytes = json.dumps(index_data, indent=2, ensure_ascii=False).encode('utf-8')
        json_buffer = io.BytesIO(json_bytes)

        minio_client.put_object(
            BUCKET_NAME,
            INDEX_FILE_NAME,
            json_buffer,
            len(json_bytes),
            content_type='application/json'
        )

        print(f"Successfully updated search index for {parquet_filename}")
        return True

    except Exception as e:
        error_msg = f"Failed to update search index: {str(e)}"
        print(error_msg)

        # Show error dialog
        messagebox.showerror("Search Index Error",
                            f"Could not update search index for {parquet_filename}.\n\n"
                            f"Error: {str(e)}\n\n"
                            f"The file was uploaded successfully, but won't be searchable until the index is updated.")
        return False


def launch_editor():
    """Launch the KRAK Editor in a separate process"""
    try:
        import subprocess
        import sys
        import os

        # Check if we're running as an executable or as a script
        if getattr(sys, 'frozen', False):
            # Running as executable - look for krak_editor.exe in same directory
            exe_dir = os.path.dirname(sys.executable)
            editor_exe = os.path.join(exe_dir, "krak_editor.exe")

            if os.path.exists(editor_exe):
                subprocess.Popen([editor_exe], cwd=exe_dir)
                print("Editor launched successfully from executable")
            else:
                messagebox.showerror("Editor Not Found",
                    f"Editor executable not found at: {editor_exe}\n\n"
                    f"Make sure krak_editor.exe is in the same directory as krak_logger.exe")
        else:
            # Running as script - use Python interpreter
            script_dir = os.path.dirname(os.path.abspath(__file__))
            editor_path = os.path.join(script_dir, "krak_editor_gui.py")
            subprocess.Popen([sys.executable, editor_path])
            print("Editor launched successfully from script")

    except Exception as e:
        messagebox.showerror("Launch Error", f"Failed to launch editor: {str(e)}")


def on_closing():
    global mcu_protocol

    # Disconnect MCU serial
    try:
        if mcu_protocol is not None and mcu_protocol.connected:
            mcu_protocol.disconnect()
    except Exception as e:
        print(f"Error disconnecting MCU: {e}")

    try:
        # Stop any audio playback
        sd.stop()
        print("Audio playback stopped")
    except Exception as e:
        print(f"Error stopping audio: {e}")

    try:
        # Close LAN-XI stream
        if Lanxi is not None:
            Lanxi.close_stream()
        print("LAN-XI stream closed")
    except Exception as e:
        print(f"Error closing LAN-XI stream: {e}")

    try:
        # Stop matplotlib animations/timers
        plt.close('all')
        print("Matplotlib plots closed")
    except Exception as e:
        print(f"Error closing plots: {e}")

    # Attempt to stop all non-main threads gracefully
    for thread in threading.enumerate():
        if thread is not threading.main_thread():
            try:
                print(f"Waiting for thread to finish: {thread.name}")
                thread.join(timeout=2)  # Increased timeout
            except Exception as e:
                print(f"Error joining thread {thread.name}: {e}")

    print("Cleanup complete, closing application")
    root.quit()  # Stop the mainloop
    root.destroy()  # Destroy the window


# ---------------------------------------------------------------------------
# Mel Spectrogram helpers
# ---------------------------------------------------------------------------

def _compute_mel_spectrogram(signal, sr, n_mels=128, f_max=None):
    """Compute mel spectrogram with scipy+numpy; returns (mel_db, times, mel_freqs_hz)."""
    from scipy.signal import stft as _stft
    # Scale n_fft with sample rate so low-frequency mel bands always have
    # enough FFT bins (bin width < ~25 Hz keeps triangular filters non-zero).
    n_fft = 2048 if sr <= 52000 else (4096 if sr <= 100_000 else 8192)
    hop   = n_fft // 4
    _, t, Zxx = _stft(signal, fs=sr, nperseg=n_fft, noverlap=n_fft - hop, window="hann")
    power = np.abs(Zxx) ** 2

    f_min = 20.0
    if f_max is None:
        f_max = sr / 2.0
    m_min = 2595.0 * np.log10(1 + f_min / 700.0)
    m_max = 2595.0 * np.log10(1 + f_max / 700.0)
    mel_pts = np.linspace(m_min, m_max, n_mels + 2)
    hz_pts  = 700.0 * (10.0 ** (mel_pts / 2595.0) - 1.0)
    bins    = np.floor((n_fft + 1) * hz_pts / sr).astype(int)
    n_bins  = n_fft // 2 + 1

    fbank = np.zeros((n_mels, n_bins))
    for m in range(1, n_mels + 1):
        lo, ctr, hi = bins[m - 1], bins[m], bins[m + 1]
        if ctr > lo:
            for k in range(lo, ctr):
                fbank[m - 1, k] = (k - lo) / (ctr - lo)
        if hi > ctr:
            for k in range(ctr, hi):
                fbank[m - 1, k] = (hi - k) / (hi - ctr)

    mel_spec = fbank @ power
    mel_db   = 10.0 * np.log10(np.maximum(mel_spec, 1e-10))
    return mel_db, t, hz_pts[1:-1]


def _build_mel_tab(notebook, root_win):
    """Add a Mel Spectrogram tab to notebook."""
    tab = ttk.Frame(notebook)
    notebook.add(tab, text="Mel Spectrogram")

    # --- Left control panel ---
    ctrl = tk.Frame(tab, width=200)
    ctrl.pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=8)
    ctrl.pack_propagate(False)

    tk.Label(ctrl, text="Channel:").pack(anchor="w")
    channel_var = tk.StringVar(value="AI0")
    for ch in ("AI0", "AI2", "AI3 Mic", "AI04"):
        tk.Radiobutton(ctrl, text=ch, variable=channel_var, value=ch).pack(anchor="w")

    mel_hp_var = tk.BooleanVar(value=False)
    tk.Checkbutton(ctrl, text="1 kHz high-pass filter",
                   variable=mel_hp_var).pack(anchor="w", pady=(10, 0))
    mel_hp5k_var = tk.BooleanVar(value=False)
    tk.Checkbutton(ctrl, text="5 kHz high-pass filter",
                   variable=mel_hp5k_var).pack(anchor="w")

    tk.Label(ctrl, text="Mel bands:").pack(anchor="w", pady=(10, 0))
    n_mels_var = tk.IntVar(value=128)
    tk.Spinbox(ctrl, from_=32, to=256, increment=16,
               textvariable=n_mels_var, width=6).pack(anchor="w")

    tk.Button(ctrl, text="Plot", command=lambda: _mel_plot(),
              bg="lightblue", width=14).pack(anchor="w", pady=(14, 0))

    status_var = tk.StringVar(value="Load a recording, then click Plot.")
    tk.Label(ctrl, textvariable=status_var, fg="gray",
             wraplength=180, justify="left").pack(anchor="w", pady=(8, 0))

    # --- Matplotlib figure ---
    fig_mel = plt.figure(figsize=(9, 4))
    canvas_mel = FigureCanvasTkAgg(fig_mel, master=tab)
    canvas_mel.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _mel_plot():
        ch     = channel_var.get()
        use_hp = mel_hp_var.get()
        use_hp5k = mel_hp5k_var.get()
        n_mels = max(32, min(256, n_mels_var.get()))

        # --- Gather signal ---
        sig, sr = None, None
        if ch == "AI0":
            if not os.path.exists(OUTPUT_WAV_FILE):
                status_var.set("No WAV file found."); return
            sr, raw = wav.read(OUTPUT_WAV_FILE)
            sig = raw.astype(np.float32)
            if sig.ndim > 1:
                sig = sig[:, 0]
        elif ch == "AI2":
            if recorded_data is None or len(recorded_data) <= 2:
                status_var.set("No AI2 data."); return
            sig = (recorded_data[2] / 10.0).astype(np.float32)
            sr  = SAMPLE_RATE
        elif ch == "AI04":
            stwin_wav = OUTPUT_WAV_FILE.replace('.wav', '_stwinma2.wav')
            if not os.path.exists(stwin_wav):
                status_var.set("No STWINMA2 WAV found. Record with source 'STWINMA2 Ch0 (192 kHz)' or enable 'Also record STWINMA2 Ch0'."); return
            sr, raw = wav.read(stwin_wav)
            sig = raw.astype(np.float32)
            if sig.ndim > 1:
                sig = sig[:, 0]
        else:
            if recorded_data is None or len(recorded_data) <= 3:
                status_var.set("No AI3 data."); return
            sig = recorded_data[3].astype(np.float32)
            sr  = SAMPLE_RATE

        # --- Optional high-pass ---
        if use_hp:
            sos = butter(4, 1000, btype="highpass", fs=sr, output="sos")
            sig = sosfilt(sos, sig).astype(np.float32)
        if use_hp5k:
            sos = butter(4, 5000, btype="highpass", fs=sr, output="sos")
            sig = sosfilt(sos, sig).astype(np.float32)

        status_var.set("Computing…")
        root_win.update_idletasks()

        try:
            mel_fmax = 80_000.0 if ch == "AI04" else None
            mel_db, t, mel_hz = _compute_mel_spectrogram(sig, sr, n_mels, f_max=mel_fmax)
        except Exception as exc:
            status_var.set(f"Error: {exc}"); return

        # --- Draw ---
        fig_mel.clf()
        ax = fig_mel.add_subplot(111)
        im = ax.imshow(
            mel_db, aspect="auto", origin="lower",
            extent=[t[0], t[-1], 0, n_mels],
            cmap="inferno",
        )
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Mel band")
        if use_hp5k:
            title_suffix = " — HP 5 kHz"
        elif use_hp:
            title_suffix = " — HP 1 kHz"
        else:
            title_suffix = ""
        ax.set_title(f"Mel Spectrogram – {ch}{title_suffix}")

        # Frequency axis ticks — extend to 80 kHz for STWINMA2 (192 kHz, capped at 80 kHz)
        tick_hz = [100, 500, 1000, 2000, 5000, 10000, 20000]
        if ch == "AI04":
            tick_hz += [32000, 48000, 64000, 80000]
        valid   = [f for f in tick_hz if mel_hz[0] <= f <= mel_hz[-1]]
        idx     = [float(np.searchsorted(mel_hz, f)) for f in valid]
        ax.set_yticks(idx)
        ax.set_yticklabels([f"{f//1000}k" if f >= 1000 else str(f) for f in valid])

        fig_mel.colorbar(im, ax=ax, label="dB")
        fig_mel.tight_layout(pad=2.0)
        canvas_mel.draw()
        status_var.set(
            f"Done — {mel_db.shape[1]} frames × {n_mels} mel bands"
        )

    return tab


# ===========================================================================
# Signal Processing GUI (originally signal_processing_gui.py)
# Merged into krak_logger_gui.py -- do not import signal_processing_gui
# ===========================================================================
class WeightedEnsemble(BaseEstimator, ClassifierMixin):
    def __init__(self, models, feature_lists, weights, classes):
        self.models       = models
        self.feature_lists = feature_lists
        self.weights      = weights
        self.classes_     = classes

    def fit(self, X, y): return self

    def predict_proba(self, X):
        result = np.zeros((len(X), len(self.classes_)))
        cls_list = list(self.classes_)
        for mdl, feats, w in zip(self.models, self.feature_lists, self.weights):
            Xf = X.copy()
            for m in [f for f in feats if f not in Xf.columns]:
                Xf[m] = 0.0
            Xf = Xf[feats]
            proba = mdl.predict_proba(Xf)
            mdl_cls = list(mdl.classes_)
            for i, cls in enumerate(cls_list):
                if cls in mdl_cls:
                    result[:, i] += proba[:, mdl_cls.index(cls)] * w
        return result

    def predict(self, X):
        probas = self.predict_proba(X)
        return self.classes_[np.argmax(probas, axis=1)]

class WeightedEnsemble_20260505(WeightedEnsemble):
    pass

class WeightedEnsemble_20260506(WeightedEnsemble):
    pass

# ---------------------------------------------------------------------------
# Paths  (relative to this script Ã¢â€ â€™ mobile cookie crusher/)
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR    = os.path.dirname(os.path.dirname(SCRIPT_DIR))   # mobile cookie crusher/
MODELS_DIR = os.path.join(BASE_DIR, "models")

AVAILABLE_MODELS_DICT = {}
if os.path.exists(MODELS_DIR):
    for d in os.listdir(MODELS_DIR):
        full_path = os.path.join(MODELS_DIR, d)
        if os.path.isdir(full_path):
            AVAILABLE_MODELS_DICT[d] = full_path

# Fallback just in case
if not AVAILABLE_MODELS_DICT:
    STANDALONE_DIR = os.path.join(BASE_DIR, "standalone_ensemble")
    STANDALONE_DIR_20260505 = os.path.join(BASE_DIR, "standalone_ensemble_20260505")
    STANDALONE_DIR_NOISY = os.path.join(BASE_DIR, "standalone_ensemble_noisy")
    AVAILABLE_MODELS_DICT = {
        "Standalone Ensemble": STANDALONE_DIR,
        "Standalone Ensemble 20260505": STANDALONE_DIR_20260505,
        "Standalone Ensemble Noisy": STANDALONE_DIR_NOISY,
    }

FEATURE_EXTRACTION_DIR = os.path.join(SCRIPT_DIR, "feature_extraction")

# ---------------------------------------------------------------------------
# Feature-extraction parsing constants
# ---------------------------------------------------------------------------
KNOWN_FILTERS = [
    "nofilter",
    "antialiasing_32khz",
    "standard_bandpass_1000_25000",
    "band5k_0_5000",
    "band5k_5000_10000",
    "band5k_10000_15000",
    "band5k_15000_20000",
    "band5k_20000_25000",
    "acoustic_range",
    "mid_frequency",
    "high_frequency",
    "low_frequency",
    "ultrasonic",
]

KNOWN_CONFIGS = {
    "default", "high_res_temporal", "high_res_spectral",
    "speech_standard", "percussive_focus", "balanced",
    "Tresh_0_Prom_8e-2", "Tresh_1e-2_Prom_8e-2", "Tresh_2e-2_Prom_8e-2", "Tresh_2e-2_Prom_1e-1"
}

LFCC_ONLY_PREFIXES = (
    "lfcc_mean_", "lfcc_std_", "lfcc_max_", "lfcc_min_", "lfcc_range_",
    "lfcc_zcr_", "lfcc_entropy_", "lfcc_regularity_",
    "lfcc_deriv_std_", "lfcc_deriv_mean_abs_",
    "delta_lfcc_", "delta2_lfcc_",
)

MFCC_ONLY_PREFIXES = (
    "mfcc_mean_", "mfcc_std_", "mfcc_max_", "mfcc_min_", "mfcc_range_",
    "mfcc_zcr_", "mfcc_entropy_", "mfcc_regularity_",
    "mfcc_deriv_std_", "mfcc_deriv_mean_abs_",
    "delta_mfcc_", "delta2_mfcc_",
    "chroma_", "chroma_std_",
    "tonnetz_", "poly_",
)

ALL_SPECTRUM_PREFIXES = LFCC_ONLY_PREFIXES + MFCC_ONLY_PREFIXES

PEAK_DEPENDENT_FEATURES = {
    "num_peaks", "mean_peak_height", "std_peak_height",
    "max_peak_height", "min_peak_height", "peak_density",
    "mean_peak_width", "std_peak_width", "mean_peak_prominence",
    "std_peak_prominence", "mean_peak_interval", "std_peak_interval",
}


def parse_feature(feat_name: str):
    """Return (base_feature, filter_name, config_or_None)."""
    for filt in sorted(KNOWN_FILTERS, key=len, reverse=True):
        for cfg in KNOWN_CONFIGS:
            if feat_name.endswith(f"_{filt}_{cfg}"):
                base = feat_name[: -(len(filt) + len(cfg) + 2)]
                return base, filt, cfg
        if feat_name.endswith(f"_{filt}"):
            base = feat_name[: -(len(filt) + 1)]
            return base, filt, None
    return feat_name, None, None


def load_required_features(model_dir):
    json_path = os.path.join(model_dir, "elastic_net_model_selected_features.json")
    if os.path.exists(json_path):
        import json
        with open(json_path, "r") as fh:
            return json.load(fh)
    
    req_path = os.path.join(model_dir, "required_features.txt")
    with open(req_path, "r") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


# ---------------------------------------------------------------------------
# Defaults  (from chips_demo_airmic_05_05.json)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "ACOUSTIC_COLUMN": "AI0 (V)",
    "REFERENCE_COLUMN": "AI1 (V)",
    "SAMPLING_RATE": 65536,
    "TRIM_METHOD": "Peak Force",
    # Threshold params
    "START_THRESHOLD": 0.1,
    "START_EXTRA_TIME_SECONDS": 0.0,
    "STOP_THRESHOLD": 0.4,
    "HYSTERESIS": 0.2,
    "EXTRA_TIME_SECONDS": 0.25,
    "MIN_DURATION_BELOW_THRESHOLD": 0.25,
    # Threshold % params
    "START_THRESHOLD_PCT": 10.0,
    "STOP_THRESHOLD_PCT": 40.0,
    "HYSTERESIS_PCT": 20.0,
    # Peak Force params
    "PEAK_FORCE_TIME_BEFORE": 14.0,
    "PEAK_FORCE_TIME_AFTER": 0.5,
    # Threshold to Peak params
    "THRESHOLD_TO_PEAK_THRESHOLD": 0.1,
    "THRESHOLD_TO_PEAK_TIME_BEFORE": 0.1,
    "THRESHOLD_TO_PEAK_TIME_AFTER": 0.1,
    # Peak refinement
    "REFINE_WITH_PEAKS": True,
    "PEAK_DETECTION_CHANNEL": "AI0 (V)",
    "PEAK_REFINEMENT_METHOD": "First-to-Last Peak",
    "EXTRA_TIME_BEFORE_FIRST_PEAK_MS": 85.0,
    "EXTRA_TIME_AFTER_LAST_PEAK_MS": 85.0,
    "PEAK_WINDOW_DURATION": 0.2,
    "PEAK_WINDOW_LEFT": 0.1,
    "PEAK_WINDOW_RIGHT": 0.1,
    "PEAK_STRIDE_DURATION": 0.01,
    "DROP_MEASUREMENT_PERIOD": 10.0,
    "DROP_SEARCH_WINDOW_BEFORE": 0.2,
    "DROP_SEARCH_WINDOW_AFTER": 0.2,
    # Filter (off by default here Ã¢â‚¬â€ no filter_helper wired up)
    "ENABLE_FILTER": False,
    "FILTER_PARAMS": {},
    "FILTER_CHAIN": [],
    "FADE_DURATION_MS": 10.0,
    "PEAK_PARAMS": {},
}

DEFAULT_PEAK_PARAMS = {
    "height": 0.4,
    "distance": 1,
    "threshold": 0.01,
    "prominence": 0.1,
    "width": None,
    "rel_height": 0.5,
}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
sp_loaded_df        = None
sp_loaded_filename  = None
trim_result      = None          # (start_idx, end_idx, duration_s)
detected_peaks_arr = None        # peak indices inside trim window

feat_thread      = None
feat_cancel_flag = threading.Event()
_deep_thread      = None
_deep_cancel_flag = threading.Event()

def build_signal_processing_tabs(parent_notebook, root_window):
    """Build Set Window / Feature Extraction / Deep Predict tabs on parent_notebook."""
    root     = root_window
    notebook = parent_notebook

    # ---------------------------------------------------------------------------
    # Font size Ã¢â‚¬â€ applies to all widgets via the named system fonts
    # ---------------------------------------------------------------------------
    _DEFAULT_FONT_SIZE = 15

    font_size_var = tk.IntVar(value=_DEFAULT_FONT_SIZE)

    def _apply_font_size(*_):
        size = max(8, min(30, font_size_var.get()))
        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except Exception:
                pass
        ttk.Style().configure(".", font=("TkDefaultFont", size))
        root.update_idletasks()

    _apply_font_size()  # set to 15 on startup

    # ---------------------------------------------------------------------------
    # Main Notebook
    # ---------------------------------------------------------------------------
    # Tab containers
    tab_signal = ttk.Frame(notebook)
    notebook.add(tab_signal, text="  Set Window  ")

    tab_feat = ttk.Frame(notebook)
    notebook.add(tab_feat, text="  Feature Extraction  ")

    # ============================================================================
    # TAB 1 Ã¢â‚¬â€ Set Window
    # ============================================================================

    # Ã¢â€â‚¬Ã¢â€â‚¬ Scrollable left panel Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    _paned = tk.PanedWindow(tab_signal, orient=tk.HORIZONTAL, sashwidth=6, sashrelief=tk.RAISED, sashpad=2)
    _paned.pack(fill=tk.BOTH, expand=True)

    left_outer = tk.Frame(_paned, width=360)
    left_outer.pack_propagate(False)
    _paned.add(left_outer, minsize=180, stretch="never")

    _lcanvas = tk.Canvas(left_outer, highlightthickness=0)
    _lscroll = tk.Scrollbar(left_outer, orient=tk.VERTICAL, command=_lcanvas.yview)
    _lcanvas.configure(yscrollcommand=_lscroll.set)
    _lscroll.pack(side=tk.RIGHT, fill=tk.Y)
    _lcanvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    left_frame = tk.Frame(_lcanvas)
    _win_id = _lcanvas.create_window((0, 0), window=left_frame, anchor="nw")

    def _on_left_frame_configure(event):
        _lcanvas.configure(scrollregion=_lcanvas.bbox("all"))

    def _on_canvas_configure(event):
        _lcanvas.itemconfig(_win_id, width=event.width)

    left_frame.bind("<Configure>", _on_left_frame_configure)
    _lcanvas.bind("<Configure>", _on_canvas_configure)

    def _on_mousewheel(event):
        _lcanvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    left_frame.bind_all("<MouseWheel>", _on_mousewheel)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Right panel (plot) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    right_frame = tk.Frame(_paned)
    _paned.add(right_frame, minsize=400, stretch="always")

    # ============================================================================
    # LEFT PANEL Ã¢â‚¬â€ controls
    # ============================================================================

    # Ã¢â€â‚¬Ã¢â€â‚¬ Font size control Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    font_row = tk.Frame(left_frame)
    font_row.pack(fill=tk.X, pady=(0, 6))
    tk.Label(font_row, text="Font Size:").pack(side=tk.LEFT)
    font_spinbox = tk.Spinbox(font_row, from_=8, to=30, textvariable=font_size_var,
                              width=4, command=_apply_font_size)
    font_spinbox.pack(side=tk.LEFT, padx=4)
    font_spinbox.bind("<Return>", _apply_font_size)
    font_spinbox.bind("<FocusOut>", _apply_font_size)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Section: File loading Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    file_frame = tk.LabelFrame(left_frame, text="Load Parquet File (saved by KRAK Logger)", padx=8, pady=6)
    file_frame.pack(fill=tk.X, pady=(0, 6))

    def load_file():
        global sp_loaded_df, sp_loaded_filename, trim_result, detected_peaks_arr
        _initial_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_files")
        if not os.path.isdir(_initial_dir):
            _initial_dir = os.path.dirname(os.path.abspath(__file__))
        path = filedialog.askopenfilename(
            title="Select Parquet File",
            initialdir=_initial_dir,
            filetypes=[
                ("Parquet files", "*.parquet"),
                ("All files",     "*.*"),
            ],
        )
        if not path:
            return
        try:
            df = pd.read_parquet(path)
            if hasattr(df, "attrs") and "Sample Rate (Hz)" in df.attrs:
                sampling_rate_var.set(int(df.attrs["Sample Rate (Hz)"]))

            sp_loaded_df = df
            sp_loaded_filename = os.path.basename(path)
            trim_result = None
            detected_peaks_arr = None

            sr = sampling_rate_var.get()
            duration_s = len(df) / sr
            file_label_var.set(
                f"{sp_loaded_filename}  |  {len(df):,} samples  |  {duration_s:.3f} s"
            )
            status_var.set(f"Loaded: {sp_loaded_filename}")
            _draw_plot()
        except Exception as exc:
            messagebox.showerror("Load Error", f"Failed to load file:\n{exc}")

    load_btn = tk.Button(file_frame, text="Open Parquet File...", command=load_file,
                         bg="lightblue", width=22)
    load_btn.pack(side=tk.LEFT, padx=(0, 8))

    file_label_var = tk.StringVar(value="No file loaded")
    tk.Label(file_frame, textvariable=file_label_var,
             fg="blue", anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Section: Sampling rate Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    sr_frame = tk.Frame(left_frame)
    sr_frame.pack(fill=tk.X, pady=(0, 4))
    tk.Label(sr_frame, text="Sampling Rate (Hz):").pack(side=tk.LEFT)
    sampling_rate_var = tk.IntVar(value=DEFAULT_CONFIG["SAMPLING_RATE"])
    tk.Entry(sr_frame, textvariable=sampling_rate_var, width=8).pack(side=tk.LEFT, padx=4)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Section: Trim method Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    trim_frame = tk.LabelFrame(left_frame, text="Trimming Method", padx=8, pady=6)
    trim_frame.pack(fill=tk.X, pady=(0, 6))

    TRIM_METHODS = ["Peak Force", "Threshold", "Threshold %", "Threshold to Peak"]
    trim_method_var = tk.StringVar(value=DEFAULT_CONFIG["TRIM_METHOD"])
    trim_method_combo = ttk.Combobox(trim_frame, textvariable=trim_method_var,
                                     values=TRIM_METHODS, state="readonly", width=22)
    trim_method_combo.pack(anchor="w")

    # Ã¢â€â‚¬Ã¢â€â‚¬ Peak Force params Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    pf_frame = tk.LabelFrame(left_frame, text="Peak Force Parameters", padx=8, pady=6)

    pf_before_var = tk.DoubleVar(value=DEFAULT_CONFIG["PEAK_FORCE_TIME_BEFORE"])
    pf_after_var = tk.DoubleVar(value=DEFAULT_CONFIG["PEAK_FORCE_TIME_AFTER"])

    _r = tk.Frame(pf_frame); _r.pack(fill=tk.X, pady=2)
    tk.Label(_r, text="Time Before Peak (s):").pack(side=tk.LEFT)
    tk.Entry(_r, textvariable=pf_before_var, width=8).pack(side=tk.LEFT, padx=4)

    _r = tk.Frame(pf_frame); _r.pack(fill=tk.X, pady=2)
    tk.Label(_r, text="Time After Peak (s):").pack(side=tk.LEFT)
    pf_after_scale = tk.Scale(_r, from_=0.0, to=5.0, resolution=0.01,
                               orient=tk.HORIZONTAL, variable=pf_after_var, length=160)
    pf_after_scale.pack(side=tk.LEFT)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Threshold params Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    thresh_frame = tk.LabelFrame(left_frame, text="Threshold Parameters", padx=8, pady=6)

    thresh_start_var = tk.DoubleVar(value=DEFAULT_CONFIG["START_THRESHOLD"])
    thresh_stop_var = tk.DoubleVar(value=DEFAULT_CONFIG["STOP_THRESHOLD"])
    thresh_hyst_var = tk.DoubleVar(value=DEFAULT_CONFIG["HYSTERESIS"])
    thresh_extra_var = tk.DoubleVar(value=DEFAULT_CONFIG["EXTRA_TIME_SECONDS"])
    thresh_mindur_var = tk.DoubleVar(value=DEFAULT_CONFIG["MIN_DURATION_BELOW_THRESHOLD"])
    thresh_start_extra_var = tk.DoubleVar(value=DEFAULT_CONFIG["START_EXTRA_TIME_SECONDS"])

    for label, var, lo, hi, res in [
        ("Start Threshold:", thresh_start_var, 0.0, 2.0, 0.001),
        ("Stop Threshold:", thresh_stop_var, 0.0, 2.0, 0.001),
        ("Hysteresis:", thresh_hyst_var, 0.0, 0.5, 0.001),
        ("Extra Time (s):", thresh_extra_var, -2.0, 2.0, 0.01),
        ("Min Duration Below Thresh (s):", thresh_mindur_var, 0.0, 5.0, 0.01),
        ("Start Extra Time (s):", thresh_start_extra_var, -2.0, 2.0, 0.01),
    ]:
        _r = tk.Frame(thresh_frame); _r.pack(fill=tk.X, pady=1)
        tk.Label(_r, text=label, width=24, anchor="w").pack(side=tk.LEFT)
        tk.Scale(_r, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                 variable=var, length=150).pack(side=tk.LEFT)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Threshold % params Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    thresh_pct_frame = tk.LabelFrame(left_frame, text="Threshold % Parameters", padx=8, pady=6)

    thresh_pct_start_var = tk.DoubleVar(value=DEFAULT_CONFIG["START_THRESHOLD_PCT"])
    thresh_pct_stop_var = tk.DoubleVar(value=DEFAULT_CONFIG["STOP_THRESHOLD_PCT"])
    thresh_pct_hyst_var = tk.DoubleVar(value=DEFAULT_CONFIG["HYSTERESIS_PCT"])
    thresh_pct_extra_var = tk.DoubleVar(value=DEFAULT_CONFIG["EXTRA_TIME_SECONDS"])
    thresh_pct_mindur_var = tk.DoubleVar(value=DEFAULT_CONFIG["MIN_DURATION_BELOW_THRESHOLD"])

    for label, var, lo, hi, res in [
        ("Start Threshold (%):", thresh_pct_start_var, 0.1, 100.0, 0.5),
        ("Stop Threshold (%):", thresh_pct_stop_var, 0.1, 100.0, 0.5),
        ("Hysteresis (%):", thresh_pct_hyst_var, 0.0, 100.0, 0.5),
        ("Extra Time (s):", thresh_pct_extra_var, -2.0, 2.0, 0.01),
        ("Min Duration Below Thresh (s):", thresh_pct_mindur_var, 0.0, 5.0, 0.01),
    ]:
        _r = tk.Frame(thresh_pct_frame); _r.pack(fill=tk.X, pady=1)
        tk.Label(_r, text=label, width=24, anchor="w").pack(side=tk.LEFT)
        tk.Scale(_r, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                 variable=var, length=150).pack(side=tk.LEFT)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Threshold to Peak params Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    t2p_frame = tk.LabelFrame(left_frame, text="Threshold to Peak Parameters", padx=8, pady=6)

    t2p_thresh_var = tk.DoubleVar(value=DEFAULT_CONFIG["THRESHOLD_TO_PEAK_THRESHOLD"])
    t2p_before_var = tk.DoubleVar(value=DEFAULT_CONFIG["THRESHOLD_TO_PEAK_TIME_BEFORE"])
    t2p_after_var = tk.DoubleVar(value=DEFAULT_CONFIG["THRESHOLD_TO_PEAK_TIME_AFTER"])

    for label, var, lo, hi, res in [
        ("Start Threshold:", t2p_thresh_var, 0.0001, 1.0, 0.001),
        ("Time Before Threshold (s):", t2p_before_var, 0.0, 5.0, 0.01),
        ("Time After Peak (s):", t2p_after_var, 0.0, 5.0, 0.01),
    ]:
        _r = tk.Frame(t2p_frame); _r.pack(fill=tk.X, pady=1)
        tk.Label(_r, text=label, width=24, anchor="w").pack(side=tk.LEFT)
        tk.Scale(_r, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                 variable=var, length=150).pack(side=tk.LEFT)

    # Map method name -> (param frame, pack kwargs)
    _METHOD_FRAMES = {
        "Peak Force": pf_frame,
        "Threshold": thresh_frame,
        "Threshold %": thresh_pct_frame,
        "Threshold to Peak": t2p_frame,
    }

    def _update_trim_method_ui(*_):
        for f in _METHOD_FRAMES.values():
            f.pack_forget()
        selected = trim_method_var.get()
        if selected in _METHOD_FRAMES:
            _METHOD_FRAMES[selected].pack(fill=tk.X, pady=(0, 6))

    trim_method_var.trace_add("write", _update_trim_method_ui)

    # Show default method params immediately
    _update_trim_method_ui()

    # Ã¢â€â‚¬Ã¢â€â‚¬ Section: Peak-Based Fine Tuning Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    peak_ft_frame = tk.LabelFrame(left_frame, text="Peak-Based Fine Tuning", padx=8, pady=6)
    peak_ft_frame.pack(fill=tk.X, pady=(0, 6))

    refine_var = tk.BooleanVar(value=DEFAULT_CONFIG["REFINE_WITH_PEAKS"])
    tk.Checkbutton(peak_ft_frame, text="Enable Peak-Based Fine Tuning",
                   variable=refine_var).pack(anchor="w")

    # Channel selector
    _r = tk.Frame(peak_ft_frame); _r.pack(fill=tk.X, pady=2)
    tk.Label(_r, text="Peak Detection Channel:").pack(side=tk.LEFT)
    peak_channel_var = tk.StringVar(value=DEFAULT_CONFIG["PEAK_DETECTION_CHANNEL"])
    peak_channel_combo = ttk.Combobox(_r, textvariable=peak_channel_var,
                                      values=["AI0 (V)", "AI2 (V)"], state="readonly", width=12)
    peak_channel_combo.pack(side=tk.LEFT, padx=4)

    # Refinement method selector
    _r = tk.Frame(peak_ft_frame); _r.pack(fill=tk.X, pady=2)
    tk.Label(_r, text="Refinement Method:").pack(side=tk.LEFT)
    PEAK_METHODS = [
        "First-to-Last Peak",
        "Sliding Window (Max Density)",
        "Center Peak (Fixed Window)",
        "Fine-Tuned Drop Detection",
    ]
    peak_method_var = tk.StringVar(value=DEFAULT_CONFIG["PEAK_REFINEMENT_METHOD"])
    peak_method_combo = ttk.Combobox(_r, textvariable=peak_method_var,
                                     values=PEAK_METHODS, state="readonly", width=28)
    peak_method_combo.pack(side=tk.LEFT, padx=4)

    # Ã¢â€â‚¬Ã¢â€â‚¬ First-to-Last Peak params Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    ftlp_frame = tk.Frame(peak_ft_frame)

    ftlp_before_var = tk.DoubleVar(value=DEFAULT_CONFIG["EXTRA_TIME_BEFORE_FIRST_PEAK_MS"])
    ftlp_after_var = tk.DoubleVar(value=DEFAULT_CONFIG["EXTRA_TIME_AFTER_LAST_PEAK_MS"])

    _r = tk.Frame(ftlp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Label(_r, text="Extra Before First Peak (ms):", width=27, anchor="w").pack(side=tk.LEFT)
    tk.Scale(_r, from_=0.0, to=500.0, resolution=5.0, orient=tk.HORIZONTAL,
             variable=ftlp_before_var, length=150).pack(side=tk.LEFT)

    _r = tk.Frame(ftlp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Label(_r, text="Extra After Last Peak (ms):", width=27, anchor="w").pack(side=tk.LEFT)
    tk.Scale(_r, from_=0.0, to=500.0, resolution=5.0, orient=tk.HORIZONTAL,
             variable=ftlp_after_var, length=150).pack(side=tk.LEFT)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Sliding Window / Center Peak params Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    sw_frame = tk.Frame(peak_ft_frame)

    sw_left_var = tk.DoubleVar(value=DEFAULT_CONFIG["PEAK_WINDOW_LEFT"])
    sw_right_var = tk.DoubleVar(value=DEFAULT_CONFIG["PEAK_WINDOW_RIGHT"])
    sw_stride_var = tk.DoubleVar(value=DEFAULT_CONFIG["PEAK_STRIDE_DURATION"])

    _r = tk.Frame(sw_frame); _r.pack(fill=tk.X, pady=1)
    tk.Label(_r, text="Window Before Peak (s):", width=24, anchor="w").pack(side=tk.LEFT)
    tk.Scale(_r, from_=0.001, to=0.5, resolution=0.001, orient=tk.HORIZONTAL,
             variable=sw_left_var, length=150).pack(side=tk.LEFT)

    _r = tk.Frame(sw_frame); _r.pack(fill=tk.X, pady=1)
    tk.Label(_r, text="Window After Peak (s):", width=24, anchor="w").pack(side=tk.LEFT)
    tk.Scale(_r, from_=0.001, to=0.5, resolution=0.001, orient=tk.HORIZONTAL,
             variable=sw_right_var, length=150).pack(side=tk.LEFT)

    sw_stride_row = tk.Frame(sw_frame)
    sw_stride_row.pack(fill=tk.X, pady=1)
    tk.Label(sw_stride_row, text="Stride (s):", width=24, anchor="w").pack(side=tk.LEFT)
    tk.Scale(sw_stride_row, from_=0.005, to=0.05, resolution=0.005, orient=tk.HORIZONTAL,
             variable=sw_stride_var, length=150).pack(side=tk.LEFT)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Fine-Tuned Drop Detection params Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    ftdd_frame = tk.Frame(peak_ft_frame)

    ftdd_period_var = tk.DoubleVar(value=DEFAULT_CONFIG["DROP_MEASUREMENT_PERIOD"])
    ftdd_before_var = tk.DoubleVar(value=DEFAULT_CONFIG["DROP_SEARCH_WINDOW_BEFORE"])
    ftdd_after_var = tk.DoubleVar(value=DEFAULT_CONFIG["DROP_SEARCH_WINDOW_AFTER"])
    ftdd_left_var = tk.DoubleVar(value=DEFAULT_CONFIG["PEAK_WINDOW_LEFT"])
    ftdd_right_var = tk.DoubleVar(value=DEFAULT_CONFIG["PEAK_WINDOW_RIGHT"])

    for label, var, lo, hi, res in [
        ("Drop Meas. Period (ms):", ftdd_period_var, 1.0, 1000.0, 1.0),
        ("Search Before Drop (s):", ftdd_before_var, 0.01, 1.0, 0.01),
        ("Search After Drop (s):", ftdd_after_var, 0.01, 1.0, 0.01),
        ("Final Window Before (s):", ftdd_left_var, 0.001, 0.5, 0.001),
        ("Final Window After (s):", ftdd_right_var, 0.001, 0.5, 0.001),
    ]:
        _r = tk.Frame(ftdd_frame); _r.pack(fill=tk.X, pady=1)
        tk.Label(_r, text=label, width=24, anchor="w").pack(side=tk.LEFT)
        tk.Scale(_r, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                 variable=var, length=150).pack(side=tk.LEFT)

    _PEAK_METHOD_FRAMES = {
        "First-to-Last Peak": ftlp_frame,
        "Sliding Window (Max Density)": sw_frame,
        "Center Peak (Fixed Window)": sw_frame,   # same sliders, stride row toggled
        "Fine-Tuned Drop Detection": ftdd_frame,
    }

    def _update_peak_method_ui(*_):
        for f in set(_PEAK_METHOD_FRAMES.values()):
            f.pack_forget()
        sw_stride_row.pack_forget()

        if not refine_var.get():
            return

        pm = peak_method_var.get()
        frame = _PEAK_METHOD_FRAMES.get(pm)
        if frame:
            frame.pack(fill=tk.X, pady=(4, 0))
        if pm == "Sliding Window (Max Density)":
            sw_stride_row.pack(fill=tk.X, pady=1)

    def _update_peak_refinement_ui(*_):
        enabled = refine_var.get()
        peak_channel_combo.configure(state="readonly" if enabled else tk.DISABLED)
        peak_method_combo.configure(state="readonly" if enabled else tk.DISABLED)
        _update_peak_method_ui()
        # show/hide find_peaks section
        if enabled:
            fp_frame.pack(fill=tk.X, pady=(0, 6))
        else:
            fp_frame.pack_forget()

    refine_var.trace_add("write", _update_peak_refinement_ui)
    peak_method_var.trace_add("write", _update_peak_method_ui)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Section: find_peaks Parameters Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    fp_frame = tk.LabelFrame(left_frame, text="find_peaks Parameters (scipy)", padx=8, pady=6)

    # Height
    height_enable_var = tk.BooleanVar(value=DEFAULT_PEAK_PARAMS["height"] is not None)
    height_var = tk.DoubleVar(value=DEFAULT_PEAK_PARAMS["height"] or 0.4)

    _r = tk.Frame(fp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Checkbutton(_r, text="Height:", variable=height_enable_var, width=14, anchor="w").pack(side=tk.LEFT)
    tk.Entry(_r, textvariable=height_var, width=7).pack(side=tk.LEFT)

    # Distance
    distance_var = tk.IntVar(value=DEFAULT_PEAK_PARAMS["distance"])
    _r = tk.Frame(fp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Label(_r, text="Distance (samples):", width=20, anchor="w").pack(side=tk.LEFT)
    tk.Entry(_r, textvariable=distance_var, width=7).pack(side=tk.LEFT)

    # Threshold
    threshold_var = tk.DoubleVar(value=DEFAULT_PEAK_PARAMS["threshold"])
    _r = tk.Frame(fp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Label(_r, text="Threshold:", width=20, anchor="w").pack(side=tk.LEFT)
    tk.Entry(_r, textvariable=threshold_var, width=7).pack(side=tk.LEFT)

    # Prominence
    prom_enable_var = tk.BooleanVar(value=DEFAULT_PEAK_PARAMS["prominence"] is not None)
    prom_var = tk.DoubleVar(value=DEFAULT_PEAK_PARAMS["prominence"] or 0.1)

    _r = tk.Frame(fp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Checkbutton(_r, text="Prominence:", variable=prom_enable_var, width=14, anchor="w").pack(side=tk.LEFT)
    tk.Entry(_r, textvariable=prom_var, width=7).pack(side=tk.LEFT)

    # Width
    width_enable_var = tk.BooleanVar(value=False)
    width_var = tk.IntVar(value=1)

    _r = tk.Frame(fp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Checkbutton(_r, text="Width (samples):", variable=width_enable_var, width=14, anchor="w").pack(side=tk.LEFT)
    tk.Entry(_r, textvariable=width_var, width=7).pack(side=tk.LEFT)

    # Rel height
    rel_height_var = tk.DoubleVar(value=DEFAULT_PEAK_PARAMS["rel_height"])
    _r = tk.Frame(fp_frame); _r.pack(fill=tk.X, pady=1)
    tk.Label(_r, text="Rel. Height:", width=20, anchor="w").pack(side=tk.LEFT)
    tk.Entry(_r, textvariable=rel_height_var, width=7).pack(side=tk.LEFT)

    # Show/hide find_peaks section based on initial refine state
    if refine_var.get():
        fp_frame.pack(fill=tk.X, pady=(0, 6))

    # Show initial peak method params
    _update_peak_method_ui()

    # Ã¢â€â‚¬Ã¢â€â‚¬ Buttons Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    btn_frame = tk.Frame(left_frame)
    btn_frame.pack(fill=tk.X, pady=(6, 4))

    def reset_to_defaults():
        global trim_result, detected_peaks_arr
        trim_method_var.set(DEFAULT_CONFIG["TRIM_METHOD"])
        pf_before_var.set(DEFAULT_CONFIG["PEAK_FORCE_TIME_BEFORE"])
        pf_after_var.set(DEFAULT_CONFIG["PEAK_FORCE_TIME_AFTER"])
        thresh_start_var.set(DEFAULT_CONFIG["START_THRESHOLD"])
        thresh_stop_var.set(DEFAULT_CONFIG["STOP_THRESHOLD"])
        thresh_hyst_var.set(DEFAULT_CONFIG["HYSTERESIS"])
        thresh_extra_var.set(DEFAULT_CONFIG["EXTRA_TIME_SECONDS"])
        thresh_mindur_var.set(DEFAULT_CONFIG["MIN_DURATION_BELOW_THRESHOLD"])
        thresh_start_extra_var.set(DEFAULT_CONFIG["START_EXTRA_TIME_SECONDS"])
        thresh_pct_start_var.set(DEFAULT_CONFIG["START_THRESHOLD_PCT"])
        thresh_pct_stop_var.set(DEFAULT_CONFIG["STOP_THRESHOLD_PCT"])
        thresh_pct_hyst_var.set(DEFAULT_CONFIG["HYSTERESIS_PCT"])
        t2p_thresh_var.set(DEFAULT_CONFIG["THRESHOLD_TO_PEAK_THRESHOLD"])
        t2p_before_var.set(DEFAULT_CONFIG["THRESHOLD_TO_PEAK_TIME_BEFORE"])
        t2p_after_var.set(DEFAULT_CONFIG["THRESHOLD_TO_PEAK_TIME_AFTER"])
        refine_var.set(DEFAULT_CONFIG["REFINE_WITH_PEAKS"])
        peak_channel_var.set(DEFAULT_CONFIG["PEAK_DETECTION_CHANNEL"])
        peak_method_var.set(DEFAULT_CONFIG["PEAK_REFINEMENT_METHOD"])
        ftlp_before_var.set(DEFAULT_CONFIG["EXTRA_TIME_BEFORE_FIRST_PEAK_MS"])
        ftlp_after_var.set(DEFAULT_CONFIG["EXTRA_TIME_AFTER_LAST_PEAK_MS"])
        sw_left_var.set(DEFAULT_CONFIG["PEAK_WINDOW_LEFT"])
        sw_right_var.set(DEFAULT_CONFIG["PEAK_WINDOW_RIGHT"])
        sw_stride_var.set(DEFAULT_CONFIG["PEAK_STRIDE_DURATION"])
        height_enable_var.set(True)
        height_var.set(DEFAULT_PEAK_PARAMS["height"])
        distance_var.set(DEFAULT_PEAK_PARAMS["distance"])
        threshold_var.set(DEFAULT_PEAK_PARAMS["threshold"])
        prom_enable_var.set(True)
        prom_var.set(DEFAULT_PEAK_PARAMS["prominence"])
        width_enable_var.set(False)
        rel_height_var.set(DEFAULT_PEAK_PARAMS["rel_height"])
        trim_result = None
        detected_peaks_arr = None
        _draw_plot()

    preview_btn = tk.Button(btn_frame, text="Preview Trim Window",
                            command=lambda: _run_preview(),
                            bg="lightgreen", width=20,
                            font=tkfont.Font(font=tkfont.nametofont("TkDefaultFont"), weight="bold"))
    preview_btn.pack(side=tk.LEFT, padx=(0, 8))

    reset_btn = tk.Button(btn_frame, text="Reset Defaults", command=reset_to_defaults, width=14)
    reset_btn.pack(side=tk.LEFT)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Status label Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    status_var = tk.StringVar(value="Ready Ã¢â‚¬â€ load a parquet file to begin")
    tk.Label(left_frame, textvariable=status_var,
             fg="blue", relief=tk.SUNKEN, anchor="w").pack(fill=tk.X, pady=(4, 0))

    # ============================================================================
    # RIGHT PANEL Ã¢â‚¬â€ matplotlib figure
    # ============================================================================
    fig, axes = plt.subplots(4, 1, figsize=(9, 9), sharex=True)
    fig.tight_layout(pad=2.5)

    canvas_fig = FigureCanvasTkAgg(fig, master=right_frame)
    canvas_fig.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ============================================================================
    # TAB 2 Ã¢â‚¬â€ Feature Extraction
    # ============================================================================

    # Ã¢â€â‚¬Ã¢â€â‚¬ Left control panel Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    feat_left = tk.Frame(tab_feat, width=310)
    feat_left.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0), pady=8)
    feat_left.pack_propagate(False)

    # Signal info
    feat_info_frame = tk.LabelFrame(feat_left, text="Current Signal", padx=8, pady=6)
    feat_info_frame.pack(fill=tk.X, pady=(0, 6))

    feat_file_var = tk.StringVar(value="No file loaded")
    tk.Label(feat_info_frame, textvariable=feat_file_var,
             fg="blue", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w")

    feat_trim_var = tk.StringVar(value="No trim window set")
    tk.Label(feat_info_frame, textvariable=feat_trim_var,
             fg="darkgreen", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w")

    # Model path info
    feat_model_frame = tk.LabelFrame(feat_left, text="Model", padx=8, pady=6)
    feat_model_frame.pack(fill=tk.X, pady=(0, 6))

    feat_model_var = tk.StringVar(value="Standalone Ensemble 20260505")
    # Dropdown for models
    feat_model_cb = ttk.Combobox(feat_model_frame, textvariable=feat_model_var, 
                                 values=list(AVAILABLE_MODELS_DICT.keys()), state="readonly")
    feat_model_cb.pack(fill=tk.X, pady=(0, 4))
    feat_model_cb.set("Standalone Ensemble 20260505")

    feat_model_status_var = tk.StringVar()
    feat_model_status_label = tk.Label(feat_model_frame, textvariable=feat_model_status_var,
                                       anchor="w", wraplength=270, justify=tk.LEFT)
    feat_model_status_label.pack(anchor="w")

    def _update_model_status(*args):
        sel_mdl = feat_model_var.get()
        if sel_mdl in AVAILABLE_MODELS_DICT:
            m_dir = AVAILABLE_MODELS_DICT[sel_mdl]
            p1 = os.path.join(m_dir, "weighted_ensemble_model.joblib")
            p2 = os.path.join(m_dir, "elastic_net_model_pipeline.pkl")
            m_path = p2 if os.path.exists(p2) else p1
            m_exists = os.path.exists(m_path)
            feat_model_status_var.set(
                f"Model: {'found' if m_exists else 'NOT FOUND'}  ({os.path.basename(m_path)})"
            )
            feat_model_status_label.config(fg="darkgreen" if m_exists else "red")

    feat_model_var.trace_add("write", _update_model_status)
    _update_model_status()

    feat_extractor_status_var = tk.StringVar(
        value=f"Extractor: {'found' if os.path.isdir(FEATURE_EXTRACTION_DIR) else 'NOT FOUND'}"
    )
    tk.Label(feat_model_frame, textvariable=feat_extractor_status_var,
             fg="darkgreen" if os.path.isdir(FEATURE_EXTRACTION_DIR) else "red",
             anchor="w").pack(anchor="w")

    # Extract button
    feat_extract_btn = tk.Button(
        feat_left, text="Extract Features & Predict",
        command=lambda: _start_extraction(),
        bg="lightgreen", relief=tk.RAISED,
        font=tkfont.Font(font=tkfont.nametofont("TkDefaultFont"), weight="bold"),
    )
    feat_extract_btn.pack(fill=tk.X, pady=(8, 2))

    # Cancel button
    feat_cancel_btn = tk.Button(
        feat_left, text="Cancel",
        command=lambda: _cancel_extraction(),
        bg="salmon", state=tk.DISABLED,
    )
    feat_cancel_btn.pack(fill=tk.X, pady=(0, 8))

    # -- Audio Normalization ---------------------------------------------------
    norm_frame = tk.LabelFrame(feat_left, text="Audio Normalization", padx=8, pady=6)
    norm_frame.pack(fill=tk.X, pady=(0, 6))

    norm_enable_var = tk.BooleanVar(value=True)
    tk.Checkbutton(norm_frame, text="Enable Audio Normalization",
                   variable=norm_enable_var).pack(anchor="w")

    _norm_method_row = tk.Frame(norm_frame)
    _norm_method_row.pack(fill=tk.X, pady=(4, 0))
    tk.Label(_norm_method_row, text="Method:", width=10, anchor="w").pack(side=tk.LEFT)
    norm_method_var = tk.StringVar(value="peak")
    norm_method_cb = ttk.Combobox(_norm_method_row, textvariable=norm_method_var,
                                   values=["peak", "rms"], state="readonly", width=8)
    norm_method_cb.pack(side=tk.LEFT)

    _norm_target_label_val = tk.Label(norm_frame, text="Target Level:  0.90", anchor="w")
    _norm_target_label_val.pack(anchor="w", pady=(4, 0))

    norm_target_var = tk.DoubleVar(value=0.9)
    norm_target_scale = tk.Scale(norm_frame, variable=norm_target_var,
                                  from_=0.1, to=1.0, resolution=0.05,
                                  orient=tk.HORIZONTAL, showvalue=False)
    norm_target_scale.pack(fill=tk.X)

    def _update_norm_target_label(*_):
        _norm_target_label_val.config(text=f"Target Level:  {norm_target_var.get():.2f}")

    norm_target_var.trace_add("write", _update_norm_target_label)

    # Progress bar
    feat_progress_var = tk.DoubleVar(value=0.0)
    ttk.Progressbar(feat_left, variable=feat_progress_var, maximum=100, length=280).pack(fill=tk.X, pady=(0, 4))

    feat_status_var = tk.StringVar(value="Ready Ã¢â‚¬â€ load a file and set trim window first")
    tk.Label(feat_left, textvariable=feat_status_var,
             fg="gray", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w", pady=(0, 8))

    # Ã¢â€â‚¬Ã¢â€â‚¬ Right results panel Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    feat_right = tk.Frame(tab_feat)
    feat_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

    # Prediction result label
    feat_pred_frame = tk.LabelFrame(feat_right, text="Prediction", padx=8, pady=8)
    feat_pred_frame.pack(fill=tk.X, pady=(0, 8))

    feat_class_var = tk.StringVar(value="Ã¢â‚¬â€")
    feat_class_label = tk.Label(
        feat_pred_frame, textvariable=feat_class_var,
        font=tkfont.Font(font=tkfont.nametofont("TkDefaultFont"), size=28, weight="bold"),
        fg="steelblue",
    )
    feat_class_label.pack(pady=4)

    feat_conf_var = tk.StringVar(value="")
    tk.Label(feat_pred_frame, textvariable=feat_conf_var, fg="gray").pack()

    feat_missing_var = tk.StringVar(value="")
    feat_missing_label = tk.Label(feat_pred_frame, textvariable=feat_missing_var,
                                   fg="gray", wraplength=600, justify="left",
                                   font=("TkDefaultFont", 9))
    feat_missing_label.pack(pady=(4, 0))

    # Probability bar chart
    feat_fig, feat_ax = plt.subplots(1, 1, figsize=(8, 5))
    feat_fig.tight_layout(pad=2.0)
    feat_canvas = FigureCanvasTkAgg(feat_fig, master=feat_right)
    feat_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # Update info when switching tabs
    def _on_tab_change(event):
        selected = notebook.select()
        if selected == str(tab_feat):
            _update_feat_info()
        elif selected == str(tab_deep):
            _update_deep_info()

    notebook.bind("<<NotebookTabChanged>>", _on_tab_change)

    def _update_feat_info():
        if sp_loaded_df is not None and sp_loaded_filename is not None:
            sr = sampling_rate_var.get()
            n = len(sp_loaded_df)
            feat_file_var.set(f"File: {sp_loaded_filename}\n{n:,} samples @ {sr} Hz")
        else:
            feat_file_var.set("No file loaded")

        if trim_result is not None:
            s, e, dur = trim_result
            feat_trim_var.set(f"Trim: sample {s} Ã¢â€ â€™ {e}  ({dur:.4f} s)")
        else:
            feat_trim_var.set("No trim window Ã¢â‚¬â€ run Preview Trim Window first")

    # ============================================================================
    # Logic Ã¢â‚¬â€ Set Window
    # ============================================================================

    def _build_config() -> dict:
        """Assemble current config dict from all GUI variables."""
        peak_params = {}
        if height_enable_var.get():
            peak_params["height"] = height_var.get()
        try:
            d = distance_var.get()
            if d >= 1:
                peak_params["distance"] = d
        except Exception:
            pass
        t = threshold_var.get()
        if t > 0:
            peak_params["threshold"] = t
        if prom_enable_var.get():
            peak_params["prominence"] = prom_var.get()
        if width_enable_var.get():
            peak_params["width"] = width_var.get()
        peak_params["rel_height"] = rel_height_var.get()

        cfg = {
            "ACOUSTIC_COLUMN": "AI0 (V)",
            "REFERENCE_COLUMN": "AI1 (V)",
            "SAMPLING_RATE": sampling_rate_var.get(),
            "TRIM_METHOD": trim_method_var.get(),
            # Threshold
            "START_THRESHOLD": thresh_start_var.get(),
            "START_EXTRA_TIME_SECONDS": thresh_start_extra_var.get(),
            "STOP_THRESHOLD": thresh_stop_var.get(),
            "HYSTERESIS": thresh_hyst_var.get(),
            "EXTRA_TIME_SECONDS": thresh_extra_var.get(),
            "MIN_DURATION_BELOW_THRESHOLD": thresh_mindur_var.get(),
            # Threshold %
            "START_THRESHOLD_PCT": thresh_pct_start_var.get(),
            "STOP_THRESHOLD_PCT": thresh_pct_stop_var.get(),
            "HYSTERESIS_PCT": thresh_pct_hyst_var.get(),
            # Peak Force
            "PEAK_FORCE_TIME_BEFORE": pf_before_var.get(),
            "PEAK_FORCE_TIME_AFTER": pf_after_var.get(),
            # Threshold to Peak
            "THRESHOLD_TO_PEAK_THRESHOLD": t2p_thresh_var.get(),
            "THRESHOLD_TO_PEAK_TIME_BEFORE": t2p_before_var.get(),
            "THRESHOLD_TO_PEAK_TIME_AFTER": t2p_after_var.get(),
            # Peak refinement
            "REFINE_WITH_PEAKS": refine_var.get(),
            "PEAK_DETECTION_CHANNEL": peak_channel_var.get(),
            "PEAK_REFINEMENT_METHOD": peak_method_var.get(),
            "EXTRA_TIME_BEFORE_FIRST_PEAK_MS": ftlp_before_var.get(),
            "EXTRA_TIME_AFTER_LAST_PEAK_MS": ftlp_after_var.get(),
            "PEAK_WINDOW_LEFT": sw_left_var.get(),
            "PEAK_WINDOW_RIGHT": sw_right_var.get(),
            "PEAK_WINDOW_DURATION": sw_left_var.get() + sw_right_var.get(),
            "PEAK_STRIDE_DURATION": sw_stride_var.get(),
            "DROP_MEASUREMENT_PERIOD": ftdd_period_var.get(),
            "DROP_SEARCH_WINDOW_BEFORE": ftdd_before_var.get(),
            "DROP_SEARCH_WINDOW_AFTER": ftdd_after_var.get(),
            # Filter (disabled)
            "ENABLE_FILTER": False,
            "FILTER_PARAMS": {},
            "FILTER_CHAIN": [],
            "FADE_DURATION_MS": 10.0,
            "PEAK_PARAMS": peak_params,
        }
        return cfg


    def _run_preview():
        global trim_result, detected_peaks_arr
        if sp_loaded_df is None:
            messagebox.showwarning("No File", "Please load a parquet file first.")
            return

        cfg = _build_config()
        acoustic_col = cfg["ACOUSTIC_COLUMN"]
        reference_col = cfg["REFERENCE_COLUMN"]
        sr = cfg["SAMPLING_RATE"]

        if acoustic_col not in sp_loaded_df.columns or reference_col not in sp_loaded_df.columns:
            messagebox.showerror(
                "Column Error",
                f"Required columns not found.\nAvailable: {sp_loaded_df.columns.tolist()}"
            )
            return

        try:
            processor = SignalProcessor(cfg)
            acoustic = sp_loaded_df[acoustic_col].values.astype(np.float32)
            reference = sp_loaded_df[reference_col].values.astype(np.float32)
            ai2 = (sp_loaded_df["AI2 (V)"].values.astype(np.float32)
                   if "AI2 (V)" in sp_loaded_df.columns else None)

            tm = cfg["TRIM_METHOD"]

            if tm == "Peak Force":
                trimmed, s, e = processor.trim_by_peak_force(acoustic, reference, ai2)
            elif tm == "Threshold to Peak":
                trimmed, s, e = processor.trim_by_threshold_to_peak(acoustic, reference, ai2)
            elif tm == "Threshold %":
                trimmed, s, e = processor.trim_by_threshold_pct(acoustic, reference, ai2)
            else:
                trimmed, s, e = processor.trim_by_threshold(acoustic, reference, ai2)

            if len(trimmed) == 0:
                messagebox.showwarning("Empty Trim",
                                       "Trim returned an empty signal Ã¢â‚¬â€ adjust parameters.")
                return

            duration_s = len(trimmed) / sr
            trim_result = (s, e, duration_s)
            status_var.set(
                f"Trim window ({tm}): sample {s} Ã¢â€ â€™ {e}  |  {duration_s:.4f} s"
            )

            # Peak-based refinement (First-to-Last Peak): narrow trim window to acoustic peaks
            detected_peaks_arr = None
            if cfg.get("REFINE_WITH_PEAKS") and cfg.get("PEAK_REFINEMENT_METHOD") == "First-to-Last Peak":
                pk_ch = cfg.get("PEAK_DETECTION_CHANNEL", acoustic_col)
                pk_sig = (sp_loaded_df[pk_ch].values.astype(np.float32)[s:e]
                          if pk_ch in sp_loaded_df.columns else acoustic[s:e])
                pk_kwargs = {k: v for k, v in cfg["PEAK_PARAMS"].items() if v is not None}
                try:
                    pks, _ = find_peaks(pk_sig, **pk_kwargs)
                    if len(pks) > 0:
                        extra_before = int(cfg.get("EXTRA_TIME_BEFORE_FIRST_PEAK_MS", 85.0) / 1000.0 * sr)
                        extra_after  = int(cfg.get("EXTRA_TIME_AFTER_LAST_PEAK_MS",  85.0) / 1000.0 * sr)
                        refined_s = max(0,              s + pks[0]  - extra_before)
                        refined_e = min(len(acoustic),  s + pks[-1] + extra_after)
                        s, e = refined_s, refined_e
                        trimmed = acoustic[s:e]
                        duration_s = len(trimmed) / sr
                        trim_result = (s, e, duration_s)
                        # recalculate peaks relative to new (refined) s
                        pk_sig2 = (sp_loaded_df[pk_ch].values.astype(np.float32)[s:e]
                                   if pk_ch in sp_loaded_df.columns else acoustic[s:e])
                        pks2, _ = find_peaks(pk_sig2, **pk_kwargs)
                        detected_peaks_arr = pks2
                        status_var.set(
                            f"Trim window ({tm}, refined): sample {s} -> {e}  |  {duration_s:.4f} s"
                            f"  |  {len(pks2)} peaks"
                        )
                    else:
                        status_var.set(status_var.get() + "  |  no peaks found, keeping initial window")
                except Exception as pe:
                    print(f"Peak refinement warning: {pe}")

            _draw_plot()

        except Exception as exc:
            messagebox.showerror("Trim Error", f"Trim failed:\n{exc}")
            import traceback; traceback.print_exc()


    def _draw_plot():
        """Redraw the 4-panel figure."""
        for ax in axes:
            ax.clear()

        if sp_loaded_df is None:
            for ax in axes:
                ax.set_visible(False)
            fig.canvas.draw()
            return

        for ax in axes:
            ax.set_visible(True)

        sr = sampling_rate_var.get()
        time_axis = np.arange(len(sp_loaded_df)) / sr
        cols = sp_loaded_df.columns.tolist()

        # colours / column names
        channels = [
            ("AI0 (V)", "steelblue", "Amplitude (V)"),
            ("AI1 (V)", "mediumpurple", "Amplitude (V)"),
            ("AI2 (V)", "seagreen", "Amplitude (V)"),
            ("AI3 (mV)", "tomato", "Amplitude (mV)"),
        ]

        has_trim = trim_result is not None
        s_idx, e_idx = (trim_result[0], trim_result[1]) if has_trim else (0, 0)

        for ax, (col, colour, ylabel) in zip(axes, channels):
            if col in cols:
                data = sp_loaded_df[col].values
                # DC-remove reference for display
                if col == "AI1 (V)" and has_trim:
                    half = max(1, int(0.5 * sr))
                    dc = np.mean(data[:half])
                    data = data - dc
                    ax.set_title(f"{col}  (DC removed: {dc:.3f} V)", fontsize=9)
                else:
                    ax.set_title(col, fontsize=9)

                ax.plot(time_axis, data, color=colour, linewidth=0.4, alpha=0.85, label=col)

                if has_trim and s_idx < e_idx:
                    ax.axvspan(s_idx / sr, e_idx / sr, color="orange", alpha=0.25,
                               label="Trim Window")

                # Peak markers on AI0 (or peak detection channel) when First-to-Last
                if (col == "AI0 (V)" and has_trim and detected_peaks_arr is not None
                        and len(detected_peaks_arr) > 0
                        and peak_channel_var.get() == "AI0 (V)"):
                    pk_abs = detected_peaks_arr + s_idx
                    ax.plot(pk_abs / sr,
                            sp_loaded_df["AI0 (V)"].values[pk_abs],
                            "r^", markersize=5, zorder=5,
                            label=f"{len(detected_peaks_arr)} peaks")

                if (col == "AI2 (V)" and has_trim and detected_peaks_arr is not None
                        and len(detected_peaks_arr) > 0
                        and peak_channel_var.get() == "AI2 (V)"):
                    pk_abs = detected_peaks_arr + s_idx
                    ax.plot(pk_abs / sr,
                            sp_loaded_df["AI2 (V)"].values[pk_abs],
                            "r^", markersize=5, zorder=5,
                            label=f"{len(detected_peaks_arr)} peaks")

                ax.set_ylabel(ylabel, fontsize=8)
                ax.legend(loc="upper right", fontsize=7)
                ax.grid(True, alpha=0.3)
            else:
                ax.set_title(f"{col}  (not in file)", fontsize=9, color="gray")
                ax.text(0.5, 0.5, "Channel not available", transform=ax.transAxes,
                        ha="center", va="center", color="gray")

        axes[-1].set_xlabel("Time (s)", fontsize=9)
        fig.tight_layout(pad=2.0)
        fig.canvas.draw()


    # ============================================================================
    # Logic Ã¢â‚¬â€ Feature Extraction
    # ============================================================================

    def _draw_proba_plot(classes, proba):
        feat_ax.clear()
        colors = ["forestgreen" if p == max(proba) else "steelblue" for p in proba]
        bars = feat_ax.bar(range(len(classes)), proba, color=colors,
                           edgecolor="black", linewidth=0.5, tick_label=classes)
        feat_ax.set_ylim(0, 1.05)
        feat_ax.set_ylabel("Probability")
        feat_ax.set_title("Class Probabilities")
        for bar, p in zip(bars, proba):
            feat_ax.text(bar.get_x() + bar.get_width() / 2,
                         bar.get_height() + 0.02,
                         f"{p:.3f}", ha="center", va="bottom", fontsize=9)
        feat_fig.tight_layout(pad=2.0)
        feat_canvas.draw()


    def _start_extraction():
        global feat_thread
        if feat_thread is not None and feat_thread.is_alive():
            return

        if sp_loaded_df is None:
            messagebox.showwarning("No File", "Please load a parquet file first.")
            return
        if trim_result is None:
            messagebox.showwarning("No Trim",
                                   "Please set a trim window first.\n"
                                   "Go to 'Set Window' and click 'Preview Trim Window'.")
            return

        sel_mdl = feat_model_var.get()
        m_dir = AVAILABLE_MODELS_DICT.get(sel_mdl)
        if not m_dir:
            messagebox.showerror("Configuration Error", "Selected model directory not found in configuration.")
            return

        p1 = os.path.join(m_dir, "weighted_ensemble_model.joblib")
        p2 = os.path.join(m_dir, "elastic_net_model_pipeline.pkl")
        m_path = p2 if os.path.exists(p2) else p1

        if not os.path.exists(m_path):
            messagebox.showerror("Model Not Found", f"Model file not found:\n{m_path}")
            return
        if not os.path.isdir(FEATURE_EXTRACTION_DIR):
            messagebox.showerror("Extractor Not Found",
                                 f"Feature extraction directory not found:\n{FEATURE_EXTRACTION_DIR}")
            return

        feat_cancel_flag.clear()
        feat_extract_btn.config(state=tk.DISABLED)
        feat_cancel_btn.config(state=tk.NORMAL)
        feat_status_var.set("Starting feature extractionÃ¢â‚¬Â¦")
        feat_progress_var.set(0)
        feat_class_var.set("Ã¢â‚¬â€")
        feat_conf_var.set("")
        feat_missing_var.set("")
        feat_missing_label.config(fg="gray")

        s_idx, e_idx, dur = trim_result
        signal = sp_loaded_df["AI0 (V)"].values[s_idx:e_idx].astype(np.float64)
        sr = sampling_rate_var.get()

        if norm_enable_var.get():
            _norm_method = norm_method_var.get()
            _norm_target = norm_target_var.get()
            if _norm_method == "peak":
                _peak = np.max(np.abs(signal))
                if _peak > 0:
                    signal = signal / _peak * _norm_target
            elif _norm_method == "rms":
                _rms = np.sqrt(np.mean(signal ** 2))
                if _rms > 0:
                    signal = signal / _rms * _norm_target

        feat_thread = threading.Thread(
            target=_run_extraction_thread,
            args=(signal, sr, m_dir, m_path),
            daemon=True,
        )
        feat_thread.start()


    def _cancel_extraction():
        feat_cancel_flag.set()
        feat_status_var.set("CancellingÃ¢â‚¬Â¦")


    def _feat_progress(current, total, msg):
        root.after(0, lambda c=current, t=total, m=msg: _feat_progress_gui(c, t, m))


    def _feat_progress_gui(current, total, msg):
        pct = (current / total * 100) if total > 0 else 0
        feat_progress_var.set(pct)
        feat_status_var.set(msg)


    def _feat_done(pred_class, proba, classes, missing=None):
        root.after(0, lambda: _feat_done_gui(pred_class, proba, classes, missing or []))


    def _feat_done_gui(pred_class, proba, classes, missing=None):
        if missing is None:
            missing = []
        feat_extract_btn.config(state=tk.NORMAL)
        feat_cancel_btn.config(state=tk.DISABLED)
        feat_progress_var.set(100)
        top_prob = float(max(proba))
        n_total = len(classes)  # approximate â€” use required feature count if available
        if missing:
            warn = f"WARNING: {len(missing)} features zero-padded  |  Done: {pred_class} ({top_prob:.1%})"
            feat_status_var.set(warn)
            feat_missing_var.set(f"{len(missing)} features missing (zero-padded) â€” prediction may be unreliable!\n"
                                 + ", ".join(missing[:10]) + ("..." if len(missing) > 10 else ""))
            feat_missing_label.config(fg="red")
        else:
            feat_status_var.set(f"Done!  Predicted: {pred_class}  ({top_prob:.1%} confidence)  |  All features found.")
            feat_missing_var.set("All features extracted successfully.")
            feat_missing_label.config(fg="green")
        feat_class_var.set(str(pred_class))
        feat_conf_var.set(f"Confidence: {top_prob:.1%}")
        _draw_proba_plot(classes, proba)


    def _feat_error(error_msg, tb_str):
        root.after(0, lambda: _feat_error_gui(error_msg, tb_str))


    def _feat_error_gui(error_msg, tb_str):
        feat_extract_btn.config(state=tk.NORMAL)
        feat_cancel_btn.config(state=tk.DISABLED)
        feat_progress_var.set(0)
        feat_status_var.set(f"Error: {error_msg}")
        messagebox.showerror("Extraction Error", f"{error_msg}\n\n{tb_str[:800]}")


    def _run_extraction_thread(signal, sr, m_dir, m_path):
        """Background thread: extract features, then predict."""
        import traceback as _tb
        try:
            # --- Lazy imports (heavy) ---
            if FEATURE_EXTRACTION_DIR not in sys.path:
                sys.path.insert(0, FEATURE_EXTRACTION_DIR)
            if m_dir not in sys.path:
                sys.path.insert(0, m_dir)

            _feat_progress(0, 1, "Importing librariesÃ¢â‚¬Â¦")
            from feature_extraction_class import FeatureExtractor  # noqa: F401
            import joblib

            if feat_cancel_flag.is_set():
                _feat_progress(0, 1, "Cancelled.")
                return

            # --- Initialise extractor ---
            _feat_progress(0, 1, "Initialising extractorÃ¢â‚¬Â¦")
            extractor = FeatureExtractor(acoustic_column="AI0 (V)", sampling_rate=sr)

            # --- Load required features & build extraction plan ---
            required_features = load_required_features(m_dir)

            # Strip channel/normalisation suffix the extractor doesnâ€™t add
            _req_sfx = ("_AI0_PN", "_AI2_PN")
            required_features = [
                next((f[:-len(s)] for s in _req_sfx if f.endswith(s)), f)
                for f in required_features
            ]

            # Parse each feature â†’ (filter, config_or_None)
            plan = {}   # filter_name Ã¢â€ â€™ {"configs": set, "base_needed": bool}
            base_features_set = set()
            for feat_name in required_features:
                base, filt, cfg = parse_feature(feat_name)
                base_features_set.add(base)
                if filt is None:
                    continue
                if filt not in plan:
                    plan[filt] = {"configs": set(), "base_needed": False}
                if cfg is None:
                    plan[filt]["base_needed"] = True
                else:
                    plan[filt]["configs"].add(cfg)

            # Count total extraction calls
            n_calls = sum(
                len(v["configs"]) + (2 if v["base_needed"] else 0)
                for v in plan.values()
            )
            call_idx = 0

            all_extracted = {}   # final_feature_name Ã¢â€ â€™ value

            for filter_name, group in plan.items():
                if feat_cancel_flag.is_set():
                    _feat_progress(call_idx, n_calls, "Cancelled.")
                    return

                # Apply Butterworth filter
                filter_cfg = extractor.filter_configs.get(filter_name, {})
                _feat_progress(call_idx, n_calls,
                               f"Applying filter: {filter_name}Ã¢â‚¬Â¦")

                filtered = extractor.apply_butterworth_filter(
                    signal,
                    filter_cfg.get("low_freq"),
                    filter_cfg.get("high_freq"),
                    filter_cfg.get("order", 4),
                    filter_cfg.get("filter_type") or "lowpass",
                )
                if np.isnan(filtered).any():
                    filtered = np.nan_to_num(filtered, nan=0.0)

                filter_upper_cutoff = filter_cfg.get("high_freq")

                # --- Base call (no config suffix) ---
                if group["base_needed"]:
                    _feat_progress(call_idx, n_calls,
                                   f"Base features: {filter_name}  ({call_idx+1}/{n_calls})")
                    feats = extractor.extract_basic_features(
                        filtered, "default", filter_upper_cutoff, "default", "default",
                        selected_features=base_features_set
                    )
                    for k, v in feats.items():
                        if (not any(k.startswith(p) for p in ALL_SPECTRUM_PREFIXES)
                                and k not in PEAK_DEPENDENT_FEATURES):
                            all_extracted[f"{k}_{filter_name}"] = v
                    call_idx += 1

                    # --- Advanced call (burst, formant, microcrack, etc.) ---
                    _feat_progress(call_idx, n_calls,
                                   f"Advanced features: {filter_name}  ({call_idx+1}/{n_calls})")

                    def _safe_add(feats_dict):
                        if feats_dict:
                            for k, v in feats_dict.items():
                                all_extracted[f"{k}_{filter_name}"] = v

                    # The extract_advanced_features method already calls texture, microcrack, 
                    # energy decay, knock impact, short time energy internally!
                    _safe_add(extractor.extract_advanced_features(filtered, "default", selected_features=base_features_set))

                    # Only run extremely slow wavelet/fractal extractions if actually required
                    needs_wavelet = any("wavelet" in f for f in required_features)
                    needs_fractal = any("fractal" in f or "dfa" in f or "hfd" in f or "pfd" in f for f in required_features)

                    if needs_wavelet:
                        try:
                            print("Starting Wavelet features...")
                            _safe_add(extractor.extract_wavelet_features(filtered, selected_features=set(required_features)))
                        except Exception as e: print(f"Wavelet skipped: {e}")

                    if needs_fractal:
                        try:
                            print("Starting Fractal features...")
                            _safe_add(extractor.extract_fractal_features(filtered, selected_features=set(required_features)))
                        except Exception as e: print(f"Fractal skipped: {e}")

                    call_idx += 1

                # --- Config calls (LFCC + chroma computed together) ---
                for config in group["configs"]:
                    if feat_cancel_flag.is_set():
                        _feat_progress(call_idx, n_calls, "Cancelled.")
                        return

                    _feat_progress(call_idx, n_calls,
                                   f"Config '{config}': {filter_name}  ({call_idx+1}/{n_calls})")

                    if "Tresh" in config:
                        mfcc_cfg = "default"
                        lfcc_cfg = "default"
                        peak_cfg = config
                    else:
                        mfcc_cfg = config
                        lfcc_cfg = config
                        peak_cfg = "default"

                    feats = extractor.extract_basic_features(
                        filtered,
                        mfcc_chroma_config=mfcc_cfg,     # chroma with this config
                        filter_upper_cutoff=filter_upper_cutoff,
                        peak_detection_config=peak_cfg,
                        lfcc_config=lfcc_cfg,            # LFCC with same config
                        selected_features=base_features_set
                    )
                    for k, v in feats.items():
                        all_extracted[f"{k}_{filter_name}_{config}"] = v
                    call_idx += 1

            if feat_cancel_flag.is_set():
                _feat_progress(call_idx, n_calls, "Cancelled.")
                return

            # --- Build feature row for the model ---
            _feat_progress(n_calls, n_calls, "Building feature vectorÃ¢â‚¬Â¦")
            missing_features = [f for f in required_features if f not in all_extracted]
            n_found = len(required_features) - len(missing_features)
            n_missing = len(missing_features)
            print(f"[Feature Extraction] {n_found}/{len(required_features)} features found, "
                  f"{n_missing} padded with 0")
            if missing_features:
                print("[Missing features]:")
                for mf in missing_features:
                    print(f"  - {mf}")

            row = {feat: float(all_extracted.get(feat, 0.0)) for feat in required_features}
            df_feat = pd.DataFrame([row])

            # --- Load model & predict ---
            _feat_progress(n_calls, n_calls, "Loading modelÃ¢â‚¬Â¦")
            import sys as _sys
            _sys.modules[__name__].WeightedEnsemble = WeightedEnsemble
            _sys.modules[__name__].WeightedEnsemble_20260505 = WeightedEnsemble
            model = joblib.load(m_path)

            # Strip known prefixes/suffixes from model feature names (both model variants)
            _PREFIXES = ("PF_PN_", "PP_PN_")
            _SUFFIXES = ("_AI0_PN", "_AI2_PN")

            def _clean(name):
                for p in _PREFIXES:
                    if name.startswith(p):
                        name = name[len(p):]
                        break
                for s in _SUFFIXES:
                    if name.endswith(s):
                        name = name[:-len(s)]
                        break
                return name

            if hasattr(model, "feature_lists"):
                model.feature_lists = [[_clean(f) for f in fl] for fl in model.feature_lists]

            # Also clean feature_names_in_ on every sub-model sklearn validates against
            def _clean_estimator(est):
                if hasattr(est, "feature_names_in_"):
                    try:
                        est.feature_names_in_ = np.array([_clean(f) for f in est.feature_names_in_])
                    except AttributeError:
                        pass  # read-only property (e.g. Pipeline) â€” recurse into sub-estimators instead
                # recurse into pipelines / meta-estimators
                for attr in ("estimators_", "estimator_", "base_estimator_", "steps"):
                    sub = getattr(est, attr, None)
                    if sub is None:
                        continue
                    if isinstance(sub, list):
                        for item in sub:
                            _clean_estimator(item[1] if isinstance(item, tuple) else item)
                    else:
                        _clean_estimator(sub)

            if hasattr(model, "models"):
                for mdl in model.models:
                    _clean_estimator(mdl)

            _feat_progress(n_calls, n_calls, "Running predictionÃ¢â‚¬Â¦")
            pred   = model.predict(df_feat)
            probas = model.predict_proba(df_feat)

            out_pred = pred[0]
            out_classes = model.classes_

            # Check for Label Encoder
            le_path = os.path.join(m_dir, "elastic_net_model_label_encoder.pkl")
            if os.path.exists(le_path):
                le = joblib.load(le_path)
                out_pred = le.inverse_transform([out_pred])[0]
                out_classes = le.inverse_transform(out_classes)

            _feat_done(out_pred, probas[0], out_classes, missing_features)

        except Exception as exc:
            _feat_error(str(exc), _tb.format_exc())
        finally:
            root.after(0, lambda: feat_extract_btn.config(state=tk.NORMAL))
            root.after(0, lambda: feat_cancel_btn.config(state=tk.DISABLED))


    # ============================================================================
    # TAB 3 â€” Deep Predict  (PaSST 10s + 3s ensemble)
    # ============================================================================
    tab_deep = ttk.Frame(notebook)
    notebook.add(tab_deep, text="  Deep Predict  ")

    # -- Left control panel ---------------------------------------------------
    deep_left = tk.Frame(tab_deep, width=310)
    deep_left.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0), pady=8)
    deep_left.pack_propagate(False)

    # Signal info
    deep_info_frame = tk.LabelFrame(deep_left, text="Current Signal", padx=8, pady=6)
    deep_info_frame.pack(fill=tk.X, pady=(0, 6))

    deep_file_var = tk.StringVar(value="No file loaded")
    tk.Label(deep_info_frame, textvariable=deep_file_var,
             fg="blue", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w")

    deep_trim_var = tk.StringVar(value="No trim window set")
    tk.Label(deep_info_frame, textvariable=deep_trim_var,
             fg="darkgreen", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w")

    def _update_deep_info():
        if sp_loaded_df is not None and sp_loaded_filename is not None:
            sr = sampling_rate_var.get()
            n = len(sp_loaded_df)
            deep_file_var.set(f"File: {sp_loaded_filename}\n{n:,} samples @ {sr} Hz")
        else:
            deep_file_var.set("No file loaded")
        if trim_result is not None:
            s, e, dur = trim_result
            deep_trim_var.set(f"Trim: sample {s} â†’ {e}  ({dur:.4f} s)")
        else:
            deep_trim_var.set("No trim window â€” run Preview Trim Window first")

    # Models frame
    deep_model_frame = tk.LabelFrame(deep_left, text="PaSST Models", padx=8, pady=6)
    deep_model_frame.pack(fill=tk.X, pady=(0, 6))

    # Python interpreter
    tk.Label(deep_model_frame, text="Python interpreter:", anchor="w").pack(anchor="w")
    _py_row = tk.Frame(deep_model_frame); _py_row.pack(fill=tk.X, pady=(0, 4))
    deep_python_var = tk.StringVar(value=sys.executable)
    tk.Entry(_py_row, textvariable=deep_python_var, width=22).pack(side=tk.LEFT, fill=tk.X, expand=True)
    def _browse_python():
        p = filedialog.askopenfilename(title="Select Python executable",
                                       filetypes=[("Python", "python*.exe"), ("All", "*.*")])
        if p: deep_python_var.set(p)
    tk.Button(_py_row, text="â€¦", width=3, command=_browse_python).pack(side=tk.LEFT, padx=(2, 0))

    # Model directory
    tk.Label(deep_model_frame, text="Model directory (.pt files):", anchor="w").pack(anchor="w")
    _mdir_row = tk.Frame(deep_model_frame); _mdir_row.pack(fill=tk.X, pady=(0, 4))
    deep_model_dir_var = tk.StringVar(value=MODELS_DIR if os.path.isdir(MODELS_DIR) else SCRIPT_DIR)
    tk.Entry(_mdir_row, textvariable=deep_model_dir_var, width=22).pack(side=tk.LEFT, fill=tk.X, expand=True)
    def _browse_model_dir():
        d = filedialog.askdirectory(title="Select folder containing .pt checkpoints")
        if d: deep_model_dir_var.set(d)
    tk.Button(_mdir_row, text="â€¦", width=3, command=_browse_model_dir).pack(side=tk.LEFT, padx=(2, 0))

    # Device
    _dev_row = tk.Frame(deep_model_frame)
    _dev_row.pack(fill=tk.X, pady=(0, 4))
    tk.Label(_dev_row, text="Device:", width=10, anchor="w").pack(side=tk.LEFT)
    deep_device_var = tk.StringVar(value="cpu")
    ttk.Combobox(_dev_row, textvariable=deep_device_var,
                 values=["cpu", "cuda"], state="readonly", width=8).pack(side=tk.LEFT)

    deep_model_status_var = tk.StringVar(value="Click 'Validate Setup' to test the interpreter")
    tk.Label(deep_model_frame, textvariable=deep_model_status_var,
             fg="gray", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w", pady=(4, 0))

    deep_load_btn = tk.Button(deep_model_frame, text="Validate Setup",
                              command=lambda: _deep_validate_setup(), bg="lightyellow")
    deep_load_btn.pack(fill=tk.X, pady=(4, 0))

    # -- Model selection -------------------------------------------------------
    deep_mdl_frame = tk.LabelFrame(deep_left, text="Model", padx=6, pady=4)
    deep_mdl_frame.pack(fill=tk.X, pady=(0, 6))

    _DUR_MAP = {"10s": 320_000, "5s": 160_000, "3s": 96_000, "2s": 64_000}

    def _infer_dur(path):
        name = os.path.basename(path).lower()
        for k in ("10s", "5s", "3s", "2s"):
            if k in name:
                return k
        return "10s"

    deep_mdl_path_var = tk.StringVar(value="")
    deep_mdl_dur_var  = tk.StringVar(value="10s")

    # Path row
    _mdl_r1 = tk.Frame(deep_mdl_frame); _mdl_r1.pack(fill=tk.X)
    tk.Entry(_mdl_r1, textvariable=deep_mdl_path_var, width=22).pack(
        side=tk.LEFT, fill=tk.X, expand=True)

    def _deep_browse_model():
        p = filedialog.askopenfilename(
            title="Select checkpoint",
            filetypes=[("PyTorch", "*.pt *.pth"), ("All", "*.*")])
        if p:
            deep_mdl_path_var.set(p)
            deep_mdl_dur_var.set(_infer_dur(p))

    tk.Button(_mdl_r1, text="â€¦", width=3,
              command=_deep_browse_model).pack(side=tk.LEFT, padx=(2, 0))

    # Duration row
    _mdl_r2 = tk.Frame(deep_mdl_frame); _mdl_r2.pack(fill=tk.X, pady=(4, 0))
    tk.Label(_mdl_r2, text="Duration:").pack(side=tk.LEFT)
    ttk.Combobox(_mdl_r2, textvariable=deep_mdl_dur_var, values=list(_DUR_MAP.keys()),
                 state="readonly", width=5).pack(side=tk.LEFT, padx=(4, 0))
    tk.Label(_mdl_r2, text="(last N seconds fed to model)",
             fg="gray", font=tkfont.Font(font=tkfont.nametofont("TkDefaultFont"), size=8)
             ).pack(side=tk.LEFT, padx=(6, 0))

    # Model picker combobox (populated by Scan Dir)
    _mdl_combo_paths = {}   # basename â†’ full path
    deep_mdl_pick_var = tk.StringVar(value="")
    _mdl_combo = ttk.Combobox(deep_mdl_frame, textvariable=deep_mdl_pick_var, state="readonly")
    _mdl_combo.pack(fill=tk.X, pady=(4, 0))

    def _mdl_combo_selected(e=None):
        name = deep_mdl_pick_var.get()
        if name in _mdl_combo_paths:
            p = _mdl_combo_paths[name]
            deep_mdl_path_var.set(p)
            deep_mdl_dur_var.set(_infer_dur(p))

    _mdl_combo.bind("<<ComboboxSelected>>", _mdl_combo_selected)

    def _deep_scan_models():
        mdir = deep_model_dir_var.get().strip()
        if not os.path.isdir(mdir):
            messagebox.showwarning("No Directory", "Set the model directory first.")
            return
        pts = sorted(f for f in os.listdir(mdir) if f.lower().endswith((".pt", ".pth")))
        if not pts:
            messagebox.showinfo("None Found", f"No .pt files in:\n{mdir}")
            return
        _mdl_combo_paths.clear()
        for f in pts:
            _mdl_combo_paths[f] = os.path.join(mdir, f)
        _mdl_combo["values"] = pts
        _mdl_combo.set(pts[0])
        _mdl_combo_selected()

    tk.Button(deep_mdl_frame, text="âŸ³ Scan Dir",
              command=_deep_scan_models).pack(fill=tk.X, pady=(4, 0))

    # Pre-populate from MODELS_DIR on startup
    _preload_dir = MODELS_DIR if os.path.isdir(MODELS_DIR) else SCRIPT_DIR
    if os.path.isdir(_preload_dir):
        _pt_files = sorted(f for f in os.listdir(_preload_dir)
                           if f.lower().endswith((".pt", ".pth")))
        if _pt_files:
            for _pt in _pt_files:
                _mdl_combo_paths[_pt] = os.path.join(_preload_dir, _pt)
            _mdl_combo["values"] = _pt_files
            _mdl_combo.set(_pt_files[0])
            deep_mdl_path_var.set(_mdl_combo_paths[_pt_files[0]])
            deep_mdl_dur_var.set(_infer_dur(_mdl_combo_paths[_pt_files[0]]))

    # Run / Cancel
    deep_predict_btn = tk.Button(
        deep_left, text="Run Deep Prediction",
        command=lambda: _deep_start_prediction(),
        bg="lightgreen", relief=tk.RAISED,
        font=tkfont.Font(font=tkfont.nametofont("TkDefaultFont"), weight="bold"),
    )
    deep_predict_btn.pack(fill=tk.X, pady=(8, 2))

    deep_cancel_btn = tk.Button(
        deep_left, text="Cancel",
        command=lambda: _deep_cancel(),
        bg="salmon", state=tk.DISABLED,
    )
    deep_cancel_btn.pack(fill=tk.X, pady=(0, 8))

    # Progress + status
    deep_progress_var = tk.DoubleVar(value=0.0)
    ttk.Progressbar(deep_left, variable=deep_progress_var, maximum=100, length=280).pack(fill=tk.X, pady=(0, 4))

    deep_status_var = tk.StringVar(value="Select a model, then run prediction")
    tk.Label(deep_left, textvariable=deep_status_var,
             fg="gray", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w")

    # -- Right results panel --------------------------------------------------
    deep_right = tk.Frame(tab_deep)
    deep_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

    # Prediction result
    deep_pred_frame = tk.LabelFrame(deep_right, text="Prediction", padx=8, pady=8)
    deep_pred_frame.pack(fill=tk.X, pady=(0, 8))

    deep_ens_pred_var = tk.StringVar(value="â€”")
    tk.Label(deep_pred_frame, textvariable=deep_ens_pred_var, fg="steelblue",
             font=tkfont.Font(font=tkfont.nametofont("TkDefaultFont"), size=20, weight="bold")
             ).pack(side=tk.LEFT)

    # Chart frame â€” canvas is created/replaced dynamically
    deep_chart_frame = tk.Frame(deep_right)
    deep_chart_frame.pack(fill=tk.BOTH, expand=True)
    deep_canvas = None   # created in _draw_deep_plot

    # -- Deep Predict logic ---------------------------------------------------


    def _draw_deep_plot(result):
        global deep_canvas
        # result: {"pred": str, "proba": {class: float}}
        if deep_canvas is not None:
            deep_canvas.get_tk_widget().destroy()
            plt.close("all")

        classes = list(result["proba"].keys())
        proba   = [result["proba"][c] for c in classes]
        winner  = result["pred"]

        fig, ax = plt.subplots(figsize=(6, 3.5))
        colors = ["forestgreen" if c == winner else "steelblue" for c in classes]
        bars = ax.bar(range(len(classes)), proba, color=colors,
                      edgecolor="black", linewidth=0.5, tick_label=classes)
        ax.set_ylim(0, 1.15)
        ax.set_title(f"Prediction:  {winner}", fontsize=12, weight="bold")
        ax.set_ylabel("Probability", fontsize=9)
        ax.tick_params(axis="x", labelrotation=20, labelsize=9)
        for bar, p in zip(bars, proba):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 0.02,
                    f"{p:.2f}", ha="center", va="bottom", fontsize=9)

        fig.tight_layout(pad=2.0)
        deep_canvas = FigureCanvasTkAgg(fig, master=deep_chart_frame)
        deep_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        deep_canvas.draw()


    def _deep_validate_setup():
        deep_load_btn.config(state=tk.DISABLED)
        deep_model_status_var.set("Validatingâ€¦")
        deep_status_var.set("Testing Python interpreterâ€¦")

        def _worker():
            import subprocess as _sp
            py = deep_python_var.get().strip()
            if not py or not os.path.exists(py):
                root.after(0, lambda: _deep_validate_fail("Python executable not found: " + py))
                return
            try:
                r = _sp.run([py, "-c", "import torch; import deepaudiox; print('ok')"],
                            capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    err = (r.stderr.strip().split("\n") or ["unknown error"])[-1]
                    root.after(0, lambda e=err: _deep_validate_fail(e))
                    return
            except Exception as exc:
                root.after(0, lambda e=str(exc): _deep_validate_fail(e))
                return
            active_path = deep_mdl_path_var.get().strip()
            all_ok = bool(active_path) and os.path.exists(active_path)
            ck = (f"{'OK' if all_ok else 'MISSING'}: "
                  f"{os.path.basename(active_path) if active_path else '(no model selected)'}")
            lines = ["Python OK  (torch + deepaudiox found)", ck]
            root.after(0, lambda: _deep_validate_ok("\n".join(lines), all_ok))

        threading.Thread(target=_worker, daemon=True).start()


    def _deep_validate_ok(msg, all_ok):
        deep_model_status_var.set(msg)
        deep_load_btn.config(state=tk.NORMAL)
        deep_status_var.set("Setup OK â€” ready to run prediction" if all_ok
                            else "Fix missing checkpoints then retry")


    def _deep_validate_fail(msg):
        deep_model_status_var.set(f"Validation failed: {msg}")
        deep_load_btn.config(state=tk.NORMAL)
        deep_status_var.set("Fix the issue above, then retry")
        messagebox.showerror("Setup Validation Failed",
                             f"{msg}\n\nMake sure the selected Python has torch and "
                             "deepaudiox installed.")


    def _deep_start_prediction():
        global _deep_thread
        if _deep_thread is not None and _deep_thread.is_alive():
            return
        if sp_loaded_df is None:
            messagebox.showwarning("No File", "Please load a parquet file first.")
            return
        if trim_result is None:
            messagebox.showwarning("No Trim",
                                   "Please set a trim window first.\n"
                                   "Go to 'Set Window' and click 'Preview Trim Window'.")
            return
        py = deep_python_var.get().strip()
        if not py or not os.path.exists(py):
            messagebox.showwarning("Python Not Set",
                                   "Please set the Python interpreter and click 'Validate Setup'.")
            return
        mdl_path = deep_mdl_path_var.get().strip()
        if not mdl_path:
            messagebox.showwarning("No Model", "Please select a model checkpoint.")
            return
        if not os.path.exists(mdl_path):
            messagebox.showwarning("Model Not Found", f"File not found:\n{mdl_path}")
            return

        _deep_cancel_flag.clear()
        deep_predict_btn.config(state=tk.DISABLED)
        deep_cancel_btn.config(state=tk.NORMAL)
        deep_progress_var.set(10)
        deep_status_var.set("Running inferenceâ€¦")
        deep_ens_pred_var.set("â€”")

        s_idx, e_idx, _ = trim_result
        signal = sp_loaded_df["AI0 (V)"].values[s_idx:e_idx].astype(np.float32)

        _deep_thread = threading.Thread(
            target=_deep_run_thread, args=(signal,), daemon=True
        )
        _deep_thread.start()


    def _deep_cancel():
        _deep_cancel_flag.set()
        deep_status_var.set("Cancellingâ€¦")


    def _deep_run_thread(signal):
        import traceback as _tb, subprocess as _sp, tempfile, json as _json
        npy_path = None
        try:
            py        = deep_python_var.get().strip()
            dev       = deep_device_var.get()
            mdl_path  = deep_mdl_path_var.get().strip()
            n_samples = _DUR_MAP.get(deep_mdl_dur_var.get(), 320_000)

            fd, npy_path = tempfile.mkstemp(suffix=".npy")
            os.close(fd)
            np.save(npy_path, signal)

            def _esc(p): return p.replace("\\", "\\\\")
            sd  = _esc(SCRIPT_DIR)
            npy = _esc(npy_path)
            mp  = _esc(mdl_path)

            script = (
                "import sys, json, numpy as np\n"
                "import torch, torch.nn.functional as F, scipy.signal\n"
                f"sys.path.insert(0, '{sd}')\n"
                "from deepaudiox import AudioClassifier\n"
                "SRC_SR=65536; TGT_SR=32000\n"
                "CLASS_NAMES=['type_1','type_2','type_3','type_4','type_5']\n"
                "def _prep(sig, n):\n"
                "    rs=scipy.signal.resample(sig,int(len(sig)*TGT_SR/SRC_SR)).astype('float32')\n"
                "    rs=rs[-n:] if len(rs)>=n else np.concatenate([np.zeros(n-len(rs),'float32'),rs])\n"
                "    pk=np.max(np.abs(rs));rs=rs/pk if pk>0 else rs\n"
                "    return torch.tensor(rs).unsqueeze(0)\n"
                f"signal=np.load('{npy}')\n"
                f"n={n_samples}\n"
                f"mdl=AudioClassifier.from_checkpoint('{mp}').to('{dev}'); mdl.eval()\n"
                "wav=_prep(signal,n)\n"
                "with torch.no_grad():\n"
                f"    p=F.softmax(mdl(wav.to('{dev}')),dim=-1).squeeze(0).cpu().numpy()\n"
                "pred=CLASS_NAMES[int(np.argmax(p))]\n"
                "print(json.dumps({'pred':pred,'proba':dict(zip(CLASS_NAMES,p.tolist()))}))\n"
            )

            if _deep_cancel_flag.is_set():
                root.after(0, _deep_reset_btns); return

            root.after(0, lambda: deep_status_var.set("Loading model & running inferenceâ€¦"))
            root.after(0, lambda: deep_progress_var.set(30))

            proc = _sp.run([py, "-c", script], capture_output=True, text=True, timeout=300)

            if _deep_cancel_flag.is_set():
                root.after(0, _deep_reset_btns); return

            if proc.returncode != 0:
                err = (proc.stderr.strip().split("\n") or ["subprocess failed"])[-1]
                raise RuntimeError(err)

            out_lines = [l for l in proc.stdout.strip().split("\n") if l.strip()]
            result = _json.loads(out_lines[-1])
            root.after(0, lambda r=result: _deep_done(r))

        except Exception as exc:
            msg, tb = str(exc), _tb.format_exc()
            root.after(0, lambda: _deep_error(msg, tb))
        finally:
            if npy_path and os.path.exists(npy_path):
                try: os.unlink(npy_path)
                except Exception: pass


    def _deep_done(result):
        deep_predict_btn.config(state=tk.NORMAL)
        deep_cancel_btn.config(state=tk.DISABLED)
        deep_progress_var.set(100)
        deep_ens_pred_var.set(result["pred"])
        deep_status_var.set(f"Done!  Prediction: {result['pred']}")
        _draw_deep_plot(result)


    def _deep_error(msg, tb):
        deep_predict_btn.config(state=tk.NORMAL)
        deep_cancel_btn.config(state=tk.DISABLED)
        deep_progress_var.set(0)
        deep_status_var.set(f"Error: {msg}")
        messagebox.showerror("Prediction Error", f"{msg}\n\n{tb[:800]}")


    def _deep_reset_btns():
        deep_predict_btn.config(state=tk.NORMAL)
        deep_cancel_btn.config(state=tk.DISABLED)
        deep_progress_var.set(0)
        deep_status_var.set("Cancelled.")


    # ============================================================================
    # Clean shutdown
    # ============================================================================
    def _sp_cleanup():
        """Stop background threads and close figures; call on application close."""
        global feat_thread, _deep_thread
        if feat_thread is not None and feat_thread.is_alive():
            feat_cancel_flag.set()
            feat_thread.join(timeout=2.0)
        if _deep_thread is not None and _deep_thread.is_alive():
            _deep_cancel_flag.set()
            _deep_thread.join(timeout=2.0)
        import matplotlib.pyplot as _plt
        _plt.close('all')

    _draw_plot()
    return _sp_cleanup
# Create the GUI
root = tk.Tk()
root.title("KRAK Suite – LAN-XI Recorder + MCU Controller")
scale_fonts(15)  # set readable default; user can adjust via the Font spinbox

# ── MCU Serial Connection bar (always visible, above tabs) ─────────────────
mcu_protocol = MCUProtocol()
mcu_ctrl: MCUController | None = None  # set after notebook is built

mcu_bar = ttk.LabelFrame(root, text="MCU Serial Connection (STM32 NUCLEO)", padding=5)
mcu_bar.pack(fill=tk.X, padx=10, pady=(8, 2))

tk.Label(mcu_bar, text="COM Port:").pack(side=tk.LEFT, padx=(0, 4))
mcu_port_var = tk.StringVar()
mcu_port_combo = ttk.Combobox(mcu_bar, textvariable=mcu_port_var, width=10, state="readonly")
mcu_port_combo.pack(side=tk.LEFT, padx=(0, 4))

def _refresh_mcu_ports():
    ports = MCUProtocol.list_ports()
    mcu_port_combo["values"] = ports
    if ports and not mcu_port_var.get():
        mcu_port_var.set(ports[0])
    mcu_status_lbl.config(text=f"Found {len(ports)} port(s)" if ports else "No ports found",
                          foreground="gray")

ttk.Button(mcu_bar, text="Refresh", command=_refresh_mcu_ports).pack(side=tk.LEFT, padx=(0, 4))

mcu_connect_btn = ttk.Button(mcu_bar, text="Connect", command=lambda: _toggle_mcu())
mcu_connect_btn.pack(side=tk.LEFT, padx=(0, 12))

mcu_status_lbl = tk.Label(mcu_bar, text="Disconnected", foreground="red")
mcu_status_lbl.pack(side=tk.LEFT)

tk.Label(mcu_bar, text="Font:").pack(side=tk.LEFT, padx=(16, 2))
_font_size_var = tk.IntVar(value=15)

def _apply_font_size(*_):
    try:
        size = _font_size_var.get()
        if 8 <= size <= 20:
            scale_fonts(size)
    except tk.TclError:
        pass

_font_spin = ttk.Spinbox(mcu_bar, from_=8, to=20, width=3,
                          textvariable=_font_size_var, command=_apply_font_size)
_font_spin.pack(side=tk.LEFT)
_font_spin.bind("<Return>", _apply_font_size)

def _toggle_mcu():
    global mcu_ctrl
    if mcu_protocol.connected:
        try:
            mcu_protocol.send("STOP")
        except Exception:
            pass
        mcu_protocol.disconnect()
        if mcu_ctrl:
            mcu_ctrl.on_disconnect()
        mcu_connect_btn.config(text="Connect")
        mcu_status_lbl.config(text="Disconnected", foreground="red")
    else:
        port = mcu_port_var.get()
        if not port:
            messagebox.showwarning("No Port", "Please select a COM port.")
            return
        try:
            mcu_protocol.connect(port)
            if mcu_ctrl:
                mcu_ctrl.on_connect()
            mcu_connect_btn.config(text="Disconnect")
            mcu_status_lbl.config(text=f"Connected to {port}", foreground="green")
        except Exception as exc:
            messagebox.showerror("Connection Error", f"Failed to connect:\n{exc}")

_refresh_mcu_ports()

# ── Main notebook ───────────────────────────────────────────────────────────
notebook = ttk.Notebook(root)
notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))

# KRAK Logger tab (existing functionality)
krak_frame = ttk.Frame(notebook)
notebook.add(krak_frame, text="KRAK Logger")

shared_duration_var = tk.StringVar(value="15")

# MCU tabs – created now so they exist before any connect attempt
mcu_ctrl = MCUController(notebook, root, mcu_protocol, start_daq=start_linked_recording, duration_var=shared_duration_var)

# ── KRAK Logger content (inside krak_frame) ─────────────────────────────────

# Metadata Controls
control_frame = tk.Frame(krak_frame)
control_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=10, pady=10)


parameter_frame = tk.Frame(control_frame)
parameter_frame.pack(anchor="e")

# Excel Metadata Section
excel_section = tk.LabelFrame(control_frame, text="Excel Metadata", padx=5, pady=5)
excel_section.pack(anchor="e", fill=tk.X, pady=(10, 5))

# Excel file selection
excel_file_frame = tk.Frame(excel_section)
excel_file_frame.pack(fill=tk.X, pady=2)
select_excel_btn = tk.Button(excel_file_frame, text="Select Excel File", command=select_excel_file)
select_excel_btn.pack(side=tk.LEFT)
excel_file_label = tk.Label(excel_file_frame, text="No Excel file selected", fg="gray")
excel_file_label.pack(side=tk.LEFT, padx=(10, 0))

# Reference number input
ref_frame = tk.Frame(excel_section)
ref_frame.pack(fill=tk.X, pady=2)
tk.Label(ref_frame, text="Reference Number:").pack(side=tk.LEFT)
ref_number_entry = tk.Entry(ref_frame, width=10)
ref_number_entry.pack(side=tk.LEFT, padx=(5, 5))
load_metadata_btn = tk.Button(ref_frame, text="Load Metadata", command=load_metadata_from_excel)
load_metadata_btn.pack(side=tk.LEFT)

# Excel metadata display
excel_metadata_text = tk.Text(excel_section, height=6, width=40)
excel_metadata_text.pack(fill=tk.X, pady=2)

# Edit a single metadata field
edit_field_frame = tk.Frame(excel_section)
edit_field_frame.pack(fill=tk.X, pady=(2, 0))
tk.Label(edit_field_frame, text="Edit field:").pack(side=tk.LEFT)
edit_field_var = tk.StringVar()
edit_field_combo = ttk.Combobox(edit_field_frame, textvariable=edit_field_var,
                                 values=[], width=16, state="readonly")
edit_field_combo.pack(side=tk.LEFT, padx=(4, 4))
edit_field_combo.bind("<<ComboboxSelected>>", _on_edit_field_selected)
edit_value_var = tk.StringVar()
edit_value_entry = tk.Entry(edit_field_frame, textvariable=edit_value_var, width=16)
edit_value_entry.pack(side=tk.LEFT, padx=(0, 4))
tk.Button(edit_field_frame, text="Update",
          command=lambda: _apply_metadata_edit()).pack(side=tk.LEFT)

# Additional metadata section
additional_section = tk.LabelFrame(control_frame, text="Additional Metadata", padx=5, pady=5)
additional_section.pack(anchor="e", fill=tk.X, pady=5)

additional_metadata_frame = tk.Frame(additional_section)
additional_metadata_frame.pack(fill=tk.X)

add_field_btn = tk.Button(additional_section, text="Add Metadata Field", command=add_additional_metadata_field)
add_field_btn.pack(pady=2)

clear_metadata_btn = tk.Button(additional_section, text="Clear All Metadata", command=clear_excel_metadata_display)
clear_metadata_btn.pack(pady=2)


dur_row = tk.Frame(control_frame)
dur_row.pack(anchor="e", fill=tk.X, pady=(4, 0))
tk.Label(dur_row, text="Duration (s):").pack(side=tk.LEFT)
duration_entry = tk.Entry(dur_row, textvariable=shared_duration_var, width=7)
duration_entry.pack(side=tk.LEFT, padx=(4, 8))
manual_stop_var = tk.BooleanVar(value=False)

def _toggle_manual_stop(*_):
    duration_entry.configure(state="disabled" if manual_stop_var.get() else "normal")

tk.Checkbutton(dur_row, text="Manual stop", variable=manual_stop_var,
               command=_toggle_manual_stop).pack(side=tk.LEFT)

# Recording section
recording_section = tk.LabelFrame(control_frame, text="Recording", padx=5, pady=5)
recording_section.pack(anchor="e", fill=tk.X, pady=(10, 5))

# Audio source selection
audio_source_var = tk.StringVar(value="LAN-XI")
src_row = tk.Frame(recording_section)
src_row.pack(fill=tk.X, pady=(0, 3))
tk.Label(src_row, text="Audio source:").pack(side=tk.LEFT)
audio_source_combo = ttk.Combobox(src_row, textvariable=audio_source_var, width=26, state="readonly")
audio_source_combo.pack(side=tk.LEFT, padx=(4, 2))
ttk.Button(src_row, text="Refresh", command=refresh_audio_devices).pack(side=tk.LEFT)

# STWINMA2 simultaneous capture (USB 1-ch, 192 kHz, Ch0 only @ 192 kHz)
stwin_section = tk.LabelFrame(recording_section, text="STWINMA2 (1-ch, 192 kHz)", padx=4, pady=4)
stwin_section.pack(fill=tk.X, pady=(0, 3))

stwinma2_enable_var = tk.BooleanVar(value=True)
tk.Checkbutton(stwin_section, text="Also record STWINMA2 Ch0 (192 kHz, USB)",
               variable=stwinma2_enable_var).pack(anchor="w")

stwin_dev_row = tk.Frame(stwin_section)
stwin_dev_row.pack(fill=tk.X, pady=(2, 0))
tk.Label(stwin_dev_row, text="Device:").pack(side=tk.LEFT)
stwinma2_device_var = tk.StringVar(value="Auto-detect")
stwinma2_device_combo = ttk.Combobox(stwin_dev_row, textvariable=stwinma2_device_var,
                                      values=["Auto-detect"], width=24, state="readonly")
stwinma2_device_combo.pack(side=tk.LEFT, padx=(4, 2))

# Focusrite input sensitivity (V / FS)
sens_row = tk.Frame(recording_section)
sens_row.pack(fill=tk.X, pady=(0, 3))
tk.Label(sens_row, text="Sensitivity (V/FS):").pack(side=tk.LEFT)
focusrite_sensitivity_var = tk.StringVar(value="1.0")
tk.Entry(sens_row, textvariable=focusrite_sensitivity_var, width=7).pack(side=tk.LEFT, padx=(4, 0))

# Load cell logging (optional, independent of LAN-XI AI channels)
lc_row = tk.Frame(recording_section)
lc_row.pack(fill=tk.X, pady=(0, 3))
loadcell_enable_var = tk.BooleanVar(value=False)
tk.Checkbutton(lc_row, text="Log load cell (UART)", variable=loadcell_enable_var).pack(side=tk.LEFT)

# Populate audio device list after StringVars are assigned
refresh_audio_devices()

record_button = tk.Button(recording_section, text="Start Recording", command=start_recording)
record_button.pack(anchor="e")

# High-pass filter toggles
hp_filter_var = tk.BooleanVar(value=False)
tk.Checkbutton(recording_section, text="1 kHz high-pass filter", variable=hp_filter_var).pack(anchor="e")
hp5k_filter_var = tk.BooleanVar(value=False)
tk.Checkbutton(recording_section, text="5 kHz high-pass filter", variable=hp5k_filter_var).pack(anchor="e")

# Playback buttons
play_btn_frame = tk.Frame(recording_section)
play_btn_frame.pack(anchor="e", fill=tk.X)
play_button = tk.Button(play_btn_frame, text="Play AI0", command=play_recorded_audio)
play_button.pack(side=tk.LEFT)
play_ai2_button = tk.Button(play_btn_frame, text="Play AI2", command=play_ai2_audio)
play_ai2_button.pack(side=tk.LEFT, padx=(4, 0))
play_ai3_button = tk.Button(play_btn_frame, text="Play AI3 Mic", command=play_ai3_audio)
play_ai3_button.pack(side=tk.LEFT, padx=(4, 0))
play_ai4_button = tk.Button(play_btn_frame, text="Play AI04", command=play_ai4_audio)
play_ai4_button.pack(side=tk.LEFT, padx=(4, 0))
stop_button = tk.Button(play_btn_frame, text="Stop", command=stop_audio, fg="red")
stop_button.pack(side=tk.LEFT, padx=(4, 0))

action_row = tk.Frame(recording_section)
action_row.pack(anchor="e", pady=(4, 0))
tk.Button(action_row, text="Save to Disk", command=save_to_disk).pack(side=tk.LEFT, padx=(0, 4))
upload_button = tk.Button(action_row, text="Upload to MinIO", command=upload_to_minio)
upload_button.pack(side=tk.LEFT)


# File list display for S3 files
file_section = tk.LabelFrame(control_frame, text="File Management", padx=5, pady=5)
file_section.pack(anchor="e", fill=tk.BOTH, expand=True, pady=(10, 5))

dropdown_label = tk.Label(file_section, text="Select sample from S3:")
dropdown_label.pack(anchor="e", pady=(5, 0))

# Sorting options frame
sort_frame = tk.Frame(file_section)
sort_frame.pack(anchor="e", pady=(5, 0))

tk.Label(sort_frame, text="Sort by:").pack(side=tk.LEFT, padx=(0, 5))
sort_by_var = tk.StringVar(value=sort_by)
sort_by_combo = ttk.Combobox(sort_frame, textvariable=sort_by_var, values=["name", "time"],
                             width=8, state="readonly")
sort_by_combo.pack(side=tk.LEFT, padx=(0, 10))
sort_by_combo.bind('<<ComboboxSelected>>', lambda e: change_sort_criteria())

tk.Label(sort_frame, text="Order:").pack(side=tk.LEFT, padx=(0, 5))
sort_order_var = tk.StringVar(value=sort_order)
sort_order_combo = ttk.Combobox(sort_frame, textvariable=sort_order_var,
                                values=["asc", "desc"], width=6, state="readonly")
sort_order_combo.pack(side=tk.LEFT)
sort_order_combo.bind('<<ComboboxSelected>>', lambda e: change_sort_criteria())

# Create a frame for the file list with scrollbar
file_list_frame = tk.Frame(file_section)
file_list_frame.pack(anchor="e", fill=tk.BOTH, pady=5)

# Add scrollbar for the listbox
scrollbar = tk.Scrollbar(file_list_frame)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

# Create a larger listbox for better filename visibility
dropdown_var = tk.StringVar()
file_listbox = tk.Listbox(file_list_frame, height=8, width=45, yscrollcommand=scrollbar.set, font=_F()["mono"])
file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
scrollbar.config(command=file_listbox.yview)

# Populate the listbox with initial sorting
refresh_file_list()

# Bind selection event
def on_file_select(event):
    selection = file_listbox.curselection()
    if selection:
        selected_file = file_listbox.get(selection[0])
        dropdown_var.set(selected_file)

file_listbox.bind('<<ListboxSelect>>', on_file_select)

load_button = tk.Button(file_section, text="Load Sample", command=load_sample)
load_button.pack(anchor="e")

metadata_text = tk.Text(file_section, height=8, width=40)
metadata_text.pack(anchor="e")

editor_button = tk.Button(file_section, text="Editor", command=launch_editor)
editor_button.pack(anchor="e", pady=(10, 0))


fig = plt.figure(figsize=(10, 4))
canvas = FigureCanvasTkAgg(fig, master=krak_frame)
canvas.get_tk_widget().pack(side=tk.LEFT, expand=True, fill=tk.BOTH)

build_signal_processing_tabs(notebook, root)
_build_mel_tab(notebook, root)
root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()




