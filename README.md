# Sensor-web-page

## 專案簡介
Sensor-web-page 是一套多感測器資料整合與即時監控平台，支援溫度感測器（RS485/Modbus）、音訊監測（麥克風）、AvaSpec 光譜儀、KEYENCE LK-G5000 雷射測距儀等多種裝置。提供 Tkinter 圖形化操作介面與 Flask SSE 網頁即時監控，並可將量測資料寫入 MongoDB，適用於實驗室、工業現場等多感測應用。

---

## 主要功能
- **溫度感測**：即時讀取 RS485/Modbus 溫度感測器數據。
- **音訊監測**：支援錄音、即時波形/頻譜/聲音熱圖顯示，並可儲存 CSV。
- **光譜儀資料收集**：AvaSpec 光譜儀即時量測、繪圖與自動儲存。
- **雷射測距儀**：KEYENCE LK-G5000 單次/連續量測、資料儲存與顯示。
- **圖形化操作介面**：Tkinter 整合多感測器監控、參數設定、即時圖表。
- **網頁即時監控**：Flask SSE 多圖表即時資料推送，支援多用戶瀏覽。
- **資料庫記錄**：透過 `db_logger.py` 將量測資料與頻譜寫入 MongoDB。

---

## 目錄結構
```
Sensor-web-page/
├── main_csv.py                # Tkinter 多感測器整合主程式
├── main_csv_test.py           # 測試用 GUI 主程式
├── main_web.py                # Flask SSE 網頁即時監控主程式
├── temperture_main.py         # 單純溫度感測器讀取腳本
├── sound_main.py              # 單純音訊監測腳本
├── spectrum_main.py           # 單純光譜儀資料收集腳本
├── rangefinder_main.py        # 單純雷射測距儀腳本
├── db_logger.py               # MongoDB 寫入模組（DatabaseLogger）
├── check_db.py                # 獨立工具：查詢資料庫最新一筆紀錄
├── rangefinder/               # 測距儀驅動與工具
├── temp_py_package/           # 溫度感測器通訊協定與驅動
├── signal_package/            # 音訊錄音、處理、儲存模組
├── spectrum_py_package/       # 光譜儀通訊與資料處理
├── utils/                     # 工具函式
├── templates/                 # Flask 前端 HTML 模板
├── spectra_logs/              # 光譜資料儲存資料夾
├── pyproject.toml             # 專案定義與相依套件（uv）
├── uv.lock                    # 相依套件鎖定檔（請一併提交）
├── .python-version            # 指定 Python 版本（3.12）
├── environment.yml            # 舊版 Conda 環境檔（已不再維護，僅保留參考）
├── .gitignore
└── README.md
```

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
uv run python main_csv.py
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
| `flask` | 網頁伺服器與 SSE 推送（`main_web.py`） |
| `numpy` | 訊號與光譜數值運算 |
| `matplotlib` | 即時圖表繪製（含 Tkinter backend） |
| `pymongo` | MongoDB 寫入與查詢（`bson` 隨此套件附帶） |
| `pyserial` | RS485/Modbus 序列埠通訊 |
| `pyaudio` | 麥克風錄音 |
| `psutil` | 系統資源狀態輸出（CPU／記憶體使用率） |
| `pyqt5` | AvaSpec 光譜儀 SDK 的事件迴圈需求 |

> `bson` 不要另外從 PyPI 安裝，PyPI 上的獨立 `bson` 套件會與 `pymongo` 內建的版本衝突。

### 從 Conda 遷移

原本的 `environment.yml` 是 Windows miniconda **base 環境的完整匯出**，混入大量本專案未使用的套件（conda 本身、pytorch、dlib、face-recognition、scikit-surprise、opencv 等），因此並未直接轉換，而是重新依程式碼實際 import 建立相依清單。該檔案僅保留作為歷史參考，確認不再需要後可自行刪除。

若原本已有 conda 環境，可直接移除：

```sh
conda env remove -n sensor
```

---

## 執行方式

### 1. 圖形化多感測器整合（推薦）
```sh
uv run python main_csv.py
```
- 啟動 Tkinter 視窗，整合溫度、音訊、測距儀即時監控與圖表。

### 2. 單一感測器測試
- 溫度感測器：
  ```sh
  uv run python temperture_main.py
  ```
- 音訊監測：
  ```sh
  uv run python sound_main.py
  ```
- 光譜儀：
  ```sh
  uv run python spectrum_main.py
  ```
- 測距儀：
  ```sh
  uv run python rangefinder_main.py
  ```

