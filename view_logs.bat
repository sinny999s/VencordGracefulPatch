@echo off
set "LOG_PATH=%LOCALAPPDATA%\VencordGracefulPatch\patcher.log"
if exist "%LOG_PATH%" (
    start notepad.exe "%LOG_PATH%"
) else (
    echo Log file not created yet: %LOG_PATH%
    pause
)
