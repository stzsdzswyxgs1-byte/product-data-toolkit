@echo off
chcp 65001
cd /d "%~dp0"

REM ---- Cloud update check (silent, never blocks) ----
if exist "..\_updater\updater.py" (
    python "..\_updater\updater.py" --app goofish_scraper 2>nul
)

python -c "import curl_cffi" 2>err.tmp
if errorlevel 1 (
    echo Installing curl_cffi ...
    pip install curl_cffi -q
)
python -c "import openpyxl" 2>err.tmp
if errorlevel 1 (
    echo Installing openpyxl ...
    pip install openpyxl -q
)
del err.tmp 2>err2.tmp
del err2.tmp
pythonw app.py
if errorlevel 1 (
    python app.py
    pause
)
