@echo off
setlocal
title Vencord Graceful Patcher - Unpatch
color 0E

echo =======================================================
echo          Restore Discord to Stock (Unpatch)
echo =======================================================
echo.
echo Make sure Discord is completely closed before proceeding.
echo.
pause

python "%~dp0patcher.py" --unpatch
echo.
pause
