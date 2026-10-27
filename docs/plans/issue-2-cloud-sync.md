# Issue #2 規劃：監測結束後自動回傳資料到實驗室電腦 ＋ 歷史實驗視覺化網站

> 狀態：**規劃草案，尚未實作**（2026-10-05 整理）。相關：#1（儲存方式決策，已關閉）、#3（在監控系統內瀏覽已收集的數據）。
> 依據：README.md「存檔內容」、`app/web_monitor.py`（`ExperimentRecorder`、`preflight`、`_save_all`）、`app/chunked_spectrogram.py`。
> 檔名沿用 issue 原標題（cloud-sync），內容已改為「回傳實驗室電腦」方案。

## 已決定事項（2026-10-05，使用者確認）

| 項目 | 決定 |
|---|---|
| 資料去向 | **不上公有雲**。每次實驗結束後，資料回傳到實驗室電腦；雲端方案的調查結果保留在 §1.4 作為備案 |
| 網路 | 收資料的電腦和實驗室電腦通常在**同一個網路** |
| 實驗室電腦 | **Windows**，可 **24 小時開機** |
| 回傳範圍 | **所有數據**都回傳（含 WAV 與頻譜 CSV） |
| 開始前檢查 | 跑實驗前先測試是否連得到實驗室電腦；**連不到時允許開始**，先只存本機，之後自動補傳 |
| 機器 | 收資料的電腦 1 台（Windows）；實驗室電腦理想上 1 台（Windows）。程式本身跨平台（Windows／macOS），換機器不影響設計 |
| 模擬資料 | **不回傳** |
| 舊資料 | **要回傳**（含沒有 `experiment_<id>.json` 的舊格式資料夾） |
| 網站存取 | 先只在實驗室電腦本機瀏覽；之後有需要再做外部存取 |
| 網站內容 | 列出過去與未來的所有實驗、依實驗分類，顯示並視覺化各實驗收集到的數據。**詳細設計延後**，目前階段先完成連線／回傳方案 |
| 本機資料 | 回傳完成後**不自動刪除**收資料電腦上的資料 |

---

## 0. 現況與前提

每次按「開始監測」會建立 `Sensor_Data/EXP_YYYYmmdd_HHMMSS/`（`Sensor_Data/` 已列在 `.gitignore`）：

| 檔案 | 產生時機 | 預設設定下大小（README／`EST_BYTES_PER_HOUR`） | 性質 |
|---|---|---|---|
| `audio_<id>.wav` | 邊錄邊寫 | ≈159 MB／小時（22050 Hz 單聲道 16-bit；雙聲道加倍） | **唯一不可重建的原始資料** |
| `temperature_<id>.csv` | 邊錄邊寫，約 1 Hz | ≈0.2 MB／小時 | 原始量測 |
| `distance_<id>.csv` | 邊錄邊寫，預設 0.1 s | ≈1.5–2 MB／小時（README 寫 1.5、程式常數寫 2.0） | 原始量測 |
| `spectrogram_<id>.csv` + `_metadata.txt` | 停止後由 WAV 分塊產生 | ≈298 MB／小時（每 0.05 s 一列 × 129 頻率 × 3 欄，`%.6f`） | 衍生資料，可由 WAV 以 `chunked_spectrogram.py` 重算 |
| `experiment_<id>.json` | 開始時寫入、結束時補完（原子更新） | 數 KB | 參數、裝置、起訖、停止原因、各檔筆數、錯誤 |
| `SIMULATED.txt` | 模擬模式才有 | — | 模擬資料標記 |

關鍵事實：
- `_save_all()` 最後把 json 的 `status` 設為 `"completed"`，這是「可以開始回傳」最可靠的訊號（`raw_saved` 表示原始檔已關閉、頻譜 CSV 還沒產生完）。
- `preflight()`（`app/web_monitor.py:689`）已經分成 `errors`（擋下開始）與 `warnings`（提示後可繼續），連線檢查可以直接放進這個結構。
- 實驗 ID 是開始時間（`EXP_YYYYmmdd_HHMMSS`），**多台電腦可能撞名**，所以實驗室電腦上要依來源電腦分資料夾。
- 舊格式資料（例如 `EXP_20260910_*`，只有頻譜 CSV、沒有 json）需要另外處理。
- 監測服務預設只聽 `127.0.0.1:5002`；前端不依賴 CDN（`monitor.js` 自繪 `<canvas>`），可離線使用。

