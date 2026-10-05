@echo off
setlocal

title Vencord Graceful Patcher - Uninstaller
color 0E

echo =======================================================
echo         Vencord Graceful Patcher Uninstaller
echo =======================================================
echo.

set "SCRIPT_PATH=%~dp0patcher.py"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VencordGracefulPatch.vbs"

echo [*] Stopping the background patcher...
python "%SCRIPT_PATH%" --stop

echo [*] Removing from Windows Startup...
if exist "%STARTUP_VBS%" (
    del /f /q "%STARTUP_VBS%"
    echo [OK] Removed startup launcher.
) else (
    echo [i] Startup launcher was not found.
)

echo.
choice /C YN /N /M "Also restore Discord to stock (remove Vencord patch and stereo audio)? [Y/N] "
if errorlevel 2 goto :done
if not errorlevel 1 goto :done
echo.
echo Make sure Discord is completely closed.
python "%SCRIPT_PATH%" --unpatch

:done
echo.
color 0A
echo =======================================================
echo  [SUCCESS] Vencord Graceful Patcher has been removed.
echo =======================================================
echo.
pause
