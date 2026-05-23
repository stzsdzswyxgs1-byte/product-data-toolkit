@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Phone Diagnostic
python _diagnose.py
pause