---

## 1. 方案比較

### 1.1 回傳實驗室電腦 vs 上公有雲

| 面向 | 回傳實驗室電腦（採用） | 公有雲（Firebase／Supabase／雲端硬碟，見 §1.4） |
|---|---|---|
| 費用 | 0（只需要硬碟） | 只放摘要時可免費；要放全部原始資料（每小時 460 MB）就一定要付費 |
| 容量限制 | 只受實驗室電腦硬碟限制 | 受免費額度與單檔上限限制（例如 Supabase Free 單檔 50 MB） |
| 能否傳完整原始資料 | 可以，同網路下很快 | 可以，但貴 |
| 網站 | 和資料在同一台電腦，直接讀資料夾 | 要另外做雲端資料來源與快取 |
| 主要風險 | 實驗室電腦變成**唯一完整副本**，要另外備份；硬碟壞掉就全沒了 | 金鑰外洩、帳單、服務政策改變（例如 Firebase Storage 2026-02-03 起要求付費方案） |
| 離開實驗室網路時 | 先存本機，回到同一網路後自動補傳（已決定）；之後也可以透過 Tailscale 等 VPN 在外面直接回傳 | 只要有網路就能傳 |

### 1.2 回傳方式比較（兩台在同一網路、實驗室電腦是 Windows）

> **2026-10-05 更新**：查過實驗室機台現況（§1.3a）後，連線設計改為「實驗室機台上的 HTTP 接收服務 ＋ `tailscale serve`」，詳見 [issue-2-transfer-design.md](issue-2-transfer-design.md)。下表保留為當初的比較。

| 方式 | 做法 | 優點 | 缺點 |
|---|---|---|---|
| **A. Windows 共用資料夾（SMB）＋ 本專案內建的複製程式**（建議） | 實驗室電腦開一個共用資料夾；收資料的電腦在實驗完成後，由背景程式把整個實驗資料夾複製過去並驗證 | Windows 內建，不用裝其他軟體；開始前的檢查可以直接「寫一個測試檔到共用資料夾」，測的就是實際傳輸路徑；全部邏輯都在本 repo，狀態可以顯示在監控網頁上 | 重試、驗證要自己寫（份量不大）；收資料的電腦若是 macOS，要先掛載共用資料夾 |
| B. Syncthing（點對點同步軟體） | 兩台各裝 Syncthing，把 `Sensor_Data/` 設為單向傳送 | 斷線續傳、加密、跨平台都已經做好；不用寫傳輸程式 | 要多裝、多維護一個常駐軟體；同步時機不受我們控制（錄製中也會一直傳正在寫入的檔案）；開始前檢查要另外去查 Syncthing 狀態 |
| C. 實驗室電腦的監控網站加「上傳 API」 | 收資料的電腦用 HTTP 把檔案 POST 給實驗室電腦的 Flask | 跨平台、不需要共用資料夾 | 要寫分段上傳與續傳（WAV 8 小時約 1.3 GB）、要做驗證，還要把實驗室電腦的網站開放給內網連入；工作量最大 |

**建議 A**。同網路下傳輸速度不是問題（見 §2），A 最符合「開始前確認連得到」的需求，而且不用多維護一個軟體。

關於「ping」：Windows 防火牆預設會擋 ICMP（ping），ping 得到也不代表共用資料夾能寫入。建議的檢查方式是**實際寫入並刪除一個測試檔**（加 3 秒逾時），這一步同時驗證了網路、帳號權限和實驗室電腦的剩餘空間。

### 1.3 實驗室電腦上的資料夾結構（建議）

```
D:\SensorArchive\                       ← 共用資料夾（磁碟代號依實際電腦）
  ├─ <來源電腦名稱>\                      ← 例如 LAB-LAPTOP-01，避免不同電腦的實驗 ID 撞名
  │    └─ EXP_YYYYmmdd_HHMMSS\           ← 與收資料電腦上的內容完全相同
  │         ├─ audio_*.wav、*.csv、experiment_*.json …
  │         └─ transfer_<id>.json        ← 回傳紀錄（來源、時間、每個檔的大小與 sha256）
  └─ .incoming\                          ← 傳輸中的暫存區，完成驗證後才搬到正式位置
```