### 3. 網頁即時監控
```sh
uv run python main_web.py
```
- 啟動 Flask 伺服器，瀏覽器開啟 [http://localhost:5000/chart_test](http://localhost:5000/chart_test) 查看即時圖表。

### 一鍵啟動（不用開終端機）
點圖示就會自動啟動伺服器，並在服務就緒後用預設瀏覽器開啟網頁。若電腦沒有 `uv` 會詢問是否自動安裝；**第一次執行需要網路**（下載 Python 與套件）。

- **macOS**：雙擊專案根目錄的 `Sensor Monitor.app`（也可雙擊 `launchers/mac/start_monitor.command`）。想放桌面或 Dock：對 app 按右鍵 →「製作替身」，再把替身拖到桌面或 Dock（替身仍能找到專案資料夾；**不要**把 app 本體搬出專案資料夾）。第一次若被 Gatekeeper 擋下：對 app 按右鍵 →「打開」→ 再按「打開」。第一次可能跳出「想要控制終端機」的授權，請按允許。
- **Windows**：第一次先雙擊 `create_desktop_shortcut.bat`，會在桌面建立「Sensor Monitor」捷徑（含圖示），之後點桌面圖示即可。也可以直接雙擊 `start_monitor.bat`。若 SmartScreen 擋下，點「其他資訊」→「仍要執行」。
- 兩個平台的行為一致：5002 已有監測服務在跑就只開瀏覽器、不會重複啟動；5002 被別的程式占用會自動改用 5003…；要固定其他埠可設環境變數 `SENSOR_MONITOR_PORT`。
- 伺服器跑在彈出的終端機／命令提示字元視窗裡。**關閉該視窗即停止服務；監測中請先在網頁按「停止」。** 萬一直接關閉視窗（macOS 的 SIGHUP、Windows 的關閉主控台事件），程式會先停止監測並存檔，但仍建議先按停止。
- 圖示由 `uv run --with pillow python launchers/make_icons.py` 產生（pillow 只在這個一次性指令使用，不在專案相依套件裡）。

### 網頁版多感測器整合（取代 Tkinter 介面）
```sh
uv run python web_monitor.py              # 預設 http://127.0.0.1:5002
uv run python web_monitor.py --port 5003  # 5002 被占用時改用其他埠
```
- **一鍵啟動**：見上一節（Windows 的 `start_monitor.bat`、macOS 的 `Sensor Monitor.app`）。
- 瀏覽器開啟 [http://127.0.0.1:5002](http://127.0.0.1:5002)。功能與 `main_csv.py` 相同（溫度、音訊波形／頻譜／頻譜圖、測距儀），介面改為網頁；**一律手動按「停止監測」才結束**。
- 監測在伺服器端進行，關掉或重新整理網頁不會中斷；重新開啟頁面會自動復原目前畫面。
- 在終端機按 `Ctrl+C` 關閉伺服器：會先停止監測、關檔、產生頻譜 CSV 並釋放硬體。
- 圖表不依賴 CDN，實驗室電腦離線也能使用。
- 第一次在新電腦（特別是 Windows 筆電）使用前，先跑自我檢查：`uv run python scripts/windows_selfcheck.py`（加 `--mongo` 會一併檢查 MongoDB）。

#### 存檔內容（整場實驗完整保存）
按下「開始監測」就建立 `Sensor_Data/EXP_YYYYmmdd_HHMMSS/`，資料**邊錄邊寫**（每筆都 flush，當機或斷電也只會少最後一瞬間）：

| 檔案 | 內容 | 預設設定下的大小 |
|---|---|---|
| `audio_<id>.wav` | 全程原始音訊（16-bit，實際取樣率與聲道數） | 約 160 MB／小時（22050 Hz 單聲道；雙聲道加倍） |
| `temperature_<id>.csv` | `Timestamp(ISO), Elapsed(s), Temperature(C), Status`；讀取失敗的列 Temperature 留空、Status 記錄原因 | 約 0.2 MB／小時 |
| `distance_<id>.csv` | `Timestamp, Elapsed(s), Absolute(mm), Relative(mm)`（欄位與原程式相同） | 約 1.5 MB／小時（測距間隔 0.1 秒） |
| `spectrogram_<id>.csv` + `_metadata.txt` | 停止後由完整 WAV 產生，格式與原本 `save_spectrogram_to_csv` 完全相同 | 約 270–300 MB／小時 |
| `experiment_<id>.json` | 參數、啟用的感測器、裝置、開始／結束時間、停止原因、各檔案筆數與錯誤紀錄 | 數 KB |

- 合計約 **460 MB／小時**，8 小時約 3.7 GB。預檢時若存檔磁碟剩餘空間 < 2 GB 會警告；錄製中寫檔失敗（例如磁碟滿）會跳通知並記錄在 json，監測不會靜默中斷。
- 只有啟用的感測器才會建檔（例如沒選 COM 埠就不會有 temperature CSV）。
- 頻譜 CSV 以分塊方式計算，記憶體用量約 150 MB、與錄音長度無關；1 小時錄音約 6 秒產生完。若中途失敗，可事後補產生：`uv run python chunked_spectrogram.py Sensor_Data/EXP_YYYYmmdd_HHMMSS`。
- 「頻譜圖顯示長度」只影響畫面（上限 600 秒），不影響存檔。

#### MongoDB（預設關閉）
- 設定區的「同時寫入 MongoDB」預設關閉；關閉時程式完全不會載入 pymongo 或連線，沒裝 MongoDB 的電腦也能正常使用。
- 開啟後，按「開始監測」時會先測試連線（最多 3 秒）；連不上會顯示說明，可選擇「關閉 MongoDB 繼續」。
- MongoDB 單筆文件上限 16 MB，約等於 50 秒音訊的頻譜；超過時會**跳通知並略過**寫入（資料仍完整在檔案中），不會靜默失敗。

#### 模擬模式（沒有硬體時測試完整流程）
```sh
uv run python web_monitor.py --simulate temp,distance            # 溫度、測距儀用模擬資料，音訊仍用麥克風
uv run python web_monitor.py --simulate all --simulate-faults    # 全部模擬，並定期模擬斷線／恢復
```
模擬資料會清楚標示：頁面頂部顯示「模擬模式」、對應卡片標「模擬」、`experiment_<id>.json` 記錄 `"simulated"`，實驗資料夾另有 `SIMULATED.txt`。**不要把模擬資料夾當成真實量測使用。**

#### 常見問題排除
- **MongoDB 連不到**：資料仍會完整存成檔案。若要使用 MongoDB：1) 安裝 MongoDB Community Server（Windows 安裝時勾選 Install as a Service）2) 執行 `services.msc` 確認服務「MongoDB」已啟動 3) 或關閉「同時寫入 MongoDB」。
- **找不到 COM 埠**：確認 USB 轉 RS485 轉換器已接上並安裝驅動（常見晶片 CH340、FTDI、CP210x、PL2303），在「裝置管理員 → 連接埠 (COM 和 LPT)」確認出現 `COMx`；也可以在欄位直接輸入埠名。
- **麥克風錄到全為 0／沒有音訊設備**：Windows「設定 → 隱私權與安全性 → 麥克風」開啟「麥克風存取」與「讓桌面應用程式存取麥克風」；確認輸入裝置沒被停用，並關閉占用麥克風的程式（Teams、Zoom）。macOS 則在「系統設定 → 隱私權與安全性 → 麥克風」允許終端機。
- **LKIF2.dll 載入失敗／測距儀初始化失敗**：需要 64 位元 Python 搭配 64 位元 LKIF2.dll（專案附的版本），`CmnLib.dll`、`KeyUsbDrv.dll` 要在同一資料夾；安裝 KEYENCE 的 USB 驅動並確認裝置管理員中有控制器。macOS 無法使用測距儀。
- **port 5002 被占用**：`uv run python web_monitor.py --port 5003`，或編輯 `start_monitor.bat` 開頭的 `set PORT=5002`。

