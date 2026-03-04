# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None

# Collect all kaitaistruct files
kaitai_datas = []
kaitai_hiddenimports = []
kaitai_binaries = []

# Add kaitaistruct from src directory
kaitai_path = os.path.join('src', 'kaitaistruct')
if os.path.exists(kaitai_path):
    kaitai_datas.append((kaitai_path, 'kaitaistruct'))

# Collect matplotlib data files
mpl_datas, mpl_binaries, mpl_hiddenimports = collect_all('matplotlib')

# Collect pandas data files
pd_datas, pd_binaries, pd_hiddenimports = collect_all('pandas')

# Additional hidden imports
hiddenimports = [
    'numpy',
    'pandas',
    'matplotlib',
    'matplotlib.backends.backend_tkagg',
    'scipy',
    'scipy.io.wavfile',
    'sounddevice',
    'soundfile',
    'minio',
    'dotenv',
    'requests',
    'openpyxl',
    'pyarrow',
    'pyarrow.parquet',
    'fastparquet',
    'tkinter',
    'tkinter.ttk',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'threading',
    'socket',
    'io',
    'datetime',
    'json',
    'subprocess',
    'HelpFunctions',
    'HelpFunctions.lanxi',
    'HelpFunctions.utility',
    'HelpFunctions.Stream',
    'HelpFunctions.Buffer',
    'openapi',
    'openapi.openapi_header',
    'openapi.openapi_stream',
    'kaitaistruct',
] + mpl_hiddenimports + pd_hiddenimports

# Data files to include
datas = [
    ('HelpFunctions', 'HelpFunctions'),
    ('openapi', 'openapi'),
] + mpl_datas + pd_datas + kaitai_datas

a = Analysis(
    ['krak_logger_gui.py'],
    pathex=[],
    binaries=mpl_binaries + pd_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pytest',
        'IPython',
        'jupyter',
        'notebook',
        'sphinx',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KRAK_Logger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Set to True to see debug output, False for GUI-only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon path here if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KRAK_Logger',
)