### 1.3a 實驗室機台現況（2026-10-05 由機上的 Claude session 只讀查詢）

| 項目 | 結果 | 對規劃的影響 |
|---|---|---|
| 作業系統 | Windows 10 專業版 22H2（10.0.19045） | 可開 SMB 共用、建本機帳號 |
| 磁碟 | C: 1.86 TB，剩約 488 GB（系統與 GPU 分析共用）；D: 1.86 TB，幾乎全空 | **封存資料放 D:**。以每年 275 GB 估約可放 6–6.7 年；C: 只夠約 1.7 年，不建議。C、D 是否為不同實體硬碟未確認 |
| 共用資料夾 | 只有系統預設的 `ADMIN$`、`C$`、`D$`、`IPC$` | 需要新建 `SensorArchive` 共用 |
| 防火牆 | 三個設定檔都開著；「檔案及印表機共用」群組（SMB-In 等）**全部停用** | 目前從外部連不到 SMB 445，必須開規則（需系統管理員） |
| 網路設定檔 | 校園 Wi-Fi：**公用**；Tailscale：私人 | **不要在公用（校園 Wi-Fi）設定檔開 SMB**；建議只對 Tailscale 介面／私人設定檔開放 |
| 工具 | git 2.44；**沒有 uv**；機上沒有 Sensor-web-page 的 clone | 架網站前要先 clone 並安裝 uv |
| 權限 | 機上 Claude session 沒有系統管理員權限 | 開共用、改防火牆、建帳號要由使用者本人操作 |

**因此建議回傳路徑改走 Tailscale**：收資料電腦也加入同一個 Tailscale 網路，透過實驗室機台的 Tailscale IP 寫入共用資料夾，防火牆的 SMB-In 只對 Tailscale 介面（私人設定檔）開放。
- 好處：不必在校園公用 Wi-Fi 上開放檔案共用；離開實驗室時也能直接回傳。
- 要先測：兩台在校園網路中能否建立 Tailscale 直連（`tailscale ping` 顯示 `direct`）。如果只能走中繼（DERP），8 小時 3.7 GB 的資料會傳很慢；這時改成「在實驗室網路內才回傳大檔」，或評估其他做法。
- 收資料電腦的機器資訊（主機名稱、Tailscale IP）只放在 repo 外的設定檔，不寫進 repo。

**連線實測（2026-10-05，當天校園網路不穩）**：
- 實驗室機台 `tailscale netcheck`：UDP 可用、`MappingVariesByDestIP: false`（不是對稱型 NAT）、沒有 UPnP／NAT-PMP 埠映射、只有 IPv4。代表外部節點可以打洞直連。
- 兩個方向都實際打通直連：MacBook（手機熱點）↔ 實驗室機台，第一個封包就直連，延遲 39–274 ms（波動來自熱點）；另一台 Windows（校外網路）↔ 實驗室機台，前 8 個封包走香港中繼，第 9 個轉為直連（41 ms）。
- **設計影響**：連線可能要幾秒才從中繼轉成直連。回傳程式在開始傳大檔前先「暖身」（例如送幾個小請求，或先寫測試檔），開始前的連線檢查（§3.1）也要容許前幾秒較慢。
- **還沒測**：收資料電腦在校內的直連情況（校園 Wi-Fi 之間可能會擋），以及實際傳輸速度（SMB 尚未開放）。

### 1.4 公有雲方案調查（備案，2026-10-05 查詢）

> 查詢日期：**2026-10-05**。「✔」＝官方頁面確認；「？」＝未能在官方頁面確認，採用前請再查；價格均為美元、未含稅。
> 方法說明：多數數值由 subagent 以 WebFetch 讀官方頁面取得（WebFetch 回傳的是摘要文字），「Cloud Storage for Firebase 自 2026-02-03 起需 Blaze」這一條另以 curl 直接讀原始頁面確認。

#### 總表

