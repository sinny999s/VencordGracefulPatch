@echo off
title Revert Discord Stereo to Stock Mono
cd /d "%~dp0"
echo Reverting Discord Voice Module to stock mono...
python voice_patcher.py --unpatch
echo.
pause
