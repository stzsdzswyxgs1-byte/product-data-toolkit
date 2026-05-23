@echo off
chcp 65001 > nul
cd /d "%~dp0"
title USB Forward
"..\platform-tools\adb.exe" forward --remove-all >nul 2>&1
"..\platform-tools\adb.exe" forward tcp:10102 tcp:10102
if errorlevel 1 (
    echo [X] adb forward failed - check USB
    pause
    exit /b 1
)
echo [OK] USB forward established: 127.0.0.1:10102 -^> phone:10102
echo.
echo You can close this window. forward stays until adb server restarts.
timeout /t 3 >nul
