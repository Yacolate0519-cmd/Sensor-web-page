# Sensor-web-page

## 專案簡介
Sensor-web-page 是一套多感測器資料整合與即時監控平台，支援溫度感測器（RS485/Modbus）、音訊監測（麥克風）、AvaSpec 光譜儀、KEYENCE LK-G5000 雷射測距儀等多種裝置。主要介面是 Flask＋SSE 的網頁版監控服務（`main.py` → `app/web_monitor.py`），監測在伺服器端執行、資料一律以檔案形式存放；早期的 Tkinter 介面與單一感測器腳本保留在 `legacy/`。

---

## 主要功能
- **溫度感測**：RS485/Modbus 溫度計，COM 埠自動偵測。
- **音訊監測**：全程錄 WAV、即時頻譜圖（Spectrogram），停止後產生頻譜 CSV；預設選用 RØDE 麥克風。
- **光譜儀**：AvaSpec-ULS2048L 即時光譜與時間熱圖，原始 counts 存 CSV。
- **雷射測距儀**：KEYENCE LK-G5000 連續量測，以 µm 記錄。
- **網頁即時監控**：Flask SSE 推送，多個瀏覽器可同時觀看；關掉或重新整理網頁不影響監測。
- **實驗標籤**：開始前填寫金屬、試片尺寸、電解液等，寫入實驗 JSON。
- **狀態監控與 log**：每個感測器有狀態卡顯示失敗原因，開始前預檢，異常與恢復會跳通知並寫進實驗 log。
- **模擬模式**：沒有硬體也能跑完整流程。

---

## 目錄結構
```
Sensor-web-page/
├── main.py                    # 啟動入口：uv run python main.py（網頁版監控服務）
├── Sensor Monitor.app         # macOS 一鍵啟動
├── start_monitor.bat          # Windows 一鍵啟動
├── create_desktop_shortcut.bat# Windows：在桌面建立捷徑
├── README.md
├── Sensor_Data/               # 實驗資料（EXP_<時間>/，不進 git）
├── logs/                      # 伺服器 log（server_YYYYMMDD.log，不進 git）
├── app/                       # 網頁版監控服務
│   ├── web_monitor.py         #   Flask 伺服器與監測邏輯（main.py 呼叫它）
│   ├── templates/             #   前端 HTML（monitor.html；test.html、clock_test.html 給 legacy/main_web.py）
│   ├── static/                #   前端 JS / CSS（monitor.js、monitor.css）
│   ├── spectrometer_driver.py #   AvaSpec 驅動的延遲載入包裝
│   ├── chunked_spectrogram.py #   由 WAV 分塊產生頻譜 CSV
│   └── sim_devices.py         #   --simulate 模擬感測器
├── sensors/                   # 感測器驅動套件
│   ├── temp_py_package/       #   溫度感測器通訊協定與驅動
│   ├── signal_package/        #   音訊錄音、處理、儲存
│   ├── rangefinder/           #   KEYENCE 測距儀（LKIF2.dll 介接）
│   ├── spectrum_py_package/   #   AvaSpec 光譜儀
│   ├── truplasma_package/     #   TRUMPF TruPlasma 電源：RS232 協定、控制器、參數表、模擬器
│   └── utils/                 #   工具函式
├── drivers/                   # 廠商 DLL：LKIF2.dll、CmnLib.dll、KeyUsbDrv.dll、avaspecx64.dll
│   ├── keyence_usb/           #   測距儀 USB 驅動安裝檔（DPInst_64.exe）
│   └── avaspec_usb/           #   光譜儀 USB 驅動安裝檔（dpinst.exe）
├── legacy/                    # 舊版程式（Tkinter 介面、單一感測器腳本、check_db.py、environment.yml、spectra_logs/）
├── launchers/                 # 一鍵啟動器的實際邏輯與圖示（mac/、windows/）
├── scripts/                   # windows_selfcheck.py 環境自我檢查、test_thermometer.py、soak_test.py 長時間穩定測試
├── tests/                     # pytest 測試（電源驅動、穩定測試腳本）
├── docs/plans/                # 規劃與研究文件（電源協定整理、控制功能追蹤清單等）
├── truplasma_logger_backup/   # 實驗室電腦上電源唯讀紀錄器的備份
├── pyproject.toml             # 專案定義與相依套件（uv）
├── uv.lock                    # 相依套件鎖定檔（請一併提交）
├── .python-version            # 指定 Python 版本（3.12）
└── .gitignore / .gitattributes
```
所有指令都在**專案根目錄**執行。

---

## 環境建置（使用 uv）

