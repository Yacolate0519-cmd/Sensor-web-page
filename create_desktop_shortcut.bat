@echo off
rem Creates a "Sensor Monitor" shortcut on the current user's Desktop that runs start_monitor.bat.
rem Keep this file CRLF (see .gitattributes).
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "SM_ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=$env:SM_ROOT.TrimEnd('\'); $desk=[Environment]::GetFolderPath('Desktop'); $lnk=Join-Path $desk 'Sensor Monitor.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($lnk); $s.TargetPath=Join-Path $root 'start_monitor.bat'; $s.WorkingDirectory=$root; $s.IconLocation=(Join-Path $root 'launchers\windows\sensor_monitor.ico')+',0'; $s.Description='Sensor Monitor'; $s.Save(); Write-Host ('Created: '+$lnk)"
if errorlevel 1 (
  echo.
  echo [錯誤] 建立桌面捷徑失敗。可以手動在 start_monitor.bat 按右鍵，選「傳送到」再選「桌面（建立捷徑）」。
  pause
  exit /b 1
)
echo.
echo 已在桌面建立「Sensor Monitor」捷徑，之後點桌面圖示即可啟動。
pause
