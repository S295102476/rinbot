@echo off
setlocal
set "BUNDLE_ROOT=%~dp0"
if exist "%BUNDLE_ROOT%INCOMPLETE.txt" (
    echo This package is incomplete. Create a new package on the original PC.
    pause
    exit /b 1
)
if not exist "%BUNDLE_ROOT%apps\vscode\Code.exe" (
    echo Missing VS Code. Copy the entire package folder, then retry.
    pause
    exit /b 1
)
if not exist "%BUNDLE_ROOT%workspace\qq-bot-py\bot.py" (
    echo Missing project workspace. Copy the entire package folder, then retry.
    pause
    exit /b 1
)
if not exist "%BUNDLE_ROOT%apps\git\cmd\git.exe" (
    echo Missing bundled Git. Copy the entire package folder, then retry.
    pause
    exit /b 1
)
if not exist "%BUNDLE_ROOT%apps\node\node.exe" (
    echo Missing bundled Node.js. Copy the entire package folder, then retry.
    pause
    exit /b 1
)
set "PATH=%BUNDLE_ROOT%apps\git\cmd;%BUNDLE_ROOT%apps\node;%PATH%"
set "ELECTRON_RUN_AS_NODE="
set "VSCODE_DEV="
cd /d "%BUNDLE_ROOT%workspace"
if errorlevel 1 exit /b 1
start "" "%BUNDLE_ROOT%apps\vscode\Code.exe" --new-window --user-data-dir "%BUNDLE_ROOT%vscode-user" --extensions-dir "%BUNDLE_ROOT%extensions" "%BUNDLE_ROOT%workspace"
