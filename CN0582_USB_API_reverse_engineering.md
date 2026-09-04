# CN0582 (EVAL-CN0582-USBZ) — USB API Reverse-Engineering Log

Reverse-engineering the USB command protocol of the "Analog Devices USB3-SPI" bridge
(Cypress/Infineon EZ-USB **FX3**) on the EVAL-CN0582-USBZ, to drive the board from
Python (pyusb / libusb).

> Status: **in progress.** EP 0x81 sample stream fully decoded (§6) — data path is usable end to end. Living document — new captures appended.
> Confirmed = verified across ≥2 observations or decoded cleanly. Hypothesis = plausible, unverified.

---

## 1. Device identity

| Field | Value |
|---|---|
| Product string | `Analog Devices USB3-SPI` |
| Manufacturer | ADI |
| USB VID | `0x0456` (Analog Devices) |
| USB PID | `0xED11` |
| Serial / board ID | `ADI202302150064` (read via command, see CMD-ID) |
| bcdUSB | `0x0200` (enumerated USB 2.0 high-speed via dock hub) |
| bMaxPacketSize0 | 64 |
| Configurations | 1 |
| Windows driver | CyUSB3 (Cypress) — swap to WinUSB via Zadig for pyusb/libusb |

```python
import usb.core
dev = usb.core.find(idVendor=0x0456, idProduct=0xED11)
```

## 2. Interface / endpoint map

Single vendor-specific interface (bInterfaceClass = `0xFF`), 3 bulk endpoints, 512-byte max packet:

| Endpoint | Dir | Type | Role |
|---|---|---|---|
| `0x81` | IN  | Bulk | **ADC sample stream** — 4 ch x 24 bit, nibble-encoded (see §6) |
| `0x02` | OUT | Bulk | **Command channel** (host → device) |
| `0x82` | IN  | Bulk | **Command response / echo** (device → host) |

## 3. Protocol framing (CONFIRMED)

- Every logical operation = **one OUT to EP 0x02, then one IN from EP 0x82 of equal length.**
- The device **echoes the command** back on EP 0x82. For **reads**, the echoed response keeps
  the header bytes and fills the zero-padded data field with the read-back value.
- Observed frame families by length: 1, 2, 3, 11, 18, and 2048 bytes.
- The `0x50` (memory/coefficient) family uses **request prefix `0xC3`, response prefix `0x69`**,
  i.e. request `C3 50 <addr> <data...>`  →  response `69 50 <addr> <data...>`.

## 4. Command log (decoded)

### CMD-ID — Read board serial / ID string   *(CONFIRMED)*
```
OUT EP0x02: c3 50 00  + 15 x 00        (18 bytes)
IN  EP0x82: 69 50 00  + "ADI202302150064" (ASCII)   (18 bytes)
```
`0x50 0x00` = read ID string.

### CMD-CAL — Read calibration coefficient   *(CONFIRMED)*
```
OUT EP0x02: c3 50 <addr>  + 8 x 00     (11 bytes)
IN  EP0x82: 69 50 <addr>  + <8-byte IEEE-754 big-endian double>   (11 bytes)
```
- `<addr>` nibbles: **high nibble = channel (1..5)**, **low nibble = coefficient index**.
- Coefficient indices seen: 0,1,2,3 and 5,6,7,8 (index 4 not read in this capture).
- Decoded values (representative):
  - idx 0 (`0x10..0x50`): ~0.805–0.964  → looks like **per-channel gain**.
  - idx 5/6 include several **exactly 1.0** (`3ff0000000000009`) → unity-gain defaults.
  - other indices: very small magnitudes (1e-6 … 1e-24) → **offset / higher-order
    polynomial correction terms**.
- Read as a full block at startup (channels 1–5 × coefficients). Block is issued twice
  during GUI launch (initial read, then again — likely on entering the main window).

### CMD-GAIN — Set per-channel PGA gain (LTC6910)   *(CORRECTED, hardware-verified)*

> **The `0x49 + 9*ch + gain_index` formula was wrong.** It reproduces ch0 correctly and
> happens to match one Ch1 capture, but it is not a per-channel command at all - every
> write carries **all four channels**, so using it to set one channel silently changes
> the other three. That is exactly what made channel gains look inconsistent.

The two `0x67` registers hold **one 12-bit word, 3 bits per channel**:

```text
word = reg0x02 | (reg0x03 << 8)
gain_index[ch] = (word >> (3*ch)) & 7

index :   0     1   2   3   4    5    6    7
gain  :   0     1   2   5   10   20   50   100        (index 0 = muted; LTC6910 codes)
```

Write with `67 02 <word & 0xFF> 00` then `67 03 <word >> 8> 00`; read with
`67 02 00 ff` / `67 03 00 ff` (each returns the byte replicated four times).

**How it was found.** Sweeping the byte in reg 0x02 from `0x49` to `0x6a` and watching all
four lanes: ch0's response cycled with period 8 through ratios 1 : 2 : 5 : 10 (the gain
sequence), while ch1 changed once every 8 codes - i.e. ch0 is bits[2:0] and ch1 is
bits[5:3]. Sweeping reg 0x03 the same way put ch3 at bits[3:1] of that register, and ch2
turned out to straddle the two (reg2 bits[7:6] plus reg3 bit[0]) - which is just the
contiguous 12-bit word above.

**Cross-check against the original captures** (both from one session, in order):

| capture | byte written | unpacks to |
|---|---|---|
| `gain_CH0_2` | `0x4a` | ch0=2, ch1=1, ch2=1, ch3=1 |
| `CH0_gain5` | `0x4b` | ch0=5, ch1=1, ch2=1, ch3=1 |
| `CH1_gain_2` | `0x53` | **ch0 still 5**, ch1=2, ch2=1, ch3=1 |

`0x53` only makes sense if ch0's gain of 5 is still carried in the same byte - which is
the packing. The always-present companion write `67 03 02 00` is simply the high byte:
`0x02` = ch2 index 1, ch3 index 1, i.e. both at gain 1.

**Measured on hardware**, setting one channel at a time with the others held at 1:

