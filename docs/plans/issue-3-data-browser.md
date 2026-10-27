# Issue #3 規劃：在監控系統內瀏覽實驗紀錄（本機 → 雲端）

- 狀態：草案。部分問題已在 2026-10-05 決定（見 §7.1），Q4/Q5 仍在討論
- 撰寫日期：2026-10-05
- 相關：#3（本 issue）、#2（雲端上傳與歷史視覺化）、#1（儲存方式）
- 範圍：只有規劃。本文件不含功能程式碼，也不修改 issue。

---

## 0. 現況摘要（規劃依據）

### 0.1 架構

- 啟動：`main.py` → `web_monitor.main()`。Flask 用 `threaded=True`，預設 `127.0.0.1:5002`，沒有 reloader（`app/web_monitor.py:1509`）。
- 推播：SSE `/api/stream`，事件有 `state / tick / audio / toast / resync / reset`。沒有 SocketIO。
- 現有路由：`/`、`/api/devices`、`/api/preflight`、`/api/start`、`/api/stop`、`/api/state`、`/api/spectrogram`、`/api/stream`（`web_monitor.py:1357-1432`）。目前沒有任何歷史資料或下載路由。
- 執行緒都在同一個行程內（`MonitorService`，`web_monitor.py:401`），有 `timer`、`temp`（1 s）、`audio`（每 0.1 s 錄音 → 寫 WAV → FFT/specgram → base64）、`rangefinder`（0.1 s），停止時另開 `finalize`。
- 鎖：`service.lock`（RLock）與 `recorder.lock`。寫檔、`summary()`、`write_json` 共用 `recorder.lock`。
- `phase` 有四種值：`idle | running | stopping | stopped`。
- 前端是單頁，沒有外部函式庫，原生 canvas 自繪（`app/static/monitor.js`，單一 IIFE）。
  - 主要元件：`CanvasChart` 基底、`LineChart`、`Spectrogram`（Int16 dB×10、Jet LUT、像素合併時取 max、autoRange 百分位）。
  - 溫度與距離目前只有數字卡，沒有曲線。
- `DATA_DIR = BASE_DIR/Sensor_Data`（`web_monitor.py:63`），寫死、不可設定，已列入 .gitignore。

### 0.2 新格式（`Sensor_Data/EXP_<YYYYmmdd_HHMMSS>/`）

| 檔案 | 內容 | 注意 |
|---|---|---|
| `experiment_<id>.json` | `experiment_id, status(running→raw_saved→completed), start_time(ISO+tz), start_time_unix, end_time(_unix), duration_s, stop_reason, parameters{sample_rate, update_interval_s, display_history_s, distance_interval_s, refl_mode, use_mongodb, nfft, noverlap}, enabled{temp,audio,distance}, simulated[], simulate_faults, devices, platform, audio{channels,…}, spectrogram{status,rows,…}, mongodb, files{…}, errors[]` | 用 tmp + `os.replace` 原子更新。程式崩潰時會停在 `running`，沒有 `end_time`。 |
| `temperature_<id>.csv` | `Timestamp(ISO 字串), Elapsed(s), Temperature(C), Status` | utf-8-sig（有 BOM）。1 s 一列，讀取失敗也會寫一列，溫度欄留空。 |
| `distance_<id>.csv` | `Timestamp(epoch float), Elapsed(s), Absolute(mm), Relative(mm)` | utf-8-sig。約 0.1 s 一列，只寫有效讀值。**Elapsed 以第一筆有效讀值為 0，和溫度的零點不同**（`web_monitor.py:1239,1278`）。 |
| `audio_<id>.wav` | 單一檔案，16-bit，取樣率由使用者設定（預設 22050），聲道數由裝置決定 | 每次 `writeframes` 都會回寫 header。逾時時會寫入補零片段。 |
| `spectrogram_<id>.csv` | `Time(s)` 加上每個頻率三欄 `{f}Hz_DB_like / _Power / _SNR(dB)`，129 bins，共 388 欄，時間插值為 0.05 s | **只在停止後**由 `chunked_spectrogram.py` 從 WAV 產生。`DB_like` 是線性 PSD，不是 dB。`Power = DB_like²`。SNR 每個頻率只有一個常數，整欄重複（`app/chunked_spectrogram.py:270-302`）。1 小時約 72,000 列、約 300 MB。 |
| `spectrogram_<id>_metadata.txt` | `key: value` 文字檔（SR、NFFT、noverlap、Time Interval、bins…） | 沒有指定 encoding，Windows 上可能是 cp950。 |
| `SIMULATED.txt` | 模擬資料警語 | 只有模擬模式會產生。 |

大小估計（`web_monitor.py:79-80`）：每小時約 458 MB，其中 WAV 約 159 MB、頻譜 CSV 約 298 MB、距離約 2 MB、溫度約 0.2 MB。

### 0.3 舊格式（`EXP_20260910_144139`、`EXP_20260910_153623`）

- 只有兩個檔：`spectrogram_<id>.csv` 與 `spectrogram_<id>_metadata.txt`。沒有 json、WAV，也沒有溫度與距離資料。
- CSV 欄位結構與新格式相同（388 欄），可以共用解析器。長度分別約 3 s 與 13.5 s。
- 開始時間只能從資料夾名稱或 metadata 的 `Timestamp` 推得。

### 0.4 規劃時順帶發現的既有問題（本計畫不修，只記錄）

