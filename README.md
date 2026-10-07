# Sensor-web-page

## 專案簡介
Sensor-web-page 是一套多感測器資料整合與即時監控平台，支援溫度感測器（RS485/Modbus）、音訊監測（麥克風）、AvaSpec 光譜儀、KEYENCE LK-G5000 雷射測距儀等多種裝置。提供 Tkinter 圖形化操作介面與 Flask SSE 網頁即時監控；量測資料一律以檔案形式存放，適用於實驗室、工業現場等多感測應用。

---

## 主要功能
- **溫度感測**：即時讀取 RS485/Modbus 溫度感測器數據。
- **音訊監測**：支援錄音、即時聲音熱圖（Spectrogram）顯示，並可儲存 CSV。
- **光譜儀資料收集**：AvaSpec 光譜儀即時量測、繪圖與自動儲存。
- **雷射測距儀**：KEYENCE LK-G5000 單次/連續量測、資料儲存與顯示。
- **圖形化操作介面**：Tkinter 整合多感測器監控、參數設定、即時圖表。
- **網頁即時監控**：Flask SSE 多圖表即時資料推送，支援多用戶瀏覽。

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
├── app/                       # 網頁版監控服務
│   ├── web_monitor.py         #   Flask 伺服器與監測邏輯（main.py 呼叫它）
│   ├── templates/             #   前端 HTML（monitor.html 等）
│   ├── static/                #   前端 JS / CSS
│   ├── chunked_spectrogram.py #   由 WAV 分塊產生頻譜 CSV
│   └── sim_devices.py         #   --simulate 模擬感測器
├── sensors/                   # 感測器驅動套件
│   ├── temp_py_package/       #   溫度感測器通訊協定與驅動
│   ├── signal_package/        #   音訊錄音、處理、儲存
│   ├── rangefinder/           #   KEYENCE 測距儀（LKIF2.dll 介接）
│   ├── spectrum_py_package/   #   AvaSpec 光譜儀
│   └── utils/                 #   工具函式
├── drivers/                   # 廠商 DLL：LKIF2.dll、CmnLib.dll、KeyUsbDrv.dll、avaspecx64.dll
├── legacy/                    # 舊版程式（Tkinter 介面、單一感測器腳本、check_db.py、environment.yml、spectra_logs/）
├── launchers/                 # 一鍵啟動器的實際邏輯與圖示（mac/、windows/）
├── scripts/                   # windows_selfcheck.py 環境自我檢查
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
| `pyqt5`（選裝，`spectrometer` extra） | 只有舊的光譜儀腳本 `legacy/spectrum_main.py` 需要；網頁版不需要。需要時執行 `uv sync --extra spectrometer`。注意：新版 PyQt5-Qt5 沒有 Windows wheel，所以不列為預設依賴，否則 Windows 上 `uv sync` 會失敗 |

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
- **一鍵啟動**：見上一節（Windows 的 `start_monitor.bat`、macOS 的 `Sensor Monitor.app`）。
- 瀏覽器開啟 [http://127.0.0.1:5002](http://127.0.0.1:5002)。功能與 `legacy/main_csv.py` 相同（溫度、音訊頻譜圖、測距儀，並即時繪製溫度與距離曲線），介面改為網頁；**一律手動按「停止監測」才結束**。
- 監測在伺服器端進行，關掉或重新整理網頁不會中斷；重新開啟頁面會自動復原目前畫面。
- 在終端機按 `Ctrl+C` 關閉伺服器：會先停止監測、關檔、產生頻譜 CSV 並釋放硬體。
- 圖表不依賴 CDN，實驗室電腦離線也能使用。
- 第一次在新電腦（特別是 Windows 筆電）使用前，先跑自我檢查：`uv run python scripts/windows_selfcheck.py`。

#### 存檔內容（整場實驗完整保存）
按下「開始監測」就建立 `Sensor_Data/EXP_YYYYmmdd_HHMMSS/`，資料**邊錄邊寫**（每筆都 flush，當機或斷電也只會少最後一瞬間）：

| 檔案 | 內容 | 預設設定下的大小 |
|---|---|---|
| `audio_<id>.wav` | 全程原始音訊（16-bit，實際取樣率與聲道數） | 約 160 MB／小時（22050 Hz 單聲道；雙聲道加倍） |
| `temperature_<id>.csv` | `Timestamp(ISO), Elapsed(s), Temperature(C), Status`；讀取失敗的列 Temperature 留空、Status 記錄原因 | 約 0.2 MB／小時 |
| `distance_<id>.csv` | `Timestamp(Unix 秒), Elapsed(s), Absolute(mm), Relative(mm), Status`；每次讀取都記一列，Status 為儀器的 FloatResult（`VALID`／`+RANGEOVER`／`-RANGEOVER`／`WAITING`／`ALARM`／`INVALID`）或 `ERROR: <訊息>`，非 VALID 的列距離留空 | 約 1.5 MB／小時（測距間隔 0.1 秒） |
| `spectrogram_<id>.csv` + `_metadata.txt` | 停止後由完整 WAV 產生，格式與原本 `save_spectrogram_to_csv` 完全相同 | 約 270–300 MB／小時 |
| `experiment_<id>.json` | 參數、啟用的感測器、裝置、開始／結束時間、停止原因、各檔案筆數與錯誤紀錄 | 數 KB |

- 合計約 **460 MB／小時**，8 小時約 3.7 GB。預檢時若存檔磁碟剩餘空間 < 2 GB 會警告；錄製中寫檔失敗（例如磁碟滿）會跳通知並記錄在 json，監測不會靜默中斷。
- 只有啟用的感測器才會建檔（例如沒選 COM 埠就不會有 temperature CSV）。
- 頻譜 CSV 以分塊方式計算，記憶體用量約 150 MB、與錄音長度無關；1 小時錄音約 6 秒產生完。若中途失敗，可事後補產生：`uv run python app/chunked_spectrogram.py Sensor_Data/EXP_YYYYmmdd_HHMMSS`。
- 「頻譜圖顯示長度」只影響畫面（上限 600 秒），不影響存檔。

#### 模擬模式（沒有硬體時測試完整流程）
```sh
uv run python main.py --simulate temp,distance            # 溫度、測距儀用模擬資料，音訊仍用麥克風
uv run python main.py --simulate all --simulate-faults    # 全部模擬，並定期模擬斷線／恢復
```
模擬資料會清楚標示：頁面頂部顯示「模擬模式」、對應卡片標「模擬」、`experiment_<id>.json` 記錄 `"simulated"`，實驗資料夾另有 `SIMULATED.txt`。**不要把模擬資料夾當成真實量測使用。**

#### 常見問題排除
- **找不到 COM 埠**：確認 USB 轉 RS485 轉換器已接上並安裝驅動（常見晶片 CH340、FTDI、CP210x、PL2303），在「裝置管理員 → 連接埠 (COM 和 LPT)」確認出現 `COMx`；也可以在欄位直接輸入埠名。
- **麥克風錄到全為 0／沒有音訊設備**：Windows「設定 → 隱私權與安全性 → 麥克風」開啟「麥克風存取」與「讓桌面應用程式存取麥克風」；確認輸入裝置沒被停用，並關閉占用麥克風的程式（Teams、Zoom）。macOS 則在「系統設定 → 隱私權與安全性 → 麥克風」允許終端機。
- **LKIF2.dll 載入失敗／測距儀初始化失敗**：需要 64 位元 Python 搭配 64 位元 LKIF2.dll（專案附的版本），`CmnLib.dll`、`KeyUsbDrv.dll` 要一起放在 `drivers/`；安裝 KEYENCE 的 USB 驅動並確認裝置管理員中有控制器。macOS 無法使用測距儀。
- **port 5002 被占用**：`uv run python main.py --port 5003`，或設定環境變數 `SENSOR_MONITOR_PORT`（一鍵啟動器會自動改用下一個可用的埠）。

### 舊版程式（legacy/）
從專案根目錄執行（腳本開頭已設定好 `sensors/`、`app/` 的匯入路徑）：
```sh
uv run python legacy/main_csv.py          # Tkinter 多感測器整合（資料同樣存到根目錄 Sensor_Data/）
uv run python legacy/temperture_main.py   # 單一溫度感測器
uv run python legacy/sound_main.py        # 單一音訊（見「已知問題」）
uv sync --extra spectrometer && uv run python legacy/spectrum_main.py   # 單一光譜儀，需選裝 PyQt5（輸出到 legacy/spectra_logs/）
uv run python legacy/rangefinder_main.py  # 單一測距儀
uv run python legacy/main_web.py          # 早期 Flask SSE 範例：http://localhost:5000/chart_test
uv run python legacy/check_db.py          # 列出 MongoDB 最新一筆紀錄
```

---

## 子系統與模組說明

以下套件都在 `sensors/`。

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

### app/templates、app/static
- 網頁版前端（`monitor.html`、`monitor.js`、`monitor.css`）；`test.html`、`clock_test.html` 供 `legacy/main_web.py` 使用。

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