| gain | ch0 | ch1 | ch2 | ch3 | other lanes |
|---|---|---|---|---|---|
| 1 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 2 | 1.94 | 1.93 | 1.93 | 1.98 | 1.00 |
| 5 | 4.74 | 4.70 | 4.69 | 4.90 | 1.00 |
| 10 | 9.37 | 9.28 | 9.26 | 9.72 | 1.00 |

Untouched lanes stay at exactly 1.00, and `get_gains()` round-trips `[1, 2, 5, 10]`.
Ratios run a few percent under nominal - LTC6910 gain tolerance plus the front end.

```python
GAINS = (0, 1, 2, 5, 10, 20, 50, 100)

def set_gains(gains):            # always writes all four
    word = 0
    for c, g in enumerate(gains):
        word |= GAINS.index(g) << (3 * c)
    cmd(bytes([0x67, 0x02, word & 0xFF, 0x00]), 4)
    cmd(bytes([0x67, 0x03, (word >> 8) & 0xFF, 0x00]), 4)
```

### GUI note — greyed "Set" button
The greyed **Set** is the **Sensor DC Bias** apply button, not Gain. Gain applies immediately on
dropdown change (confirmed in capture). Not a comms failure — every write was echoed/acknowledged.

### CMD-COUPLING — Set per-channel AC/DC coupling (ADG5436)   *(CONFIRMED)*
Source: `CH0_AC_Coupling_ON_OF.json` (Ch0 AC on, then off). Two-byte ASCII, echoed.
```
OUT 0x02: "c" + <digit>        e.g. 63 30 = "c0"     (echoed on 0x82)
digit = 2*channel + state      (ASCII '0'..'7')
  state 0 = AC coupling ON
  state 1 = AC coupling OFF (DC-coupled)
```
| | AC on | DC (off) |
|---|---|---|
| Ch0 | c0 | c1 |
| Ch1 | c2 | c3 |
| Ch2 | c4 | c5 |
| Ch3 | c6 | c7 |
Ch0 confirmed directly. Startup init sends `c1 c3 c5 c7` = all 4 channels -> DC
(matches datasheet "DC for all channels" default), confirming the odd/DC codes.

pyusb:
```python
def set_coupling(ch: int, ac_coupled: bool):
    digit = 2*ch + (0 if ac_coupled else 1)
    cmd(b'c' + str(digit).encode(), 2)
```

### CMD-DCBIAS — Set per-channel sensor DC bias / level-shift (AD5686R)   *(CONFIRMED ON HARDWARE)*

> **Correction (hardware-verified).** The channel-select byte's low nibble is the
> AD5686R **DAC channel bitmask**, not `0x31 + channel`. The old reading is correct for
> ch0/ch1 by coincidence (`1<<0`=1, `1<<1`=2) but addresses the wrong DAC for ch2/ch3.
> What gave it away: byte `0x33` moves **ch0 and ch1 together**. Measured, stepping one
> mask at a time and watching all four lanes:
>
> | byte | mask | lanes that moved |
> |---|---|---|
> | `0x31` | 0001 | ch0 |
> | `0x32` | 0010 | ch1 |
> | `0x34` | 0100 | ch2 |
> | `0x38` | 1000 | ch3 |
> | `0x3F` | 1111 | ch0, ch1, ch2, ch3 simultaneously |

Sources: `sensor_DC_bias_set.json` (Ch0=2450 mV), `sensor_DC_bias_set_CH1.json` (Ch1=11000 mV),
`CH0_BIAS_4000mV.json` (Ch0=4000 mV), and the `03 31 e5 42` write from the gain capture (Ch0=11000 mV).
Single write, echoed on 0x82.
```
OUT 0x02: 03 <ch> <hi> <lo>       (echoed)
  ch        = 0x30 | (1 << channel)   <-- DAC channel BITMASK, verified on hardware
              ch0=0x31  ch1=0x32  ch2=0x34  ch3=0x38   (0x3F = write all four at once)
  <hi><lo>  = big-endian 16-bit AD5686R DAC code
```
Concrete confirmed examples:
```text
CH0 = 4000 mV  -> OUT 0x02: 03 31 92 8d  -> IN 0x82: 03 31 92 8d
CH0 = 2450 mV  -> OUT 0x02: 03 31 80 3c  -> IN 0x82: 03 31 80 3c
CH0 = 11000 mV -> OUT 0x02: 03 31 e5 42  -> IN 0x82: 03 31 e5 42
```
Code (channel-INDEPENDENT — Ch0 and Ch1 at 11000 mV both = 0xe542):
```
code = round( (2.52 + 0.3*V_bias) / 1.3 / 5.0 * 65536 )     # V_bias in volts
     = round( 25417 + 3.0248 * V_bias_mV )                  # exact empirical linear form
```
Data points (both fit): 2450 mV -> 0x803c (32828), 4000 mV -> 0x928d (37741), 11000 mV -> 0xe542 (58690). Big-endian confirmed.

Corrected: the small residual vs nominal formula constants is NOT per-channel calibration
(Ch0 and Ch1 give identical codes for identical voltage) -- it is just nominal V_OCM/V_REF rounding.
The stored cal coefficients are applied to measured data, not to this DAC write.

pyusb:
```python
def set_dc_bias(ch: int, mv: float):
    v = mv/1000.0
    vshift = (2.52 + 0.3*v)/1.3
    code = max(0, min(65535, round(vshift/5.0*65536)))
    # low nibble is the AD5686R DAC channel BITMASK, not a channel index
    cmd(bytes([0x03, 0x30 | (1 << ch), (code>>8)&0xFF, code&0xFF]), 4)
```

### CMD-START / CMD-SUSPEND — Acquire and stop the data burst   *(CONFIRMED from `single_cap_ch0.json`)*
The single-shot capture contains a full acquisition lifecycle on endpoint `0x02`:

```text
OUT EP0x02: "start" + 0xFF padding to 2048 bytes
IN  EP0x82: echo of the same 2048-byte frame

... then the ADC sample burst begins on EP0x81 (multiple 2048-byte bulk IN packets) ...

OUT EP0x02: "suspend" + 0xFF padding to 2048 bytes
IN  EP0x82: echo of the same 2048-byte frame
```

