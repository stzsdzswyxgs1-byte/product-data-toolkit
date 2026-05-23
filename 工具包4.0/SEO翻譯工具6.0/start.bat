@echo off
chcp 65001 >nul 2>&1
title SEO翻譯工具6.0
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ---- Check Python ----
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [ERROR] Python not found. Install Python 3.10+ first:
    echo  https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM ---- Cloud update check (silent, never blocks) ----
if exist "..\_updater\updater.py" (
    python "..\_updater\updater.py" --app seo_translator 2>nul
)

REM ---- Launch ----
python run.py
if errorlevel 1 (
    echo.
    echo App exited with error code %errorlevel%
    pause
)
