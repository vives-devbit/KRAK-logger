# CN0582 measurement setup and sensor calibration

Practical companion to [CN0582_USB_API_reverse_engineering.md](CN0582_USB_API_reverse_engineering.md).
That document covers the USB protocol; this one covers **how to configure a channel
for a given sensor, and what the numbers mean**. Every figure here was measured on
this board (serial `ADI202302150064`), not taken from a datasheet.

---

## 1. The one thing to get right first: bias

The AD5686R level-shift DAC comes out of reset parking the front end within ~180 mV
of the negative rail. An unbiased channel reads about **-3.9 V and clips on anything**.
Setting the bias is not optional.

`V_adc` is linear in the bias setting. Measured on ch0 with an open input:

```text
V_adc = 0.000735 * bias_mV - 1.336        r = 0.9998
```

The slope differs per channel and with whatever is connected (measured range
0.0006 to 0.0028 V per mV), so **do not reuse another channel's number** - use
`auto_bias()` or the GUI's **Auto** button, which bisects on the sign of the error
and is repeatable to 0 mV across runs.

### The 11000 mV trap

The vendor GUI defaults every channel to 11000 mV. That value exists to cancel an
**IEPE sensor's ~10 V standing bias** and is correct only for a DC-coupled IEPE
channel. On anything else it rails the channel at +4.096 V all by itself, and the
channel "registers nothing".

| ch0 configuration at bias 11000 mV | result |
|---|---|
| DC coupled, IEPE on | -0.96 V, works |
| DC coupled, IEPE off | **railed** |
| AC coupled, IEPE on | **railed** |
| AC coupled, IEPE off | **railed** |

### Negative bias is legal and useful

The DAC code at 0 mV is 25408, not 0, so there is room below it. Verified working
down to -5000 mV (code 10284); -6000 mV rails. Negative bias shifts the operating
point down and buys **positive** input range - use it for a sensor with a large
positive-going output.

---

## 2. Scaling

```text
V_adc  = code / 2^23 * 4.096          1 LSB = 0.4883 uV
V_bnc  = V_adc / (0.8 * gain)         front end = 0.3 level shift x 2.667 FDA x G_PGA
```

Full scale is +/-4.096 V at the ADC, so at gain 1 the input range is **+/-5.12 V at
the BNC**, centred wherever the bias puts it. Gain only ever *reduces* usable range:

| gain | input range at the BNC |
|---|---|
| 1 | +/-5.12 V |
| 2 | +/-2.56 V |
| 5 | +/-1.02 V |
| 10 | +/-0.51 V |
| 20 | +/-0.26 V |

Gain 0 exists in the LTC6910 and means **muted**, not unity.

---

## 3. Channel 0 - B&K CCLD accelerometer

```text
AC coupling  ON
IEPE source  ON
bias         ~2190 mV
gain         1   (10 is better if the signal allows - see below)
```

**Use AC coupling.** With the current source on, the sensor sits at a standing bias
that AC coupling blocks. Measured: DC-coupled needs 10492 mV to centre, AC-coupled
needs 2189 mV - the 8303 mV difference *is* the sensor's standing bias.

| configuration | bias to centre | noise |
|---|---|---|
| AC, IEPE on | **2189 mV** | **45 uVrms** |
| AC, IEPE off | 2148 mV | 147 uV |
| DC, IEPE off | 1861 mV | 103 uV |
| DC, IEPE on | 10492 mV | 393 uV, converges poorly |

AC + IEPE on is both correctly centred and the quietest. The bias is repeatable to
0 mV across runs, barely moves with gain (fixed 2190 mV holds from gain 1 to 100
without clipping), and shifts only 21 mV when the current source is toggled - so
**find it once and type it in from then on**.

### Sensor health check

With DC coupling, compare the bias needed with the current source off vs on. The
difference is the sensor's standing bias:

```text
IEPE off -> 1858 mV
IEPE on  -> 9397 mV
standing bias = 7.54 V
```

