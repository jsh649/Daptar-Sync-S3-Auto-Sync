@echo off
cd /d "%~dp0"

if exist "DaptarSync.exe" (
    start "" "DaptarSync.exe"
    exit /b
)

where pythonw >nul 2>&1
if %errorlevel% equ 0 (
    start "" pythonw "daptar_sync.py"
) else (
    start "" python "daptar_sync.py"
)