@echo off
cd /d "%~dp0"
title Push VencordGracefulPatch to GitHub
color 0B

echo =======================================================
echo          Pushing VencordGracefulPatch to GitHub
echo =======================================================
echo.
echo Target Remote:
git remote -v
echo.
echo Pushing branch 'main' to origin...
git push -u origin main

echo.
if %ERRORLEVEL% EQU 0 (
    color 0A
    echo [SUCCESS] Successfully pushed to GitHub!
) else (
    color 0C
    echo [ERROR] Push failed. Make sure the repository exists on GitHub
    echo and that you are logged into your account.
)
echo.
pause