| 面向 | Firebase（Firestore + Cloud Storage） | Supabase（Postgres + Storage） | 雲端硬碟同步 `Sensor_Data/`（Google Drive／OneDrive） |
|---|---|---|---|
| 免費方案能否放檔案 | **不能**：Cloud Storage 自 2026-02-03 起連預設 bucket 都要 Blaze（隨用隨付）方案 ✔ [1] | 可以：Free 1 GB Storage、單檔上限 50 MB ✔ [5][6] | 可以：Google 帳號免費 15 GB ✔ [9]；OneDrive 免費 5 GB（搜尋摘要，官方頁讀取逾時）？ [12] |
| 資料庫免費額度 | Firestore：1 GiB 儲存、每日 50,000 讀／20,000 寫／20,000 刪、每月 10 GiB 流出 ✔ [2][3] | 每個專案 500 MB DB、每月 5 GB egress、2 個 active 專案 ✔ [5] | 無資料庫 |
| 關鍵限制 | Firestore 單一文件 1 MiB ✔ [2]；免費額度只限預設資料庫 ✔ [2] | Free 專案**閒置 1 週會暫停**（Studio 內 1 年內可恢復）✔ [5][7]；Free 單檔 50 MB，不能調高 ✔ [6] | Drive 單檔 5 TB ✔ [10]、每人每 24 小時上傳 750 GB ✔ [11]；沒有查詢能力 |
| 付費方案與超量價格 | Firestore：讀 $0.03／10 萬次、寫 $0.09／10 萬次、儲存約 $0.15／GiB-月（頁面預設區域，**不確定是哪個區域**）？ [4]；Storage 新式 bucket 免費額度 5 GB-月 + 每月 100 GB 下載，只限 us-central1／us-west1／us-east1 ✔ [3]；超量依 GCS 區域價，asia-east1 價格未查到 ？ [13] | Pro「$25 起／月」，含 8 GB DB 磁碟、100 GB Storage、250 GB egress、單檔 500 GB ✔ [5]；超量：磁碟 $0.125/GB-月、Storage $0.0213/GB、egress $0.09/GB ✔ [5][8] | Google One（台灣）：100 GB NT$65／月、2 TB NT$330／月 ✔ [9]；學校 Google Workspace for Education 有全校共用 100 TB 儲存池（只看到搜尋摘要）？ [14]；Microsoft 365 Personal US$99.99／年、1 TB（美國價）？ [12] |
| 防止爆帳單 | **只有預算提醒、不會自動停用** Firestore／Storage（硬上限只適用 AI Logic、App Hosting、Functions、Extensions）✔ [15]；Blaze 是否必綁信用卡未在官方頁確認 ？ | Pro 的 **spend cap 預設開啟**：超過額度就停止該項使用、不收超量費（compute、PITR 等除外）✔ [16] | 固定月費，容量滿了就停止同步，不會產生超額費用 |
| 適合的查詢方式 | 等值／範圍查詢＋索引；支援 count／sum／average 聚合 ✔ [17]；多欄位排序篩選需要複合索引（免費 200 個 ✔ [2]）；沒有 SQL 和 join（？未查到官方說明） | 完整 SQL（篩選、join、聚合、時間序列表都行）；Python 端可用 supabase client 或直接連 Postgres | 無查詢；網站要自己列資料夾、讀每個 `experiment_*.json` 建索引（實驗少時可行） |
| 大檔續傳 | GCS resumable upload（本次未查官方頁 ？） | TUS 續傳，chunk 固定 6 MB，超過 6 MB 的檔案建議使用 ✔ [18] | 同步軟體自行處理 |
| 金鑰 | service account（Admin SDK 會略過 Security Rules）；用法本次未查官方頁 ？ | publishable key（前端用，受 RLS 限制）／secret key（只能放後端，略過 RLS）；舊版 anon／service_role 預計 2026 年底淘汰 ✔ [19] | 不需要金鑰（登入在 Drive 桌面版） |
| 實作量 | 中：兩套 SDK（Firestore + Storage），注意 1 MiB 文件上限 | 中：一套 SDK；DB schema 可用 SQL 管理 | **最低**：Drive for desktop 可以 mirror 任意本機資料夾 ✔ [20]，程式幾乎不用改 |

#### 若只上傳摘要與預覽（每場數 MB）

