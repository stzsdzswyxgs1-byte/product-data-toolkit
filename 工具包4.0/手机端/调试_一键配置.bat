@echo on
cd /d "%~dp0"
title Phone Setup (debug)

echo === Python check ===
where python
python --version
if errorlevel 1 (
    echo.
    echo [ERROR] Python not installed or not in PATH
    echo Install Python 3.10+ from python.org, tick "Add to PATH"
    echo.
    pause
    exit /b 1
)

echo.
echo === Script check ===
if not exist "%~dp0setup_phone.py" (
    echo [ERROR] setup_phone.py not found in %~dp0
    pause
    exit /b 1
)
echo OK: setup_phone.py found

echo.
echo === Running setup ===
python "%~dp0setup_phone.py"

echo.
echo === Script exited with code %errorlevel% ===
pause
