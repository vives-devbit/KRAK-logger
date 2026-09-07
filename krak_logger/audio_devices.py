"""Audio device enumeration for Focusrite / STWINMA2 via sounddevice."""

import sounddevice as sd

# Substrings identifying the STWINMA2 USB audio device in device names.
STWIN_KEYWORDS = ('stwin', 'steval', 'stm32')

CN0582_SOURCE = "CN0582 (256 kHz)"
STWINMA2_SOURCE = "STWINMA2 Ch0 (192 kHz)"
STWIN_AUTO_DETECT = "Auto-detect"


def list_audio_input_devices():
    """Return list of (index, name) for all input-capable audio devices."""
    try:
        sd._terminate()
        sd._initialize()
        return [(i, d['name']) for i, d in enumerate(sd.query_devices())
                if d['max_input_channels'] > 0]
    except Exception:
        return []


def build_device_choices():
    """Enumerate input devices for the source dropdowns.

    Returns (device_map, source_choices, stwin_choices):
        device_map     -- display name -> sd device index;
                          CN0582_SOURCE -> None, STWINMA2_SOURCE -> "__stwinma2__"
        source_choices -- entries for the main audio-source combobox (all
                          STWINMA2 host-API duplicates excluded)
        stwin_choices  -- entries for the STWINMA2 device combobox
    """
    device_map = {CN0582_SOURCE: None, STWINMA2_SOURCE: "__stwinma2__"}

    # Identify all STWINMA2 device indices (MME + WASAPI instances of the same
    # physical device) so every duplicate is excluded from the generic dropdown.
    stwin_indices = set()
    try:
        for i, d in enumerate(sd.query_devices()):
            if d['max_input_channels'] >= 1:
                if any(k in d['name'].lower() for k in STWIN_KEYWORDS):
                    stwin_indices.add(i)
    except Exception:
        pass

    source_choices = [CN0582_SOURCE, STWINMA2_SOURCE]
    stwin_choices = [STWIN_AUTO_DETECT]
    for idx, name in list_audio_input_devices():
        key = f"{name} [{idx}]"
        device_map[key] = idx
        stwin_choices.append(key)          # all input devices selectable as STWINMA2 target
        if idx not in stwin_indices:       # exclude all STWINMA2 instances from generic dropdown
            source_choices.append(key)

    return device_map, source_choices, stwin_choices


def find_stwinma2_device(selection, device_map):
    """Return sounddevice index for STWINMA2, or None if not found.

    selection is the value of the STWINMA2 device combobox; anything other
    than STWIN_AUTO_DETECT is honoured directly via device_map.

    Auto-detect prefers the WASAPI host-API instance of the device (supports
    192 kHz on Windows).  Falls back to the last name-matched entry in the
    device list, which on Windows is also WASAPI (MME/DS come first, WASAPI last).
    """
    try:
        devices  = sd.query_devices()
        hostapis = sd.query_hostapis()

        # Collect all name-matched input devices with their host-API name
        candidates = []
        for i, d in enumerate(devices):
            if d['max_input_channels'] >= 1:
                n = d['name'].lower()
                if any(k in n for k in STWIN_KEYWORDS):
                    ha = hostapis[d['hostapi']]['name']
                    candidates.append((i, d['name'], ha))
                    print(f"STWINMA2 candidate [{i}] {d['name']}  hostapi={ha}")

        if selection and selection != STWIN_AUTO_DETECT:
            idx = device_map.get(selection)
            if idx is not None and 0 <= idx < len(devices):
                chosen = devices[idx]
                chosen_name = chosen['name']
                chosen_ha = hostapis[chosen['hostapi']]['name']
                if 'wasapi' in chosen_ha.lower():
                    print(f"STWINMA2: using explicit selection '{selection}' -> "
                          f"[{idx}] {chosen_name} ({chosen_ha})")
                    return idx

                # A same-name WASAPI sibling is the reliable high-rate endpoint;
                # the non-WASAPI duplicate (WDM-KS/MME/DirectSound) the dropdown
                # picked is what raises "WdmSyncIoctl ... Windows WDM-KS error".
                for ci, cname, cha in candidates:
                    if cname == chosen_name and 'wasapi' in cha.lower():
                        print(f"STWINMA2: explicit selection '{selection}' maps to "
                              f"non-WASAPI [{idx}] {chosen_name} ({chosen_ha}); "
                              f"using sibling [{ci}] ({cha}) instead")
                        return ci

                print(f"STWINMA2: explicit selection '{selection}' uses non-WASAPI "
                      f"[{idx}] {chosen_name} ({chosen_ha}); falling back to auto-select logic")

        # Priority 1: WASAPI instance
        for i, name, ha in candidates:
            if 'wasapi' in ha.lower():
                print(f"STWINMA2: auto-selected [{i}] {name} (WASAPI)")
                return i

        # Priority 2: last in list (Windows orders MME->DS->WASAPI, so last ~ WASAPI)
        if candidates:
            i, name, ha = candidates[-1]
            print(f"STWINMA2: auto-selected [{i}] {name} (last candidate, hostapi={ha})")
            return i

    except Exception as e:
        print(f"STWINMA2 device search error: {e}")
    return None


