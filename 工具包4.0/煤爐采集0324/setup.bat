@echo off
title 煤爐采集器 - 一鍵安裝
cd /d "%~dp0"

echo.
echo  ============================
echo    煤爐采集器 一鍵安裝
echo  ============================
echo.
echo  此腳本會幫你做四件事:
echo  1. 檢查 Node.js 是否安裝 ^(否則開啟下載頁^)
echo  2. 安裝依賴 ^(node_modules^)
echo  3. 詢問是否設定開機自啟動
echo  4. 顯示如何安裝油猴腳本
echo.
pause

echo.
echo === 1. 檢查 Node.js ===
where node >nul 2>&1
if errorlevel 1 (
    echo  未檢測到 Node.js,請從以下網址下載安裝後重新運行:
    echo  https://nodejs.org/zh-tw/download
    echo  建議版本:Node 18 LTS 或 20 LTS
    start https://nodejs.org/zh-tw/download
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node -v') do echo  Node 版本: %%v

echo.
echo === 2. 安裝依賴 ===
if exist node_modules (
    echo  已安裝過,跳過
) else (
    echo  正在執行 npm install ^(預計 1-2 分鐘^)...
    call npm install
    if errorlevel 1 (
        echo  [錯誤] 安裝失敗,請檢查網絡
        pause
        exit /b 1
    )
)

echo.
echo === 3. 開機自啟動設置 ===
choice /M "要設定開機自動在背景啟動採集器嗎"
if not errorlevel 2 (
    call autostart_install.bat
)

echo.
echo === 4. 油猴腳本(可選) ===
echo  油猴腳本能讓你在 Mercari 賣家頁直接看到:
echo    ? 是否已被隊員採過
echo    ? 點按鈕一鍵發送到本機採集器
echo.
echo  安裝步驟:
echo  1. 在 Chrome / Edge 安裝 Tampermonkey 擴展:
echo     https://www.tampermonkey.net/
echo  2. 訪問: http://<RELAY_IP_REDACTED>:3031/tampermonkey.user.js
echo     Tampermonkey 會自動彈出安裝對話框
echo.
choice /M "現在打開 Tampermonkey 下載頁嗎"
if not errorlevel 2 (
    start https://www.tampermonkey.net/
    timeout /t 3 /nobreak >nul
    start http://<RELAY_IP_REDACTED>:3031/tampermonkey.user.js
)

echo.
echo  ============================
echo    安裝完成
echo  ============================
echo.
echo  下次直接雙擊 start.bat 或重啟電腦 ^(如已設開機自啟動^)
echo  訪問 http://localhost:3030/ 開始採集
echo  訪問 http://<RELAY_IP_REDACTED>:3031/ 查看團隊看板
echo.
choice /M "現在立即啟動採集器嗎"
if not errorlevel 2 (
    netstat -an 2>nul | findstr ":3030 " | findstr "LISTENING" >nul
    if not errorlevel 1 (
        echo  採集器已經在運行 ^(可能是剛才設好的開機自啟動^),直接打開瀏覽器...
        start http://localhost:3030/
    ) else (
        start "" cmd /c start.bat
    )
)
pause
