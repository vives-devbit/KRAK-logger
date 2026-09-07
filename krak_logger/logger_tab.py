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
import sounddevice as sd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from scipy.signal import butter, decimate, sosfilt

from . import audio_devices
from .audio_devices import CN0582_SOURCE, STWIN_AUTO_DETECT, STWINMA2_SOURCE, open_stwinma2_stream
from .external_link import check_nexygenplus_ready, trigger_nexygenplus_start
from .config import (
    BUCKET_NAME,
    CN0582_LONG_RECORD_WARN_S,
    CN0582_MAX_CLIP_S,
    FOCUSRITE_SAMPLE_RATE,
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

    Owns the recording pipeline (CN0582 / STWINMA2 / generic sounddevice
    sources, optional load-cell logging), the metadata panel, playback
    and MinIO upload.
    """

    def __init__(self, root, parent, mcu_protocol, daq, daq_sample_rate,
                 daq_available, duration_var):
        self.root = root
        self.frame = parent
        self.mcu_protocol = mcu_protocol
        self.daq = daq
        self.daq_sample_rate = daq_sample_rate
        self.daq_available = daq_available

        # Recording state
        self.recording = False
        self.duration = 15.0
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
        self._long_record_warned = False   # manual-stop size warning shown once

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
        self.audio_source_var = tk.StringVar(value=CN0582_SOURCE)
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

        # Load cell logging (optional, independent of the CN0582 AI channels)
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
        """Repopulate the audio source combobox with CN0582 + STWINMA2 + detected input devices."""
        self._audio_device_map, choices, stwin_choices = audio_devices.build_device_choices()

        self.audio_source_combo.configure(values=choices)
        if self.audio_source_var.get() not in choices:
            self.audio_source_var.set(CN0582_SOURCE)
        self.stwinma2_device_combo.configure(values=stwin_choices)
        if self.stwinma2_device_var.get() not in stwin_choices:
            self.stwinma2_device_var.set(STWIN_AUTO_DETECT)

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

    def _generate_filename(self):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # Check if we have Excel metadata with ID field
        if self.loaded_excel_metadata and 'ID' in self.loaded_excel_metadata:
            id_value = self.loaded_excel_metadata['ID']
            base_name = f"{timestamp}-({id_value})"
        else:
            # Fallback to timestamp if no Excel metadata
            base_name = f"recording_{timestamp}"

        return f"{base_name}.parquet"

    def _toggle_manual_stop(self, *_):
        self.duration_entry.configure(state="disabled" if self.manual_stop_var.get() else "normal")

    def start_recording(self):
        if self.recording:
            return
        # Everything that can say no runs before anything irreversible happens:
        # pressing NexygenPlus's Start button physically starts a test, so it is
        # the last step, never the one that discovers a problem. A KRAK
        # recording and its NexygenPlus test are only useful as a pair, so
        # either both start or neither does.
        source = self.audio_source_var.get()
        need_stwin = (source == STWINMA2_SOURCE
                      or (self.stwinma2_enable_var.get() and source != STWINMA2_SOURCE))
        if need_stwin and not self._stwinma2_available_at_192k():
            return  # error already shown; nothing started, NexygenPlus untouched

        ready, detail = check_nexygenplus_ready()
        if not ready:
            print(f"NexygenPlus: {detail}")
            messagebox.showerror(
                "NexygenPlus Not Ready",
                f"The NexygenPlus test could not be started:\n\n{detail}\n\n"
                "Recording was not started.")
            return

        if self.manual_stop_var.get():
            if (self.audio_source_var.get() == CN0582_SOURCE
                    and not self._long_record_warned):
                if not messagebox.askokcancel(
                        "Long CN0582 Recording",
                        "Manual-stop recording on the CN0582 streams 8.192 MB/s to disk "
                        "and decodes into memory once you stop.\n\n"
                        f"Past about {CN0582_LONG_RECORD_WARN_S:g} s that is "
                        f"{CN0582_LONG_RECORD_WARN_S * 8.192:.0f} MB on disk, and it "
                        "climbs by ~0.5 GB on disk and ~1 GB in RAM per further minute.\n\n"
                        "Continue?"):
                    return
                self._long_record_warned = True

        # Point of no return: the test is now running.
        if not self._trigger_nexygenplus():
            messagebox.showerror(
                "NexygenPlus Did Not Start",
                "The NexygenPlus Start button could not be pressed (see console).\n\n"
                "Recording was not started.")
            return

        if self.manual_stop_var.get():
            self.stop_indefinite.clear()
            self.record_button.config(text="⏹  Stop", bg="red", fg="white",
                                      command=self._stop_manual_recording)
        threading.Thread(target=self.record_data, daemon=True).start()

    def _stwinma2_available_at_192k(self):
        """Verify AI04 can actually be opened at 192 kHz right now, without
        holding the device open -- AI04 is only ever valid at 192 kHz, so this
        gates the whole recording (and the NexygenPlus test) rather than
        letting record_data() discover the failure after the fact.
        """
        try:
            probe = open_stwinma2_stream(
                self.stwinma2_device_var.get(), self._audio_device_map,
                callback=lambda *a: None, block_size=STWINMA2_BLOCK_SIZE,
                required_rate=STWINMA2_SAMPLE_RATE)
        except Exception as e:
            messagebox.showerror(
                "STWINMA2 Not Available",
                f"AI04 (STWINMA2) could not be opened at {STWINMA2_SAMPLE_RATE} Hz:\n\n{e}\n\n"
                "Recording was not started.")
            return False
        try:
            probe.stop()
            probe.close()
        except Exception:
            pass
        return True

    def _trigger_nexygenplus(self):
        """Press NexygenPlus 4.1's Start button so its test runs alongside us.

        Never raises and never blocks: a missing or busy NexygenPlus must not
        stop a KRAK recording that is otherwise good to go.
        """
        try:
            ok, detail = trigger_nexygenplus_start()
            print(f"NexygenPlus: {detail}")
            return ok
        except Exception as e:
            print(f"NexygenPlus: Start trigger failed: {e}")
            return False

    def _stop_manual_recording(self):
        self.stop_indefinite.set()
        self.record_button.config(text="Stopping…", state="disabled")

    def start_linked_recording(self, on_daq_ready):
        """Start a DAQ-linked recording. on_daq_ready() fires when data starts flowing
        (CN0582: the moment the USB stream runs; Focusrite: just before sd.rec())."""
        if self.audio_source_var.get() == CN0582_SOURCE and not self.daq_available:
            messagebox.showerror("CN0582 Not Available",
                                 "No EVAL-CN0582-USBZ found.\n\n"
                                 "Check the USB connection and that the WinUSB driver is "
                                 "installed (driver/install_driver.cmd), then restart.")
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

        if source == CN0582_SOURCE and not self.daq_available:
            messagebox.showerror("CN0582 Not Available",
                                 "No EVAL-CN0582-USBZ found.\n\n"
                                 "Check the USB connection and that the WinUSB driver is "
                                 "installed (driver/install_driver.cmd), then restart.")
            return

        if source == CN0582_SOURCE and self.daq is not None:
            startup_bias_done = getattr(self.daq, "startup_bias_done", None)
            if startup_bias_done is not None and not startup_bias_done.is_set():
                startup_bias_done.wait()
            # Match the standalone CN0582 GUI: push every board setting right
            # before capture so channel power/coupling cannot drift between runs.
            try:
                self.daq.apply_config()
            except Exception as cfg_err:
                messagebox.showerror(
                    "CN0582 Configuration Failed",
                    f"Could not apply CN0582 settings before recording:\n\n{cfg_err}")
                return

        if not manual_mode:
            try:
                self.duration = float(self.duration_entry.get())
            except ValueError:
                messagebox.showerror("Invalid Input", "Please enter a valid number for duration.")
                return
            if source == CN0582_SOURCE and self.duration > CN0582_MAX_CLIP_S:
                messagebox.showerror(
                    "Duration Too Long",
                    f"The CN0582 records fixed clips of at most {CN0582_MAX_CLIP_S:g} s "
                    f"({CN0582_MAX_CLIP_S * 8.192:.0f} MB on disk).\n\n"
                    "For a longer run tick 'Manual stop', which streams gaplessly "
                    "until you stop it.")
                return

        parquet_name = self._generate_filename()
        ensure_temp_dir()
        self.output_parquet_file = os.path.join(TEMP_DIR, parquet_name)

        self.recording = True
        lc_duration  = 86400.0 if manual_mode else self.duration   # effectively indefinite for manual stop
        lc_collector = None
        # --- STWINMA2 simultaneous capture setup (skipped when STWINMA2 is the primary source) ---
        stwin_enabled    = self.stwinma2_enable_var.get() and source != STWINMA2_SOURCE
        stwin_buf        = None
        stwin_stream     = None
        stwin_gate       = threading.Event()   # set() when the DAQ on_ready fires
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
            # start_recording()'s preflight already confirmed AI04 opens at
            # 192 kHz before NexygenPlus was triggered; this is the real open,
            # done fresh rather than reusing the probe stream. AI04 is only
            # ever valid at 192 kHz -- a failure here (e.g. a device dropped
            # out between the preflight and now) aborts the whole recording
            # rather than silently continuing without it.
            try:
                _stwin_raw_path = os.path.join(TEMP_DIR, f"_stwin_{int(time.time()*1000)}.raw")
                stwin_buf    = DiskBuffer(_stwin_raw_path, dtype=np.float32)
                stwin_stream = open_stwinma2_stream(
                    self.stwinma2_device_var.get(), self._audio_device_map,
                    callback=_stwin_callback, block_size=STWINMA2_BLOCK_SIZE,
                    required_rate=STWINMA2_SAMPLE_RATE)
                # The DAQ can become ready within milliseconds; block here so
                # the firmware's USB-open mute window (and the click it
                # suppresses) always falls in the discarded warm-up.
                time.sleep(STWIN_WARMUP_S)
            except Exception as stwin_err:
                if stwin_buf is not None:
                    stwin_buf.discard()
                    stwin_buf = None
                self.recording = False
                messagebox.showerror(
                    "STWINMA2 Not Available",
                    f"AI04 (STWINMA2) could not be opened at {STWINMA2_SAMPLE_RATE} Hz:\n\n"
                    f"{stwin_err}\n\nRecording was not started.")
                return

        time_axis = None
        data      = None

        try:
            # -- CN0582 ------------------------------------------------------------
            if source == CN0582_SOURCE:
                def _daq_ready_wrapper(orig=on_daq_ready):
                    nonlocal lc_collector
                    stwin_gate_t[0] = time.perf_counter()
                    stwin_gate.set()   # open buffer gate -- hardware already warm
                    lc_collector = self._lc_collector_if_enabled(lc_duration)
                    if orig is not None:
                        orig()

                try:
                    if manual_mode:
                        # One "start" and one "suspend" for the whole run, so there is
                        # no seam between chunks the way the LAN-XI path had: decode
                        # verifies frame sync from the first sample to the last, and
                        # raises rather than hand back a recording with a gap in it.
                        time_axis, data = self.daq.SampleUntil(
                            self.stop_indefinite, on_ready=_daq_ready_wrapper)
                        self.duration = float(time_axis[-1]) if len(time_axis) else 0.0
                    else:
                        time_axis, data = self.daq.SampleChannels(
                            self.duration, on_ready=_daq_ready_wrapper)
                except Exception as e:
                    self.recording = False
                    messagebox.showerror(
                        "Recording Error",
                        f"CN0582 recording failed:\n\n{e}\n\n"
                        "The endpoints will be resynchronised; if this persists, "
                        "replug the board.")
                    try:
                        self.daq.reset_stream()
                    except Exception as reset_err:
                        print(f"CN0582 resync after failure did not help: {reset_err}")
                    return

            # -- STWINMA2-only -------------------------------------------------------
            elif source == STWINMA2_SOURCE:
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

                # start_recording()'s preflight already confirmed AI04 opens at
                # 192 kHz; a failure here (device dropped out since) aborts
                # cleanly -- AI04 is never valid at anything but 192 kHz.
                try:
                    stwin_only_stream = open_stwinma2_stream(
                        self.stwinma2_device_var.get(), self._audio_device_map,
                        callback=_stwin_only_cb, block_size=STWINMA2_BLOCK_SIZE,
                        required_rate=STWINMA2_SAMPLE_RATE)
                except Exception as e:
                    so_buf.discard()
                    self.recording = False
                    messagebox.showerror(
                        "STWINMA2 Not Available",
                        f"AI04 (STWINMA2) could not be opened at {STWINMA2_SAMPLE_RATE} Hz:\n\n"
                        f"{e}\n\nRecording was not started.")
                    return

                try:
                    try:
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
                    finally:
                        try:
                            stwin_only_stream.stop()
                            stwin_only_stream.close()
                        except Exception:
                            pass

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
        if source == CN0582_SOURCE:
            self._warn_on_daq_clipping()

        # The parquet carries every channel; playback and the mel tab work from
        # the samples still in memory, so no WAV is written.
        # Save parquet immediately so it is available locally before MinIO upload
        self.save_to_parquet(lc_resampled)

        # Make upload button red to indicate data needs to be uploaded
        self.upload_button.config(bg="red", fg="white")

    def source_sample_rate(self):
        """Sample rate of the primary recorded channels, by source."""
        source = self.audio_source_var.get()
        if source == CN0582_SOURCE:
            return self.daq_sample_rate
        if source == STWINMA2_SOURCE:
            return STWINMA2_SAMPLE_RATE
        return FOCUSRITE_SAMPLE_RATE

    def stwinma2_signal(self):
        """The AI04 samples, or None.

        As a simultaneous capture they sit in recorded_stwinma2; as the primary
        source the normalised ch0 signal is data[0] instead.
        """
        if self.recorded_stwinma2 is not None:
            return self.recorded_stwinma2
        if (self.audio_source_var.get() == STWINMA2_SOURCE
                and self.recorded_data is not None and len(self.recorded_data) > 0):
            return self.recorded_data[0]
        return None

    def _focusrite_sensitivity(self):
        try:
            return float(self.focusrite_sensitivity_var.get())
        except (ValueError, tk.TclError):
            return 1.0

    def _warn_on_daq_clipping(self):
        """Warn when the CN0582 flagged over-range samples.

        The AD7768 sets header bit 3 per sample, so this is the hardware's own
        verdict rather than the threshold-and-run-length heuristic AI04 needs.
        """
        sat = getattr(self.daq, "last_saturated", None) if self.daq is not None else None
        if sat is None or sat.size == 0:
            return
        total = sat.shape[1]
        hits = [(c, int(np.count_nonzero(sat[c]))) for c in range(sat.shape[0])]
        hits = [(c, n) for c, n in hits if n]
        if not hits:
            return
        lines = "\n".join(f"AI{c}: {n:,} samples ({100.0 * n / total:.2f} %)"
                          for c, n in hits)
        self.root.after(0, lambda: messagebox.showwarning(
            "CN0582 Over-range",
            f"The ADC flagged saturated samples:\n\n{lines}\n\n"
            "Reduce the channel gain, or re-run Auto-bias if the front end has drifted."))

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

    @staticmethod
    def _decimation_stages(q):
        """Split an integer decimation factor into stages of at most 10.

        One big factor makes the anti-alias filter so narrow that it rings;
        scipy's own docs advise decimating in stages above about 13.
        """
        stages = []
        for f in (10, 8, 5, 4, 3, 2):
            while q > 1 and q % f == 0:
                stages.append(f)
                q //= f
        return stages if q == 1 else None

    def _decimate_for_storage(self, signal, from_hz, to_hz):
        """Anti-alias filter and decimate a channel down to `to_hz`.

        Plain slicing would fold everything above to_hz/2 straight back into the
        band, which is exactly the noise a slow channel is being decimated to get
        away from, so the filter is not optional. Returns the signal untouched if
        the ratio is not a clean integer or the filter cannot run.
        """
        q = int(round(from_hz / to_hz))
        stages = self._decimation_stages(q) if q > 1 else None
        if not stages:
            return signal
        out = np.asarray(signal, dtype=np.float64)
        try:
            for f in stages:
                out = decimate(out, f, ftype="fir", zero_phase=True)
        except ValueError as err:          # too few samples for the filter
            print(f"Storage decimation to {to_hz} Hz skipped: {err}")
            return signal
        return out

    def save_to_parquet(self, lc_resampled=None):
        if self.recorded_data is None:
            messagebox.showwarning("No Data", "No recorded data to save.")
            return

        source = self.audio_source_var.get()
        if source == CN0582_SOURCE:
            _sr = self.daq_sample_rate
        elif source == STWINMA2_SOURCE:
            _sr = STWINMA2_SAMPLE_RATE
        else:
            _sr = FOCUSRITE_SAMPLE_RATE

        metadata = self.get_all_metadata()
        metadata.update({
            "Sample Rate (Hz)": _sr,
            "Audio Source": source,
        })
        if source == CN0582_SOURCE and self.daq is not None:
            metadata["Inverted Channels"] = [ch for ch, inv
                                             in enumerate(self.daq.invert) if inv]

        # No time column is stored: every column starts at t = 0 and steps at its
        # own rate, both of which the metadata carries, so a time axis is
        # np.arange(len) / rate. The two ramps this replaces were ~40% of the file
        # and compress badly, being pure float64 with no delta encoding.
        df_dict = {
            "AI0 (V)": self.recorded_data[0],
            "AI1 (V)": self.recorded_data[1],
            "AI2 (V)": self.recorded_data[2],
            "AI3 (mV)": self.recorded_data[3] * 1000,
        }
        # The rate each column is actually stored at, keyed by column name. The
        # columns do not share one rate -- AI04 runs at 192 kHz against the DAQ's
        # 256 kHz, and a slow channel can be stored slower still -- so a single
        # file-level rate cannot describe the file.
        rates = {name: _sr for name in df_dict}

        # Store the channels that do not need the full rate at a lower one. A load
        # cell changes far too slowly to be worth 256 kSPS on disk; only what is
        # written shrinks, the capture and the plot stay at the acquisition rate.
        if source == CN0582_SOURCE and self.daq is not None:
            for ch, name in enumerate(("AI0 (V)", "AI1 (V)", "AI2 (V)", "AI3 (mV)")):
                target = self.daq.store_rate_hz[ch]
                if target and target < _sr:
                    df_dict[name] = self._decimate_for_storage(df_dict[name], _sr, target)
                    rates[name] = target

        if lc_resampled is not None:
            df_dict["Load Cell (mV)"] = lc_resampled
            # Stored on the DAQ time axis, but interpolated up from the
            # collector's own much slower rate -- worth saying so, because the
            # column carries far less information than its length suggests.
            rates["Load Cell (mV)"] = _sr
            metadata["Load Cell Native Rate (Hz)"] = LoadCellCollector.SAMPLE_RATE_HZ

        if self.recorded_stwinma2 is not None:
            df_dict["AI04 (norm)"] = self.recorded_stwinma2
            rates["AI04 (norm)"] = STWINMA2_SAMPLE_RATE
            metadata["STWINMA2 Sample Rate (Hz)"] = STWINMA2_SAMPLE_RATE
            if self.recorded_stwin_offset is not None:
                metadata["STWINMA2 Start Offset (s)"] = round(self.recorded_stwin_offset, 6)

        # Columns now differ in length -- AI04 runs at its own rate, a decimated
        # channel is shorter still -- and a DataFrame will not take that. Pad each
        # short column at the tail, so every column's samples start at t = 0 and
        # the NaN run is the part with nothing in it. Snappy compresses those runs
        # well, though a padded column still costs more than a missing one.
        n_rows = max(len(np.asarray(v)) for v in df_dict.values())
        for name, values in df_dict.items():
            values = np.asarray(values, dtype=np.float64)
            if len(values) < n_rows:
                values = np.concatenate([values, np.full(n_rows - len(values), np.nan)])
            df_dict[name] = values

        metadata["Channel Sample Rates (Hz)"] = rates

        df = pd.DataFrame(df_dict)
        df.attrs.update(metadata)
        df.to_parquet(self.output_parquet_file, index=False)
        print(f"Data saved as {self.output_parquet_file} with metadata")

    # ------------------------------------------------------------------
    # Upload / save
    # ------------------------------------------------------------------

    def upload_to_minio(self):
        # check if file exists
        if not os.path.exists(self.output_parquet_file):
            self.save_to_parquet()

        parquet_path = self.output_parquet_file
        parquet_basename = os.path.basename(parquet_path)

        def upload_worker():
            try:
                # Disable upload button during operation
                self.root.after(0, lambda: self.upload_button.config(text="Uploading...", state="disabled"))

                minio_client = create_minio_client()

                # Get file size for verification
                parquet_size = os.path.getsize(parquet_path)

                # Direct upload of the parquet -- it carries every channel
                self.root.after(0, lambda: self.upload_button.config(text="Uploading parquet..."))
                minio_client.fput_object(BUCKET_NAME, parquet_basename, parquet_path)

                # Verify uploaded file
                self.root.after(0, lambda: self.upload_button.config(text="Verifying upload..."))
                final_parquet_obj = minio_client.stat_object(BUCKET_NAME, parquet_basename)

                if final_parquet_obj.size != parquet_size:
                    raise Exception("Parquet file verification failed - size mismatch")

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

                    messagebox.showinfo("Upload Complete",
                                        f"File uploaded to MinIO successfully!\n\n"
                                        f"Parquet: {parquet_basename}")

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
        """Copy the current recording's parquet to a user-chosen folder."""
        if not os.path.exists(self.output_parquet_file):
            self.save_to_parquet()
            if not os.path.exists(self.output_parquet_file):
                return          # save_to_parquet has already said why

        dest_dir = filedialog.askdirectory(title="Choose folder to save recording")
        if not dest_dir:
            return

        name = os.path.basename(self.output_parquet_file)
        try:
            shutil.copy2(self.output_parquet_file, os.path.join(dest_dir, name))
        except Exception as e:
            messagebox.showerror("Save to Disk", f"{name} could not be copied:\n\n{e}")
            return

        self.upload_button.config(bg="SystemButtonFace", fg="black")
        messagebox.showinfo("Save to Disk", f"Saved {name} to:\n{dest_dir}")

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
        """Play AI0 from the samples held in memory"""
        try:
            if self.recorded_data is not None and len(self.recorded_data) > 0:
                sample_rate = self.source_sample_rate()
                audio = self._prepare_audio(self.recorded_data[0], sample_rate)
                self._play_at(audio, sample_rate)
            else:
                messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play audio: {str(e)}")

    @staticmethod
    def stop_audio():
        """Stop any active sounddevice playback."""
        sd.stop()

    @staticmethod
    def _play_at(audio, rate):
        """Play at the native rate, resampling to 48 kHz if the card refuses it.

        The CN0582's 256 kHz is past what most output devices accept -- the LAN-XI's
        51.2 kHz mostly went through untouched -- so every high-rate channel needs
        this fallback, not just AI04.
        """
        try:
            sd.play(audio, int(rate))
        except Exception:
            from scipy.signal import resample as _rs
            n_out = int(len(audio) * 48000 / rate)
            sd.play(_rs(audio, n_out).astype(np.float32), 48000)

    def play_ai2_audio(self):
        """Play the recorded audio from AI2 (accelerometer channel)"""
        try:
            if self.recorded_data is not None and len(self.recorded_data) > 2:
                # _prepare_audio peak-normalises, so no per-channel sensitivity
                # divisor is needed here.
                audio = self._prepare_audio(self.recorded_data[2], self.daq_sample_rate)
                self._play_at(audio, self.daq_sample_rate)
            else:
                messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play AI2 audio: {str(e)}")

    def play_ai3_audio(self):
        """Play the recorded audio from AI3 (HBK 4518 CCLD microphone channel)"""
        try:
            if self.recorded_data is not None and len(self.recorded_data) > 3:
                audio = self._prepare_audio(self.recorded_data[3], self.daq_sample_rate)
                self._play_at(audio, self.daq_sample_rate)
            else:
                messagebox.showwarning("No Audio", "No recorded data found. Please record audio first.")
        except Exception as e:
            messagebox.showerror("Playback Error", f"Failed to play AI3 audio: {str(e)}")

    def play_ai4_audio(self):
        """Play the recorded STwin AI04 channel at 192 kHz (falls back to 48 kHz if unsupported)."""
        try:
            signal = self.stwinma2_signal()
            if signal is None:
                messagebox.showwarning("No Audio", "No AI04 data recorded yet.")
                return
            audio = self._prepare_audio(signal, STWINMA2_SAMPLE_RATE)
            self._play_at(audio, STWINMA2_SAMPLE_RATE)
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

