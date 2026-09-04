"""Dedicated CN0582 settings page for live board configuration."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from HelpFunctions.cn0582 import GAINS

from .config import CN0582_LOADCELL_CHANNEL

# What each channel is written to the parquet at. "Full" keeps the acquisition
# rate; the rest divide 256 kHz exactly, as decimation needs an integer factor.
STORE_RATE_LABELS = (("Full", 0), ("32 kHz", 32000), ("8 kHz", 8000), ("1 kHz", 1000))
STORE_RATE_BY_LABEL = {label: hz for label, hz in STORE_RATE_LABELS}
STORE_RATE_BY_HZ = {hz: label for label, hz in STORE_RATE_LABELS}


def _chk(parent, var, cmd, text=""):
    cb = ttk.Checkbutton(parent, text=text, variable=var, command=cmd)
    cb.state(["!alternate"])
    return cb


class CN0582SettingsTab:
    def __init__(self, notebook, root, daq, daq_available):
        self.root = root
        self.daq = daq
        self.daq_available = daq_available
        self._busy = False
        self._ma_420_controls = {}
        self._auto_buttons = {}

        self.frame = ttk.Frame(notebook, padding=8)
        notebook.add(self.frame, text="CN0582 Settings")

        self.current_source_vars = [tk.BooleanVar(value=False) for _ in range(4)]
        self.coupling_vars = [tk.BooleanVar(value=False) for _ in range(4)]
        self.invert_vars = [tk.BooleanVar(value=False) for _ in range(4)]
        self.store_rate_vars = [tk.StringVar(value="Full") for _ in range(4)]
        self.startup_bias_vars = [tk.BooleanVar(value=False) for _ in range(4)]
        self.gain_vars = [tk.StringVar(value="1") for _ in range(4)]
        self.bias_vars = [tk.StringVar(value="11000") for _ in range(4)]
        self.ma_420_var = tk.BooleanVar(value=False)
        self.serial_var = tk.StringVar(value="-")
        self.status_var = tk.StringVar(value="CN0582 not connected")

        self._build()
        self._load_from_daq()

        self._startup_bias_channels = [
            ch for ch in range(4) if self.startup_bias_vars[ch].get()
        ]
        if self._startup_bias_channels and self.daq is not None:
            self.daq.startup_bias_done.clear()
        if not (self.daq_available and self.daq is not None):
            self._set_controls_enabled(False)

    def _build(self):
        top = ttk.LabelFrame(self.frame, text="Channel setup", padding=8)
        top.pack(fill="x", padx=4, pady=(0, 8))

        headers = [
            "",
            "Current Source",
            "4-20 mA",
            "AC Coupling",
            "Gain",
            "Invert",
            "Store rate",
            "Sensor DC Bias (mV)",
            "",
            "Auto-bias on startup",
        ]
        for col, text in enumerate(headers):
            ttk.Label(top, text=text).grid(row=0, column=col, padx=8, pady=(0, 6))

        self._controls = []
        for ch in range(4):
            ttk.Label(top, text=f"Channel {ch}").grid(row=ch + 1, column=0, sticky="w", padx=8)

            current = _chk(top, self.current_source_vars[ch], lambda ch=ch: self._apply_current_source(ch))
            current.grid(row=ch + 1, column=1)
            self._controls.append(current)

            ma_420 = _chk(top, self.ma_420_var, self._apply_4_20ma)
            ma_420.grid(row=ch + 1, column=2)
            self._controls.append(ma_420)
            self._ma_420_controls[ch] = ma_420
            if ch != 3:
                ma_420.state(["disabled"])

            coupling = _chk(top, self.coupling_vars[ch], lambda ch=ch: self._apply_coupling(ch))
            coupling.grid(row=ch + 1, column=3)
            self._controls.append(coupling)

            gain = ttk.Combobox(top, textvariable=self.gain_vars[ch], width=6, state="readonly",
                                values=[str(v) for v in GAINS])
            gain.grid(row=ch + 1, column=4, padx=6)
            gain.bind("<<ComboboxSelected>>", lambda _event, ch=ch: self._apply_gain(ch))
            self._controls.append(gain)

            invert = _chk(top, self.invert_vars[ch], lambda ch=ch: self._apply_invert(ch))
            invert.grid(row=ch + 1, column=5)
            self._controls.append(invert)

            if ch == CN0582_LOADCELL_CHANNEL:
                store = ttk.Combobox(top, textvariable=self.store_rate_vars[ch], width=8,
                                     state="readonly",
                                     values=[label for label, _ in STORE_RATE_LABELS])
                store.bind("<<ComboboxSelected>>",
                           lambda _event, ch=ch: self._apply_store_rate(ch))
                self._controls.append(store)
            else:
                store = ttk.Label(top, text="Full")
            store.grid(row=ch + 1, column=6, padx=6)

            bias = ttk.Entry(top, textvariable=self.bias_vars[ch], width=10, justify="right")
            bias.grid(row=ch + 1, column=7, padx=6)
            self._controls.append(bias)

            set_btn = ttk.Button(top, text="Set", width=6, command=lambda ch=ch: self._apply_bias(ch))
            set_btn.grid(row=ch + 1, column=8, padx=4)
            self._controls.append(set_btn)

            startup = _chk(top, self.startup_bias_vars[ch], lambda ch=ch: self._apply_startup_bias(ch))
            startup.grid(row=ch + 1, column=9)
            self._controls.append(startup)

            auto_btn = ttk.Button(top, text="Auto", width=6, command=lambda ch=ch: self._auto_bias([ch]))
            auto_btn.grid(row=ch + 1, column=10, padx=(8, 0))
            self._controls.append(auto_btn)
            self._auto_buttons[ch] = auto_btn

        device = ttk.LabelFrame(self.frame, text="Device", padding=8)
        device.pack(fill="x", padx=4, pady=(0, 8))
        ttk.Label(device, text="Serial").pack(side="left")
        ttk.Label(device, textvariable=self.serial_var).pack(side="left", padx=(6, 16))
        ttk.Label(device, text="Status").pack(side="left")
        self.status_label = ttk.Label(device, textvariable=self.status_var)
        self.status_label.pack(side="left", padx=(6, 0))

        actions = ttk.Frame(self.frame)
        actions.pack(fill="x", padx=4)
        self.auto_selected_btn = ttk.Button(actions, text="Auto-bias selected startup channels",
                                            command=self._run_startup_auto_bias)
        self.auto_selected_btn.pack(side="left")
        self._controls.append(self.auto_selected_btn)

    def _set_status(self, text):
        self.status_var.set(text)

    def _set_controls_enabled(self, enabled):
        for control in self._controls:
            try:
                if enabled:
                    control.state(["!disabled"])
                else:
                    control.state(["disabled"])
            except tk.TclError:
                try:
                    control.configure(state="normal" if enabled else "disabled")
                except tk.TclError:
                    pass

        # Only channel 3 supports the on-board 249 ohm load.
        for ch, control in self._ma_420_controls.items():
            if ch != 3:
                try:
                    control.state(["disabled"])
                except tk.TclError:
                    control.configure(state="disabled")

        self._sync_auto_buttons(enabled)

    def _sync_auto_buttons(self, enabled=True):
        """Grey out Auto on any channel not ticked for auto-bias.

        An unticked channel is one whose bias was set deliberately -- channel 1 sits
        at 4000 mV for the load cell -- and a search would sweep the DAC across its
        whole range to find a new one.  The tick is the only way to allow that.
        """
        for ch, button in self._auto_buttons.items():
            allow = enabled and bool(self.startup_bias_vars[ch].get())
            try:
                button.state(["!disabled"] if allow else ["disabled"])
            except tk.TclError:
                button.configure(state="normal" if allow else "disabled")

    def _load_from_daq(self):
        if self.daq is None:
            self.serial_var.set("-")
            self._set_status("CN0582 not connected")
            return
        snapshot = self.daq.snapshot_settings()
        for ch in range(4):
            self.current_source_vars[ch].set(snapshot["current_source"][ch])
            self.coupling_vars[ch].set(bool(snapshot["coupling"][ch]))
            self.invert_vars[ch].set(bool(snapshot["invert"][ch]))
            self.store_rate_vars[ch].set(
                STORE_RATE_BY_HZ.get(snapshot["store_rate_hz"][ch], "Full"))
            self.startup_bias_vars[ch].set(snapshot["auto_bias_on_startup"][ch])
            self.gain_vars[ch].set(str(snapshot["gains"][ch]))
            self.bias_vars[ch].set(f"{snapshot['bias_mv'][ch]:g}")
        self.ma_420_var.set(snapshot["ma_420"])
        self._sync_auto_buttons(self.daq_available and not self._busy)
        try:
            self.serial_var.set(self.daq.dev.read_serial())
            self._set_status("connected")
        except Exception as err:
            self.serial_var.set("-")
            self._set_status(f"serial read failed: {err}")

    def _guard(self, what, fn, success_text):
        if self.daq is None or not self.daq_available:
            messagebox.showerror("CN0582 Not Available", "No CN0582 connected.")
            return False
        if self._busy:
            return False
        try:
            fn()
        except Exception as err:
            self._set_status(f"{what} failed: {err}")
            messagebox.showerror("CN0582 Settings", f"{what} failed:\n\n{err}")
            self._load_from_daq()
            return False
        self._set_status(success_text() if callable(success_text) else success_text)
        return True

    def _apply_current_source(self, ch):
        enabled = self.current_source_vars[ch].get()
        if self._guard(
            f"ch{ch} current source",
            lambda: self.daq.set_current_source_enabled(ch, enabled),
            lambda: f"current-source mask = 0x{self.daq.iepe_mask:02X}",
        ):
            self._load_from_daq()

    def _apply_4_20ma(self):
        enabled = self.ma_420_var.get()
        if self._guard(
            "4-20 mA mode",
            lambda: self.daq.set_4_20ma_enabled(enabled),
            f"ch3 4-20 mA {'on' if enabled else 'off'}",
        ):
            self._load_from_daq()

    def _apply_coupling(self, ch):
        enabled = self.coupling_vars[ch].get()
        self._guard(
            f"ch{ch} coupling",
            lambda: self.daq.set_channel_coupling(ch, enabled),
            f"ch{ch} coupling = {'AC' if enabled else 'DC'}",
        )

    def _apply_gain(self, ch):
        gain = int(self.gain_vars[ch].get())
        self._guard(
            f"ch{ch} gain",
            lambda: self.daq.set_channel_gain(ch, gain),
            lambda: f"gains = {self.daq.gains}",
        )

    def _apply_bias(self, ch):
        try:
            mv = float(self.bias_vars[ch].get())
        except ValueError:
            messagebox.showerror("Bias", "Enter a number in mV")
            return
        self._guard(
            f"ch{ch} bias",
            lambda: self.daq.set_channel_bias(ch, mv),
            f"ch{ch} DC bias = {mv:g} mV",
        )

    def _apply_invert(self, ch):
        """Flip the recorded sign of a channel.

        Software only, so there is no board write to fail and nothing to re-bias:
        it takes effect on the next capture, in the plot and in the parquet alike.
        """
        enabled = self.invert_vars[ch].get()
        if self.daq is None:
            return
        self.daq.set_channel_invert(ch, enabled)
        self._set_status(f"ch{ch} signal {'inverted' if enabled else 'normal'}")

    def _apply_store_rate(self, ch):
        # Only reachable from the load-cell channel's dropdown; the DAQ refuses
        # the rest anyway, but there is no second path to it from here.
        """Set the rate this channel is written to the parquet at.

        A save-time decimation, so nothing about the capture changes -- the plot,
        playback and the mel tab all still see the full acquisition rate.
        """
        hz = STORE_RATE_BY_LABEL.get(self.store_rate_vars[ch].get(), 0)
        if self.daq is None:
            return
        self.daq.set_channel_store_rate(ch, hz)
        self._set_status(f"ch{ch} stored at "
                         + ("the full rate" if hz == 0 else f"{hz} Hz"))

    def _apply_startup_bias(self, ch):
        enabled = self.startup_bias_vars[ch].get()
        if self.daq is None:
            return
        self.daq.set_auto_bias_on_startup(ch, enabled)
        self._sync_auto_buttons(self.daq_available and not self._busy)
        self._set_status(f"startup auto-bias for ch{ch} {'enabled' if enabled else 'disabled'}")

    def run_startup_auto_bias_now(self):
        """Startup pass: confirm the restored bias, and search only what is off.

        apply_config() has already pushed the saved bias for every channel, so this
        usually costs one capture for the whole board instead of 8-10 s per channel.
        """
        channels = [ch for ch in range(4) if self.startup_bias_vars[ch].get()]
        self._startup_bias_channels = channels
        if channels:
            self._auto_bias_blocking(channels)
        elif self.daq is not None:
            self.daq.startup_bias_done.set()

    def _run_startup_auto_bias(self):
        """Re-search every ticked channel on request, skipping the centred check."""
        channels = [ch for ch in range(4) if self.startup_bias_vars[ch].get()]
        if not channels:
            self._set_status("no channels are ticked for auto-bias")
            return
        self._auto_bias(channels)

    def _describe(self, result):
        searched = result.get("searched") or []
        if not searched:
            return "nothing to bias"
        return "biased " + ", ".join(f"ch{ch}" for ch in searched)

    def _auto_bias_blocking(self, channels):
        if self.daq is None or not self.daq_available:
            return

        def _progress(ch, idx, total):
            self._set_status(f"biasing ch{ch} ({idx + 1}/{total})...")

        self._busy = True
        self._set_controls_enabled(False)
        try:
            self._set_status("biasing startup channels...")
            result = self.daq.auto_bias(force=True, progress=_progress,
                                        channels=channels)
            # auto_bias touches the analog chain repeatedly; re-apply the saved
            # front-end state so current source/coupling/gain always end up
            # exactly as configured after startup.
            self.daq.apply_config()
            self._load_from_daq()
            self._set_status(f"startup bias: {self._describe(result)}")
        except Exception as err:
            self._set_status(f"startup auto-bias failed: {err}")
            messagebox.showerror("CN0582 Auto-bias", f"Startup auto-bias failed:\n\n{err}")
        finally:
            self._busy = False
            if self.daq is not None:
                self.daq.startup_bias_done.set()
            self._set_controls_enabled(True)

    def _auto_bias(self, channels):
        """Re-search one or more channels, on a worker thread."""
        if self.daq is None or not self.daq_available or self._busy:
            return

        allowed = self.daq.biasable_channels(channels)
        blocked = [ch for ch in channels if ch not in allowed]
        if blocked:
            self._set_status("not ticked for auto-bias: "
                             + ", ".join(f"ch{ch}" for ch in blocked))
        if not allowed:
            return

        def _progress(ch, idx, total):
            self.root.after(0, lambda: self._set_status(f"biasing ch{ch} ({idx + 1}/{total})..."))

        def _worker():
            self._busy = True
            self.root.after(0, lambda: self._set_controls_enabled(False))
            try:
                result = self.daq.auto_bias(force=True, progress=_progress,
                                            channels=allowed)
                # Keep the board state deterministic after a manual auto-bias too.
                self.daq.apply_config()
                self.root.after(0, self._load_from_daq)
                self.root.after(0, lambda: self._set_status(self._describe(result)))
            except Exception as err:
                self.root.after(0, lambda: self._set_status(f"auto-bias failed: {err}"))
                self.root.after(0, lambda: messagebox.showerror(
                    "CN0582 Auto-bias", f"Auto-bias failed:\n\n{err}"))
            finally:
                self._busy = False
                if self.daq is not None:
                    self.daq.startup_bias_done.set()
                self.root.after(0, lambda: self._set_controls_enabled(True))

        threading.Thread(target=_worker, daemon=True).start()