Healthy IEPE sensors sit around 8-14 V. Near 0 V means a shorted element; near the
compliance voltage means an open circuit. 7.54 V is slightly low, and the microphone
later read lower still (2.81 V) - see section 10. The current source has since been
measured at ~3.5 mA against a specified 4 mA, which is too close to explain either
reading, so the cause of the low standing bias is still open.

### Bandwidth limit: the sensor, not the DAQ

The mounted resonance is at **40-42 kHz** and dominates everything above ~30 kHz.
Confirmed by exciting it two independent ways - cracking chips and a tap on the
surface both produce the same narrow peak ~22 dB above its neighbours:

| freq | crackle | tap on surface |
|---|---|---|
| 20 kHz | 13.1 dB | 2.2 dB |
| **40 kHz** | **32.9 dB** | **25.3 dB** |
| **42 kHz** | **35.2 dB** | **23.2 dB** |
| 50 kHz | 15.8 dB | 2.5 dB |

Two unrelated sources cannot share a narrow peak unless it belongs to the measuring
chain. **Low-pass at ~30 kHz before spectral analysis**, or you are plotting sensor
ring. For event *detection* the resonance is helpful - it amplifies broadband
emission into an easily-triggered burst.

### Mounting matters more than anything else

Hand-holding the sensor injects broadband energy of its own and roughly triples the
noise floor (836 uVrms hand-held vs 64 uVrms surface-mounted). It also makes
transients non-repeatable, because contact stiffness varies with grip. Use wax,
adhesive or a stud. The number to watch is the resonance-band share of total RMS:
**17.7 % hand-held** in one recording, and it should fall well below that with a
rigid mount.

---

## 4. Channel 1 - amplified load cell (DC, voltage output)

```text
AC coupling  OFF
IEPE source  OFF       <-- critical
bias         4000 mV   (4400 for maximum compression range)
gain         1
```

**The current source must be off.** A load cell is not IEPE; injecting 2-4 mA into a
voltage-output amplifier can damage it and will certainly push it out of range. This
was the cause of an early "out of range at 0 mV" symptom.

### Calibration

```text
sensitivity  16.10 mV/N          (measured: 7.726 V for a calibrated 480 N)
500 N        -> 8.05 V
noise floor  0.060 N rms         (0.012 % of a 480 N range)
```

Output is **negative-going under compression**, so a **positive** bias is what buys
compression range.

Bias transfer function measured with the load cell attached:

```text
V_adc = 0.000801 * bias_mV + 0.0815
```

| bias | compression | tension / zero drift |
|---|---|---|
| 3500 mV | 542 N | 94 N |
| **4000 mV** | **573 N** | **63 N** |
| 4400 mV | 598 N | 38 N |

**4000 mV is the recommended balance** for a 480 N working range. At 4400 mV the
tension side is down to ~38 N, and the unloaded baseline was observed to drift 70 mV
(~4 N) between sessions - that drift eats the direction you have least of. If you
see over-range at the *top* rather than the bottom, lower the bias.

**No voltage divider is needed.** A verified 481.8 N capture had 0 over-range samples
and 116 N of headroom remaining.

### Reading force from a capture

```python
import cn0582, numpy as np
data, hdr = cn0582.decode_file("captures/....bin")
v1    = cn0582.to_volts_input(data[1], 1.0)     # volts at the BNC
force = (np.median(v1) - v1) / 0.01610          # N
assert not cn0582.saturated(hdr[1]).any()       # always check
```

For a single figure per test, use the mean over the hold window rather than the
instantaneous peak - the peak is one sample and always reads slightly high. In the
481.8 N capture the signal stayed above 95 % of peak for 77.3 ms, mean 471.4 N,
sd 7.46 N.

---

## 5. B&K 4966-H-041 microphone (CCLD) - planning figures

Electrically compatible: CCLD is B&K's name for the same constant-current interface
as IEPE/ICP. Sensitivity **46.5 mV/Pa** (-26.6 dB re 1 V/Pa).

