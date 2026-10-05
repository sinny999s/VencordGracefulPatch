@echo off
title Vencord Graceful Patcher - Stop
echo Stopping the background patcher (it will start again at next login,
echo run uninstall.bat to remove it permanently)...
python "%~dp0patcher.py" --stop
echo.
pause
