@echo off
REM ============================================
REM   Product Processor Hub - Smart Launcher
REM   First run: auto-install dependencies
REM   Later runs: launch directly
REM   ASCII-only file (no encoding issues)
REM   4.0.46: cmd uses system codepage (GBK on zh-cn) to parse .bat,
REM           any non-ASCII char will break parsing. NO Chinese here.
REM           Update-fail abort is handled by processors\_update_check.py.
REM ============================================

cd /d "%~dp0"
REM 4.0.49: NEVER use 'chcp 65001' here - it breaks cmd parser (errorlevel becomes 'orlevel')
REM   This is a documented Windows bug. Keep console at default OEM codepage (936/GBK on zh-cn).
REM   Updater Chinese output is re-encoded by _update_check.py using locale.getpreferredencoding,
REM   so it displays correctly without touching console codepage.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ---- Try .venv Python first ----
if not exist ".venv\Scripts\python.exe" goto TRY_SYSTEM
".venv\Scripts\python.exe" -c "import simple_lama_inpainting, torch, pandas" >nul 2>nul
if errorlevel 1 goto TRY_SYSTEM

REM ---- Cloud update check + abort if (new version found but download failed) ----
REM   4.0.49: removed 'type' here - _update_check.py prints the log using
REM   console codepage (GBK), so Chinese displays correctly without chcp tricks.
if not exist "..\_updater\updater.py" goto LAUNCH_VENV
".venv\Scripts\python.exe" "..\_updater\updater.py" --app product_hub > "%TEMP%\toolkit_updater.log" 2>&1
if not exist "processors\_update_check.py" goto LAUNCH_VENV
".venv\Scripts\python.exe" "processors\_update_check.py" "%TEMP%\toolkit_updater.log"
if errorlevel 2 goto UPDATE_FAILED
goto LAUNCH_VENV

:UPDATE_FAILED
echo.
echo =========================================================
echo   *** UPDATE FAILED - OLD VERSION BLOCKED ***
echo   Reason: VPN/SSL flap or network jitter
echo   Fix 1: Wait 1-2 min, click start.bat again
echo   Fix 2: Improve network, then retry
echo   Fix 3: If keeps failing, contact admin
echo =========================================================
echo.
pause
exit /b 1

:LAUNCH_VENV
echo Launching with .venv Python...
".venv\Scripts\python.exe" app.py
goto END

REM ---- Try system Python ----
:TRY_SYSTEM
where python >nul 2>nul
if errorlevel 1 goto NEED_SETUP
python -c "import simple_lama_inpainting, torch, pandas" >nul 2>nul
if errorlevel 1 goto NEED_SETUP
echo Launching with system Python...
python app.py
goto END

REM ---- No working Python: auto-run setup ----
:NEED_SETUP
echo.
echo =========================================================
echo   First-time setup needed (or dependencies missing)
echo   Will auto-install everything now (5-15 minutes)...
echo =========================================================
echo.

REM ---- 4.0.20: run cloud update before setup so setup_env.py is latest
REM      previous bug: deleting .venv + rerun used stale local source
REM      use system python (updater is pure stdlib, no venv deps)
where python >nul 2>nul
if not errorlevel 1 (
    if exist "..\_updater\updater.py" (
        echo Checking cloud update before setup...
        python "..\_updater\updater.py" --app product_hub
        echo.
    )
)

if not exist "setup.bat" (
    echo ERROR: setup.bat not found in this folder.
    echo Please re-extract the toolkit zip.
    pause
    exit /b 1
)
call "%~dp0setup.bat"

REM Setup will recurse-call this start.bat at end. After return we're done.
goto END

:END
if errorlevel 1 (
    echo.
    echo App exited with error code %errorlevel%
    pause
)
