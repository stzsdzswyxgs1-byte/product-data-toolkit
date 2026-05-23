@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Install Python Dependencies

echo ============================================
echo   Step 1: Install Python dependencies
echo ============================================
echo.

python --version
if errorlevel 1 (
    echo.
    echo [ERROR] python not in PATH. Install Python 3.10+ from python.org first
    pause
    exit /b 1
)

echo.
echo Installing: curl_cffi / requests / openpyxl ...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] pip install failed
    pause
    exit /b 1
)

echo.
echo ============================================
echo   DONE. Next: double-click xianyu.bat
echo ============================================
pause
