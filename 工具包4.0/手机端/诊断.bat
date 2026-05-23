@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Phone Diagnosis

set ADB=..\platform-tools\adb.exe
set DEV=7e1f0b5

echo ============================================
echo   手机端问题诊断
echo ============================================
echo.

echo === [A] LSPosed 守护进程 ===
%ADB% -s %DEV% shell "su -c 'ps -A | grep -E \"lspd|daemon\" | grep -v grep'"
echo.

echo === [B] LSPosed 模块目录 ===
%ADB% -s %DEV% shell "su -c 'ls /data/adb/modules/'"
echo.

echo === [C] LSPosed DB 现状 ===
%ADB% -s %DEV% shell "su -c 'ls -la /data/adb/lspd/config/'"
echo.

echo === [D] 启用的模块 ===
%ADB% -s %DEV% shell "su -c 'sqlite3 /data/adb/lspd/config/modules_config.db \"SELECT mid, module_pkg_name, enabled FROM modules;\"'"
echo.

echo === [E] 模块 scope (必须看到 com.taobao.idlefish) ===
%ADB% -s %DEV% shell "su -c 'sqlite3 /data/adb/lspd/config/modules_config.db \"SELECT * FROM scope;\"'"
echo.

echo === [F] appsign 模块包是否装上 ===
%ADB% -s %DEV% shell "pm path com.tianya.idlefish7920"
echo.

echo === [G] 闲鱼 APP 是否在跑 ===
%ADB% -s %DEV% shell "su -c 'ps -A | grep idlefish | grep -v grep'"
echo.

echo === [H] 10102 端口谁在监听 ===
%ADB% -s %DEV% shell "su -c 'netstat -tlnp 2>/dev/null | grep 10102'"
%ADB% -s %DEV% shell "su -c 'ss -tlnp 2>/dev/null | grep 10102'"
echo.

echo === [I] LSPosed 最近加载日志 (关键) ===
%ADB% -s %DEV% shell "su -c 'logcat -d -t 200 | grep -iE \"LSPosed|idlefish7920|appsign\" | tail -30'"
echo.

echo === [J] 闲鱼进程被 hook 日志 ===
%ADB% -s %DEV% shell "su -c 'logcat -d -t 500 | grep -E \"com.taobao.idlefish.*LSPosed|Loaded.*idlefish7920\" | tail -20'"
echo.

echo ============================================
echo  诊断完成! 把上面全部输出复制给我
echo ============================================
pause