1. 多聲道 WAV 的樣本是交錯排列，`chunked_spectrogram` 把它當成一維訊號計算頻譜（`chunked_spectrogram.py:39-40,70`），多聲道錄音的頻譜因此失真。
2. 頻譜 CSV 以 `%.6f` 輸出線性 PSD，很小的值會被截成 0。實測舊檔約 3–10% 的值是 0，轉 dB 時要設下限。
3. 溫度與距離的 Timestamp 格式不同，Elapsed 的零點也不同（見上表）。
4. metadata.txt 用系統預設編碼寫入。

---

## 1. 使用情境與頁面設計

### 1.1 使用情境

| # | 情境 | 需要的功能 |
|---|---|---|
| U1 | 剛停止監測，想立刻看完整的溫度、距離、頻譜 | 列表最上方就是剛結束的實驗，一鍵進入回放 |
| U2 | 回頭找上週某次 PEO 實驗 | 依日期、時長、感測器、模擬或真實篩選；可加備註或標籤（見 §7 Q9） |
| U3 | 比較數次實驗的溫度上升曲線或距離變化 | 疊圖比較，可選擇時間對齊方式 |
| U4 | 某段頻譜有異常，想聽那段聲音 | 框選時間範圍，播放或下載該段 WAV |
| U5 | 把原始檔交給同學分析 | 單檔下載，或整包 zip |
| U6 | 監測進行中，同時翻看昨天的資料 | 歷史頁不能影響即時監測（§4） |
| U7 | （#2 完成後）在另一台電腦看雲端資料 | 切換資料來源（§2.3） |

### 1.2 頁面形式（建議，待 §7 Q2 確認）

建議新增獨立頁面 `/history`，不在 monitor.html 內加分頁：

- monitor.js 是單一 IIFE，裡面有 SSE、狀態機與即時圖表。把歷史瀏覽塞進去，檔案會膨脹，也容易互相干擾。
- 歷史頁不連 SSE，瀏覽器不必一邊畫即時頻譜、一邊畫歷史圖。
- 兩頁在 topbar 用 `即時監測 | 實驗紀錄` 互相連結。主畫面（monitor.html）要有明顯的「歷史數據」按鈕可以跳轉（使用者 2026-10-05 決定）。監測中的 topbar 會顯示紅點 chip「監測中 03:12:45」，點了回到即時頁。
- 共用部分：`monitor.css` 的色票與 class，以及從 monitor.js 抽出的 `charts.js`（`CanvasChart`、`LineChart`、`Spectrogram`、Jet LUT、`readTheme`），見 §6 P0。
- 路由用 hash：`/history#/`（列表）、`#/exp/<id>`（單次）、`#/compare?ids=a,b`（比較）。可以加書籤，也可以分享 URL。

### 1.3 風格

沿用現有深色儀表板，不引入新色票：

- 色票：`--bg #0e1116`、`--surface #151a21`、`--border #252c36`、`--accent #4c8dff`、狀態色 `--ok/--warn/--err/--off`、圖表色 `--chart-*`。淺色模式自動跟隨 `prefers-color-scheme`。
- 元件：`.topbar`、`.layout`（側欄 300px 加內容區）、`.group`、`.field`、`.btn*`、`.stat`、`.chart`、`.canvas-wrap`、`.canvas-spec`、`.recbar-files`、`.dot[data-level]`、`.sim-tag`、`.toast`、`dialog`。
- 數字一律用 `--mono` 加 `tabular-nums`。
- 疊圖比較需要新增 4–6 個類別色，以 `--series-1..6` 變數加入 monitor.css，深色與淺色兩組都要。這是唯一的新 token。

### 1.4 列表頁 `#/`

```
┌ topbar: [■ Sensor Monitor]  即時監測 | 實驗紀錄*      資料來源 [本機 ▾|雲端(待#2)]  ● 監測中 01:12:03 ┐
├ sidebar ───────────────┬ content ───────────────────────────────────────────────────────────────┤
│ 篩選                    │ 共 42 筆 · 本機 18.3 GB                          [比較已選 (2)] [重新整理] │
│ 日期  [2026-09-01]~[  ] │ ☐ 狀態 開始時間          ID                  時長     感測器      大小   │
│ 感測器 ☑溫 ☑音 ☑距      │ ☐ ●錄製中 10-05 09:00  EXP_20261005_090000  01:12:03 溫 音 距     —     │
│ ☐ 隱藏模擬資料          │ ☐ ●完成  10-04 13:20  EXP_20261004_132011  08:00:12 溫 音 距   3.6 GB │
│ ☐ 只顯示有頻譜          │ ☐ ●完成  10-04 10:02  EXP_20261004_100230  00:05:00 溫 音 [模擬] 40 MB │
│ 搜尋 ID/備註 [       ]  │ ☐ ▲中斷  10-03 …      …                    01:02:00 溫 音       …    │
│ 排序 [新→舊 ▾]          │ ☐ ◆舊格式 09-10 15:36 EXP_20260910_153623  00:00:13 (僅頻譜)   1 MB  │
└────────────────────────┴───────────────────────────────────────────────────────────────────────┘
```

- 狀態 dot 對應：`completed` 顯示 ok，`running` 且為目前實驗顯示 busy（錄製中），`running` 但非目前實驗顯示 warn（中斷，未正常結束），`raw_saved` 或頻譜 error 顯示 warn（頻譜未產生），舊格式顯示 off 並加註「舊格式」。
- 感測器 chip 依 `enabled`；模擬資料用 `.sim-tag`。
- 大小欄取 `files{}` 統計。若沒有統計或是舊格式，就用 `os.stat` 加總。
- 列表很長時分頁（limit 50），日後可改成無限捲動。

