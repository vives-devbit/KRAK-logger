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
    if selection and selection != STWIN_AUTO_DETECT:
        idx = device_map.get(selection)
        if idx is not None:
            print(f"STWINMA2: using explicit selection '{selection}' -> device {idx}")
            return idx
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
