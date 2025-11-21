# Audio Level Debugging Guide

## Problem Description
Parquet files recorded minutes apart show different background noise levels, suggesting inconsistent normalization or scaling between recordings.

## Current Normalization in the Code

### 1. LAN-XI Device Internal Scaling
**Location:** `HelpFunctions/lanxi.py:134-139`
```python
scale_factor = interpretations[signal.signal_id - 1].get(
    OpenapiStream.Interpretation.EDescriptorType.scale_factor, 1.0
)
values = np.array([x.calc_value for x in signal.values])
# Convert to volts (divide by 2^23 and multiply by scale factor)
values = (values * scale_factor) / (2 ** 23)
```
- **Purpose:** Device-internal calibration/normalization
- **Problem:** Scale factor may vary between measurements
- **Effect:** Changes raw voltage values stored in parquet files

### 2. WAV File Normalization
**Location:** `krak_logger_gui.py:257-260`
```python
# Save WAV file for AI0 only
max_voltage = 10
audio_data = (data[0] / max_voltage * 32767).astype(np.int16)
wav.write(OUTPUT_WAV_FILE, SAMPLE_RATE, audio_data)
```
- **Purpose:** Convert voltage to 16-bit integer for WAV format
- **Problem:** Hardcoded 10V may not match actual channel range
- **Effect:** Only affects WAV files, not parquet files

### 3. Parquet File Storage
**Location:** `krak_logger_gui.py:282-286`
```python
df = pd.DataFrame({
    "Time (s)": recorded_time_axis,
    "AI0 (V)": recorded_data[0],      # Raw voltage data after LAN-XI scaling
    "AI1 (V)": recorded_data[1]
})
```
- **Purpose:** Store raw voltage measurements
- **Note:** Uses data already processed by LAN-XI scale factor

## Debugging Steps

### Step 1: Add Scale Factor Debug Prints

**File:** `HelpFunctions/lanxi.py`
**Add after line 135:**
```python
print(f"DEBUG: Channel {signal.signal_id} scale_factor: {scale_factor}")
```

**Add after line 139:**
```python
print(f"DEBUG: Channel {signal.signal_id} voltage range: {values.min():.6f}V to {values.max():.6f}V")
```

### Step 2: Enhanced Setup Debug Output

**File:** `HelpFunctions/lanxi.py`
**Replace line 72 with:**
```python
print("=" * 50)
print("DEBUG: LAN-XI Channel Setup")
print("=" * 50)
for i, channel in enumerate(self.setup["channels"]):
    if channel.get("enabled", False):
        print(f"Channel {i}:")
        print(f"  Range: {channel.get('range', 'Not set')}")
        print(f"  CCLD: {channel.get('ccld', 'Not set')}")
        print(f"  Filter: {channel.get('filter', 'Not set')}")
        print(f"  Enabled: {channel.get('enabled', False)}")
        if 'transducer' in channel and channel['transducer']:
            trans = channel['transducer']
            print(f"  Transducer Type: {trans.get('type', 'Unknown')}")
            print(f"  Transducer Serial: {trans.get('serialNumber', 'Unknown')}")
        print()
print("=" * 50)
```

### Step 3: Recording Session Debug Output

**File:** `krak_logger_gui.py`
**Add after line 248 (after `time_axis, data = Lanxi.SampleChannels(DURATION)`):**
```python
print(f"DEBUG: Recording completed at {datetime.datetime.now()}")
print(f"DEBUG: AI0 voltage range: {data[0].min():.6f}V to {data[0].max():.6f}V")
print(f"DEBUG: AI0 RMS level: {np.sqrt(np.mean(data[0]**2)):.6f}V")
print(f"DEBUG: AI1 voltage range: {data[1].min():.6f}V to {data[1].max():.6f}V")
print(f"DEBUG: Sample rate: {SAMPLE_RATE} Hz")
print(f"DEBUG: Duration: {DURATION} seconds")
print()
```

### Step 4: WAV Normalization Debug

**File:** `krak_logger_gui.py`
**Add after line 259:**
```python
print(f"DEBUG: WAV normalization - max_voltage: {max_voltage}V")
print(f"DEBUG: WAV audio_data range: {audio_data.min()} to {audio_data.max()}")
print(f"DEBUG: Original voltage range: {data[0].min():.6f}V to {data[0].max():.6f}V")
```

## Testing Protocol

### 1. Baseline Recording
1. Start the application
2. Make one recording - note all debug output
3. Save the console output to a file

### 2. Consecutive Recordings
1. Without restarting the application, make 2-3 more recordings
2. Compare debug output between recordings
3. Look for differences in:
   - Scale factors
   - Channel setup (especially range)
   - Voltage ranges
   - RMS levels

### 3. Restart Test
1. Close and restart the application
2. Make another recording
3. Compare with previous recordings

## What to Look For

### Scale Factor Variations
- Different scale_factor values between recordings
- Pattern: same physical conditions but different digital values

### Range Changes
- Channel range changing between measurements
- Auto-ranging behavior

### Voltage Level Inconsistencies
- Same background noise conditions producing different voltage readings
- RMS levels varying significantly

## Expected Outcomes

### If Scale Factor is the Problem:
- You'll see different scale_factor values printed
- Voltage ranges will vary proportionally
- Channel setup will show consistent range settings

### If Range is the Problem:
- Channel range will change between recordings
- Scale factors may be consistent
- TEDS detection results may vary

### If Hardware Auto-Gain:
- Both scale factors and ranges may vary
- Pattern may correlate with signal amplitude

## Fixes to Consider

### Option 1: Lock the Scale Factor
Modify `lanxi.py` to use a fixed scale factor instead of device-provided one.

### Option 2: Fix the Range
Ensure channel range is explicitly set and doesn't auto-adjust.

### Option 3: Post-Processing Normalization
Apply consistent normalization in post-processing using known reference levels.

### Option 4: Store Raw ADC Values
Bypass the scale factor entirely and store raw ADC counts for later calibration.

## File Locations for Debugging
- Main GUI: `krak_logger_gui.py`
- LAN-XI Interface: `HelpFunctions/lanxi.py`
- Console output contains all debug information