### 1.5 單次實驗回放 `#/exp/<id>`

```
┌ EXP_20261004_132011 ●完成   2026-10-04 13:20:11 → 21:20:23 (08:00:12)  [下載 ▾] [比較] [JSON] ┐
├ 摘要卡 (.stat ×4)：溫度 min/avg/max │ 距離 min/max/Δ │ 音訊 22050Hz 1ch │ 錯誤 3 筆            │
├ 總覽條 overview：低解析度頻譜縮圖（全長） + 可拖曳的框選範圍 [=====]                            │
├ 溫度 (°C) ── LineChart（min/max 包絡 + 平均線）                                              │
├ 距離 (mm) ── LineChart；切換 Absolute / Relative                                             │
├ 頻譜 (Hz × t) ── Spectrogram 熱圖 + colorbar；色階 [自動|固定 vmin vmax]  頻率上限 [11025 ▾]   │
├ 波形包絡 ── min/max 包絡；[▶ 播放選取範圍]（上限 60 s）                                        │
├ 檔案 (.recbar-files)：每檔名稱、大小、[下載]；[全部打包 zip]                                  │
└ 錯誤紀錄 errors[]（time, message）／參數 parameters／裝置 devices                              │
```

- 四條時間軸共用同一個 x 軸，單位是實驗開始後的秒數。滾輪縮放、拖曳平移、hover 顯示十字線與各軸數值。
- 縮放後依目前範圍與畫面寬度向後端請求新的解析度（§3 金字塔），有 150 ms debounce，並取消前一次尚未完成的請求（`AbortController`）。
- 某感測器沒有啟用或沒有檔案時，那一列顯示 `placeholder()`，例如「本次未啟用距離感測」或「舊格式資料，無溫度紀錄」。
- 錄製中的實驗只顯示摘要與一個「前往即時監測」按鈕（§4.4）。
- 中斷的實驗、或 `raw_saved` 而沒有頻譜 CSV 的實驗，頻譜區仍可從 WAV 顯示（§3.2）。是否提供「補產生頻譜 CSV」按鈕見 §7 Q11。

### 1.6 疊圖比較 `#/compare?ids=…`

- 從列表勾選 2–6 筆，或在單次頁按「比較」加入比較籃。
- 指標：溫度、距離（Abs/Rel）、頻帶能量（使用者選頻帶，例如 2–4 kHz，對每個時間欄計算該頻帶 dB 平均，得到一條曲線）。
- 對齊方式：
  - 依實驗開始（預設）。
  - 依絕對時間（同一天的數次實驗）。
  - 依事件：手動輸入每筆的偏移秒數。
- 頻譜圖不疊合，改用小倍數（small multiples）上下排列，共用頻率軸與色階，並可勾選「統一色階」。
- 每條線的顏色用 `--series-n`，圖例標示 ID 與備註。

### 1.7 下載

- 單檔：`GET …/files/<name>`，支援 Range 與續傳。大檔不經 Python 讀進記憶體，用 `send_file(conditional=True)`。
- 整包：zip 串流，用 `ZIP_STORED`，不壓縮。WAV 與 CSV 壓縮效益低又耗 CPU。zip 實作放在 P3。
- 範圍匯出（P3）：目前選取範圍的溫度、距離 CSV，以及該段 WAV。頻譜 CSV 的範圍匯出用行號切片，不重算。

---

## 2. 後端 API 與資料來源抽象層

### 2.1 程式結構（新增，不動現有模組的行為）

```
app/history/
  __init__.py          # Flask Blueprint "history"，url_prefix="/api/history"；另有頁面路由 /history
  model.py             # dataclass：ExperimentSummary, ExperimentDetail, FileInfo, Series, SpecTile
  sources/
    base.py            # ExperimentSource 抽象類別
    local.py           # LocalSource(DATA_DIR)：新、舊格式解析
    cloud.py           # （P4）CloudSource，依 #2 的方案實作
  readers.py           # CSV 尾端安全讀取、BOM、時間對齊、metadata.txt 解析
  preview.py           # 預覽產物（金字塔）的建立與讀取（§3）
  worker.py            # 低優先序的子行程建置佇列（§4）
app/templates/history.html
app/static/charts.js   # 從 monitor.js 抽出的共用圖表元件
app/static/history.js
```

`web_monitor.py` 只做兩件事：`app.register_blueprint(history_bp)`，以及提供一個 lock-free 的唯讀函式 `live_status() -> (phase, current_experiment_id)` 給 blueprint 使用。

### 2.2 端點

所有回應都包成 `{data: …}` 或 `{error: {code, message}}`，與現有的 `api()` 包裝一致。`source` 參數預設為 `local`。

