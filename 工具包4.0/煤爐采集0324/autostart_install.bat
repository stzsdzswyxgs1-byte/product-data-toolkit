@echo off
title 煤爐采集器 - 開機自啟動設置
cd /d "%~dp0"

set SCRIPT_DIR=%~dp0
set STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_NAME=煤爐采集器.lnk
set VBS_PATH=%SCRIPT_DIR%launch_hidden.vbs

echo.
echo  ============================
echo    煤爐采集器 - 開機自啟動
echo  ============================
echo.
echo  此腳本會做兩件事:
echo  1. 創建 launch_hidden.vbs (背景啟動,不顯示黑窗口)
echo  2. 在 Windows 啟動文件夾添加快捷方式
echo.
echo  程式目錄: %SCRIPT_DIR%
echo  啟動目錄: %STARTUP_DIR%
echo.
choice /M "確定安裝開機自啟動嗎"
if errorlevel 2 (
    echo 已取消
    pause
    exit /b 0
)

echo.
echo === 1. 創建 launch_hidden.vbs ===
(
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo WshShell.CurrentDirectory = "%SCRIPT_DIR%"
    echo WshShell.Run """%SCRIPT_DIR%start.bat"" --silent", 0, False
) > "%VBS_PATH%"
if not exist "%VBS_PATH%" (
    echo  [錯誤] 創建 vbs 失敗
    pause
    exit /b 1
)
echo  已創建: %VBS_PATH%

echo.
echo === 2. 添加開機啟動快捷方式 ===
powershell -NoProfile -Command "$s = New-Object -ComObject WScript.Shell; $lnk = $s.CreateShortcut('%STARTUP_DIR%\%SHORTCUT_NAME%'); $lnk.TargetPath = '%VBS_PATH%'; $lnk.WorkingDirectory = '%SCRIPT_DIR%'; $lnk.Description = '煤爐采集器後台啟動'; $lnk.IconLocation = '%SystemRoot%\System32\shell32.dll,16'; $lnk.Save()"
if errorlevel 1 (
    echo  [錯誤] 創建快捷方式失敗
    pause
    exit /b 1
)
if not exist "%STARTUP_DIR%\%SHORTCUT_NAME%" (
    echo  [錯誤] 快捷方式不存在於啟動目錄
    pause
    exit /b 1
)
echo  已創建: %STARTUP_DIR%\%SHORTCUT_NAME%

echo.
echo  ============================
echo    安裝完成
echo  ============================
echo.
echo  下次 Windows 啟動時,採集器會自動在背景運行
echo  訪問: http://localhost:3030/ 查看
echo.
echo  要禁用:刪除 %STARTUP_DIR%\%SHORTCUT_NAME%
echo  或運行 autostart_remove.bat
echo.
choice /M "現在立即在背景啟動採集器嗎"
if not errorlevel 2 (
    echo 已啟動,訪問 http://localhost:3030/
    start "" wscript "%VBS_PATH%"
    timeout /t 3 /nobreak >nul
    start http://localhost:3030/
)
echo.
pause
