from HelpFunctions.lanxi import LanXI
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scipy.io.wavfile as wav
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import threading
import time
import dotenv
import datetime
import sounddevice as sd
import os
import io
import json
import subprocess
from minio import Minio
from minio.error import S3Error
from minio.commonconfig import CopySource
import atexit
import signal

from mcu_protocol import MCUProtocol
from mcu_tabs import MCUController, _ts, scale_fonts, _F, _parse_field as _mcu_parse_field
from signal_processing_gui import build_signal_processing_tabs

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
    """Repopulate the audio source combobox with LAN-XI + detected input devices."""
    global _audio_device_map
    _audio_device_map = {"LAN-XI": None}
    choices = ["LAN-XI"]
    for idx, name in list_audio_input_devices():
        key = f"{name} [{idx}]"
        _audio_device_map[key] = idx
        choices.append(key)
    if audio_source_combo is not None:
        audio_source_combo.configure(values=choices)
        if audio_source_var is not None and audio_source_var.get() not in choices:
            audio_source_var.set("LAN-XI")

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
    global recorded_data, recorded_time_axis, recorded_loadcell

    source = audio_source_var.get() if audio_source_var is not None else "LAN-XI"

    if source == "LAN-XI" and not LANXI_AVAILABLE:
        messagebox.showerror("LAN-XI Not Available",
                             "No LAN-XI device connected. Check BKDAQ_IP in .env and restart.")
        return

    try:
        DURATION = float(duration_entry.get())
    except ValueError:
        messagebox.showerror("Invalid Input", "Please enter a valid number for duration.")
        return

    OUTPUT_WAV_FILE, OUTPUT_PARQUET_FILE = generate_filenames()
    ensure_temp_dir()
    OUTPUT_WAV_FILE = os.path.join(TEMP_DIR, OUTPUT_WAV_FILE)
    OUTPUT_PARQUET_FILE = os.path.join(TEMP_DIR, OUTPUT_PARQUET_FILE)

    recording = True

    lc_collector = None
    lc_enabled = loadcell_enable_var is not None and loadcell_enable_var.get()

    time_axis = None
    data = None

    try:
        if source == "LAN-XI":
            try:
                def _daq_ready_wrapper(orig=on_daq_ready):
                    nonlocal lc_collector
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
        else:
            # Focusrite / sounddevice path -- device enumerated at runtime so any model works
            device_idx = _audio_device_map.get(source)
            try:
                sensitivity = float(focusrite_sensitivity_var.get()) if focusrite_sensitivity_var else 1.0
            except (ValueError, tk.TclError):
                sensitivity = 1.0

            sr = FOCUSRITE_SAMPLE_RATE
            n_samples = int(sr * DURATION)
            try:
                audio_raw = sd.rec(n_samples, samplerate=sr, channels=1,
                                   device=device_idx, dtype="float32")
                # Start LC exactly when audio stream opens
                if lc_enabled and mcu_protocol is not None and mcu_protocol.connected:
                    lc_collector = LoadCellCollector(mcu_protocol)
                    lc_collector.start(DURATION)
                if on_daq_ready is not None:
                    on_daq_ready()
                sd.wait()
                ai0 = audio_raw[:, 0].astype(float) * sensitivity
                time_axis = np.linspace(0, DURATION, n_samples, endpoint=False)
                # AI1-AI3 not available from Focusrite; stored as NaN to preserve parquet schema
                data = [ai0,
                        np.full(n_samples, np.nan),
                        np.full(n_samples, np.nan),
                        np.full(n_samples, np.nan)]
            except Exception as e:
                recording = False
                messagebox.showerror("Recording Error", f"Focusrite recording failed:\n\n{e}")
                return
    finally:
        # Always stop LC_LOGGING stream, even on error
        if lc_collector is not None:
            lc_t, lc_v = lc_collector.stop()
            recorded_loadcell = (lc_t, lc_v) if lc_t is not None else None
        else:
            recorded_loadcell = None

    if time_axis is None or data is None:
        return

    # Resample load cell onto audio time axis for plotting and storage
    lc_resampled = None
    if recorded_loadcell is not None:
        lc_t, lc_v = recorded_loadcell
        lc_resampled = np.interp(time_axis, lc_t, lc_v,
                                  left=float("nan"), right=float("nan"))

    update_plot(time_axis, data, lc_resampled)

    recorded_data = data
    recorded_time_axis = time_axis
    recording = False

    # Write WAV from AI0
    _sr = SAMPLE_RATE if source == "LAN-XI" else FOCUSRITE_SAMPLE_RATE
    try:
        sensitivity = float(focusrite_sensitivity_var.get()) if focusrite_sensitivity_var else 1.0
    except (ValueError, tk.TclError):
        sensitivity = 1.0
    max_v = 10.0 if source == "LAN-XI" else max(sensitivity, 1e-9)
    audio_int16 = np.nan_to_num(data[0] / max_v * 32767, nan=0).astype(np.int16)
    wav.write(OUTPUT_WAV_FILE, _sr, audio_int16)

    # Save parquet immediately so it is available locally before MinIO upload
    save_to_parquet(lc_resampled)

    # Make upload button red to indicate data needs to be uploaded
    upload_button.config(bg="red", fg="white")

