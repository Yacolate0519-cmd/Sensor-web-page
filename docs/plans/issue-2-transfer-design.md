# Issue #2 連線系統設計：收資料電腦 → 實驗室機台

> 狀態：**設計草案，尚未實作**（2026-10-05）。上層規劃見 [issue-2-cloud-sync.md](issue-2-cloud-sync.md)。
> 本文件取代該檔 §1.2「SMB 共用資料夾」的建議，理由見 §1。

## 角色

| 角色 | 機器 | 在這個系統裡做什麼 |
|---|---|---|
| **收資料電腦**（傳送端） | 接感測器、跑監測程式的 Windows 電腦（1 台） | 監測結束後，把實驗資料夾傳到實驗室機台 |
| **實驗室機台**（接收端、封存、網站） | 24 小時開機的 Windows 10 Pro（D: 約 1.86 TB 全空） | 接收並驗證資料、封存到 `D:\SensorArchive`、之後在上面架歷史實驗網站 |

兩台都在同一個 Tailscale 網路內。2026-10-05 實測：實驗室機台在校園 NAT 後面，但外部節點可以打洞直連；連線有時要先走幾秒中繼才轉成直連（見上層規劃 §1.3a）。

---

## 1. 傳輸方式：改用「實驗室機台上的 HTTP 接收服務 ＋ tailscale serve」

| 方式 | 實驗室機台要做的設定 | 優點 | 缺點 |
|---|---|---|---|
| **HTTP 接收服務 ＋ `tailscale serve`**（採用） | 安裝 uv、clone 本 repo、啟動接收服務、執行一次 `tailscale serve` | 接收服務只聽 `127.0.0.1`，由 Tailscale 轉進來，**預期不需要開 Windows 防火牆、不需要建 Windows 帳號**（待實測）；開始前的檢查可以直接問接收服務「在不在、剩多少空間」；續傳、驗證、狀態都在本 repo 控制；以後的網站也在同一套程式裡 | 要寫傳送端和接收端（份量中等） |
| SMB 共用資料夾（原建議） | 建共用資料夾、建專用帳號、只對 Tailscale 開放 SMB-In 防火牆規則，全部需要系統管理員權限 | 傳送端只要複製檔案 | 實驗室機台目前 SMB 規則全關、設定項目多；SMB 在高延遲連線上效率差；密碼要存在收資料電腦上 |
| Taildrop（`tailscale file cp`） | 幾乎不用 | 不用寫傳輸程式 | 檔案落在「下載」資料夾、沒有資料夾結構、沒有回傳確認，不適合當封存 |

`tailscale serve` 的用法已在本機 Tailscale 1.102.4 的 `--help` 確認：`tailscale serve --bg --http <對外埠> <本機埠>` 會把 tailnet 上的 HTTP 請求轉給本機服務，只有 tailnet 內的節點連得到。**在 Windows 上是否需要系統管理員權限、是否真的不必開防火牆，要在實驗室機台實測後才能確定**（§7 驗證項目 V1）。

---

## 2. 元件

```
收資料電腦                                         實驗室機台
┌──────────────────────────────┐                 ┌───────────────────────────────────────┐
│ web_monitor.py（現有）        │                 │ archive_server（新，同一個 repo）       │
│  ├ preflight() ──健康檢查──────┼──┐              │  ├ 127.0.0.1:5011 接收 API（ingest）   │
│  └ _save_all() 完成 → 喚醒     │  │  Tailscale   │  │    ↑ tailscale serve --http 5011    │
│ TransferWorker（新，背景執行緒） ├──┴─ HTTP ──────▶│  │                                    │
│  ├ 掃描 Sensor_Data/EXP_*     │                 │  ├ 127.0.0.1:5010 歷史網站（之後做，只限本機）│
│  ├ 算 sha256、分段上傳、續傳    │                 │  └ D:\SensorArchive\                  │
│  └ 狀態：transfer_<id>.json    │                 │      ├ <來源>\EXP_*\  （正式封存）      │
│ 設定：%APPDATA%\SensorMonitor\ │                 │      └ .incoming\<來源>\EXP_*\ （傳輸中）│
│       transfer.json（repo 外） │                 │ 設定：%APPDATA%\SensorArchive\server.json│
└──────────────────────────────┘                 └───────────────────────────────────────┘
```

- 接收 API 和網站分兩個埠：只有接收 API（5011）透過 `tailscale serve` 開放給 tailnet；網站（5010）維持只限本機，符合「先只在本機看」的決定。埠號為暫定。
- 接收服務在實驗室機台上用「工作排程器：使用者登入時啟動」執行（目前使用者層級的排程不需要系統管理員權限，實作時確認）。

