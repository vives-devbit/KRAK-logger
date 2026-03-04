@echo off
REM KRAK-Logger Virtual Environment Activation (CMD/Batch version)
REM Automatically detects current user and activates their virtual environment

echo KRAK-Logger Environment Activation
echo ===================================
echo.

REM Get current username
set username=%USERNAME%
echo Current user: %username%
echo.

REM Check for user-specific venv
if exist ".venv_%username%\Scripts\activate.bat" (
    echo Activating virtual environment: .venv_%username%
    call ".venv_%username%\Scripts\activate.bat"
    echo.
    echo Virtual environment activated!
    echo Python version:
    python --version
    echo.
    echo To run the application:
    echo   python krak_logger_gui.py
    echo.
) else if exist ".venv\Scripts\activate.bat" (
    echo User-specific environment not found.
    echo Using shared .venv ^(may cause issues if created by different user^)
    echo.
    call ".venv\Scripts\activate.bat"
    echo.
    echo Virtual environment activated!
    echo Python version:
    python --version
    echo.
    echo Consider creating your own environment by running:
    echo   setup_venv.ps1
    echo.
) else (
    echo ERROR: No virtual environment found!
    echo.
    echo To create your virtual environment, run:
    echo   setup_venv.ps1
    echo.
    exit /b 1
)
