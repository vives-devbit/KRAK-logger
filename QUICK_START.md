# KRAK-Logger Quick Start Guide

## For User u0143459 (Current User) ✅

Your environment is already set up! Just activate and run:

```powershell
.\activate_venv.ps1
python krak_logger_gui.py
```

Or use your personal activation script:

```powershell
.\activate_venv_u0143459.ps1
python krak_logger_gui.py
```

Your virtual environment: **`.venv_u0143459/`**

---

## For User u0141579 ⚠️

You need to set up your environment first. Run this **once**:

```powershell
.\setup_venv.ps1
```

After setup, activate and run:

```powershell
.\activate_venv.ps1
python krak_logger_gui.py
```

Your virtual environment will be: **`.venv_u0141579/`**

---

## Why Did This Change?

The original `.venv` was created with a Python installation that no longer exists, causing the error:

```
No Python at '"C:\Users\u0141579\AppData\Local\Microsoft\WindowsApps\..."
```

Now each user has their own virtual environment that points to their own Python installation. No more conflicts!

---

## Daily Workflow

1. **Activate** your environment:
   ```powershell
   .\activate_venv.ps1
   ```

2. **Run** the application:
   ```powershell
   python krak_logger_gui.py
   ```

3. **Deactivate** when done (optional):
   ```powershell
   deactivate
   ```

---

## Troubleshooting

**Problem:** Script won't run
**Solution:** Enable script execution
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Problem:** Environment not found
**Solution:** Run the setup script
```powershell
.\setup_venv.ps1
```

**Problem:** Import errors or package not found
**Solution:** Reinstall dependencies
```powershell
.\activate_venv.ps1
pip install -r requirements.txt
```

---

## What's Changed

| File/Folder | Purpose |
|------------|---------|
| `.venv_u0143459/` | Virtual environment for user u0143459 ✅ |
| `.venv_u0141579/` | Virtual environment for user u0141579 (after setup) |
| `.venv_broken_backup/` | Backup of the broken environment |
| `setup_venv.ps1` | First-time setup script |
| `activate_venv.ps1` | Auto-detecting activation script |
| `activate_venv_<user>.ps1` | User-specific activation scripts |
| `SETUP_INSTRUCTIONS.md` | Detailed setup documentation |
| `QUICK_START.md` | This file |

---

## Need More Help?

See [SETUP_INSTRUCTIONS.md](SETUP_INSTRUCTIONS.md) for detailed documentation.
