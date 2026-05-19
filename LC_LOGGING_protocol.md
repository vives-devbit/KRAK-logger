# LC_LOGGING - MCU Firmware Protocol Specification

## Overview

`LC_LOGGING` is a UART command that streams calibrated load cell readings to the KRAK Logger PC application during a measurement. It runs concurrently with the DAQ recording (LAN-XI or Focusrite) and is independent of all LAN-XI analog input channels (AI0-AI3).

- **Baud rate:** 115 200, 8N1 (matches all other MCU commands)
- **Sample rate:** 200 Hz (one line every 5 ms)
- **Trigger:** sent by the PC at the start of a recording; stopped at the end

---

## Commands (PC to MCU)

| Command | Description |
|---|---|
| `LC_LOGGING START` | Begin streaming load cell data at 200 Hz |
| `LC_LOGGING STOP` | Stop streaming |

Commands are newline-terminated ASCII strings, consistent with all other KRAK commands.

---

## Response format (MCU to PC)

While streaming is active the MCU sends one line per sample:

```
LC:<value>
```

| Field | Type | Example | Notes |
|---|---|---|---|
| `LC:` | prefix | - | Fixed identifier |
| `<value>` | float | `12.34` | Calibrated Newton value |

**Example stream (5 lines):**
```
LC:12.34
LC:12.41
LC:12.38
LC:12.35
LC:12.40
```

### Why calibrated Newtons?

The MCU already stores the two-point calibration constants (`TARE` and `APN` - ADC counts per Newton) in EEPROM, set via the `CALLOAD` commands in the KRAKalyser diagnostics tab. Applying the calibration on the MCU keeps the Python side simple and ensures the stored `"Load Cell (N)"` parquet column always contains physical units without requiring the user to manage a separate calibration constant in the logger UI.

If the MCU has not been calibrated yet, stream the raw 12-bit ADC value as a float and advise the user to run the calibration procedure first.

---

## Timing requirements

| Requirement | Value |
|---|---|
| Nominal interval | 5 ms (200 Hz) |
| Jitter tolerance | +/- 0.5 ms |
| Max line latency after sample | < 2 ms |

Use a 5 ms hardware timer interrupt (e.g. TIM6/TIM7 on STM32) to read PA1 (load cell ADC input) and queue the formatted line. Transmit from the main loop or a low-priority interrupt to avoid blocking higher-priority tasks.

---

## Recommended implementation sketch (STM32 HAL)

```c
// Timer ISR - fires every 5 ms
void TIM6_DAC_IRQHandler(void) {
    HAL_TIM_IRQHandler(&htim6);
    if (lc_logging_active) {
        uint32_t adc_raw = read_load_cell_adc();   // PA1, 12-bit
        float newtons = (adc_raw - tare) / apn;    // apply stored calibration
        char buf[32];
        snprintf(buf, sizeof(buf), "LC:%.2f\r\n", newtons);
        HAL_UART_Transmit_IT(&huart2, (uint8_t*)buf, strlen(buf));
    }
}

// Command parser (existing dispatch table)
else if (strcmp(cmd, "LC_LOGGING START") == 0) {
    lc_logging_active = true;
    HAL_TIM_Base_Start_IT(&htim6);
}
else if (strcmp(cmd, "LC_LOGGING STOP") == 0) {
    lc_logging_active = false;
    HAL_TIM_Base_Stop_IT(&htim6);
}
```

> **Note:** If TIM6 is already used by another peripheral, use any free general-purpose timer configured for a 5 ms overflow.

---

## Behaviour during a recording

```
PC                              MCU
 |-- LC_LOGGING START ---------->|   (sent ~1 ms before audio recording starts)
 |<-- LC:12.34 ------------------|   t = 0 ms
 |<-- LC:12.41 ------------------|   t = 5 ms
 |       ...                     |
 |<-- LC:12.38 ------------------|   t = N*5 ms
 |-- LC_LOGGING STOP ----------->|   (sent immediately after audio stops)
```

The PC timestamps each received line relative to the start of the recording using `time.perf_counter()`, then linearly interpolates the 200 Hz load cell trace onto the audio time axis before saving to the parquet file. A latency of a few milliseconds in the `LC_LOGGING START` command is acceptable; it only affects the first few samples at the head of the trace.

---

## Error handling

| Situation | Expected behaviour |
|---|---|
| `LC_LOGGING START` received while already streaming | Reset the stream (restart timer, clear pending TX) |
| `LC_LOGGING STOP` received while not streaming | No-op, no response needed |
| Load cell ADC read fails | Omit the line for that sample (do not send `LC:nan`) |
| MCU reset during logging | PC fills the gap with NaN on interpolation |

---

## Interaction with existing commands

`LC_LOGGING` is independent of `ADCSTREAM`. Both can run simultaneously if needed (e.g. for diagnostics during a recording), but this is not the normal use case. The `ADCSTREAM` command streams both current sense (PA0) and load cell (PA1) raw ADC counts at a user-specified interval for diagnostic purposes and should **not** be used as a substitute for `LC_LOGGING` in production recordings.