### 4. 檢查資料庫紀錄
```sh
uv run python check_db.py
```
- 連線 MongoDB 並列出指定集合的最新一筆紀錄。

---

## 資料庫（MongoDB）

- 預設連線字串：`mongodb://localhost:27017/`，資料庫名稱 `sensor_data`。
- 集合：`measurements`（單點量測，如溫度、測距）、`spectrograms`（音訊頻譜）。
- 連線設定目前寫在 `db_logger.py` 的 `DatabaseLogger.__init__` 預設參數，以及 `check_db.py` 的 `MONGO_URI` 常數；若要連到其他主機請修改這兩處。
- 若 MongoDB 未啟動，`DatabaseLogger` 會印出連線失敗訊息並停用寫入，不會中斷感測器主程式。

### 安裝並啟動 MongoDB（macOS / Homebrew）

```sh
brew tap mongodb/brew
brew trust --formula mongodb/brew/mongodb-community        # 新版 Homebrew 需先信任第三方 tap
brew trust --formula mongodb/brew/mongodb-database-tools   # 相依套件
brew trust --formula mongodb/brew/mongodb-enterprise       # 僅供 brew 檢查衝突，不會安裝
brew install mongodb-community
brew services start mongodb-community      # 開機自動啟動
```

常用管理指令：

| 動作 | 指令 |
|---|---|
| 啟動（並設為開機啟動） | `brew services start mongodb-community` |
| 停止 | `brew services stop mongodb-community` |
| 重新啟動 | `brew services restart mongodb-community` |
| 查看狀態 | `brew services info mongodb-community` |
| 確認埠號有在監聽 | `lsof -nP -iTCP:27017 -sTCP:LISTEN` |
| 進入資料庫 shell | `mongosh` |
| 查看日誌 | `tail -f /opt/homebrew/var/log/mongodb/mongo.log` |

