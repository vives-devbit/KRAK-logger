"""Noise characterisation for CN0582 recordings.

Answers one question: is the noise floor coupled from outside (shielding will
help) or intrinsic to the ADC/PGA (shielding will not)?

Two ways in:

    # analyse a recording the GUI already made
    python noise_check.py --parquet temp_files/20260907_112113-(TEST).parquet

    # record a fresh clip straight from the board
    python noise_check.py --capture 5 --gains 1,1,20,20

Run it twice -- once with the sensors connected, once with the BNCs terminated
(50 ohm or a short) -- and compare. The terminated run is the instrument's own
floor; whatever the connected run adds on top of it came in through the air or
the cables.

    python noise_check.py --parquet terminated.parquet --ref connected.parquet
"""

import argparse
import json
import os
import sys

import numpy as np
from scipy import signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from HelpFunctions.cn0582 import VREF  # noqa: E402

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "cn0582_settings.json")

# Spur families worth naming. Anything sitting well above the local floor at
# these frequencies has a known cause, and only some of them care about a box.
SPUR_FAMILIES = [
    ("mains", 50.0, 12, "ground loop / E-field -- shielding + grounding help"),
    ("USB SOF", 1000.0, 8, "USB frame rate, coupled on-board -- box won't help"),
    ("USB microframe", 8000.0, 4, "USB high-speed microframe -- box won't help"),
]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_settings():
    """The GUI's saved CN0582 front-end configuration."""
    try:
        with open(SETTINGS_FILE) as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"  (could not read {SETTINGS_FILE}: {exc}; assuming defaults)")
        return {}


def load_gains(override=None):
    """PGA gain per CN0582 channel, from the settings file unless overridden."""
    if override:
        return [int(g) for g in override.split(",")]
    gains = load_settings().get("gains")
    return [int(g) for g in gains] if gains else [1, 1, 1, 1]


def full_scale_v(gain):
    """Full-scale input at the BNC for a PGA gain, matching cn0582_daq."""
    return VREF / (0.8 * max(gain, 1))


def load_parquet(path):
    """Return {name: (samples_in_volts, fs)} for the CN0582 channels."""
    import pandas as pd

    df = pd.read_parquet(path)
    attrs = df.attrs or {}
    rates = attrs.get("Channel Sample Rates (Hz)", {})
    default_fs = float(attrs.get("Sample Rate (Hz)", 256000))

    out = {}
    for name in df.columns:
        if name.startswith("AI04"):
            continue  # STWINMA2, normalised units -- not a CN0582 channel
        x = df[name].to_numpy(dtype=np.float64)
        x = x[np.isfinite(x)]  # shorter channels are NaN-padded to full length
        if x.size < 1024:
            continue
        if "(mV)" in name:
            x = x / 1000.0
        out[name] = (x, float(rates.get(name, default_fs)))
    return out


def capture(seconds, gains, bin_path):
    """Record a fresh clip, reproducing the GUI's saved front-end configuration.

    Same gains, coupling, DC bias and IEPE mask the logger would have used, so a
    capture here is directly comparable with one the GUI made.
    """
    import time

    from HelpFunctions import cn0582 as drv

    st = load_settings()
    coupling = st.get("coupling", [1, 1, 1, 1])
    bias_mv = st.get("bias_mv", [0.0] * 4)
    sources = st.get("current_source", [False] * 4)
    mask = sum(1 << ch for ch, on in enumerate(sources) if on)

    with drv.CN0582() as dev:
        print(f"  serial:  {dev.read_serial()}")
        for ch, g in enumerate(gains):
            dev.set_gain(ch, g)
            dev.set_coupling(ch, ac_coupled=bool(coupling[ch]))
            if bias_mv[ch]:
                dev.set_dc_bias(ch, float(bias_mv[ch]))
        dev.set_current_sources(mask)
        print(f"  gains={gains} coupling={coupling} IEPE mask=0x{mask:02X}")
        time.sleep(1.0)  # let the AC-coupling networks and bias settle
        print(f"  recording {seconds:g} s -> {bin_path}")
        dev.record(seconds=seconds, path=bin_path)

    data, hdr = drv.decode_file(bin_path)
    for ch in range(4):
        n_sat = int(drv.saturated(hdr[ch]).sum())
        if n_sat:
            print(f"  ! AI{ch}: {n_sat} over-range samples -- reduce that gain")
    return {f"AI{ch} (V)": (drv.to_volts_input(data[ch], gains[ch]),
                            float(drv.FS_HZ))
            for ch in range(4)}


