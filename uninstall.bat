@echo off
setlocal

title Vencord Graceful Patcher - Uninstaller
color 0E

echo =======================================================
echo         Vencord Graceful Patcher Uninstaller
echo =======================================================
echo.

set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VencordGracefulPatch.vbs"

echo [*] Stopping running background processes...
wmic process where "commandline like '%%VencordGracefulPatch%%patcher.py%%'" call terminate >nul 2>&1

echo [*] Removing from Windows Startup...
if exist "%STARTUP_VBS%" (
    del /f /q "%STARTUP_VBS%"
    echo [OK] Removed startup launcher.
) else (
    echo [i] Startup launcher was not found.
)

echo.
color 0A
echo =======================================================
echo  [SUCCESS] Vencord Graceful Patcher has been removed.
echo =======================================================
echo.
pause
