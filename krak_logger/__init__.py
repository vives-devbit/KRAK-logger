"""KRAK Logger application package.

Modules:
    config        -- paths, environment loading, shared constants
    lanxi_daq     -- LAN-XI DAQ initialisation
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
