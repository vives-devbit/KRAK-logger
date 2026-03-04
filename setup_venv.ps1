# KRAK-Logger Virtual Environment Setup Script
# This script creates a user-specific virtual environment for the project
# Each user gets their own venv to avoid Python version conflicts

Write-Host "KRAK-Logger Virtual Environment Setup" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# Get current username
$username = $env:USERNAME
Write-Host "Current user: $username" -ForegroundColor Green

# Define venv path based on username
$venvPath = ".venv_$username"

# Check if venv already exists
if (Test-Path $venvPath) {
    Write-Host ""
    Write-Host "Virtual environment already exists at: $venvPath" -ForegroundColor Yellow
    $response = Read-Host "Do you want to recreate it? (y/N)"
    if ($response -ne "y" -and $response -ne "Y") {
        Write-Host "Setup cancelled. Use activate_venv.ps1 to activate your existing environment." -ForegroundColor Cyan
        exit 0
    }
    Write-Host "Removing existing virtual environment..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $venvPath
}

# Find Python executable
Write-Host ""
Write-Host "Searching for Python installation..." -ForegroundColor Cyan

# Try common Python locations
$pythonPaths = @(
    "py",  # Python launcher (recommended for Windows)
    "python",
    "python3"
)

$pythonExe = $null
foreach ($pyCmd in $pythonPaths) {
    try {
        $version = & $pyCmd --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            $pythonExe = $pyCmd
            Write-Host "Found Python: $version" -ForegroundColor Green
            break
        }
    } catch {
        continue
    }
}

if (-not $pythonExe) {
    Write-Host "ERROR: Python not found!" -ForegroundColor Red
    Write-Host "Please install Python from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "Make sure to check 'Add Python to PATH' during installation." -ForegroundColor Red
    exit 1
}

# Create virtual environment
Write-Host ""
Write-Host "Creating virtual environment at: $venvPath" -ForegroundColor Cyan
& $pythonExe -m venv $venvPath

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to create virtual environment!" -ForegroundColor Red
    exit 1
}

Write-Host "Virtual environment created successfully!" -ForegroundColor Green

# Activate the venv and install dependencies
Write-Host ""
Write-Host "Installing project dependencies..." -ForegroundColor Cyan
Write-Host "This may take a few minutes..." -ForegroundColor Yellow

$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
& $activateScript

# Upgrade pip
Write-Host ""
Write-Host "Upgrading pip..." -ForegroundColor Cyan
& python -m pip install --upgrade pip --quiet

# Install requirements
if (Test-Path "requirements.txt") {
    Write-Host ""
    Write-Host "Installing packages from requirements.txt..." -ForegroundColor Cyan
    & python -m pip install -r requirements.txt

    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "WARNING: Some packages failed to install!" -ForegroundColor Yellow
        Write-Host "You may need to install them manually." -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "All dependencies installed successfully!" -ForegroundColor Green
    }
} else {
    Write-Host "WARNING: requirements.txt not found!" -ForegroundColor Yellow
}

# Create activation helper script for this user
$activationHelperPath = "activate_venv_$username.ps1"
$activationHelper = @"
# Activation helper for user: $username
# Quick script to activate your virtual environment

`$venvPath = ".venv_$username"

if (Test-Path `$venvPath) {
    & "`$venvPath\Scripts\Activate.ps1"
    Write-Host "Virtual environment activated for $username" -ForegroundColor Green
    Write-Host "Python location: " -NoNewline
    & python --version
    Write-Host ""
} else {
    Write-Host "ERROR: Virtual environment not found at `$venvPath" -ForegroundColor Red
    Write-Host "Run setup_venv.ps1 to create it." -ForegroundColor Yellow
}
"@

Set-Content -Path $activationHelperPath -Value $activationHelper
Write-Host ""
Write-Host "Created activation helper: $activationHelperPath" -ForegroundColor Green

# Summary
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Your virtual environment: $venvPath" -ForegroundColor Cyan
Write-Host ""
Write-Host "To activate your environment:" -ForegroundColor Cyan
Write-Host "  .\activate_venv_$username.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "Or manually:" -ForegroundColor Cyan
Write-Host "  .\$venvPath\Scripts\Activate.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "To run the application:" -ForegroundColor Cyan
Write-Host "  python krak_logger_gui.py" -ForegroundColor Yellow
Write-Host ""
