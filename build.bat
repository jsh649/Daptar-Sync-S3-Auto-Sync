@echo off
setlocal enabledelayedexpansion
title Daptar Sync - Build EXE

cd /d "%~dp0"

REM ============================================================
REM  Check for Administrator privileges
REM ============================================================
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [INFO] Administrator privileges required. Requesting UAC...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================================
echo    Building Daptar Sync as a portable Windows application
echo ============================================================
echo.

REM ============================================================
REM  Check Python
REM ============================================================
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    pause
    exit /b 1
)

for /f "delims=" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [INFO] Using %PYVER%
echo.

REM ============================================================
REM  Backup current pip configuration (so we can restore later)
REM ============================================================
set "PIPCFG_FILE=%APPDATA%\pip\pip.ini"
set "BACKUP_FILE=%TEMP%\pip_backup_%RANDOM%.ini"
set "HAD_PIPCFG=0"

if exist "%PIPCFG_FILE%" (
    copy /y "%PIPCFG_FILE%" "%BACKUP_FILE%" >nul 2>&1
    set "HAD_PIPCFG=1"
    echo [INFO] Existing pip.ini backed up.
)

set "OLD_HTTP_PROXY=%http_proxy%"
set "OLD_HTTPS_PROXY=%https_proxy%"
set "OLD_INDEX_URL=%PIP_INDEX_URL%"
set "OLD_TRUSTED_HOST=%PIP_TRUSTED_HOST%"

REM ============================================================
REM  Install PyInstaller (chain of mirrors)
REM ============================================================
set "INSTALLED_OK=0"

where pyinstaller >nul 2>&1
if not errorlevel 1 (
    echo [INFO] PyInstaller already installed.
    set "INSTALLED_OK=1"
    goto :skip_pyinstaller_install
)

echo [INFO] PyInstaller not found. Attempting installation...
echo.

REM --- Attempt 1: default PyPI ---
echo [TRY 1/4] Installing from default PyPI...
python -m pip install --disable-pip-version-check --no-cache-dir pyinstaller >nul 2>&1
if not errorlevel 1 (
    echo          ^> Success via default PyPI.
    set "INSTALLED_OK=1"
    goto :skip_pyinstaller_install
)
echo          ^> Failed.

REM --- Attempt 2: Runflare mirror ---
echo [TRY 2/4] Installing from Runflare mirror...
python -m pip install --disable-pip-version-check --no-cache-dir ^
    -i https://mirror-pypi.runflare.com/simple ^
    --trusted-host mirror-pypi.runflare.com ^
    pyinstaller >nul 2>&1
if not errorlevel 1 (
    echo          ^> Success via Runflare.
    set "INSTALLED_OK=1"
    goto :skip_pyinstaller_install
)
echo          ^> Failed.

REM --- Attempt 3: Tsinghua mirror ---
echo [TRY 3/4] Installing from Tsinghua mirror...
python -m pip install --disable-pip-version-check --no-cache-dir ^
    -i https://pypi.tuna.tsinghua.edu.cn/simple ^
    --trusted-host pypi.tuna.tsinghua.edu.cn ^
    pyinstaller >nul 2>&1
if not errorlevel 1 (
    echo          ^> Success via Tsinghua.
    set "INSTALLED_OK=1"
    goto :skip_pyinstaller_install
)
echo          ^> Failed.

REM --- Attempt 4: Aliyun mirror ---
echo [TRY 4/4] Installing from Aliyun mirror...
python -m pip install --disable-pip-version-check --no-cache-dir ^
    -i https://mirrors.aliyun.com/pypi/simple ^
    --trusted-host mirrors.aliyun.com ^
    pyinstaller >nul 2>&1
if not errorlevel 1 (
    echo          ^> Success via Aliyun.
    set "INSTALLED_OK=1"
    goto :skip_pyinstaller_install
)
echo          ^> Failed.

REM All attempts failed
echo.
echo [ERROR] Could not install PyInstaller from any source.
echo.
echo   Possible causes:
echo     - No internet access
echo     - A proxy/firewall is blocking pip
echo     - Python installation is broken
echo.
echo   Manual workaround:
echo     python -m pip install pyinstaller --proxy http://user:pass@host:port
echo.
goto :cleanup_and_exit

:skip_pyinstaller_install
echo.

REM ============================================================
REM  Verify / install other required libraries
REM  (watchdog is REQUIRED for instant file-change sync;
REM   without it the app falls back to 20s periodic scanning)
REM ============================================================
echo [INFO] Verifying required libraries...
set MISSING=
python -c "import boto3" 2>nul || set MISSING=!MISSING! boto3
python -c "import pystray" 2>nul || set MISSING=!MISSING! pystray
python -c "from PIL import Image" 2>nul || set MISSING=!MISSING! Pillow
python -c "from cryptography.fernet import Fernet" 2>nul || set MISSING=!MISSING! cryptography
python -c "import watchdog" 2>nul || set MISSING=!MISSING! watchdog

