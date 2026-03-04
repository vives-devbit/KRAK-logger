# Virtual Environment Setup Instructions

## Problem
The previous virtual environments were created with Python installations that no longer exist or are inaccessible.

## Solution
Each user creates their own virtual environment using the shared Python 3.14 installation.

## For User: u0141579

```powershell
# Remove old broken venvs (optional, for cleanup)
Remove-Item -Recurse -Force venv, .venv_broken_backup -ErrorAction SilentlyContinue

# Create your venv
C:\Users\u0141579\AppData\Local\Programs\Python\Python314\python.exe -m venv .venv_u0141579

# Activate it
.\.venv_u0141579\Scripts\Activate.ps1

# Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Run the application
python krak_logger_gui.py
```

## For User: u0143459 (Current User)

```powershell
# Remove broken venv
Remove-Item -Recurse -Force .venv_u0143459 -ErrorAction SilentlyContinue

# Create your venv using the accessible Python
C:\Users\u0141579\AppData\Local\Programs\Python\Python314\python.exe -m venv .venv_u0143459

# Activate it
.\.venv_u0143459\Scripts\Activate.ps1

# Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Run the application
python krak_logger_gui.py
```

## Quick Activation Scripts

After setup, use these shortcuts:

**User u0141579:**
```powershell
.\activate_venv_u0141579.ps1  # Will be created by setup script
```

**User u0143459:**
```powershell
.\activate_venv_u0143459.ps1  # Already exists
```

## Why This Works
Both users use the same Python 3.14.1 installation (located in u0141579's directory), but each has their own isolated virtual environment. This is safe because:
- Each venv is independent
- Changes in one venv don't affect the other
- Both users can work on the project simultaneously (though not recommended for file conflicts)
