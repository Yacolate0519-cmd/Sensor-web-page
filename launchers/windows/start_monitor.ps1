# Sensor Monitor - Windows 啟動流程（由 start_monitor.bat 呼叫）。
# 中文訊息放在這裡而不是 .bat：cmd 在 chcp 65001 下讀取含 UTF-8 的 ( ) 區塊會切錯位元組，
# 把訊息後半段當成指令執行。本檔須存成 UTF-8 with BOM，Windows PowerShell 5.1 才會正確讀取中文。
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $MonitorArgs)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = 'Sensor Monitor'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Root
# uv 的預設安裝位置；也涵蓋剛安裝完、PATH 尚未更新的情況
$env:Path = "$env:Path;$env:USERPROFILE\.local\bin"
$InstallCmd = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'

function Stop-WithPause([int] $Code) {
    Write-Host ''
    Read-Host '按 Enter 關閉視窗' | Out-Null
    exit $Code
}

Write-Host '=== 感測器監測 Sensor Monitor ==='

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ''
    Write-Host '找不到 uv，本程式用它來管理 Python 與套件。'
    Write-Host '可以自動安裝：從 astral.sh 下載，需要網路，不需要系統管理員權限。'
    $answer = Read-Host '是否現在自動安裝 uv？輸入 Y 安裝，其他鍵取消'
    if ($answer -notmatch '^[Yy]') {
        Write-Host ''
        Write-Host '已取消。請手動安裝 uv：開啟 PowerShell，貼上下面這行並按 Enter，完成後重新開啟本程式：'
        Write-Host "    $InstallCmd"
        Stop-WithPause 0
    }
    powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host ''
        Write-Host 'uv 安裝失敗。請檢查網路，或手動安裝：'
        Write-Host "    $InstallCmd"
        Write-Host '安裝完成後，重新開啟本程式。'
        Stop-WithPause 1
    }
}

Write-Host ''
Write-Host '同步套件 uv sync（含光譜儀用的 PyQt5）：第一次執行需要網路，可能要幾分鐘…'
uv sync --extra spectrometer
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '[錯誤] 套件同步失敗。請檢查：'
    Write-Host '  1. 網路是否連線，第一次執行必須上網下載 Python 與套件。'
    Write-Host '  2. 若訊息提到某個套件沒有 Windows 版本（wheel），請把上面的訊息截圖給負責人。'
    Write-Host '  3. 其他錯誤也請把上面的訊息截圖給負責人。'
    Stop-WithPause 1
}

uv run python launchers\windows\launch_monitor.py @MonitorArgs
Write-Host ''
Write-Host '監測服務已停止。資料在 Sensor_Data 資料夾。'
Stop-WithPause 0