- **Firebase**：Firestore 免費額度足夠放摘要，但預覽檔和 CSV 需要 Storage，所以**一定要升 Blaze**，而且 Firestore／Storage 沒有硬性花費上限。對這個用量來說成本可能很低（新式 bucket 在美國區域有 5 GB-月免費），但要綁計費帳戶，金鑰外洩時的風險也比較高。
- **Supabase Free**：500 MB DB 與 1 GB Storage 大約能撐一年（若降採樣曲線也寫進 DB 資料表，DB 用量會增加，需要再估）。最大的風險是**閒置 1 週暫停**：實驗間隔超過一週，專案就會被暫停，上傳器需要能偵測並提示有人去恢復。要上傳原始音訊的話，Free 的 1 GB 不夠，需要 Pro（$25 起／月，spend cap 預設開啟）。
- **雲端硬碟同步**：如果直接同步整個 `Sensor_Data/`（每小時 460 MB），15 GB 免費空間大概 30 小時實驗就用完，而且正在寫入的大檔會一直被重新上傳。**只同步一個「匯出資料夾」**（上傳器只把摘要、溫度／距離 CSV 與預覽檔複製進去）就沒有這些問題，學校帳號空間通常也夠。缺點是沒有資料庫查詢，網站要靠本機快取建立清單。

#### 當時的建議

1. **需要跨實驗查詢、長期趨勢** → **Supabase**（SQL 最直接、Pro 有花費上限；Free 方案要處理閒置暫停）。
2. **只要「備份＋換電腦也看得到」、不想綁信用卡** → **雲端硬碟同步「匯出資料夾」**（若實驗室有學校 Google Workspace 帳號更理想），網站一律讀本機或同步下來的資料夾。
3. **Firebase 最不推薦**：存檔一定要 Blaze、沒有硬性花費上限、1 MiB 文件上限和這批資料形狀不合。只有在實驗室已經在用 Firebase 的情況下才考慮。
4. 也可以混合：摘要放 Supabase、原始音訊 FLAC 放雲端硬碟（大檔便宜、免費 egress 不受影響）。

#### 來源（皆查詢於 2026-10-05）
1. Cloud Storage for Firebase 方案變更 FAQ：https://firebase.google.com/docs/storage/faqs-storage-changes-announced-sept-2024
2. Firestore 配額與限制：https://firebase.google.com/docs/firestore/quotas
3. Firebase 價格：https://firebase.google.com/pricing
4. Firestore 價格（Google Cloud）：https://cloud.google.com/firestore/pricing
5. Supabase 價格：https://supabase.com/pricing
6. Supabase Storage 檔案大小限制：https://supabase.com/docs/guides/storage/uploads/file-limits
7. Supabase 升級／暫停專案：https://supabase.com/docs/guides/platform/upgrading
8. Supabase 磁碟用量：https://supabase.com/docs/guides/platform/manage-your-usage/disk-size
9. Google One 方案（台灣）：https://one.google.com/about/plans?hl=zh-TW
10. Google Drive 可儲存的檔案：https://support.google.com/drive/answer/37603
11. Google Workspace 儲存與上傳限制：https://knowledge.workspace.google.com/admin/drive/storage-and-upload-limits-for-google-workspace
12. OneDrive 方案：https://www.microsoft.com/en-us/microsoft-365/onedrive/onedrive-plans-and-pricing（只有搜尋摘要）
13. Cloud Storage 價格：https://cloud.google.com/storage/pricing（頁面讀取失敗）
14. Workspace for Education 儲存政策：https://support.google.com/a/answer/10403871（只有搜尋摘要）
15. Firebase 避免意外帳單：https://firebase.google.com/docs/projects/billing/avoid-surprise-bills
16. Supabase spend cap：https://supabase.com/docs/guides/platform/spend-cap
17. Firestore 聚合查詢：https://firebase.google.com/docs/firestore/query-data/aggregation-queries
18. Supabase 續傳上傳：https://supabase.com/docs/guides/storage/uploads/resumable-uploads
19. Supabase API keys：https://supabase.com/docs/guides/api/api-keys
20. Google Drive for desktop 同步：https://support.google.com/drive/answer/13401938

