"""
EVAL-CN0582-USBZ driver — control commands + CONTINUOUS (gapless) streaming.

The vendor GUI acquires in 32.768 ms bursts at a ~11 % duty cycle: it sends "start",
reads exactly 128 packets, then sends "suspend". That burst length is a *host* choice,
not a device limit — packets arrive at a steady 250.0 us (= 2048 B / 32 B per sample-set
/ 256 kSPS), so the FX3 streams ADC-paced in real time rather than dumping a fixed DMA
buffer. This driver sends "start" and simply never sends "suspend".

Wire rate: 32 B/sample-set x 256 kSPS = 8.192 MB/s, about 21 % of USB 2.0 high-speed
bulk. To sustain it you must keep several transfers in flight at all times; a loop of
synchronous dev.read() calls will drop samples in the gaps between transfers.

    pip install libusb1        # preferred, async streaming
    pip install pyusb          # control-only fallback

Windows setup: the board binds to ADI's 'adifx3' driver, which libusb cannot use. Rebind
the 0456:ED11 device to WinUSB with Zadig (per USB port). Flashing the firmware to SPI
flash and setting S1 to SPI boot (S1-1 OFF, S1-2 ON) makes the board come up ready on
its own, so the vendor GUI is not needed at all.

Validated against hardware: board ID and calibration constants match the captures, frame
sync holds on live data, and gapless recording sustains 8.19 MB/s - the predicted rate -
with no dropped samples.

Three encodings here were wrong in the original capture analysis and are corrected in
set_gain(), set_dc_bias() and set_current_source(): the gain byte packs ALL FOUR channels
into a 12-bit word, the DC-bias channel byte is a DAC bitmask rather than a channel index,
and the current source is addressed one channel per write rather than by a bitmask.
"""

from __future__ import annotations

import os
import queue
import shutil
import struct
import threading
import time

import numpy as np

VID, PID = 0x0456, 0xED11

NOT_FOUND_MSG = (f"EVAL-CN0582-USBZ ({VID:#06x}:{PID:#06x}) not found - "
                 "check the USB cable and that the board is powered.")
NO_BACKEND_MSG = (
    "No USB backend in this interpreter:\n"
    "    {exe}\n\n"
    "Run from the project venv instead:\n"
    "    .venv\\Scripts\\python.exe cn0582_gui.py\n\n"
    "or install one here:  python -m pip install libusb1 pyusb\n\n"
    "({err})"
)
DRIVER_MSG = (
    "The board is present but libusb cannot open it ({err}).\n\n"
    "If the vendor 'CN0582 EVALUATION SOFTWARE' is running, close it first - it holds\n"
    "the device open.\n\n"
    "Otherwise the board is still bound to ADI's FX3 driver (service 'adifx3'), which\n"
    "libusb cannot use. Rebind it to WinUSB:\n"
    "  1. run Zadig (zadig.akeo.ie) as administrator\n"
    "  2. Options -> List All Devices\n"
    f"  3. pick the entry whose USB ID is {VID:04x} {PID:04x} (Analog Devices USB3-SPI)\n"
    "  4. set the target box to WinUSB and press Replace Driver\n\n"
    "Driver binding is PER USB PORT: do this on the port you will actually use, and\n"
    "stay on it. Plugging into a different port re-binds adifx3 there.\n\n"
    "This takes the board away from the vendor GUI. To go back: Device Manager ->\n"
    "Update driver -> Browse -> Let me pick -> 'Analog Devices USB3-SPI'."
)

EP_CMD_OUT, EP_RSP_IN, EP_DATA_IN = 0x02, 0x82, 0x81

# Board bring-up block, replayed byte-for-byte from startup_interaction.json.
# The vendor GUI sends this immediately after reading the board ID and before any
# user setting. The meaning of the 0x00/0x01/0x02/0x74-prefixed writes is still
# undecoded (they need the schematic), but replaying them verbatim is enough to put
# the FX3 and the AD7768-4 into the same state the vendor GUI leaves them in.
#
# Every one of these is echoed back on EP 0x82, so each write is verifiable.
#
# Note: the four bare bytes 00/02/04/06 are the per-channel current-source command
# (byte = (channel << 1) | enable), i.e. all four sources switched OFF one at a time.
# So init_board() always leaves every current source off - follow it with an explicit
# set_current_source()/set_current_sources(), exactly as the vendor GUI does.
_XFER_STATUS = {0: "COMPLETED", 1: "ERROR", 2: "TIMED_OUT", 3: "CANCELLED",
                4: "STALL", 5: "NO_DEVICE", 6: "OVERFLOW"}

