# 在目前使用者的桌面建立「Sensor Monitor」捷徑，指向 start_monitor.bat（由 create_desktop_shortcut.bat 呼叫）。
# 本檔須存成 UTF-8 with BOM，Windows PowerShell 5.1 才會正確讀取中文。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
try {
    $desk = [Environment]::GetFolderPath('Desktop')
    $lnk = Join-Path $desk 'Sensor Monitor.lnk'
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = Join-Path $Root 'start_monitor.bat'
    $s.WorkingDirectory = $Root
    $s.IconLocation = (Join-Path $Root 'launchers\windows\sensor_monitor.ico') + ',0'
    $s.Description = 'Sensor Monitor'
    $s.Save()
    Write-Host ''
    Write-Host "已在桌面建立「Sensor Monitor」捷徑：$lnk"
    Write-Host '之後點桌面圖示即可啟動。'
} catch {
    Write-Host ''
    Write-Host "[錯誤] 建立桌面捷徑失敗：$($_.Exception.Message)"
    Write-Host '可以手動在 start_monitor.bat 按右鍵，選「傳送到」再選「桌面（建立捷徑）」。'
}
Write-Host ''
Read-Host '按 Enter 關閉視窗' | Out-Null
