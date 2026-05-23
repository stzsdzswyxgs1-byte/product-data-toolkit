@echo off
REM Bootstrap-only BAT (ASCII-safe). Calls setup_env.py for the real work.
REM This file MUST stay ASCII-only to avoid Chinese Windows GBK/UTF-8 codepage issues.

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   Product Processor Hub - Auto Installer
echo ============================================
echo.

REM ---------- Step 1: Detect Python ----------
set PY_OK=0
where python >nul 2>nul && (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul && set PY_OK=1
)

if %PY_OK%==1 (
    echo [Step 1/4] Python OK
    goto VENV
)

REM ---------- Auto-install Python (no admin) ----------
echo [Step 1/4] Python not found or version less than 3.10
echo            Auto-downloading Python 3.11.9 installer (~30MB)...

set "PY_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
set "PY_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"

if exist "%PY_INSTALLER%" del "%PY_INSTALLER%" >nul 2>nul

powershell -ExecutionPolicy Bypass -NoProfile -Command "& {[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest '%PY_URL%' -OutFile '%PY_INSTALLER%' -UseBasicParsing } catch { Write-Host 'Download failed:' $_.Exception.Message; exit 1 }}"
if not exist "%PY_INSTALLER%" (
    echo            Download failed.
    echo            Please install Python 3.11 manually:
    echo                https://www.python.org/downloads/
    echo            and re-run setup.bat
    pause
    exit /b 1
)

echo            Installing Python (no admin needed, current user only)...
"%PY_INSTALLER%" /passive InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=1
if errorlevel 1 (
    echo            Python installer exited with error.
    pause
    exit /b 1
)
del "%PY_INSTALLER%" >nul 2>nul

REM Refresh PATH for current shell
set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"

REM Verify
where python >nul 2>nul
if errorlevel 1 (
    echo            Python installed but not on PATH.
    echo            Please CLOSE this window and double-click setup.bat again.
    pause
    exit /b 1
)
echo            Python installed.

:VENV
REM ---------- Step 2-4: All in setup_env.py (proper UTF-8 / no encoding bugs) ----------
echo [Step 2/4] Handing off to Python installer...
echo.

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
python "%~dp0setup_env.py"
if errorlevel 1 (
    echo.
    echo Setup failed. See error above.
    pause
    exit /b 1
)

echo.
echo Press any key to launch the app, or close this window to exit.
pause >nul
call "%~dp0start.bat"
