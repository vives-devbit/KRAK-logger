"""KRAK Logger application package.

Modules:
    config        -- paths, environment loading, shared constants
    cn0582_daq    -- CN0582 DAQ initialisation and acquisition adapter
    cn0582_settings_tab -- CN0582 live configuration notebook page
    loadcell      -- MCU load-cell streaming (LC_LOGGING protocol)
    disk_buffer   -- disk-backed audio buffer for high-rate capture
    audio_devices -- sounddevice enumeration / STWINMA2 detection
    storage       -- MinIO client, file listing, search-index updates
    ml            -- ensemble model classes and feature-name parsing
    mel_tab       -- Mel Spectrogram tab
    sp_tabs       -- Set Window / Feature Extraction / Deep Predict tabs
    logger_tab    -- KRAK Logger recording tab
    app           -- main window assembly and entry point
"""