This is explicitly visible in the capture JSON:

- `frame 51277`: `0x02` payload starts with `73:74:61:72:74` = ASCII `start`
- followed by bulk IN packets on `0x81` (`frame 51280`, `51282`, ...)
- later: `frame 51537`: `0x02` payload starts with `73:75:73:70:65:6e:64` = ASCII `suspend`

This means the single capture is not a bare `read` from `0x81`; it is a lifecycle:

1. send `start`
2. receive the ADC burst on `0x81`
3. send `suspend`

This confirms the 2048-byte plain-ASCII-keyword command family observed earlier and shows that the acquisition command is carried as a padded keyword frame, not a short binary opcode.

### Continuous stream layout — confirmed from `continuous_cap_ch0.json`   *(CONFIRMED)*
The continuous capture was the decisive check: it shows the same acquisition state machine repeated many times rather than a one-off burst.

Evidence from the capture summary:

- `1320` total packets in the capture
- `1280` packets on endpoint `0x81`
- `20` packets on endpoint `0x02`
- `20` packets on endpoint `0x82`
- all data packets on `0x81` are exactly `2048` bytes long
- the `0x02` packets are always 2048-byte padded keyword frames, e.g. `b'start\xff\xff...'` or `b'suspend\xff\xff...'`

The actual repeated stream layout is:

```text
OUT EP0x02:  "start" padded to 2048 bytes
IN  EP0x82:  echo of the same 2048-byte start frame
IN  EP0x81:  2048-byte binary sample block
IN  EP0x81:  2048-byte binary sample block
IN  EP0x81:  2048-byte binary sample block
... repeated many times ...
OUT EP0x02: "suspend" padded to 2048 bytes
IN  EP0x82:  echo of the same 2048-byte suspend frame
```

Frame-level evidence from the capture:

- `frame 4075`: `0x02` payload starts with `b'start'`
- `frame 4078`: first `0x81` data block appears immediately after
- `frame 4335`: `0x02` payload starts with `b'suspend'`
- the same pattern repeats repeatedly through the file: `start -> data burst -> suspend`

This means the host does not send a short binary command to the data endpoint. Instead, the board starts and stops the stream using a padded keyword command on `0x02`, and the actual ADC samples are streamed in repeated 2048-byte bursts on `0x81`.

In other words, the continuous stream is a normal USB bulk data stream whose lifecycle is controlled by `start`/`suspend` frames on the command endpoint. This is the correct model to use when building a Python acquisition loop.

### CMD-002xx — SPI-level register writes   *(OBSERVED, not yet interpreted)*
3-byte writes to EP 0x02 during init, echoed on EP 0x82. Meaning requires the CN0582
schematic/BOM + target-chip datasheet register maps. Raw observed (in order):
```
00 21 00   01 00 00   00 21 00   00 40 00   00 40 00   00 20 00
02 08 05   02 33 04   02 01 07   02 00 01   02 00 06   02 80 06
02 00 06   02 80 06   74 34 00   74 19 00
```
Note the recurring leading byte `02` and `74` — probably a target/chip selector or opcode.

### ~~CMD-ASCIIQ~~ — CORRECTED: these are CMD-COUPLING, not queries
The startup `"c1" "c3" "c5" "c7"` are the **coupling** command (see CMD-COUPLING):
digit = 2*ch+1 for ch 0..3 = all channels set to **DC coupling** at startup.

### CMD-CURRSRC - Set IEPE current source, PER CHANNEL   *(CORRECTED, hardware-verified)*

> **The bitmask reading was wrong.** It is not a mask and there is no global register.
> The command addresses **one channel at a time**:

```text
OUT EP0x02: <byte>        IN EP0x82: <byte>   (echo)

byte = (channel << 1) | enable

  ch0: 0x00 off / 0x01 on        ch2: 0x04 off / 0x05 on
  ch1: 0x02 off / 0x03 on        ch3: 0x06 off / 0x07 on
```

Same shape as CMD-COUPLING, which is ASCII `'c'` followed by `2*channel + state`.

**What disproves the mask.** `CH3_current_ON_OFF toggling.json` toggles ch3 four times:

```text
07  06  07  06
```

That moves **bit 0**, not bit 3. A bitmask cannot express "ch3 on" by setting bit 0.
Under the correct encoding it reads ch3 on, ch3 off, ch3 on, ch3 off.

**It also explains the startup block.** The four bare bytes `00 02 04 06` are not a
different context - they are ch0..ch3 current sources switched **off**, one at a time.
So `init_board()` always leaves every source disabled.

**Why the mask appeared to work.** For a single channel the mask value collides with the
correct per-channel code: mask `0x01` and "ch0 on" are both `0x01`; mask `0x02` happens to
be "ch1 off". The earlier capture (`01 -> 03 -> 02 -> 00`) fits both readings, because the
action order was Ch0 on, Ch1 on, **Ch1 off, Ch0 off** - not the order recorded in the
original note. Anything involving two or more channels breaks: a panel with ch0, ch2 and
ch3 enabled computes mask `0x0D`, which is sent as one byte meaning **"channel 6 on"**, so
nothing is enabled at all.

**Verified on hardware.** DC-coupled, watching the bias needed to centre ch0:

```text
source off -> 1837 mV        source on -> 9617 mV        standing bias 7.78 V
```

A source that never switched would show no difference. Sending the correct byte per
channel restores the accelerometer; sending the mask does not.

```python
def set_current_source(ch: int, on: bool):
    cmd(bytes([((ch & 3) << 1) | (1 if on else 0)]), 1)

def set_current_sources(mask: int):        # convenience only - the wire has no mask
    for ch in range(4):
        set_current_source(ch, bool(mask & (1 << ch)))
```

### CMD-4-20mA - channel assignment confirmed   *(hardware-verified)*

`74 19 01` / `74 19 00` carry no channel field, and ch3 was previously only *inferred*
from the hardware. Measured with all four channels DC-coupled at a fixed bias while
toggling the command:

