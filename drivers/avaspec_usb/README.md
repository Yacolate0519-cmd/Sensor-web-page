# AvaSpec 光譜儀 USB 驅動（WinUSB，Win10/11 x64）

來源：AvaSpecX64-DLL 9.14.0.0 安裝包內 `AVS/`。
INF：`AvaSpec_winusb.inf`，DriverVer 04/14/2015,2.1.0.1，裝置 `USB\VID_1992&PID_0667~0670`、`USB\VID_0471&PID_0667`。

## 安裝（需系統管理員權限，每台電腦一次）
1. 光譜儀先**不要**接 USB。
2. 以系統管理員身分執行 `dpinst.exe`。
3. 接上 USB，裝置管理員應出現 `AvantesSpectrometers` 類別下的裝置。

另外 `drivers/avaspecx64.dll` 需要 Microsoft Visual C++ 2015–2022 x64 執行階段；
若載入時報缺 `VCRUNTIME140.dll`／`MSVCP140.dll`，安裝 `vc_redist.x64.exe`
（廠商安裝包附的那份，或 https://aka.ms/vs/17/release/vc_redist.x64.exe ）。
