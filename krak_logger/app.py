"""Main window assembly: MCU connection bar, notebook tabs and shutdown."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

import matplotlib.pyplot as plt
import sounddevice as sd

from mcu_protocol import MCUProtocol
from mcu_tabs import MCUController, scale_fonts

from .cn0582_daq import init_cn0582
from .config import TEMP_DIR, ensure_temp_dir, load_env
from .cn0582_settings_tab import CN0582SettingsTab
from .logger_tab import LoggerTab
from .mel_tab import build_mel_tab
from .sp_tabs import build_signal_processing_tabs


def main():
    load_env()
    ensure_temp_dir()
    daq, daq_sample_rate, daq_available = init_cn0582(TEMP_DIR)

    root = tk.Tk()
    root.withdraw()
    root.title("KRAK Suite – CN0582 Recorder + MCU Controller")
    scale_fonts(15)  # set readable default; user can adjust via the Font spinbox

    # -- MCU Serial Connection bar (always visible, above tabs) --------------
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
    _font_size_var = tk.IntVar(value=12)

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

    # -- Main notebook --------------------------------------------------------
    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))

    # KRAK Logger tab
    krak_frame = ttk.Frame(notebook)
    notebook.add(krak_frame, text="KRAK Logger")

    # Duration shared between the logger tab and the MCU measurement tab
    shared_duration_var = tk.StringVar(value="15")

    logger = LoggerTab(root, krak_frame, mcu_protocol,
                       daq, daq_sample_rate, daq_available,
                       shared_duration_var)
    settings_tab = CN0582SettingsTab(notebook, root, daq, daq_available)

    # MCU tabs -- created now so they exist before any connect attempt
    mcu_ctrl = MCUController(notebook, root, mcu_protocol,
                             start_daq=logger.start_linked_recording,
                             duration_var=shared_duration_var)

    sp_cleanup = build_signal_processing_tabs(notebook, root)
    mel_tab = build_mel_tab(notebook, root, logger)

    # -- Ctrl+H: hide / restore all tabs except the always-visible set ---------
    _extra_tabs_hidden = [False]
    _hidden_tab_data   = []   # list of (position, child_widget, {options})

    def _toggle_extra_tabs(event=None):
        _always_visible = {krak_frame, settings_tab.frame, mcu_ctrl.measurement, mcu_ctrl.position, mel_tab}
        if not _extra_tabs_hidden[0]:
            # Collect and hide every tab except the always-visible set
            _hidden_tab_data.clear()
            for pos, tab_id in enumerate(notebook.tabs()):
                child = notebook.nametowidget(tab_id)
                if child not in _always_visible:
                    opts = {}
                    for key in ('text', 'image', 'compound', 'underline',
                                'sticky', 'padding', 'state'):
                        try:
                            opts[key] = notebook.tab(tab_id, key)
                        except tk.TclError:
                            pass
                    _hidden_tab_data.append((pos, child, opts))
            for _, child, _opts in _hidden_tab_data:
                notebook.hide(child)
            notebook.select(krak_frame)
            _extra_tabs_hidden[0] = True
        else:
            # Restore in original position order
            for pos, child, opts in _hidden_tab_data:
                notebook.insert(pos, child, **opts)
            _hidden_tab_data.clear()
            _extra_tabs_hidden[0] = False

    root.bind('<Control-h>', _toggle_extra_tabs)
    _toggle_extra_tabs()   # start with extra tabs hidden

    if daq_available and daq is not None:
        settings_tab.run_startup_auto_bias_now()

    root.deiconify()

    # -- Clean shutdown ---------------------------------------------------------
    def on_closing():
        # Disconnect MCU serial
        try:
            if mcu_protocol.connected:
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
            # Close the CN0582 USB stream
            if daq is not None:
                daq.close_stream()
            print("CN0582 stream closed")
        except Exception as e:
            print(f"Error closing CN0582 stream: {e}")

        try:
            # Stop signal-processing worker threads, then matplotlib figures
            sp_cleanup()
            plt.close('all')
            print("Matplotlib plots closed")
        except Exception as e:
            print(f"Error closing plots: {e}")

        # Attempt to stop all non-main threads gracefully
        for thread in threading.enumerate():
            if thread is not threading.main_thread():
                try:
                    print(f"Waiting for thread to finish: {thread.name}")
                    thread.join(timeout=2)
                except Exception as e:
                    print(f"Error joining thread {thread.name}: {e}")

        print("Cleanup complete, closing application")
        root.quit()     # Stop the mainloop
        root.destroy()  # Destroy the window

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()
