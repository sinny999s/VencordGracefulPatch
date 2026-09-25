@echo off
title Discord Voice Stereo Status
cd /d "%~dp0"
python voice_patcher.py --status
echo.
pause
