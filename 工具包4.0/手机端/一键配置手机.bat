@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Phone Setup
python "%~dp0setup_phone.py"
pause
