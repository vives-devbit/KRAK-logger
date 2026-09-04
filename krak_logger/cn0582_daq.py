"""EVAL-CN0582-USBZ DAQ initialisation and acquisition adapter.

Wraps HelpFunctions.cn0582 in the interface the logger tab already speaks --
SampleChannels(duration, on_ready) -> (time_axis, data[4, N]) -- so the recording
pipeline does not care which DAQ is underneath.

Connecting is attempted once at startup; when the board is absent the application
keeps running with the CN0582 source disabled.

Configuration comes from the environment (see .env):
    CN0582_GAINS        four PGA codes, e.g. "1,1,1,1"  (allowed: 1 2 5 10 20 50 100)
    CN0582_COUPLING     four flags, 1 = AC-coupled, e.g. "1,1,1,1"
    CN0582_IEPE_MASK    which channels get IEPE power, as a host-side bitmask,
                        e.g. "0x0F" for all four. The board itself is addressed
                        one channel at a time; this is just how we configure it.
    CN0582_BIAS_CHANNELS  channels to auto-bias, e.g. "0,1,2,3" ("" disables)
"""

import atexit
import json
import os
import signal
import sys
import threading
import time

import numpy as np

from HelpFunctions import cn0582

from .config import (
    CN0582_CURRENT_SOURCE_BIT_FOR_CHANNEL,
    CN0582_DEFAULT_COUPLING,
    CN0582_LOADCELL_CHANNEL,
    CN0582_LOADCELL_STORE_RATE,
    CN0582_DEFAULT_GAINS,
    CN0582_DEFAULT_IEPE_MASK,
    CN0582_SETTINGS_FILE,
    CN0582_SAMPLE_RATE,
)


# Rates a channel may be stored at, as offered by the settings page. 0 means the
# full acquisition rate. Every other value divides 256 kHz exactly, because
# decimation needs an integer factor.
STORE_RATE_CHOICES = (0, 32000, 8000, 1000)


def _channel_to_current_source_bit(ch):
    """Bit position used to track a GUI channel in the host-side mask.

    The board has no current-source bitmask - it takes one byte per channel,
    (channel << 1) | enable. The mask below is purely our own bookkeeping.
    """
    return int(CN0582_CURRENT_SOURCE_BIT_FOR_CHANNEL[int(ch)])


def _current_source_bitmask_for_channel(ch):
    return 1 << _channel_to_current_source_bit(ch)


def _current_source_flags_from_mask(mask):
    return [bool(int(mask) & _current_source_bitmask_for_channel(ch))
            for ch in range(4)]


def _current_source_mask_from_flags(flags):
    return sum(_current_source_bitmask_for_channel(ch)
               for ch, enabled in enumerate(flags) if enabled)


def _ch3_current_source_bit():
    return _current_source_bitmask_for_channel(3)


def _parse_int_list(raw, default, n=4):
    """Parse a comma-separated list of n ints, falling back to default."""
    if not raw:
        return list(default)
    try:
        vals = [int(v.strip(), 0) for v in raw.split(",") if v.strip() != ""]
    except ValueError:
        print(f"CN0582: cannot parse {raw!r}, using {default}")
        return list(default)
    if len(vals) != n:
        print(f"CN0582: expected {n} values in {raw!r}, using {default}")
        return list(default)
    return vals


def _parse_bias_channels(raw, n=4):
    """Parse channel indices from a comma-separated string."""
    if raw is None:
        return [0, 1, 2, 3]
    try:
        channels = [int(v.strip()) for v in raw.split(",") if v.strip() != ""]
    except ValueError:
        print(f"CN0582: cannot parse CN0582_BIAS_CHANNELS={raw!r}, biasing all four channels")
        return [0, 1, 2, 3]
    return [ch for ch in channels if 0 <= ch < n]