INIT_SEQUENCE = [
    bytes.fromhex(h) for h in (
        "002100", "010000", "002100", "004000", "004000", "002000",
        "020805", "023304", "020107", "020001",
        "020006", "028006", "020006", "028006",
        "00", "02", "04", "06",
        "743400", "741900",
        "6331", "6333", "6335", "6337",      # all four channels -> DC coupling
    )
]

FS_HZ = 256_000.0
VREF = 4.096
FRAME_BYTES = 32                     # bytes on the wire per 4-channel sample-set
GAINS = (0, 1, 2, 5, 10, 20, 50, 100)   # LTC6910 codes 0..7; 0 = muted
MAX_CLIP_S = 15.0                    # intended use: short clips, decoded offline

# ---------------------------------------------------------------------------
# stream decoding
# ---------------------------------------------------------------------------


def decode(buf: bytes | np.ndarray):
    """Decode a 32-byte-aligned EP 0x81 buffer -> (data[4,N] int32, headers[4,N] uint8).

    Each byte is 0xF? and its low nibble is a bit-slice across the AD7768-4's four
    DOUT lines; 32 bytes = one 32-bit frame per channel = [8-bit header][24-bit data].
    """
    b = np.frombuffer(buf, dtype=np.uint8) if isinstance(buf, (bytes, bytearray)) else buf
    n = (len(b) // FRAME_BYTES) * FRAME_BYTES
    fr = (b[:n] & 0x0F).reshape(-1, FRAME_BYTES)

    words = np.empty((4, fr.shape[0]), dtype=">u4")
    for c in range(4):
        # packbits packs MSB-first along axis 1 — exactly the AD7768 shift order
        words[c] = np.packbits((fr >> c) & 1, axis=1).view(">u4").ravel()

    headers = (words >> 24).astype(np.uint8)
    data = (words & 0xFFFFFF).astype(np.int64)
    data = np.where(data >= (1 << 23), data - (1 << 24), data).astype(np.int32)
    return data, headers


def find_alignment(buf: bytes) -> int:
    """Byte offset 0..31 at which frames start, using header[2:0] == channel id.

    Returns -1 if no offset validates — that means the buffer is not a CN0582 stream
    (or the high-nibble 0xF marker is missing).
    """
    b = np.frombuffer(buf, dtype=np.uint8)
    if len(b) < FRAME_BYTES * 8:
        raise ValueError("need at least 8 frames to establish alignment")
    for off in range(FRAME_BYTES):
        n = ((len(b) - off) // FRAME_BYTES) * FRAME_BYTES
        if n < FRAME_BYTES * 8:
            break
        _, hdr = decode(b[off:off + n])
        if all(np.all((hdr[c] & 0x07) == c) for c in range(4)):
            return off
    return -1


def to_volts_adc(data):
    """Voltage at the ADC input."""
    return data.astype(np.float64) / (1 << 23) * VREF


def to_volts_input(data, pga_gain=1.0):
    """Voltage at the BNC: front end is 0.3 (level shift) x 2.667 (FDA) x G_PGA."""
    return to_volts_adc(data) / (0.8 * pga_gain)


def saturated(headers):
    """Samples the AD7768 flagged over-range (header bit 3)."""
    return (headers & 0x08) != 0


def _free_mb(path):
    """Free space in MB on the volume holding path; the file itself need not exist."""
    try:
        return shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free / (1 << 20)
    except OSError:
        return float("inf")     # cannot tell -- do not stop the recording over it


# ---------------------------------------------------------------------------
# device
# ---------------------------------------------------------------------------


class CN0582:
    def __init__(self, backend="auto"):
        self._usb1 = self._ctx = self._h = None
        self._pyusb = None
        self._gains = [1, 1, 1, 1]          # shadow of the packed gain word
        last = None
        if backend in ("auto", "libusb1"):
            try:
                import usb1
            except ImportError as e:
                if backend == "libusb1":
                    raise
                last = e
            else:
                self._usb1 = usb1
                self._ctx = usb1.USBContext()
                self._ctx.open()
                dev = next((d for d in self._ctx.getDeviceList()
                            if (d.getVendorID(), d.getProductID()) == (VID, PID)), None)
                if dev is None:
                    raise OSError(NOT_FOUND_MSG)
                try:
                    self._h = dev.open()
                    self._h.claimInterface(0)
                    # a previous run may have died mid-stream; clear that before
                    # any command, or every reply is one behind
                    self.resync()
                except usb1.USBError as e:
                    self._h = None
                    self._ctx.close()
                    self._ctx = None
                    raise OSError(DRIVER_MSG.format(err=e)) from e
        if self._h is None:
            try:
                import usb.core
            except ImportError:
                import sys as _sys
                raise OSError(NO_BACKEND_MSG.format(exe=_sys.executable, err=last))
            dev = usb.core.find(idVendor=VID, idProduct=PID)
            if dev is None:
                raise OSError(NOT_FOUND_MSG)
            dev.set_configuration()
            self._pyusb = dev
            self.resync()

    # -- transport ---------------------------------------------------------

    def cmd(self, payload: bytes, resp_len: int | None = None, timeout=1000) -> bytes:
        """One command out on EP 0x02, one response in on EP 0x82 of equal length."""
        n = resp_len if resp_len is not None else len(payload)
        if self._h is not None:
            self._h.bulkWrite(EP_CMD_OUT, payload, timeout)
            return bytes(self._h.bulkRead(EP_RSP_IN, n, timeout))
        self._pyusb.write(EP_CMD_OUT, payload, timeout)
        return bytes(self._pyusb.read(EP_RSP_IN, n, timeout))

    def _drain(self, ep, timeout=250, max_reads=200):
        """Read and discard whatever is still queued on an IN endpoint."""
        total = 0
        for _ in range(max_reads):
            try:
                if self._h is not None:
                    n = len(self._h.bulkRead(ep, 16384, timeout))
                else:
                    n = len(self._pyusb.read(ep, 16384, timeout))
            except Exception:
                break                      # timeout == pipe is empty
            if not n:
                break
            total += n
        return total

    def resync(self):
        """Stop any orphaned stream and clear both IN pipes.

        Needed because the board keeps streaming after "start" until it is told to
        suspend. If a process dies mid-recording - a crash, a kill, closing the window
        during a capture - the board is left streaming and the 2048-byte echo of the
        last keyword command sits half-read in the response pipe. Every later command
        then reads the PREVIOUS command's reply, so the serial comes back as garbage
        like 'rt\\xff\\xff...' (characters 3-4 of the word "start").

        Called automatically on connect. Returns (bytes_drained_0x81, bytes_drained_0x82).
        """
        # write suspend without reading its echo yet - the pipe is not trustworthy
        payload = b"suspend" + b"\xff" * (2048 - 7)
        try:
            if self._h is not None:
                self._h.bulkWrite(EP_CMD_OUT, payload, 1000)
            else:
                self._pyusb.write(EP_CMD_OUT, payload, 1000)
        except Exception:
            pass
        time.sleep(0.05)
        n81 = self._drain(EP_DATA_IN)
        n82 = self._drain(EP_RSP_IN)
        return n81, n82

    def _echo(self, payload: bytes):
        r = self.cmd(payload)
        if r[:len(payload)] != payload:
            raise IOError(f"echo mismatch: sent {payload.hex()} got {r.hex()}")
        return r

    def close(self):
        # idempotent: app.py's on_closing() and the atexit/SIGINT handlers
        # registered in cn0582_daq.py can all reach this, and a second call on an
        # already-released handle segfaults deep in libusb (access violation on a
        # freed struct) rather than raising a catchable Python exception
        if self._h is None and self._pyusb is None:
            return
        try:                                # never leave a stream running
            self.resync()
        except Exception:
            pass
        if self._h is not None:
            try:
                self._h.releaseInterface(0)
            finally:
                self._h.close()
                self._ctx.close()
                self._h = None
                self._ctx = None
        self._pyusb = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        try:
            self.suspend()
        except Exception:
            pass
        self.close()

    # -- identity / calibration -------------------------------------------

    def read_serial(self) -> str:
        r = self.cmd(b"\xc3\x50\x00" + b"\x00" * 15, 18)
        if r[:3] != b"\x69\x50\x00":
            raise IOError(
                f"ID response out of sync (got {r[:3].hex()}, expected 695000) - the "
                "command pipe is still holding an older reply. Call resync().")
        return r[3:].split(b"\x00")[0].decode("ascii", "replace")

    def init_board(self, strict=True):
        """Replay the vendor GUI's bring-up block (see INIT_SEQUENCE).

        The FX3 keeps its application firmware until the board is unplugged, but the
        register state this block sets up is only applied by whoever sends it. Call
        this once after connecting, then push your own settings on top.

        Every write is echoed; with strict=True a mismatch raises. Returns the number
        of writes that were acknowledged.
        """
        ok = 0
        for payload in INIT_SEQUENCE:
            r = self.cmd(payload)
            if r[:len(payload)] != payload:
                if strict:
                    raise IOError(f"init write {payload.hex()} echoed as {r.hex()}")
            else:
                ok += 1
        return ok

    def read_cal(self, addr: int) -> float:
        r = self.cmd(b"\xc3\x50" + bytes([addr]) + b"\x00" * 8, 11)
        return struct.unpack(">d", r[3:11])[0]

    def read_all_cal(self) -> dict:
        """{(channel, index): coefficient}. Channels are 1..5, index 0..3 and 5..8."""
        out = {}
        for ch in range(1, 6):
            for idx in list(range(4)) + list(range(5, 9)):
                try:
                    out[(ch, idx)] = self.read_cal((ch << 4) | idx)
                except Exception:
                    pass
        return out

    # -- configuration -----------------------------------------------------

    def set_gain(self, ch: int, gain: int):
        """Set one channel's PGA gain (LTC6910). gain in {0,1,2,5,10,20,50,100}.

        The two 'g' registers hold ONE 12-bit word, 3 bits per channel:

            word = reg0x02 | (reg0x03 << 8)
            gain_index[ch] = (word >> (3*ch)) & 7
            index 0..7  ->  gain 0 (muted), 1, 2, 5, 10, 20, 50, 100

        So a write always carries all four channels. Setting one channel means
        read-modify-write on the shadow copy, which is why the earlier
        "code = 0x49 + 9*ch + index" reading silently clobbered the other channels.

        Cross-checked against the original captures: `CH0_gain5` wrote 0x4b
        (ch0=5, ch1=1) and the next capture `CH1_gain_2` wrote 0x53
        (ch0 still 5, ch1=2) - which only makes sense under this packing.
        """
        self._gains[ch] = GAINS.index(gain)
        self._write_gains()

    def set_gains(self, gains):
        """Set all four gains at once, e.g. set_gains([1, 1, 10, 10])."""
        for c, g in enumerate(gains):
            self._gains[c] = GAINS.index(g)
        self._write_gains()

    def _write_gains(self):
        word = 0
        for c in range(4):
            word |= (self._gains[c] & 7) << (3 * c)
        self._echo(bytes([0x67, 0x02, word & 0xFF, 0x00]))
        self._echo(bytes([0x67, 0x03, (word >> 8) & 0xFF, 0x00]))

    def get_gains(self):
        """Read both registers back and unpack to a list of four gains."""
        lo = self.cmd(b"\x67\x02\x00\xff", 4)[0]
        hi = self.cmd(b"\x67\x03\x00\xff", 4)[0]
        word = lo | (hi << 8)
        return [GAINS[(word >> (3 * c)) & 7] for c in range(4)]

    def set_coupling(self, ch: int, ac_coupled: bool):
        """AC/DC coupling (ADG5436). AC removes the sensor's standing DC bias."""
        self._echo(b"c" + str(2 * ch + (0 if ac_coupled else 1)).encode())

    def set_dc_bias(self, ch: int, mv: float):
        """Sensor DC bias / level shift (AD5686R).

        The channel-select byte's low nibble is the AD5686R DAC channel **bitmask**,
        not a channel index:

            ch0 -> 0x31    ch1 -> 0x32    ch2 -> 0x34    ch3 -> 0x38
            0x3F writes all four DACs at once

        Verified on hardware: byte 0x33 moves ch0 AND ch1 together, which is what gave
        the bitmask away. (An earlier "0x31 + channel" reading happens to be correct
        for ch0/ch1 but addresses the wrong DAC for ch2 and ch3.)
        """
        self.set_dc_bias_mask(1 << ch, mv)

    def set_dc_bias_mask(self, mask: int, mv: float):
        """Write the level-shift DAC for every channel in `mask` simultaneously."""
        code = round((2.52 + 0.3 * mv / 1000.0) / 1.3 / 5.0 * 65536)
        code = max(0, min(65535, code))
        self._echo(bytes([0x03, 0x30 | (mask & 0x0F),
                          (code >> 8) & 0xFF, code & 0xFF]))

    def auto_bias(self, ch: int, target_v=0.0, lo_mv=0, hi_mv=14000, steps=12,
                  settle=0.5, tol_v=0.05, refine=3):
        """Find the Sensor DC Bias that centres a channel, and leave it applied.

        The AD5686R comes out of reset with the level-shift reference at its default,
        which parks the front end within ~180 mV of the negative rail. That is the whole
        reason an unbiased channel reads about -3.9 V and clips on the smallest signal.

        Bisects on the SIGN of the error rather than fitting a line, because the slope
        varies a lot between channels and configurations (measured 0.6 to 2.8 mV of ADC
        per mV of bias), and a clipped reading still carries a correct sign. Bisection is
        followed by secant refinement, because a DC-coupled IEPE channel settles slowly
        and a pure bisection can stop a long way off target.

        `settle` matters: with a sensor powered by the current source and DC coupling,
        the input needs a few hundred ms after each bias change. Raise it if the result
        does not land within `tol_v`.

        Returns the chosen bias in mV. Raises RuntimeError if the target is not
        reachable anywhere in [lo_mv, hi_mv].
        """
        def measure(mv):
            self.set_dc_bias(ch, mv)
            time.sleep(settle)
            d, _ = self.capture(8192)
            return to_volts_adc(d[ch]).mean()

        lo, hi = float(lo_mv), float(hi_mv)
        v_lo, v_hi = measure(lo), measure(hi)
        if (v_lo - target_v) * (v_hi - target_v) > 0:
            raise RuntimeError(
                f"ch{ch}: target {target_v:+.3f} V is not reachable - bias {lo:.0f} mV "
                f"gives {v_lo:+.3f} V and {hi:.0f} mV gives {v_hi:+.3f} V")
        for _ in range(steps):
            mid = 0.5 * (lo + hi)
            v = measure(mid)
            if (v_lo - target_v) * (v - target_v) <= 0:
                hi, v_hi = mid, v
            else:
                lo, v_lo = mid, v

        best = 0.5 * (lo + hi)
        v = measure(best)
        # secant polish against the bracket slope; bisection alone can stall on a
        # slow-settling channel
        for _ in range(refine):
            if abs(v - target_v) <= tol_v:
                break
            slope = (v_hi - v_lo) / (hi - lo) if hi != lo else 0.0
            if abs(slope) < 1e-9:
                break
            nxt = max(lo_mv, min(hi_mv, best + (target_v - v) / slope))
            if abs(nxt - best) < 1.0:
                break
            best, v = nxt, measure(nxt)

        best = int(round(best))
        self.set_dc_bias(ch, best)
        time.sleep(settle)
        return best

    def set_current_source(self, ch: int, on: bool):
        """Enable/disable one channel's IEPE current source.

        One bare byte, addressed PER CHANNEL - this is not a bitmask:

            byte = (channel << 1) | enable

            ch0: 0x00 off / 0x01 on      ch2: 0x04 off / 0x05 on
            ch1: 0x02 off / 0x03 on      ch3: 0x06 off / 0x07 on

        Same shape as the coupling command, which is ASCII 'c' followed by
        (2*channel + state).

        The bitmask reading this replaces was wrong, and wrong in a way that hid
        itself: for a single channel the mask value collides with the correct
        per-channel code, so ch0-only worked by coincidence while any two-channel
        selection silently did nothing. A mask of 0x0D went out as one byte
        meaning "channel 6 on", so no source was enabled at all.

        Disproved by CH3_current_ON_OFF toggling.json, where toggling ch3 moves
        bit 0 (0x07 <-> 0x06) rather than bit 3, and by the startup block's
        00 02 04 06 - all four sources switched off one at a time.
        """
        self._echo(bytes([((ch & 3) << 1) | (1 if on else 0)]))

    def set_current_sources(self, mask: int):
        """Set all four current sources from a host-side bitmask.

        Convenience only: the wire protocol has no mask, so this writes one byte
        per channel. Keeping a mask as *host* state is fine; sending one is not.
        """
        for ch in range(4):
            self.set_current_source(ch, bool(mask & (1 << ch)))

    def set_4_20ma(self, on: bool):
        """Ch3 4-20 mA current-loop input mode (249 ohm on-board load)."""
        self._echo(bytes([0x74, 0x19, 1 if on else 0]))

    # -- acquisition -------------------------------------------------------

    def start(self):
        self.cmd(b"start" + b"\xff" * (2048 - 5), 2048)

    def suspend(self):
        self.cmd(b"suspend" + b"\xff" * (2048 - 7), 2048)

    def capture(self, n_samples=8192, timeout=2000):
        """One GUI-style burst. Simple and safe; NOT gapless if called repeatedly."""
        need = n_samples * FRAME_BYTES
        self.start()
        try:
            chunks, got = [], 0
            while got < need:
                if self._h is not None:
                    d = self._h.bulkRead(EP_DATA_IN, min(65536, need - got), timeout)
                else:
                    d = self._pyusb.read(EP_DATA_IN, min(65536, need - got), timeout)
                if not len(d):
                    break
                chunks.append(bytes(d))
                got += len(d)
        finally:
            self.suspend()
        return decode(b"".join(chunks)[:need])

    def record(self, seconds=None, n_samples=None, path="capture.bin",
               n_transfers=32, xfer_bytes=131072, timeout=1000, on_ready=None):
        """Gapless capture of a fixed length, straight to disk.

        Decode afterwards with decode_file(). Returns the number of bytes written,
        and raises if any sample was dropped, so a gap can never be mistaken for
        real data. Size on disk: 8.192 MB per second of capture.

        on_ready fires once, on the calling thread, at the moment the stream is
        actually running — use it to trigger external hardware that has to line up
        with the first sample.
        """
        if seconds is None and n_samples is None:
            raise ValueError("give seconds= or n_samples=")
        if seconds is not None and not (0.1 <= seconds <= MAX_CLIP_S):
            raise ValueError(f"seconds must be 0.1..{MAX_CLIP_S:g} "
                             f"({MAX_CLIP_S:g} s = {MAX_CLIP_S*8.192:.0f} MB); "
                             f"use record_until() for longer recordings")
        target = (int(n_samples) * FRAME_BYTES if n_samples is not None
                  else int(seconds * FS_HZ) * FRAME_BYTES)
        return self._stream_to_disk(path, target, None, n_transfers, xfer_bytes,
                                    timeout, on_ready)

    def record_until(self, stop_event, path="capture.bin", n_transfers=32,
                     xfer_bytes=131072, timeout=1000, on_ready=None,
                     min_free_mb=2048):
        """Gapless capture that runs until stop_event is set.

        One "start" and one "suspend" for the whole recording, so unlike a loop of
        record() calls there is no seam between chunks: frame sync is continuous
        from the first sample to the last, and decode_file() will confirm it.

        There is no length limit, so mind the rate — 8.192 MB/s is 491 MB/min and
        29.5 GB/h. Recording ends cleanly, without raising, if free space on the
        target volume would fall below min_free_mb; pass 0 to disable that guard.
        """
        return self._stream_to_disk(path, None, stop_event, n_transfers, xfer_bytes,
                                    timeout, on_ready, min_free_mb)

    def _stream_to_disk(self, path, target, stop_event, n_transfers, xfer_bytes,
                        timeout, on_ready, min_free_mb=0):
        """Shared engine behind record() and record_until().

        target is a byte count, or None to run until stop_event is set. Nothing is
        decoded while recording — the USB callback only hands raw bytes to a writer
        thread — so the host has an enormous margin over the 8.192 MB/s wire rate.
        Sends "start" once and "suspend" only when the recording ends.
        """
        if self._h is None:
            raise RuntimeError("gapless recording needs libusb1 (pip install libusb1)")
        usb1 = self._usb1
        if xfer_bytes % FRAME_BYTES:
            raise ValueError("xfer_bytes must be a multiple of 32")

        wq: queue.Queue = queue.Queue(maxsize=256)
        stop = threading.Event()
        err: list = []
        written = [0]

        def writer():
            with open(path, "wb", buffering=1 << 20) as f:
                while True:
                    blk = wq.get()
                    if blk is None:
                        break
                    f.write(blk)
                    written[0] += len(blk)

        def on_done(t):
            st = t.getStatus()
            if st == usb1.TRANSFER_COMPLETED:
                try:
                    wq.put_nowait(bytes(t.getBuffer()[:t.getActualLength()]))
                except queue.Full:
                    err.append("writer fell behind - samples dropped")
                    stop.set()
                    return
            elif st == usb1.TRANSFER_CANCELLED:
                # we cancel every outstanding transfer during teardown; that is the
                # normal way a finished recording ends, not a failure
                return
            elif st != usb1.TRANSFER_TIMED_OUT:
                err.append(f"transfer status {st} ({_XFER_STATUS.get(st, '?')})")
                stop.set()
                return
            if not stop.is_set():
                try:
                    t.submit()
                except Exception as e:
                    err.append(e)
                    stop.set()

        transfers = []
        for _ in range(n_transfers):
            t = self._h.getTransfer()
            t.setBulk(EP_DATA_IN, xfer_bytes, callback=on_done, timeout=timeout)
            transfers.append(t)

        wt = threading.Thread(target=writer, daemon=True)
        wt.start()
        self.start()
        for t in transfers:
            t.submit()
        t0 = time.perf_counter()
        if on_ready is not None:
            try:
                on_ready()
            except Exception as cb_err:
                print(f"on_ready callback raised: {cb_err}")

        # A bounded run gets a deadline scaled to its target. An open-ended one
        # cannot tell "slow" from "still going", so it only watches for a stream
        # that never starts, and for the volume filling up underneath it.
        deadline = (t0 + (target / (FS_HZ * FRAME_BYTES)) * 3 + 5
                    if target is not None else None)
        next_space_check = t0 + 1.0
        try:
            while not stop.is_set():
                if target is not None:
                    if written[0] + wq.qsize() * xfer_bytes >= target:
                        break
                elif stop_event.is_set():
                    break
                self._ctx.handleEventsTimeout(0.05)
                now = time.perf_counter()
                if deadline is not None and now > deadline:
                    err.append("timed out waiting for data - was 'start' accepted?")
                    break
                if deadline is None and written[0] == 0 and now - t0 > 5.0:
                    err.append("timed out waiting for data - was 'start' accepted?")
                    break
                if min_free_mb and now >= next_space_check:
                    next_space_check = now + 1.0
                    if _free_mb(path) < min_free_mb:
                        print(f"stopping recording: under {min_free_mb} MB free on "
                              f"the volume holding {path}")
                        break
        finally:
            stop.set()
            for t in transfers:
                try:
                    t.cancel()
                except Exception:
                    pass
            deadline = time.perf_counter() + 2.0
            while any(t.isSubmitted() for t in transfers) and time.perf_counter() < deadline:
                self._ctx.handleEventsTimeout(0.05)
            wq.put(None)
            wt.join(timeout=5.0)
            try:
                self.suspend()
            except Exception:
                pass
        if err:
            raise IOError(f"recording failed: {err[0]}")
        # transfers already in flight when the target is reached overshoot it a little;
        # trim so record(seconds=N) yields exactly N seconds
        if target is not None and written[0] > target:
            with open(path, "r+b") as f:
                f.truncate(target)
            written[0] = target
        el = time.perf_counter() - t0
        print(f"recorded {written[0]/1e6:.1f} MB ({written[0]//FRAME_BYTES} samples/ch) "
              f"in {el:.2f} s -> {written[0]/el/1e6:.2f} MB/s")
        return written[0]


def decode_file(path, max_samples=None):
    """Decode a raw EP 0x81 recording. Returns (data[4,N] int32, headers[4,N] uint8)."""
    b = np.memmap(path, dtype=np.uint8, mode="r")
    if not np.all(b[:4096] >= 0xF0):
        raise ValueError("not a CN0582 EP 0x81 recording (high nibbles are not 0xF)")
    off = find_alignment(bytes(b[:4096]))
    if off < 0:
        raise ValueError("cannot frame-sync recording")
    n = (len(b) - off) // FRAME_BYTES
    if max_samples:
        n = min(n, max_samples)
    data = np.empty((4, n), np.int32)
    hdr = np.empty((4, n), np.uint8)
    step = 1 << 20                                   # decode in 1 M-sample chunks
    for i in range(0, n, step):
        j = min(i + step, n)
        d, h = decode(b[off + i * FRAME_BYTES: off + j * FRAME_BYTES])
        data[:, i:j], hdr[:, i:j] = d, h
    bad = [c for c in range(4) if not np.all((hdr[c] & 0x07) == c)]
    if bad:
        raise ValueError(f"frame sync lost in lanes {bad} — recording has a gap")
    return data, hdr


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# offline plotting
# ---------------------------------------------------------------------------


def plot_clip(data, hdr, channels=(0,), gain=1.0, path="capture.png",
              domain="time", title=None):
    """Plot a decoded clip. domain = 'time' | 'freq' | 'both'."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    COL = ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e"]
    channels = list(channels)
    v = to_volts_input(data, gain)
    n = data.shape[1]
    t = np.arange(n) / FS_HZ

    rows = 2 if domain == "both" else 1
    fig, axes = plt.subplots(rows, 1, figsize=(13, 4.2 * rows), squeeze=False)
    axes = axes[:, 0]
    k = 0
    if domain in ("time", "both"):
        ax = axes[k]; k += 1
        for c in channels:
            ax.plot(t, v[c], lw=0.5, color=COL[c], label=f"ch{c}")
            sat = saturated(hdr[c])
            if sat.any():
                ax.plot(t[sat], v[c][sat], ".", ms=2, color="k")
        ax.set_xlabel("s"); ax.set_ylabel("V at input"); ax.legend(fontsize=8, ncol=4)
        ax.set_title(title or f"{n} samples/channel @ {FS_HZ/1000:.0f} kSPS "
                              f"({n/FS_HZ:.3f} s), gain {gain:g}")
        ax.margins(x=0.002)
    if domain in ("freq", "both"):
        ax = axes[k]
        nfft = min(1 << 15, 1 << int(np.log2(n)))
        for c in channels:
            x = v[c][:nfft] - v[c][:nfft].mean()
            sp = np.abs(np.fft.rfft(x * np.hanning(nfft))) / nfft * 2
            f = np.fft.rfftfreq(nfft, 1 / FS_HZ)
            ax.semilogy(f / 1e3, sp + 1e-12, lw=0.6, color=COL[c], label=f"ch{c}")
        ax.set_xlabel("kHz"); ax.set_ylabel("V"); ax.legend(fontsize=8, ncol=4)
        ax.set_title(f"Spectrum ({nfft}-point)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def default_output_rate(fallback=48_000):
    """Sample rate the Windows audio output is running at.

    winsound.PlaySound hands the file to the device without resampling, so a WAV
    written at any other rate plays at the wrong speed - a 32 kHz file on a 48 kHz
    device runs 1.5x fast. Writing at the device's own rate avoids the conversion
    entirely.

    Read from the MMDevices registry: each endpoint stores a WAVEFORMATEX blob whose
    nSamplesPerSec sits at offset 12. Only endpoints with DeviceState == 1 (active)
    are considered, and the most common rate among them wins - the registry does not
    mark which endpoint is the default, but in practice a machine's active outputs
    agree. Falls back to `fallback` on any problem, including non-Windows.
    """
    try:
        import winreg
    except ImportError:
        return fallback
    KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
    FMT = "{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0"
    rates = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    name = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, name) as dev:
                        if winreg.QueryValueEx(dev, "DeviceState")[0] != 1:
                            continue        # not an active endpoint
                        with winreg.OpenKey(dev, "Properties") as props:
                            blob = winreg.QueryValueEx(props, FMT)[0]
                    if len(blob) >= 16:
                        r = int.from_bytes(blob[12:16], "little")
                        if 8000 <= r <= 384000:
                            rates.append(r)
                except OSError:
                    continue
    except OSError:
        return fallback
    if not rates:
        return fallback
    return max(set(rates), key=rates.count)


# Resolved once at import; override by assigning cn0582.WAV_RATE before exporting.
WAV_RATE = default_output_rate()


def resample_fft(x, n_out):
    """Band-limited resample by truncating the spectrum. numpy only, no scipy."""
    n_in = len(x)
    if n_out == n_in:
        return x.astype(np.float64)
    X = np.fft.rfft(x)
    keep = min(len(X), n_out // 2 + 1)
    Y = np.zeros(n_out // 2 + 1, dtype=complex)
    Y[:keep] = X[:keep]
    return np.fft.irfft(Y, n=n_out) * (n_out / n_in)


def to_wav(data, ch=0, rate=WAV_RATE, gain=1.0, normalize=True, headroom_db=1.0):
    """One channel -> (int16 samples, rate), ready to write as a WAV.

    Vibration recorded at 256 kSPS is 5.3x faster than CD audio and mostly empty above
    20 kHz, so it is resampled down to `rate` (a plain 8:1 decimation at the default)
    before being written. The DC offset is removed first - it carries no sound and would
    otherwise eat the whole int16 range.
    """
    v = to_volts_input(np.asarray(data[ch]), gain)
    v = v - v.mean()
    n_out = max(1, int(round(len(v) * rate / FS_HZ)))
    y = resample_fft(v, n_out)
    peak = np.max(np.abs(y))
    if normalize and peak > 0:
        y = y / peak * (10 ** (-headroom_db / 20.0))
    return np.clip(y * 32767, -32768, 32767).astype(np.int16), rate


def export_wav(data, path="capture.wav", ch=0, rate=WAV_RATE, gain=1.0, normalize=True):
    """Write one channel as a 16-bit mono WAV you can play or open in Audacity."""
    import wave
    pcm, rate = to_wav(data, ch=ch, rate=rate, gain=gain, normalize=normalize)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


def export_csv(data, hdr, path="capture.csv", gain=1.0, channels=(0, 1, 2, 3)):
    """Write decoded volts to CSV (time, ch..)."""
    v = to_volts_input(data, gain)
    n = data.shape[1]
    cols = np.vstack([np.arange(n) / FS_HZ] + [v[c] for c in channels]).T
    np.savetxt(path, cols, delimiter=",", fmt="%.9g",
               header="time_s," + ",".join(f"ch{c}_V" for c in channels), comments="")
    return path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Record a gapless CN0582 clip, then decode and plot it.")
    ap.add_argument("seconds", nargs="?", type=float, default=5.0)
    ap.add_argument("-o", "--out", default="capture.bin")
    ap.add_argument("--ch", type=int, default=0)
    ap.add_argument("--gain", type=int, default=1, choices=GAINS)
    ap.add_argument("--dc", action="store_true", help="DC-couple (default AC)")
    ap.add_argument("--no-iepe", action="store_true")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()

    with CN0582() as dev:
        print("serial:", dev.read_serial())
        dev.set_coupling(a.ch, ac_coupled=not a.dc)
        dev.set_current_sources(0x00 if a.no_iepe else (1 << a.ch))
        dev.set_gain(a.ch, a.gain)
        time.sleep(0.5)
        print(f"recording {a.seconds:g} s to {a.out} ...")
        dev.record(seconds=a.seconds, path=a.out)

    # ---- everything below runs only after the recording is finished ----
    data, hdr = decode_file(a.out)
    v = to_volts_input(data, a.gain)
    n = data.shape[1]
    print(f"decoded {n} samples/channel = {n/FS_HZ:.3f} s")
    for c in range(4):
        print(f"  ch{c}: dc={v[c].mean():+8.5f} V  ac={v[c].std()*1e3:8.3f} mVrms  "
              f"p-p={np.ptp(v[c])*1e3:9.3f} mV  over-range={int(saturated(hdr[c]).sum())}")
    if not a.no_plot:
        print("wrote", plot_clip(data, hdr, channels=[a.ch], gain=a.gain,
                                 path=a.out.rsplit(".", 1)[0] + ".png", domain="both"))
