"""KRAK Logger tab: recording, metadata, playback, upload and file management."""

import datetime
import os
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy.io.wavfile as wav
import sounddevice as sd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from scipy.signal import butter, sosfilt

from . import audio_devices
from .audio_devices import LANXI_SOURCE, STWIN_AUTO_DETECT, STWINMA2_SOURCE
from .config import (
    BUCKET_NAME,
    FOCUSRITE_SAMPLE_RATE,
    LANXI_CHUNK_DURATION,
    STWINMA2_BLOCK_SIZE,
    STWINMA2_SAMPLE_RATE,
    TEMP_DIR,
    ensure_temp_dir,
)
from .disk_buffer import DiskBuffer
from .loadcell import LoadCellCollector
from .storage import create_minio_client, update_search_index_on_server

# AI04 clipping detection (STwin float32 stream saturates at +-1.0).
CLIP_THRESHOLD = 0.99
CLIP_MIN_RUN = 4   # consecutive saturated samples required to count as clipping

# The STWIN firmware mutes the first ~50 ms after the USB stream opens
# (click suppression in AMicArray audio_application.c).  No samples may be
# kept until this window has certainly passed, otherwise the recording
# starts with digital silence and AI04 onsets appear shifted vs AI0-AI3.
STWIN_WARMUP_S = 0.15