| gain | max SPL | noise floor |
|---|---|---|
| 1 | 134.8 dB | 48.1 dB |
| 5 | 120.8 dB | 34.2 dB |
| **10** | **114.8 dB** | **28.1 dB** |
| 20 | 108.8 dB | 22.1 dB |
| 50 | 100.8 dB | 14.2 dB |

Gain 10-20 is the sensible starting point. The noise figures assume the ADC floor
keeps dividing with gain, which stops being true once the preamp dominates - the
4966 is specified around 16-17 dB(A), so expect to bottom out in the 20s.

Setup: AC coupling on, current source on, **press Auto** - the mic's bias differs
from the accelerometer's, so a stored 2190 mV will not be right.

These are planning figures from the sensitivity alone. For what was actually measured
once the mic was on ch0, see section 9; for the bias problem it revealed, section 10.

---

## 6. Things that bite

**Unused channels rail.** Any channel whose bias was never set sits pinned at
+/-4.096 V for the whole recording and carries no usable data. Harmless, but do not
let downstream code read it. Press Auto on every channel you intend to use.

**All four channels are always recorded.** The `.bin` is the raw EP 0x81 stream,
which is bit-parallel across the AD7768-4's four DOUT lines - a single byte carries
all four. `decode_file()` returns `(4, N)`. Only the *display* and the WAV export are
single-channel.

**The WAV is lossy.** DC removed, peak-normalised, band-limited, one channel. It is a
monitoring aid. **The `.bin` is the measurement** - anything quantitative comes from
`decode_file()` or CSV export.

**Check the over-range flag, not the graph.** `cn0582.saturated(hdr[c])` is a
per-sample hardware flag from the ADC. A plot at 400 samples per pixel shows spikes
that look like clipping and are not; a genuinely clipped peak sits at one identical
value for many samples while a real peak is a single sample with smooth neighbours.

**Headroom is worth spending.** A recording peaking at 4 % of full scale wastes 27.8 dB
of SNR. Check the peak after a test run and raise the gain if there is room - but leave
margin for impulsive signals, which have high crest factors (39.6 measured on one
crackle recording).

---

## 7. Fixed constants

| | |
|---|---|
| Sample rate | 256 kSPS/channel (measured +65 ppm) |
| Resolution | 24-bit two's complement, 1 LSB = 0.4883 uV |
| VREF | 4.096 V |
| Channels | 4, simultaneous, always all recorded |
| ADC filter | sinc5, -3 dB near 52 kHz |
| Wire rate | 8.192 MB/s (~8.2 MB per second of capture) |
| Front-end gain | 0.8 x G_PGA |
| PGA gains | 0 (mute), 1, 2, 5, 10, 20, 50, 100 |
| Idle noise floor | ~180 uVrms at the ADC, open input, gain 1 |

---

## 8. Does raising the gain improve SNR?

**Only while the ADC's noise dominates.** The PGA sits *before* the ADC, so gain lifts
the signal above ADC noise - but it amplifies everything already present at the input
along with the signal.

Two measurements on this board, same channel, different conditions:

| condition | noise @ ADC, gain 1 | best input-referred | verdict |
|---|---|---|---|
| accelerometer, quiet room | 74.9 uV | 32.3 uV at gain 20 | **2.9x = 9.3 dB gained** |
| microphone, normal room | 1134 uV | flat, 1.0x | **nothing gained** |

The threshold is the ADC's own floor, roughly **75-190 uV at the ADC with gain 1**
(an open input sits near 190 uV; a powered low-impedance sensor pulls it down to ~75 uV).

### The test to run

Record at gain 1 and look at the quietest 100 ms.

* near 75-100 uV -> ADC-limited, raise the gain, up to about 2.9x at gain 20
* much higher -> input-limited, gain only costs range

Full curve for the accelerometer case (quiet room, AC coupled, IEPE on):

