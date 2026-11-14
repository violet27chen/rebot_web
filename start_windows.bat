@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Robot Arm Platform - Windows Launcher

echo ============================================================
echo  Robot Arm Control Platform / Windows launcher
echo  Usage: start_windows.bat [--lan] [--port 8000] [--no-browser]
echo ============================================================

set "PY=python"
where python >nul 2>nul
if %errorlevel%==0 goto RUN
set "PY=py -3"
where py >nul 2>nul
if %errorlevel%==0 goto RUN

echo [ERROR] Python 3.10+ not found in PATH.
echo         Install Python and check "Add python.exe to PATH", then retry.
pause
exit /b 1

:RUN
echo [start] Python command: %PY%
echo [start] Starting web service, press Ctrl+C to stop.
echo.
%PY% start.py %*
if errorlevel 1 (
  echo.
  echo [ERROR] The server exited with an error. See the messages above.
  pause
)
