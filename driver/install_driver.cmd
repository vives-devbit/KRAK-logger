@echo off
REM ---------------------------------------------------------------------------
REM  EVAL-CN0582-USBZ - install the WinUSB driver package (no Zadig needed)
REM
REM  Run as Administrator. Installs on ALL USB ports, not just the one the board
REM  happens to be plugged into, because the INF claims the hardware ID
REM  USB\VID_0456&PID_ED11 rather than a port path.
REM
REM  Do NOT install the "CN0582 EVALUATION SOFTWARE" on this machine afterwards:
REM  it adds ADI's hssi.inf, which claims the same hardware ID and will compete
REM  for any port the board is newly plugged into.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: run this from an Administrator command prompt.
    exit /b 1
)

echo [1/3] trusting the driver certificate ...
REM The catalogue is self-signed by libwdi, so the certificate has to be its own
REM root as well as a trusted publisher, or the package will not verify.
certutil -addstore -f Root            cn0582_winusb.cer || goto :fail
certutil -addstore -f TrustedPublisher cn0582_winusb.cer || goto :fail

echo.
echo [2/3] adding the driver package to the driver store ...
pnputil /add-driver cn-0582.inf /install || goto :fail

echo.
echo [3/3] done. Plug the board in on any port; Windows will bind WinUSB.
echo.
echo Verify with:
echo     pnputil /enum-drivers ^| findstr /i cn-0582
echo     python -c "import usb1;print([hex(d.getProductID()) for d in usb1.USBContext().getDeviceList() if d.getVendorID()==0x0456])"
exit /b 0

:fail
echo.
echo FAILED - see the message above.
exit /b 1