| 4-20 mA | ch3 source | ch0 | ch1 | ch2 | ch3 |
|---|---|---|---|---|---|
| off | off | +0.0587 | -0.4426 | -0.4400 | -0.4639 |
| **on** | off | +0.0584 | -0.4426 | -0.4400 | **+1.9034** |
| **on** | **on** | +0.0583 | -0.4426 | -0.4400 | **+1.0292** |
| off | **on** | +0.0582 | -0.4427 | -0.4400 | **-5.1200 railed** |

ch0, ch1 and ch2 move by under 0.6 mV across every combination while ch3 moves by volts.
**The command acts on ch3 alone**, as assumed.

Two useful side effects:

* **ch3 with the current source on and 4-20 mA off rails the input** (open circuit driven
  by the source). Expect this whenever a channel has no sensor and its source is enabled.
* With the 249 Ohm load switched in, toggling only the current source steps ch3 by
  **-0.874 V**, which through 249 Ohm is **~3.5 mA** against a specified 4 mA. That is the
  first direct measurement of the source. Caveat: it assumes the 249 Ohm sits directly
  across the input, and the +1.90 V reading with the load in but no source shows the node
  is not simply referenced to ground, so treat 3.5 mA as approximate.

## 5. pyusb sketch (hardware-validated encodings)

```python
import usb.core, struct

dev = usb.core.find(idVendor=0x0456, idProduct=0xED11)
dev.set_configuration()
EP_CMD_OUT, EP_RSP_IN = 0x02, 0x82

def cmd(payload: bytes, resp_len: int | None = None) -> bytes:
    dev.write(EP_CMD_OUT, payload)
    return bytes(dev.read(EP_RSP_IN, resp_len if resp_len else len(payload)))

def read_serial() -> str:
    r = cmd(b'\xc3\x50\x00' + b'\x00'*15, 18)
    return r[3:].split(b'\x00')[0].decode('ascii', 'replace')

def read_cal(addr: int) -> float:
    r = cmd(b'\xc3\x50' + bytes([addr]) + b'\x00'*8, 11)
    return struct.unpack('>d', r[3:11])[0]

def suspend():
    buf = b'suspend' + b'\xff'*(2048-7)
    cmd(buf, 2048)

def set_current_sources(mask: int):
    """Per channel: byte = (ch << 1) | enable. There is no mask on the wire."""
    for ch in range(4):
        b = ((ch & 3) << 1) | (1 if mask & (1 << ch) else 0)
        assert cmd(bytes([b]), 1)[0] == b, "device echo mismatch"

def set_current_source(ch: int, on: bool):
    """One channel per write - this is the primitive; there is no mask on the wire."""
    b = ((ch & 3) << 1) | (1 if on else 0)
    assert cmd(bytes([b]), 1)[0] == b, "device echo mismatch"

GAINS = (0, 1, 2, 5, 10, 20, 50, 100)      # LTC6910 codes 0..7; index 0 = muted

def set_gains(gains):
    """ALL four channels travel in one packed 12-bit word - there is no per-channel write."""
    word = 0
    for c, g in enumerate(gains):
        word |= GAINS.index(g) << (3 * c)
    cmd(bytes([0x67, 0x02, word & 0xFF, 0x00]), 4)
    cmd(bytes([0x67, 0x03, (word >> 8) & 0xFF, 0x00]), 4)

def get_gains():
    word = cmd(b'\x67\x02\x00\xff', 4)[0] | (cmd(b'\x67\x03\x00\xff', 4)[0] << 8)
    return [GAINS[(word >> (3 * c)) & 7] for c in range(4)]

def set_coupling(ch, ac):
    cmd(b'c' + str(2*ch + (0 if ac else 1)).encode(), 2)

def start():
    cmd(b'start' + b'ÿ'*(2048-5), 2048)

# read all cal coeffs: channels 1..5, coeff idx 0..8
# cal = {(ch,idx): read_cal((ch<<4)|idx) for ch in range(1,6) for idx in range(9)}
```

> This sketch is the minimal illustration. The working driver is
> [cn0582.py](cn0582.py) (control + gapless recording) with
> [cn0582_gui.py](cn0582_gui.py) on top; both are validated against hardware.

## 6. Sample stream on EP 0x81 — **FULLY DECODED**   *(CONFIRMED)*

Decoded and cross-validated against four independent captures (`single_cap_ch0`,
`continuous_cap_ch0`, `tapping_CH0`, `CH0_tapping_bias4000mV`). Working decoder:
[cn0582_stream.py](cn0582_stream.py).

### 6.1 The key insight: nibble-per-byte, bit-parallel across channels

Every byte on EP 0x81 has **high nibble `0xF`** (verified: 4,718,592 / 4,718,592 bytes in
`tapping_CH0`). Only the low nibble carries data. The earlier 16-bit-sample reading was
wrong — it produced pure noise (see `tapping_CH0_detrended.png`).

The stream is **not byte-serial**. The FX3 samples the AD7768-4's **four DOUT lines in
parallel**, so each low nibble is one *bit-slice* across the four channels at one DCLK edge:

```text
byte = 1111 d3 d2 d1 d0
              |  |  |  +-- DOUT0 -> channel 0
              |  |  +----- DOUT1 -> channel 1
              |  +-------- DOUT2 -> channel 2
              +----------- DOUT3 -> channel 3
```

**32 consecutive bytes = 32 DCLK edges = one AD7768 output frame per channel**, MSB-first:

```text
[ 8-bit header ][ 24-bit two's-complement conversion ]
```

Evidence that fixed the alignment: per-nibble-position randomness rises monotonically from
position 0 (constant across a whole 8192-frame burst) to position 31 (uniform over 0–15) —
a single MSB→LSB gradient across the whole 32-byte frame. That is impossible for four
independent byte-serial samples, but is exactly what bit-parallel lanes look like.

### 6.2 Header byte — decoded empirically

