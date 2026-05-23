@echo off
cd /d "%~dp0"
title LSPosed Clean Reinstall

set ADB=..\platform-tools\adb.exe

echo ============================================
echo   LSPosed Clean Reinstall (nuke + rebuild)
echo ============================================
echo.
echo This script will:
echo   1. Uninstall LSPosed module, manager, appsign module
echo   2. Clear LSPosed dalvik cache (broken daemon state)
echo   3. Reboot phone
echo   4. You then run yijianpeizhi-shouji.bat (setup) again
echo.
echo Make sure phone is: USB connected + unlocked.
echo.
pause

echo.
echo === [1/5] Detect phone ===
for /f "tokens=1" %%a in ('%ADB% devices ^| findstr /v "List" ^| findstr /v "sigma" ^| findstr "device"') do set DEV=%%a
if "%DEV%"=="" (
    echo [ERROR] no phone detected! check USB.
    pause
    exit /b 1
)
echo   phone: %DEV%

echo.
echo === [2/5] Request root (tap ALLOW on phone if prompted) ===
%ADB% -s %DEV% shell "su -c 'id'"

echo.
echo === [3/5] Uninstall LSPosed + manager + appsign ===
echo   [a] remove LSPosed Magisk module dir...
%ADB% -s %DEV% shell "su -c 'rm -rf /data/adb/modules/zygisk_lsposed /data/adb/modules_update/zygisk_lsposed'"
echo   [b] remove LSPosed daemon state + config DB...
%ADB% -s %DEV% shell "su -c 'rm -rf /data/adb/lspd'"
echo   [c] uninstall LSPosed manager APK...
%ADB% -s %DEV% shell "pm uninstall org.lsposed.manager"
echo   [d] uninstall appsign module APK...
%ADB% -s %DEV% shell "pm uninstall com.tianya.idlefish7920"

echo.
echo === [4/5] Clear corrupt dex cache ===
%ADB% -s %DEV% shell "su -c 'find /data/dalvik-cache -iname \"*lspd*\" -delete 2>/dev/null'"
%ADB% -s %DEV% shell "su -c 'find /data/dalvik-cache -iname \"*lsposed*\" -delete 2>/dev/null'"
%ADB% -s %DEV% shell "su -c 'find /data/dalvik-cache -iname \"*tianya*\" -delete 2>/dev/null'"
%ADB% -s %DEV% shell "su -c 'find /data/dalvik-cache -iname \"*idlefish7920*\" -delete 2>/dev/null'"

echo.
echo === [5/5] Reboot phone ===
%ADB% -s %DEV% reboot
echo   reboot sent. phone boots in ~60-90 seconds.
echo.
echo ============================================
echo   DONE!
echo ============================================
echo.
echo NEXT STEP (after phone fully boots):
echo   double-click: yijianpeizhi-shouji.bat
echo   (the Chinese name: yi-jian-pei-zhi-shou-ji.bat)
echo.
pause