**採用前需要再查的項目**：Firestore 和 Cloud Storage 在 asia-east1 的價格；Blaze 是否必須綁信用卡；Firestore 每份文件的寫入速率與 batch 上限（官方頁沒寫明確數字）；Workspace for Education 儲存池（學校實際配額要問校方）；Microsoft 365 台灣價格。

---

## 2. 資料量與硬碟規劃

全部回傳，所以每小時實驗回傳量 ≈ **460 MB**（README 實測估計）：

| 實驗長度 | 回傳量 | 100 Mbps 網路（理論值） | 1 Gbps 網路（理論值） |
|---|---|---|---|
| 1 小時 | ≈0.46 GB | ≈40 秒 | ≈4 秒 |
| 8 小時 | ≈3.7 GB | ≈5 分鐘 | ≈30 秒 |

（理論值 = 檔案大小 ÷ 頻寬，實際上通常慢 1.5–3 倍；Wi-Fi 會更慢。）

實驗室電腦硬碟：假設一年 150 場、平均 4 小時 → 約 **275 GB／年**（假設值，請換成實際實驗頻率）。建議：
- 封存資料放在獨立的資料碟，預留至少 2 年的空間。
- **另外備份**：實驗室電腦變成唯一完整副本，至少要有一份外接硬碟或學校雲端硬碟的定期備份（可以只備份不可重建的 WAV、溫度／距離 CSV 與 json，頻譜 CSV 能重算）。
- 可選：頻譜 CSV 占總量約 65%，而且能由 WAV 重算。如果之後空間或傳輸時間成為問題，可以改成「不傳頻譜 CSV，在實驗室電腦上重算」。目前依決定**全部回傳**。

網站用的預覽檔（在實驗室電腦上產生，不需要回傳）：
- 降採樣曲線：溫度（原本約 1 Hz）、距離降到 1 s 一點（mean/min/max），每小時壓縮後估 0.1–0.2 MB。
- 頻譜預覽：只取 `DB_like`，時間軸平均成 1 s 一格 × 129 頻率，量化成 uint8，每小時約 0.46 MB（未壓縮）。網站讀這些預覽檔，不直接讀 300 MB 的頻譜 CSV。

---

## 3. 回傳流程

### 3.1 開始監測前：連線檢查（在 `preflight()` 加一項）

```
若「回傳到實驗室電腦」功能已開啟：
  1. 解析共用資料夾路徑（例如 \\LAB-PC\SensorArchive）
  2. 在 <來源電腦名稱>\.probe_<隨機字串> 寫入一個小檔並刪除，總逾時 3 秒
  3. 讀取實驗室電腦剩餘空間；少於「預估本次用量」時加警告
  失敗 → 放進 warnings（不擋下開始），訊息寫清楚原因
         （找不到主機／帳號密碼錯誤／沒有寫入權限／逾時），
         並說明「本次資料先存本機，連得到實驗室電腦後會自動補傳」；
         使用者在既有的確認視窗按「仍要開始」即可
```
- 回傳功能**關閉**時完全不檢查，行為和現在一樣（沒有設定的電腦不受影響）。
- 模擬模式（`--simulate`）不做這項檢查，因為模擬資料不回傳。
- 檢查失敗時在 `experiment_<id>.json` 記下「開始時連不到實驗室電腦」，方便事後追查。

### 3.2 監測中
- 不傳輸，避免和 WAV／CSV 寫入搶磁碟與網路。
- 實驗途中斷網不影響錄製（資料本來就先寫在本機），停止後再排隊回傳。