| bits | meaning | evidence |
|---|---|---|
| `[2:0]` | **channel ID** | the lane extracted from nibble bit *c* has `header & 7 == c` in **100 %** of 410,752 frames across all four captures |
| `[3]` | **saturation / over-range flag** | in `tapping_CH0`, all 956 samples at negative full scale (`-8388608`) have it set and **zero** non-saturated samples do; +216 more during filter recovery. Independently confirmed in `single_cap_ch0`, where ch1 sits at *positive* full scale (`+8388607`) and its header is `0x09` = ch1 + flag |
| `[7:4]` | always 0 in every capture | bit 6 = filter type ⇒ **sinc5 filter active** (matches the observed roll-off) |

Header values seen: `0x00 0x01 0x02 0x03` (normal), `0x08` / `0x09` (saturated ch0 / ch1).
**Use `header & 7 == lane` as the frame-sync check** — it catches any misalignment instantly.

### 6.3 Burst geometry and sample rate   *(CONFIRMED)*

```text
2048-byte USB packet = 64 samples x 4 channels
1 "start" burst      = 128 packets = 262144 bytes = 8192 samples/channel
```

8192 samples/channel per burst matches the GUI's "8192 data points" label exactly.

Sample rate from capture timestamps (`single_cap_ch0.json`): the 128 packets of one burst span
31.751 ms first-to-last, i.e. 127 packet-intervals = 8128 samples:

```text
8128 / 0.031751 s = 255,992 SPS  =>  fs = 256 kSPS   (AD7768-4 max ODR, MCLK 32.768 MHz)
burst duration = 8192 / 256000 = 32.768 ms
```

The `continuous_cap` bursts measure ~227 kSPS by the same method, but that is host/GUI
scheduling overhead spread across the burst, not a different ODR — use **256 kSPS**.

### 6.4 Scaling to volts

```text
V_adc = code / 2^23 * 4.096          (VREF = 4.096 V)
V_in  = V_adc / (0.8 * G_PGA)        (front end: 0.3 level shift x 2.667 FDA x G_PGA)
```

Sanity check on decoded output (`tapping_CH0`, 18 bursts, 147,456 samples/channel):

| ch | mean | RMS noise | p-p | saturated |
|---|---|---|---|---|
| 0 (tapped) | −3.94304 V | 33 864 µV | 529.1 mV | 1172 |
| 1 | −4.03307 V | **185.8 µV** | 1.72 mV | 0 |
| 2 | −4.02676 V | **183.9 µV** | 1.68 mV | 0 |
| 3 | −4.04179 V | **176.8 µV** | 1.67 mV | 0 |

The ~180 µVrms floor on the three idle channels is a credible 24-bit Σ-Δ noise floor at
256 kSPS, and the three idle channels are *independent* — strong evidence the lane separation
is real and not an artefact of the decode.

The tap transients are unambiguous: quiet bursts sit at 0.20–0.29 mV RMS, the two tap bursts
(6 and 10) at **98.8 / 98.4 mV RMS, 529 / 489 mV p-p**, with a textbook impact shape — flat
baseline, hard clip at the −4.096 V rail (ADC railed, over-range flag set), then an exponential
recovery overshoot. The spectrum shows the sinc5 roll-off from ~60 kHz.

### 6.5 Acquisition lifecycle   *(CONFIRMED)*

Both single and "continuous" modes are the **same** state machine; continuous is just the host
repeating it:

```text
OUT EP0x02: "start"   + 0xFF padding to exactly 2048 bytes
IN  EP0x82: echo (2048 bytes)
IN  EP0x81: 128 x 2048-byte packets  = 8192 samples/channel
OUT EP0x02: "suspend" + 0xFF padding to exactly 2048 bytes
IN  EP0x82: echo (2048 bytes)
```

Padding verified byte-exact: every byte after the keyword is `0xFF`, total length 2048.
Burst length is fixed at 128 packets in **all** captures — no length field was ever sent, so
8192 points is either firmware-fixed or set by a command not yet triggered.

### 6.6 Reference decoder

```python
import numpy as np

def decode(raw: bytes):
    """raw = concatenated EP 0x81 payload -> (data[4,N] int32, headers[4,N] uint8)"""
    b = np.frombuffer(raw, dtype=np.uint8)
    assert np.all(b >= 0xF0), "not nibble-encoded"
    n = (len(b) // 32) * 32
    fr = (b[:n] & 0x0F).reshape(-1, 32).astype(np.uint32)
    words = np.empty((4, fr.shape[0]), dtype=np.uint32)
    for c in range(4):                       # bit c of each nibble = DOUTc = channel c
        bits = (fr >> np.uint32(c)) & np.uint32(1)
        w = np.zeros(fr.shape[0], dtype=np.uint32)
        for j in range(32):                  # MSB-first
            w = (w << np.uint32(1)) | bits[:, j]
        words[c] = w
    headers = (words >> np.uint32(24)).astype(np.uint8)
    for c in range(4):
        assert np.all((headers[c] & 0x07) == c), "frame misalignment"
    d = (words & np.uint32(0xFFFFFF)).astype(np.int64)
    return np.where(d >= 1 << 23, d - (1 << 24), d).astype(np.int32), headers

def volts_adc(data):  return data / (1 << 23) * 4.096
def saturated(hdr):   return (hdr & 0x08) != 0
```

### CMD-4-20mA — Enable Ch3 4–20 mA current-loop input   *(CONFIRMED)*
Source: `CH3_4_20mA_ON_OF.json` — the whole capture is exactly two 3-byte writes, both echoed:
```text
OUT 0x02: 74 19 01   -> IN 0x82: 74 19 01     4-20 mA mode ON
OUT 0x02: 74 19 00   -> IN 0x82: 74 19 00     4-20 mA mode OFF
```
No channel field — Ch3 is the only channel with the on-board 249 Ω load resistor (V = 249 Ω · I),
so the setting is implicitly Ch3. Startup init sends `74 19 00` (off) together with `74 34 00`
(same `0x74` family, function still unknown).

```python
def set_4_20ma(on: bool):
    cmd(bytes([0x74, 0x19, 1 if on else 0]), 3)
```

### CMD-GAIN readback — *(superseded by the corrected CMD-GAIN in section 4)*

