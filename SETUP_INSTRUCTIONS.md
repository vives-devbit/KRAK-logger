# KRAK-Logger Multi-User Setup Instructions

This project supports multiple users working on the same codebase with different Python installations. Each user gets their own virtual environment to avoid conflicts.

## Quick Start

### First Time Setup

Run the setup script to create your virtual environment:

```powershell
.\setup_venv.ps1
```

This will:
- Detect your username automatically
- Create a virtual environment specific to you (`.venv_<username>`)
- Install all required dependencies
- Create a personalized activation script

### Daily Use

Activate your virtual environment before running the application:

```powershell
.\activate_venv.ps1
```

Or use your personalized activation script:

```powershell
.\activate_venv_<username>.ps1
```

Then run the application:

```powershell
python krak_logger_gui.py
```

## How It Works

### User-Specific Virtual Environments

Each user has their own virtual environment:
- **User u0143459**: `.venv_u0143459/`
- **User u0141579**: `.venv_u0141579/` (needs to run setup_venv.ps1)
- Any other user: `.venv_<username>/`

These environments are:
- **Isolated**: Each user's Python version and packages don't interfere
- **Independent**: You can use different Python versions if needed
- **Ignored by Git**: Won't be committed to version control (see `.gitignore`)

### Why This Approach?

The original `.venv` was created by user u0141579 with a Python installation that no longer exists. This caused errors when user u0143459 tried to use it. With user-specific environments:

✅ Each user has a working environment
✅ No conflicts between different Python versions
✅ No permission issues accessing other users' directories
✅ Easy to recreate if something breaks

## Troubleshooting

### "Python not found" Error

Make sure Python is installed and added to your PATH:
1. Download from https://www.python.org/downloads/
2. During installation, check "Add Python to PATH"
3. Restart PowerShell after installation

### "Virtual environment not found" Error

Run the setup script to create your environment:

```powershell
.\setup_venv.ps1
```

### "Execution policy" Error

If you get an error about script execution being disabled:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then run the setup script again.

### Recreating Your Environment

If your environment gets corrupted:

1. Delete your user-specific venv folder:
   ```powershell
   Remove-Item -Recurse -Force .venv_<username>
   ```

2. Run setup again:
   ```powershell
   .\setup_venv.ps1
   ```

## For Git Users

The following are ignored by Git (see `.gitignore`):
- `.venv` (old shared environment)
- `.venv_*` (all user-specific environments)
- `.venv_broken_backup` (backup of the broken environment)
- `.env` (environment configuration file)

This means each user maintains their own environment locally, and it won't cause Git conflicts.

## Manual Setup (Advanced)

If you prefer to create your environment manually:

```powershell
# Create venv
python -m venv .venv_<your_username>

# Activate it
.\.venv_<your_username>\Scripts\Activate.ps1

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

## File Structure

```
KRAK-logger/
├── .venv_u0143459/          # User u0143459's environment
├── .venv_u0141579/          # User u0141579's environment (after setup)
├── .venv_broken_backup/     # Backup of broken environment
├── setup_venv.ps1           # Setup script (run once)
├── activate_venv.ps1        # Auto-detecting activation script
├── activate_venv_u0143459.ps1  # User-specific activation script
├── requirements.txt         # Python package dependencies
├── krak_logger_gui.py      # Main application
└── SETUP_INSTRUCTIONS.md   # This file
```

## Current Status

- **u0143459**: ✅ Virtual environment ready at `.venv_u0143459/`
- **u0141579**: ⚠️ Needs to run `.\setup_venv.ps1` to create their environment

## Questions?

If you encounter any issues, check:
1. Your Python installation is working: `python --version`
2. Your virtual environment exists: `ls .venv_*`
3. Requirements file exists: `ls requirements.txt`

For additional help, contact your team lead or check the project documentation.