本專案以 [uv](https://docs.astral.sh/uv/) 管理 Python 環境與相依套件，取代先前的 Conda 流程。

### 1. 安裝 uv

```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows（PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 或使用套件管理器
brew install uv        # macOS
pipx install uv        # 跨平台
```

### 2. 建立環境

於專案資料夾執行：

```sh
uv sync
```

`uv sync` 會自動完成三件事：

1. 依 `.python-version` 取得 Python 3.12（沒有的話自動下載，不需事先安裝）。
2. 在專案下建立 `.venv` 虛擬環境。
3. 依 `uv.lock` 安裝所有相依套件，確保每台機器版本一致。

### 3. 執行程式

不需要手動 `activate`，直接用 `uv run`：

```sh
uv run python main.py
```

若習慣先啟用虛擬環境，也可以：

```sh
source .venv/bin/activate     # macOS / Linux
.venv\Scripts\activate        # Windows
```

### 4. VS Code 設定

選擇專案內的 `.venv/bin/python`（Windows 為 `.venv\Scripts\python.exe`）作為 Python 解譯器。

### 常用 uv 指令

| 動作 | 指令 |
|---|---|
| 建立／同步環境 | `uv sync` |
| 新增套件 | `uv add <套件名>` |
| 移除套件 | `uv remove <套件名>` |
| 執行程式 | `uv run python <檔名>.py` |
| 更新套件版本鎖定 | `uv lock --upgrade` |
| 檢視已安裝套件 | `uv pip list` |

### 相依套件

`pyproject.toml` 中的相依套件是依實際程式碼 import 整理而成：

| 套件 | 用途 |
|---|---|
| `flask` | 網頁伺服器與 SSE 推送（`main.py` → `app/web_monitor.py`） |
| `numpy` | 訊號與光譜數值運算 |
| `matplotlib` | 即時圖表繪製（含 Tkinter backend） |
| `pyserial` | RS485/Modbus 序列埠通訊 |
| `pyaudio` | 麥克風錄音 |
| `psutil` | 系統資源狀態輸出（CPU／記憶體使用率） |
| `pyqt5`（選裝，`spectrometer` extra） | AvaSpec 光譜儀驅動 import 時需要（網頁版與 `legacy/spectrum_main.py` 皆是）。手動執行時用 `uv sync --extra spectrometer`；Windows 一鍵啟動器已自動帶上。PyQt5-Qt5 5.15.3 起沒有 Windows wheel，所以 Windows 鎖在 5.15.2、其他平台用新版（見 `pyproject.toml`） |

### 從 Conda 遷移

原本的 `environment.yml`（現在在 `legacy/`）是 Windows miniconda **base 環境的完整匯出**，混入大量本專案未使用的套件（conda 本身、pytorch、dlib、face-recognition、scikit-surprise、opencv 等），因此並未直接轉換，而是重新依程式碼實際 import 建立相依清單。該檔案僅保留作為歷史參考，確認不再需要後可自行刪除。

若原本已有 conda 環境，可直接移除：

```sh
conda env remove -n sensor
```

---

## 執行方式

主要使用方式是網頁版監控服務（`uv run python main.py`，或下面的一鍵啟動）。`legacy/` 裡的舊程式仍可執行，見本節最後的「舊版程式」。

### 一鍵啟動（不用開終端機）
點圖示就會自動啟動伺服器，並在服務就緒後用預設瀏覽器開啟網頁。若電腦沒有 `uv` 會詢問是否自動安裝；**第一次執行需要網路**（下載 Python 與套件）。

- **macOS**：雙擊專案根目錄的 `Sensor Monitor.app`（也可雙擊 `launchers/mac/start_monitor.command`）。想放桌面或 Dock：對 app 按右鍵 →「製作替身」，再把替身拖到桌面或 Dock（替身仍能找到專案資料夾；**不要**把 app 本體搬出專案資料夾）。第一次若被 Gatekeeper 擋下：對 app 按右鍵 →「打開」→ 再按「打開」。第一次可能跳出「想要控制終端機」的授權，請按允許。
- **Windows**：第一次先雙擊 `create_desktop_shortcut.bat`，會在桌面建立「Sensor Monitor」捷徑（含圖示），之後點桌面圖示即可。也可以直接雙擊 `start_monitor.bat`。若 SmartScreen 擋下，點「其他資訊」→「仍要執行」。
- 兩個平台的行為一致：5002 已有監測服務在跑就只開瀏覽器、不會重複啟動；5002 被別的程式占用會自動改用 5003…；要固定其他埠可設環境變數 `SENSOR_MONITOR_PORT`。
- 伺服器跑在彈出的終端機／命令提示字元視窗裡。**關閉該視窗即停止服務；監測中請先在網頁按「停止」。** 萬一直接關閉視窗（macOS 的 SIGHUP、Windows 的關閉主控台事件），程式會先停止監測並存檔，但仍建議先按停止。
- 圖示由 `uv run --with pillow python launchers/make_icons.py` 產生（pillow 只在這個一次性指令使用，不在專案相依套件裡）。

### 網頁版多感測器整合（主要使用方式，取代 Tkinter 介面）
```sh
uv run python main.py              # 預設 http://127.0.0.1:5002
uv run python main.py --port 5003  # 5002 被占用時改用其他埠
uv run python main.py --help       # 所有參數
```
（`main.py` 只是入口，實際程式在 `app/web_monitor.py`；`uv run python app/web_monitor.py` 效果相同。）

| 參數／環境變數 | 預設 | 說明 |
|---|---|---|
| `--host` | `127.0.0.1` | 監聽位址 |
| `--port` | `5002` | 監聽埠 |
| `--simulate` | （空） | 以逗號分隔要模擬的感測器：`temp`、`distance`、`audio`、`spectrometer`、`psu`、`all`，見「模擬模式」 |
| `--simulate-faults` | 關 | 搭配 `--simulate`，定期模擬斷線／恢復 |
| `SENSOR_AUDIO_PREFER` | `RØDE,RODE` | 預設麥克風的名稱關鍵字（逗號分隔、不分大小寫） |
| `SENSOR_MONITOR_PORT` | `5002` | **只有一鍵啟動器讀取**；`main.py` 本身只看 `--port` |
| `SENSOR_MONITOR_NO_BROWSER=1` | — | 一鍵啟動器不自動開瀏覽器 |

一鍵啟動器（`start_monitor.bat`、`launchers/`）會把多餘的參數原樣轉給 `main.py`。
- **一鍵啟動**：見上一節（Windows 的 `start_monitor.bat`、macOS 的 `Sensor Monitor.app`）。
- 瀏覽器開啟 [http://127.0.0.1:5002](http://127.0.0.1:5002)。功能與 `legacy/main_csv.py` 相同（溫度、音訊頻譜圖、測距儀，並即時繪製溫度與距離曲線），介面改為網頁；**一律手動按「停止監測」才結束**。
- 監測在伺服器端進行，關掉或重新整理網頁不會中斷；重新開啟頁面會自動復原目前畫面。
- 在終端機按 `Ctrl+C` 關閉伺服器：會先停止監測、關檔、產生頻譜 CSV 並釋放硬體。
- 圖表不依賴 CDN，實驗室電腦離線也能使用。
- 第一次在新電腦（特別是 Windows 筆電）使用前，先裝硬體驅動（見下方「首次安裝硬體驅動」），再跑自我檢查：`uv run python scripts/windows_selfcheck.py`。

#### 首次安裝硬體驅動（Windows，每台電腦只做一次）
`drivers/` 裡的 DLL 是程式用的通訊函式庫，**不會**自動安裝 USB 驅動；USB 驅動要另外用系統管理員身分裝一次。裝好後寫進 Windows，**重開機、關機都不會消失**，之後直接啟動系統即可。只有重灌 Windows、換電腦，或在裝置管理員手動解除安裝時才需要重裝。

| 裝置 | 安裝方式 | 裝好後裝置管理員會看到 |
|---|---|---|
| 測距儀 KEYENCE LK-G5000 | 先拔 USB → 右鍵 `drivers/keyence_usb/DPInst_64.exe` →「以系統管理員身分執行」→ 再接 USB | `WDF USB for KEYENCE LK-G5000` |
| 光譜儀 AvaSpec | 先拔 USB → 右鍵 `drivers/avaspec_usb/dpinst.exe` →「以系統管理員身分執行」→ 再接 USB | `AvantesSpectrometers` 類別下的裝置 |

若 `avaspecx64.dll` 載入時報缺 `VCRUNTIME140.dll`／`MSVCP140.dll`，安裝 Microsoft Visual C++ 2015–2022 x64 執行階段（`vc_redist.x64.exe`）。各資料夾內的 `README.md` 有驅動版本與裝置 ID。

#### 網頁介面與操作流程
- **頂部**：連線狀態（連線中／已連線／重新連線中）；模擬模式時多一個「模擬模式」標籤。
- **左側設定**（監測中或儲存中會鎖住）：
  - 「裝置」：溫度計 COM 埠（留空＝自動偵測，旁邊有重新整理鈕列出可用埠）、音訊輸入裝置。
  - 可收合的參數群組：
    - 音訊參數：採樣率 22050 Hz、更新間隔 0.1 秒、頻譜圖顯示長度 100 秒。
    - 測距儀：反射模式（0 漫反射／1 鏡面反射）、測距間隔 0.1 秒。
    - 光譜儀：量測間隔 0.5 秒、積分時間 50 ms。
  - 「輸入實驗標籤」開啟標籤視窗、「停止監測」（存檔期間顯示「儲存中…」）。監測沒有時間上限，一律手動停止。
- **感測器狀態卡**（溫度／音訊／測距儀／光譜儀）：綠色＝正常、紅色＝警告或錯誤、橘色＝其他（待機、初始化、未啟用等）。卡片顯示原因分類（驅動／韌體問題、找不到裝置、Port 衝突、數據收不進來、接線／訊號異常、超出量程、未啟用、其他錯誤）、可行動的說明、「最後正常 N 秒前」與連續失敗次數。原始例外訊息不顯示在畫面上，請看實驗 log。
- **圖表**：Spectrogram（聲音頻譜圖）與 Optical Spectrum（光譜，熱圖＋即時光譜）左右並排，下面是 Temperature（溫度）、Distance（距離）、PSU Voltage（電源電壓）、PSU Current (peak)（電源電流峰值）；標題一律「英文（中文）」。以 canvas 繪製、跟隨系統深淺色。兩張電源圖只在實驗錄製中且電源已連線時才有資料。
- **開始流程**：填好實驗標籤並送出後進行預檢（偵測溫度計 COM 埠、選麥克風、試開測距儀與光譜儀、檢查參數與標籤、檢查磁碟空間）。預檢結果會先顯示在狀態卡上；有警告會跳確認視窗（「繼續監測」／「取消」），有錯誤則無法開始。至少要有一個感測器可用。找不到的感測器會被停用，不影響其他感測器。
- **通知**：右側跳出 info／success／warning／error 通知（error 需手動關閉）。會通知的情況：感測器連續失敗 10 次判定斷線、恢復連線、音訊連續 3 秒全為 0、寫檔失敗、頻譜 CSV 產生失敗（附補產生指令）、監測完成摘要。所有通知同時寫進 log。
- **麥克風預設**：音訊裝置清單中第一個名稱含 RØDE／RODE 的輸入裝置，找不到則用第一個輸入裝置（可用 `SENSOR_AUDIO_PREFER` 修改）。Windows 上被編碼弄亂的裝置名稱（如 `RÃ˜DE`）會自動修正顯示。
- **重新整理／多視窗**：頁面載入或斷線重連時會向伺服器取回目前狀態、曲線與頻譜圖，畫面自動復原；實驗標籤不會保留，每次開始都要重新填寫。

#### 實驗標籤
在「輸入實驗標籤」視窗填寫，前端與伺服器都會檢查，存進 `experiment_<id>.json` 的 `experiment_labels`：

| 欄位 | JSON key | 必填 | 說明 |
|---|---|---|---|
| 金屬種類 | `metal` | ✅ | 自由文字，例：AZ31 鎂合金 |
| 試片 | `specimen` | ✅ | 正方形：`{"shape": "square", "length_mm", "width_mm", "thickness_mm"}`；圓形：`{"shape": "circle", "area_mm2", "thickness_mm"}`。尺寸皆須為 > 0 的數字 |
| 電解液配方與濃度 | `electrolyte` | ✅ | 自由文字 |
| 添加物 | `additive` | 選填 | 空白時存 `null` |
| 電源模式 | `power_mode` | — | 固定為「雙極脈衝」，不在畫面上顯示 |

#### Log
- 伺服器 log：`logs/server_YYYYMMDD.log`；每場實驗另有 `experiment_<id>.log`。
- 格式 `YYYY-mm-dd HH:MM:SS.mmm [LEVEL] 來源 訊息`。只在狀態改變時寫一行；持續失敗時每 60 秒寫一行摘要，恢復時記錄中斷時長與次數。

#### HTTP API（前端使用，供除錯參考）
| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | `/` | 監控頁面 |
| GET | `/api/devices?kind=all\|com\|audio` | 列出 COM 埠與音訊裝置 |
| POST | `/api/preflight` | 預檢 |
| POST | `/api/start` | 開始監測（有警告時需帶 `confirm`） |
| POST | `/api/stop` | 停止監測 |
| GET | `/api/state` | 目前完整狀態、曲線、近期通知 |
| GET | `/api/spectrogram` | 頻譜圖資料（int16 二進位） |
| GET | `/api/stream` | SSE 即時事件流 |
| GET | `/api/psu/state` | 電源狀態、量測值、參數表 |
| POST | `/api/psu/connect`、`/api/psu/disconnect` | 電源連線／斷線（`{"port": "COM8"}`） |
| POST | `/api/psu/recipe` | 驗證並套用脈衝模式、頻率、佔空比與共用 U／I／P 上限（只准本機、輸出關閉時） |
| POST | `/api/psu/setpoints`、`/api/psu/output` | 設定 U／I／P、開關輸出（只准本機） |
| POST | `/api/psu/estop` | 緊急關閉（任何連線者都可） |
| POST | `/api/psu/reset-alarms`、`/api/psu/reset-counters` | 重設警報、電弧計數（只准本機） |
| POST | `/api/psu/params/read`、`/api/psu/param` | 讀參數、寫單一參數（只准本機、輸出關閉時） |

#### 存檔內容（整場實驗完整保存）
按下「開始監測」就建立 `Sensor_Data/EXP_YYYYmmdd_HHMMSS/`，資料**邊錄邊寫**（每筆都 flush，當機或斷電也只會少最後一瞬間）：

| 檔案 | 內容 | 預設設定下的大小 |
|---|---|---|
| `audio_<id>.wav` | 全程原始音訊（16-bit，實際取樣率與聲道數） | 約 160 MB／小時（22050 Hz 單聲道；雙聲道加倍） |
| `temperature_<id>.csv` | `Timestamp(ISO), Elapsed(s), Temperature(C), Status`；讀取失敗的列 Temperature 留空、Status 記錄原因 | 約 0.2 MB／小時 |
| `distance_<id>.csv` | `Timestamp(ISO), Elapsed(s), Absolute(µm), Relative(µm), Status`（以 µm 記錄、小數 3 位；Absolute = 50 mm 基準 + Relative）；Timestamp 與 Elapsed 的定義和其他 CSV 相同（Elapsed 從實驗開始起算）；測距儀初始化約 2.7 秒，這段沒有列。2026-10-30 以前的檔案 Timestamp 是 Unix 秒、Elapsed 從測距儀第一筆讀值起算（比實驗開始晚約 2.66 秒），對齊請用 Timestamp；每次讀取都記一列，Status 為儀器的 FloatResult（`VALID`／`+RANGEOVER`／`-RANGEOVER`／`WAITING`／`ALARM`／`INVALID`）或 `ERROR: <訊息>`，非 VALID 的列距離留空 | 約 1.5 MB／小時（測距間隔 0.1 秒） |
| `spectrogram_<id>.csv` + `_metadata.txt` | 停止後由完整 WAV 產生，格式與原本 `save_spectrogram_to_csv` 完全相同 | 約 270–300 MB／小時 |
| `experiment_<id>.json` | 參數、實驗標籤（`experiment_labels`，見上節）、啟用的感測器、裝置、開始／結束時間、停止原因（手動停止、Ctrl+C、關閉終端機／主控台視窗、錯誤等）、各檔案筆數、頻譜 CSV 狀態與錯誤紀錄 | 數 KB |
| `truplasma_<id>.csv` | 電源量測與狀態，每 0.5 秒一列（只有錄製中且電源已連線才會有，欄位見「電源供應器」一節） | 約 1–2 MB／小時（模擬、輸出關閉時實測 1.1 MB） |
| `experiment_<id>.log` | 這次實驗的完整 log（開始參數、各感測器狀態變化與失敗原因、恢復、通知、停止與存檔摘要），每行即時寫入；伺服器層級的 log 另存在 `logs/server_YYYYMMDD.log` | 數十 KB |

- 合計約 **460 MB／小時**，8 小時約 3.7 GB。預檢時若存檔磁碟剩餘空間 < 2 GB 會警告；錄製中寫檔失敗（例如磁碟滿）會跳通知並記錄在 json，監測不會靜默中斷。
- 只有啟用的感測器才會建檔（例如自動偵測找不到溫度計就不會有 temperature CSV）。
- 頻譜 CSV 以分塊方式計算，記憶體用量約 150 MB、與錄音長度無關；1 小時錄音約 6 秒產生完。若中途失敗，可事後補產生：`uv run python app/chunked_spectrogram.py Sensor_Data/EXP_YYYYmmdd_HHMMSS`。
- 「頻譜圖顯示長度」只影響畫面（上限 600 秒），不影響存檔。

#### 模擬模式（沒有硬體時測試完整流程）
```sh
uv run python main.py --simulate temp,distance            # 溫度、測距儀用模擬資料，音訊仍用麥克風
uv run python main.py --simulate spectrometer             # 只模擬光譜儀（別名 spectrum / spec / avaspec）
uv run python main.py --simulate all --simulate-faults    # 全部模擬，並定期模擬斷線／恢復
```
模擬資料會清楚標示：頁面頂部顯示「模擬模式」、對應卡片標「模擬」、`experiment_<id>.json` 記錄 `"simulated"`，實驗資料夾另有 `SIMULATED.txt`。**不要把模擬資料夾當成真實量測使用。**

#### 光譜儀（AvaSpec-ULS2048L）
- 網頁新增 **Optical Spectrum** 卡片：上方熱圖（x=時間、y=波長、顏色=counts，色階自動取近期 1%–99% 百分位），下方為最新一筆即時光譜（標示前 3 高峰；≥65000 counts 顯示飽和警告）。左側「光譜儀」區塊可設量測間隔（預設 0.5 秒）與積分時間（預設 50 ms）。
- 存檔 `spectrometer_<id>.csv`：Timestamp 為 ISO 格式；檔頭 `Timestamp,Elapsed(s),Status,` 後接 2048 個波長（nm），之後每筆一列原始 counts（未扣暗光）；失敗列強度留空、Status 為原因。約 100 MB／小時（預設設定）。波長範圍、積分時間等寫在 `experiment_<id>.json` 的 `spectrometer` 欄位。
- 沒有硬體：用 `--simulate spectrometer`（或 `all`）。真實模式需要：Windows 64 位元、`drivers/avaspecx64.dll`（AvaSpec 官方驅動）、PyQt5（`uv sync --extra spectrometer`，驅動 import 時需要）、USB 接上光譜儀。缺任何一項時預檢會停用光譜儀並在狀態面板顯示原因，不影響其他感測器。

#### 電源供應器（TRUMPF TruPlasma Bipolar 4010）
網頁的「電源 TruPlasma」面板顯示正負向平均值，並可設定脈衝配方與共用電壓／電流上限；進階區提供功率上限及完整參數。電壓、電流、功率會記進實驗資料夾。電源**獨立於實驗的開始／停止**：只有按「連線」才會連，伺服器啟動時不會自動連線；停止實驗不會動輸出。

**使用方式**
1. 在接電源的電腦（實驗室收資料電腦，電源接在 COM8）上開網頁，COM 欄位填 `COM8`（預設）按「連線」；沒有電源時用 `--simulate psu`（或 `all`）練習。
2. 輸入模式、頻率、佔空比及 U／I 上限（P 上限在進階區），確認並套用設定，再按「開始輸出」。輸入框改了卻沒套用時不會開輸出。
3. 「關閉輸出」正常關機；「緊急關閉」立刻要求關閉輸出（連線中隨時可按）。
4. 「電源參數」展開後按「讀取參數」，每列有「寫入」，會先確認「舊值 → 新值」。

**安全行為**
- 程式每 0.5 秒送一次心跳；程式或連線中斷超過 4–5 秒，電源自己會報 61613 並關閉輸出。
- 斷線、伺服器關閉（含 Ctrl+C／關視窗）會先關輸出再放掉 COM 埠；警報、Interlock、連續 3 次沒回應都會自動關輸出，且不會自動重開，必須人工重新開啟。
- 開啟順序：Ready、無警報、無 Interlock → 接通主繼電器 → 等 3 秒以上 → Power ON。
- 控制只准伺服器本機（`127.0.0.1`／`::1`）；從其他電腦開網頁只能看，面板會停用所有控制鈕，但「緊急關閉」仍可按。
- 除了 U／I／P，其他參數會寫入電源的 EEPROM，只能在輸出關閉時寫，寫完會讀回比對；通訊相關設定（介面、鮑率、位址、逾時等）只顯示、不開放寫入。
- 電源使用中，溫度計 COM 自動偵測會略過電源的埠。

**CSV**：實驗錄製中且電源已連線時，資料夾內會有 `truplasma_<id>.csv`（UTF-8 BOM，每 0.5 秒一列；實驗中途才連線則從連線那刻開始）。欄位：`Timestamp, Elapsed(s), Phase, Uset_V, Iset_A, Pset_kW, Uact_V, Iact_peak_A, Pact_kW`，接著是 `Uavg_A_V, Iavg_A_A, Pavg_A_kW, Uavg_B_V, Iavg_B_A, Pavg_B_kW, Freq_Hz, Duty_pct, T_OutputCores_C`，最後是 `RelaysOn, PowerOn, Interlock, AlarmActive, WarningActive, AlarmCode, Mode, Regulation, ArcRate_per_s, ArcCnt_Imax, ArcCnt_UxI, ArcCnt_dU, ArcCnt_Usag, ArcCnt_ArcBurst, Bits0, Bits1, Bits2, Bits3`（布林為 0／1；讀不到的量測值留空）。網頁另有 PSU Voltage、PSU Current (peak) 兩張曲線。

**注意**
- COM8 同時只能一個程式使用：PVDPower／LabVIEW 開著時本程式連不上，反過來也一樣，請擇一。
- **尚未用真實電源測試**：目前只在模擬器（`--simulate psu`）驗證；第一次接真機請先用低設定值（例如 50 V／1 A／0.1 kW）並有人在旁邊。

#### 長時間穩定測試（scripts/soak_test.py）
確認伺服器長時間執行時記憶體不會一直漲、執行緒不會一直增加、電源通訊不會偶發失敗。腳本**只讀取狀態**，不會開輸出、不會改設定值或參數；偵測到有人開了電源輸出會立刻中止測試（不會替你關）。

```sh
# 先另開終端機啟動伺服器（正式或模擬模式皆可），再執行：
uv run python scripts/soak_test.py --duration 60m --connect-psu --with-recording
```
- `--duration`：測試長度（`3h`、`90m`、`600s`）；記憶體判準至少要 30 分鐘才會判斷。
- `--connect-psu`：電源未連線時由腳本連線（`--psu-port`，預設 COM8），結束時只斷開自己連的。電源沒通電時會連不上並中止，這時不要加這個參數。
- `--with-recording`：同時錄一場實驗（標籤為 soak test），結束時停止。至少要有一個感測器可用，否則伺服器預檢會拒絕。一小時約 0.5 GB。
- `--interval`（預設 10 秒）取樣伺服器記憶體、CPU、執行緒、handle 與感測器狀態；電源狀態另外每 0.5 秒檢查一次。`--pid` 可指定伺服器程序（預設自動尋找，會連同 `uv` 底下的子程序一起量）。
- 輸出到 `logs/soak_<時間>/`（或 `--out`）：`samples.csv`、`events.csv`、`summary.md`、`summary.json`。判定為「通過」「不通過」「未判定」（時間或資料不足），結束碼分別是 0／1／2。
- 判準（可在腳本開頭調整）：記憶體成長 < 5 MB／小時、執行緒不持續成長、電源斷線 0 次、最長未通訊 < 2 秒、HTTP 失敗 0 次。

#### 常見問題排除
- **溫度計 COM 埠（預設自動偵測）**：COM 欄位留空（或填 `auto`），按開始時會逐一探測 USB 轉序列埠，回應的站號、功能碼與 CRC 都正確的那個就是溫度計，log 會記「自動偵測：溫度計在 COMx」。只探測 USB 轉接器，不碰主機板內建 COM。COM 號碼會隨 USB 孔改變（尤其沒有序號的 PL2303），所以不必記住號碼；也可以在欄位手動輸入埠名。
- **找不到 COM 埠**：確認 USB 轉 RS485 轉換器已接上並安裝驅動（常見晶片 CH340、FTDI、CP210x、PL2303），在「裝置管理員 → 連接埠 (COM 和 LPT)」確認出現 `COMx`；也可以在欄位直接輸入埠名。
- **麥克風錄到全為 0／沒有音訊設備**：Windows「設定 → 隱私權與安全性 → 麥克風」開啟「麥克風存取」與「讓桌面應用程式存取麥克風」；確認輸入裝置沒被停用，並關閉占用麥克風的程式（Teams、Zoom）。macOS 則在「系統設定 → 隱私權與安全性 → 麥克風」允許終端機。
- **LKIF2.dll 載入失敗／測距儀初始化失敗**：需要 64 位元 Python 搭配 64 位元 LKIF2.dll（專案附的版本），`CmnLib.dll`、`KeyUsbDrv.dll` 要一起放在 `drivers/`；安裝 KEYENCE 的 USB 驅動並確認裝置管理員中有控制器。macOS 無法使用測距儀。
- **port 5002 被占用**：手動執行時用 `uv run python main.py --port 5003`；一鍵啟動器會自動改用下一個可用的埠，或用環境變數 `SENSOR_MONITOR_PORT` 指定起始埠。

### 舊版程式（legacy/）
從專案根目錄執行（腳本開頭已設定好 `sensors/`、`app/` 的匯入路徑）：
```sh
uv run python legacy/main_csv.py          # Tkinter 多感測器整合（資料同樣存到根目錄 Sensor_Data/）
uv run python legacy/temperture_main.py   # 單一溫度感測器
uv run python legacy/sound_main.py        # 單一音訊（見「已知問題」）
uv sync --extra spectrometer && uv run python legacy/spectrum_main.py   # 單一光譜儀，需選裝 PyQt5（輸出到 legacy/spectra_logs/）
uv run python legacy/rangefinder_main.py  # 單一測距儀
uv run python legacy/main_web.py          # 早期 Flask SSE 範例：http://localhost:5001/chart_test
uv run --with pymongo python legacy/check_db.py   # 列出 MongoDB 最新一筆紀錄（pymongo 已不在專案相依套件中）
```

---

## 子系統與模組說明

以下套件都在 `sensors/`。

### temp_py_package
- 溫度感測器 Modbus RTU 通訊：CRC（`crc.py`）、封包組裝與解析（`frame.py`、`parser.py`）、COM 埠自動偵測（`port_finder.py`）、讀取與錯誤診斷（`reader.py`）。

### signal_package
- 音訊錄音（`audio_recorder.py`）、訊號處理（`signal_processor.py`）、頻譜儲存（`audio_save.py`）、單機測試（`sound_main.py`）。

### spectrum_py_package
- 光譜儀驅動、校正、資料解析、即時繪圖與儲存。

### rangefinder
- KEYENCE LK-G5000 測距儀 DLL 介接、參數設定、資料讀取、錯誤處理。

### utils
- 目前只有一個測試用的 `test.py`，沒有實際功能。

### app/templates、app/static
- 網頁版前端（`monitor.html`、`monitor.js`、`monitor.css`）；`temp_calibration.html` 是溫度計校正頁（`/temp-calibration`，只讀溫度計、記錄校正點並匯出 CSV，與監測互斥）；`test.html`、`clock_test.html` 供 `legacy/main_web.py` 使用。

---

## 平台需求

| 功能 | Windows | macOS / Linux |
|---|---|---|
| 溫度感測、音訊監測、網頁監控 | ✅ | ✅ |
| 雷射測距儀（`rangefinder`） | ✅ | ❌ 使用 `ctypes.WinDLL` 載入 `LKIF2.dll`，僅限 Windows |
| 光譜儀（`spectrum_py_package`） | ✅ 需 `drivers/avaspecx64.dll` | ⚠️ 需自行安裝 AvaSpec 原生函式庫（macOS：`/usr/local/lib/libavs.0.dylib`；Linux：`/usr/local/lib/libavs.so.0`），否則匯入即失敗 |

Python 套件本身在三大平台皆可安裝，上述限制來自硬體廠商提供的原生驅動。

---

## 已知問題

- 溫度計 COM 自動偵測只有在電源**已連線**時才會略過電源的埠（COM8）；電源沒連線時，它會對 COM8 送溫度計的 Modbus 探測命令。電源應該會當成格式錯誤丟掉，但不該發生，待修正為一律略過電源的埠。

- `legacy/sound_main.py` 與 `legacy/main_csv_test.py` 匯入了 `signal_package.save_spectrogram_to_tdms`，但 `signal_package` 只提供 `save_spectrogram_to_csv`，執行時會直接 `ImportError`。此問題與環境無關（改用 uv 之前即存在），需補上 TDMS 儲存實作（另需安裝 `npTDMS`）或改用 CSV 版本。

---

## 常見問題

- **Q: `uv sync` 之後仍找不到套件？**
  - 請用 `uv run python ...` 執行，或確認 VS Code 選到的是專案內的 `.venv`，而不是系統或舊的 conda Python。
- **Q: 執行時找不到 DLL/Lib？**
  - 請確認驅動檔案（`LKIF2.dll`、`CmnLib.dll`、`KeyUsbDrv.dll`、`avaspecx64.dll`）都在專案的 `drivers/` 資料夾。
- **Q: 在 macOS 匯入 `spectrum_py_package` 出現 `dlopen ... libavs.0.dylib (no such file)`？**
  - AvaSpec 的原生函式庫未安裝。此模組需要廠商提供的驅動，只在有實體光譜儀的機器上使用。
- **Q: 音訊裝置無法偵測？**
  - 請確認麥克風已正確連接，並安裝對應驅動。macOS 另需在「系統設定 → 隱私權與安全性 → 麥克風」授權終端機或 VS Code。
- **Q: Flask 頁面無法顯示即時資料？**
  - 請確認感測器已連接，且後端程式無錯誤。

---

## 版權與授權
本專案採用 MIT License，歡迎自由使用、修改與發佈。

---

## 聯絡方式
如有問題或建議，請於 GitHub 提出 issue 或聯絡專案
