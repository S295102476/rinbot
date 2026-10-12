@echo off
setlocal
if not exist "%~dp0Restore-Dev.ps1" (
  echo ERROR: Restore-Dev.ps1 is missing from this package.
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Restore-Dev.ps1" %*
set "restore_exit=%ERRORLEVEL%"
if not "%restore_exit%"=="0" (
  echo Restore failed. Check the error message above.
)
if "%~1"=="" pause
exit /b %restore_exit%
