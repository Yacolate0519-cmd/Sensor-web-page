@echo off
rem Sensor Monitor - one-click launcher for Windows. Double-click, or use the desktop shortcut
rem made by create_desktop_shortcut.bat.
rem NOTES: keep this file CRLF (see .gitattributes). Do NOT use goto/labels or put %PATH% inside
rem ( ) blocks: this file contains UTF-8 text, and PATH may contain parentheses.
rem Env: SENSOR_MONITOR_PORT (default 5002). Extra arguments go to web_monitor.py.
chcp 65001 >nul
setlocal EnableExtensions
title Sensor Monitor
cd /d "%~dp0" || (
  echo 找不到專案資料夾，請不要把 start_monitor.bat 單獨搬走（可以改用捷徑）。
  pause
  exit /b 1
)
rem uv's default install folder; also covers the case right after a fresh install.
set "PATH=%PATH%;%USERPROFILE%\.local\bin"

echo === 感測器監測 Sensor Monitor ===

where uv >nul 2>nul
if errorlevel 1 (
  echo.
  echo 找不到 uv，本程式用它來管理 Python 與套件。
  echo 可以自動安裝：從 astral.sh 下載，需要網路，不需要系統管理員權限。
  choice /c YN /n /m "是否現在自動安裝 uv？按 Y 安裝，按 N 取消： "
  if errorlevel 2 (
    echo.
    echo 已取消。請手動安裝 uv：開啟 PowerShell，貼上下面這行並按 Enter，
    echo 完成後重新開啟本程式：
    echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo.
    pause
    exit /b 0
  )
  powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  where uv >nul 2>nul
  if errorlevel 1 (
    echo.
    echo uv 安裝失敗。請檢查網路，或手動安裝：
    echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo 安裝完成後，重新開啟本程式。
    pause
    exit /b 1
  )
)

echo.
echo 同步套件 uv sync：第一次執行需要網路，可能要幾分鐘…
uv sync
if errorlevel 1 (
  echo.
  echo [錯誤] 套件同步失敗。請檢查：
  echo   1. 網路是否連線，第一次執行必須上網下載 Python 與套件。
  echo   2. 若訊息提到 pyaudio：請安裝 Microsoft C++ Build Tools 後重試，或聯絡負責人。
  echo   3. 仍失敗請把上面的訊息截圖給負責人。
  pause
  exit /b 1
)

uv run python launchers\windows\launch_monitor.py %*
echo.
echo 監測服務已停止。資料在 Sensor_Data 資料夾。
pause
