@echo off
setlocal enabledelayedexpansion
title Daptar Sync - Installer

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

echo ============================================================
echo    Daptar Sync - Prerequisites Installer
echo ============================================================
echo.

echo [1/5] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.8+ from python.org
    echo         and check "Add Python to PATH" during setup.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo       Found: !PYVER!
echo.

echo [2/5] Ensuring pip...
python -m pip --version >nul 2>&1
if errorlevel 1 (
    python -m ensurepip --upgrade
    if errorlevel 1 (
        echo [ERROR] Cannot install pip.
        pause
        exit /b 1
    )
)
echo       pip OK.
echo.

echo [3/5] Upgrading core tools...
python -m pip install --upgrade pip setuptools wheel --quiet --disable-pip-version-check
echo.

echo [4/5] Installing libraries and PyInstaller...
python -m pip install boto3 pystray Pillow cryptography pyinstaller --disable-pip-version-check
if errorlevel 1 (
    echo       Retrying with alternate settings...
    python -m pip install boto3 pystray Pillow cryptography pyinstaller ^
        --index-url https://pypi.org/simple --timeout 120 --retries 5 --disable-pip-version-check
)
if errorlevel 1 (
    echo [ERROR] Installation failed.
    pause
    exit /b 1
)
echo.

echo [5/5] Verifying...
set MISSING=
python -c "import boto3" 2>nul || set MISSING=!MISSING! boto3
python -c "import pystray" 2>nul || set MISSING=!MISSING! pystray
python -c "from PIL import Image" 2>nul || set MISSING=!MISSING! Pillow
python -c "from cryptography.fernet import Fernet" 2>nul || set MISSING=!MISSING! cryptography
python -c "import PyInstaller" 2>nul || set MISSING=!MISSING! pyinstaller

if defined MISSING (
    echo [ERROR] Missing:!MISSING!
    pause
    exit /b 1
)
echo       All OK.
echo.
echo ============================================================
echo    Done. Now run build.bat to create the .exe
echo ============================================================
pause
endlocal