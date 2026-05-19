# LC_LOGGING — MCU Firmware Protocol Specification

## Overview

`LC_LOGGING` is a UART command that streams load cell readings to the KRAK Logger PC
application during a measurement. It runs concurrently with the DAQ recording and is
independent of all LAN-XI / Focusrite audio channels.

- **Baud rate:** 115 200, 8N1
- **Sample rate:** 200 Hz (one sample every 5 ms)
- **Trigger:** sent by PC when the audio stream opens; ends automatically after `<duration_s>`

---

## Commands (PC -> MCU)

| Command | Description |
|---|---|
| `LC_LOGGING START <duration_s>\n` | Begin streaming for exactly `<duration_s>` seconds |
| `LC_LOGGING STOP\n` | Emergency stop (PC cancelled early or error) |

`<duration_s>` is a positive integer, e.g. `LC_LOGGING START 14\n` for a 14-second recording.

---

## Response sequence (MCU -> PC)

```
LC:START\n                  <- sent once, immediately when the 200 Hz timer starts
LC:<value>\n                <- one line per sample at 200 Hz
LC:<value>\n
  ...  (duration_s * 200 lines total)
LC:END\n                    <- sent after the last sample is transmitted
```

| Line | Meaning |
|---|---|
| `LC:START` | Timer has started; PC resets its sample buffer |
| `LC:<float>` | One calibrated sample (raw ADC count or Newton value) |
| `LC:END` | All samples have been transmitted; PC may now finalise the data |

**Example (5 samples, then end):**
```
LC:START
LC:567
LC:572
LC:580
LC:578
LC:591
LC:END
```

---

## Why LC:START and LC:END matter

The MCU typically **buffers** outgoing UART data and flushes it in batches. From the PC
side, all samples may arrive seconds after they were acquired. Without `LC:END` the PC
cannot know when to stop waiting, and would return partial data. With `LC:END` the PC
simply blocks until the marker arrives, guaranteeing every sample is captured regardless
of how the MCU schedules its UART transmissions.

`LC:START` lets the PC reset the buffer in case a previous stream was interrupted.

---

## Timing requirements

| Requirement | Value |
|---|---|
| Nominal interval | 5 ms (200 Hz) |
| Jitter tolerance | ± 0.5 ms |
| `LC:START` latency after command | < 5 ms |
| `LC:END` sent | after the last sample is **queued** in the UART TX buffer |

---

## Recommended STM32 HAL implementation

```c
static bool     lc_logging_active  = false;
static uint32_t lc_sample_count    = 0;
static uint32_t lc_target_samples  = 0;

// Command parser (existing dispatch table)
else if (strncmp(cmd, "LC_LOGGING START", 16) == 0) {
    uint32_t duration_s = (uint32_t)atoi(cmd + 17);   // parse the integer after "START "
    lc_target_samples  = duration_s * 200;             // total samples at 200 Hz
    lc_sample_count    = 0;
    lc_logging_active  = true;
    HAL_UART_Transmit(&huart2, (uint8_t*)"LC:START\r\n", 10, HAL_MAX_DELAY);
    HAL_TIM_Base_Start_IT(&htim6);                     // start 5 ms timer
}
else if (strcmp(cmd, "LC_LOGGING STOP") == 0) {
    lc_logging_active = false;
    HAL_TIM_Base_Stop_IT(&htim6);
    // no LC:END sent on forced stop -- PC will handle timeout
}

// Timer ISR -- fires every 5 ms
void TIM6_DAC_IRQHandler(void) {
    HAL_TIM_IRQHandler(&htim6);
    if (!lc_logging_active) return;

    if (lc_sample_count >= lc_target_samples) {
        // All samples acquired -- stop timer and signal end
        lc_logging_active = false;
        HAL_TIM_Base_Stop_IT(&htim6);
        HAL_UART_Transmit_IT(&huart2, (uint8_t*)"LC:END\r\n", 8);
        return;
    }

    uint32_t adc_raw = read_load_cell_adc();            // PA1, 12-bit
    char buf[32];
    snprintf(buf, sizeof(buf), "LC:%lu\r\n", adc_raw); // raw ADC count
    // or: snprintf(buf, sizeof(buf), "LC:%.2f\r\n", (adc_raw - tare) / apn); // Newtons
    HAL_UART_Transmit_IT(&huart2, (uint8_t*)buf, strlen(buf));
    lc_sample_count++;
}
```

> **Note:** `HAL_UART_Transmit_IT` queues the transmission. The MCU may buffer many
> samples before the UART hardware sends them. `LC:END` is queued after the last sample,
> so it always arrives at the PC after all sample lines -- regardless of buffering.

---

## Full sequence diagram

```
PC                                      MCU
 |-- LC_LOGGING START 14 -------------->|   duration = 14 s
 |<-- LC:START --------------------------|   timer started
 |<-- LC:567 ----------------------------|   t = 5 ms
 |<-- LC:572 ----------------------------|   t = 10 ms
 |          ...  (2800 lines total)      |
 |<-- LC:591 ----------------------------|   t = 14 000 ms
 |<-- LC:END ----------------------------|   all samples transmitted
```

PC behaviour:
1. Sends `LC_LOGGING START <n>` when the audio stream opens.
2. Collects all `LC:<value>` lines into a buffer.
3. Blocks in `stop()` until `LC:END` is received (or a `duration + 10 s` safety timeout).
4. Reconstructs the time axis as `np.arange(n) / 200.0` -- sample index is the clock.

---

## Error handling

| Situation | Expected behaviour |
|---|---|
| `LC_LOGGING START` while already streaming | Reset counter, restart timer, re-send `LC:START` |
| `LC_LOGGING STOP` received mid-stream | Stop immediately; do **not** send `LC:END` |
| ADC read fails for one sample | Omit that line (skip, do not send `LC:nan`) |
| PC timeout before `LC:END` | PC returns partial data with `np.arange(n)/200.0` |

---

## Interaction with other commands

`LC_LOGGING` is independent of `ADCSTREAM`. `ADCSTREAM` streams raw ADC counts for
diagnostics and must not be used as a substitute for `LC_LOGGING`.