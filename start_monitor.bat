@echo off
rem Sensor monitor (web version) - one-click launcher for Windows.
rem Double-click this file. To use another port, change PORT below (e.g. 5003).
setlocal
set PORT=5002
cd /d "%~dp0"
chcp 65001 >nul

where uv >nul 2>nul
if errorlevel 1 (
  echo [ERROR] uv not found. Install it in PowerShell:
  echo     irm https://astral.sh/uv/install.ps1 ^| iex
  echo then reopen this window.
  pause
  exit /b 1
)

echo Starting sensor monitor on http://127.0.0.1:%PORT%  (press Ctrl+C to stop and save)
rem Open the default browser after the server has had a few seconds to start.
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:%PORT%"
uv run python web_monitor.py --port %PORT% %*

echo.
echo Server stopped. Data is in the Sensor_Data folder.
pause