def _read_persisted_settings(defaults, path=CN0582_SETTINGS_FILE):
    """Load the saved CN0582 panel state, falling back to defaults."""
    settings = {
        "gains": list(defaults["gains"]),
        "coupling": list(defaults["coupling"]),
        "bias_mv": list(defaults["bias_mv"]),
        "current_source": list(defaults["current_source"]),
        "invert": list(defaults["invert"]),
        "store_rate_hz": list(defaults["store_rate_hz"]),
        "auto_bias_on_startup": list(defaults["auto_bias_on_startup"]),
        "ma_420": bool(defaults["ma_420"]),
    }
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return settings
    except Exception as err:
        print(f"CN0582: ignoring unreadable settings file {path}: {err}")
        return settings

    # Accept legacy keys from the standalone CN0582 GUI settings file.
    if "gains" not in raw and "gain" in raw:
        raw["gains"] = raw.get("gain")
    if "coupling" not in raw and "ac_coupling" in raw:
        raw["coupling"] = raw.get("ac_coupling")

    def _seq(name, cast):
        value = raw.get(name)
        if not isinstance(value, list) or len(value) != 4:
            return settings[name]
        out = []
        for item in value:
            try:
                out.append(cast(item))
            except (TypeError, ValueError):
                return settings[name]
        return out

    gains = _seq("gains", int)
    settings["gains"] = [g if g in cn0582.GAINS else settings["gains"][i]
                         for i, g in enumerate(gains)]
    settings["coupling"] = [1 if bool(v) else 0 for v in _seq("coupling", int)]
    settings["bias_mv"] = _seq("bias_mv", float)
    settings["current_source"] = [bool(v) for v in _seq("current_source", int)]
    settings["invert"] = [bool(v) for v in _seq("invert", int)]
    settings["store_rate_hz"] = [hz if hz in STORE_RATE_CHOICES else settings["store_rate_hz"][i]
                                 for i, hz in enumerate(_seq("store_rate_hz", int))]
    settings["auto_bias_on_startup"] = [bool(v) for v in _seq("auto_bias_on_startup", int)]
    raw_ma_420 = raw.get("ma_420", settings["ma_420"])
    if isinstance(raw_ma_420, list):
        settings["ma_420"] = any(bool(v) for v in raw_ma_420)
    else:
        settings["ma_420"] = bool(raw_ma_420)
    return settings