### 3.3 停止後：背景回傳
```
_save_all() 寫完 status="completed" → 喚醒回傳程式（不等待結果，不延後「監測完成」通知）

回傳程式每次醒來（停止監測後、程式啟動時、之後每 N 分鐘、網頁按「立即重試」）：
  for 每個 Sensor_Data/EXP_*：
     json 有 "simulated" 或資料夾有 SIMULATED.txt → 跳過（模擬資料不回傳）
     有 experiment json 但 status != "completed" → 跳過（還在錄或還在產生頻譜 CSV）
     沒有 experiment json 的舊格式資料夾    → 照樣回傳（見下方「舊資料」）
     本機 transfer_<id>.json 顯示已完成     → 跳過
     監測正在進行                           → 整輪延後
     否則：
       1. 逐檔複製到 \\LAB-PC\SensorArchive\.incoming\<來源>\<id>\（先寫 *.part，完成再改名）
       2. 逐檔比對大小與 sha256
       3. 全部通過 → 整個資料夾搬到 <來源>\<id>\，寫入兩邊的 transfer_<id>.json
       4. 任何一步失敗 → 記錄錯誤，下次從失敗的檔案繼續（已驗證的檔案不重傳）
```
- **冪等**：目的地已存在且 hash 相同就視為完成；hash 不同則不覆蓋，標成衝突、在網頁顯示，交給人處理。
- **失敗重試**：指數退避（1 → 2 → 4 … 最長 30 分鐘），沒有次數上限。帳號密碼錯誤、沒有權限這類錯誤不自動重試，直接在網頁顯示。
- **狀態寫在磁碟上**：`transfer_<id>.json` 用暫存檔 + `os.replace` 原子更新（和 `experiment_<id>.json` 同一模式），程式當掉或重開都能接續。
- **只讀不刪**：回傳不會修改、搬移、刪除收資料電腦上的原始檔。之後若要清理本機空間，另外做，而且必須以「實驗室電腦已驗證」為前提、由人操作。
- 監控網頁顯示每場實驗的回傳狀態（待傳／傳輸中／已完成／失敗原因），以及「N 場待回傳」。
- **模擬資料不回傳**：依 json 的 `simulated` 欄位或 `SIMULATED.txt` 判斷。
- **舊資料要回傳**：沒有 `experiment_<id>.json` 的資料夾沒有 `completed` 標記，判斷方式改成「資料夾最後修改時間早於程式啟動時間，而且目前沒有在監測」。回傳時在 `transfer_<id>.json` 標記 `legacy: true`，網站顯示為舊格式。第一次啟用回傳功能時，這些舊資料會自動排進佇列。
- **補傳**：開始時連不到實驗室電腦的實驗，和一般失敗的實驗一樣留在佇列裡，下次連得到時自動送出。

---

## 4. 帳號、密碼與設定（repo 是公開的）

1. 實驗室電腦建立**專用的本機帳號**（例如 `sensor-upload`），只給 `SensorArchive` 共用資料夾的寫入權限，不給系統管理員權限、不能登入桌面。
2. 收資料電腦的設定（共用資料夾路徑、來源電腦名稱、帳號）放在 **repo 外**，例如 `%APPDATA%\SensorMonitor\transfer.json`（Windows）、`~/Library/Application Support/SensorMonitor/transfer.json`（macOS）；repo 只放 `transfer.example.json`。
3. 密碼不寫進設定檔：Windows 用系統的「認證管理員」保存一次（例如 `cmdkey`；之後連線由系統自動帶入），macOS 用鑰匙圈。實作時確認作法後再寫進 README。
4. 主機名稱、IP 也屬於內部資訊，一樣不進 repo。
5. `.gitignore` 加保險規則：`transfer.json`、`.env*`（保留 `transfer.example.json`）。
6. 實驗室電腦的防火牆只在 Tailscale 介面（私人設定檔）開放檔案共用（SMB，TCP 445）；**不要**在校園 Wi-Fi（公用設定檔）或對外網開放（見 §1.3a）。

---

## 5. 視覺化網站架構（部署在實驗室電腦）

實驗室電腦執行同一個 repo 的監控服務，加上 #3 的「實驗紀錄」頁，資料來源設為封存資料夾：

