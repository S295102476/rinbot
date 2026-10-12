@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0New-UsbPackage.ps1" %*
if errorlevel 1 (
    echo Packaging failed. Do not use a folder marked INCOMPLETE.txt.
    pause
    exit /b 1
)
pause