> The register model below is right - `67 <reg> <val> <00=write|FF=read>`, response is the
> byte replicated four times. What was wrong is the *meaning* of the value: it is a packed
> 12-bit gain word covering all four channels, not a single channel+gain code. See
> CMD-GAIN in section 4.
The gain family is a small register file: `67 <reg> <value> <mode>`, where
**`mode = 0x00` writes and `mode = 0xFF` reads**. A read returns 4 bytes with the register value
**replicated four times** — not four per-channel values, which was the earlier confusion.

```text
OUT 67 02 00 ff  ->  IN 49 49 49 49      read reg 0x02 (gain code) = 0x49
OUT 67 03 00 ff  ->  IN 02 02 02 02      read reg 0x03            = 0x02
OUT 67 02 4a 00  ->  IN 67 02 4a 00      write reg 0x02 = 0x4a  (echo)
OUT 67 03 02 00  ->  IN 67 03 02 00      write reg 0x03 = 0x02  (echo, fixed companion)
```

Reg 0x02 is a **single global byte**, which is why all four returned bytes are equal:
`CH0_gain5` writes `0x4b` and the next capture's readback returns `4b 4b 4b 4b`. That part
holds. What it does **not** confirm is the old `0x49 + 9*ch + gain_index` reading - the byte
is a packed word covering all four channels (see CMD-GAIN in section 4), so a readback of
`0x4b` means ch0=5, ch1=1, ch2=1, ch3=1 rather than "channel 0, gain 5". `0x49` read at
session start unpacks to all four channels at gain 1, which is the power-on default.

## 7. Open questions / next captures

- **Q1 - current-source addressing - RESOLVED, and the earlier answer was wrong.**
  Not a bitmask: `byte = (channel << 1) | enable`, one channel per write. See
  CMD-CURRSRC. Verified on hardware for all four channels.
- **Q2 — coefficient index 4** still not read in any capture; find a flow that touches it.
- **Q3 — EP 0x81 sample stream — RESOLVED.** Nibble-per-byte, bit-parallel over four DOUT
  lanes, 8-bit header + 24-bit two's complement, MSB-first, 256 kSPS, 8192 samples/burst.
  See §6.
- **Q4 — 3-byte register writes:** the `00/01/02/74`-prefixed init writes are still opaque.
  All are echoed verbatim, so the echo carries **no readback information** — decoding needs
  the CN0582 schematic + BOM. Note that `02 80 06` has bit 7 set, which would be an SPI read
  of reg 0x00 under a `02 <reg> <val>` = "SPI to AD7768-4" model. **Hypothesis only.**
- **Q5 — other 2048-byte keyword commands — partly resolved.** Only `start` and `suspend` have
  ever appeared, in every data capture. No length or rate keyword was ever sent, so the
  8192-point burst size and the 256 kSPS ODR appear firmware-fixed. Capture a GUI run with a
  *changed* sample count to test this.
- **Q6 — sine / shaker output:** still uncaptured (AD9833 frequency, AD5543 amplitude,
  ADG1219 filter select).
- **Q7 — new:** what is `74 34 00`? Same family as the 4–20 mA command, sent once at startup.
- **Q8 - RESOLVED (hardware).** The bias DAC does act on the measurement path:
  V_adc = 0.000735 x bias_mV - 1.336 (r = 0.9998). See 7c. Original wording kept below
  for the record.
- **Q8 - superseded.** An earlier note here claimed the bias DAC does not affect the
  measurement path, based on `tapping_CH0` vs `CH0_tapping_bias4000mV` having the same DC
  level. **That comparison was invalid.** Capture timestamps show the ordering is:

  | capture | wall clock |
  |---|---|
  | `tapping_CH0.json` | 18:40:02 |
  | `CH0_tapping_bias4000mV.json` | 18:49:21 |
  | `CH0_BIAS_4000mV.json` (the actual `03 31 92 8d` write) | **19:08:17** |

  The bias was written **19 minutes after** the recording named for it, so both tapping
  captures almost certainly ran at the same 11000 mV default. The effect of the bias DAC
  on the measured DC level is **untested**, not disproved. Sweep it while streaming.

## 7b. Fixed constants for the driver

| Constant | Value |
|---|---|
| Sample rate | 256 kSPS (measured +65 ppm, see below) |
| Channels | 4, simultaneous |
| Resolution | 24-bit two's complement |
| VREF | 4.096 V |
| Bytes per sample-set on the wire | 32 (nibble-encoded) |
| Samples per 2048-byte packet | 64 per channel |
| Packets per `start` burst | 128 |
| Samples per burst | 8192 per channel (32.768 ms) |
| ADC digital filter | sinc5 (header bit 6 = 0) |
| Idle-channel noise floor | ~180 µVrms at the ADC |

## 7c. The DC-offset problem - **RESOLVED ON HARDWARE**

The ~4 V offset was **the level-shift DAC never being programmed**. The AD5686R comes out
of reset with its default output, which parks the front end within 54-180 mV of the
negative rail. Nothing about the sample decode, the calibration constants, or the input
coupling was involved.

**First test was run wrong.** These 1 s clips were taken with the current source **off**,
which leaves an IEPE sensor unpowered and therefore with no standing bias for AC coupling
to block. Under that condition coupling barely matters:

| coupling | ch0 DC | rms |
|---|---|---|
| DC (`c1`) | -3.91723 V | 115 uV |
| AC (`c0`) | -3.89067 V | 3304 uV |
| back to DC (`c1`) | -3.91722 V | 115 uV |

That 27 mV difference briefly looked like it killed the "AC coupling removes the sensor
bias" idea. It did not - the test simply could not see the effect. With the current source
**on** (see 7e) AC coupling shifts the required bias by ~8.3 V, which is exactly the
sensor's standing bias being blocked. Both mechanisms are real:

- **the unprogrammed level-shift DAC** - the dominant term, and the reason the original
  captures sat near the rail;
- **AC coupling** - removes the IEPE standing bias, but only once the sensor is powered.

**The bias DAC is the whole story.** Sweeping it while watching ch0 (gain 1, DC-coupled,
current source off):

