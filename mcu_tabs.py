"""MCU controller tab frames for integration into the KRAK Logger notebook.

All handle_line() methods are called from the main Tkinter thread (the
MCUController schedules them via root.after), so direct widget updates are safe.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import datetime
import csv
import os
import tkinter.font as _tkfont

# ---------------------------------------------------------------------------
# Font system – named Font objects so the whole UI rescales in one call
# ---------------------------------------------------------------------------

_BASE_SIZE = 10
_fonts: dict = {}


def _F() -> dict:
    """Lazy font store; safe to call only after tk.Tk() exists."""
    if not _fonts:
        b = _BASE_SIZE
        _fonts.update({
            "mono":       _tkfont.Font(family="Consolas", size=b - 1),
            "indicator":  _tkfont.Font(family="Arial",    size=b + 2),
            "small_bold": _tkfont.Font(size=b - 1, weight="bold"),
            "pos_bold":   _tkfont.Font(size=b,     weight="bold"),
            "value_lg":   _tkfont.Font(size=b + 3),
            "btn_large":  _tkfont.Font(size=b + 4, weight="bold"),
            "btn_stop":   _tkfont.Font(size=b + 6, weight="bold"),
            "btn_jog":    _tkfont.Font(size=b + 2, weight="bold"),
            "force":      _tkfont.Font(size=b + 10, weight="bold"),
        })
    return _fonts


def scale_fonts(base_size: int) -> None:
    """Rescale all app fonts. Must be called from the main thread."""
    global _BASE_SIZE
    _BASE_SIZE = base_size
    f = _F()
    f["mono"].configure(      family="Consolas", size=max(7, base_size - 1))
    f["indicator"].configure( family="Arial",    size=base_size + 2)
    f["small_bold"].configure(size=max(7, base_size - 1), weight="bold")
    f["pos_bold"].configure(  size=base_size,             weight="bold")
    f["value_lg"].configure(  size=base_size + 3)
    f["btn_large"].configure( size=base_size + 4, weight="bold")
    f["btn_stop"].configure(  size=base_size + 6, weight="bold")
    f["btn_jog"].configure(   size=base_size + 2, weight="bold")
    f["force"].configure(     size=base_size + 10, weight="bold")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                 "TkHeadingFont", "TkSmallCaptionFont", "TkTooltipFont"):
        try:
            _tkfont.nametofont(name).configure(size=base_size)
        except Exception:
            pass
    try:
        _tkfont.nametofont("TkFixedFont").configure(size=max(7, base_size - 1))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_field(response: str, field: str) -> str:
    """Extract 'FIELD:value' from a comma-separated response string."""
    prefix = field + ":"
    idx = response.find(prefix)
    if idx < 0:
        return "?"
    start = idx + len(prefix)
    end = response.find(",", start)
    return response[start:] if end < 0 else response[start:end]


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _append_log(text_widget: tk.Text, msg: str, max_chars: int = 8000) -> None:
    text_widget.config(state="normal")
    text_widget.insert(tk.END, msg)
    if len(text_widget.get("1.0", tk.END)) > max_chars:
        text_widget.delete("1.0", f"1.0+{max_chars // 2}c")
    text_widget.see(tk.END)
    text_widget.config(state="disabled")


def _indicator(parent, color="gray") -> tk.Label:
    """A small coloured circle label used as a status indicator."""
    lbl = tk.Label(parent, text="●", font=_F()["indicator"], fg=color)
    return lbl


# ---------------------------------------------------------------------------
# MCUController – owns the protocol and all tab frames
# ---------------------------------------------------------------------------

class MCUController:
    """
    Creates all MCU tab frames, adds them to *notebook*, and routes serial
    lines to every tab's handle_line() method on the main thread.

    Parameters
    ----------
    notebook : ttk.Notebook
        The parent notebook to which MCU tabs are appended.
    root : tk.Tk
        Used to schedule serial callbacks on the main thread.
    protocol : MCUProtocol
        The already-created serial protocol object.
    """

    def __init__(self, notebook: ttk.Notebook, root: tk.Tk, protocol, start_daq=None, duration_var=None):
        self.protocol = protocol
        self._root = root

        self.measurement = MeasurementTab(notebook, self, start_daq=start_daq, duration_var=duration_var)
        self.position = PositionTab(notebook, self)
        self.speed_cal = SpeedCalTab(notebook, self)
        self.pos_cal = PosCalTab(notebook, self)
        self.load_cal = LoadCellCalTab(notebook, self)
        self.pid = PIDTuningTab(notebook, self)
        self.diag = DiagnosticsTab(notebook, self)

        self._tabs = [
            self.measurement, self.position, self.speed_cal,
            self.pos_cal, self.load_cal, self.pid, self.diag,
        ]

        notebook.add(self.measurement, text="Measurement")
        notebook.add(self.position,    text="Position")
        notebook.add(self.speed_cal,   text="Speed Cal")
        notebook.add(self.pos_cal,     text="Pos Cal")
        notebook.add(self.load_cal,    text="Load Cell Cal")
        notebook.add(self.pid,         text="PID Tuning")
        notebook.add(self.diag,        text="Diagnostics")

        protocol.add_callback(self._on_line_raw)

    # Called from serial read thread
    def _on_line_raw(self, line: str) -> None:
        self._root.after(0, lambda l=line: self._dispatch(l))

    # Called on main thread
    def _dispatch(self, line: str) -> None:
        for tab in self._tabs:
            for attr in ("_log", "_rx_log"):
                if hasattr(tab, attr):
                    try:
                        _append_log(getattr(tab, attr), f"[{_ts()}] RAW> {line}\n")
                    except Exception:
                        pass
            try:
                tab.handle_line(line)
            except Exception:
                pass

    def send(self, cmd: str) -> None:
        self.protocol.send(cmd)

    def on_connect(self) -> None:
        for tab in self._tabs:
            tab.on_connect()
        # Sync speed slider value
        self.position.sync_speed()

    def on_disconnect(self) -> None:
        for tab in self._tabs:
            tab.on_disconnect()


# ---------------------------------------------------------------------------
# Base tab
# ---------------------------------------------------------------------------

class _MCUTab(ttk.Frame):
    def __init__(self, parent, ctrl: MCUController):
        super().__init__(parent)
        self._ctrl = ctrl

    def send(self, cmd: str) -> None:
        self._ctrl.send(cmd)

    def handle_line(self, line: str) -> None:  # override in subclasses
        pass

    def on_connect(self) -> None:
        pass

    def on_disconnect(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tab 1 – Measurement
# ---------------------------------------------------------------------------

class MeasurementTab(_MCUTab):
    def __init__(self, parent, ctrl, start_daq=None, duration_var=None):
        super().__init__(parent, ctrl)
        self._is_measuring = False
        self._start_daq = start_daq  # callable(on_daq_ready=fn) or None
        self._duration_var = duration_var
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 5}

        # ── Method 1: MEASMAXFORCE ──────────────────────────────────────────
        gf1 = ttk.LabelFrame(self, text="Method 1 – Max Force Measurement (MEASMAXFORCE)", padding=8)
        gf1.pack(fill=tk.X, **pad)

        ttk.Label(gf1, text="Moves actuator DOWN until the specified maximum force is reached.").pack(anchor="w")
        row1 = ttk.Frame(gf1); row1.pack(anchor="w", pady=(6, 0))
        ttk.Label(row1, text="Target Force (N):").pack(side=tk.LEFT)
        self._mf_force = ttk.Entry(row1, width=10); self._mf_force.insert(0, "100"); self._mf_force.pack(side=tk.LEFT, padx=5)
        self._mf_btn = ttk.Button(row1, text="Start Measurement", command=self._start_maxforce, state="disabled")
        self._mf_btn.pack(side=tk.LEFT, padx=5)
        row1s = ttk.Frame(gf1); row1s.pack(anchor="w", pady=(4, 0))
        ttk.Label(row1s, text="Status:", font=_F()["small_bold"]).pack(side=tk.LEFT)
        self._mf_status = ttk.Label(row1s, text="Ready", foreground="gray"); self._mf_status.pack(side=tk.LEFT, padx=4)

        # ── Method 2: MEASTHRESHOLD ─────────────────────────────────────────
        gf2 = ttk.LabelFrame(self, text="Method 2 – Threshold Measurement (MEASTHRESHOLD)", padding=8)
        gf2.pack(fill=tk.X, **pad)

        ttk.Label(gf2, text="Moves DOWN until threshold force reached, then continues for extra distance.").pack(anchor="w")
        row2 = ttk.Frame(gf2); row2.pack(anchor="w", pady=(6, 0))
        ttk.Label(row2, text="Threshold Force (N):").pack(side=tk.LEFT)
        self._th_force = ttk.Entry(row2, width=8); self._th_force.insert(0, "50"); self._th_force.pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Extra Distance (mm):").pack(side=tk.LEFT)
        self._th_dist = ttk.Entry(row2, width=8); self._th_dist.insert(0, "10"); self._th_dist.pack(side=tk.LEFT, padx=4)
        self._th_btn = ttk.Button(row2, text="Start Measurement", command=self._start_threshold, state="disabled")
        self._th_btn.pack(side=tk.LEFT, padx=5)
        row2s = ttk.Frame(gf2); row2s.pack(anchor="w", pady=(4, 0))
        ttk.Label(row2s, text="Status:", font=_F()["small_bold"]).pack(side=tk.LEFT)
        self._th_status = ttk.Label(row2s, text="Ready", foreground="gray"); self._th_status.pack(side=tk.LEFT, padx=4)

        # ── Control / log ───────────────────────────────────────────────────
        gf3 = ttk.LabelFrame(self, text="Measurement Control", padding=8)
        gf3.pack(fill=tk.BOTH, expand=True, **pad)

        if self._duration_var is not None:
            dur_frame = ttk.Frame(gf3)
            dur_frame.pack(anchor="e", pady=(0, 6), fill=tk.X)
            ttk.Label(dur_frame, text="Audio Recording/Measurement Duration (s):").pack(side=tk.LEFT)
            ttk.Entry(dur_frame, textvariable=self._duration_var, width=8).pack(side=tk.LEFT, padx=4)

        self._log = tk.Text(gf3, height=10, state="disabled", background="#f5f5f5", font=_F()["mono"])
        self._log.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(gf3, command=self._log.yview); sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log.config(yscrollcommand=sb.set)

        btns = ttk.Frame(gf3); btns.pack(anchor="w", pady=(5, 0))
        self._stop_btn = tk.Button(btns, text="⚠ Stop Measurement", command=self._stop,
                                   state="disabled", bg="#ff4444", fg="white", font=_F()["small_bold"], padx=12, pady=5)
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btns, text="Clear Log", command=self._clear_log).pack(side=tk.LEFT)

    # ── Commands ────────────────────────────────────────────────────────────

    def _arm_measurement(self, cmd: str, status_lbl):
        """Common logic for both measurement modes. If a DAQ is linked, starts
        DAQ recording first and sends *cmd* only once the stream socket connects.
        Without a DAQ link, sends *cmd* immediately."""
        self._is_measuring = True
        self._mf_btn.config(state="disabled")
        self._th_btn.config(state="disabled")
        self._stop_btn.config(state="normal")

        if self._start_daq is not None:
            status_lbl.config(text="Starting DAQ…", foreground="orange")
            _append_log(self._log, f"[{_ts()}] Starting DAQ → will send '{cmd}' when streaming\n")

            def _on_daq_ready():
                # Called from recording background thread – send is thread-safe
                self.send(cmd)
                # Marshal UI updates back to main thread
                self.after(0, lambda: status_lbl.config(
                    text="DAQ streaming – MCU triggered ✓", foreground="blue"))
                self.after(0, lambda: _append_log(
                    self._log, f"[{_ts()}] DAQ ready → sent '{cmd}' to MCU\n"))

            self._start_daq(on_daq_ready=_on_daq_ready)
        else:
            self.send(cmd)
            status_lbl.config(text="Starting…", foreground="orange")
            _append_log(self._log, f"[{_ts()}] {cmd}\n")

    def _start_maxforce(self):
        try:
            f = float(self._mf_force.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter a valid force value (N).")
            return
        if not (0 < f <= 600):
            messagebox.showwarning("Input Error", "Force must be between 0 and 600 N.")
            return
        self._arm_measurement(f"MEASMAXFORCE {f:.1f}", self._mf_status)

    def _start_threshold(self):
        try:
            f = float(self._th_force.get())
            d = float(self._th_dist.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter valid numeric values.")
            return
        if not (0 < f <= 600):
            messagebox.showwarning("Input Error", "Threshold force must be 0–600 N.")
            return
        if d <= 0:
            messagebox.showwarning("Input Error", "Extra distance must be > 0.")
            return
        self._arm_measurement(f"MEASTHRESHOLD {f:.1f} {d:.1f}", self._th_status)

    def _stop(self):
        self.send("STOP")
        self._is_measuring = False
        self._mf_btn.config(state="normal")
        self._th_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._mf_status.config(text="Stopped", foreground="red")
        self._th_status.config(text="Stopped", foreground="red")
        _append_log(self._log, f"[{_ts()}] Measurement stopped by user.\n")

    def _clear_log(self):
        self._log.config(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.config(state="disabled")

    # ── Response handling ───────────────────────────────────────────────────

    def handle_line(self, line: str):
        if line.startswith("Starting measurement until max force"):
            self._mf_status.config(text="Measuring…", foreground="blue")
            _append_log(self._log, f"[{_ts()}] {line}\n")
        elif line == "MEASMAXFORCE:RETRACTING":
            self._mf_status.config(text="Retracting 40 mm…", foreground="orange")
            _append_log(self._log, f"[{_ts()}] Force reached – retracting 20 mm\n")
        elif line == "MEASMAXFORCE:COMPLETE":
            self._mf_status.config(text="Complete ✓", foreground="green")
            self._mf_btn.config(state="normal")
            self._th_btn.config(state="normal")
            self._stop_btn.config(state="disabled")
            self._is_measuring = False
            _append_log(self._log, f"[{_ts()}] Measurement complete\n")
        elif line.startswith("Starting threshold measurement"):
            self._th_status.config(text="Moving to threshold…", foreground="blue")
            _append_log(self._log, line + "\n")
        elif line.startswith("Threshold reached"):
            self._th_status.config(text="Moving extra distance…", foreground="orange")
            _append_log(self._log, line + "\n")
        elif line.startswith("Threshold measurement complete"):
            _append_log(self._log, line + "\n")
        elif line == "MEASTHRESHOLD:RETRACTING":
            self._th_status.config(text="Retracting 40 mm…", foreground="orange")
            _append_log(self._log, f"[{_ts()}] Extra distance done – retracting 20 mm\n")
        elif line == "MEASTHRESHOLD:COMPLETE":
            self._th_status.config(text="Complete ✓", foreground="green")
            self._mf_btn.config(state="normal")
            self._th_btn.config(state="normal")
            self._stop_btn.config(state="disabled")
            self._is_measuring = False
            _append_log(self._log, f"[{_ts()}] Measurement complete\n")
        elif line.startswith("Moving extra distance"):
            _append_log(self._log, line + "\n")

    def on_connect(self):
        self._mf_btn.config(state="normal")
        self._th_btn.config(state="normal")
        self._mf_status.config(text="Ready", foreground="gray")
        self._th_status.config(text="Ready", foreground="gray")

    def on_disconnect(self):
        self._mf_btn.config(state="disabled")
        self._th_btn.config(state="disabled")
        self._stop_btn.config(state="disabled")
        self._is_measuring = False


# ---------------------------------------------------------------------------
# Tab 2 – Position
# ---------------------------------------------------------------------------

class PositionTab(_MCUTab):
    def __init__(self, parent, ctrl):
        super().__init__(parent, ctrl)
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 5}

        # ── Movement ────────────────────────────────────────────────────────
        gm = ttk.LabelFrame(self, text="Movement Control", padding=8)
        gm.pack(fill=tk.X, **pad)

        btns = ttk.Frame(gm); btns.pack(fill=tk.X, pady=(0, 8))
        self._up_btn = tk.Button(btns, text="▲  UP", height=2, width=18,
                                 font=_F()["btn_large"], state="disabled",
                                 command=lambda: (self.send("UP"), self._clear_endstops()))
        self._up_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._dn_btn = tk.Button(btns, text="▼  DOWN", height=2, width=18,
                                 font=_F()["btn_large"], state="disabled",
                                 command=lambda: (self.send("DOWN"), self._clear_endstops()))
        self._dn_btn.pack(side=tk.LEFT)

        spd = ttk.Frame(gm); spd.pack(anchor="w")
        ttk.Label(spd, text="Speed (mm/s):").pack(side=tk.LEFT)
        self._spd_var = tk.IntVar(value=5)
        self._spd_lbl = ttk.Label(spd, text="5", width=3); self._spd_lbl.pack(side=tk.LEFT, padx=4)
        self._spd_slider = ttk.Scale(spd, from_=1, to=5, orient=tk.HORIZONTAL,
                                     variable=self._spd_var, length=200,
                                     command=self._on_speed_change)
        self._spd_slider.pack(side=tk.LEFT)

        # ── Home ─────────────────────────────────────────────────────────────
        gh = ttk.LabelFrame(self, text="Home Position", padding=8)
        gh.pack(fill=tk.X, **pad)

        ttk.Label(gh, text="Jog to desired home, then 'Set Home'. Use 'Go Home' to return.").pack(anchor="w")
        hrow = ttk.Frame(gh); hrow.pack(anchor="w", pady=(6, 0))
        self._set_home_btn = ttk.Button(hrow, text="Set Home (CALPOS)", state="disabled",
                                        command=self._set_home)
        self._set_home_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._go_home_btn = ttk.Button(hrow, text="Go Home", state="disabled",
                                       command=self._go_home)
        self._go_home_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._get_pos_btn = ttk.Button(hrow, text="Get Position", state="disabled",
                                       command=lambda: self.send("GET_POS"))
        self._get_pos_btn.pack(side=tk.LEFT, padx=(0, 12))
        self._pos_lbl = ttk.Label(hrow, text="Pos: — mm", foreground="blue", font=_F()["pos_bold"])
        self._pos_lbl.pack(side=tk.LEFT, padx=(0, 12))
        self._home_status = ttk.Label(hrow, text="", font=_F()["small_bold"])
        self._home_status.pack(side=tk.LEFT)

        # ── Emergency stop ───────────────────────────────────────────────────
        ge = ttk.LabelFrame(self, text="Emergency", padding=8)
        ge.pack(fill=tk.X, **pad)
        self._stop_btn = tk.Button(ge, text="⚠  STOP", height=2, bg="#ff4444",
                                   fg="white", font=_F()["btn_stop"],
                                   state="disabled", command=lambda: self.send("STOP"))
        self._stop_btn.pack(fill=tk.X, padx=4)

        # ── Status ───────────────────────────────────────────────────────────
        gs = ttk.LabelFrame(self, text="Status and Feedback", padding=8)
        gs.pack(fill=tk.BOTH, expand=True, **pad)

        row_conn = ttk.Frame(gs); row_conn.pack(anchor="w", pady=(0, 6))
        ttk.Label(row_conn, text="Connection:", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 4))
        self._conn_lbl = ttk.Label(row_conn, text="Disconnected", foreground="red")
        self._conn_lbl.pack(side=tk.LEFT)

        endstops = ttk.Frame(gs); endstops.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(endstops, text="Endstop Top:", font=_F()["small_bold"]).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._es_top = ttk.Label(endstops, text="Unknown"); self._es_top.grid(row=0, column=1, sticky="w", padx=(0, 20))
        ttk.Label(endstops, text="Endstop Bottom:", font=_F()["small_bold"]).grid(row=0, column=2, sticky="w", padx=(0, 6))
        self._es_bot = ttk.Label(endstops, text="Unknown"); self._es_bot.grid(row=0, column=3, sticky="w")

        ttk.Label(gs, text="Force (Load Cell):", font=_F()["small_bold"]).pack(anchor="w")
        self._force_lbl = ttk.Label(gs, text="N/A", font=_F()["force"], foreground="navy")
        self._force_lbl.pack(anchor="w", pady=(2, 8))

        ttk.Label(gs, text="Received Data:", font=_F()["small_bold"]).pack(anchor="w")
        self._rx_log = tk.Text(gs, height=6, state="disabled", background="#f5f5f5",
                               font=_F()["mono"])
        self._rx_log.pack(fill=tk.BOTH, expand=True)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _on_speed_change(self, val=None):
        v = int(self._spd_var.get())
        self._spd_lbl.config(text=str(v))
        if self._ctrl.protocol.connected:
            self.send(f"SETSPD {v}")

    def sync_speed(self):
        self.send(f"SETSPD {int(self._spd_var.get())}")

    def _set_home(self):
        self.send("CALPOS")
        self._home_status.config(text="Home set (CALPOS)", foreground="green")

    def _go_home(self):
        self.send("HOME")
        self._home_status.config(text="Homing…", foreground="orange")

    def _clear_endstops(self):
        self._es_top.config(text="Clear", foreground="green")
        self._es_bot.config(text="Clear", foreground="green")

    def _append_rx(self, msg: str):
        _append_log(self._rx_log, msg)

    # ── Response handling ─────────────────────────────────────────────────────

    def handle_line(self, line: str):
        if line.startswith("ENDSTOP_TOP:"):
            v = line.split(":")[1].strip()
            self._es_top.config(text="TRIGGERED" if v == "1" else "Clear",
                                foreground="red" if v == "1" else "green")
        elif line.startswith("ENDSTOP_BOTTOM:"):
            v = line.split(":")[1].strip()
            self._es_bot.config(text="TRIGGERED" if v == "1" else "Clear",
                                foreground="red" if v == "1" else "green")
        elif line.startswith("FORCE:"):
            self._force_lbl.config(text=line.split(":")[1].strip() + " N")
        elif line.startswith("POS:"):
            self._pos_lbl.config(text="Pos: " + line.split(":")[1].strip() + " mm")
        elif line.startswith("HOME:OK"):
            self._home_status.config(text="At home position ✓", foreground="green")
            self._append_rx("Homing complete.\n")
        elif line.startswith("END:IN"):
            self._es_top.config(text="TRIGGERED", foreground="red")
            self._es_bot.config(text="Clear", foreground="green")
            self._append_rx("Inner endstop reached.\n")
        elif line.startswith("END:OUT"):
            self._es_bot.config(text="TRIGGERED", foreground="red")
            self._es_top.config(text="Clear", foreground="green")
            self._append_rx("Outer endstop reached (140 mm).\n")
        elif line.startswith("ERR:OVERCURRENT"):
            self._append_rx("SAFETY: Overcurrent – motor stopped.\n")
        elif line.startswith("ERR:ABSOLUTE_LIMIT"):
            self._append_rx("SAFETY: Absolute force limit (600 N) reached.\n")
        elif line.startswith("ERR:STALL"):
            self._append_rx("SAFETY: Motor stall detected.\n")
        elif line.startswith("ERR:DUTY_CYCLE"):
            self._append_rx("SAFETY: Duty cycle limit – 8 min cooldown started.\n")
        elif line.startswith("ERR:COOLDOWN"):
            self._append_rx("SAFETY: Cooldown active – wait before moving.\n")
        elif line.startswith("MAX LOAD REACHED"):
            self._append_rx("SAFETY: Max load reached – retracting.\n")

    def on_connect(self):
        self._up_btn.config(state="normal")
        self._dn_btn.config(state="normal")
        self._stop_btn.config(state="normal")
        self._set_home_btn.config(state="normal")
        self._go_home_btn.config(state="normal")
        self._get_pos_btn.config(state="normal")
        self._conn_lbl.config(text="Connected", foreground="green")

    def on_disconnect(self):
        self._up_btn.config(state="disabled")
        self._dn_btn.config(state="disabled")
        self._stop_btn.config(state="disabled")
        self._set_home_btn.config(state="disabled")
        self._go_home_btn.config(state="disabled")
        self._get_pos_btn.config(state="disabled")
        self._conn_lbl.config(text="Disconnected", foreground="red")
        self._force_lbl.config(text="N/A")
        self._pos_lbl.config(text="Pos: — mm")


# ---------------------------------------------------------------------------
# Tab 3 – Speed Calibration
# ---------------------------------------------------------------------------

class SpeedCalTab(_MCUTab):
    def __init__(self, parent, ctrl):
        super().__init__(parent, ctrl)
        self._waiting_disp = False
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 5}

        gp = ttk.LabelFrame(self, text="Speed Calibration Procedure (CALSPD)", padding=8)
        gp.pack(fill=tk.X, **pad)

        info = ("Steps:\n"
                "1. Move actuator to home position.\n"
                "2. Click Start – firmware moves 7,247,700 encoder counts.\n"
                "3. Measure actual displacement with a ruler (mm).\n"
                "4. Enter the measured value when prompted.\n"
                "5. COUNTS_PER_MM is computed and stored in EEPROM.")
        ttk.Label(gp, text=info, justify=tk.LEFT).pack(anchor="w", pady=(0, 8))

        row = ttk.Frame(gp); row.pack(anchor="w")
        self._start_btn = ttk.Button(row, text="Start Calibration (CALSPD)", state="disabled",
                                     command=self._start)
        self._start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._stop_btn = ttk.Button(row, text="Stop", state="disabled",
                                    command=lambda: (self.send("STOP"), self._reset_ui()))
        self._stop_btn.pack(side=tk.LEFT, padx=(0, 10))
        self._status_lbl = ttk.Label(row, text="", font=_F()["small_bold"])
        self._status_lbl.pack(side=tk.LEFT)

        # Displacement input (hidden until firmware prompts)
        self._disp_frame = ttk.LabelFrame(self, text="Measured Displacement Input", padding=8)
        ttk.Label(self._disp_frame,
                  text="Calibration move complete. Measure displacement with ruler and enter below:",
                  foreground="darkorange", font=_F()["small_bold"]).pack(anchor="w", pady=(0, 6))
        drow = ttk.Frame(self._disp_frame); drow.pack(anchor="w")
        ttk.Label(drow, text="Measured Displacement (mm):").pack(side=tk.LEFT)
        self._disp_entry = ttk.Entry(drow, width=12); self._disp_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(drow, text="Submit", command=self._submit_disp).pack(side=tk.LEFT)

        gr = ttk.LabelFrame(self, text="Calibration Results", padding=8)
        gr.pack(fill=tk.X, **pad)
        row2 = ttk.Frame(gr); row2.pack(fill=tk.X)
        ttk.Label(row2, text="Encoder Counts Moved:", font=_F()["small_bold"]).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(row2, text="7,247,700 counts", font=_F()["value_lg"]).grid(row=0, column=1, sticky="w", padx=(0, 30))
        ttk.Label(row2, text="COUNTS_PER_MM:", font=_F()["small_bold"]).grid(row=0, column=2, sticky="w", padx=(0, 8))
        self._cpm_lbl = ttk.Label(row2, text="N/A", font=_F()["btn_large"], foreground="green")
        self._cpm_lbl.grid(row=0, column=3, sticky="w")

        gl = ttk.LabelFrame(self, text="Calibration Log", padding=8)
        gl.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = tk.Text(gl, height=8, state="disabled", background="#f5f5f5", font=_F()["mono"])
        self._log.pack(fill=tk.BOTH, expand=True)

    def _start(self):
        self.send("CALSPD")
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._status_lbl.config(text="Calibrating…", foreground="blue")
        self._cpm_lbl.config(text="N/A")
        self._disp_frame.pack_forget()
        self._waiting_disp = False
        _append_log(self._log, f"[{_ts()}] CALSPD started – firmware moving 7,247,700 counts.\n")

    def _reset_ui(self):
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")
        self._status_lbl.config(text="Stopped", foreground="red")
        self._disp_frame.pack_forget()
        self._waiting_disp = False

    def _submit_disp(self):
        try:
            d = float(self._disp_entry.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter a valid displacement value (mm).")
            return
        if d <= 0:
            messagebox.showwarning("Input Error", "Displacement must be > 0.")
            return
        self.send(f"{d:.1f}")
        expected = 7247700.0 / d
        _append_log(self._log, f"[{_ts()}] Submitted {d:.1f} mm → expected CPM ≈ {expected:.2f}\n")
        self._disp_entry.delete(0, tk.END)

    def handle_line(self, line: str):
        if ("Please enter the measured displacement" in line or
                "Calibration movement complete" in line):
            self._waiting_disp = True
            self._disp_frame.pack(fill=tk.X, padx=8, pady=5)
            self._status_lbl.config(text="Waiting for displacement input", foreground="orange")
            self._stop_btn.config(state="disabled")
            _append_log(self._log, f"[{_ts()}] {line}\n")
        elif line.startswith("New COUNTS_PER_MM:"):
            val = line.split(":")[1].strip().split()[0]
            self._cpm_lbl.config(text=f"{val} counts/mm")
            _append_log(self._log, f"[{_ts()}] {line}\n")
        elif "Calibration complete" in line:
            self._waiting_disp = False
            self._disp_frame.pack_forget()
            self._status_lbl.config(text="Calibration complete ✓", foreground="green")
            self._start_btn.config(state="normal")
            _append_log(self._log, f"[{_ts()}] Stored in EEPROM.\n")

    def on_connect(self):
        self._start_btn.config(state="normal")

    def on_disconnect(self):
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="disabled")
        self._disp_frame.pack_forget()
        self._waiting_disp = False


# ---------------------------------------------------------------------------
# Tab 4 – Position Calibration
# ---------------------------------------------------------------------------

class PosCalTab(_MCUTab):
    def __init__(self, parent, ctrl):
        super().__init__(parent, ctrl)
        self._active = False
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 5}

        # Step 1
        self._step1 = ttk.LabelFrame(self, text="Step 1 – Initiate Re-calibration", padding=8)
        self._step1.pack(fill=tk.X, **pad)
        ttk.Label(self._step1, text="⚠ This temporarily disables the inner endstop.\n"
                  "Jog to the desired home position, then confirm.",
                  foreground="darkorange", font=_F()["small_bold"]).pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(self._step1); row.pack(anchor="w")
        self._cont_btn = ttk.Button(row, text="Continue (Send RESETPOS)", state="disabled",
                                    command=self._continue)
        self._cont_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._cancel1_btn = ttk.Button(row, text="Cancel", state="disabled",
                                       command=self._cancel)
        self._cancel1_btn.pack(side=tk.LEFT)

        # Step 2 (hidden)
        self._step2 = ttk.LabelFrame(self, text="Step 2 – Jog to New Home Position", padding=8)
        ttk.Label(self._step2,
                  text="Endstop disabled. Use ▲/▼ to move to new home (position may go negative).",
                  wraplength=600).pack(anchor="w", pady=(0, 8))

        jog = ttk.Frame(self._step2); jog.pack(anchor="w", pady=(0, 8))
        self._jog_up = tk.Button(jog, text="▲  UP", width=10, height=2,
                                 font=_F()["btn_jog"], command=lambda: self.send("UP"))
        self._jog_up.pack(side=tk.LEFT, padx=(0, 6))
        self._jog_dn = tk.Button(jog, text="▼  DOWN", width=10, height=2,
                                 font=_F()["btn_jog"], command=lambda: self.send("DOWN"))
        self._jog_dn.pack(side=tk.LEFT, padx=(0, 6))
        self._jog_stop = tk.Button(jog, text="■  STOP", width=10, height=2,
                                   font=_F()["btn_jog"], bg="#ff4444", fg="white",
                                   command=lambda: self.send("STOP"))
        self._jog_stop.pack(side=tk.LEFT)

        pos_row = ttk.Frame(self._step2); pos_row.pack(anchor="w", pady=(0, 8))
        ttk.Label(pos_row, text="Current Position:", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 6))
        self._cur_pos = ttk.Label(pos_row, text="N/A", font=_F()["btn_large"], foreground="blue")
        self._cur_pos.pack(side=tk.LEFT)

        confirm = ttk.Frame(self._step2); confirm.pack(anchor="w")
        self._set_here_btn = tk.Button(confirm, text="✓  Set Home Here (CALPOS)",
                                       font=_F()["small_bold"], bg="#44bb44", fg="white",
                                       padx=10, pady=5, command=self._set_home_here)
        self._set_here_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(confirm, text="Cancel", command=self._cancel).pack(side=tk.LEFT)

        # Log
        gl = ttk.LabelFrame(self, text="Position Calibration Log", padding=8)
        gl.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = tk.Text(gl, height=8, state="disabled", background="#f5f5f5", font=_F()["mono"])
        self._log.pack(fill=tk.BOTH, expand=True)

    def _continue(self):
        self.send("RESETPOS")
        self._active = True
        self._step1.pack_forget()
        self._step2.pack(fill=tk.X, padx=8, pady=5)
        _append_log(self._log, f"[{_ts()}] RESETPOS sent – inner endstop disabled.\n")
        _append_log(self._log, f"[{_ts()}] Jog to new home position.\n")

    def _cancel(self):
        self.send("STOP")
        was_active = self._active
        self._active = False
        self._step2.pack_forget()
        self._step1.pack(fill=tk.X, padx=8, pady=5)
        if was_active:
            _append_log(self._log, f"[{_ts()}] Cancelled – WARNING: endstop may still be disabled until CALPOS.\n")
            messagebox.showwarning("Cancelled",
                                   "Home not set. Inner endstop remains disabled until you complete the procedure.")
        else:
            _append_log(self._log, f"[{_ts()}] Position calibration cancelled.\n")

    def _set_home_here(self):
        self.send("STOP")
        self.send("CALPOS")
        self._active = False
        self._step2.pack_forget()
        self._step1.pack(fill=tk.X, padx=8, pady=5)
        _append_log(self._log, f"[{_ts()}] CALPOS sent – new home set, endstop re-enabled.\n")

    def handle_line(self, line: str):
        if line.startswith("POSITION:") and self._active:
            self._cur_pos.config(text=line.split(":")[1].strip() + " counts")

    def on_connect(self):
        self._cont_btn.config(state="normal")
        self._cancel1_btn.config(state="normal")

    def on_disconnect(self):
        self._cont_btn.config(state="disabled")
        self._cancel1_btn.config(state="disabled")
        self._active = False
        self._step2.pack_forget()
        self._step1.pack(fill=tk.X, padx=8, pady=5)


# ---------------------------------------------------------------------------
# Tab 5 – Load Cell Calibration
# ---------------------------------------------------------------------------

class LoadCellCalTab(_MCUTab):
    def __init__(self, parent, ctrl):
        super().__init__(parent, ctrl)
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        # Step 1
        g1 = ttk.LabelFrame(self, text="Step 1 – Capture Point 1 (unloaded or known weight)", padding=8)
        g1.pack(fill=tk.X, **pad)
        ttk.Label(g1, text="Enter force currently on load cell (typically 0 N). Force must be ≥ 0.").pack(anchor="w")
        r1 = ttk.Frame(g1); r1.pack(anchor="w", pady=(6, 0))
        ttk.Label(r1, text="Force 1 (N):").pack(side=tk.LEFT)
        self._f1_entry = ttk.Entry(r1, width=10); self._f1_entry.insert(0, "0.0"); self._f1_entry.pack(side=tk.LEFT, padx=5)
        self._cap1_btn = ttk.Button(r1, text="Capture Point 1", state="disabled", command=self._capture1)
        self._cap1_btn.pack(side=tk.LEFT, padx=5)
        r1s = ttk.Frame(g1); r1s.pack(anchor="w", pady=(4, 0))
        ttk.Label(r1s, text="ADC 1:", font=_F()["small_bold"]).pack(side=tk.LEFT)
        self._adc1_lbl = ttk.Label(r1s, text="—", font=_F()["value_lg"], foreground="blue"); self._adc1_lbl.pack(side=tk.LEFT, padx=6)
        self._p1_status = ttk.Label(r1s, text="", font=_F()["small_bold"]); self._p1_status.pack(side=tk.LEFT)

        # Step 2
        g2 = ttk.LabelFrame(self, text="Step 2 – Capture Point 2 & Apply (heavier known weight)", padding=8)
        g2.pack(fill=tk.X, **pad)
        ttk.Label(g2, text="Apply second reference load. Force 2 must be greater than Force 1.").pack(anchor="w")
        r2 = ttk.Frame(g2); r2.pack(anchor="w", pady=(6, 0))
        ttk.Label(r2, text="Force 2 (N):").pack(side=tk.LEFT)
        self._f2_entry = ttk.Entry(r2, width=10); self._f2_entry.pack(side=tk.LEFT, padx=5)
        self._cap2_btn = ttk.Button(r2, text="Capture Point 2 & Apply", state="disabled", command=self._capture2)
        self._cap2_btn.pack(side=tk.LEFT, padx=5)
        r2s = ttk.Frame(g2); r2s.pack(anchor="w", pady=(4, 0))
        ttk.Label(r2s, text="ADC 2:", font=_F()["small_bold"]).pack(side=tk.LEFT)
        self._adc2_lbl = ttk.Label(r2s, text="—", font=_F()["value_lg"], foreground="blue"); self._adc2_lbl.pack(side=tk.LEFT, padx=6)
        self._p2_status = ttk.Label(r2s, text="", font=_F()["small_bold"]); self._p2_status.pack(side=tk.LEFT)

        # Results
        gr = ttk.LabelFrame(self, text="Calibration Results", padding=8)
        gr.pack(fill=tk.X, **pad)
        rr = ttk.Frame(gr); rr.pack(fill=tk.X)
        ttk.Label(rr, text="TARE ADC:", font=_F()["small_bold"]).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._tare_lbl = ttk.Label(rr, text="—", font=_F()["value_lg"]); self._tare_lbl.grid(row=0, column=1, sticky="w", padx=(0, 30))
        ttk.Label(rr, text="ADC / Newton (APN):", font=_F()["small_bold"]).grid(row=0, column=2, sticky="w", padx=(0, 6))
        self._apn_lbl = ttk.Label(rr, text="—", font=_F()["value_lg"], foreground="green"); self._apn_lbl.grid(row=0, column=3, sticky="w")

        # Quick single-point cal
        gq = ttk.LabelFrame(self, text="Quick Cal – Fixed Tare (0 N = ADC 50)", padding=8)
        gq.pack(fill=tk.X, **pad)
        ttk.Label(gq, text="Apply known force. Tare (0 N = ADC 50) is hardcoded.").pack(anchor="w")
        rq = ttk.Frame(gq); rq.pack(anchor="w", pady=(4, 0))
        ttk.Label(rq, text="Force (N):").pack(side=tk.LEFT)
        self._sq_force = ttk.Entry(rq, width=8); self._sq_force.insert(0, "251"); self._sq_force.pack(side=tk.LEFT, padx=4)
        self._sq_btn = ttk.Button(rq, text="Capture & Apply", state="disabled", command=self._single)
        self._sq_btn.pack(side=tk.LEFT, padx=4)
        ttk.Label(rq, text="ADC:").pack(side=tk.LEFT, padx=(8, 2))
        self._sq_adc = ttk.Label(rq, text="—", font=_F()["value_lg"], foreground="blue"); self._sq_adc.pack(side=tk.LEFT, padx=(0, 8))
        self._sq_status = ttk.Label(rq, text="", font=_F()["small_bold"]); self._sq_status.pack(side=tk.LEFT)

        # Log
        gl = ttk.LabelFrame(self, text="Load Cell Calibration Log", padding=8)
        gl.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = tk.Text(gl, height=7, state="disabled", background="#f5f5f5", font=_F()["mono"])
        self._log.pack(fill=tk.BOTH, expand=True)

    def _capture1(self):
        try:
            f = float(self._f1_entry.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter a valid force value (N)."); return
        if f < 0:
            messagebox.showwarning("Input Error", "Force must be ≥ 0 N."); return
        self.send(f"CALLOAD1 {f:.1f}")
        self._p1_status.config(text="Capturing…", foreground="orange")
        self._adc1_lbl.config(text="—")
        self._adc2_lbl.config(text="—"); self._tare_lbl.config(text="—"); self._apn_lbl.config(text="—")
        self._p2_status.config(text=""); self._cap2_btn.config(state="disabled")
        _append_log(self._log, f"[{_ts()}] Sent CALLOAD1 {f:.1f} N\n")

    def _capture2(self):
        try:
            f = float(self._f2_entry.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter a valid force value (N)."); return
        if f < 0:
            messagebox.showwarning("Input Error", "Force must be ≥ 0 N."); return
        self.send(f"CALLOAD2 {f:.1f}")
        self._p2_status.config(text="Capturing & computing…", foreground="orange")
        self._adc2_lbl.config(text="—")
        _append_log(self._log, f"[{_ts()}] Sent CALLOAD2 {f:.1f} N\n")

    def _single(self):
        try:
            f = float(self._sq_force.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter a valid force value (N)."); return
        if f <= 0:
            messagebox.showwarning("Input Error", "Force must be > 0 N."); return
        self.send(f"CALLOAD_SINGLE {f:.1f}")
        self._sq_status.config(text="Capturing…", foreground="orange")
        self._sq_adc.config(text="—")
        _append_log(self._log, f"[{_ts()}] Sent CALLOAD_SINGLE {f:.1f} N (tare=50)\n")

    def handle_line(self, line: str):
        if line.startswith("CALLOAD1:OK"):
            adc = _parse_field(line, "ADC"); f = _parse_field(line, "F")
            self._adc1_lbl.config(text=adc)
            self._p1_status.config(text="Point 1 captured ✓", foreground="green")
            self._cap2_btn.config(state="normal")
            _append_log(self._log, f"[{_ts()}] Pt1: F={f} N, ADC={adc}\n")
        elif line.startswith("CALLOAD2:OK"):
            adc = _parse_field(line, "ADC"); f = _parse_field(line, "F")
            tare = _parse_field(line, "TARE"); apn = _parse_field(line, "APN")
            self._adc2_lbl.config(text=adc)
            self._tare_lbl.config(text=tare); self._apn_lbl.config(text=apn)
            self._p2_status.config(text="Calibration saved to EEPROM ✓", foreground="green")
            self._cap2_btn.config(state="disabled")
            _append_log(self._log, f"[{_ts()}] Pt2: F={f} N, ADC={adc} → TARE={tare}, APN={apn}\n")
        elif line.startswith("CALLOAD_SINGLE:OK"):
            adc = _parse_field(line, "ADC"); apn = _parse_field(line, "APN")
            self._sq_adc.config(text=adc)
            self._sq_status.config(text=f"Applied: TARE=50, APN={apn} ✓", foreground="green")
            _append_log(self._log, f"[{_ts()}] Single cal: ADC={adc}, TARE=50, APN={apn}\n")
        elif line.startswith("ERR:CALLOAD_ORDER"):
            self._p2_status.config(text="Error: capture Pt1 first, or F2 must be > F1", foreground="red")
            _append_log(self._log, f"[{_ts()}] {line}\n")
        elif line.startswith("ERR:EEPROM"):
            self._p2_status.config(text="Applied but EEPROM save failed", foreground="orange")
            _append_log(self._log, f"[{_ts()}] WARNING: {line}\n")
        elif line.startswith("ERR:CALLOAD_SINGLE"):
            self._sq_status.config(text="Error – check force value or ADC", foreground="red")
            _append_log(self._log, f"[{_ts()}] {line}\n")

    def on_connect(self):
        self._cap1_btn.config(state="normal")
        self._sq_btn.config(state="normal")

    def on_disconnect(self):
        self._cap1_btn.config(state="disabled")
        self._cap2_btn.config(state="disabled")
        self._sq_btn.config(state="disabled")


# ---------------------------------------------------------------------------
# Tab 6 – PID Tuning
# ---------------------------------------------------------------------------

class PIDTuningTab(_MCUTab):
    def __init__(self, parent, ctrl):
        super().__init__(parent, ctrl)
        self._spd_streaming = False
        self._spd_rows: list[str] = []  # cached CSV data
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        # Current params
        gc = ttk.LabelFrame(self, text="Current Parameters (from MCU)", padding=8)
        gc.pack(fill=tk.X, **pad)
        cr = ttk.Frame(gc); cr.pack(fill=tk.X, pady=(0, 6))
        for col, (lbl, attr) in enumerate([("Kp:", "_kp_lbl"), ("Ki:", "_ki_lbl"), ("Kd:", "_kd_lbl")]):
            ttk.Label(cr, text=lbl, font=_F()["small_bold"]).grid(row=0, column=col*2, sticky="w", padx=(0, 4))
            lbl_w = ttk.Label(cr, text="—", width=10); lbl_w.grid(row=0, column=col*2+1, sticky="w", padx=(0, 20))
            setattr(self, attr, lbl_w)
        fr = ttk.Frame(gc); fr.pack(anchor="w")
        ttk.Label(fr, text="Loop frequency:", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 4))
        self._freq_lbl = ttk.Label(fr, text="—"); self._freq_lbl.pack(side=tk.LEFT, padx=(0, 12))
        self._refresh_btn = ttk.Button(fr, text="Refresh (GET_PID)", state="disabled",
                                       command=lambda: self.send("GET_PID"))
        self._refresh_btn.pack(side=tk.LEFT)

        # Set new params
        gs = ttk.LabelFrame(self, text="Set New Parameters", padding=8)
        gs.pack(fill=tk.X, **pad)
        pr = ttk.Frame(gs); pr.pack(anchor="w", pady=(0, 6))
        for i, (lbl, attr, default) in enumerate([("Kp:", "_kp_e", "0.001"), ("Ki:", "_ki_e", "0.0005"), ("Kd:", "_kd_e", "0.0")]):
            ttk.Label(pr, text=lbl).grid(row=0, column=i*3, sticky="w", padx=(i*4, 2))
            e = ttk.Entry(pr, width=10); e.insert(0, default); e.grid(row=0, column=i*3+1, padx=(0, 16))
            setattr(self, attr, e)
        arow = ttk.Frame(gs); arow.pack(anchor="w", pady=(0, 6))
        self._apply_btn = ttk.Button(arow, text="Apply PID", state="disabled", command=self._apply_pid)
        self._apply_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._apply_status = ttk.Label(arow, text="", font=_F()["small_bold"]); self._apply_status.pack(side=tk.LEFT)

        frow = ttk.Frame(gs); frow.pack(anchor="w")
        ttk.Label(frow, text="Loop frequency:").pack(side=tk.LEFT)
        self._freq_e = ttk.Entry(frow, width=6); self._freq_e.insert(0, "50"); self._freq_e.pack(side=tk.LEFT, padx=4)
        ttk.Label(frow, text="Hz").pack(side=tk.LEFT, padx=(0, 8))
        self._freq_btn = ttk.Button(frow, text="Apply Frequency", state="disabled", command=self._apply_freq)
        self._freq_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._freq_status = ttk.Label(frow, text="", font=_F()["small_bold"]); self._freq_status.pack(side=tk.LEFT)

        # Speed stream
        gst = ttk.LabelFrame(self, text="Speed Stream", padding=8)
        gst.pack(fill=tk.X, **pad)
        sr = ttk.Frame(gst); sr.pack(anchor="w")
        ttk.Label(sr, text="Stream:", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 4))
        self._stream_ind = _indicator(sr, "gray"); self._stream_ind.pack(side=tk.LEFT, padx=(0, 2))
        self._stream_status = ttk.Label(sr, text="Stopped"); self._stream_status.pack(side=tk.LEFT, padx=(0, 12))
        self._stream_start = ttk.Button(sr, text="Start Stream", state="disabled",
                                        command=lambda: self.send("SPDSTREAM 1"))
        self._stream_start.pack(side=tk.LEFT, padx=(0, 6))
        self._stream_stop = ttk.Button(sr, text="Stop Stream", state="disabled",
                                       command=lambda: self.send("SPDSTREAM 0"))
        self._stream_stop.pack(side=tk.LEFT)

        # Manual PI control
        gpi = ttk.LabelFrame(self, text="Manual PI Control", padding=8)
        gpi.pack(fill=tk.X, **pad)
        pir = ttk.Frame(gpi); pir.pack(anchor="w")
        ttk.Label(pir, text="Speed Setpoint (mm/s):").pack(side=tk.LEFT)
        self._pi_spd = ttk.Entry(pir, width=6); self._pi_spd.insert(0, "5"); self._pi_spd.pack(side=tk.LEFT, padx=4)
        self._pi_set_btn = ttk.Button(pir, text="Set Speed", state="disabled",
                                      command=self._set_pi_speed)
        self._pi_set_btn.pack(side=tk.LEFT, padx=(0, 16))
        self._pi_up = ttk.Button(pir, text="PI UP (Retract)", state="disabled",
                                 command=lambda: self.send("PI_UP"))
        self._pi_up.pack(side=tk.LEFT, padx=(0, 6))
        self._pi_dn = ttk.Button(pir, text="PI DOWN (Extend)", state="disabled",
                                 command=lambda: self.send("PI_DOWN"))
        self._pi_dn.pack(side=tk.LEFT, padx=(0, 6))
        self._pi_stop = ttk.Button(pir, text="STOP", state="disabled",
                                   command=lambda: self.send("STOP"))
        self._pi_stop.pack(side=tk.LEFT)

        # Speed data table
        gt = ttk.LabelFrame(self, text="Speed Data", padding=8)
        gt.pack(fill=tk.BOTH, expand=True, **pad)
        cols = ("tick", "speed", "setpoint", "error", "duty", "pos")
        hdrs = ("Time (ms)", "Speed (cps)", "Setpoint (cps)", "Error", "Duty", "Pos (mm)")
        self._tree = ttk.Treeview(gt, columns=cols, show="headings", height=8)
        for col, hdr in zip(cols, hdrs):
            self._tree.heading(col, text=hdr)
            self._tree.column(col, width=90, anchor="e")
        vsb = ttk.Scrollbar(gt, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        eb = ttk.Frame(self); eb.pack(anchor="w", padx=8, pady=(0, 6))
        ttk.Button(eb, text="Export CSV", command=self._export_csv).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(eb, text="Clear Data", command=self._clear_data).pack(side=tk.LEFT)

    def _apply_pid(self):
        try:
            kp = float(self._kp_e.get())
            ki = float(self._ki_e.get())
            kd = float(self._kd_e.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter valid numeric values for Kp, Ki, Kd."); return
        if any(v < 0 for v in (kp, ki, kd)):
            messagebox.showwarning("Input Error", "All gains must be ≥ 0."); return
        self.send(f"SET_PID KP:{kp} KI:{ki} KD:{kd}")
        self._apply_status.config(text="Sending…", foreground="orange")

    def _apply_freq(self):
        try:
            hz = int(self._freq_e.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter a valid integer for frequency."); return
        if not (1 <= hz <= 200):
            messagebox.showwarning("Input Error", "Frequency must be 1–200 Hz."); return
        self.send(f"SET_LOOPFREQ {hz}")
        self._freq_status.config(text="Sending…", foreground="orange")

    def _set_pi_speed(self):
        try:
            spd = float(self._pi_spd.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Invalid speed value."); return
        self.send(f"SETSPD {spd:.1f}")

    def _clear_data(self):
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._spd_rows.clear()

    def _export_csv(self):
        if not self._spd_rows:
            messagebox.showinfo("Export CSV", "No data to export."); return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"speed_data_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
        if path:
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Time_ms", "Speed_cps", "Setpoint_cps", "Error_cps", "Duty", "Pos_mm"])
                for row in self._spd_rows:
                    w.writerow(row)

    def handle_line(self, line: str):
        if line.startswith("PID:KP:"):
            self._kp_lbl.config(text=_parse_field(line, "KP"))
            self._ki_lbl.config(text=_parse_field(line, "KI"))
            self._kd_lbl.config(text=_parse_field(line, "KD"))
            period = _parse_field(line, "PERIOD")
            try:
                ms = int(period)
                self._freq_lbl.config(text=f"{1000 // ms} Hz (period: {ms} ms)")
            except (ValueError, ZeroDivisionError):
                self._freq_lbl.config(text=f"{period} ms")
        elif line.startswith("PID:OK:"):
            self._kp_lbl.config(text=_parse_field(line, "KP"))
            self._ki_lbl.config(text=_parse_field(line, "KI"))
            self._kd_lbl.config(text=_parse_field(line, "KD"))
            self._apply_status.config(text="Applied ✓", foreground="green")
        elif line.startswith("ERR:SET_PID:RANGE"):
            self._apply_status.config(text="Value out of range", foreground="red")
        elif line.startswith("LOOPFREQ:OK:"):
            hz = _parse_field(line, "OK").replace("Hz", "")
            ms = _parse_field(line, "PERIOD").replace("ms", "")
            self._freq_lbl.config(text=f"{hz} Hz (period: {ms} ms)")
            self._freq_status.config(text="Applied ✓", foreground="green")
        elif line.startswith("ERR:SET_LOOPFREQ:RANGE"):
            self._freq_status.config(text="1–200 Hz only", foreground="red")
        elif line == "SPDSTREAM:ON":
            self._spd_streaming = True
            self._stream_ind.config(fg="green")
            self._stream_status.config(text="Streaming", foreground="green")
            self._stream_start.config(state="disabled")
            self._stream_stop.config(state="normal")
        elif line == "SPDSTREAM:OFF":
            self._spd_streaming = False
            self._stream_ind.config(fg="gray")
            self._stream_status.config(text="Stopped", foreground="black")
            self._stream_start.config(state="normal")
            self._stream_stop.config(state="disabled")
        elif line.startswith("SS:"):
            parts = line[3:].split(",")
            if len(parts) >= 5:
                pos_mm = "?"
                if len(parts) >= 6:
                    try:
                        pos_mm = f"{int(parts[5]) / 100:.2f}"
                    except ValueError:
                        pass
                row = (parts[0], parts[1], parts[2], parts[3], parts[4], pos_mm)
                self._spd_rows.append(row)
                if len(self._spd_rows) > 500:
                    self._spd_rows.pop(0)
                    if self._tree.get_children():
                        self._tree.delete(self._tree.get_children()[0])
                iid = self._tree.insert("", tk.END, values=row)
                self._tree.see(iid)

    def on_connect(self):
        self._refresh_btn.config(state="normal")
        self._apply_btn.config(state="normal")
        self._freq_btn.config(state="normal")
        self._stream_start.config(state="normal")
        self._pi_set_btn.config(state="normal")
        self._pi_up.config(state="normal")
        self._pi_dn.config(state="normal")
        self._pi_stop.config(state="normal")

    def on_disconnect(self):
        for btn in (self._refresh_btn, self._apply_btn, self._freq_btn,
                    self._stream_start, self._stream_stop,
                    self._pi_set_btn, self._pi_up, self._pi_dn, self._pi_stop):
            btn.config(state="disabled")
        self._spd_streaming = False
        self._stream_ind.config(fg="gray")
        self._stream_status.config(text="Stopped", foreground="black")


# ---------------------------------------------------------------------------
# Tab 7 – Diagnostics
# ---------------------------------------------------------------------------

class DiagnosticsTab(_MCUTab):
    def __init__(self, parent, ctrl):
        super().__init__(parent, ctrl)
        self._ping_after: str | None = None
        self._as5600_after: str | None = None
        self._eeprom_after: str | None = None
        self._run_all_active = False
        self._run_all_step = 0
        self._adc_streaming = False
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        ttk.Label(self, text="⚠  Not for normal operation – commissioning and fault-finding only.",
                  foreground="darkorange", font=_F()["small_bold"]).pack(anchor="w", padx=8, pady=(8, 4))

        # 1. UART
        g1 = ttk.LabelFrame(self, text="1. UART Communication", padding=8)
        g1.pack(fill=tk.X, **pad)
        pr = ttk.Frame(g1); pr.pack(anchor="w", pady=(0, 6))
        ttk.Label(pr, text="Status:", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 4))
        self._ping_ind = _indicator(pr, "gray"); self._ping_ind.pack(side=tk.LEFT, padx=(0, 2))
        self._ping_status = ttk.Label(pr, text="Waiting"); self._ping_status.pack(side=tk.LEFT)
        self._ping_btn = ttk.Button(g1, text="Ping (PING)", state="disabled", command=self._ping)
        self._ping_btn.pack(anchor="w")

        # 2. AS5600
        g2 = ttk.LabelFrame(self, text="2. AS5600 Magnetic Encoder (I2C1, 0x36, SDA=PB7 SCL=PB6)", padding=8)
        g2.pack(fill=tk.X, **pad)
        ar = ttk.Frame(g2); ar.pack(fill=tk.X, pady=(0, 6))
        # row 0
        ttk.Label(ar, text="Magnet:", font=_F()["small_bold"]).grid(row=0, column=0, sticky="w", padx=(0, 4))
        self._mag_ind = _indicator(ar, "gray"); self._mag_ind.grid(row=0, column=1, padx=(0, 2))
        self._mag_status = ttk.Label(ar, text="—"); self._mag_status.grid(row=0, column=2, sticky="w", padx=(0, 30))
        ttk.Label(ar, text="AGC:", font=_F()["small_bold"]).grid(row=0, column=3, sticky="w", padx=(0, 4))
        self._agc_lbl = ttk.Label(ar, text="—"); self._agc_lbl.grid(row=0, column=4, sticky="w")
        # row 1
        ttk.Label(ar, text="Status:", font=_F()["small_bold"]).grid(row=1, column=0, sticky="w", padx=(0, 4))
        self._as_status = ttk.Label(ar, text="—"); self._as_status.grid(row=1, column=2, sticky="w")
        ttk.Label(ar, text="Raw:", font=_F()["small_bold"]).grid(row=1, column=3, sticky="w", padx=(0, 4))
        self._raw_lbl = ttk.Label(ar, text="—"); self._raw_lbl.grid(row=1, column=4, sticky="w", padx=(0, 16))
        ttk.Label(ar, text="Angle:", font=_F()["small_bold"]).grid(row=1, column=5, sticky="w", padx=(0, 4))
        self._angle_lbl = ttk.Label(ar, text="—"); self._angle_lbl.grid(row=1, column=6, sticky="w")
        self._as5600_btn = ttk.Button(g2, text="Test AS5600", state="disabled", command=self._test_as5600)
        self._as5600_btn.pack(anchor="w")

        # 3. EEPROM
        g3 = ttk.LabelFrame(self, text="3. AT24C256N EEPROM (I2C1, 0x50)", padding=8)
        g3.pack(fill=tk.X, **pad)
        er = ttk.Frame(g3); er.pack(anchor="w", pady=(0, 6))
        ttk.Label(er, text="Write/Read test:", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 4))
        self._ee_ind = _indicator(er, "gray"); self._ee_ind.pack(side=tk.LEFT, padx=(0, 2))
        self._ee_status = ttk.Label(er, text="—"); self._ee_status.pack(side=tk.LEFT)
        self._eeprom_btn = ttk.Button(g3, text="Test EEPROM", state="disabled", command=self._test_eeprom)
        self._eeprom_btn.pack(anchor="w")

        # 4. Calibration values
        g4 = ttk.LabelFrame(self, text="4. Stored Calibration Values", padding=8)
        g4.pack(fill=tk.X, **pad)
        cv = ttk.Frame(g4); cv.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(cv, text="COUNTS_PER_MM:", font=_F()["small_bold"]).grid(row=0, column=0, sticky="w", padx=(0, 4))
        self._cal_cpm = ttk.Label(cv, text="—"); self._cal_cpm.grid(row=0, column=1, sticky="w", padx=(0, 30))
        ttk.Label(cv, text="Load cell tare:", font=_F()["small_bold"]).grid(row=0, column=2, sticky="w", padx=(0, 4))
        self._cal_tare = ttk.Label(cv, text="—"); self._cal_tare.grid(row=0, column=3, sticky="w", padx=(0, 30))
        ttk.Label(cv, text="ADC/Newton:", font=_F()["small_bold"]).grid(row=0, column=4, sticky="w", padx=(0, 4))
        self._cal_apn = ttk.Label(cv, text="—"); self._cal_apn.grid(row=0, column=5, sticky="w")
        self._read_cal_btn = ttk.Button(g4, text="Read Calibration (READ_CAL)", state="disabled",
                                        command=self._read_cal)
        self._read_cal_btn.pack(anchor="w")

        # 5. Raw ADC
        g5 = ttk.LabelFrame(self, text="5. Raw ADC Values", padding=8)
        g5.pack(fill=tk.X, **pad)
        adcr = ttk.Frame(g5); adcr.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(adcr, text="Current sense (PA0):", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 4))
        self._adc_curr = ttk.Label(adcr, text="—", font=_F()["value_lg"]); self._adc_curr.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(adcr, text="/ 4095", foreground="gray").pack(side=tk.LEFT, padx=(0, 20))
        ttk.Label(adcr, text="Load cell (PA1):", font=_F()["small_bold"]).pack(side=tk.LEFT, padx=(0, 4))
        self._adc_load = ttk.Label(adcr, text="—", font=_F()["value_lg"]); self._adc_load.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(adcr, text="/ 4095", foreground="gray").pack(side=tk.LEFT)

        adc_ctl = ttk.Frame(g5); adc_ctl.pack(anchor="w")
        self._adc_snap_btn = ttk.Button(adc_ctl, text="Snapshot", state="disabled",
                                        command=lambda: self.send("GET_ADC"))
        self._adc_snap_btn.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(adc_ctl, text="Stream rate:").pack(side=tk.LEFT)
        self._adc_rate = ttk.Entry(adc_ctl, width=6); self._adc_rate.insert(0, "100"); self._adc_rate.pack(side=tk.LEFT, padx=4)
        ttk.Label(adc_ctl, text="ms").pack(side=tk.LEFT, padx=(0, 8))
        self._adc_start_btn = ttk.Button(adc_ctl, text="Start Stream", state="disabled",
                                         command=self._adc_stream_start)
        self._adc_start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._adc_stop_btn = ttk.Button(adc_ctl, text="Stop Stream", state="disabled",
                                        command=lambda: self.send("ADCSTREAM 0"))
        self._adc_stop_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._adc_ind = _indicator(adc_ctl, "gray"); self._adc_ind.pack(side=tk.LEFT, padx=(0, 2))
        self._adc_stream_status = ttk.Label(adc_ctl, text=""); self._adc_stream_status.pack(side=tk.LEFT)

        # Run all + raw log
        bot = ttk.Frame(self); bot.pack(fill=tk.BOTH, expand=True, **pad)
        run_row = ttk.Frame(bot); run_row.pack(anchor="w", pady=(0, 4))
        self._run_all_btn = ttk.Button(run_row, text="Run All Tests", state="disabled",
                                       command=self._run_all)
        self._run_all_btn.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(run_row, text="Raw serial (last 2 kB):", foreground="gray").pack(side=tk.LEFT)
        self._raw_log = tk.Text(bot, height=5, state="disabled", background="#f8f8f8",
                                font=_F()["mono"])
        self._raw_log.pack(fill=tk.BOTH, expand=True)

    # ── Commands ──────────────────────────────────────────────────────────────

    def _cancel_timer(self, attr: str):
        aid = getattr(self, attr)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        setattr(self, attr, None)

    def _ping(self):
        self._cancel_timer("_ping_after")
        self._ping_ind.config(fg="orange")
        self._ping_status.config(text="Pinging…", foreground="orange")
        self.send("PING")
        self._ping_after = self.after(500, self._ping_timeout)

    def _ping_timeout(self):
        self._ping_after = None
        self._ping_ind.config(fg="red")
        self._ping_status.config(text="No response – check COM port / USB cable", foreground="red")
        if self._run_all_active:
            self._run_all_active = False

    def _test_as5600(self):
        self._cancel_timer("_as5600_after")
        self._mag_ind.config(fg="orange"); self._mag_status.config(text="Testing…", foreground="orange")
        self._agc_lbl.config(text="—"); self._raw_lbl.config(text="—"); self._angle_lbl.config(text="—")
        self.send("TEST_AS5600")
        self._as5600_after = self.after(2000, self._as5600_timeout)

    def _as5600_timeout(self):
        self._as5600_after = None
        self._mag_ind.config(fg="red")
        self._mag_status.config(text="No response – firmware timeout", foreground="red")
        if self._run_all_active:
            self._run_all_active = False

    def _test_eeprom(self):
        self._cancel_timer("_eeprom_after")
        self._ee_ind.config(fg="orange"); self._ee_status.config(text="Testing…", foreground="orange")
        self.send("TEST_EEPROM")
        self._eeprom_after = self.after(2000, self._eeprom_timeout)

    def _eeprom_timeout(self):
        self._eeprom_after = None
        self._ee_ind.config(fg="red")
        self._ee_status.config(text="No response – firmware timeout", foreground="red")
        if self._run_all_active:
            self._run_all_active = False

    def _read_cal(self):
        self._cal_cpm.config(text="—"); self._cal_tare.config(text="—"); self._cal_apn.config(text="—")
        self.send("READ_CAL")

    def _adc_stream_start(self):
        try:
            rate = int(self._adc_rate.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Enter a valid integer for stream rate."); return
        if not (10 <= rate <= 10000):
            messagebox.showwarning("Input Error", "Stream rate must be 10–10000 ms."); return
        self.send(f"ADCSTREAM {rate}")
        self._adc_ind.config(fg="orange"); self._adc_stream_status.config(text="Starting…", foreground="orange")

    def _run_all(self):
        self._run_all_active = True
        self._run_all_step = 0
        # Reset indicators
        for ind in (self._ping_ind, self._mag_ind, self._ee_ind):
            ind.config(fg="gray")
        self._ping_status.config(text="Waiting", foreground="black")
        self._mag_status.config(text="—", foreground="black")
        self._ee_status.config(text="—", foreground="black")
        self._cal_cpm.config(text="—"); self._cal_tare.config(text="—"); self._cal_apn.config(text="—")
        self._run_all_next()

    def _run_all_next(self):
        self._run_all_step += 1
        self.after(300, self._run_all_dispatch)

    def _run_all_dispatch(self):
        if not self._run_all_active:
            return
        step = self._run_all_step
        if step == 1:
            self._ping()
        elif step == 2:
            self._test_as5600()
        elif step == 3:
            self._test_eeprom()
        elif step == 4:
            self._read_cal()
        else:
            self._run_all_active = False

    # ── Response handling ─────────────────────────────────────────────────────

    def handle_line(self, line: str):
        # Always append to raw log
        _append_log(self._raw_log, line + "\n", max_chars=2000)

        if line == "PONG":
            self._cancel_timer("_ping_after")
            self._ping_ind.config(fg="green")
            self._ping_status.config(text="Connected ✓", foreground="green")
            # Unlock subsequent diag buttons
            for btn in (self._as5600_btn, self._eeprom_btn, self._read_cal_btn, self._run_all_btn):
                btn.config(state="normal")
            if self._run_all_active:
                self._run_all_next()

        elif line.startswith("TEST_AS5600:OK"):
            self._cancel_timer("_as5600_after")
            md = _parse_field(line, "MD"); ml = _parse_field(line, "ML"); mh = _parse_field(line, "MH")
            agc = _parse_field(line, "AGC"); raw = _parse_field(line, "RAW"); angle = _parse_field(line, "ANGLE")
            status = _parse_field(line, "STATUS")
            self._as_status.config(text=status)
            if md == "1" and ml == "0" and mh == "0":
                self._mag_ind.config(fg="green"); self._mag_status.config(text="Detected ✓", foreground="green")
            elif ml == "1":
                self._mag_ind.config(fg="orange"); self._mag_status.config(text="Too weak", foreground="orange")
            elif mh == "1":
                self._mag_ind.config(fg="orange"); self._mag_status.config(text="Too strong", foreground="orange")
            else:
                self._mag_ind.config(fg="red"); self._mag_status.config(text="Not found", foreground="red")
            try:
                agc_v = int(agc)
                self._agc_lbl.config(text=f"{agc_v} ⚠ check magnet dist." if agc_v > 200 else str(agc_v),
                                     foreground="orange" if agc_v > 200 else "black")
            except ValueError:
                self._agc_lbl.config(text=agc)
            try:
                self._raw_lbl.config(text=f"{raw} ({int(raw) * 360 / 4096:.1f}°)")
            except (ValueError, TypeError):
                self._raw_lbl.config(text=raw)
            try:
                self._angle_lbl.config(text=f"{angle} ({int(angle) * 360 / 4096:.1f}°)")
            except (ValueError, TypeError):
                self._angle_lbl.config(text=angle)
            if self._run_all_active:
                self._run_all_next()

        elif line.startswith("TEST_AS5600:ERR"):
            self._cancel_timer("_as5600_after")
            self._mag_ind.config(fg="red")
            self._mag_status.config(text="I2C ERROR – check wiring (SDA=PB7, SCL=PB6)", foreground="red")
            self._run_all_active = False

        elif line.startswith("TEST_EEPROM:OK"):
            self._cancel_timer("_eeprom_after")
            self._ee_ind.config(fg="green"); self._ee_status.config(text="PASS ✓", foreground="green")
            if self._run_all_active:
                self._run_all_next()

        elif line.startswith("TEST_EEPROM:ERR"):
            self._cancel_timer("_eeprom_after")
            detail = "write error" if "WRITE" in line else "read error" if "READ" in line else "verify error"
            self._ee_ind.config(fg="red"); self._ee_status.config(text=f"FAIL – {detail}", foreground="red")
            self._run_all_active = False

        elif line.startswith("CAL:"):
            cpm = _parse_field(line, "CPM"); tare = _parse_field(line, "TARE"); apn = _parse_field(line, "APN")
            self._cal_cpm.config(text=cpm if cpm != "0" else f"{cpm} ⚠ not calibrated – run CALSPD",
                                 foreground="orange" if cpm == "0" else "black")
            self._cal_tare.config(text=tare)
            default_apn = apn in ("4.18", "4.1800")
            self._cal_apn.config(text=apn if not default_apn else f"{apn} ⚠ default – run CALLOAD",
                                 foreground="orange" if default_apn else "black")
            if self._run_all_active:
                self._run_all_active = False  # last step

        elif line.startswith("ADC:CURR:"):
            self._adc_curr.config(text=_parse_field(line, "CURR"))
            self._adc_load.config(text=_parse_field(line, "LOAD"))

        elif line.startswith("ADCSTREAM:ON"):
            self._adc_streaming = True
            self._adc_ind.config(fg="green"); self._adc_stream_status.config(text="Streaming", foreground="green")
            self._adc_start_btn.config(state="disabled"); self._adc_stop_btn.config(state="normal")

        elif line == "ADCSTREAM:OFF":
            self._adc_streaming = False
            self._adc_ind.config(fg="gray"); self._adc_stream_status.config(text="", foreground="black")
            self._adc_start_btn.config(state="normal"); self._adc_stop_btn.config(state="disabled")

        elif line.startswith("ERR:ADCSTREAM:RANGE"):
            self._adc_stream_status.config(text="10–10000 ms only", foreground="red")

        elif line.startswith("AS:"):
            parts = line[3:].split(",")
            if len(parts) >= 3:
                self._adc_curr.config(text=parts[1])
                self._adc_load.config(text=parts[2])

    def on_connect(self):
        self._ping_btn.config(state="normal")
        self._adc_snap_btn.config(state="normal")
        self._adc_start_btn.config(state="normal")
        # AS5600, EEPROM, READ_CAL and Run All unlock only after successful PING

    def on_disconnect(self):
        for btn in (self._ping_btn, self._as5600_btn, self._eeprom_btn,
                    self._read_cal_btn, self._run_all_btn,
                    self._adc_snap_btn, self._adc_start_btn, self._adc_stop_btn):
            btn.config(state="disabled")
        self._run_all_active = False
        self._adc_streaming = False
        self._adc_ind.config(fg="gray"); self._adc_stream_status.config(text="")
        for timer in ("_ping_after", "_as5600_after", "_eeprom_after"):
            self._cancel_timer(timer)
