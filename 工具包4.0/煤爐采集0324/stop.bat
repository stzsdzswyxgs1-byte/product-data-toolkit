@echo off
title 煤爐采集器 - 停止
setlocal enabledelayedexpansion

echo.
echo  正在查找運行中的採集器...

set FOUND=0
set "KILLED=,"
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":3030 " ^| findstr "LISTENING"') do (
    echo !KILLED! | findstr ",%%p," >nul
    if errorlevel 1 (
        echo  發現進程 PID=%%p,正在停止...
        taskkill /F /PID %%p >nul 2>&1
        if not errorlevel 1 echo  已停止 PID=%%p
        set "KILLED=!KILLED!%%p,"
        set FOUND=1
    )
)

if "%FOUND%" == "0" (
    echo  沒有運行中的採集器
) else (
    echo.
    echo  採集器已停止
)
echo.
echo  注意:如果你設置了開機自啟動,下次重啟時會自動再啟動
echo        如要完全禁用,請執行 autostart_remove.bat
echo.
pause