```
瀏覽器 ──HTTP──▶ 實驗室電腦上的 Flask（app/web_monitor.py）
                   ├─ /history         實驗清單（篩選：日期、來源電腦、感測器、模擬資料）
                   ├─ /history/<id>    單次回放：溫度／距離曲線、頻譜預覽、下載原檔
                   ├─ /compare?ids=…   多場疊圖（對齊經過秒數）
                   └─ /api/history/*   JSON／預覽資料
                         │
                  讀 D:\SensorArchive\<來源>\EXP_*\（experiment json + 預覽檔）
                  預覽檔不存在時在背景產生並快取
```
- 不需要資料庫：清單由各實驗的 `experiment_<id>.json` 建立（啟動時掃描，結果放記憶體或 SQLite 快取）。實驗數到上千場、或需要「長期趨勢」查詢時，再加 SQLite 索引。
- **同一套程式在收資料的電腦上也能用**：資料來源指向自己的 `Sensor_Data/`，就是 #3 的本機紀錄頁。#3 原本規劃的「雲端來源」不再需要。
- 沿用現有前端風格（無 CDN、`<canvas>` 自繪），離線可用。
- **目前只在實驗室電腦本機瀏覽**（維持預設 `127.0.0.1`）。以下是之後要開放時的注意事項：
- 對內網或外部開放：實驗室電腦用 `--host 0.0.0.0` 讓其他電腦瀏覽時，「開始／停止監測」也會一起暴露。建議把歷史頁做成**唯讀模式**（例如 `--history-only`，只掛 `/history` 相關路由），或至少加共用密碼。Windows 防火牆只開放校內網段。從校外看時，優先用 Tailscale 這類 VPN，不要直接在路由器開埠對外。
- 實驗室電腦上的網站要**開機自動啟動**（工作排程器或服務），因為它 24 小時開著。

---

## 6. 分階段實作計畫

| 階段 | 內容 | 依賴 | 驗收 |
|---|---|---|---|
| **P1 預覽檔** | 由實驗資料夾產生 `preview_<id>.npz`（降採樣曲線 + 頻譜預覽 + 統計值）；提供 CLI 為既有資料夾補產生 | 無 | 1 小時實驗的預覽檔 < 2 MB；與原 CSV 抽樣比對一致 |
| **P2 實驗紀錄頁（#3）** | `/history` 清單、單次回放、下載；資料夾路徑可設定；標示舊格式 | P1 | 收資料電腦監測進行中打開歷史頁，不影響存檔（比對 WAV 長度與 json 筆數） |
| **P3 實驗室電腦設定** | 共用資料夾、專用帳號、防火牆、網站開機自動啟動；寫成 README 步驟 | 無（可與 P1/P2 平行） | 另一台電腦能用專用帳號寫入共用資料夾、不能寫其他地方 |
| **P4 開始前連線檢查** | `preflight()` 加入共用資料夾寫入測試；設定檔放 repo 外；功能預設關閉 | P3 | 拔網路線 → 按開始時出現警告、確認後照常錄製，接回網路後自動補傳；功能關閉時行為與現在相同 |
| **P5 背景回傳** | `transfer_<id>.json`、`.incoming` 暫存與驗證、退避重試、監測中暫停、網頁顯示狀態 | P3、P4 | 傳到一半拔網路線 → 恢復後自動續傳且 sha256 一致；強制結束程式後重開能接續；8 小時模擬實驗完整送達 |
| **P6 多場比較** | `/compare` 疊圖、篩選 | P2 | 能選 2–5 場實驗疊圖 |
| **P7 舊資料回傳與備份** | 既有資料夾（含沒有 json 的舊格式）排進佇列回傳；實驗室電腦的備份排程 | P5 | 現有 `Sensor_Data/EXP_*`（不含模擬資料）全部在實驗室電腦上，sha256 一致 |
| P8（之後再說） | 外部存取：`--history-only` 唯讀模式、共用密碼、Tailscale | P2 | 另一台電腦能瀏覽但無法開始／停止監測 |

P1、P2、P3 可以先做；P4、P5 是本 issue 的核心。

---

## 7. 還需要決定的問題

已回答（2026-10-05）：連不到時允許開始並補傳、收資料電腦 1 台 Windows、模擬資料不傳、舊資料要傳、網站先只在本機看、本機資料不自動刪除。網站要呈現的內容已有方向（見開頭「已決定事項」），細節等連線方案完成後再設計（P2、P6 屆時再細化）。

尚未決定：
1. **備份**：封存放 D:（見 §1.3a）。另外要備份到哪裡（外接硬碟、學校雲端硬碟、或 C: 做第二份）、多久一次？
2. **回傳走 Tailscale 還是校園區網**（見 §1.3a 建議）？收資料電腦能不能裝 Tailscale？
3. **實驗室電腦是否確定**：若之後換機器，只要改 repo 外的設定檔（共用資料夾路徑）即可，設計不受影響。
