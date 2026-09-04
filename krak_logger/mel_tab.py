"""Mel Spectrogram tab."""

import tkinter as tk
from tkinter import ttk

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from scipy.signal import butter, sosfilt

from .config import STWINMA2_SAMPLE_RATE


def compute_mel_spectrogram(signal, sr, n_mels=128, f_max=None):
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


def build_mel_tab(notebook, root_win, logger):
    """Add a Mel Spectrogram tab to notebook.

    logger is the LoggerTab instance; its current recording (WAV files,
    in-memory channel data and sample rates) is read at plot time.
    """
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

        recorded_data = logger.recorded_data

        # --- Gather signal ---
        sig, sr = None, None
        if ch == "AI0":
            if recorded_data is None or len(recorded_data) == 0:
                status_var.set("No AI0 data."); return
            sig = np.asarray(recorded_data[0], dtype=np.float32)
            sr  = logger.source_sample_rate()
        elif ch == "AI2":
            if recorded_data is None or len(recorded_data) <= 2:
                status_var.set("No AI2 data."); return
            sig = (recorded_data[2] / 10.0).astype(np.float32)
            sr  = logger.daq_sample_rate
        elif ch == "AI04":
            stwin = logger.stwinma2_signal()
            if stwin is None:
                status_var.set("No STWINMA2 data. Record with source 'STWINMA2 Ch0 (192 kHz)' or enable 'Also record STWINMA2 Ch0'."); return
            sig = np.asarray(stwin, dtype=np.float32)
            sr  = STWINMA2_SAMPLE_RATE
        else:
            if recorded_data is None or len(recorded_data) <= 3:
                status_var.set("No AI3 data."); return
            sig = recorded_data[3].astype(np.float32)
            sr  = logger.daq_sample_rate

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
            mel_db, t, mel_hz = compute_mel_spectrogram(sig, sr, n_mels, f_max=mel_fmax)
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
