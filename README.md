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
