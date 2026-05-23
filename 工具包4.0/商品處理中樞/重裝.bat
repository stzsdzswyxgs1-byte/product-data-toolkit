@echo off
REM ============================================
REM   重裝 — 清掉 .venv + 拉雲端最新 + 重跑 setup
REM   什麼時候用:
REM     - 換顯卡 (例如剛買 RTX 50xx 換上)
REM     - 套件衝突 / torch 載入失敗
REM     - admin 推了重大環境更新 (例如 cu124 → cu128 路線)
REM ============================================
chcp 65001 >nul
cd /d "%~dp0"
title 重裝 商品處理中樞

echo.
echo =========================================================
echo   重裝商品處理中樞 — 會清 .venv + 拉雲端最新 + 重新裝套件
echo   全程約 10-30 分鐘 (看網速)
echo =========================================================
echo.
echo 步驟:
echo   1. 拉雲端最新 source (updater.py)
echo   2. 刪掉舊 .venv
echo   3. 重跑 setup.bat 建新環境
echo.
choice /c YN /m "確認重裝嗎?"
if errorlevel 2 (
    echo 取消, 沒動任何東西.
    pause
    exit /b 0
)

echo.
echo [1/3] 拉雲端最新 source ...
where python >nul 2>nul
if errorlevel 1 (
    echo   - system Python 找不到, 跳過 updater
    echo   - 將用本地當前 source 跑 setup ^(可能不是最新版^)
) else (
    if exist "..\_updater\updater.py" (
        python "..\_updater\updater.py" --app product_hub
    ) else (
        echo   - updater.py 不存在, 跳過
    )
)

echo.
echo [2/3] 清掉舊 .venv ...
if exist ".venv" (
    rmdir /s /q ".venv"
    if exist ".venv" (
        echo   ! 刪 .venv 失敗, 是不是還有軟件開著? 關掉再試
        pause
        exit /b 1
    )
    echo   ✓ 已刪
) else (
    echo   ✓ 沒舊 .venv
)

echo.
echo [3/3] 重跑 setup.bat ...
echo.
call "%~dp0setup.bat"

echo.
echo =========================================================
echo   重裝完成 ✓
echo   雙擊 [start.bat] 啟動軟件
echo =========================================================
pause