def open_stwinma2_stream(selection, device_map, callback, block_size, required_rate=192_000):
    """Open an InputStream on the STWINMA2 device at required_rate, tolerant of
    duplicate host-API endpoints of the same physical device.

    A single fixed device-index attempt is what produces errors like
    "WdmSyncIoctl: DeviceIoControl ... Windows WDM-KS error" -- that is a WDM-KS
    duplicate of the device rejecting the 192 kHz pin/format WASAPI accepts, not
    a hardware fault. This tries every name-matched device index (WASAPI first,
    then WDM-KS/DirectSound/MME) at required_rate only -- AI04 is only ever
    valid at 192 kHz, so there is no lower-rate fallback: if no candidate opens
    at required_rate this raises rather than silently recording slower.

    Returns the started stream. Raises RuntimeError if every candidate failed.
    """
    base_idx = find_stwinma2_device(selection, device_map)
    if base_idx is None:
        raise RuntimeError("Could not find a USB audio input (STWINMA2)")

    try:
        devices  = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as e:
        raise RuntimeError(f"Unable to enumerate audio devices: {e}")

    candidate_rows = []
    for i, d in enumerate(devices):
        if d['max_input_channels'] < 1:
            continue
        if any(k in d['name'].lower() for k in STWIN_KEYWORDS):
            candidate_rows.append((i, d['name'], hostapis[d['hostapi']]['name']))

    def _ha_rank(ha_name):
        s = ha_name.lower()
        if 'wasapi' in s:
            return 0
        if 'wdm-ks' in s or 'wdm ks' in s:
            return 1
        if 'directsound' in s:
            return 2
        if 'mme' in s:
            return 3
        return 9

    ordered, seen = [], set()
    for idx in [base_idx] + [r[0] for r in sorted(candidate_rows, key=lambda r: (_ha_rank(r[2]), r[0]))]:
        if idx not in seen:
            ordered.append(idx)
            seen.add(idx)

    attempt_errors = []

    for idx in ordered:
        d = devices[idx]
        ha_name = hostapis[d['hostapi']]['name']
        try:
            stream = sd.InputStream(
                device=idx, samplerate=required_rate, channels=1, dtype='float32',
                blocksize=block_size, callback=callback,
            )
            stream.start()
            print(f"STWINMA2: opened [{idx}] {d['name']} ({ha_name}) @ {required_rate} Hz")
            return stream
        except Exception as e:
            attempt_errors.append(f"[{idx}] {d['name']} ({ha_name}) @ {required_rate} Hz -> {e}")

    summary = "\n".join(attempt_errors[-6:])
    raise RuntimeError(
        f"Could not open STWINMA2 at {required_rate} Hz on any candidate device.\n{summary}")
