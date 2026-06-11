"""Disk-backed audio buffer -- avoids accumulating 96kHz+ frames in RAM."""

import os
import queue
import threading

import numpy as np


class DiskBuffer:
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

    # Called from audio callback -- must be fast
    def push(self, chunk):
        self._q.put(chunk.tobytes())

    @property
    def sample_count(self):
        return self._count

    def _writer(self):
        while True:
            item = self._q.get()
            if item is None:          # sentinel -> shut down
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