def save_npz(path, channels, gains):
    """Store a run so a later one can be compared against it."""
    payload = {f"{name}|data": x for name, (x, _fs) in channels.items()}
    payload.update({f"{name}|fs": np.array(fs) for name, (_x, fs) in channels.items()})
    payload["gains"] = np.array(gains)
    np.savez_compressed(path, **payload)
    print(f"\nsaved {path} ({os.path.getsize(path) / 1e6:.1f} MB)")


def load_any(path):
    """Load either a GUI parquet or an .npz saved by this script."""
    if path.endswith(".npz"):
        z = np.load(path)
        names = sorted({k.split("|")[0] for k in z.files if "|" in k})
        return {n: (z[f"{n}|data"], float(z[f"{n}|fs"])) for n in names}
    return load_parquet(path)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def psd(x, fs):
    """Welch PSD in V^2/Hz with ~1 Hz bins, Hann window, 50 % overlap."""
    nperseg = int(min(len(x), max(4096, 2 ** np.ceil(np.log2(fs)))))
    f, p = signal.welch(x - x.mean(), fs=fs, nperseg=nperseg,
                        noverlap=nperseg // 2, window="hann")
    return f, p


def band_rms(f, p, lo, hi):
    """RMS in volts over [lo, hi] by integrating the PSD."""
    m = (f >= lo) & (f <= hi)
    if not m.any():
        return float("nan")
    return float(np.sqrt(np.trapezoid(p[m], f[m])))


def noise_density(f, p, lo, hi):
    """Median noise density in V/sqrt(Hz) over a band -- median ignores spurs."""
    m = (f >= lo) & (f <= hi)
    if not m.any():
        return float("nan")
    return float(np.sqrt(np.median(p[m])))


def find_spurs(f, p, base, count, min_db=6.0):
    """Harmonics of `base` standing at least min_db above the local floor."""
    df = f[1] - f[0]
    hits = []
    for n in range(1, count + 1):
        target = base * n
        if target >= f[-1]:
            break
        k = int(round(target / df))
        # local floor = median of a window either side, excluding the peak
        half = max(3, int(round(base * 0.4 / df)))
        lo, hi = max(0, k - 10 * half), min(len(f), k + 10 * half)
        neigh = np.concatenate([p[lo:max(lo, k - half)], p[min(hi, k + half):hi]])
        if neigh.size == 0:
            continue
        peak = p[max(0, k - 2):k + 3].max()
        floor = np.median(neigh)
        if floor <= 0:
            continue
        db = 10 * np.log10(peak / floor)
        if db >= min_db:
            hits.append((target, db))
    return hits


def mains_harmonics(f, p, base=50.0, count=6, halfwidth=2.0):
    """Absolute RMS in volts inside a narrow window on each mains harmonic.

    This is the number to watch across a plugged-in / unplugged pair: a ground
    loop through the charger shows up here and almost nowhere else.
    """
    out = []
    for n in range(1, count + 1):
        fc = base * n
        if fc >= f[-1]:
            break
        out.append((fc, band_rms(f, p, fc - halfwidth, fc + halfwidth)))
    return out


def analyse(channels, gains, band, label):
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    results = {}
    for i, (name, (x, fs)) in enumerate(sorted(channels.items())):
        ch = int(name[2]) if name[2].isdigit() else i
        gain = gains[ch] if ch < len(gains) else 1
        fs_v = full_scale_v(gain)

        f, p = psd(x, fs)
        lo, hi = band[0], min(band[1], fs / 2 * 0.98)
        rms_band = band_rms(f, p, lo, hi)
        rms_wide = band_rms(f, p, f[1], fs / 2 * 0.98)
        # quiet stretch well above mains and below any anti-alias roll-off
        dens = noise_density(f, p, min(20e3, fs / 4), min(80e3, fs / 2 * 0.9))

        print(f"\n{name}   PGA gain {gain}   full scale +-{fs_v:.3f} V")
        print(f"  DC                {x.mean() * 1e3:+10.3f} mV")
        print(f"  peak              {np.abs(x).max() * 1e3:10.3f} mV"
              f"   = {np.abs(x).max() / fs_v * 100:5.1f} % of FS"
              f"   ({20 * np.log10(max(np.abs(x).max(), 1e-12) / fs_v):+6.1f} dBFS)")
        print(f"  RMS {lo:>6.0f}-{hi:<7.0f} Hz {rms_band * 1e6:10.2f} uVrms"
              f"   -> SNR vs FS {20 * np.log10(fs_v / max(rms_band, 1e-15)):6.1f} dB")
        print(f"  RMS wideband      {rms_wide * 1e6:10.2f} uVrms"
              f"   -> SNR vs FS {20 * np.log10(fs_v / max(rms_wide, 1e-15)):6.1f} dB")
        print(f"  noise density     {dens * 1e9:10.1f} nV/sqrt(Hz)  (median, "
              f"{min(20e3, fs / 4) / 1e3:.0f}-{min(80e3, fs / 2 * 0.9) / 1e3:.0f} kHz)")

        penalty = 10 * np.log10((fs / 2) / (hi - lo))
        print(f"  bandwidth penalty {penalty:10.1f} dB"
              f"   (wideband vs {lo:.0f}-{hi:.0f} Hz -- mind this when "
              f"comparing to a band-limited instrument)")

        for fam, base, count, note in SPUR_FAMILIES:
            hits = find_spurs(f, p, base, count)
            if hits:
                worst = max(hits, key=lambda h: h[1])
                print(f"  ! {fam:<14} {len(hits)} harmonic(s), worst "
                      f"{worst[0]:.0f} Hz at +{worst[1]:.1f} dB over floor")
                print(f"    {' ' * 14} {note}")

        harm = mains_harmonics(f, p)
        total = np.sqrt(sum(v ** 2 for _fc, v in harm))
        print("  mains harmonics   " +
              "  ".join(f"{fc:.0f}Hz {v * 1e6:.1f}uV" for fc, v in harm))
        # The number that actually decides whether chasing mains is worth it:
        # noise adds in power, so a spur at 27 % of the RMS amplitude is only
        # 7 % of the power, and deleting it buys a third of a dB.
        frac = min((total / max(rms_band, 1e-15)) ** 2, 0.999999)
        print(f"  mains total       {total * 1e6:10.2f} uVrms"
              f"   = {total / max(rms_band, 1e-15) * 100:5.1f} % of in-band RMS,"
              f" {frac * 100:4.1f} % of power")
        print(f"  -> killing mains entirely would gain "
              f"{-10 * np.log10(1 - frac):.2f} dB here")

        results[name] = dict(rms_band=rms_band, rms_wide=rms_wide, dens=dens,
                             harm=harm, mains_total=total)
    return results


def compare(ref, cur, band, ref_label="reference", cur_label="current"):
    """In-band noise of the two runs side by side.

    Whatever the noisier run has on top of the quieter one arrived through
    whatever changed between them -- the charger's ground path, the cabling,
    or the air.
    """
    print(f"\n{'=' * 78}\nIN-BAND NOISE: {ref_label}  vs  {cur_label}\n{'=' * 78}")
    for name in sorted(set(ref) & set(cur)):
        a, b = ref[name]["rms_band"], cur[name]["rms_band"]
        if not (np.isfinite(a) and np.isfinite(b)) or min(a, b) <= 0:
            continue
        delta = 20 * np.log10(b / a)
        print(f"\n{name}: {a * 1e6:8.2f} -> {b * 1e6:8.2f} uVrms  ({delta:+.1f} dB)")
        if b >= a:
            print(f"    no improvement -- the floor is intrinsic here")
            continue
        removed = np.sqrt(max(a ** 2 - b ** 2, 0.0))
        share = (removed ** 2) / (a ** 2) * 100
        verdict = ("worth chasing" if share > 50 else
                   "mostly intrinsic -- not where the SNR is going")
        print(f"    removed {removed * 1e6:8.2f} uVrms = {share:.0f} % of the "
              f"original noise power -> {verdict}")


def compare_mains(ref, cur, ref_label, cur_label):
    """Mains harmonic amplitudes side by side -- the ground-loop verdict."""
    print(f"\n{'=' * 78}\nMAINS HARMONICS: {ref_label}  vs  {cur_label}"
          f"\n{'=' * 78}")
    for name in sorted(set(ref) & set(cur)):
        a, b = ref[name]["mains_total"], cur[name]["mains_total"]
        if not (np.isfinite(a) and np.isfinite(b)) or min(a, b) <= 0:
            continue
        delta = 20 * np.log10(b / a)
        print(f"\n{name}: {a * 1e6:8.2f} -> {b * 1e6:8.2f} uVrms  ({delta:+.1f} dB)")
        for (fc, va), (_fc, vb) in zip(ref[name]["harm"], cur[name]["harm"]):
            if va > 0 and vb > 0:
                print(f"    {fc:5.0f} Hz  {va * 1e6:8.2f} -> {vb * 1e6:8.2f} uV"
                      f"  ({20 * np.log10(vb / va):+6.1f} dB)")
        if delta <= -6:
            print("    -> mains noise dropped a lot: that was a ground loop "
                  "through the charger, not radiated pickup")
        elif delta >= 6:
            print("    -> mains noise rose a lot")
        else:
            print("    -> little change: the mains pickup is not coming from "
                  "the charger's ground path")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--parquet", help="recording to analyse (.parquet or .npz)")
    src.add_argument("--capture", type=float, metavar="SECONDS",
                     help="record a fresh clip from the board")
    ap.add_argument("--ref", help="second recording to compare against")
    ap.add_argument("--save", help="write the analysed run to this .npz "
                                   "so a later run can be compared to it")
    ap.add_argument("--gains", help="PGA gains 'a,b,c,d' (default: settings file)")
    ap.add_argument("--band", nargs=2, type=float, default=[20.0, 25000.0],
                    metavar=("LO", "HI"),
                    help="comparison band in Hz (default 20 25000, i.e. what a "
                         "band-limited instrument would see)")
    ap.add_argument("--bin", default=None,
                    help="where to put the raw capture (default: alongside --save)")
    a = ap.parse_args()

    gains = load_gains(a.gains)

    if a.capture:
        bin_path = a.bin or ((a.save or "capture").rsplit(".", 1)[0] + ".bin")
        primary = capture(a.capture, gains, bin_path)
        label = f"FRESH CAPTURE ({a.capture:g} s)"
    else:
        primary = load_any(a.parquet)
        label = os.path.basename(a.parquet)

    first = analyse(primary, gains, a.band, label)

    if a.save:
        save_npz(a.save, primary, gains)

    if a.ref:
        ref_label = os.path.basename(a.ref)
        second = analyse(load_any(a.ref), gains, a.band, ref_label)
        compare(second, first, a.band, ref_label, label)
        compare_mains(second, first, ref_label, label)


if __name__ == "__main__":
    main()