| 方法 | 路徑 | 參數 | 回應 |
|---|---|---|---|
| GET | `/history` | — | history.html |
| GET | `/api/history/sources` | — | `[{id:"local", label:"本機", available:true, capabilities:{raw_files:true, full_spectrogram:true}}, {id:"cloud", available:false, reason:"待 #2"}]` |
| GET | `/api/history/experiments` | `source, q, from, to, sensors=temp,audio,distance, hide_sim, has_spec, sort=start_desc, limit=50, offset=0` | `{items:[ExperimentSummary], total, total_bytes}` |
| GET | `/api/history/experiments/<id>` | `source` | `ExperimentDetail` |
| GET | `/api/history/experiments/<id>/series/<kind>` | `kind = temperature \| distance_abs \| distance_rel \| band_energy`；`t0, t1`（秒，相對開始）、`points=1500`；band_energy 另需 `f_lo, f_hi` | `{t0, dt?, t:[…], min:[…], max:[…], mean:[…], level, exact:bool}`。點數少於 `points` 時回傳原始點，`exact=true`。 |
| GET | `/api/history/experiments/<id>/spectrogram` | `t0, t1, cols=1200, fmax` | `application/octet-stream`：Int16 dB×10，排列與現有 `/api/spectrogram` 相同，可直接餵給 `Spectrogram`。header 帶 `X-Spec-T0, X-Spec-Dt, X-Spec-Cols, X-Spec-NFreq, X-Spec-Df, X-Spec-Source(csv\|wav), X-Spec-Level, ETag`。 |
| GET | `/api/history/experiments/<id>/waveform` | `t0, t1, points=1500, ch=0` | `{t0, dt, min:[…], max:[…]}` |
| GET | `/api/history/experiments/<id>/audio` | `t0, t1`（上限 60 s） | `audio/wav` 片段，伺服器端以 `wave` 讀取區段並重組 header |
| GET | `/api/history/experiments/<id>/files/<name>` | `download=1` | 原檔串流，支援 Range 與 ETag。雲端來源則 302 到簽名 URL。 |
| GET | `/api/history/experiments/<id>/archive.zip` | `include=…` | zip 串流（P3） |
| GET | `/api/history/experiments/<id>/preview` | — | 預覽建置狀態 `{state: none\|queued\|building\|ready\|error, progress}` |
| POST | `/api/history/experiments/<id>/preview` | — | 要求建置。監測中依 §4 的策略可能回 `202 queued` 或 `409 deferred`。 |

- 比較頁不需要新端點：前端對每個 id 各呼叫 `series`，在前端依對齊方式平移。
- 若選擇做「備註／標籤」（§7 Q9），另加 `PATCH /api/history/experiments/<id>/notes`，寫入獨立的 `notes.json`，不改 experiment json。

`ExperimentSummary`：

```json
{
  "id": "EXP_20261004_132011",
  "source": "local",
  "format": "v2 | legacy_spec | unknown",
  "status": "live | finalizing | completed | spec_missing | interrupted | legacy | corrupt",
  "start_unix": 1791098411.0, "end_unix": 1791127223.0, "duration_s": 28812.0,
  "duration_estimated": false,
  "sensors": {"temp": true, "audio": true, "distance": true},
  "simulated": false,
  "has": {"temperature": true, "distance": true, "wav": true, "spectrogram_csv": true},
  "total_bytes": 3711000000,
  "error_count": 3,
  "notes": null
}
```

`ExperimentDetail` 在 Summary 之外再加：`parameters`、`devices`、`audio`、`errors[]`、`files[{name, kind, bytes, mtime, downloadable}]`、`spectrogram{sr, nfft, noverlap, n_freq, df, dt, rows}`、`preview{state}`、`raw_json`。舊格式沒有的欄位給 `null`。

### 2.3 資料來源抽象層（為 #2 預留）

```python
class ExperimentSource(Protocol):
    id: str; label: str
    def capabilities(self) -> dict: ...                      # raw_files, full_spectrogram, audio_playback, notes
    def list_experiments(self, flt: Filter) -> Page[ExperimentSummary]: ...
    def get_experiment(self, exp_id: str) -> ExperimentDetail: ...
    def read_series(self, exp_id, kind, t0, t1, points, **kw) -> Series: ...
    def read_spectrogram(self, exp_id, t0, t1, cols, fmax) -> SpecTile: ...
    def read_waveform(self, exp_id, t0, t1, points, ch) -> Series: ...
    def open_file(self, exp_id, name) -> LocalFile | RemoteURL: ...
```

- 註冊表是 `SOURCES = {"local": LocalSource(DATA_DIR)}`。雲端來源只在設定檔或環境變數有金鑰時才註冊。金鑰不可進 git，延續 #2 的考量。
- 前端只依 `capabilities` 決定要顯示哪些按鈕（例如雲端沒有原始 WAV 時，就隱藏播放與 WAV 下載），不判斷來源種類。
- **關鍵設計：§3 的「預覽產物」格式同時就是 #2 的「雲端摘要」格式。**
  - 本機的預覽產物有 summary.json、降採樣曲線、頻譜金字塔低階層，每小時約數 MB，可直接被 #2 上傳。
  - `CloudSource` 只要能讀到同樣的產物就能實作上面的介面，可以是 Supabase Storage 物件或雲端硬碟同步資料夾。
  - 若 #2 選擇「雲端硬碟同步 `Sensor_Data/`」，`CloudSource` 幾乎等於指向另一個根目錄的 `LocalSource`。
- 遠端讀取有延遲，`CloudSource` 自己負責本機快取。API 層與前端不需要改。

### 2.4 安全

- `<id>` 只接受 `^EXP_\d{8}_\d{6}(_\w+)?$`，`<name>` 只接受該實驗 `files` 白名單內的檔名。最後以 `realpath` 確認路徑仍在 `DATA_DIR` 內，防止路徑穿越。
- 第一版所有端點都唯讀，沒有刪除或改名。即使未來加入，也要擋下錄製中的實驗。
- 預設綁定 127.0.0.1。若使用者用 `--host 0.0.0.0`，資料會對區網公開，見 §7 Q12。

---

## 3. 大檔處理：降採樣、分段載入、快取

### 3.1 原則

