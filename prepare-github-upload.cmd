@echo off
chcp 65001 >nul
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prepare-github-upload.ps1"
if errorlevel 1 (
    echo.
    echo Failed to prepare the upload folder.
    pause
)

endlocal
