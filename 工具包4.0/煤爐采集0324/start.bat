@echo off
title 煤爐采集器
cd /d "%~dp0"

REM ---- Parse flag ----
set SILENT=0
if "%~1" == "/silent" set SILENT=1
if "%~1" == "--silent" set SILENT=1

REM ---- Check if already running on port 3030 ----
netstat -an 2>nul | findstr ":3030 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    if "%SILENT%" == "0" (
        echo.
        echo  [!] 採集器似乎已經在 3030 端口運行中
        echo      可能是開機自啟動的後台進程
        echo.
        echo  你想要:
        echo    A^) 開瀏覽器到現有的採集器頁面 ^(推薦^)
        echo    B^) 強制停止現有的,重新啟動
        echo    C^) 取消
        echo.
        choice /C ABC /N /M "選擇 [A/B/C]: "
        if errorlevel 3 exit /b 0
        if errorlevel 2 goto kill_existing
        if errorlevel 1 (
            start http://localhost:3030/
            exit /b 0
        )
    ) else (
        REM Silent mode: just exit, don't try to take over
        exit /b 0
    )
)
goto skip_kill

:kill_existing
echo  停止現有進程...
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":3030 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul

:skip_kill

REM ---- Cloud update check (silent, requires Python; skip if no Python) ----
where python >nul 2>nul
if not errorlevel 1 (
    if exist "..\_updater\updater.py" (
        python "..\_updater\updater.py" --app mercari_scraper 2>nul
    )
)

REM ---- Node version check ----
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [錯誤] 未找到 Node.js,請先安裝 Node 18~24:
    echo  https://nodejs.org/
    if "%SILENT%" == "0" pause
    exit /b 1
)

for /f "tokens=1 delims=v." %%a in ('node -p "process.versions.node"') do set NODE_MAJOR=%%a
if "%NODE_MAJOR%" lss "18" (
    echo  [錯誤] Node 版本太舊 ^(目前 v%NODE_MAJOR%^),需要 Node 18~24
    if "%SILENT%" == "0" pause
    exit /b 1
)
if "%NODE_MAJOR%" gtr "24" (
    echo  [警告] Node 版本太新 ^(目前 v%NODE_MAJOR%^),建議 Node 18~24
    if "%SILENT%" == "0" timeout /t 3 /nobreak >nul
)

REM ---- First-time dependency install ----
if not exist node_modules (
    echo  首次啟動,正在安裝依賴 ^(可能需要幾分鐘^)...
    call npm install
    if %errorlevel% neq 0 (
        echo  [錯誤] 依賴安裝失敗
        if "%SILENT%" == "0" pause
        exit /b 1
    )
)

if "%SILENT%" == "0" (
    echo.
    echo  ============================
    echo    煤爐采集器 正在啟動...
    echo  ============================
    echo.
    echo  瀏覽器將自動打開,如未打開請訪問:
    echo  http://localhost:3030/
    echo.
    echo  此窗口關閉即停止服務
    echo  服務崩潰會自動重啟 ^(間隔 5 秒,最多 5 次^)
    echo  ============================
    echo.
    timeout /t 2 /nobreak >nul 2>&1
    start http://localhost:3030/
)

REM ---- Auto-restart loop with port conflict detection ----
set RESTART_COUNT=0

:restart_loop
REM Pre-check: if port already held by another process, abort cleanly
netstat -an 2>nul | findstr ":3030 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo  ============================
    echo  [錯誤] 端口 3030 已被另一個進程佔用,無法啟動
    echo  ============================
    echo.
    echo  可能原因:
    echo    1. 你重複執行了 start.bat ^(本實例放棄,先有的保留^)
    echo    2. 開機自啟動的後台進程還在跑
    echo    3. 其他程式佔用了 3030 端口
    echo.
    echo  解決方法:雙擊 stop.bat 後重試,或直接訪問 http://localhost:3030/
    echo.
    if "%SILENT%" == "0" pause
    exit /b 2
)

node server.js
set EXIT_CODE=%errorlevel%

if "%EXIT_CODE%" == "0" (
    if "%SILENT%" == "0" (
        echo  正常退出,不再重啟
        pause
    )
    exit /b 0
)

REM Limit restart count to avoid infinite loops in pathological cases
set /a RESTART_COUNT=%RESTART_COUNT%+1
if %RESTART_COUNT% GEQ 5 (
    echo.
    echo  ============================
    echo  [錯誤] 已重啟 %RESTART_COUNT% 次仍失敗,放棄
    echo  ============================
    echo  請檢查上方錯誤信息,或執行 stop.bat 後重試
    if "%SILENT%" == "0" pause
    exit /b 3
)

if "%SILENT%" == "0" (
    echo.
    echo  ============================
    echo  服務器於 %TIME% 異常退出 ^(code=%EXIT_CODE%, 第 %RESTART_COUNT%/5 次重啟^)
    echo  5 秒後自動重啟... 按 Ctrl+C 取消
    echo  ============================
)
timeout /t 5 /nobreak >nul
goto restart_loop
