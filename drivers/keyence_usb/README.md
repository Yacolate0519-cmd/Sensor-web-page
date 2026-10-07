# KEYENCE LK-G5000 USB 驅動（Win10/11 x64）

來源：LK-Navigator 2 Ver.1.04.00（2016-12-20，官方標示支援 Windows 10）安裝包內 `Installer/Driver/Windows7`。
INF：`KEYENCE_LK-G5000.inf`，DriverVer 04/10/2014,1.7.0004.0，裝置 `USB\VID_0720&PID_0013`。

## 安裝（需系統管理員權限）
1. LK-G5000 控制器先**不要**接 USB。
2. 以系統管理員身分執行 `DPInst_64.exe`（32 位元 Windows 用 `DPInst_86.exe`）。
3. 接上 USB，裝置管理員應出現「WDF USB for KEYENCE LK-G5000」。
   若裝置顯示為未知裝置：右鍵 → 更新驅動程式 → 瀏覽 → 指向本資料夾。

`drivers/` 上層的 LKIF2.dll / CmnLib.dll / KeyUsbDrv.dll 是使用者層通訊 DLL（x64），與此核心驅動是兩回事，兩者都要。