1. 不把整份大檔丟給瀏覽器。每次回應的大小只取決於畫面像素：約 1200 欄 × 129 bins × 2 B，約 310 KB；曲線約 1500 點 × 3。
2. 不在 HTTP 請求執行緒裡解析大檔。重的工作只在一次性的「預覽建置」裡做（子行程，§4）。請求執行緒只做 memmap 切片與小量 pooling。
3. 降採樣一律用 min/max（包絡）或 max-pooling，不用平均。這樣尖峰不會被抹掉，也與即時頁 `Spectrogram` 合併像素時取 max 的行為一致。

### 3.2 預覽產物（preview）

每個實驗建置一次，存成快取：

```
<cache_root>/<source>/<exp_id>/v1/
  manifest.json        # 版本、來源檔 (name, size, mtime_ns)、建置時間、參數、各層級資訊
  summary.json         # ExperimentSummary + 統計值 (min/max/mean)
  temp.npz             # t(float64 秒, 相對 start), y(float32)；level k: min/max/mean，每層 ×8
  dist.npz             # 同上，abs 與 rel
  spec_L0.npy          # int16 dB×10，shape (n_cols, n_freq)，dt = 0.05 s（與 CSV 相同解析度）
  spec_L1.npy …        # 時間方向每層 ×4 max-pooling，直到 ≤ 2048 欄
  wave_L*.npy          # 波形 min/max 包絡金字塔（L0 = 每 256 樣本一對 min/max）
  band_cache/          # 已算過的頻帶能量曲線（按需求產生）
```

- 大小估計（1 小時）：
  - `spec_L0` 為 72,000 × 129 × 2 B，約 18.6 MB；全部層級合計約 25 MB。原 CSV 約 300 MB。
  - 波形包絡約 2.5 MB。
  - 8 小時總計約 220 MB，約為原始資料的 6%。
- 讀取時用 `np.load(mmap_mode="r")`。選擇欄數大於等於請求 `cols` 的最粗層級，切出 `[t0, t1]`，再 pool 到 `cols`。單次請求只觸及數 MB 的資料。
- dB 轉換：`dB = 10*log10(max(PSD, 1e-12))`，存成 dB×10 的 int16。CSV 中被截成 0 的 PSD 值會落到下限，畫面上是色階最低色。

**頻譜的來源**（§7 Q4）：

- 有 WAV 時（建議）：沿用 `chunked_spectrogram` 的分塊 specgram 邏輯，從 WAV 重算，速度約每小時錄音 6 秒。
  - 這也可以涵蓋 `raw_saved` 或中斷實驗這類沒有頻譜 CSV 的情況。
  - 建置時要與 CSV 做一致性抽樣檢查：隨機抽 20 欄比對，誤差 < 0.1 dB。
- 只有 CSV 時（舊格式）：串流逐塊讀取，只取 `DB_like` 欄（每 3 欄取第 1 欄），不讀 Power 與 SNR。
  - 文字解析很慢，估計每 300 MB 數十秒，要實測。
  - 但只做一次，而且舊資料都很小。
- SNR 是每個頻率一個常數，存進 `summary.json` 即可，不必存進矩陣。

**時間對齊**：所有序列統一換算成「相對 `start_time_unix` 的秒數」。

- 溫度：解析 ISO `Timestamp` → epoch → 減去 start。
- 距離：`Timestamp` 是 epoch float，直接減去 start。不用 Elapsed，因為兩者 Elapsed 的零點不同。
- 頻譜與波形：時間 0 即為音訊開始。音訊實際開始與 start_time 的微小差距是已知限制，要寫進文件。

### 3.3 小檔（溫度、距離）

- 溫度 1 小時約 3,600 列，距離約 36,000 列，8 小時約 29 萬列、16 MB。
- 第一版（P1）可以在請求時直接用 `csv` 模組串流解析並降採樣，不需要預覽。
- 建置預覽之後改讀 npz。

### 3.4 快取

- 位置（§7 Q5）：建議放在 `Sensor_Data/.cache/`。好處是與資料同一顆磁碟、已被 gitignore、不污染各實驗資料夾，也不會被 #2 誤上傳成原始檔。列表時要略過以 `.` 開頭的目錄。
- 失效判斷：`manifest` 記錄來源檔的 `(size, mtime_ns)` 與演算法版本 `v1`，任一不符就重建。
- 錄製中的實驗不建快取，因為檔案還在變動。
- 容量：在 manifest 記錄最後存取時間。超過上限（預設 5 GB）時，以 LRU 刪除，但只刪 `.cache` 內的檔案。
- HTTP：頻譜與曲線回應帶 `ETag = manifest hash + 參數`，再加 `Cache-Control: private, max-age=…`。瀏覽器重複縮放同一區間時直接命中快取。
- 前端：已載入的 tile 依 `(level, t0 區塊)` 存成 LRU，上限約 50 MB。

### 3.5 未建置預覽時的行為

- 進入單次頁時，若 `preview.state = none`，就自動 `POST …/preview` 排入佇列（監測中依 §4 策略）。
  - 頁面先顯示 json 摘要、溫度與距離（小檔即時解析），頻譜區顯示進度條。
  - 前端每 1 s 輪詢 `GET …/preview`。不走 SSE，避免和即時串流混用。
- 舊格式的小檔頻譜 CSV（< 20 MB）允許在請求中直接解析，不需排隊。

---

## 4. 監測進行中瀏覽歷史資料：不拖慢即時監測與存檔

### 4.1 風險來源

