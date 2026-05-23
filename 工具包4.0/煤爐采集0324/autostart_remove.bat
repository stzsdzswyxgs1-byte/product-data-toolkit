@echo off
title 煤爐采集器 - 移除開機自啟動

set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_NAME=煤爐采集器.lnk

echo.
echo  移除開機自啟動...
echo.
if exist "%STARTUP_DIR%\%SHORTCUT_NAME%" (
    del "%STARTUP_DIR%\%SHORTCUT_NAME%"
    echo  已刪除快捷方式
) else (
    echo  快捷方式不存在,無需刪除
)

if exist "%~dp0launch_hidden.vbs" (
    del "%~dp0launch_hidden.vbs"
    echo  已刪除 launch_hidden.vbs
)

echo.
echo  完成。下次重啟後,採集器不會自動啟動。
echo  你仍可雙擊 start.bat 手動啟動。
echo.
pause
