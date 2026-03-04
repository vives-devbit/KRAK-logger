# VS Code Python Extension Setup

This guide shows how to configure VS Code to use the correct user-specific virtual environment.

## Quick Fix (Already Done for u0143459)

I've already configured VS Code for user **u0143459**. The Python extension should now use `.venv_u0143459/Scripts/python.exe`.

## Manual Method (If Quick Fix Doesn't Work)

### Step 1: Open Command Palette

Press `Ctrl+Shift+P` (or `F1`)

### Step 2: Select Python Interpreter

Type: **"Python: Select Interpreter"**

### Step 3: Choose Your Virtual Environment

Look for and select:
- **For u0143459**: `.venv_u0143459\Scripts\python.exe`
- **For u0141579**: `.venv_u0141579\Scripts\python.exe` (after running setup)

If your venv doesn't appear:
1. Click **"Enter interpreter path..."**
2. Click **"Find..."**
3. Navigate to `.venv_<your_username>/Scripts/python.exe`

### Step 4: Verify

Check the bottom-right corner of VS Code. It should show:
```
Python 3.14.2 64-bit ('.venv_u0143459': venv)
```

## For User u0141579

After running `.\setup_venv.ps1`, you need to update VS Code settings:

### Option 1: Use Command Palette (Recommended)

1. Press `Ctrl+Shift+P`
2. Type "Python: Select Interpreter"
3. Choose `.venv_u0141579\Scripts\python.exe`

### Option 2: Edit Settings File

Edit `.vscode/settings.json` and change:

```json
"python.defaultInterpreterPath": "${workspaceFolder}/.venv_u0141579/Scripts/python.exe",
```

## Integrated Terminal Setup

VS Code's integrated terminal will automatically activate your venv when you open a new terminal, thanks to this setting:

```json
"python.terminal.activateEnvironment": true
```

## Troubleshooting

### Problem: VS Code shows wrong Python version

**Solution:**
1. Close all terminals in VS Code (`Terminal` → `Kill All Terminals`)
2. Reload VS Code window (`Ctrl+Shift+P` → `Developer: Reload Window`)
3. Select the correct interpreter again

### Problem: Import errors even with correct interpreter

**Solution:**
1. Make sure the virtual environment is activated in the terminal
2. Install missing packages: `pip install -r requirements.txt`
3. Restart the Python language server: `Ctrl+Shift+P` → `Python: Restart Language Server`

### Problem: Can't find the virtual environment

**Solution:**
1. Make sure you ran `setup_venv.ps1` first
2. Check that `.venv_<username>` folder exists
3. Manually enter the path: `.venv_<username>/Scripts/python.exe`

## Settings Explained

Your [.vscode/settings.json](c:\KRAK-logger\.vscode\settings.json) file contains:

```json
{
    // Points to your user-specific virtual environment
    "python.defaultInterpreterPath": "${workspaceFolder}/.venv_u0143459/Scripts/python.exe",

    // Auto-activates venv when opening terminal
    "python.terminal.activateEnvironment": true,

    // Basic type checking for better IntelliSense
    "python.analysis.typeCheckingMode": "basic",

    // Hide Python cache files from explorer
    "files.exclude": {
        "**/__pycache__": true,
        "**/*.pyc": true,
        ".venv_broken_backup": true
    },

    // Don't watch venv folders for changes (performance)
    "files.watcherExclude": {
        "**/.venv_*/**": true,
        "**/temp_files/**": true
    }
}
```

## Running Python Files

Once configured, you can:

1. **Run current file**: Press `F5` or `Ctrl+F5`
2. **Run in terminal**: Right-click → `Run Python File in Terminal`
3. **Debug**: Set breakpoints and press `F5`

All of these will use your correct virtual environment automatically.

## Check Current Configuration

Open a new terminal in VS Code and run:

```powershell
python --version
where python
```

Should show:
```
Python 3.14.2
C:\KRAK-logger\.venv_u0143459\Scripts\python.exe
```

## Additional Tips

### Keyboard Shortcuts

- `Ctrl+Shift+P`: Command Palette
- `Ctrl+` `: Toggle integrated terminal
- `F5`: Start debugging
- `Shift+F5`: Stop debugging

### Recommended Extensions

- **Python** (Microsoft) - Already have this
- **Pylance** - Usually installed with Python extension
- **Python Debugger** - For debugging support

## Summary

✅ **Current Setup (u0143459):**
- VS Code configured to use `.venv_u0143459/Scripts/python.exe`
- Terminal auto-activation enabled
- Hidden clutter files in explorer

⚠️ **For u0141579:**
- After running `setup_venv.ps1`
- Use Command Palette to select `.venv_u0141579/Scripts/python.exe`
- Or edit [.vscode/settings.json](c:\KRAK-logger\.vscode\settings.json) to update the path