資料存放於 `/opt/homebrew/var/mongodb`，日誌位於 `/opt/homebrew/var/log/mongodb/mongo.log`。

### 安裝並啟動 MongoDB（Windows）

1. 下載並執行 [MongoDB Community Server MSI 安裝程式](https://www.mongodb.com/try/download/community)。
2. 安裝精靈中勾選 **Install MongoDB as a Service**，即會註冊為 Windows 服務並自動啟動。
3. 以系統管理員身分於 PowerShell 管理服務：
   ```powershell
   net start MongoDB     # 啟動
   net stop MongoDB      # 停止
   ```

### 驗證連線

```sh
mongosh --quiet --eval 'db.runCommand({ping:1})'   # 預期輸出 { ok: 1 }
uv run python check_db.py                          # 用專案程式碼實際查詢
```

---

## 子系統與模組說明

### temp_py_package
- 溫度感測器通訊協定、CRC、封包解析、連線與資料讀取（支援 Modbus RTU）。

### signal_package
- 音訊錄音（`audio_recorder.py`）、訊號處理（`signal_processor.py`）、頻譜儲存（`audio_save.py`）、單機測試（`sound_main.py`）。

### spectrum_py_package
- 光譜儀驅動、校正、資料解析、即時繪圖與儲存。

### rangefinder
- KEYENCE LK-G5000 測距儀 DLL 介接、參數設定、資料讀取、錯誤處理。

### utils
- 各類輔助函式與測試腳本。

### templates
- Flask 前端 HTML 模板（`test.html`、`clock_test.html`）。

---

## 平台需求

| 功能 | Windows | macOS / Linux |
|---|---|---|
| 溫度感測、音訊監測、網頁監控、MongoDB | ✅ | ✅ |
| 雷射測距儀（`rangefinder`） | ✅ | ❌ 使用 `ctypes.WinDLL` 載入 `LKIF2.dll`，僅限 Windows |
| 光譜儀（`spectrum_py_package`） | ✅ 需 `avaspecx64.dll` | ⚠️ 需自行安裝 AvaSpec 原生函式庫（macOS：`/usr/local/lib/libavs.0.dylib`；Linux：`/usr/local/lib/libavs.so.0`），否則匯入即失敗 |

Python 套件本身在三大平台皆可安裝，上述限制來自硬體廠商提供的原生驅動。

---

## 已知問題

- `sound_main.py`（根目錄）與 `main_csv_test.py` 匯入了 `signal_package.save_spectrogram_to_tdms`，但 `signal_package` 只提供 `save_spectrogram_to_csv`，執行時會直接 `ImportError`。此問題與環境無關（改用 uv 之前即存在），需補上 TDMS 儲存實作（另需安裝 `npTDMS`）或改用 CSV 版本。

---

## 常見問題

- **Q: `uv sync` 之後仍找不到套件？**
  - 請用 `uv run python ...` 執行，或確認 VS Code 選到的是專案內的 `.venv`，而不是系統或舊的 conda Python。
- **Q: 執行時找不到 DLL/Lib？**
  - 請確認驅動檔案（如 `LKIF2.dll`、`avaspecx64.dll`）已放置於專案根目錄或系統路徑下。
- **Q: 在 macOS 匯入 `spectrum_py_package` 出現 `dlopen ... libavs.0.dylib (no such file)`？**
  - AvaSpec 的原生函式庫未安裝。此模組需要廠商提供的驅動，只在有實體光譜儀的機器上使用。
- **Q: 音訊裝置無法偵測？**
  - 請確認麥克風已正確連接，並安裝對應驅動。macOS 另需在「系統設定 → 隱私權與安全性 → 麥克風」授權終端機或 VS Code。
- **Q: Flask 頁面無法顯示即時資料？**
  - 請確認感測器已連接，且後端程式無錯誤。
- **Q: MongoDB 連線失敗？**
  - 請確認本機 MongoDB 服務已啟動（見上方「安裝並啟動 MongoDB」），或修改 `db_logger.py` / `check_db.py` 中的連線字串。
- **Q: `brew services` 顯示 started，但 27017 沒有服務在聽？**
  - 服務起來後隨即崩潰。查日誌 `tail -50 /opt/homebrew/var/log/mongodb/mongo.log`。若看到 `Wrong mongod version` / `invalid featureCompatibilityVersion`，表示資料目錄是舊版 MongoDB 留下的，新版無法直接掛載。確認 `/opt/homebrew/var/mongodb` 內沒有需要保留的資料後，將該目錄改名備份再重建空目錄，然後重新啟動服務。

---

## 版權與授權
本專案採用 MIT License，歡迎自由使用、修改與發佈。

---

## 聯絡方式
如有問題或建議，請於 GitHub 提出 issue 或聯絡專案