class CN0582DAQ:
    """Acquisition adapter over a CN0582 board.

    Samples are returned as volts at the BNC on all four channels, shape (4, N),
    alongside a matching time axis -- the same contract the logger tab used for
    the LAN-XI. Recordings stream to a raw .bin first and are decoded afterwards,
    which is what keeps the capture gapless at 8.192 MB/s.
    """

    def __init__(self, dev, gains, coupling, iepe_mask, bias_channels, temp_dir,
                 bias_values=None, ma_420_enabled=False, invert=None,
                 store_rate_hz=None,
                 settings_path=CN0582_SETTINGS_FILE):
        self.dev = dev
        self.gains = list(gains)
        self.coupling = [1 if bool(v) else 0 for v in coupling]
        self.iepe_mask = int(iepe_mask) & 0x0F
        self.bias_channels = sorted({int(ch) for ch in bias_channels if 0 <= int(ch) < 4})
        self.bias_values = ([float(v) for v in bias_values]
                            if bias_values is not None else [11000.0] * 4)
        self.ma_420_enabled = bool(ma_420_enabled)
        # Per-channel sign flip, applied on decode. Some sensors are wired the
        # other way round -- the load cell on AI1 reads negative for a downward
        # force -- and it is the recorded sign that has to be right.
        self.invert = ([bool(v) for v in invert] if invert is not None
                       else [False] * 4)
        # Rate each channel is written to the parquet at, 0 meaning the full
        # acquisition rate. A load cell on AI1 changes far too slowly to be worth
        # 256 kSPS of file, and the samples are decimated at save time only --
        # what is captured, plotted and played back stays at the full rate.
        rates = ([int(v) for v in store_rate_hz] if store_rate_hz is not None
                 else [0] * 4)
        self.store_rate_hz = [hz if ch == CN0582_LOADCELL_CHANNEL else 0
                              for ch, hz in enumerate(rates)]
        self.temp_dir = temp_dir
        self.settings_path = settings_path
        self.sample_rate = int(cn0582.FS_HZ)
        self.startup_bias_done = threading.Event()
        self.startup_bias_done.set()

        # Over-range flags from the most recent acquisition, shape (4, N) bool,
        # or None when nothing has been recorded yet.
        self.last_saturated = None
        self._biased = False

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    @property
    def full_scale_v(self):
        """Full-scale input in volts per channel, given the configured PGA gains.

        The front end is 0.3 (level shift) x 2.667 (FDA) x G, so a +-VREF ADC swing
        corresponds to +-VREF / (0.8 G) at the BNC.
        """
        return [cn0582.VREF / (0.8 * max(g, 1)) for g in self.gains]

    def _selected_channels(self, flags):
        return [ch for ch, enabled in enumerate(flags) if enabled]

    def snapshot_settings(self):
        return {
            "gains": list(self.gains),
            "coupling": list(self.coupling),
            "bias_mv": list(self.bias_values),
            "current_source": _current_source_flags_from_mask(self.iepe_mask),
            "invert": list(self.invert),
            "store_rate_hz": list(self.store_rate_hz),
            "auto_bias_on_startup": [ch in self.bias_channels for ch in range(4)],
            "ma_420": self.ma_420_enabled,
        }

    def save_settings(self):
        cfg = self.snapshot_settings()
        tmp = self.settings_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(cfg, handle, indent=2)
            os.replace(tmp, self.settings_path)
        except Exception as err:
            print(f"CN0582: could not save settings: {err}")

    def set_channel_gain(self, ch, gain):
        gain = int(gain)
        if gain not in cn0582.GAINS:
            raise ValueError(f"gain must be one of {cn0582.GAINS}")
        self.gains[ch] = gain
        self.dev.set_gains(self.gains)
        self._biased = False
        self.save_settings()

    def set_channel_coupling(self, ch, ac_coupled):
        self.coupling[ch] = 1 if ac_coupled else 0
        self.dev.set_coupling(ch, ac_coupled=bool(ac_coupled))
        self._biased = False
        self.save_settings()

    def set_channel_bias(self, ch, mv):
        mv = float(mv)
        self.dev.set_dc_bias(ch, mv)
        self.bias_values[ch] = mv
        self._biased = False
        self.save_settings()

    def set_current_source_enabled(self, ch, enabled):
        # CH3 has a dedicated 4-20 mA path; keep it exclusive with IEPE power.
        if ch == 3 and enabled and self.ma_420_enabled:
            self.ma_420_enabled = False
            self.dev.set_4_20ma(False)
        bit = _current_source_bitmask_for_channel(ch)
        if enabled:
            self.iepe_mask |= bit
        else:
            self.iepe_mask &= ~bit
        # one byte, addressed at this channel - the board has no mask
        self.dev.set_current_source(ch, enabled)
        self._biased = False
        self.save_settings()

    def set_4_20ma_enabled(self, enabled):
        self.ma_420_enabled = bool(enabled)
        if self.ma_420_enabled:
            self.iepe_mask &= ~_ch3_current_source_bit()
        self.dev.set_4_20ma(self.ma_420_enabled)
        self.dev.set_current_sources(self.iepe_mask)
        self._biased = False
        self.save_settings()

    def set_channel_invert(self, ch, enabled):
        """Flip the sign of a channel from the next capture onward.

        Purely a decode-time operation: the front end and the auto-bias search
        both keep working on the true signal, so a flipped channel still centres
        on the same DAC setting.
        """
        self.invert[ch] = bool(enabled)
        self.save_settings()

    def set_channel_store_rate(self, ch, hz):
        """Set the rate this channel is stored at; 0 keeps the acquisition rate.

        Refuses to reduce anything but the load-cell channel: the others carry
        audio, where throwing away bandwidth is never what anyone wanted.
        """
        hz = int(hz)
        if hz not in STORE_RATE_CHOICES:
            raise ValueError(f"store rate must be one of {STORE_RATE_CHOICES}")
        if hz and ch != CN0582_LOADCELL_CHANNEL:
            raise ValueError(f"ch{ch} carries audio and is always stored at the "
                             f"full rate; only ch{CN0582_LOADCELL_CHANNEL} "
                             f"(the load cell) may be reduced")
        if hz and self.sample_rate % hz:
            raise ValueError(f"{hz} Hz does not divide {self.sample_rate} Hz exactly")
        self.store_rate_hz[ch] = hz
        self.save_settings()

    def set_auto_bias_on_startup(self, ch, enabled):
        channels = set(self.bias_channels)
        if enabled:
            channels.add(ch)
        else:
            channels.discard(ch)
        self.bias_channels = sorted(channels)
        self.save_settings()

    def apply_config(self):
        """Push gains, coupling and current sources to the board."""
        if self.ma_420_enabled:
            self.iepe_mask &= ~_ch3_current_source_bit()
        self.dev.set_gains(self.gains)
        for ch, ac in enumerate(self.coupling):
            self.dev.set_coupling(ch, ac_coupled=bool(ac))
        for ch, mv in enumerate(self.bias_values):
            self.dev.set_dc_bias(ch, mv)
        self.dev.set_4_20ma(self.ma_420_enabled)
        self.dev.set_current_sources(self.iepe_mask)
        powered = [ch for ch, enabled in enumerate(
            _current_source_flags_from_mask(self.iepe_mask)) if enabled]
        print(f"CN0582: gains={self.gains} coupling={self.coupling} "
              f"bias={self.bias_values} 4-20mA={self.ma_420_enabled} "
              f"IEPE mask=0x{self.iepe_mask:02X} powered={powered}")

    def biasable_channels(self, channels=None):
        """The channels an automatic bias is allowed to touch.

        The per-channel "auto-bias on startup" tick is the authority, and it holds
        for every automatic path -- startup and the per-channel Auto button alike.
        An unticked channel keeps the bias in the panel whatever happens: channel 1
        carries the load cell at a fixed 4000 mV, and a search would walk the DAC
        across its whole range and destroy that setting.
        """
        requested = (self.bias_channels if channels is None
                     else [int(ch) for ch in channels if 0 <= int(ch) < 4])
        return [ch for ch in requested if ch in self.bias_channels]

    def auto_bias(self, force=False, progress=None, channels=None):
        """Centre the front end on every channel ticked for auto-bias.

        The level-shift DAC comes out of reset parked near the negative rail, so an
        unbiased channel reads about -3.9 V and clips on the smallest signal.

        Every selected channel is searched, always. Measuring the DC level first and
        skipping the ones that look centred was tried and reverted: a stored bias
        can be right for an unpowered sensor and badly wrong once the current source
        has it running, and the reading does not distinguish the two.

        progress: optional callable(channel, index, total) fired before each channel.
        Returns {"searched": [...]}.
        """
        selected = self.biasable_channels(channels)
        result = {"searched": []}
        if self._biased and not force and channels is None:
            return result
        if not selected:
            self._biased = True
            return result

        total = len(selected)
        for i, ch in enumerate(selected):
            if progress is not None:
                progress(ch, i, total)
            mv = self.dev.auto_bias(ch)
            self.bias_values[ch] = float(mv)
            result["searched"].append(ch)
            print(f"CN0582: channel {ch} biased at {mv:.0f} mV")
        self._biased = True
        self.save_settings()
        return result

    # ------------------------------------------------------------------
    # acquisition
    # ------------------------------------------------------------------

    def _raw_path(self):
        return os.path.join(self.temp_dir, f"_cn0582_{int(time.time() * 1000)}.bin")

    def _decode(self, path):
        """Decode a raw capture to (time_axis, volts[4, N]) and drop the .bin."""
        try:
            data, hdr = cn0582.decode_file(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        self.last_saturated = cn0582.saturated(hdr)

        # to_volts_input applies one PGA gain to the whole array, but gain is
        # per channel here, so scale each row with its own.
        volts = cn0582.to_volts_adc(data)
        for ch, g in enumerate(self.gains):
            sign = -1.0 if self.invert[ch] else 1.0
            volts[ch] *= sign / (0.8 * max(g, 1))

        n = volts.shape[1]
        time_axis = np.arange(n, dtype=np.float64) / self.sample_rate
        return time_axis, volts

    def SampleChannels(self, duration, on_ready=None):
        """Record `duration` seconds on all four channels.

        on_ready fires the moment the stream is running, which is what the load-cell
        collector and the STWINMA2 gate hang off.

        Returns (time_axis, data[4, N]) with data in volts at the BNC.
        """
        self.auto_bias()
        path = self._raw_path()
        self.dev.record(seconds=duration, path=path, on_ready=on_ready)
        return self._decode(path)

    def SampleUntil(self, stop_event, on_ready=None):
        """Record until stop_event is set, with no seam anywhere in the capture.

        Returns (time_axis, data[4, N]) in volts at the BNC. At 8.192 MB/s on the
        wire and 4 x float64 in memory after decoding, a long recording gets big
        fast -- roughly 1 GB of RAM per minute captured.
        """
        self.auto_bias()
        path = self._raw_path()
        self.dev.record_until(stop_event, path=path, on_ready=on_ready)
        return self._decode(path)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def reset_stream(self):
        """Resynchronise the endpoints after an aborted transfer."""
        self.dev.resync()
        self.apply_config()

    def close_stream(self):
        try:
            self.dev.suspend()
        except Exception:
            pass
        self.dev.close()


def init_cn0582(temp_dir):
    """Connect to the EVAL-CN0582-USBZ and apply the configured front-end setup.

    Returns (daq, sample_rate, available):
        daq         -- CN0582DAQ instance, or None when unavailable
        sample_rate -- 256000, whether or not the board was found
        available   -- True when the board is connected and configured
    """
    try:
        dev = cn0582.CN0582()
        dev.init_board()

        gains = _parse_int_list(os.getenv("CN0582_GAINS"), CN0582_DEFAULT_GAINS)
        coupling = _parse_int_list(os.getenv("CN0582_COUPLING"), CN0582_DEFAULT_COUPLING)
        raw_mask = os.getenv("CN0582_IEPE_MASK")
        try:
            iepe_mask = int(raw_mask, 0) if raw_mask else CN0582_DEFAULT_IEPE_MASK
        except ValueError:
            print(f"CN0582: cannot parse CN0582_IEPE_MASK={raw_mask!r}, "
                  f"using 0x{CN0582_DEFAULT_IEPE_MASK:02X}")
            iepe_mask = CN0582_DEFAULT_IEPE_MASK

        bias_channels = _parse_bias_channels(os.getenv("CN0582_BIAS_CHANNELS"))
        defaults = {
            "gains": gains,
            "coupling": coupling,
            "bias_mv": [11000.0, 11000.0, 11000.0, 11000.0],
            "current_source": _current_source_flags_from_mask(iepe_mask),
            "invert": [False, False, False, False],
            "store_rate_hz": [CN0582_LOADCELL_STORE_RATE
                              if ch == CN0582_LOADCELL_CHANNEL else 0
                              for ch in range(4)],
            "auto_bias_on_startup": [ch in bias_channels for ch in range(4)],
            "ma_420": False,
        }
        persisted = _read_persisted_settings(defaults)
        current_mask = _current_source_mask_from_flags(persisted["current_source"])
        startup_bias_channels = [ch for ch, enabled in enumerate(
            persisted["auto_bias_on_startup"]) if enabled
        ]

        daq = CN0582DAQ(
            dev,
            persisted["gains"],
            persisted["coupling"],
            current_mask,
            startup_bias_channels,
            temp_dir,
            bias_values=persisted["bias_mv"],
            ma_420_enabled=persisted["ma_420"],
            invert=persisted["invert"],
            store_rate_hz=persisted["store_rate_hz"],
        )
        daq.apply_config()

        atexit.register(daq.close_stream)
        signal.signal(signal.SIGINT, lambda _s, _f: (daq.close_stream(), sys.exit(0)))
        print(f"CN0582 connected: serial {dev.read_serial()}")
        return daq, daq.sample_rate, True
    except Exception as err:
        print(f"CN0582 not available: {err}")
        return None, CN0582_SAMPLE_RATE, False