| bias (mV) | ch0 DC (V) | | bias (mV) | ch0 DC (V) |
|---|---|---|---|---|
| 0 | -1.26895 | | 4000 | +1.57848 |
| 500 | -0.93653 | | 5000 | +2.32809 |
| 1000 | -0.59683 | | 6000 | +3.08730 |
| 2000 | +0.10687 | | 7000 | +3.85141 |
| 3000 | +0.83588 | | 8000+ | +4.09600 (railed) |

Linear fit over 15 points: **V_adc = 0.000735 x bias_mV - 1.33627, r = 0.999822.**

This also **resolves Q8**: the bias DAC unambiguously acts on the measurement path. The
earlier "no effect" reading came from comparing two captures that were both taken before
the bias write, and the note that replaced it called it untested - it is now tested.

Why the original captures looked the way they did: the vendor GUI's **Set** button is what
pushes the bias, and your notes recorded that button as greyed out. So the DAC sat at its
reset value for the whole capture session, leaving ch0 with 153 mV of headroom - which is
exactly why a tap clipped instantly.

### Centring a channel

`CN0582.auto_bias(ch)` bisects on the sign of the error and leaves the bias applied.
Bisection rather than a line fit, because the slope varies by ~4x between channels
depending on what is connected, and a clipped reading still has a usable sign. Measured
on all four channels, current sources off, gain 1:

| ch | bias found | resulting DC | rms | headroom |
|---|---|---|---|---|
| 0 | 1868 mV | +0.00164 V | 896 uV | 4094 mV |
| 1 | 2493 mV | -0.00300 V | 1879 uV | 4093 mV |
| 2 | 2473 mV | +0.00038 V | 200 uV | 4096 mV |
| 3 | 2504 mV | -0.00015 V | 192 uV | 4096 mV |

Headroom goes from 54-180 mV to ~4.09 V - about **23x more usable range**. The higher
noise on ch0/ch1 (896/1879 uV vs ~195 uV) is real sensor noise; ch2/ch3 were open.

Note the vendor GUI's default of **11000 mV railed an unloaded channel** at +4.096 V in
this test. That default assumes an IEPE sensor sitting at ~11 V whose standing bias it is
cancelling - it is not a sane default for an open or DC-coupled input.

## 7d. Hardware validation log

Everything below was confirmed against the board over WinUSB, after flashing
`MultiChannel_Vibration_Test.img` to SPI flash and setting S1 to SPI boot.

| Item | Result |
|---|---|
| Board ID | `ADI202302150064` - matches the captures exactly |
| Cal coefficients | ch1 idx0 `0.8055200201`, ch4 idx0 `0.8059550566` - match captures exactly |
| `init_board()` | 24/24 writes acknowledged |
| Frame headers, live | `{0} {1} {2} {3}` - frame sync valid on live data |
| Idle noise floor | 182-189 uVrms, matching the captured floor |
| Idle DC (ch1/2/3) | -4.03304 / -4.02669 / -4.04171 V vs captured -4.03307 / -4.02676 / -4.04179 |
| Single burst | 8192 samples, headers clean, 0 over-range |
| Gapless record 1 s | 8.14 MB/s, 256000 samples/ch |
| Gapless record 5 s | **8.19 MB/s** - matches the predicted 8.192 MB/s exactly |
| Frame sync over 5 s | valid across all 1,286,144 samples |

The 250 us packet cadence predicted from the captures held: sustained streaming works at
the full ADC rate with no dropped samples, so the vendor GUI's 11 % duty cycle really was
a host-side choice.

### Sample-rate measurement - how to do it correctly

Timing a whole `record()` call does **not** measure the sample rate. The elapsed time
includes disk-flush overhead that scales with file size - measured at ~15 ms for a 2 s
clip but ~47 ms for a 10 s clip - so a two-point fit that assumes constant overhead is
confounded. Three baselines taken that way disagreed by 5300 ppm.

Timing the **bulk transfer arrivals** instead removes both the disk I/O and the
start/stop overhead. Over an 18.2 s steady-state span (149.2 MB, no file writes):

```text
sample rate = 256,016.6 SPS   ->  +65 ppm vs nominal 256,000
```

That is within crystal tolerance (the AD7768-4 ODR is MCLK-derived, and MCLK is an
on-board crystal at typically +/-20 to 50 ppm). The independent USBPcap-timestamp
figure from the original captures, 255,992 SPS (-31 ppm), agrees. **Use the nominal
256,000** - it is correct to about +/-100 ppm, which is the limit of what either
method resolves.

## 7e. Working configuration for a powered IEPE sensor (ch0)

Measured with the accelerometer connected to ch0, gain 1, at the vendor default bias of
11000 mV:

| coupling | IEPE | ch0 DC | rms | over-range |
|---|---|---|---|---|
| DC | off | +4.0960 V | 0 | **8192 (railed)** |
| DC | **on** | -0.9555 V | 7427 uV | 0 |
| AC | off | +4.0960 V | 0 | **8192 (railed)** |
| AC | **on** | +4.0960 V | 0 | **8192 (railed)** |

So the vendor's 11000 mV default is tuned for **DC-coupled with the current source on** -
it cancels the sensor's ~10 V standing bias. In every other combination it rails.

`auto_bias(0)` in each configuration, sensor connected:

| coupling | IEPE | bias found | resulting DC | rms |
|---|---|---|---|---|
| DC | off | 1861 mV | -0.00004 V | 103 uV |
| DC | on | 10492 mV | +1.16579 V | 393 uV |
| AC | off | 2148 mV | -0.00225 V | 147 uV |
| **AC** | **on** | **2189 mV** | **+0.00145 V** | **45 uV** |

The gap between DC-coupled-on (10492 mV) and AC-coupled-on (2189 mV) is **8303 mV** - the
sensor's standing bias, blocked by the coupling capacitor. That is the direct measurement
of the effect the first test was blind to.

**Recommended for a powered IEPE accelerometer: AC coupling ON, current source ON,
bias ~2190 mV, gain 1.** It centres at 0 V with the full +/-4.096 V of headroom and the
lowest noise floor of any configuration (45 uVrms - better even than an open input, since
the powered sensor presents a low source impedance).