| 資源 | 風險 | 對策 |
|---|---|---|
| GIL | 音訊 worker 每 0.1 s 要做錄音、寫 WAV、FFT/specgram 與 base64。若同一行程內解析 300 MB CSV 或重算頻譜，會搶 GIL，造成 `record_audio` 逾時、補零、即時頻譜卡頓。 | **重工作一律放子行程**：`concurrent.futures.ProcessPoolExecutor(max_workers=1)`，用 spawn context，Windows 與 macOS 一致。請求執行緒只做 memmap 切片。 |
| CPU | 子行程與擷取行程搶核心 | 子行程啟動後降低優先序：POSIX 用 `os.nice(10)`；Windows 用 `ctypes` 呼叫 `SetPriorityClass(BELOW_NORMAL_PRIORITY_CLASS)`，不新增依賴。 |
| 磁碟 I/O | 讀數百 MB 與寫 WAV／CSV（每列 flush）競爭磁碟 | 監測中建置時分塊讀取，每塊之間 `sleep` 節流，例如上限 30 MB/s。下載採 `send_file`，由 OS 處理。 |
| 鎖 | `service.lock` 與 `recorder.lock` 被寫檔與 SSE 使用 | **歷史模組完全不取這兩把鎖**。只透過 `live_status()` 讀取 `phase` 與目前的 `experiment_id`，兩者都是單一屬性讀取，不需要鎖。 |
| Flask 執行緒 | 每個 SSE 連線佔一條執行緒；慢請求會佔住執行緒 | 歷史端點用 `threading.BoundedSemaphore(2)` 限制同時執行數，超過就回 `429` 加 `Retry-After`。所有請求都有時間上限，tile 查詢目標 < 50 ms。 |
| 記憶體 | 大陣列載入記憶體 | 一律 memmap，或分塊處理後立刻釋放。 |

### 4.2 監測中的建置策略（§7 Q6，建議選 B）

- A. 監測中照常建置（低優先子行程加 I/O 節流）。
- **B. 監測中只建置「小型」實驗（預估 < 500 MB）**。大型實驗排入佇列，等 `phase == idle/stopped` 再自動開始。頁面顯示「監測進行中，預覽將於監測結束後建立」，已建好的預覽照常瀏覽。
- C. 監測中完全不建置。

另外：`phase == stopping`（finalize 正在產生頻譜 CSV）時，佇列一律暫停，不跟 finalize 搶資源。

### 4.3 驗收方法（P2 必做）

- 用 `uv run python main.py --simulate` 監測 30 分鐘。期間以腳本持續請求歷史 API：列表、tile 縮放、建置一個 1 小時的合成實驗預覽。
- 和沒有歷史請求的基準組比較以下指標：
  1. experiment json 的 `errors[]` 中，音訊逾時與補零的次數，應與基準相同。
  2. WAV 的實際樣本數相對於 `duration × sr` 的落差，差異應 < 0.1%。
  3. SSE `tick` 的間隔抖動（p99），目標 < 基準 + 50 ms。
  4. `audio` 事件的頻率。
- 需要的量測點：用前端 console 記錄或另寫測試腳本訂閱 SSE。**不修改即時監測的程式**；若需要內部計時，另開 issue 討論。

### 4.4 錄製中的實驗本身

- 列表中標示「錄製中」。單次頁只顯示 json 摘要與「前往即時監測」按鈕，不讀正在寫入的 WAV 與 CSV。
- 理由：即時頁已經有這些資料，重複讀取正在寫入的檔案只增加風險。
- 若要在歷史頁看錄製中實驗的完整溫度曲線（即時頁只有數字），另列為 P3 選項：唯讀 tail 解析，丟棄最後的半行。
- `phase == stopping` 時，該實驗標示為「處理中」，完成後自動刷新。
- 判斷「中斷」：json 的 `status == running`，且這個 id ≠ 目前的 `experiment_id`（或 `phase == idle`）。

---

## 5. 舊格式資料相容

### 5.1 偵測規則（依序判斷）

1. 不是符合 `EXP_*` 的目錄，或以 `.` 開頭 → 略過。
2. 有 `experiment_<id>.json`，且能解析 → `format=v2`。
   - 解析失敗時，再試一次（可能遇到 Windows 上 `os.replace` fallback 寫到一半）。仍失敗則 `status=corrupt`，但仍列出檔案清單，並提供下載。
3. 沒有 json，但有 `spectrogram_<id>.csv` → `format=legacy_spec`：
   - 開始時間：優先用資料夾名 `EXP_YYYYmmdd_HHMMSS`（本地時區），其次用 metadata 的 `Timestamp`。
   - 時長：讀 CSV 最後一列的 `Time(s)`（從檔尾往回找最後一個完整行，不必讀整檔），標記 `duration_estimated=true`。
   - 頻譜參數：解析 metadata.txt，編碼依序試 `utf-8-sig` → `cp950` → `latin-1`。沒有 metadata 時，從 CSV header 的頻率欄名推得。
   - `sensors`：只有頻譜。溫度、距離、WAV 都是 `false`。
4. 其他（空資料夾、只有部分檔案）→ `format=unknown`。列出檔案並提供下載，不畫圖。

### 5.2 新格式中的「不完整」狀態

