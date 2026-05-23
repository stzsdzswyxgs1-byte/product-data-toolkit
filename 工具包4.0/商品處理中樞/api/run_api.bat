@echo off
REM 商品處理中樞 API server (port 7778) launcher
REM cd 到 hub 根目錄(api/ 的父目錄),確保 Pipeline 找到相對路徑的 xlsx 規則
cd /d "%~dp0\.."
title 商品處理中樞 API (7778)
python -m api.server
pause
