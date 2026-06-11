"""Load cell data collection from MCU ADCSTREAM (200 Hz)."""

import threading

import numpy as np


class LoadCellCollector:
    """Collects load cell data from MCU via LC_LOGGING at 200 Hz.

    Protocol (PC -> MCU): LC_LOGGING START <duration_s>
    Protocol (MCU -> PC): LC:START, LC:<value> x N, LC:END
    stop() blocks until LC:END is received so all buffered samples are captured.
    """

    SAMPLE_RATE_HZ = 200.0

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
        times = np.arange(len(values)) / self.SAMPLE_RATE_HZ
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
