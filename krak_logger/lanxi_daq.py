"""LAN-XI DAQ initialisation.

Connecting is attempted once at startup; when the device is unreachable the
application keeps running with the LAN-XI source disabled.
"""

import atexit
import os
import signal
import sys

from HelpFunctions.lanxi import LanXI

from .config import LANXI_FALLBACK_SAMPLE_RATE


def init_lanxi():
    """Connect to the LAN-XI at BKDAQ_IP (from the environment).

    Returns (lanxi, sample_rate, available):
        lanxi       -- LanXI instance, or None when unavailable
        sample_rate -- device rate, or LANXI_FALLBACK_SAMPLE_RATE
        available   -- True when the stream was set up successfully
    """
    try:
        ip = os.getenv("BKDAQ_IP")
        lanxi = LanXI(ip)
        lanxi.setup_stream()
        atexit.register(lanxi.close_stream)
        signal.signal(signal.SIGINT, lambda _s, _f: (lanxi.close_stream(), sys.exit(0)))
        return lanxi, lanxi.sample_rate, True
    except Exception as err:
        print(f"LAN-XI not available: {err}")
        return None, LANXI_FALLBACK_SAMPLE_RATE, False