| gain | input-referred noise | vs gain 1 | BNC range |
|---|---|---|---|
| 1 | 93.6 uV | 1.00x | +/-5.12 V |
| 2 | 92.7 uV | 1.01x | +/-2.56 V |
| 5 | 61.9 uV | 1.51x | +/-1.02 V |
| 10 | 43.0 uV | 2.18x | +/-0.51 V |
| **20** | **32.3 uV** | **2.90x** | +/-0.26 V |
| 50 | 33.3 uV | 2.81x | +/-0.10 V |
| 100 | 37.6 uV | 2.49x | +/-0.05 V |

Gain 2 is a bad trade - 1 % better noise for half the range. Nothing is gained past
gain 20, where the LTC6910 and the sensor's own amplifier take over.

---

## 9. Microphone on ch0 (B&K 4966-H-041 + 1706 preamp)

```text
AC coupling  ON
CCLD source  ON
gain         1
```

Measured room ambient through it:

```text
quietest 100 ms   63.7 dB SPL
mean over 2 s     68.0 dB SPL
DAQ noise floor   48.1 dB SPL at gain 1
headroom           15.6 dB above the DAQ floor
```

**Leave it at gain 1.** The room sets the noise floor, not the electronics - you are
already 15.6 dB clear of the DAQ. Gain 10 would lower the DAQ floor to 28 dB SPL but
ambient stays at 64 dB, so nothing improves. Gain would only matter below ~48 dB SPL,
i.e. in a treated room.

---

## 10. Current source: measured, and the low standing bias is still unexplained

The circuit note states the source is **set to 4 mA**, with 24-30 V of compliance from
a 26 V supply, switched by an ADG5401. Measured standing bias on this board, however:

| transducer | measured standing bias | expected |
|---|---|---|
| accelerometer | 7.54 V | 8-14 V |
| B&K 4966 + 1706 | **2.81 V** | ~12 V |

Two different transducers both read low. The method is sound: the bias DAC is calibrated 1:1 in input volts
(measured slope 0.000801 V at the ADC per mV of bias, against a predicted
0.8 x 0.001 = 0.0008), so the difference between the centring bias with the source
off and on *is* the sensor's standing bias.

Under-current would compress a CCLD sensor's output swing, making it clip before the
ADC does - but the source measures ~3.5 mA (below), so that is not the explanation.
The low readings remain unexplained.

### The current has now been measured: ~3.5 mA

ch3's 4-20 mA mode switches a 249 Ohm load across its input, so enabling ch3's current
source at the same time drives that current through a known resistance - the board
measures its own source with no external parts.

```text
4-20 mA on, source off  ->  ch3 +1.9034 V
4-20 mA on, source on   ->  ch3 +1.0292 V
step                        -0.8742 V  /  249 Ohm  =  3.51 mA
```

Against a specified 4 mA that is 88 %, close enough that **under-current does not
explain the low standing biases**. Something else accounts for the microphone reading
2.81 V where its preamp should sit near 12 V. Open question.

Caveat: this assumes the 249 Ohm sits directly across the input. The +1.90 V reading
with the load switched in but no current flowing shows the node is not simply referenced
to ground, so treat 3.51 mA as approximate rather than a calibration.

### Other ways to measure it

**A. Direct, with a digital multimeter.** Set the meter to DC current (mA) - on most
handhelds the red lead moves to a separate `mA` jack - and connect it across the
channel's BNC, centre to shell, with the source enabled. A constant-current source
tolerates a short, so the meter reads IOUT directly. Switch the meter back to volts
afterwards.

**B. External resistor, measured by the board.** Same idea as the ch3 trick but on any
channel: put a 1 kOhm resistor across the BNC (4 mA gives 4.0 V, inside the +/-5.12 V
range at gain 1), DC couple, source on, gain 1, bias into range, then `I = V_bnc / R`.

**C. Compliance check.** With nothing connected and the source on, measure the
open-circuit voltage with a meter; it should be 24-26 V. **Do not** attempt this through
the board - 26 V is far outside its input range. Note that a channel in exactly this
state (source on, nothing connected) rails the ADC input, which is a useful signature.
