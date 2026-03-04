# KRAK-Logger Virtual Environment Activation Script
# Automatically detects current user and activates their virtual environment

# Get current username
$username = $env:USERNAME
$venvPath = ".venv_$username"

Write-Host "KRAK-Logger Environment Activation" -ForegroundColor Cyan
Write-Host "Current user: $username" -ForegroundColor Green
Write-Host ""

# Check for user-specific venv
if (Test-Path $venvPath) {
    Write-Host "Activating virtual environment: $venvPath" -ForegroundColor Cyan
    & "$venvPath\Scripts\Activate.ps1"

    if ($LASTEXITCODE -eq 0) {
        Write-Host "Virtual environment activated!" -ForegroundColor Green
        Write-Host "Python version: " -NoNewline
        & python --version
        Write-Host ""
        Write-Host "To run the application:" -ForegroundColor Cyan
        Write-Host "  python krak_logger_gui.py" -ForegroundColor Yellow
        Write-Host ""
    }
} elseif (Test-Path ".venv") {
    # Fallback to shared .venv if it exists
    Write-Host "User-specific environment not found." -ForegroundColor Yellow
    Write-Host "Using shared .venv (may cause issues if created by different user)" -ForegroundColor Yellow
    Write-Host ""
    & ".venv\Scripts\Activate.ps1"

    if ($LASTEXITCODE -eq 0) {
        Write-Host "Virtual environment activated!" -ForegroundColor Green
        Write-Host "Python version: " -NoNewline
        & python --version
        Write-Host ""
        Write-Host "Consider creating your own environment by running:" -ForegroundColor Cyan
        Write-Host "  .\setup_venv.ps1" -ForegroundColor Yellow
        Write-Host ""
    }
} else {
    Write-Host "ERROR: No virtual environment found!" -ForegroundColor Red
    Write-Host ""
    Write-Host "To create your virtual environment, run:" -ForegroundColor Cyan
    Write-Host "  .\setup_venv.ps1" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
