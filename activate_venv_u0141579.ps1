# Activation helper for user: u0141579
# Quick script to activate your virtual environment

$venvPath = ".venv_u0141579"

if (Test-Path $venvPath) {
    & "$venvPath\Scripts\Activate.ps1"
    Write-Host "Virtual environment activated for u0141579" -ForegroundColor Green
    Write-Host "Python location: " -NoNewline
    & python --version
    Write-Host ""
} else {
    Write-Host "ERROR: Virtual environment not found at $venvPath" -ForegroundColor Red
    Write-Host ""
    Write-Host "To create it, run these commands:" -ForegroundColor Yellow
    Write-Host "  C:\Users\u0141579\AppData\Local\Programs\Python\Python314\python.exe -m venv .venv_u0141579" -ForegroundColor Cyan
    Write-Host "  .\.venv_u0141579\Scripts\Activate.ps1" -ForegroundColor Cyan
    Write-Host "  python -m pip install --upgrade pip" -ForegroundColor Cyan
    Write-Host "  python -m pip install -r requirements.txt" -ForegroundColor Cyan
}
