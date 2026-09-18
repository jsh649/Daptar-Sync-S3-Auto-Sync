@echo off
setlocal enabledelayedexpansion
title Daptar Sync - Build EXE

cd /d "%~dp0"

echo ============================================================
echo    Building Daptar Sync as a portable Windows application
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)

where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Cannot install PyInstaller.
        pause
        exit /b 1
    )
)

echo Verifying required libraries...
set MISSING=
python -c "import boto3" 2>nul || set MISSING=!MISSING! boto3
python -c "import pystray" 2>nul || set MISSING=!MISSING! pystray
python -c "from PIL import Image" 2>nul || set MISSING=!MISSING! Pillow
python -c "from cryptography.fernet import Fernet" 2>nul || set MISSING=!MISSING! cryptography

if defined MISSING (
    echo [ERROR] Missing libraries:!MISSING!
    echo         Run install.bat first.
    pause
    exit /b 1
)
echo       All libraries are present.
echo.

if not exist "daptar_sync.py" (
    echo [ERROR] daptar_sync.py not found in this folder.
    pause
    exit /b 1
)

set ICON_OPT=
if exist "icon.ico" set ICON_OPT=--icon "icon.ico"

set DATA_OPT=
if exist "icon.png" set DATA_OPT=--add-data "icon.png;."

echo Cleaning previous build...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "DaptarSync.spec" del /q "DaptarSync.spec"
echo.

echo Building DaptarSync.exe - this may take a few minutes...
echo.

python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --onedir ^
    --name "DaptarSync" ^
    --hidden-import "pystray._win32" ^
    --hidden-import "PIL._tkinter_finder" ^
    --collect-all "pystray" ^
    --collect-all "PIL" ^
    --collect-all "boto3" ^
    --collect-all "botocore" ^
    --collect-all "cryptography" ^
    %ICON_OPT% ^
    %DATA_OPT% ^
    daptar_sync.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

if exist "icon.png" (
    copy /y "icon.png" "dist\DaptarSync\icon.png" >nul
)

> "dist\DaptarSync\run.bat" echo @echo off
>> "dist\DaptarSync\run.bat" echo cd /d "%%~dp0"
>> "dist\DaptarSync\run.bat" echo start "" "DaptarSync.exe"

(
    echo Daptar Sync - Portable Version
    echo ================================
    echo.
    echo This folder is fully self-contained and can be copied to
    echo any 64-bit Windows 10/11 system WITHOUT installing Python
    echo or any libraries.
    echo.
    echo HOW TO USE:
    echo   1. Copy the entire DaptarSync folder to the target system.
    echo   2. Run DaptarSync.exe.
    echo   3. On first run, enter your S3 credentials and save.
    echo   4. Click "Start Auto Sync" and optionally enable
    echo      "Run automatically with Windows".
    echo.
    echo FILES CREATED ON FIRST RUN:
    echo   - secret.key       ^(encryption key - DO NOT DELETE^)
    echo   - config.enc       ^(encrypted settings^)
    echo   - daptarsync.log   ^(log file^)
    echo.
    echo IMPORTANT:
    echo   - If you move the folder to another path, uncheck and
    echo     re-check "Run automatically with Windows" so the
    echo     startup shortcut points to the new location.
    echo   - Windows SmartScreen may warn on first run. Click
    echo     "More info" then "Run anyway".
    echo.
) > "dist\DaptarSync\README.txt"

echo.
echo ============================================================
echo    Build completed successfully!
echo ============================================================
echo.
echo Output folder: %CD%\dist\DaptarSync
echo Executable  : DaptarSync.exe
echo.
pause
endlocal