| 情況 | 判斷 | 顯示 |
|---|---|---|
| 正常 | `status=completed` | 完整功能 |
| 頻譜 CSV 未產生 | `status=raw_saved`，或 `spectrogram.status ∈ {error, skipped}` | 頻譜改由 WAV 建置預覽；提示可用 `uv run python app/chunked_spectrogram.py Sensor_Data/EXP_x` 補產生 |
| 中斷 | `status=running`，且非目前實驗 | 標示「未正常結束」。`end` 以各檔最後時間戳推估。讀取時容忍半行。 |
| WAV header 與檔案大小不一致 | 中斷時可能發生 | `n_frames = min(header, (filesize - 44) / (2*ch))` |
| 多聲道 | `audio.channels > 1` | 預覽依 §7 Q8 的決定處理（建議只取 ch0，並標註與 CSV 不一致的原因） |

### 5.3 不支援

- `legacy/spectrogram_EXP_20250529_125433.tdms` 與 `legacy/spectra_logs/` 不在 `Sensor_Data/` 內，第一版不支援。
- 若要支援，需要 `npTDMS` 依賴，列入 §7。

---

## 6. 分階段實作計畫與測試

每個階段都可以獨立合併，而且不改變即時監測的行為。

| 階段 | 內容 | 驗收 |
|---|---|---|
| **P0 準備** | 1. 把 `CanvasChart / LineChart / Spectrogram / LUT / readTheme` 從 monitor.js 抽成 `charts.js`，monitor.js 改為引用，行為不變。<br>2. 加入 pytest 開發依賴（`uv add --dev pytest`，§7 Q10）。<br>3. 寫測試資料產生器 `tests/fixtures/make_exp.py`。 | 即時頁手動對照截圖無差異；`uv run pytest` 綠燈 |
| **P1 本機列表、單次頁（輕量）** | 1. `app/history` blueprint、`LocalSource`（v2、legacy、unknown、中斷判斷）。<br>2. 端點：`experiments` 列表與詳情、`series`（溫度、距離，請求時直接解析）、`files` 下載。<br>3. `/history` 頁：列表、篩選、單次頁的摘要、溫度與距離曲線、檔案下載、錯誤紀錄。<br>4. 頻譜區：舊格式小檔直接顯示，新格式顯示「P2 提供」。 | 兩個舊格式資料夾與合成 v2 資料都能正確列出與顯示；路徑穿越測試全擋 |
| **P2 頻譜、波形預覽** | 1. `preview.py`（WAV 或 CSV → 金字塔）、`worker.py`（子行程、低優先、節流、§4.2 策略）。<br>2. 端點：`spectrogram`、`waveform`、`audio` 片段、`preview` 狀態。<br>3. 前端：總覽條、縮放與平移、tile LRU、播放選取範圍。 | 1 小時合成實驗：預覽建置時間有紀錄；任意縮放回應 < 100 ms；**§4.3 的監測中壓力測試通過** |
| **P3 比較、匯出** | 1. 比較頁（溫度、距離、頻帶能量、對齊方式、small multiples）。<br>2. zip 串流與範圍匯出。<br>3. （選做）錄製中實驗的歷史曲線、備註與標籤。 | 3 筆實驗疊圖對齊正確；匯出檔內容與原檔切片相符 |
| **P4 雲端來源** | 等 #2 定案後實作 `CloudSource`，並啟用資料來源切換。 | 同一筆實驗在本機與雲端兩個來源的摘要與曲線一致 |

### 6.1 測試方式

- **合成資料產生器**：產生小型、確定性的實驗資料夾，涵蓋以下情境：
  - v2 完整版（溫度、距離、WAV 加頻譜 CSV，以現有 `chunked_spectrogram` 產生，確保格式真實）。
  - v2 `raw_saved`、v2 中斷（json 為 running、CSV 最後一列是半行、WAV header 不一致）。
  - 多聲道 WAV、帶 BOM 的 CSV、cp950 編碼的 metadata.txt。
  - legacy_spec、空資料夾、非 EXP 目錄、`.cache`。
- **單元測試**：
  - 格式偵測與狀態對應。
  - 時間對齊（溫度 ISO 與距離 epoch 換算後與已知值相符）。
  - min/max 降採樣保留尖峰（插入單點尖峰後必須出現在輸出中）。
  - 金字塔切片與 pooling 的邊界（t0 與 t1 落在 block 中間、超出範圍、t0 ≥ t1）。
  - dB 下限處理；快取失效（改變來源檔 mtime 後會重建）；id 與檔名白名單。
- **API 測試**：用 Flask `test_client`，把 `DATA_DIR` 指向 tmp_path（LocalSource 接受根目錄參數）。驗證回應 schema、Range 下載、429 限流、錄製中實驗的行為（mock `live_status`）。
- **一致性測試**：WAV 重算的 dB 與 CSV 換算的 dB 抽樣比對，誤差 < 0.1 dB。
- **效能測試**（標記 `slow`，預設不跑）：產生 1 小時的合成 WAV 與 CSV，量測建置時間、預覽大小與 tile 回應時間。
- **整合測試**：執行 §4.3 的監測中壓力測試，macOS 與 Windows 各一次（Windows 搭配 `scripts/windows_selfcheck.py` 的環境）。
- **手動瀏覽器檢查**：深色與淺色模式、寬度 1200、900、480 的 RWD；開啟 300 MB 級實驗時觀察 DevTools 的記憶體與網路傳輸量。

---