class LoggerTab:
    """The KRAK Logger notebook tab.

    Owns the recording pipeline (LAN-XI / STWINMA2 / generic sounddevice
    sources, optional load-cell logging), the metadata panel, playback
    and MinIO upload.
    """

    def __init__(self, root, parent, mcu_protocol, lanxi, lanxi_sample_rate,
                 lanxi_available, duration_var):
        self.root = root
        self.frame = parent
        self.mcu_protocol = mcu_protocol
        self.lanxi = lanxi
        self.lanxi_sample_rate = lanxi_sample_rate
        self.lanxi_available = lanxi_available

        # Recording state
        self.recording = False
        self.duration = 15.0
        self.output_wav_file = "recorded_audio.wav"
        self.output_parquet_file = "recorded_data.parquet"
        self.recorded_data = None
        self.recorded_time_axis = None
        self.recorded_loadcell = None      # (time_axis_s, raw_values) from MCU, or None
        self.recorded_stwinma2 = None      # float64 ndarray in [-1, 1] from float32 ch0, or None
        self.recorded_stwin_offset = None  # seconds between gate-open and first captured sample
        self.stop_indefinite = threading.Event()   # set to end a manual-stop recording

        # Metadata state
        self.excel_metadata_df = None
        self.excel_file_path = None
        self.loaded_excel_metadata = {}

        self._audio_device_map = {}   # display name -> sd device index

        self._build_ui(duration_var)
        self.refresh_audio_devices()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, duration_var):
        control_frame = tk.Frame(self.frame)
        control_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=10, pady=10)

        # -- Excel Metadata section ------------------------------------
        excel_section = tk.LabelFrame(control_frame, text="Excel Metadata", padx=5, pady=5)
        excel_section.pack(anchor="e", fill=tk.X, pady=(10, 5))

        excel_file_frame = tk.Frame(excel_section)
        excel_file_frame.pack(fill=tk.X, pady=2)
        tk.Button(excel_file_frame, text="Select Excel File",
                  command=self.select_excel_file).pack(side=tk.LEFT)
        self.excel_file_label = tk.Label(excel_file_frame, text="No Excel file selected", fg="gray")
        self.excel_file_label.pack(side=tk.LEFT, padx=(10, 0))

        ref_frame = tk.Frame(excel_section)
        ref_frame.pack(fill=tk.X, pady=2)
        tk.Label(ref_frame, text="Reference Number:").pack(side=tk.LEFT)
        self.ref_number_entry = tk.Entry(ref_frame, width=10)
        self.ref_number_entry.pack(side=tk.LEFT, padx=(5, 5))
        tk.Button(ref_frame, text="Load Metadata",
                  command=self.load_metadata_from_excel).pack(side=tk.LEFT)

        self.excel_metadata_text = tk.Text(excel_section, height=6, width=40)
        self.excel_metadata_text.pack(fill=tk.X, pady=2)

        # Edit a single metadata field
        edit_field_frame = tk.Frame(excel_section)
        edit_field_frame.pack(fill=tk.X, pady=(2, 0))
        tk.Label(edit_field_frame, text="Edit field:").pack(side=tk.LEFT)
        self.edit_field_var = tk.StringVar()
        self.edit_field_combo = ttk.Combobox(edit_field_frame, textvariable=self.edit_field_var,
                                             values=[], width=16, state="readonly")
        self.edit_field_combo.pack(side=tk.LEFT, padx=(4, 4))
        self.edit_field_combo.bind("<<ComboboxSelected>>", self._on_edit_field_selected)
        self.edit_value_var = tk.StringVar()
        tk.Entry(edit_field_frame, textvariable=self.edit_value_var, width=16).pack(
            side=tk.LEFT, padx=(0, 4))
        tk.Button(edit_field_frame, text="Update",
                  command=self._apply_metadata_edit).pack(side=tk.LEFT)

        # -- Additional metadata section ---------------------------------
        additional_section = tk.LabelFrame(control_frame, text="Additional Metadata", padx=5, pady=5)
        additional_section.pack(anchor="e", fill=tk.X, pady=5)

        self.additional_metadata_frame = tk.Frame(additional_section)
        self.additional_metadata_frame.pack(fill=tk.X)

        tk.Button(additional_section, text="Add Metadata Field",
                  command=self.add_additional_metadata_field).pack(pady=2)
        tk.Button(additional_section, text="Clear All Metadata",
                  command=self.clear_excel_metadata_display).pack(pady=2)

        # -- Duration / manual stop ---------------------------------------
        dur_row = tk.Frame(control_frame)
        dur_row.pack(anchor="e", fill=tk.X, pady=(4, 0))
        tk.Label(dur_row, text="Duration (s):").pack(side=tk.LEFT)
        self.duration_entry = tk.Entry(dur_row, textvariable=duration_var, width=7)
        self.duration_entry.pack(side=tk.LEFT, padx=(4, 8))
        self.manual_stop_var = tk.BooleanVar(value=False)
        tk.Checkbutton(dur_row, text="Manual stop", variable=self.manual_stop_var,
                       command=self._toggle_manual_stop).pack(side=tk.LEFT)

        # -- Recording section ---------------------------------------------
        recording_section = tk.LabelFrame(control_frame, text="Recording", padx=5, pady=5)
        recording_section.pack(anchor="e", fill=tk.X, pady=(10, 5))

        # Audio source selection
        self.audio_source_var = tk.StringVar(value=LANXI_SOURCE)
        src_row = tk.Frame(recording_section)
        src_row.pack(fill=tk.X, pady=(0, 3))
        tk.Label(src_row, text="Audio source:").pack(side=tk.LEFT)
        self.audio_source_combo = ttk.Combobox(src_row, textvariable=self.audio_source_var,
                                               width=26, state="readonly")
        self.audio_source_combo.pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(src_row, text="Refresh", command=self.refresh_audio_devices).pack(side=tk.LEFT)

        # STWINMA2 simultaneous capture (USB 1-ch, Ch0 only @ 192 kHz)
        stwin_section = tk.LabelFrame(recording_section, text="STWINMA2 (1-ch, 192 kHz)", padx=4, pady=4)
        stwin_section.pack(fill=tk.X, pady=(0, 3))

        self.stwinma2_enable_var = tk.BooleanVar(value=True)
        tk.Checkbutton(stwin_section, text="Also record STWINMA2 Ch0 (192 kHz, USB)",
                       variable=self.stwinma2_enable_var).pack(anchor="w")

        stwin_dev_row = tk.Frame(stwin_section)
        stwin_dev_row.pack(fill=tk.X, pady=(2, 0))
        tk.Label(stwin_dev_row, text="Device:").pack(side=tk.LEFT)
        self.stwinma2_device_var = tk.StringVar(value=STWIN_AUTO_DETECT)
        self.stwinma2_device_combo = ttk.Combobox(stwin_dev_row, textvariable=self.stwinma2_device_var,
                                                  values=[STWIN_AUTO_DETECT], width=24, state="readonly")
        self.stwinma2_device_combo.pack(side=tk.LEFT, padx=(4, 2))

        # Focusrite input sensitivity (V / FS)
        sens_row = tk.Frame(recording_section)
        sens_row.pack(fill=tk.X, pady=(0, 3))
        tk.Label(sens_row, text="Sensitivity (V/FS):").pack(side=tk.LEFT)
        self.focusrite_sensitivity_var = tk.StringVar(value="1.0")
        tk.Entry(sens_row, textvariable=self.focusrite_sensitivity_var, width=7).pack(
            side=tk.LEFT, padx=(4, 0))

        # Load cell logging (optional, independent of LAN-XI AI channels)
        lc_row = tk.Frame(recording_section)
        lc_row.pack(fill=tk.X, pady=(0, 3))
        self.loadcell_enable_var = tk.BooleanVar(value=False)
        tk.Checkbutton(lc_row, text="Log load cell (UART)",
                       variable=self.loadcell_enable_var).pack(side=tk.LEFT)

        self.record_button = tk.Button(recording_section, text="Start Recording",
                                       command=self.start_recording)
        self.record_button.pack(anchor="e")

        # High-pass filter toggles (applied to playback)
        self.hp_filter_var = tk.BooleanVar(value=False)
        tk.Checkbutton(recording_section, text="1 kHz high-pass filter",
                       variable=self.hp_filter_var).pack(anchor="e")
        self.hp5k_filter_var = tk.BooleanVar(value=False)
        tk.Checkbutton(recording_section, text="5 kHz high-pass filter",
                       variable=self.hp5k_filter_var).pack(anchor="e")

        # Playback buttons
        play_btn_frame = tk.Frame(recording_section)
        play_btn_frame.pack(anchor="e", fill=tk.X)
        tk.Button(play_btn_frame, text="Play AI0", command=self.play_recorded_audio).pack(side=tk.LEFT)
        tk.Button(play_btn_frame, text="Play AI2", command=self.play_ai2_audio).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(play_btn_frame, text="Play AI3 Mic", command=self.play_ai3_audio).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(play_btn_frame, text="Play AI04", command=self.play_ai4_audio).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(play_btn_frame, text="Stop", command=self.stop_audio, fg="red").pack(side=tk.LEFT, padx=(4, 0))

        action_row = tk.Frame(recording_section)
        action_row.pack(anchor="e", pady=(4, 0))
        tk.Button(action_row, text="Save to Disk", command=self.save_to_disk).pack(side=tk.LEFT, padx=(0, 4))
        self.upload_button = tk.Button(action_row, text="Upload to MinIO", command=self.upload_to_minio)
        self.upload_button.pack(side=tk.LEFT)

        # -- Plot ---------------------------------------------------------------
        self.fig = plt.figure(figsize=(10, 4))
        canvas = FigureCanvasTkAgg(self.fig, master=self.frame)
        canvas.get_tk_widget().pack(side=tk.LEFT, expand=True, fill=tk.BOTH)

    # ------------------------------------------------------------------
    # Audio devices
    # ------------------------------------------------------------------

    def refresh_audio_devices(self):
        """Repopulate the audio source combobox with LAN-XI + STWINMA2 + detected input devices."""
        self._audio_device_map, choices, stwin_choices = audio_devices.build_device_choices()

        self.audio_source_combo.configure(values=choices)
        if self.audio_source_var.get() not in choices:
            self.audio_source_var.set(LANXI_SOURCE)
        self.stwinma2_device_combo.configure(values=stwin_choices)
        if self.stwinma2_device_var.get() not in stwin_choices:
            self.stwinma2_device_var.set(STWIN_AUTO_DETECT)

    def _find_stwinma2_device(self):
        return audio_devices.find_stwinma2_device(
            self.stwinma2_device_var.get(), self._audio_device_map)

    # ------------------------------------------------------------------
    # Excel metadata
    # ------------------------------------------------------------------

    def select_excel_file(self):
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
                self.excel_metadata_df = pd.read_excel(file_path)
                self.excel_file_path = file_path
                self.excel_file_label.config(text=f"Excel file: {os.path.basename(file_path)}")
                messagebox.showinfo("Success", f"Excel file loaded successfully.\nColumns: {list(self.excel_metadata_df.columns)}")
                # Clear any previously loaded metadata
                self.clear_excel_metadata_display()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load Excel file: {str(e)}")
                self.excel_metadata_df = None
                self.excel_file_path = None
                self.excel_file_label.config(text="No Excel file selected")

    def load_metadata_from_excel(self):
        if self.excel_metadata_df is None:
            messagebox.showwarning("No Excel File", "Please select an Excel file first.")
            return

        ref_number = self.ref_number_entry.get().strip()
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
            ref_col = self.excel_metadata_df.columns[0]
            matching_rows = self.excel_metadata_df[self.excel_metadata_df[ref_col] == ref_num]

            if matching_rows.empty:
                # Try string comparison if int comparison failed
                matching_rows = self.excel_metadata_df[
                    self.excel_metadata_df[ref_col].astype(str) == str(ref_number)]

            if matching_rows.empty:
                messagebox.showwarning("Not Found", f"Reference number '{ref_number}' not found in Excel file.")
                return

            # Get the first matching row
            row = matching_rows.iloc[0]

            # Load metadata from all columns except the first (reference) column
            self.loaded_excel_metadata.clear()
            for col in self.excel_metadata_df.columns[1:]:
                value = row[col]
                if pd.notna(value):  # Only add non-empty values
                    self.loaded_excel_metadata[col] = str(value)

            # Add automatic timestamp and date
            current_time = datetime.datetime.now()
            self.loaded_excel_metadata['Date'] = current_time.strftime("%Y-%m-%d")
            self.loaded_excel_metadata['Timestamp'] = current_time.strftime("%Y-%m-%d %H:%M:%S")

            # Display loaded metadata for verification
            self.display_excel_metadata()

        except Exception as e:
            messagebox.showerror("Error", f"Failed to load metadata: {str(e)}")

    def display_excel_metadata(self):
        self.excel_metadata_text.delete("1.0", tk.END)
        for key, value in self.loaded_excel_metadata.items():
            self.excel_metadata_text.insert(tk.END, f"{key}: {value}\n")
        self._refresh_edit_field_combo()

    def _refresh_edit_field_combo(self):
        fields = [k for k in self.loaded_excel_metadata if k not in ('Date', 'Timestamp')]
        self.edit_field_combo.configure(values=fields)
        if fields and self.edit_field_var.get() not in fields:
            self.edit_field_var.set(fields[0])
            self.edit_value_var.set(self.loaded_excel_metadata.get(fields[0], ""))

    def _on_edit_field_selected(self, event=None):
        key = self.edit_field_var.get()
        self.edit_value_var.set(self.loaded_excel_metadata.get(key, ""))

    def _apply_metadata_edit(self):
        key = self.edit_field_var.get()
        if not key:
            return
        self.loaded_excel_metadata[key] = self.edit_value_var.get()
        self.display_excel_metadata()
        self.edit_field_combo.focus()

    def advance_to_next_reference(self):
        """Move the reference entry to the next row in the Excel file and reload metadata."""
        if self.excel_metadata_df is None:
            return
        ref_col = self.excel_metadata_df.columns[0]
        current = self.ref_number_entry.get().strip()
        if not current:
            return
        try:
            current_val = int(current)
        except ValueError:
            current_val = current
        # Find the row index of the current reference
        matches = self.excel_metadata_df.index[self.excel_metadata_df[ref_col] == current_val].tolist()
        if not matches:
            matches = self.excel_metadata_df.index[
                self.excel_metadata_df[ref_col].astype(str) == str(current)
            ].tolist()
        if not matches:
            return
        next_idx = matches[0] + 1
        if next_idx >= len(self.excel_metadata_df):
            return   # already at last reference
        next_ref = str(self.excel_metadata_df.iloc[next_idx][ref_col])
        self.ref_number_entry.delete(0, tk.END)
        self.ref_number_entry.insert(0, next_ref)
        self.load_metadata_from_excel()
        messagebox.showinfo("Next Reference", f"Reference advanced to: {next_ref}")

    def clear_excel_metadata_display(self):
        self.loaded_excel_metadata.clear()
        self.excel_metadata_text.delete("1.0", tk.END)
        self.ref_number_entry.delete(0, tk.END)
        # Clear additional metadata entries
        for widget in self.additional_metadata_frame.winfo_children():
            widget.destroy()

    def add_additional_metadata_field(self):
        frame = tk.Frame(self.additional_metadata_frame)
        frame.pack(fill=tk.X, padx=5, pady=2)

        tk.Label(frame, text="Key:").pack(side=tk.LEFT)
        key_entry = tk.Entry(frame, width=10)
        key_entry.pack(side=tk.LEFT, padx=(2, 5))

        tk.Label(frame, text="Value:").pack(side=tk.LEFT)
        value_entry = tk.Entry(frame, width=15)
        value_entry.pack(side=tk.LEFT, padx=(2, 5))

        tk.Button(frame, text="Remove", command=frame.destroy).pack(side=tk.LEFT, padx=(5, 0))

    def get_all_metadata(self):
        """Combine Excel metadata with additional metadata fields"""
        all_metadata = self.loaded_excel_metadata.copy()

        # Add additional metadata from manual entry fields
        for frame in self.additional_metadata_frame.winfo_children():
            entries = [w for w in frame.winfo_children() if isinstance(w, tk.Entry)]
            if len(entries) >= 2:
                key = entries[0].get().strip()
                value = entries[1].get().strip()
                if key and value:
                    all_metadata[key] = value

        return all_metadata

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _generate_filenames(self):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # Check if we have Excel metadata with ID field
        if self.loaded_excel_metadata and 'ID' in self.loaded_excel_metadata:
            id_value = self.loaded_excel_metadata['ID']
            base_name = f"{timestamp}-({id_value})"
        else:
            # Fallback to timestamp if no Excel metadata
            base_name = f"recording_{timestamp}"

        return f"{base_name}.wav", f"{base_name}.parquet"

    def _toggle_manual_stop(self, *_):
        self.duration_entry.configure(state="disabled" if self.manual_stop_var.get() else "normal")

    def start_recording(self):
        if self.recording:
            return
        if self.manual_stop_var.get():
            self.stop_indefinite.clear()
            self.record_button.config(text="⏹  Stop", bg="red", fg="white",
                                      command=self._stop_manual_recording)
        threading.Thread(target=self.record_data, daemon=True).start()

    def _stop_manual_recording(self):
        self.stop_indefinite.set()
        self.record_button.config(text="Stopping…", state="disabled")

    def start_linked_recording(self, on_daq_ready):
        """Start a DAQ-linked recording. on_daq_ready() fires when data starts flowing
        (LAN-XI: streaming socket connect; Focusrite: just before sd.rec())."""
        if self.audio_source_var.get() == LANXI_SOURCE and not self.lanxi_available:
            messagebox.showerror("LAN-XI Not Available",
                                 "No LAN-XI device connected. Check BKDAQ_IP in .env and restart.")
            return
        if not self.recording:
            threading.Thread(target=self.record_data, args=(on_daq_ready,), daemon=True).start()

    def _lc_collector_if_enabled(self, duration_s):
        """Start load-cell logging when enabled and the MCU is connected."""
        if (self.loadcell_enable_var.get()
                and self.mcu_protocol is not None and self.mcu_protocol.connected):
            collector = LoadCellCollector(self.mcu_protocol)
            collector.start(duration_s)
            return collector
        return None

    def record_data(self, on_daq_ready=None):
        source      = self.audio_source_var.get()
        manual_mode = self.manual_stop_var.get()

        if source == LANXI_SOURCE and not self.lanxi_available:
            messagebox.showerror("LAN-XI Not Available",
                                 "No LAN-XI device connected. Check BKDAQ_IP in .env and restart.")
            return

        if not manual_mode:
            try:
                self.duration = float(self.duration_entry.get())
            except ValueError:
                messagebox.showerror("Invalid Input", "Please enter a valid number for duration.")
                return

        wav_name, parquet_name = self._generate_filenames()
        ensure_temp_dir()
        self.output_wav_file     = os.path.join(TEMP_DIR, wav_name)
        self.output_parquet_file = os.path.join(TEMP_DIR, parquet_name)

        self.recording = True
        lc_duration  = 86400.0 if manual_mode else self.duration   # effectively indefinite for manual stop
        lc_collector = None

        # --- STWINMA2 simultaneous capture setup (skipped when STWINMA2 is the primary source) ---
        stwin_enabled    = self.stwinma2_enable_var.get() and source != STWINMA2_SOURCE
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
            _sidx = self._find_stwinma2_device()
            if _sidx is None:
                messagebox.showwarning("STWINMA2 Not Found",
                                       "Could not find a USB audio input (STWINMA2). "
                                       "Recording without STWINMA2.")
                stwin_enabled = False
            else:
                try:
                    _stwin_raw_path = os.path.join(TEMP_DIR, f"_stwin_{int(time.time()*1000)}.raw")
                    stwin_buf    = DiskBuffer(_stwin_raw_path, dtype=np.float32)
                    stwin_stream = sd.InputStream(
                        device=_sidx, samplerate=STWINMA2_SAMPLE_RATE,
                        channels=1, dtype='float32', blocksize=STWINMA2_BLOCK_SIZE,
                        callback=_stwin_callback,
                    )
                    # Start early so hardware is warm before LAN-XI is ready.
                    # The gate keeps the buffer closed until on_ready fires.
                    stwin_stream.start()
                    # LAN-XI can become ready within milliseconds; block here so
                    # the firmware's USB-open mute window (and the click it
                    # suppresses) always falls in the discarded warm-up.
                    time.sleep(STWIN_WARMUP_S)
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
            # -- LAN-XI ------------------------------------------------------------
            if source == LANXI_SOURCE:
                if manual_mode:
                    # Record in fixed-length chunks until stop_indefinite is set
                    all_times   = []
                    all_ch      = [[], [], [], []]
                    t_offset    = 0.0
                    first_chunk = True

                    while not self.stop_indefinite.is_set():
                        def _ready_first(orig=on_daq_ready):
                            nonlocal lc_collector
                            stwin_gate_t[0] = time.perf_counter()
                            stwin_gate.set()   # open buffer gate -- hardware already warm
                            lc_collector = self._lc_collector_if_enabled(lc_duration)
                            if orig is not None:
                                orig()
                        try:
                            t_c, d_c = self.lanxi.SampleChannels(
                                LANXI_CHUNK_DURATION,
                                on_ready=_ready_first if first_chunk else None,
                            )
                        except Exception as _chunk_err:
                            if not self.stop_indefinite.is_set():
                                self.recording = False
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
                        self.recording = False
                        return
                    time_axis = np.concatenate(all_times)
                    data      = [np.concatenate(all_ch[i]) for i in range(4)]
                    self.duration = float(time_axis[-1]) if len(time_axis) else 0.0

                else:
                    try:
                        def _daq_ready_wrapper(orig=on_daq_ready):
                            nonlocal lc_collector
                            stwin_gate_t[0] = time.perf_counter()
                            stwin_gate.set()   # open buffer gate -- hardware already warm
                            lc_collector = self._lc_collector_if_enabled(self.duration)
                            if orig is not None:
                                orig()
                        time_axis, data = self.lanxi.SampleChannels(self.duration,
                                                                    on_ready=_daq_ready_wrapper)
                    except ConnectionRefusedError:
                        self.recording = False
                        messagebox.showerror("Connection Error",
                                             "Failed to connect to LAN-XI device.\n\n"
                                             "The device may be busy from a previous recording.\n"
                                             "Attempting to reset the connection...")
                        try:
                            self.lanxi.reset_stream()
                            time_axis, data = self.lanxi.SampleChannels(self.duration)
                        except Exception as retry_error:
                            messagebox.showerror("Connection Failed",
                                                 f"Could not establish connection after reset.\n\n"
                                                 f"Error: {retry_error}\n\n"
                                                 f"Try restarting the application or power cycle the LAN-XI device.")
                            return
                    except Exception as e:
                        self.recording = False
                        messagebox.showerror("Recording Error", f"An error occurred during recording:\n\n{e}")
                        return

            # -- STWINMA2-only -------------------------------------------------------
            elif source == STWINMA2_SOURCE:
                stwin_idx_only = self._find_stwinma2_device()
                if stwin_idx_only is None:
                    self.recording = False
                    messagebox.showerror("STWINMA2 Not Found",
                                         "Could not find a USB audio input (STWINMA2).\n"
                                         "Check connection and click Refresh.")
                    return

                _so_raw_path = os.path.join(TEMP_DIR, f"_stwin_only_{int(time.time()*1000)}.raw")
                so_buf = DiskBuffer(_so_raw_path, dtype=np.float32)

                # Keep-gate instead of dropping blocks: warm the stream up past
                # the firmware mute window and init transient, then open the
                # gate together with the load cell so AI04 t=0 == LC t=0.
                so_keep = threading.Event()

                def _stwin_only_cb(indata, fc, ti, status):
                    if status:
                        print(f"STWINMA2: {status}")
                    if so_keep.is_set():
                        so_buf.push(indata[:, 0].copy())

                try:
                    with sd.InputStream(device=stwin_idx_only, samplerate=STWINMA2_SAMPLE_RATE,
                                        channels=1, dtype='float32', blocksize=STWINMA2_BLOCK_SIZE,
                                        callback=_stwin_only_cb):
                        time.sleep(STWIN_WARMUP_S)
                        so_keep.set()
                        lc_collector = self._lc_collector_if_enabled(lc_duration)
                        if on_daq_ready is not None:
                            on_daq_ready()
                        if manual_mode:
                            self.stop_indefinite.wait()
                        else:
                            n_expected = int(STWINMA2_SAMPLE_RATE * self.duration)
                            while so_buf.sample_count < n_expected:
                                time.sleep(0.05)

                    so_buf.finish()
                    raw = so_buf.read_float()
                    if not manual_mode and len(raw) > int(STWINMA2_SAMPLE_RATE * self.duration):
                        raw = raw[:int(STWINMA2_SAMPLE_RATE * self.duration)]

                    n_samples  = len(raw)
                    actual_dur = n_samples / STWINMA2_SAMPLE_RATE
                    if manual_mode:
                        self.duration = actual_dur
                    time_axis = np.linspace(0, actual_dur, n_samples, endpoint=False)
                    data = [raw,
                            np.full(n_samples, np.nan),
                            np.full(n_samples, np.nan),
                            np.full(n_samples, np.nan)]
                except Exception as e:
                    so_buf.discard()
                    self.recording = False
                    messagebox.showerror("Recording Error", f"STWINMA2 recording failed:\n\n{e}")
                    return

            # -- Focusrite / generic sounddevice ----------------------------------------
            else:
                device_idx = self._audio_device_map.get(source)
                sensitivity = self._focusrite_sensitivity()
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
                            lc_collector = self._lc_collector_if_enabled(lc_duration)
                            if on_daq_ready is not None:
                                on_daq_ready()
                            self.stop_indefinite.wait()

                        with fo_lock:
                            _all_fo = fo_frames[:]
                        raw_fo    = np.concatenate(_all_fo) if _all_fo else np.array([], dtype=np.float32)
                        ai0       = raw_fo.astype(float) * sensitivity
                        n_samples = len(ai0)
                        self.duration = n_samples / sr
                        time_axis = np.linspace(0, self.duration, n_samples, endpoint=False)
                        data = [ai0,
                                np.full(n_samples, np.nan),
                                np.full(n_samples, np.nan),
                                np.full(n_samples, np.nan)]
                    except Exception as e:
                        self.recording = False
                        messagebox.showerror("Recording Error", f"Recording failed:\n\n{e}")
                        return
                else:
                    n_samples = int(sr * self.duration)
                    try:
                        audio_raw = sd.rec(n_samples, samplerate=sr, channels=1,
                                           device=device_idx, dtype="float32")
                        lc_collector = self._lc_collector_if_enabled(self.duration)
                        if on_daq_ready is not None:
                            on_daq_ready()
                        sd.wait()
                        ai0 = audio_raw[:, 0].astype(float) * sensitivity
                        time_axis = np.linspace(0, self.duration, n_samples, endpoint=False)
                        data = [ai0,
                                np.full(n_samples, np.nan),
                                np.full(n_samples, np.nan),
                                np.full(n_samples, np.nan)]
                    except Exception as e:
                        self.recording = False
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
                    self.recorded_stwinma2 = stwin_buf.read_float()
                    if stwin_gate_t[0] is not None and stwin_first_t[0] is not None:
                        self.recorded_stwin_offset = stwin_first_t[0] - stwin_gate_t[0]
                        print(f"STwin capture lag after gate: {self.recorded_stwin_offset*1000:.1f} ms")
                    else:
                        self.recorded_stwin_offset = None
                else:
                    stwin_buf.discard()
                    self.recorded_stwinma2 = None
                stwin_buf = None
            else:
                self.recorded_stwinma2 = None

            # Always stop LC_LOGGING stream, even on error
            if lc_collector is not None:
                lc_t, lc_v = lc_collector.stop()
                self.recorded_loadcell = (lc_t, lc_v) if lc_t is not None else None
            else:
                self.recorded_loadcell = None

            # Reset record button when a manual-stop recording finishes
            if manual_mode:
                self.root.after(0, lambda: self.record_button.config(
                    text="Start Recording", bg="SystemButtonFace", fg="black",
                    command=self.start_recording, state="normal"))

        if time_axis is None or data is None:
            return

        # Resample load cell onto audio time axis for plotting and storage
        lc_resampled = None
        if self.recorded_loadcell is not None:
            lc_t, lc_v = self.recorded_loadcell
            lc_resampled = np.interp(time_axis, lc_t, lc_v,
                                     left=float("nan"), right=float("nan"))

        self.update_plot(time_axis, data, lc_resampled, stwinma2=self.recorded_stwinma2)

        self.recorded_data = data
        self.recorded_time_axis = time_axis
        self.recording = False

        self._warn_on_ai04_clipping()

        # Write WAV from AI0
        if source == LANXI_SOURCE:
            _sr, max_v = self.lanxi_sample_rate, 10.0
        elif source == STWINMA2_SOURCE:
            _sr, max_v = STWINMA2_SAMPLE_RATE, 1.0
        else:
            _sr = FOCUSRITE_SAMPLE_RATE
            max_v = max(self._focusrite_sensitivity(), 1e-9)
        audio_int16 = np.nan_to_num(data[0] / max_v * 32767, nan=0).astype(np.int16)
        wav.write(self.output_wav_file, _sr, audio_int16)

        # Write STWINMA2 Ch0 WAV at native 192 kHz (int16, normalised)
        stwin_wav_path = self.output_wav_file.replace('.wav', '_stwinma2.wav')
        if self.recorded_stwinma2 is not None:
            stwin_int16 = (np.clip(self.recorded_stwinma2, -1.0, 1.0) * 32767).astype(np.int16)
            wav.write(stwin_wav_path, STWINMA2_SAMPLE_RATE, stwin_int16)
        elif source == STWINMA2_SOURCE:
            # standalone source -- data[0] is already the normalised ch0 signal
            stwin_int16 = (np.clip(data[0], -1.0, 1.0) * 32767).astype(np.int16)
            wav.write(stwin_wav_path, STWINMA2_SAMPLE_RATE, stwin_int16)

        # Save parquet immediately so it is available locally before MinIO upload
        self.save_to_parquet(lc_resampled)

        # Make upload button red to indicate data needs to be uploaded
        self.upload_button.config(bg="red", fg="white")

    def _focusrite_sensitivity(self):
        try:
            return float(self.focusrite_sensitivity_var.get())
        except (ValueError, tk.TclError):
            return 1.0

    def _warn_on_ai04_clipping(self):
        """Warn when the AI04 (STwin) stream contains saturated runs.

        Requires at least CLIP_MIN_RUN consecutive samples at the threshold to
        avoid flagging isolated loud transients as clipping.
        """
        if self.recorded_stwinma2 is None:
            return
        clipped_mask = np.abs(self.recorded_stwinma2) >= CLIP_THRESHOLD
        # Count length of consecutive True runs
        runs = np.diff(np.concatenate(([0], clipped_mask.astype(int), [0])))
        run_starts = np.where(runs == 1)[0]
        run_ends   = np.where(runs == -1)[0]
        clipping_runs = int(np.sum((run_ends - run_starts) >= CLIP_MIN_RUN))
        if clipping_runs > 0:
            total_clipped = int(np.sum(clipped_mask))
            pct = 100.0 * total_clipped / len(self.recorded_stwinma2)
            self.root.after(0, lambda r=clipping_runs, c=total_clipped, p=pct:
                messagebox.showwarning(
                    "AI04 Clipping Detected",
                    f"{r} clipping event(s) detected — {c:,} samples ({p:.2f}%).\n\n"
                    "Reduce the input gain on the STwin to avoid distortion."
                ))

    def save_to_parquet(self, lc_resampled=None):
        if self.recorded_data is None:
            messagebox.showwarning("No Data", "No recorded data to save.")
            return

        source = self.audio_source_var.get()
        if source == LANXI_SOURCE:
            _sr = self.lanxi_sample_rate
        elif source == STWINMA2_SOURCE:
            _sr = STWINMA2_SAMPLE_RATE
        else:
            _sr = FOCUSRITE_SAMPLE_RATE

        metadata = self.get_all_metadata()
        metadata.update({
            "Sample Rate (Hz)": _sr,
            "Audio Source": source,
        })

        df_dict = {
            "Time (s)": self.recorded_time_axis,
            "AI0 (V)": self.recorded_data[0],
            "AI1 (V)": self.recorded_data[1],
            "AI2 (V)": self.recorded_data[2],
            "AI3 (mV)": self.recorded_data[3] * 1000,
        }
        if lc_resampled is not None:
            df_dict["Load Cell (mV)"] = lc_resampled

        # STwin runs at 192 kHz vs LAN-XI at ~51 kHz, so AI04 has ~3.75x more rows.
        # LAN-XI columns are NaN-padded to match -- ~75 % of those rows will be NaN.
        # Parquet snappy compresses NaN runs well, but the file is still larger.
        if self.recorded_stwinma2 is not None:
            n_stwin = len(self.recorded_stwinma2)
            n_lanxi = len(self.recorded_time_axis)
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
            df_dict["AI04 (norm)"]   = self.recorded_stwinma2
            metadata["STWINMA2 Sample Rate (Hz)"] = STWINMA2_SAMPLE_RATE
            if self.recorded_stwin_offset is not None:
                metadata["STWINMA2 Start Offset (s)"] = round(self.recorded_stwin_offset, 6)

        df = pd.DataFrame(df_dict)
        df.attrs.update(metadata)
        df.to_parquet(self.output_parquet_file, index=False)
        print(f"Data saved as {self.output_parquet_file} with metadata")

    # ------------------------------------------------------------------
    # Upload / save
    # ------------------------------------------------------------------

    def upload_to_minio(self):
        # check if file exists
        if not os.path.exists(self.output_parquet_file) or not os.path.exists(self.output_wav_file):
            self.save_to_parquet()

        parquet_path = self.output_parquet_file
        wav_path = self.output_wav_file
        parquet_basename = os.path.basename(parquet_path)
        wav_basename = os.path.basename(wav_path)

        def upload_worker():
            try:
                # Disable upload button during operation
                self.root.after(0, lambda: self.upload_button.config(text="Uploading...", state="disabled"))

                minio_client = create_minio_client()

                # Get file sizes for verification
                parquet_size = os.path.getsize(parquet_path)
                wav_size = os.path.getsize(wav_path)
                stwin_wav_path     = wav_path.replace('.wav', '_stwinma2.wav')
                stwin_wav_basename = os.path.basename(stwin_wav_path)
                has_stwin_wav      = os.path.exists(stwin_wav_path)

                # Direct upload of files
                self.root.after(0, lambda: self.upload_button.config(text="Uploading parquet..."))
                minio_client.fput_object(BUCKET_NAME, parquet_basename, parquet_path)

                self.root.after(0, lambda: self.upload_button.config(text="Uploading WAV..."))
                minio_client.fput_object(BUCKET_NAME, wav_basename, wav_path)

                if has_stwin_wav:
                    self.root.after(0, lambda: self.upload_button.config(text="Uploading STWINMA2 WAV..."))
                    minio_client.fput_object(BUCKET_NAME, stwin_wav_basename, stwin_wav_path)

                # Verify uploaded files
                self.root.after(0, lambda: self.upload_button.config(text="Verifying upload..."))
                final_parquet_obj = minio_client.stat_object(BUCKET_NAME, parquet_basename)
                final_wav_obj = minio_client.stat_object(BUCKET_NAME, wav_basename)

                if final_parquet_obj.size != parquet_size:
                    raise Exception("Parquet file verification failed - size mismatch")

                if final_wav_obj.size != wav_size:
                    raise Exception("WAV file verification failed - size mismatch")

                print("Upload verification successful")

                def success_update():
                    self.upload_button.config(text="Upload to MinIO", state="normal",
                                              bg="SystemButtonFace", fg="black")

                    # Update search index with metadata
                    metadata = self.get_all_metadata()
                    if metadata and os.path.exists(parquet_path):
                        try:
                            df = pd.read_parquet(parquet_path)
                            update_search_index_on_server(parquet_basename, metadata, df)
                        except Exception as e:
                            print(f"Warning: Search index update failed: {e}")

                    extra = f"\nSTWINMA2 WAV: {stwin_wav_basename}" if has_stwin_wav else ""
                    messagebox.showinfo("Upload Complete",
                                        f"Files uploaded to MinIO successfully!\n\n"
                                        f"Parquet: {parquet_basename}\n"
                                        f"WAV: {wav_basename}{extra}")

                    # Advance to the next reference number and load its metadata
                    self.advance_to_next_reference()

                self.root.after(0, success_update)

            except Exception as e:
                error_msg = str(e)
                print(f"Upload error: {error_msg}")

                def error_update():
                    self.upload_button.config(text="Upload to MinIO", state="normal")
                    messagebox.showerror("Upload Failed",
                                         f"Failed to upload files: {error_msg}")

                self.root.after(0, error_update)

        # Run upload in separate thread
        threading.Thread(target=upload_worker, daemon=True).start()

    def save_to_disk(self):
        """Copy the current recording files to a user-chosen folder."""
        if not os.path.exists(self.output_parquet_file) or not os.path.exists(self.output_wav_file):
            self.save_to_parquet()

        dest_dir = filedialog.askdirectory(title="Choose folder to save recording")
        if not dest_dir:
            return

        copied = []
        failed = []
        for src in [self.output_parquet_file, self.output_wav_file,
                    self.output_wav_file.replace('.wav', '_stwinma2.wav')]:
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(dest_dir, os.path.basename(src)))
                    copied.append(os.path.basename(src))
                except Exception as e:
                    failed.append(f"{os.path.basename(src)}: {e}")

        if failed:
            messagebox.showerror("Save to Disk", "Some files could not be copied:\n" + "\n".join(failed))
        else:
            self.upload_button.config(bg="SystemButtonFace", fg="black")
            messagebox.showinfo("Save to Disk",
                                f"Saved {len(copied)} file(s) to:\n{dest_dir}\n\n" +
                                "\n".join(copied))

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _prepare_audio(self, signal_float, sample_rate):
        """Normalise to float32 [-1, 1] and optionally apply high-pass filter(s)."""
        audio = signal_float.astype(np.float32)
        if self.hp_filter_var.get():
            sos = butter(4, 1000, btype="highpass", fs=sample_rate, output="sos")
            audio = sosfilt(sos, audio).astype(np.float32)
        if self.hp5k_filter_var.get():
            sos = butter(4, 5000, btype="highpass", fs=sample_rate, output="sos")
            audio = sosfilt(sos, audio).astype(np.float32)
        peak = np.max(np.abs(audio))
        if peak > 0:
            audio = audio / peak
        return audio

    def play_recorded_audio(self):
        """Play the recorded audio file from AI0"""
        try:
            if os.path.exists(self.output_wav_file):
                sample_rate, raw = wav.read(self.output_wav_file)
                audio = self._prepare_audio(raw.astype(np.float32), sample_rate)
                sd.play(audio, sample_rate)
            else:
                messagebox.showwarning("No Audio", "No recorded audio file found. Please record audio first.")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play audio: {str(e)}")

    @staticmethod
    def stop_audio():
        """Stop any active sounddevice playback."""
        sd.stop()

    def play_ai2_audio(self):
        """Play the recorded audio from AI2 (accelerometer channel)"""
        try:
            if self.recorded_data is not None and len(self.recorded_data) > 2:
                audio = self._prepare_audio(self.recorded_data[2] / 10.0, self.lanxi_sample_rate)
                sd.play(audio, self.lanxi_sample_rate)
            else:
                messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play AI2 audio: {str(e)}")

    def play_ai3_audio(self):
        """Play the recorded audio from AI3 (HBK 4518 CCLD microphone channel)"""
        try:
            if self.recorded_data is not None and len(self.recorded_data) > 3:
                audio = self._prepare_audio(self.recorded_data[3] / 1.0, self.lanxi_sample_rate)
                sd.play(audio, self.lanxi_sample_rate)
            else:
                messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play AI3 audio: {str(e)}")

    def play_ai4_audio(self):
        """Play the recorded STwin AI04 channel at 192 kHz (falls back to 48 kHz if unsupported)."""
        try:
            stwin_wav = self.output_wav_file.replace('.wav', '_stwinma2.wav')
            if self.recorded_stwinma2 is not None:
                audio = self._prepare_audio(self.recorded_stwinma2, STWINMA2_SAMPLE_RATE)
            elif os.path.exists(stwin_wav):
                sr, raw = wav.read(stwin_wav)
                audio = self._prepare_audio(raw.astype(np.float32), sr)
            else:
                messagebox.showwarning("No Audio", "No AI04 data recorded yet.")
                return
            try:
                sd.play(audio, STWINMA2_SAMPLE_RATE)
            except Exception:
                # Device doesn't support 192 kHz -- resample to 48 kHz for playback
                from scipy.signal import resample as _rs
                n_out = int(len(audio) * 48000 / STWINMA2_SAMPLE_RATE)
                sd.play(_rs(audio, n_out).astype(np.float32), 48000)
        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play AI04 audio: {e}")

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def update_plot(self, time_axis, data, loadcell=None, title="Recorded Data", stwinma2=None):
        def _has_data(arr):
            if arr is None:
                return False
            a = np.asarray(arr, dtype=float)
            return a.size > 0 and not np.all(np.isnan(a))

        channels = []
        if _has_data(data[0]):
            channels.append(("AI0 (V)",        np.asarray(data[0], dtype=float),         "b"))
        if _has_data(data[1]):
            channels.append(("AI1 (V)",        np.asarray(data[1], dtype=float),         "r"))
        if _has_data(data[2]):
            channels.append(("AI2 (V)",        np.asarray(data[2], dtype=float),         "g"))
        if _has_data(data[3]):
            channels.append(("AI3 (mV)",       np.asarray(data[3], dtype=float) * 1000,  "m"))
        if _has_data(loadcell):
            channels.append(("Load Cell (mV)", np.asarray(loadcell, dtype=float),        "saddlebrown"))
        if _has_data(stwinma2):
            # Resampled to match time_axis length for display
            from scipy.signal import resample as _resample
            _s = np.asarray(stwinma2, dtype=float)
            if len(_s) != len(time_axis):
                _s = _resample(_s, len(time_axis))
            channels.append(("AI04",           _s,                                       "darkcyan"))

        if not channels:
            return

        n = len(channels)
        self.fig.clear()
        axes = self.fig.subplots(n, 1, sharex=True)
        if n == 1:
            axes = [axes]

        for i, (ax, (label, values, color)) in enumerate(zip(axes, channels)):
            ax.plot(time_axis, values, color=color)
            ax.set_ylabel(label, color=color)
            ax.tick_params(axis="y", labelcolor=color)
            if i == 0:
                ax.set_title(title)
        axes[-1].set_xlabel("Time (s)")

        self.fig.tight_layout(pad=1.5)
        self.fig.canvas.draw()

