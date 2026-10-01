@echo off
chcp 65001 >nul
title Rocket Hunter
set PYTHONUTF8=1
cd /d "%~dp0"

if "%BYBIT_ROCKET_KEY%"=="" (
    echo ERROR: variable BYBIT_ROCKET_KEY is not set. Set your Bybit keys first.
    pause
    exit /b 1
)
if "%BYBIT_ROCKET_SECRET%"=="" (
    echo ERROR: variable BYBIT_ROCKET_SECRET is not set. Set your Bybit keys first.
    pause
    exit /b 1
)

:loop
echo.
echo [%date% %time%] Starting Rocket Hunter...
python main.py
echo.
echo [%date% %time%] Bot stopped (exit code %errorlevel%).
echo Restart in 10 seconds. To stop completely: press Ctrl+C, then Y.
timeout /t 10
goto loop
