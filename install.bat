@echo off
setlocal enabledelayedexpansion

title Vencord Graceful Patcher - Installer
color 0B

echo =======================================================
echo          Vencord Graceful Patcher Installer
echo =======================================================
echo.

:: 1. Check for Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    color 0C
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3 from https://www.python.org/
    pause
    exit /b 1
)

where pythonw >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    color 0C
    echo [ERROR] pythonw.exe was not found. Please ensure Python is properly installed.
    pause
    exit /b 1
)

echo [OK] Python detected.

:: 2. Stop any old or aggressive patchers
echo [*] Checking for conflicting background patchers...
taskkill /F /IM autovencordpatch.exe >nul 2>&1
taskkill /F /IM vencordinstaller.exe >nul 2>&1

:: Remove old autovencordpatch from Startup if present
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\autovencordpatch.exe" (
    del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\autovencordpatch.exe"
    echo [OK] Removed old autovencordpatch.exe from Startup.
)

:: 3. Stop any existing instance of VencordGracefulPatch
wmic process where "commandline like '%%VencordGracefulPatch%%patcher.py%%'" call terminate >nul 2>&1

:: 4. Create VBS launcher in Startup for 100%% invisible background execution
set "CURRENT_DIR=%~dp0"
set "SCRIPT_PATH=%CURRENT_DIR%patcher.py"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VencordGracefulPatch.vbs"

echo [*] Registering in Windows Startup...
(
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo WshShell.Run "pythonw.exe """ ^& "%SCRIPT_PATH%" ^& """ --daemon", 0, False
) > "%STARTUP_VBS%"

echo [OK] Added startup launcher to:
echo      "%STARTUP_VBS%"

:: 5. Launch the daemon now in background
echo [*] Starting background patcher daemon...
wscript.exe "%STARTUP_VBS%"

echo.
color 0A
echo =======================================================
echo  [SUCCESS] Vencord Graceful Patcher is now running!
echo =======================================================
echo.
echo  - It will run silently in the background on startup.
echo  - It will NEVER force-close Discord during calls.
echo  - Updates will apply smoothly whenever Discord is closed.
echo.
echo Press any key to view current status...
pause >nul

cls
python "%SCRIPT_PATH%" --status
pause