def save_to_parquet(lc_resampled=None):
    if recorded_data is not None:
        source = audio_source_var.get() if audio_source_var is not None else "LAN-XI"
        _sr = SAMPLE_RATE if source == "LAN-XI" else FOCUSRITE_SAMPLE_RATE

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

            # Direct upload of files
            root.after(0, lambda: upload_button.config(text="Uploading parquet..."))
            minio_client.fput_object(BUCKET_NAME, parquet_basename, OUTPUT_PARQUET_FILE)

            root.after(0, lambda: upload_button.config(text="Uploading WAV..."))
            minio_client.fput_object(BUCKET_NAME, wav_basename, OUTPUT_WAV_FILE)

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

                messagebox.showinfo("Upload Complete",
                                  f"Files uploaded to MinIO successfully!\n\n"
                                  f"Parquet: {parquet_basename}\n"
                                  f"WAV: {wav_basename}")

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


def play_recorded_audio():
    """Play the recorded audio file from AI0"""
    try:
        if os.path.exists(OUTPUT_WAV_FILE):
            # Read the WAV file and play it
            sample_rate, audio_data = wav.read(OUTPUT_WAV_FILE)
            sd.play(audio_data, sample_rate)
        else:
            messagebox.showwarning("No Audio", "No recorded audio file found. Please record audio first.")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play audio: {str(e)}")

def play_ai2_audio():
    """Play the recorded audio from AI2 (accelerometer channel)"""
    try:
        if recorded_data is not None and len(recorded_data) > 2:
            # Convert AI2 data to audio format
            max_voltage = 10
            audio_data = (recorded_data[2] / max_voltage * 32767).astype(np.int16)
            sd.play(audio_data, SAMPLE_RATE)
        else:
            messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play AI2 audio: {str(e)}")

def play_ai3_audio():
    """Play the recorded audio from AI3 (HBK 4518 CCLD microphone channel)"""
    try:
        if recorded_data is not None and len(recorded_data) > 3:
            # Convert AI3 data to audio format (1 Vpeak range for CCLD mic)
            max_voltage = 1
            audio_data = (recorded_data[3] / max_voltage * 32767).astype(np.int16)
            sd.play(audio_data, SAMPLE_RATE)
        else:
            messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play AI3 audio: {str(e)}")

def start_recording():
    if not recording:
        threading.Thread(target=record_data, daemon=True).start()

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




def update_plot(time_axis, data, loadcell=None, title="Recorded Data"):
    def _has_data(arr):
        if arr is None:
            return False
        a = np.asarray(arr, dtype=float)
        return a.size > 0 and not np.all(np.isnan(a))

    channels = []
    if _has_data(data[0]):
        channels.append(("AI0 (V)",        np.asarray(data[0], dtype=float),           "b"))
    if _has_data(data[1]):
        channels.append(("AI1 (V)",        np.asarray(data[1], dtype=float),           "r"))
    if _has_data(data[2]):
        channels.append(("AI2 (V)",        np.asarray(data[2], dtype=float),           "g"))
    if _has_data(data[3]):
        channels.append(("AI3 (mV)",       np.asarray(data[3], dtype=float) * 1000,   "m"))
    if _has_data(loadcell):
        channels.append(("Load Cell (mV)", np.asarray(loadcell, dtype=float),          "r"))

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

        lc_col = df["Load Cell (mV)"].values if "Load Cell (mV)" in df.columns else None
        update_plot(df["Time (s)"], [df["AI0 (V)"], df["AI1 (V)"], df["AI2 (V)"], df["AI3 (mV)"] / 1000],
                    loadcell=lc_col, title=base_name)
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

# Additional metadata section
additional_section = tk.LabelFrame(control_frame, text="Additional Metadata", padx=5, pady=5)
additional_section.pack(anchor="e", fill=tk.X, pady=5)

additional_metadata_frame = tk.Frame(additional_section)
additional_metadata_frame.pack(fill=tk.X)

add_field_btn = tk.Button(additional_section, text="Add Metadata Field", command=add_additional_metadata_field)
add_field_btn.pack(pady=2)

clear_metadata_btn = tk.Button(additional_section, text="Clear All Metadata", command=clear_excel_metadata_display)
clear_metadata_btn.pack(pady=2)


duration_label = tk.Label(control_frame, text="Duration (s):")
duration_label.pack(anchor="e")
duration_entry = tk.Entry(control_frame, textvariable=shared_duration_var)
duration_entry.pack(anchor="e")

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

play_button = tk.Button(recording_section, text="Play AI0 Audio", command=play_recorded_audio)
play_button.pack(anchor="e")

play_ai2_button = tk.Button(recording_section, text="Play AI2 Audio", command=play_ai2_audio)
play_ai2_button.pack(anchor="e")

play_ai3_button = tk.Button(recording_section, text="Play AI3 Mic Audio", command=play_ai3_audio)
play_ai3_button.pack(anchor="e")

upload_button = tk.Button(recording_section, text="Upload to MinIO", command=upload_to_minio)
upload_button.pack(anchor="e")


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
root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()




