@echo off
setlocal

title Vencord Graceful Patcher - Installer
color 0B

echo =======================================================
echo          Vencord Graceful Patcher Installer
echo =======================================================
echo.

set "CURRENT_DIR=%~dp0"
set "SCRIPT_PATH=%CURRENT_DIR%patcher.py"
set "STARTUP_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VencordGracefulPatch.vbs"

:: 1. Verify that a real Python 3.8+ is installed (the Microsoft Store stub
::    "python.exe" passes a plain `where python` check but cannot run anything).
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 (
    color 0C
    echo [ERROR] Python 3.8 or newer was not found on PATH.
    echo.
    echo   1. Install Python from https://www.python.org/downloads/
    echo   2. Tick "Add python.exe to PATH" in the installer.
    echo   3. If you only have the Microsoft Store version, open
    echo      Settings ^> Apps ^> Advanced app settings ^> App execution aliases
    echo      and turn OFF the "python.exe" / "python3.exe" aliases.
    echo.
    pause
    exit /b 1
)

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
    color 0C
    echo [ERROR] pythonw.exe was not found next to python.exe.
    echo         Re-run the Python installer and make sure PATH is enabled.
    pause
    exit /b 1
)
"%PYW%" -c "raise SystemExit(0)" >nul 2>&1
if errorlevel 1 (
    color 0C
    echo [ERROR] "%PYW%" cannot run scripts. Please reinstall Python from python.org.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo [OK] Python %PYVER% detected.
echo      Background interpreter: "%PYW%"

:: 2. Stop any old or aggressive patchers
echo [*] Checking for conflicting background patchers...
taskkill /F /IM autovencordpatch.exe >nul 2>&1
taskkill /F /IM vencordinstaller.exe >nul 2>&1
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\autovencordpatch.exe" (
    del /f /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\autovencordpatch.exe"
    echo [OK] Removed old autovencordpatch.exe from Startup.
)

:: 3. Stop any existing instance of this patcher (works on Windows 11 24H2+ where wmic is gone)
python "%SCRIPT_PATH%" --stop

:: 4. Create a VBS launcher in Startup for 100%% invisible background execution
echo [*] Registering in Windows Startup...
(
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo cmd = Chr^(34^) ^& "%PYW%" ^& Chr^(34^) ^& " " ^& Chr^(34^) ^& "%SCRIPT_PATH%" ^& Chr^(34^) ^& " --daemon"
    echo WshShell.Run cmd, 0, False
) > "%STARTUP_VBS%"
if not exist "%STARTUP_VBS%" (
    color 0C
    echo [ERROR] Could not write the startup launcher to:
    echo         "%STARTUP_VBS%"
    pause
    exit /b 1
)
echo [OK] Added startup launcher to:
echo      "%STARTUP_VBS%"

:: 5. Launch the daemon now in the background
echo [*] Starting background patcher daemon...
wscript.exe "%STARTUP_VBS%"

echo.
color 0A
echo =======================================================
echo  [SUCCESS] Vencord Graceful Patcher is now running!
echo =======================================================
echo.
echo  - It runs silently in the background and starts with Windows.
echo  - It will NEVER force-close Discord during calls.
echo  - Updates are applied the moment you close Discord.
echo  - Edit config.json any time; changes are picked up automatically.
echo.
echo Press any key to view current status...
pause >nul

cls
python "%SCRIPT_PATH%" --status
pause