## 7. 需要使用者決定的問題

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| Q1 | 第一版範圍 | (a) 只做本機，保留來源抽象層 (b) 等 #2 一起做 | **(a)**。抽象層已預留，#2 不會卡住 #3。 |
| Q2 | 頁面形式 | (a) 獨立 `/history` 頁 (b) monitor.html 內加分頁 | **(a)**，理由見 §1.2 |
| Q3 | 檢視功能的優先序 | 單次回放、下載、疊圖比較、範圍匯出、長期趨勢 | P1 與 P2 做回放加下載，P3 做比較與匯出；長期趨勢待 #2 |
| Q4 | 頻譜預覽的來源 | (a) 有 WAV 時從 WAV 重算，否則讀 CSV (b) 一律讀 CSV | **(a)**：較快，也涵蓋沒有 CSV 的中斷實驗。代價是要做一致性檢查。 |
| Q5 | 預覽快取的位置 | (a) `Sensor_Data/.cache/` (b) 各實驗資料夾內 `.preview/` (c) 專案外的使用者快取目錄 | **(a)**。若希望 #2 直接上傳預覽產物，(b) 也合理。 |
| Q6 | 監測中能否建置大型預覽 | A 照常（低優先） / B 只建小型、大型延後 / C 完全不建 | **B** |
| Q7 | 頻譜顯示的預設值 | 色階：自動百分位，或固定 vmin/vmax（便於跨實驗比較）；預設頻率上限 | 單次頁用自動，比較頁用統一固定色階；頻率上限預設為 Nyquist |
| Q8 | 多聲道 WAV | (a) 預覽只取 ch0 (b) 混成單聲道 (c) 每聲道各一張 | (a)，並另開 issue 修正 `chunked_spectrogram` 把交錯樣本當一維的問題（§0.4-1） |
| Q9 | 是否需要備註、標籤 | PEO 實驗條件（電壓、電解液、試片編號）目前不在 json 內，疊圖比較時難以辨識 | 建議加入，存在獨立的 `notes.json`，不改原始 json。需要確認要記哪些欄位。 |
| Q10 | 是否引入 pytest | repo 目前沒有測試框架 | 以 `uv add --dev pytest` 加入開發依賴 |
| Q11 | 中斷或 `raw_saved` 的實驗，是否在頁面提供「補產生頻譜 CSV」按鈕 | 會**寫入**實驗資料夾，打破「歷史頁唯讀」的原則 | 第一版不提供，只顯示 CLI 指令 |
| Q12 | 是否需要對區網開放瀏覽，例如從辦公室電腦看實驗室電腦 | 若需要，就得加簡單的存取控制 | 第一版維持 127.0.0.1，跨機瀏覽交給 #2 |
| Q13 | 舊 `.tdms` 與 `legacy/spectra_logs` 是否要納入 | 需要新增依賴 `npTDMS` | 不納入 |
| Q14 | §0.4 的既有問題（多聲道頻譜、%.6f 精度、Elapsed 零點不一致）要不要另開 issue | — | 建議另開，與 #3 分開處理，避免 #3 動到存檔流程 |

### 7.1 決策紀錄（2026-10-05）

| # | 決定 | 備註 |
|---|---|---|
| Q1 | ✅ 第一版只做本機 | 保留資料來源抽象層，之後接 #2 |
| Q2 | ✅ 獨立 `/history` 頁；主畫面加「歷史數據」按鈕跳轉 | |
| Q3 | ⏳ 未回覆 | 暫照建議：P1/P2 回放＋下載，P3 比較＋匯出 |
| Q4 | 💬 討論中 | 使用者還沒決定頻譜資料要用什麼格式、要提供哪些資訊。WAV 是原始資料；頻譜是從 WAV 算出來的衍生資料，不能反推回 WAV。見 §7.2 |
| Q5 | 💬 討論中，與 Q4 連動 | 如果頻譜改存成二進位格式，快取可能大幅簡化，甚至不需要 |
| Q6–Q8 | ⏳ 未回覆 | |
| Q9 | ✅ 要做 PEO 實驗條件標註與標籤 | **TODO：先問材料系學長平常怎麼記錄 PEO 實驗條件**（欄位、單位、命名），再定 `notes.json` 的 schema |
| Q10 | ⏳ 已解釋 pytest 是什麼，等回覆 | |
| Q11–Q13 | ⏳ 未回覆 | |
| Q14 | ✅ 既有問題都另開 issue | 使用者的開發流程：發現的問題都要記進 issue。草稿見 `docs/plans/issue-drafts.md`，發佈前先問使用者 |

### 7.2 Q4 討論要點：頻譜資料該存什麼

- **單向關係**：WAV → 頻譜（STFT，NFFT 256、hop 128）。頻譜 CSV 只存功率，沒有相位，所以**無法還原成 WAV**。反過來，有 WAV 就能隨時重算頻譜，1 小時錄音約需 6 秒（`chunked_spectrogram.py`）。
- **現況的成本**：頻譜 CSV（約 298 MB/小時）比 WAV 本身（約 159 MB/小時）還大，內容又是冗餘的：Power 是 DB_like 的平方，SNR 每個頻率只有一個常數。
- **可能方向**（待使用者決定）：
  1. 維持 CSV。好處是相容 Excel、Origin、學長的既有分析流程，但仍是文字格式，體積大、讀取慢。
  2. WAV 當唯一原始資料，頻譜在需要時才計算，或只存精簡的二進位檔（例如 dB 的 int16/float32 `.npy`，1 小時約 19–37 MB）。
  3. 兩者並存：存二進位檔供網頁與程式使用，需要 CSV 時再按「匯出」產生。
- **要先釐清的問題**：頻譜數據最終要回答什麼研究問題？例如 PEO 放電的特徵頻帶、能量隨時間的變化、事件計數。下游是誰在用、用什麼軟體？這會決定要存全頻譜還是只存特徵（例如頻帶能量曲線）。