---

## 3. 傳輸協定（HTTP／JSON，版本前綴 `/ingest/v1`）

所有請求帶 `Authorization: Bearer <token>`（§5）。`<source>` 是收資料電腦的名稱、`<exp>` 是實驗資料夾名稱，兩者都只允許 `[A-Za-z0-9_.-]`，伺服器端拒絕其他字元（防止路徑穿越）。

| 方法與路徑 | 用途 | 回應 |
|---|---|---|
| `GET /ingest/v1/health` | 開始前檢查、傳輸前暖身 | `{ok, version, free_bytes, server_time}` |
| `POST /ingest/v1/exp/<source>/<exp>/manifest` | 送出檔案清單 `[{name, size, sha256}]` 與實驗摘要（含 `legacy` 旗標） | 每個檔案的狀態：`missing`／`partial`（附目前位移）／`complete`／`conflict`；整場已封存則回 `archived` 與收據 |
| `PUT /ingest/v1/exp/<source>/<exp>/files/<name>?offset=N` | 上傳一段（建議 8 MB） | 新的已接收大小；`offset` 與伺服器端目前大小不符時回 409 並附正確位移，傳送端從那裡接續 |
| `POST /ingest/v1/exp/<source>/<exp>/files/<name>/complete` | 宣告該檔傳完 | 伺服器重算 sha256，相符回 `complete`，不符就刪掉暫存檔並回 `hash_mismatch`（暫存區屬於接收服務自己的工作檔，不是封存資料） |
| `POST /ingest/v1/exp/<source>/<exp>/commit` | 全部檔案完成後封存 | 伺服器確認每個檔都驗證過，把 `.incoming\<source>\<exp>` 整個改名搬到 `<source>\<exp>`（同一顆磁碟，改名是原子操作），寫入 `_receipt.json`，回傳收據 |

規則：
- **封存區只寫不改**：`<source>\<exp>` 已存在時，manifest 的 hash 全部相同就回 `archived`（冪等）；有任何不同就回 `conflict`，不覆蓋，交給人處理。
- **空間檢查**：manifest 階段若 `free_bytes - 本次總大小 < 保留空間（例如 50 GB）`，回 507，傳送端過 1 小時再試，並在網頁顯示「實驗室機台空間不足」。
- 同一場實驗同時只允許一個上傳（伺服器端以 `<source>/<exp>` 加鎖）。
- 收據 `_receipt.json`：來源、實驗 ID、每個檔的大小與 sha256、收到時間、接收服務版本。網站之後用它判斷封存是否完整。

傳輸量參考：8 小時實驗約 3.7 GB，8 MB 一段約 460 次 PUT。

---

## 4. 傳送端行為（收資料電腦）

### 4.1 開始監測前（`preflight()`）
- 傳輸功能開啟、而且不是模擬模式時，呼叫 `GET /health`：最多 3 次、總時限約 8 秒（容許 Tailscale 先走中繼再轉直連的那幾秒）。
- 失敗 → 放進 `warnings`，訊息分成：找不到主機（Tailscale 沒連上？）、逾時、認證失敗（token 錯）、空間不足。使用者可以按「仍要開始」，資料先存本機，之後自動補傳。
- 結果記進 `experiment_<id>.json` 的 `transfer_preflight` 欄位。

### 4.2 背景傳輸（`TransferWorker`）
醒來時機：`_save_all()` 完成後、程式啟動時、每 10 分鐘、網頁按「立即重試」。

```
若監測正在進行 → 這輪跳過
for 每個 Sensor_Data/EXP_*（由舊到新）：
    simulated 或有 SIMULATED.txt                   → 跳過（不回傳）
    有 experiment json 但 status != "completed"     → 跳過
    沒有 json（舊格式）且是目前錄製中的資料夾        → 跳過；其餘舊格式照常回傳，標 legacy
    transfer_<id>.json 已是 done                   → 跳過
    1. 計算每個檔的 size 與 sha256（結果存進 transfer_<id>.json，檔案大小／修改時間沒變就不重算）
    2. GET /health 暖身
    3. POST manifest → 只上傳 missing／partial 的檔案，從伺服器給的位移接續
    4. 每個檔傳完 → complete；hash_mismatch → 重傳該檔（最多 3 次，之後標 failed）
    5. POST commit → 收到收據、比對 hash → transfer_<id>.json 標 done
```

