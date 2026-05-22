"""KRAK Signal Processing GUI â€” Set Window / Trim / Feature Extraction"""

import io
import json
import os
import sys
import threading
from collections import defaultdict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog
from scipy.signal import find_peaks
from sklearn.base import BaseEstimator, ClassifierMixin

from signal_processor import SignalProcessor

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
# Paths  (relative to this script â†’ mobile cookie crusher/)
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

FEATURE_EXTRACTION_DIR = os.path.join(
    BASE_DIR,
    "KRAK-signal-processing-main",
    "KRAK-signal-processing-main",
    "feature_extraction",
)

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
    # Filter (off by default here â€” no filter_helper wired up)
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
loaded_df        = None
loaded_filename  = None
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
    # Font size â€” applies to all widgets via the named system fonts
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
    # TAB 1 â€” Set Window
    # ============================================================================

    # â”€â”€ Scrollable left panel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Right panel (plot) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    right_frame = tk.Frame(_paned)
    _paned.add(right_frame, minsize=400, stretch="always")

    # ============================================================================
    # LEFT PANEL â€” controls
    # ============================================================================

    # â”€â”€ Font size control â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    font_row = tk.Frame(left_frame)
    font_row.pack(fill=tk.X, pady=(0, 6))
    tk.Label(font_row, text="Font Size:").pack(side=tk.LEFT)
    font_spinbox = tk.Spinbox(font_row, from_=8, to=30, textvariable=font_size_var,
                              width=4, command=_apply_font_size)
    font_spinbox.pack(side=tk.LEFT, padx=4)
    font_spinbox.bind("<Return>", _apply_font_size)
    font_spinbox.bind("<FocusOut>", _apply_font_size)

    # â”€â”€ Section: File loading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    file_frame = tk.LabelFrame(left_frame, text="Load Parquet File (saved by KRAK Logger)", padx=8, pady=6)
    file_frame.pack(fill=tk.X, pady=(0, 6))

    def load_file():
        global loaded_df, loaded_filename, trim_result, detected_peaks_arr
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

            loaded_df = df
            loaded_filename = os.path.basename(path)
            trim_result = None
            detected_peaks_arr = None

            sr = sampling_rate_var.get()
            duration_s = len(df) / sr
            file_label_var.set(
                f"{loaded_filename}  |  {len(df):,} samples  |  {duration_s:.3f} s"
            )
            status_var.set(f"Loaded: {loaded_filename}")
            _draw_plot()
        except Exception as exc:
            messagebox.showerror("Load Error", f"Failed to load file:\n{exc}")

    load_btn = tk.Button(file_frame, text="Open Parquet File...", command=load_file,
                         bg="lightblue", width=22)
    load_btn.pack(side=tk.LEFT, padx=(0, 8))

    file_label_var = tk.StringVar(value="No file loaded")
    tk.Label(file_frame, textvariable=file_label_var,
             fg="blue", anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)

    # â”€â”€ Section: Sampling rate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    sr_frame = tk.Frame(left_frame)
    sr_frame.pack(fill=tk.X, pady=(0, 4))
    tk.Label(sr_frame, text="Sampling Rate (Hz):").pack(side=tk.LEFT)
    sampling_rate_var = tk.IntVar(value=DEFAULT_CONFIG["SAMPLING_RATE"])
    tk.Entry(sr_frame, textvariable=sampling_rate_var, width=8).pack(side=tk.LEFT, padx=4)

    # â”€â”€ Section: Trim method â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    trim_frame = tk.LabelFrame(left_frame, text="Trimming Method", padx=8, pady=6)
    trim_frame.pack(fill=tk.X, pady=(0, 6))

    TRIM_METHODS = ["Peak Force", "Threshold", "Threshold %", "Threshold to Peak"]
    trim_method_var = tk.StringVar(value=DEFAULT_CONFIG["TRIM_METHOD"])
    trim_method_combo = ttk.Combobox(trim_frame, textvariable=trim_method_var,
                                     values=TRIM_METHODS, state="readonly", width=22)
    trim_method_combo.pack(anchor="w")

    # â”€â”€ Peak Force params â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Threshold params â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Threshold % params â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Threshold to Peak params â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Section: Peak-Based Fine Tuning â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ First-to-Last Peak params â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Sliding Window / Center Peak params â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Fine-Tuned Drop Detection params â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Section: find_peaks Parameters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Buttons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ Status label â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    status_var = tk.StringVar(value="Ready â€” load a parquet file to begin")
    tk.Label(left_frame, textvariable=status_var,
             fg="blue", relief=tk.SUNKEN, anchor="w").pack(fill=tk.X, pady=(4, 0))

    # ============================================================================
    # RIGHT PANEL â€” matplotlib figure
    # ============================================================================
    fig, axes = plt.subplots(4, 1, figsize=(9, 9), sharex=True)
    fig.tight_layout(pad=2.5)

    canvas_fig = FigureCanvasTkAgg(fig, master=right_frame)
    canvas_fig.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ============================================================================
    # TAB 2 â€” Feature Extraction
    # ============================================================================

    # â”€â”€ Left control panel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    feat_status_var = tk.StringVar(value="Ready â€” load a file and set trim window first")
    tk.Label(feat_left, textvariable=feat_status_var,
             fg="gray", anchor="w", wraplength=270, justify=tk.LEFT).pack(anchor="w", pady=(0, 8))

    # â”€â”€ Right results panel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    feat_right = tk.Frame(tab_feat)
    feat_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)

    # Prediction result label
    feat_pred_frame = tk.LabelFrame(feat_right, text="Prediction", padx=8, pady=8)
    feat_pred_frame.pack(fill=tk.X, pady=(0, 8))

    feat_class_var = tk.StringVar(value="â€”")
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
        if loaded_df is not None and loaded_filename is not None:
            sr = sampling_rate_var.get()
            n = len(loaded_df)
            feat_file_var.set(f"File: {loaded_filename}\n{n:,} samples @ {sr} Hz")
        else:
            feat_file_var.set("No file loaded")

        if trim_result is not None:
            s, e, dur = trim_result
            feat_trim_var.set(f"Trim: sample {s} â†’ {e}  ({dur:.4f} s)")
        else:
            feat_trim_var.set("No trim window â€” run Preview Trim Window first")

    # ============================================================================
    # Logic â€” Set Window
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
        if loaded_df is None:
            messagebox.showwarning("No File", "Please load a parquet file first.")
            return

        cfg = _build_config()
        acoustic_col = cfg["ACOUSTIC_COLUMN"]
        reference_col = cfg["REFERENCE_COLUMN"]
        sr = cfg["SAMPLING_RATE"]

        if acoustic_col not in loaded_df.columns or reference_col not in loaded_df.columns:
            messagebox.showerror(
                "Column Error",
                f"Required columns not found.\nAvailable: {loaded_df.columns.tolist()}"
            )
            return

        try:
            processor = SignalProcessor(cfg)
            acoustic = loaded_df[acoustic_col].values.astype(np.float32)
            reference = loaded_df[reference_col].values.astype(np.float32)
            ai2 = (loaded_df["AI2 (V)"].values.astype(np.float32)
                   if "AI2 (V)" in loaded_df.columns else None)

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
                                       "Trim returned an empty signal â€” adjust parameters.")
                return

            duration_s = len(trimmed) / sr
            trim_result = (s, e, duration_s)
            status_var.set(
                f"Trim window ({tm}): sample {s} â†’ {e}  |  {duration_s:.4f} s"
            )

            # Peak-based refinement (First-to-Last Peak): narrow trim window to acoustic peaks
            detected_peaks_arr = None
            if cfg.get("REFINE_WITH_PEAKS") and cfg.get("PEAK_REFINEMENT_METHOD") == "First-to-Last Peak":
                pk_ch = cfg.get("PEAK_DETECTION_CHANNEL", acoustic_col)
                pk_sig = (loaded_df[pk_ch].values.astype(np.float32)[s:e]
                          if pk_ch in loaded_df.columns else acoustic[s:e])
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
                        pk_sig2 = (loaded_df[pk_ch].values.astype(np.float32)[s:e]
                                   if pk_ch in loaded_df.columns else acoustic[s:e])
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

        if loaded_df is None:
            for ax in axes:
                ax.set_visible(False)
            fig.canvas.draw()
            return

        for ax in axes:
            ax.set_visible(True)

        sr = sampling_rate_var.get()
        time_axis = np.arange(len(loaded_df)) / sr
        cols = loaded_df.columns.tolist()

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
                data = loaded_df[col].values
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
                            loaded_df["AI0 (V)"].values[pk_abs],
                            "r^", markersize=5, zorder=5,
                            label=f"{len(detected_peaks_arr)} peaks")

                if (col == "AI2 (V)" and has_trim and detected_peaks_arr is not None
                        and len(detected_peaks_arr) > 0
                        and peak_channel_var.get() == "AI2 (V)"):
                    pk_abs = detected_peaks_arr + s_idx
                    ax.plot(pk_abs / sr,
                            loaded_df["AI2 (V)"].values[pk_abs],
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
    # Logic â€” Feature Extraction
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

        if loaded_df is None:
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
        feat_status_var.set("Starting feature extractionâ€¦")
        feat_progress_var.set(0)
        feat_class_var.set("â€”")
        feat_conf_var.set("")
        feat_missing_var.set("")
        feat_missing_label.config(fg="gray")

        s_idx, e_idx, dur = trim_result
        signal = loaded_df["AI0 (V)"].values[s_idx:e_idx].astype(np.float64)
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
        feat_status_var.set("Cancellingâ€¦")


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
        n_total = len(classes)  # approximate — use required feature count if available
        if missing:
            warn = f"WARNING: {len(missing)} features zero-padded  |  Done: {pred_class} ({top_prob:.1%})"
            feat_status_var.set(warn)
            feat_missing_var.set(f"{len(missing)} features missing (zero-padded) — prediction may be unreliable!\n"
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

            _feat_progress(0, 1, "Importing librariesâ€¦")
            from feature_extraction_class import FeatureExtractor  # noqa: F401
            import joblib

            if feat_cancel_flag.is_set():
                _feat_progress(0, 1, "Cancelled.")
                return

            # --- Initialise extractor ---
            _feat_progress(0, 1, "Initialising extractorâ€¦")
            extractor = FeatureExtractor(acoustic_column="AI0 (V)", sampling_rate=sr)

            # --- Load required features & build extraction plan ---
            required_features = load_required_features(m_dir)

            # Strip channel/normalisation suffix the extractor doesn’t add
            _req_sfx = ("_AI0_PN", "_AI2_PN")
            required_features = [
                next((f[:-len(s)] for s in _req_sfx if f.endswith(s)), f)
                for f in required_features
            ]

            # Parse each feature → (filter, config_or_None)
            plan = {}   # filter_name â†’ {"configs": set, "base_needed": bool}
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

            all_extracted = {}   # final_feature_name â†’ value

            for filter_name, group in plan.items():
                if feat_cancel_flag.is_set():
                    _feat_progress(call_idx, n_calls, "Cancelled.")
                    return

                # Apply Butterworth filter
                filter_cfg = extractor.filter_configs.get(filter_name, {})
                _feat_progress(call_idx, n_calls,
                               f"Applying filter: {filter_name}â€¦")

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
            _feat_progress(n_calls, n_calls, "Building feature vectorâ€¦")
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
            _feat_progress(n_calls, n_calls, "Loading modelâ€¦")
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
                        pass  # read-only property (e.g. Pipeline) — recurse into sub-estimators instead
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

            _feat_progress(n_calls, n_calls, "Running predictionâ€¦")
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
    # TAB 3 — Deep Predict  (PaSST 10s + 3s ensemble)
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
        if loaded_df is not None and loaded_filename is not None:
            sr = sampling_rate_var.get()
            n = len(loaded_df)
            deep_file_var.set(f"File: {loaded_filename}\n{n:,} samples @ {sr} Hz")
        else:
            deep_file_var.set("No file loaded")
        if trim_result is not None:
            s, e, dur = trim_result
            deep_trim_var.set(f"Trim: sample {s} → {e}  ({dur:.4f} s)")
        else:
            deep_trim_var.set("No trim window — run Preview Trim Window first")

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
    tk.Button(_py_row, text="…", width=3, command=_browse_python).pack(side=tk.LEFT, padx=(2, 0))

    # Model directory
    tk.Label(deep_model_frame, text="Model directory (.pt files):", anchor="w").pack(anchor="w")
    _mdir_row = tk.Frame(deep_model_frame); _mdir_row.pack(fill=tk.X, pady=(0, 4))
    deep_model_dir_var = tk.StringVar(value=MODELS_DIR if os.path.isdir(MODELS_DIR) else SCRIPT_DIR)
    tk.Entry(_mdir_row, textvariable=deep_model_dir_var, width=22).pack(side=tk.LEFT, fill=tk.X, expand=True)
    def _browse_model_dir():
        d = filedialog.askdirectory(title="Select folder containing .pt checkpoints")
        if d: deep_model_dir_var.set(d)
    tk.Button(_mdir_row, text="…", width=3, command=_browse_model_dir).pack(side=tk.LEFT, padx=(2, 0))

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

    tk.Button(_mdl_r1, text="…", width=3,
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
    _mdl_combo_paths = {}   # basename → full path
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

    tk.Button(deep_mdl_frame, text="⟳ Scan Dir",
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

    deep_ens_pred_var = tk.StringVar(value="—")
    tk.Label(deep_pred_frame, textvariable=deep_ens_pred_var, fg="steelblue",
             font=tkfont.Font(font=tkfont.nametofont("TkDefaultFont"), size=20, weight="bold")
             ).pack(side=tk.LEFT)

    # Chart frame — canvas is created/replaced dynamically
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
        deep_model_status_var.set("Validating…")
        deep_status_var.set("Testing Python interpreter…")

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
        deep_status_var.set("Setup OK — ready to run prediction" if all_ok
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
        if loaded_df is None:
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
        deep_status_var.set("Running inference…")
        deep_ens_pred_var.set("—")

        s_idx, e_idx, _ = trim_result
        signal = loaded_df["AI0 (V)"].values[s_idx:e_idx].astype(np.float32)

        _deep_thread = threading.Thread(
            target=_deep_run_thread, args=(signal,), daemon=True
        )
        _deep_thread.start()


    def _deep_cancel():
        _deep_cancel_flag.set()
        deep_status_var.set("Cancelling…")


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

            root.after(0, lambda: deep_status_var.set("Loading model & running inference…"))
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


if __name__ == "__main__":
    _root = tk.Tk()
    _root.title("KRAK Signal Processing")
    _root.geometry("1440x900")
    _root.minsize(1100, 700)
    _nb = ttk.Notebook(_root)
    _nb.pack(fill=tk.BOTH, expand=True)
    _cleanup = build_signal_processing_tabs(_nb, _root)
    _root.protocol(
        "WM_DELETE_WINDOW",
        lambda: (_cleanup(), _root.quit(), _root.destroy()),
    )
    _root.mainloop()