Compare with the original tapping captures: DC-coupled, bias never programmed, 153 mV of
headroom - which is why a tap clipped instantly.

> **Caveat.** `auto_bias` converges tightly everywhere except DC-coupled with the current
> source on, where it stopped ~1.17 V off target. That configuration has both the steepest
> bias slope (~2.8 mV of ADC per mV of bias) and the slowest settling, so the search
> measures a moving target. It is still usable (2.9 V of headroom, no clipping), but if you
> need it centred, raise `settle` or set the bias by hand. AC coupling avoids the problem
> entirely and is the better choice for vibration work anyway.

## 8. Capture method (reproducibility)

- Board forced to USB 2.0 high-speed via HP USB-C Mini Dock internal 2.0 hub
  (under "Generieke USB-hub", not the SuperSpeed hub).
- Wireshark + USBPcap, interface **USBPcap2**, "Inject already connected devices descriptors" on.
- One discrete GUI action per capture; export JSON.
- Filters: `usb.device_address == 5`, `usb.transfer_type == 0x03`,
  `usb.endpoint_address == 0x02 || usb.endpoint_address == 0x82`.

## 9. Hardware map (from CN-0582 Circuit Note, Rev. A)

The FX3 ("USB3-SPI") is a **controller with its own command set**, not a transparent SPI
passthrough. Per the note it "configures the analog input channels, enables the current sources,
sets the ADC modes, and sets up the waveform generator." That is why we see clean 1/3-byte
opcodes instead of raw SPI framing. Chips behind each captured function:

| Captured command | Function | Chip | Notes |
|---|---|---|---|
| CMD-CURRSRC (per channel) | IEPE current-source enable per ch | **ADG5401** SPST switch/ch | `byte = (ch << 1) \| enable`; measured ~3.5 mA |
| CMD-CAL (doubles) | Per-channel cal polynomial | (stored in EEPROM) | idx0 ≈ chain gain **0.8·G_PGA** (matches measured 0.805–0.964) |
| CMD-DCBIAS `03 31 <BE16>` | Level-shift voltage / offset | **AD5686R** quad 16-bit DAC | DECODED; code via Eq.4&5, big-endian |
| 3-byte write (TBD) | Per-channel gain (PGA) | **LTC6910** | gains 1,2,5,10,20,50,100 |
| 3-byte write (TBD) | AC/DC coupling | **ADG5436** dual SPDT | per-channel coupling select |
| write (TBD) | Shaker output frequency | **AD9833** DDS | 28-bit FREQ, FOUT = FREF·FREQ / 2^28 |
| write (TBD) | Shaker output amplitude | **AD5543** DAC | amplitude = D/65536 · 8.16 Vpp |
| write (TBD) | Output filter select | **ADG1219** | filtered / unfiltered |

Fixed facts useful for the driver:
- ADC: **AD7768-4**, 4-ch simultaneous 24-bit Σ-Δ, up to 256 kSPS, VREF 4.096 V, VOCM 2.52 V.
- Sample stream (EP 0x81) will be 24-bit/channel, 4 channels, source-synchronous from the ADC.
- Overall input gain G = 0.3 (level shift) × 2.667 (FDA) × G_PGA = **0.8 · G_PGA**.
- Ch3 has a 249 Ω on-board load resistor for 4–20 mA sensors (V = 249 Ω · I).

## 9b. Vendor-GUI controls mapped to commands

From the GUI screenshot, with the command for each control:

| GUI control | command | status |
|---|---|---|
| Current Source (per ch) | one byte `(ch << 1) \| enable` | decoded |
| 4-20 mA Sensor (ch3 only; ch0-2 greyed) | `74 19 01` / `74 19 00` | decoded |
| AC Coupling (per ch) | `c<2*ch+state>` ASCII | decoded |
| Gain (per ch, 0..100) | packed 12-bit word over `67 02` + `67 03` | decoded |
| Sensor DC Bias + Set (default 11000 mV) | `03 <0x30 or (1<<ch)> <BE16>` | decoded |
| Select Device | `c3 50 00` ID read | decoded |
| Start | `start` / `suspend` 2048-byte frames | decoded |
| Number of Cycles (default 3) | **not captured** | unknown |
| Mode Single / Continuous | host-side only - both use `start`/`suspend` | decoded |
| Channel Selector (ch0..ch3) | **display only** | see below |
| Bandwidth Full / 25 kHz | **not captured** | unknown (ADG1219?) |
| External Trigger checkbox | **not captured** | unknown |
| Plot Type, Export | host-side only | n/a |

**Channel Selector is display-only.** In every capture only Channel 0 was selected in the
GUI, yet all four lanes carry valid data with correct channel-ID headers and independent
noise. The AD7768-4 always streams all 4 channels; the selector just filters the plot. A
Python client therefore gets all four channels for free.

## 10. Remaining targeted captures (one variable at a time → isolates each opcode)

| To decode | GUI action to capture | Expect |
|---|---|---|
| AD9833 freq cmd | Set signal-gen **frequency** | 28-bit value, likely split across writes |
| AD5543 amplitude cmd | Set signal-gen **amplitude** | DAC code, amplitude = D/65536·8.16 Vpp |
| Ch2/Ch3 current source | Enable current source on **Ch2 then Ch3** | confirms mask bits `0x04` / `0x08` |
| Burst length control | Change the **sample count** in the GUI, then capture | a length field, or proof 8192 is fixed |
| `74 34 xx` meaning | Toggle every remaining GUI switch one at a time | which control moves that byte |
| Bandwidth Full / 25 kHz | Flip the **Bandwidth** toggle | filter-select write (ADG1219?) |
| External Trigger | Tick **External Trigger**, then Start | extra command before `start` |
| Number of Cycles | Change **Number of Cycles** 3 -> 10 | a count field, or proof it is host-side |

Diff each new capture against the startup baseline; the bytes that change are the command.

## Appendix A — raw transaction dump (startup_interaction.json)

Command family lengths seen and counts are summarised above; full per-packet hex is in the
capture. Key sequences: [18B ID read] → [3B/2B/1B init selects & register writes] →
[11B cal-coefficient block ×2] → [2048B "suspend"].