if defined MISSING (
    echo [INFO] Missing:!MISSING!
    echo [INFO] Installing missing libraries via default PyPI...
    python -m pip install --disable-pip-version-check --no-cache-dir !MISSING! >nul 2>&1

    set MISSING=
    python -c "import boto3" 2>nul || set MISSING=!MISSING! boto3
    python -c "import pystray" 2>nul || set MISSING=!MISSING! pystray
    python -c "from PIL import Image" 2>nul || set MISSING=!MISSING! Pillow
    python -c "from cryptography.fernet import Fernet" 2>nul || set MISSING=!MISSING! cryptography
    python -c "import watchdog" 2>nul || set MISSING=!MISSING! watchdog

    if defined MISSING (
        echo [INFO] Retrying missing libraries via Runflare mirror...
        python -m pip install --disable-pip-version-check --no-cache-dir ^
            -i https://mirror-pypi.runflare.com/simple ^
            --trusted-host mirror-pypi.runflare.com ^
            !MISSING! >nul 2>&1

        set MISSING=
        python -c "import boto3" 2>nul || set MISSING=!MISSING! boto3
        python -c "import pystray" 2>nul || set MISSING=!MISSING! pystray
        python -c "from PIL import Image" 2>nul || set MISSING=!MISSING! Pillow
        python -c "from cryptography.fernet import Fernet" 2>nul || set MISSING=!MISSING! cryptography
        python -c "import watchdog" 2>nul || set MISSING=!MISSING! watchdog
    )

    if defined MISSING (
        echo [ERROR] Still missing after all attempts:!MISSING!
        echo.
        echo   NOTE: if only "watchdog" is missing, the build can still
        echo   proceed, but instant sync will be disabled in the EXE.
        echo.
    )
)
echo       All required libraries are present.
echo.

REM ============================================================
REM  Verify source file exists
REM ============================================================
if not exist "daptar_sync.py" (
    echo [ERROR] daptar_sync.py not found in this folder.
    goto :cleanup_and_exit
)

REM ============================================================
REM  Icon / data options
REM ============================================================
set ICON_OPT=
if exist "icon.ico" set ICON_OPT=--icon "icon.ico"

set DATA_OPT=
if exist "icon.png" set DATA_OPT=--add-data "icon.png;."

REM ============================================================
REM  Clean previous build
REM ============================================================
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
    --hidden-import "watchdog.observers.read_directory_changes" ^
    --collect-all "pystray" ^
    --collect-all "PIL" ^
    --collect-all "boto3" ^
    --collect-all "botocore" ^
    --collect-all "cryptography" ^
    --collect-all "watchdog" ^
    %ICON_OPT% ^
    %DATA_OPT% ^
    daptar_sync.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    goto :cleanup_and_exit
)

REM ============================================================
REM  Verify watchdog got bundled (instant sync feature)
REM ============================================================
set WATCHDOG_BUNDLED=0
if exist "dist\DaptarSync\_internal\watchdog" set WATCHDOG_BUNDLED=1
if exist "dist\DaptarSync\watchdog" set WATCHDOG_BUNDLED=1

echo.
if "%WATCHDOG_BUNDLED%"=="1" (
    echo [OK]   watchdog bundled - instant file-change sync ENABLED.
) else (
    echo [WARN] watchdog NOT found in build output!
    echo        The app will fall back to 20-second periodic scanning.
    echo        Fix: install watchdog on THIS machine and rebuild:
    echo            python -m pip install watchdog
)
echo.

REM ============================================================
REM  Post-build artifacts
REM ============================================================
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
    echo FILES CREATED ON FIRST RUN ^(in %%APPDATA%%\DaptarSync^):
    echo   - config.enc       ^(encrypted settings^)
    echo   - secret.key       ^(encryption key - DO NOT DELETE^)
    echo   - daptarsync.log   ^(rotating log file^)
    echo.
    echo NOTES:
    echo   - Settings are stored per Windows user in %%APPDATA%%,
    echo     so no admin rights are needed to save settings.
    echo   - If you move this folder to another path, uncheck and
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
goto :cleanup_and_exit

REM ============================================================
REM  Cleanup: restore pip config and proxy env vars in ALL cases
REM ============================================================
:cleanup_and_exit

echo.
echo [INFO] Restoring pip configuration to defaults...

REM Remove any index-url / trusted-host overrides we may have set
python -m pip config unset global.index-url >nul 2>&1
python -m pip config unset global.trusted-host >nul 2>&1
python -m pip config unset install.index-url >nul 2>&1
python -m pip config unset install.trusted-host >nul 2>&1

REM If user had a pip.ini before, restore it
if "%HAD_PIPCFG%"=="1" (
    if exist "%BACKUP_FILE%" (
        copy /y "%BACKUP_FILE%" "%PIPCFG_FILE%" >nul 2>&1
        del /q "%BACKUP_FILE%" >nul 2>&1
        echo [INFO] Original pip.ini restored.
    )
) else (
    REM We never had pip.ini — make sure we don't leave one behind
    if exist "%PIPCFG_FILE%" (
        REM Only delete if it's now empty (contains no [global] with index-url)
        findstr /i "index-url trusted-host" "%PIPCFG_FILE%" >nul 2>&1
        if errorlevel 1 (
            del /q "%PIPCFG_FILE%" >nul 2>&1
            echo [INFO] Temporary pip.ini removed.
        )
    )
)

REM Restore environment variables
if defined OLD_HTTP_PROXY  (set "http_proxy=%OLD_HTTP_PROXY%")  else (set "http_proxy=")
if defined OLD_HTTPS_PROXY (set "https_proxy=%OLD_HTTPS_PROXY%") else (set "https_proxy=")
if defined OLD_INDEX_URL   (set "PIP_INDEX_URL=%OLD_INDEX_URL%") else (set "PIP_INDEX_URL=")
if defined OLD_TRUSTED_HOST (set "PIP_TRUSTED_HOST=%OLD_TRUSTED_HOST%") else (set "PIP_TRUSTED_HOST=")

echo [INFO] Environment restored.
echo.

REM Report final PyInstaller status
where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [WARN] PyInstaller is NOT available in PATH at the end of this run.
) else (
    echo [OK]   PyInstaller is available.
)

echo.
echo Press any key to close this window.
pause >nul
endlocal
exit /b 0
