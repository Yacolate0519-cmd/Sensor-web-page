@echo off
rem Sensor Monitor - one-click launcher for Windows (double-click, or use the desktop shortcut).
rem Keep this file ASCII-only and CRLF: cmd mis-parses UTF-8 text inside ( ) blocks.
rem All logic and Chinese messages live in launchers\windows\start_monitor.ps1.
rem Env: SENSOR_MONITOR_PORT (default 5002). Extra arguments go to main.py.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launchers\windows\start_monitor.ps1" %*
