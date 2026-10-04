@echo off
rem Creates a "Sensor Monitor" shortcut on the Desktop that runs start_monitor.bat.
rem Keep this file ASCII-only and CRLF; logic and messages live in launchers\windows\create_shortcut.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launchers\windows\create_shortcut.ps1"