- 上傳清單排除 `transfer_<id>.json` 本身。
- 錯誤分類與處理：

| 類型 | 例子 | 處理 |
|---|---|---|
| 網路 | 連不上、逾時、連線中斷 | 指數退避：1、2、4…最長 30 分鐘，不設次數上限 |
| 認證 | 401／403 | 停止自動重試，網頁顯示「token 錯誤」 |
| 衝突 | 409 conflict | 停止該場，網頁顯示，交給人處理 |
| 空間不足 | 507 | 1 小時後重試，網頁顯示 |
| 其他 4xx | 程式錯誤 | 停止該場並記錄，網頁顯示 |

- 狀態檔 `transfer_<id>.json` 用暫存檔 + `os.replace` 原子更新，程式當掉或重開都能接續。
- 只讀不刪：不修改、不移動、不刪除 `Sensor_Data/` 裡的任何原始檔。
- 網頁顯示：每場實驗的狀態（待傳／傳輸中 xx%／已封存／失敗原因）與「N 場待回傳」。
- 傳輸期間如果按下「開始監測」，傳輸在目前這一段送完後暫停，等監測結束再繼續。

---

## 5. 安全與設定（repo 是公開的）

- 接收服務只綁 `127.0.0.1`；對外只經 `tailscale serve`，因此只有同一個 tailnet 的裝置連得到。
- 每台收資料電腦一組隨機 token（例如 32 bytes）：
  - 收資料電腦：`%APPDATA%\SensorMonitor\transfer.json` 存 `server_url`、`source_name`、`token`。
  - 實驗室機台：`%APPDATA%\SensorArchive\server.json` 只存 token 的雜湊、對應的 `source_name`、封存根目錄（`D:\SensorArchive`）、保留空間。
  - token 綁定 `source_name`：A 電腦的 token 不能寫入 B 的資料夾。比對時用常數時間比較。
- repo 只放 `transfer.example.json`、`server.example.json`；`.gitignore` 加 `transfer.json`、`server.json`。
- 主機名稱、Tailscale IP、token 都不進 repo。
- 換 token：在實驗室機台的設定換掉雜湊、在收資料電腦換掉 token，不用動程式。

---

## 6. 實驗室機台部署步驟（實作完成後才執行）

1. 安裝 uv（使用者層級安裝，不需系統管理員）。
2. clone 本 repo（位置待決定，例如 `C:\Users\ai\Sensor-web-page`）並 `uv sync`。
3. 建 `D:\SensorArchive`，寫 `server.json`。
4. 啟動接收服務，執行 `tailscale serve --bg --http 5011 5011`（指令格式以實機 `tailscale serve --help` 為準）。
5. 工作排程器：使用者登入時啟動接收服務。
6. 從收資料電腦 `GET /health` 驗證。

會動到實驗室機台設定的步驟（2–5）由使用者確認後再做；機上 CLAUDE.md 的禁止刪除規則照常遵守。

---

## 7. 驗證與測試

**實作前要先在實驗室機台驗證（都是一次性小測試）**
- V1：`tailscale serve --http` 在這台 Windows 上能不能以一般使用者執行；從另一台 tailnet 裝置連得到、而且**不用**開防火牆。
- V2：收資料電腦在校內網路對實驗室機台的 `tailscale ping` 是否為 direct。
- V3：用 V1 的臨時服務傳 1 GB 測試檔，量實際速度（決定分段大小、是否需要限速）。

**實作後的測試**
- 單元測試：名稱檢查（路徑穿越）、offset 不符、hash 不符、封存衝突、空間不足、token 綁定錯誤的 source。
- 本機整合測試：同一台電腦上起接收服務，用模擬模式的資料走完整流程（測試時暫時允許模擬資料）。
- 故障注入：上傳到一半停掉接收服務 → 重啟後從斷點續傳；上傳到一半強制結束收資料端 → 重開後續傳；拔網路 → 開始監測出現警告、照常錄製、恢復後自動補傳。
- 實機測試：8 小時模擬資料量（約 3.7 GB）經 Tailscale 完整送達，sha256 全部一致。

---

## 8. 待決定

1. 是否同意從 SMB 改成「HTTP 接收服務 ＋ tailscale serve」？
2. 收資料電腦能不能安裝 Tailscale（登入同一個帳號）？
3. 實驗室機台上 repo 放哪裡？封存根目錄用 `D:\SensorArchive` 可以嗎？
4. 接收服務在使用者 `ai` 登入時啟動，代表實驗室機台要維持登入狀態（不能登出，鎖定畫面沒關係）。可以嗎？
