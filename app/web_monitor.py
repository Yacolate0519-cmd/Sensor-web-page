"""感測器整合系統：網頁版（取代 main_csv.py 的 Tkinter 介面）。

啟動（在專案根目錄）：uv run python main.py  →  http://127.0.0.1:5002
（也可直接 uv run python app/web_monitor.py，兩者參數相同）

架構
- MonitorService（單例）持有所有監測狀態，以 threading.Lock 保護。
- 溫度／音訊／測距儀／光譜儀各一條 worker thread，另有一條計時 thread 負責執行時間與自動停止；
  行為對齊 main_csv.py（輪詢間隔、連續 10 次失敗才報斷線、恢復通知、停止時存檔）。
- 前端透過 REST 下指令，透過 SSE (/api/stream) 接收狀態、圖表資料與通知。
- 監測在伺服器端持續進行，頁面重整後由 /api/state 與 /api/spectrogram 復原畫面。
- 存檔：開始時就建立 Sensor_Data/EXP_<id>/，所有感測器資料邊錄邊寫（WAV / CSV 每筆 flush），
  停止時再由完整 WAV 分塊產生 spectrogram_<id>.csv（chunked_spectrogram.py，格式與原版相同）。
- Log（標準 logging，每行 flush，終端機也看得到）：
  logs/server_YYYYMMDD.log      伺服器 log（啟動/關閉、預檢、開始/停止、未處理例外，以及實驗期間的所有事件）
  Sensor_Data/EXP_<id>/experiment_<id>.log   該次實驗的 log（開始時掛上 handler、存檔完成後移除）
  格式：`2026-10-07 14:31:05.123 [INFO ] 溫度     訊息`。感測器只在「狀態/原因改變」時寫一行，
  持續失敗每 LOG_SUMMARY_S 秒補一行摘要，恢復時寫失敗持續秒數與次數，不會每筆失敗都寫。
"""

import argparse
import atexit
import base64
import collections
import csv
import datetime
import json
import logging
import math
import os
import queue
import platform
import shutil
import signal
import sys
import threading
import time
import wave

# 專案路徑：本檔在 <repo>/app/，感測器套件在 <repo>/sensors/，硬體 DLL 在 <repo>/drivers/。
# 以檔案位置推算，不依賴目前工作目錄（從 main.py、啟動器或直接執行本檔都一樣）。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(APP_DIR)  # 專案根目錄（Sensor_Data/ 在這裡）
SENSORS_DIR = os.path.join(BASE_DIR, "sensors")
DRIVERS_DIR = os.path.join(BASE_DIR, "drivers")
for _p in (SENSORS_DIR, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import matplotlib  # noqa: E402

# signal_package 在匯入時就會載入 pyplot，必須先指定非互動 backend，
# 否則 macOS 會選 macosx backend，在非主執行緒繪圖會直接崩潰。
matplotlib.use("Agg")
from matplotlib import mlab  # noqa: E402

import numpy as np  # noqa: E402
import pyaudio  # noqa: E402
from flask import Flask, Response, jsonify, render_template, request  # noqa: E402

from rangefinder import LKIF2Device  # noqa: E402
from rangefinder.constants import LKIF_ABLEMODE_AUTO, RC_OK  # noqa: E402
import chunked_spectrogram  # noqa: E402
import sim_devices  # noqa: E402
import spectrometer_driver  # 只定義包裝類別；真正的驅動延遲到預檢／worker 啟動才 import
from signal_package import AudioRecorder  # noqa: E402
from temp_py_package import list_candidate_ports  # noqa: E402
from temp_py_package.reader import check_port_present, read_temperature_diag

DATA_DIR = os.path.join(BASE_DIR, "Sensor_Data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LKIF_DLL_PATH = os.path.join(DRIVERS_DIR, "LKIF2.dll")

HOST = "127.0.0.1"
PORT = 5002

# 與 main_csv.py / signal_package.plot_spectrogram 相同
NFFT = 256
NOVERLAP = 128
HOP = NFFT - NOVERLAP
MAX_FAILURES = 10
LOG_SUMMARY_S = 60              # 同一原因持續失敗時，log 每隔幾秒補一行摘要
SERIES_MAX_POINTS = 2000       # 溫度／距離歷史送給前端時降採樣的點數上限
SSE_QUEUE_MAX = 300
SPEC_BINS = 512                 # 光譜熱圖每筆降到的波長 bin 數（每 bin 取 max，保留峰值）
SPEC_HISTORY_MAX = 1200         # 熱圖歷史最多保留幾筆（另受 history_duration 時間窗限制）
SPEC_MAX_COUNTS = 65535         # 送前端的強度量化上限（uint16）
MAX_DISPLAY_SECONDS = 600       # 頻譜圖顯示長度上限（只影響記憶體中的顯示緩衝）
MIN_FREE_BYTES = 2 * 1024 ** 3  # 剩餘空間低於 2 GB 時預檢警告
# 每小時檔案大小估計（22050 Hz、單聲道、預設間隔；1 小時實測）
EST_BYTES_PER_HOUR = {"wav": 158_760_044, "spectrogram": 297_639_967, "temperature": 200_000,
                      "distance": 2_000_000, "spectrometer": 111_000_000}  # 光譜儀：預設每 0.5 秒一列、2048 欄


# 測距儀固定參數（main_csv.py L67-70）
BASIC_REF = 50.0  # mm；LKIF2 回傳的 Value 是相對此基準的 mm，寫檔與顯示時換成 µm
OUT_NO = 0
SAMPLING_US = 1000
RANGE_CODE = 0

AUDIO_NONE = "無可用音訊設備"
AUDIO_FAILED = "音訊設備檢測失敗"

DEFAULT_PARAMS = {
    "com_port": "",
    "audio_device": "",
    "duration": "-1",
    "sample_rate": "22050",
    "update_interval": "0.1",
    "history_duration": "100",
    "refl_mode": "0",
    "distance_interval": "0.1",
    "spec_interval": "0.5",
    "spec_integration_ms": "50",
}

REFL_MODE_LABELS = {0: "0-漫反射", 1: "1-鏡面反射"}
STOP_REASONS = {"manual": "手動停止", "sigint": "Ctrl+C", "sigterm": "SIGTERM", "sigbreak": "Ctrl+Break",
                "sighup": "關閉終端機視窗", "console_close": "關閉主控台視窗",
                "error": "錯誤", "timeout": "計時結束"}

# 感測器狀態的原因代碼（前端 monitor.js 的 REASON_LABELS 要與此一致）
R_IDLE, R_INIT, R_OK, R_DISABLED = "idle", "init", "ok", "disabled"
R_DRIVER, R_NOT_FOUND, R_BUSY = "driver", "not_found", "busy"
R_NO_DATA, R_WIRING, R_OUT_OF_RANGE = "no_data", "wiring", "out_of_range"
R_ERROR, R_STOPPED = "error", "stopped"
# 這些原因不算「失敗」，set_sensor 不會累加 fail_count
NON_FAILURE_REASONS = {R_IDLE, R_INIT, R_OK, R_DISABLED, R_STOPPED}
REASON_LABELS = {
    R_DRIVER: "驅動／韌體問題", R_NOT_FOUND: "找不到裝置", R_BUSY: "Port 衝突／被佔用",
    R_NO_DATA: "數據收不進來", R_WIRING: "接線／訊號異常", R_OUT_OF_RANGE: "超出量程",
    R_DISABLED: "未選擇／未啟用", R_ERROR: "其他錯誤",
    R_IDLE: "待機", R_INIT: "初始化中", R_OK: "正常", R_STOPPED: "已停止",
}
# 感測器 key → 中文名（log 的來源欄、預檢說明用）。新增感測器時在這裡加，
# 並在 self.sensors / _reset_sensor_display 加同名 key；前端狀態面板的 sensors 缺少該 key 時會顯示「未接入」。
SENSOR_NAMES = {"temp": "溫度", "audio": "音訊", "distance": "距離", "spectrometer": "光譜儀"}
SRC_SYSTEM = "系統"


# ---------------------------------------------------------------------------
# Log：標準 logging。來源欄放在 record.src（系統/溫度/音訊/距離…）
# ---------------------------------------------------------------------------
log = logging.getLogger("sensor_monitor")
INFO, WARN, ERROR = logging.INFO, logging.WARNING, logging.ERROR


def slog(level, src, msg, exc=None):
    """寫一行 log；exc 為例外物件時附上完整 traceback。"""
    log.log(level, msg, extra={"src": src}, exc_info=exc)


LOG_LEVEL_NAMES = {"WARNING": "WARN", "CRITICAL": "ERROR"}


def _display_width(text):
    return sum(2 if ord(c) >= 0x2E80 else 1 for c in text)


class LogFormatter(logging.Formatter):
    """2026-10-07 14:31:05.123 [INFO ] 溫度     訊息（來源欄以顯示寬度補齊到 9 欄）。"""

    def format(self, record):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        level = LOG_LEVEL_NAMES.get(record.levelname, record.levelname)
        src = getattr(record, "src", SRC_SYSTEM)
        text = (f"{ts}.{int(record.msecs):03d} [{level:<5}] {src}"
                f"{' ' * max(1, 9 - _display_width(src))}{record.getMessage()}")
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


class DailyFileHandler(logging.Handler):
    """logs/server_YYYYMMDD.log：附加寫入、每行 flush、跨日自動換檔。"""

    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self._day = None
        self._file = None

    def emit(self, record):
        try:
            day = time.strftime("%Y%m%d")
            if day != self._day:
                if self._file:
                    self._file.close()
                os.makedirs(self.directory, exist_ok=True)
                self._file = open(os.path.join(self.directory, f"server_{day}.log"), "a",  # noqa: SIM115  長期持有
                                  encoding="utf-8-sig")
                self._day = day
            self._file.write(self.format(record) + "\n")
            self._file.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def close(self):
        with self.lock:
            if self._file:
                self._file.close()
                self._file = None
        super().close()


_logging_ready = False


def setup_logging():
    """伺服器 log（檔案）＋終端機輸出；實驗 log 由 ExperimentRecorder.attach_log 另外掛上。可重複呼叫。"""
    global _logging_ready
    if _logging_ready:
        return
    _logging_ready = True
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = LogFormatter()
    for h in (DailyFileHandler(LOG_DIR), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)


# ---------------------------------------------------------------------------
# 裝置列舉
# ---------------------------------------------------------------------------
def list_com_ports():
    """對應 main_csv.refresh_com_ports：候選埠排序後回傳，第一個為預設值。"""
    try:
        ports = [
            {"device": p.device, "description": p.description or ""}
            for p in list_candidate_ports()
        ]
        return {"ports": ports, "default": ports[0]["device"] if ports else "", "error": None}
    except Exception as e:  # noqa: BLE001
        slog(ERROR, SRC_SYSTEM, f"刷新COM端口錯誤: {e}", e)
        return {"ports": [], "default": "", "error": str(e)}


def list_audio_devices():
    """對應 main_csv.refresh_audio_devices：只列出有輸入通道的設備。"""
    try:
        p = pyaudio.PyAudio()
        devices = []
        try:
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if info["maxInputChannels"] > 0:
                    devices.append({
                        "index": i,
                        "name": info["name"],
                        "display_name": f"{i}: {info['name']}",
                        "channels": int(info["maxInputChannels"]),
                        "sample_rate": float(info["defaultSampleRate"]),
                    })
        finally:
            p.terminate()
        if devices:
            return {"devices": devices, "status": "ok", "placeholder": None,
                    "default": devices[0]["display_name"]}
        return {"devices": [], "status": "none", "placeholder": AUDIO_NONE, "default": AUDIO_NONE}
    except BaseException as e:  # noqa: BLE001  PyAudio 初始化失敗也不可拖垮伺服器
        slog(ERROR, SRC_SYSTEM, f"刷新音訊設備錯誤: {e}", e)
        return {"devices": [], "status": "error", "placeholder": AUDIO_FAILED, "default": AUDIO_FAILED}


def parse_audio_device_index(selected):
    """對應 main_csv.get_selected_audio_device_index。"""
    if not selected or selected in (AUDIO_NONE, AUDIO_FAILED):
        return None
    try:
        return int(str(selected).split(":")[0])
    except (TypeError, ValueError):
        return None


def classify_rangefinder_error(exc):
    """把 LKIF2Device 建構/open 的例外分類成 (reason, detail)。FileNotFoundError 是 OSError 子類，要先判斷。"""
    if isinstance(exc, FileNotFoundError):
        return R_DRIVER, "找不到 drivers/LKIF2.dll：確認 drivers 資料夾內有 LKIF2.dll、CmnLib.dll、KeyUsbDrv.dll"
    if isinstance(exc, AttributeError):
        return R_DRIVER, "此系統不是 Windows，無法載入 KEYENCE DLL"
    if isinstance(exc, OSError):
        return R_DRIVER, "DLL 載入失敗：缺少相依 DLL（CmnLib.dll／KeyUsbDrv.dll）或 Python 與 DLL 位元（32/64）不符"
    if isinstance(exc, RuntimeError) and "OpenDevice" in str(exc):
        return R_NOT_FOUND, "LK-G5000 未連線：檢查 USB 線、控制器電源、KEYENCE USB 驅動"
    return R_ERROR, f"測距儀發生未預期的錯誤：{exc}"


def classify_spectrometer_error(exc):
    """把光譜儀驅動 import／初始化／量測的例外分類成 (reason, detail)（沿用 R_* 代碼）。"""
    msg = str(exc)
    if isinstance(exc, ImportError):
        if getattr(exc, "name", None) == "PyQt5" or "PyQt5" in msg:
            return R_DRIVER, "缺少 PyQt5：光譜儀驅動需要 PyQt5（uv sync --extra spectrometer）"
        return R_DRIVER, f"光譜儀驅動載入失敗：{msg}"
    if isinstance(exc, FileNotFoundError):
        return R_DRIVER, "找不到 AvaSpec 驅動檔：Windows 需要 drivers/avaspecx64.dll，macOS／Linux 需安裝 AvaSpec 原生函式庫"
    if isinstance(exc, OSError):
        return R_DRIVER, f"AvaSpec DLL 載入失敗：缺少相依檔，或 Python 與 DLL 位元（32/64）不符（{msg}）"
    if isinstance(exc, AttributeError):
        return R_DRIVER, f"此系統無法載入 AvaSpec 驅動（{msg}）"
    if "沒有找到光譜儀" in msg or "無可用設備" in msg:
        return R_NOT_FOUND, "找不到光譜儀：檢查 USB 線、電源與 AvaSpec 驅動（裝置管理員是否看得到 AvaSpec）"
    if "等待超時" in msg:
        return R_NO_DATA, "光譜儀沒有回傳資料（等待超時）：確認光譜儀未被其他程式佔用，並檢查積分時間與觸發設定"
    return R_ERROR, f"光譜儀發生未預期的錯誤：{msg}"


# 測距儀 FloatResult 非 VALID 時的分類（Unknown/INVALID 另外處理）
RANGEFINDER_STATUS_DIAG = {
    "WAITING": (R_NO_DATA, "測距儀等待資料中：尚未取得量測值，確認控制器為量測模式且取樣已開始"),
    "+RANGEOVER": (R_OUT_OF_RANGE, "目標超出量測範圍（過遠）：調整感測頭與目標的距離"),
    "-RANGEOVER": (R_OUT_OF_RANGE, "目標超出量測範圍（過近）：調整感測頭與目標的距離"),
    "ALARM": (R_WIRING, "量測警報：受光量不足或感測頭接線異常，檢查感測頭連接與目標表面"),
}


def diagnose_audio_device(index):
    """AudioRecorder 因 sys.exit(1) 失敗後，直接用 pyaudio 查出原因，回傳 (reason, detail)。"""
    if index is None:
        return R_NOT_FOUND, "未選擇有效的音訊輸入設備"
    try:
        p = pyaudio.PyAudio()
    except BaseException as e:  # noqa: BLE001
        return R_DRIVER, f"音訊系統（PortAudio/PyAudio）初始化失敗：確認音訊驅動與 PyAudio 已正確安裝（{e}）"
    try:
        inputs = []
        for i in range(p.get_device_count()):
            if p.get_device_info_by_index(i)["maxInputChannels"] > 0:
                inputs.append(i)
        if not inputs:
            return R_NOT_FOUND, "系統找不到任何音訊輸入設備：確認麥克風已插上且未被系統停用"
        if index not in inputs:
            return R_NOT_FOUND, f"指定的設備 {index} 不存在或不是輸入設備：麥克風可能已被拔除，請重新整理設備清單"
        return R_BUSY, "設備無法開啟：可能被其他程式佔用，或未授予麥克風權限"
    except BaseException as e:  # noqa: BLE001
        return R_ERROR, f"查詢音訊設備時發生錯誤：{e}"
    finally:
        p.terminate()


# ---------------------------------------------------------------------------
# 資料處理小工具
# ---------------------------------------------------------------------------
def downsample_series(points, max_points=SERIES_MAX_POINTS):
    """溫度／距離時間序列降採樣：points 為 [(elapsed 秒, 值或 None), ...]，回傳 [[t, v], ...]。

    超過 max_points 時依索引分桶；桶內每段連續有效值保留 min/max（依時間排序，保住峰值），
    每段連續失敗（None）只留一個 None 點，前端遇到 None 會斷線，所以缺口不會被抹平。
    """
    n = len(points)
    if n <= max_points:
        return [[round(t, 2), v] for t, v in points]
    buckets = max(1, max_points // 2)
    edges = [n * i // buckets for i in range(buckets + 1)]

    def run_min_max(run):
        lo = min(run, key=lambda p: p[1])
        hi = max(run, key=lambda p: p[1])
        return [lo] if lo is hi else sorted((lo, hi), key=lambda p: p[0])

    out = []
    for b in range(buckets):
        run = []  # 目前這一段連續有效值
        none_open = False
        for t, v in points[edges[b]:edges[b + 1]]:
            if v is None:
                if run:
                    out.extend(run_min_max(run))
                    run = []
                if not none_open:
                    out.append((t, None))
                    none_open = True
            else:
                none_open = False
                run.append((t, v))
        if run:
            out.extend(run_min_max(run))
    if len(out) > max_points * 1.5:
        # 失敗與成功頻繁交錯時，改用簡化版（每桶最多 min/max 兩點，整桶皆失敗才留 None），確保點數有上限
        out = []
        for b in range(buckets):
            seg = points[edges[b]:edges[b + 1]]
            valid = [p for p in seg if p[1] is not None]
            out.extend(run_min_max(valid) if valid else [(seg[0][0], None)])
    return [[round(t, 2), v] for t, v in out]


def spec_to_int16_db(spec_linear):
    """線性 PSD → dB（與 matplotlib specgram 顯示相同的 10·log10）→ int16（×10，0.1 dB 解析度）。

    回傳形狀 (n_cols, n_freq)，每列是一個時間欄。
    """
    db = 10.0 * np.log10(np.maximum(spec_linear, 1e-20))
    q = np.clip(np.round(db * 10.0), -32000, 32000).astype("<i2")
    return q.T.copy()


def bin_spectrum_max(counts, n_bins=SPEC_BINS):
    """光譜 counts（長度 n）→ uint16，並降到 n_bins 個波長 bin（每 bin 取 max，保留峰值）。

    回傳 (bins uint16[n_bins], full uint16[n])；強度四捨五入並夾在 0–65535。
    """
    full = np.clip(np.rint(np.asarray(counts, dtype=float)), 0, SPEC_MAX_COUNTS).astype("<u2")
    edges = np.linspace(0, len(full), n_bins + 1).astype(int)
    return np.maximum.reduceat(full, edges[:-1]), full


def iso_now(ts=None):
    return datetime.datetime.fromtimestamp(ts if ts is not None else time.time()).astimezone().isoformat()


# ---------------------------------------------------------------------------
# 實驗資料落地（邊錄邊寫）
# ---------------------------------------------------------------------------
class ExperimentRecorder:
    """管理一次實驗的資料夾與所有串流檔案。每筆資料寫入後立即 flush，當機也不會丟掉已錄的部分。

    - audio_<id>.wav：wave 每次 writeframes 都會回寫 header，因此即使行程被強制結束，檔頭仍正確。
    - temperature_<id>.csv / distance_<id>.csv：csv.writer + flush。
    - experiment_<id>.json：開始時寫入，結束時補上結束資訊（以暫存檔 + os.replace 原子更新）。
    - experiment_<id>.log：attach_log() 掛上 logging handler，這次實驗期間的所有 log 都寫進來（每行 flush）。
    """

    def __init__(self, experiment_id, info):
        self.experiment_id = experiment_id
        self.dir = os.path.join(DATA_DIR, experiment_id)
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self._json_lock = threading.Lock()  # 多個 worker 會同時 update()；暫存檔路徑相同，寫入必須串行
        self.info = info
        self.files = {}     # key -> 統計（name、rows/frames、bytes…）
        self._handles = {}  # key -> (file, writer, kind)
        self.errors = []
        self._log_handler = None
        self.on_write_error = None  # callback(key, exc)：寫檔失敗（磁碟滿等）時通知，不讓 worker 靜默死掉
        self.write_json()

    def _write_failed(self, key, exc):
        with self.lock:
            st = self.files.get(key)
            first = st is not None and not st.get("write_error")
        if first:
            slog(ERROR, SRC_SYSTEM, f"寫入 {key} 失敗: {exc}（之後同一檔案的失敗不再逐筆記錄）", exc)
        with self.lock:
            if st is not None:
                st["write_error"] = str(exc)
                st["write_failures"] = st.get("write_failures", 0) + 1
        if first:
            try:
                self.add_error(f"{key} 寫入失敗: {exc}")
            except OSError:
                pass  # 連 json 都寫不進去（磁碟滿），至少前端會收到通知
        if self.on_write_error:
            self.on_write_error(key, exc)

    def rel_dir(self):
        return os.path.relpath(self.dir, BASE_DIR)

    def path(self, name):
        return os.path.join(self.dir, name)

    # ---- CSV ----
    def open_csv(self, key, filename, header, encoding="utf-8"):
        f = open(self.path(filename), "w", newline="", encoding=encoding)
        w = csv.writer(f)
        w.writerow(header)
        f.flush()
        with self.lock:
            self._handles[key] = (f, w, "csv")
            self.files[key] = {"name": filename, "kind": "csv", "rows": 0, "fail_rows": 0,
                               "bytes": f.tell(), "closed": False}

    def write_row(self, key, row, ok=True):
        """ok=False 代表這一列是失敗讀取，只用來統計失敗筆數（存檔摘要用）。"""
        with self.lock:
            h = self._handles.get(key)
            if not h:
                return
            f, w, _ = h
            try:
                w.writerow(row)
                f.flush()
            except OSError as e:
                err = e
            else:
                err = None
                st = self.files[key]
                st["rows"] += 1
                if not ok:
                    st["fail_rows"] += 1
                st["bytes"] = f.tell()
        if err is not None:
            self._write_failed(key, err)

    # ---- WAV ----
    def open_wav(self, key, filename, nchannels, sample_rate):
        f = open(self.path(filename), "wb")
        w = wave.open(f, "wb")
        w.setnchannels(nchannels)
        w.setsampwidth(2)  # paInt16
        w.setframerate(sample_rate)
        w.writeframes(b"")  # 先寫出 header
        f.flush()
        with self.lock:
            self._handles[key] = (f, w, "wav")
            self.files[key] = {"name": filename, "kind": "wav", "frames": 0, "channels": nchannels,
                               "sample_rate": sample_rate, "seconds": 0.0, "bytes": f.tell(), "closed": False}

    def write_audio(self, key, samples):
        data = np.asarray(samples, dtype="<i2").tobytes()
        with self.lock:
            h = self._handles.get(key)
            if not h:
                return
            f, w, _ = h
            try:
                w.writeframes(data)
                f.flush()
            except OSError as e:
                err = e
            else:
                err = None
                st = self.files[key]
                st["frames"] = w.getnframes()
                st["seconds"] = st["frames"] / st["sample_rate"]
                st["bytes"] = f.tell()
        if err is not None:
            self._write_failed(key, err)

    # ---- 共用 ----
    def close(self, key):
        with self.lock:
            h = self._handles.pop(key, None)
            if not h:
                return
            f, w, kind = h
            try:
                if kind == "wav":
                    w.close()  # 補寫最終 header（不會關掉外部傳入的檔案物件）
                f.flush()
            finally:
                f.close()
            st = self.files[key]
            st["closed"] = True
            try:
                st["bytes"] = os.path.getsize(self.path(st["name"]))
            except OSError:
                pass

    def close_all(self):
        for key in list(self._handles):
            try:
                self.close(key)
            except Exception as e:  # noqa: BLE001
                slog(ERROR, SRC_SYSTEM, f"關閉 {key} 檔案錯誤: {e}", e)

    def add_file(self, key, name, **stats):
        """登記非串流檔案（例如停止後產生的 spectrogram CSV）。"""
        with self.lock:
            p = self.path(name)
            self.files[key] = {"name": name, "kind": "file", "closed": True,
                               "bytes": os.path.getsize(p) if os.path.exists(p) else 0, **stats}

    # ---- 實驗 log ----
    def attach_log(self):
        """建立 experiment_<id>.log 並把 handler 掛到 logger；之後所有 log（含伺服器層級事件）同步寫入。"""
        name = f"experiment_{self.experiment_id}.log"
        h = logging.FileHandler(self.path(name), mode="a", encoding="utf-8-sig")
        h.setFormatter(LogFormatter())
        log.addHandler(h)
        self._log_handler = h
        with self.lock:
            self.files["log"] = {"name": name, "kind": "log", "bytes": 0, "closed": False}
        self.write_json()

    def close_log(self):
        """移除並關閉實驗 log handler，登記最終大小（存檔流程最後一步）。"""
        h, self._log_handler = self._log_handler, None
        if h is None:
            return
        log.removeHandler(h)
        h.close()
        with self.lock:
            st = self.files["log"]
            st["closed"] = True
            try:
                st["bytes"] = os.path.getsize(self.path(st["name"]))
            except OSError:
                pass
        try:
            self.write_json()
        except OSError:
            pass

    def add_error(self, msg):
        with self.lock:
            self.errors.append({"time": iso_now(), "message": msg})
        self.write_json()

    def update(self, **fields):
        with self.lock:
            self.info.update(fields)
        self.write_json()

    def summary(self):
        with self.lock:
            lg = self.files.get("log")
            if lg and not lg["closed"]:  # log 還在寫：大小即時更新，讓前端存檔列看得到
                try:
                    lg["bytes"] = os.path.getsize(self.path(lg["name"]))
                except OSError:
                    pass
            return {"experiment_id": self.experiment_id, "dir": self.rel_dir(),
                    "files": [dict(v, key=k) for k, v in self.files.items()]}

    def write_json(self):
        with self.lock:
            doc = dict(self.info)
            doc["files"] = {k: {kk: vv for kk, vv in v.items()} for k, v in self.files.items()}
            doc["errors"] = list(self.errors)
        target = self.path(f"experiment_{self.experiment_id}.json")
        tmp = target + ".tmp"
        with self._json_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(tmp, target)
            except PermissionError:
                # Windows：目標檔被其他程式（例如防毒、編輯器）短暫鎖住時 os.replace 會失敗，改為直接覆寫
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=2)
                os.remove(tmp)


# ---------------------------------------------------------------------------
# 監測服務
# ---------------------------------------------------------------------------
class MonitorService:
    def __init__(self):
        self.lock = threading.RLock()
        self.subscribers = []  # list[queue.Queue]
        self.sub_lock = threading.Lock()

        self.phase = "idle"  # idle | running | stopping | stopped
        self.status_text = "待機中"
        self.enabled = {"temp": False, "audio": False, "distance": False, "spectrometer": False}
        self.params = dict(DEFAULT_PARAMS)
        self.duration = -1
        self.start_time = None
        self.elapsed = 0.0
        self.last_result = None
        self.run_id = 0

        self.sensors = {}
        self.preflight_reasons = {}  # 最近一次預檢各感測器的 {reason, detail}
        self._track = {}        # 感測器 key → log 用的狀態追蹤（節流與恢復訊息）
        self._pf_shown = set()  # 目前由預檢結果佔著狀態面板的感測器
        self._reset_sensor_display()

        # 模擬模式（--simulate）：只有列出的感測器改用 sim_devices，其餘仍走真實硬體
        self.simulated = set()
        self.sim_faults = False
        self.sim_temp = None

        self.recorder = None        # 目前／上一次實驗的 ExperimentRecorder
        self.spectrogram_job = None  # 停止後產生頻譜 CSV 的進度
        self.write_failures = set()  # 已通知過寫檔失敗的檔案

        self.stop_event = threading.Event()
        self.threads = {}
        self.finalizing = False
        self.shutdown_done = False

        # 音訊資料
        self.sample_rate = 22050
        self.history_duration = 100.0  # 只影響頻譜圖顯示長度；存檔一律保存全程
        self.spec_pending = np.array([], dtype=np.int16)  # 尚未切成 specgram 欄的尾端樣本
        self.spec_cols = collections.deque()
        self.spec_total = 0  # 自本輪開始產生的欄總數（全域索引）
        self.spec_freqs = None
        self.zero_warned = False

        # 溫度／距離時間序列（本次監測全程）：[(elapsed 秒, 值或 None), ...]；開始新監測時清空
        self.series = {"temp": [], "distance": []}
        self.series_sent = {"temp": 0, "distance": 0}  # 已透過 SSE 送出的筆數

        # 光譜儀（Optical Spectrum）：波長軸 meta、降採樣歷史（熱圖用）、最新一筆完整解析度（即時光譜用）
        self.opt_meta = None
        self.opt_cols = collections.deque(maxlen=SPEC_HISTORY_MAX)  # (elapsed 秒, bins bytes 或 None=讀取失敗)
        self.opt_total = 0      # 自本輪開始的筆數（全域索引，含失敗筆）
        self.opt_latest = None  # {"t": elapsed, "full": uint16 bytes}，最後一筆成功的完整光譜

        self.notices = collections.deque(maxlen=30)
        self.notice_seq = 0

    # ---------------- 事件推送 ----------------
    def subscribe(self):
        # 每個 client 最多暫存 SSE_QUEUE_MAX 筆（約數十秒的圖表資料）；卡住的 client 不會讓記憶體無限成長
        q = queue.Queue(maxsize=SSE_QUEUE_MAX)
        with self.sub_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.sub_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, event, data):
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self.sub_lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # 慢速／卡住的 client：丟掉積壓資料，改送 resync，client 會用 /api/state 重新復原
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait("event: resync\ndata: {}\n\n")
                except queue.Full:
                    pass

    def notify(self, level, title, message, src=SRC_SYSTEM):
        """取代 messagebox：level = info | warning | error | success。同時寫進 log（src 為來源欄）。"""
        with self.lock:
            self.notice_seq += 1
            notice = {"id": self.notice_seq, "level": level, "title": title,
                      "message": message, "time": time.time()}
            self.notices.append(notice)
        slog({"warning": WARN, "error": ERROR}.get(level, INFO), src,
             f"網頁通知 [{title}] {message.replace(chr(10), ' / ')}")
        self.publish("toast", notice)

    def push_state(self):
        self.publish("state", self.snapshot())

    def add_series_point(self, key, value):
        """記錄溫度／距離曲線的一個時間點（value 為 None 代表讀取失敗，圖上會斷線）。

        只供畫面顯示，與存檔無關；x 軸為自開始監測起的經過秒數。
        """
        with self.lock:
            if self.start_time is None:
                return
            self.series[key].append((time.time() - self.start_time, value))

    def flush_series(self):
        """把上次送出後新增的點批次透過 SSE 推給前端（由計時 thread 每 0.25 秒呼叫）。"""
        with self.lock:
            run_id = self.run_id
            new = {}
            for key, pts in self.series.items():
                sent = self.series_sent[key]
                if len(pts) > sent:
                    new[key] = [[round(t, 2), v] for t, v in pts[sent:]]
                    self.series_sent[key] = len(pts)
        if new:
            self.publish("series", {"run_id": run_id, "points": new})

    # ---------------- 狀態 ----------------
    def _reset_sensor_display(self):
        # 狀態面板的資料來源：每個 key 一個感測器（front end 的 SENSOR_ROWS 以 key 對應；缺少的 key 顯示「未接入」）
        self.sensors = {
            "temp": {"text": "-- °C", "value": None, "level": "idle"},
            "distance": {"text": "-- µm", "value": None, "level": "idle"},
            "audio": {"text": "--", "level": "idle"},
            "spectrometer": {"text": "--", "value": None, "level": "idle"},
        }
        for s in self.sensors.values():
            s.update(reason=R_IDLE, detail="", last_ok=None, fail_count=0)
        self._track = {}
        self._pf_shown = set()

    def _track_sensor(self, key, s):
        """依 set_sensor 後的狀態寫 log（呼叫時已持有 self.lock）。

        節流：只在 (原因, 等級) 改變時寫一行「狀態變化」；同一原因持續失敗每 LOG_SUMMARY_S 秒補一行摘要；
        回到正常時寫失敗持續秒數與期間失敗次數。ok 狀態下重複呼叫（音訊每秒刷新 last_ok）不寫。
        """
        now = time.time()
        t = self._track.setdefault(key, {"reason": None, "level": None, "desc": "初始化中", "fail_start": None,
                                         "fails": 0, "next_summary": 0.0, "last_exc": None})
        name = SENSOR_NAMES.get(key, key)
        reason, level = s["reason"], s["level"]
        if reason in NON_FAILURE_REASONS:
            if reason == R_OK:
                if t["fail_start"] is not None:
                    slog(INFO, name, f"恢復正常（失敗持續 {now - t['fail_start']:.1f} 秒，期間失敗 {t['fails']} 次）")
                    t["fail_start"], t["fails"], t["last_exc"] = None, 0, None
                elif t["reason"] != R_OK:
                    slog(INFO, name, f"狀態：正常（原先為{t['desc']}）")
            t["desc"] = REASON_LABELS.get(reason, reason)
        else:
            t["fails"] = s["fail_count"]
            if t["fail_start"] is None:
                t["fail_start"] = now
            desc = REASON_LABELS.get(reason, reason) + ("（已判定斷線）" if level == "error" and t["level"] == "warn" else "")
            if (reason, level) != (t["reason"], t["level"]):
                slog(ERROR if level == "error" or reason in (R_DRIVER, R_NOT_FOUND, R_ERROR) else WARN, name,
                     f"狀態變化：{t['desc']} → {desc} [{reason}] {s['detail']}")
                t["desc"], t["next_summary"] = desc, now + LOG_SUMMARY_S
            elif now >= t["next_summary"]:
                slog(WARN, name, f"仍在失敗 [{reason}]：累計 {t['fails']} 次，已持續 {now - t['fail_start']:.0f} 秒；{s['detail']}")
                t["next_summary"] = now + LOG_SUMMARY_S
        t["reason"], t["level"] = reason, level

    def _log_exception_once(self, key, exc, what):
        """讀取迴圈的例外：同一種例外訊息只寫一次完整 traceback（恢復後重新計算），避免逐筆洗版。"""
        sig = f"{type(exc).__name__}: {exc}"
        with self.lock:
            t = self._track.setdefault(key, {"reason": None, "level": None, "desc": "初始化中", "fail_start": None,
                                             "fails": 0, "next_summary": 0.0, "last_exc": None})
            if t["last_exc"] == sig:
                return
            t["last_exc"] = sig
        slog(ERROR, SENSOR_NAMES.get(key, key), f"{what}: {sig}", exc)

    def set_sensor(self, key, text=None, value=None, level=None, push=True, reason=None, detail=None):
        """更新單一感測器。level=="ok" 自動視為正常（reason=ok、記錄 last_ok、清除失敗計數）；
        否則帶 reason 的呼叫會記錄原因／說明，並在 reason 屬於失敗類時 fail_count+1
        （idle/init/ok/disabled/stopped 不算失敗）。不帶 reason 的呼叫不動這幾個欄位。
        狀態改變時順便寫 log（_track_sensor）。"""
        with self.lock:
            s = self.sensors[key]
            if text is not None:
                s["text"] = text
            s["value"] = value if key != "audio" else s.get("value")
            if level is not None:
                s["level"] = level
            if level == "ok":
                s.update(reason=R_OK, detail="", last_ok=time.time(), fail_count=0)
                self._pf_shown.discard(key)
            elif reason is not None:
                s["reason"] = reason
                s["detail"] = detail or ""
                if reason not in NON_FAILURE_REASONS:
                    s["fail_count"] += 1
                self._pf_shown.discard(key)
            if level == "ok" or reason is not None:
                self._track_sensor(key, s)
        if push:
            self.push_state()

    def audio_meta(self):
        sr = self.sample_rate
        max_cols = max(1, int(math.ceil(self.history_duration * sr / HOP)))
        return {
            "sample_rate": sr,
            "history_duration": self.history_duration,
            "nfft": NFFT,
            "noverlap": NOVERLAP,
            "hop": HOP,
            "n_freq": NFFT // 2 + 1,
            "freq_max": sr / 2.0,
            "max_cols": max_cols,
        }

    def snapshot(self):
        with self.lock:
            duration = self.duration
            return {
                "phase": self.phase,
                "status_text": self.status_text,
                "enabled": dict(self.enabled),
                "sensors": json.loads(json.dumps(self.sensors)),
                "elapsed": round(self.elapsed, 2),
                "elapsed_int": int(self.elapsed),
                "duration": duration,
                "remaining": (max(0.0, duration - self.elapsed) if duration > 0 else None),
                "params": dict(self.params),
                "last_result": self.last_result,
                "recording": self.recorder.summary() if self.recorder else None,
                "simulated": sorted(self.simulated),
                "simulate_faults": self.sim_faults,
                "spectrogram_job": dict(self.spectrogram_job) if self.spectrogram_job else None,
                "run_id": self.run_id,
                "audio_meta": self.audio_meta(),
                "spec_total": self.spec_total,
                "server_time": time.time(),
            }

    def full_state(self):
        snap = self.snapshot()
        with self.lock:
            snap["series"] = {k: downsample_series(v) for k, v in self.series.items()}
            snap["notices"] = list(self.notices)[-10:]
        snap["spectrum"] = self.spectrum_bulk()
        return snap

    def spectrum_bulk(self):
        """光譜儀的復原資料（只放在 /api/state，不隨 SSE state 事件重送）：波長軸 meta、降採樣歷史、最新完整光譜。

        歷史 bins 以 uint16 小端串接（失敗筆補 0，由 ok 陣列標示），整段 base64。沒有光譜資料時回傳 None。
        """
        with self.lock:
            meta = self.opt_meta
            if meta is None:
                return None
            cols = list(self.opt_cols)
            total = self.opt_total
            latest = self.opt_latest
        zero = bytes(meta["n_bins"] * 2)
        return {
            "meta": meta, "start": total - len(cols), "count": len(cols),
            "t": [c[0] for c in cols], "ok": [0 if c[1] is None else 1 for c in cols],
            "bins": base64.b64encode(b"".join(c[1] or zero for c in cols)).decode("ascii"),
            "latest": ({"t": latest["t"], "full": base64.b64encode(latest["full"]).decode("ascii")}
                       if latest else None),
        }

    def spectrogram_bulk(self):
        """回傳 (起始全域索引, 欄數, n_freq, bytes)。"""
        with self.lock:
            cols = list(self.spec_cols)
            total = self.spec_total
        n_freq = NFFT // 2 + 1
        start = total - len(cols)
        data = np.concatenate(cols).tobytes() if cols else b""
        return start, len(cols), n_freq, data

    # ---------------- 預檢 ----------------
    def check_rangefinder(self):
        """對應 main_csv.start_monitoring 的測距儀檢查。回傳 (ok, 警告訊息, (reason, detail))。"""
        try:
            dev = LKIF2Device(LKIF_DLL_PATH)
            rc = dev.open()
            if rc != RC_OK:
                try:
                    dev.close()
                except Exception:  # noqa: BLE001
                    pass
                return (False, "• 測距儀連接失敗，距離監測將被停用",
                        (R_NOT_FOUND, "LK-G5000 未連線：檢查 USB 線、控制器電源、KEYENCE USB 驅動"))
            dev.close()
            return True, None, (R_OK, "")
        except BaseException as e:  # noqa: BLE001  macOS 上 ctypes.WinDLL 不存在 → AttributeError
            return False, "• 測距儀初始化失敗，距離監測將被停用", classify_rangefinder_error(e)

    def check_spectrometer(self):
        """預檢：延遲 import 驅動並嘗試初始化光譜儀。回傳 (ok, 警告訊息, (reason, detail))。
        ImportError（缺 PyQt5）、OSError（缺 DLL）、找不到裝置都在這裡被分類，不會影響 app 啟動。"""
        try:
            dev = spectrometer_driver.open_spectrometer()
            try:
                dev.close()
            except Exception as e:  # noqa: BLE001
                slog(WARN, SENSOR_NAMES["spectrometer"], f"預檢後關閉光譜儀失敗: {e}")
            return True, None, (R_OK, "")
        except Exception as e:  # noqa: BLE001
            return False, "• 光譜儀初始化失敗，光譜儀監測將被停用", classify_spectrometer_error(e)

    def validate_params(self, raw):
        """對應 main_csv.py L312-318 的參數轉型；失敗丟出 ValueError。"""
        try:
            parsed = {
                # 使用者要求一律手動停止：不提供執行時間，固定為無限（-1）
                "duration": -1,
                "sample_rate": int(str(raw.get("sample_rate", "")).strip()),
                "update_interval": float(str(raw.get("update_interval", "")).strip()),
                "history_duration": float(str(raw.get("history_duration", "")).strip()),
                "distance_interval": float(str(raw.get("distance_interval", "")).strip()),
                "refl_mode": int(str(raw.get("refl_mode", "0")).split("-")[0].strip()),
                "spec_interval": float(str(raw.get("spec_interval", DEFAULT_PARAMS["spec_interval"])).strip()),
                "spec_integration_ms": float(str(raw.get("spec_integration_ms",
                                                         DEFAULT_PARAMS["spec_integration_ms"])).strip()),
            }
        except (TypeError, ValueError) as e:
            raise ValueError(f"參數輸入錯誤: {e}") from None
        # 以下檢查原程式沒有，但數值不合理時會讓 worker 立刻失敗或卡住，因此提前擋下
        if parsed["sample_rate"] <= 0:
            raise ValueError("參數輸入錯誤: 採樣率必須大於 0")
        if parsed["update_interval"] <= 0:
            raise ValueError("參數輸入錯誤: 更新間隔必須大於 0")
        if not 0 < parsed["history_duration"] <= MAX_DISPLAY_SECONDS:
            raise ValueError(f"參數輸入錯誤: 頻譜圖顯示長度必須介於 0 與 {MAX_DISPLAY_SECONDS} 秒之間"
                             "（只影響畫面，存檔一律保存全程）")
        if parsed["distance_interval"] <= 0:
            raise ValueError("參數輸入錯誤: 測距間隔必須大於 0")
        if parsed["refl_mode"] not in (0, 1):
            raise ValueError("參數輸入錯誤: 測距儀模式必須是 0 或 1")
        if parsed["spec_interval"] <= 0:
            raise ValueError("參數輸入錯誤: 光譜儀量測間隔必須大於 0")
        if not 0 < parsed["spec_integration_ms"] <= 10000:
            raise ValueError("參數輸入錯誤: 光譜儀積分時間必須介於 0 與 10000 毫秒之間")
        return parsed

    @staticmethod
    def disk_check():
        """回傳 (剩餘位元組, 每小時估計位元組, 警告或 None)。"""
        os.makedirs(DATA_DIR, exist_ok=True)
        free = shutil.disk_usage(DATA_DIR).free
        per_hour = sum(EST_BYTES_PER_HOUR.values())
        warn = None
        if free < MIN_FREE_BYTES:
            warn = (f"• 存檔磁碟剩餘空間只有 {free / 1024 ** 3:.1f} GB（建議至少 2 GB），"
                    f"以預設設定每小時約需 {per_hour / 1024 ** 2:.0f} MB，約可錄 {free / per_hour:.1f} 小時")
        return free, per_hour, warn

    def preflight(self, raw):
        warnings = []
        enabled = {"temp": True, "audio": True, "distance": True, "spectrometer": True}
        sim = self.simulated
        com = str(raw.get("com_port", "") or "").strip()
        # diag：每個感測器預檢時的 (reason, detail)；被停用者在狀態面板顯示「未啟用」並帶出底層原因
        diag = {k: (R_OK, "") for k in enabled}
        if not com and "temp" not in sim:
            warnings.append("• 未選擇溫度感測器COM端口，溫度監測將被停用")
            enabled["temp"] = False
            diag["temp"] = (R_DISABLED, "未選擇 COM 埠")
        elif com and "temp" not in sim:
            try:  # 只查埠是否存在（不開埠）；找不到時不停用，讓 worker 持續回報並可在插上後自動恢復
                diag["temp"] = check_port_present(com) or (R_OK, "")
            except Exception as e:  # noqa: BLE001
                diag["temp"] = (R_ERROR, f"列舉 COM 埠失敗：{e}")
        audio_sel = str(raw.get("audio_device", "") or "")
        if "audio" not in sim and (not audio_sel or audio_sel in (AUDIO_NONE, AUDIO_FAILED)):
            warnings.append("• 未選擇有效的音訊設備，音訊監測將被停用")
            enabled["audio"] = False
            diag["audio"] = (R_NOT_FOUND if audio_sel == AUDIO_NONE else R_DISABLED,
                             "系統找不到任何音訊輸入設備" if audio_sel == AUDIO_NONE
                             else "音訊設備清單讀取失敗" if audio_sel == AUDIO_FAILED else "未選擇音訊設備")
        if "distance" not in sim:
            ok, msg, diag["distance"] = self.check_rangefinder()
            if not ok:
                warnings.append(msg)
                enabled["distance"] = False
        if "spectrometer" not in sim:
            ok, msg, diag["spectrometer"] = self.check_spectrometer()
            if not ok:
                warnings.append(msg)
                enabled["spectrometer"] = False
        self.preflight_reasons = {k: {"reason": r, "detail": d} for k, (r, d) in diag.items()}
        self._show_preflight(enabled, diag)

        errors = []
        if not any(enabled.values()):
            errors.append("至少需要啟用一個感測器才能開始監測")
        parsed = None
        try:
            parsed = self.validate_params(raw)
        except ValueError as e:
            errors.append(str(e))

        disk = None
        try:
            free, per_hour, disk_warn = self.disk_check()
            disk = {"free_bytes": free, "est_bytes_per_hour": per_hour}
            if disk_warn:
                warnings.append(disk_warn)
        except OSError as e:
            errors.append(f"無法建立存檔資料夾 {DATA_DIR}: {e}")

        for k, (r, d) in diag.items():  # 預檢結果寫 log（含被停用的原因）
            slog(INFO if enabled[k] and r == R_OK else WARN, SENSOR_NAMES[k],
                 f"預檢：{'可用' if enabled[k] else '停用'} [{r}] {REASON_LABELS[r]}" + (f"：{d}" if d else ""))
        for w in warnings:
            slog(WARN, SRC_SYSTEM, "預檢警告：" + w.lstrip("• "))
        for e in errors:
            slog(ERROR, SRC_SYSTEM, "預檢錯誤：" + e)
        slog(INFO if not errors else ERROR, SRC_SYSTEM,
             f"預檢結果：{'通過' if not errors else '未通過'}（啟用 "
             f"{'、'.join(SENSOR_NAMES[k] for k in enabled if enabled[k]) or '無'}；警告 {len(warnings)} 項）")
        return {"ok": not errors, "errors": errors, "warnings": warnings,
                "enabled": enabled, "parsed": parsed, "disk": disk, "diag": self.preflight_reasons}

    def _show_preflight(self, enabled, diag):
        """預檢結果立刻推到狀態面板，讓使用者在確認 dialog 前就看到各感測器為何被停用／可能有問題。
        只在非監測中更新；曾由預檢標示、現在已恢復正常的感測器還原成待機。
        直接改欄位而不走 set_sensor：預檢不算失敗，不累加 fail_count、也不寫狀態變化 log。"""
        with self.lock:
            if self.phase in ("running", "stopping"):
                return
            for k, (r, d) in diag.items():
                s = self.sensors[k]
                if not enabled[k]:
                    s.update(text="未啟用", value=None, level="off", reason=R_DISABLED,
                             detail=self._disabled_detail(SENSOR_NAMES[k], d), fail_count=0)
                    self._pf_shown.add(k)
                elif r != R_OK:  # 仍會啟用，但預檢已看出問題（例如 COM 埠不存在）
                    s.update(text="預檢異常", value=None, reason=r, detail=f"{SENSOR_NAMES[k]}：{d}", fail_count=0,
                             level="error" if r in (R_DRIVER, R_NOT_FOUND, R_ERROR) else "warn")
                    self._pf_shown.add(k)
                elif k in self._pf_shown:
                    s.update(text={"temp": "-- °C", "distance": "-- µm", "audio": "--", "spectrometer": "--"}[k],
                             level="idle", reason=R_IDLE, detail="")
                    self._pf_shown.discard(k)
        self.push_state()

    @staticmethod
    def _disabled_detail(name, detail):
        """「未啟用」的說明：帶出底層分類原因（例如 距離：此系統不是 Windows…）。"""
        return f"{name}：{detail}" if detail else f"{name}未啟用"

    # ---------------- 開始 ----------------
    def start(self, raw, confirmed):
        with self.lock:
            if self.phase in ("running", "stopping"):
                return {"ok": False, "errors": ["監測已在進行中"]}, 409
        pf = self.preflight(raw)
        if not pf["ok"]:
            return {"ok": False, "errors": pf["errors"], "warnings": pf["warnings"]}, 400
        if pf["warnings"] and not confirmed:
            return {"ok": False, "needs_confirm": True, "warnings": pf["warnings"],
                    "enabled": pf["enabled"]}, 409

        p = pf["parsed"]
        enabled = pf["enabled"]
        start_ts = time.time()
        experiment_id = "EXP_" + datetime.datetime.fromtimestamp(start_ts).strftime("%Y%m%d_%H%M%S")
        while os.path.exists(os.path.join(DATA_DIR, experiment_id)):  # 同一秒內重新開始時避免撞名
            start_ts += 1
            experiment_id = "EXP_" + datetime.datetime.fromtimestamp(start_ts).strftime("%Y%m%d_%H%M%S")
        com_port = str(raw.get("com_port") or "").strip()
        audio_sel = str(raw.get("audio_device") or "")
        info = {
            "experiment_id": experiment_id,
            "status": "running",
            "start_time": iso_now(start_ts),
            "start_time_unix": start_ts,
            "end_time": None,
            "duration_s": None,
            "stop_reason": None,
            "parameters": {
                "sample_rate": p["sample_rate"], "update_interval_s": p["update_interval"],
                "display_history_s": p["history_duration"], "distance_interval_s": p["distance_interval"],
                "refl_mode": REFL_MODE_LABELS[p["refl_mode"]],
                "nfft": NFFT, "noverlap": NOVERLAP,
                "spec_interval_s": p["spec_interval"], "spec_integration_ms": p["spec_integration_ms"],
            },
            "enabled": dict(enabled),
            # 模擬資料絕不能被誤認為真實量測：這裡列出所有模擬來源
            "simulated": sorted(k for k in self.simulated if enabled.get(k)),
            "simulate_faults": self.sim_faults,
            "devices": {
                "temperature_com_port": (("模擬（sim_devices.SimTemperature）" if "temp" in self.simulated
                                          else com_port) if enabled["temp"] else None),
                "audio_device": (("模擬（sim_devices.SimAudioRecorder）" if "audio" in self.simulated
                                  else audio_sel) if enabled["audio"] else None),
                "rangefinder": (("模擬（sim_devices.SimLKIF2Device）" if "distance" in self.simulated
                                 else "KEYENCE LK-G5000 (LKIF2.dll)") if enabled["distance"] else None),
                "spectrometer": (("模擬（sim_devices.SimSpectrometer）" if "spectrometer" in self.simulated
                                  else "AvaSpec-ULS2048L (avaspecx64.dll)") if enabled["spectrometer"] else None),
            },
            "platform": {"os": platform.platform(), "python": platform.python_version()},
            "spectrogram": None,
            "spectrometer": None,  # 光譜儀 worker 開啟裝置後補上 metadata（波長範圍、像素數、積分時間…）
        }
        recorder = None
        try:
            recorder = ExperimentRecorder(experiment_id, info)
            recorder.on_write_error = self._on_write_error
            recorder.attach_log()
            if info["simulated"]:
                with open(recorder.path("SIMULATED.txt"), "w", encoding="utf-8-sig") as f:
                    f.write("本實驗資料夾含模擬資料，不是真實量測！\n"
                            "THIS FOLDER CONTAINS SIMULATED DATA, NOT REAL MEASUREMENTS.\n"
                            f"模擬來源 / simulated: {', '.join(info['simulated'])}\n"
                            f"故障模擬 / faults: {self.sim_faults}\n")
            if enabled["temp"]:
                recorder.open_csv("temperature", f"temperature_{experiment_id}.csv",
                                  ["Timestamp", "Elapsed(s)", "Temperature(C)", "Status"], encoding="utf-8-sig")
            if enabled["distance"]:
                recorder.open_csv("distance", f"distance_{experiment_id}.csv",
                                  ["Timestamp", "Elapsed(s)", "Absolute(µm)", "Relative(µm)", "Status"], encoding="utf-8-sig")
            # 光譜儀 CSV 的檔頭要等裝置開啟、讀到波長軸後才寫（由 _spectrometer_worker 處理）
        except OSError as e:
            slog(ERROR, SRC_SYSTEM, f"無法建立實驗資料夾或檔案: {e}", e)
            if recorder:
                recorder.close_log()
            return {"ok": False, "errors": [f"無法建立實驗資料夾或檔案: {e}"]}, 500

        with self.lock:
            if self.phase in ("running", "stopping"):
                recorder.close_all()
                recorder.close_log()
                return {"ok": False, "errors": ["監測已在進行中"]}, 409
            self.run_id += 1
            self.recorder = recorder
            self.spectrogram_job = None
            self.write_failures = set()
            self.params = {k: str(raw.get(k, DEFAULT_PARAMS[k])) for k in DEFAULT_PARAMS}
            self.enabled = enabled
            self.duration = p["duration"]
            self.sample_rate = p["sample_rate"]
            self.history_duration = p["history_duration"]
            self.spec_pending = np.array([], dtype=np.int16)
            self.spec_cols = collections.deque(maxlen=self.audio_meta()["max_cols"])
            self.spec_total = 0
            self.spec_freqs = None
            self.series = {"temp": [], "distance": []}
            self.series_sent = {"temp": 0, "distance": 0}
            self.opt_meta = None
            self.opt_cols = collections.deque(maxlen=self._opt_max_cols(p["spec_interval"], p["history_duration"]))
            self.opt_total = 0
            self.opt_latest = None
            self.zero_warned = False
            self.last_result = None
            self.elapsed = 0.0
            self.start_time = start_ts
            self.stop_event = threading.Event()
            self.finalizing = False
            self._reset_sensor_display()
            parts = [name for key, name in (("temp", "溫度"), ("audio", "音訊"), ("distance", "距離"),
                                     ("spectrometer", "光譜"))
                     if enabled[key]]
            self.status_text = f"監測中 ({'+'.join(parts)})"
            self.phase = "running"
            for key, name in SENSOR_NAMES.items():
                if not enabled[key]:
                    r = pf["diag"][key]
                    self.sensors[key].update(
                        text="未啟用", level="off", reason=R_DISABLED,
                        detail=self._disabled_detail(name, r["detail"]))
            if enabled["audio"]:
                self.sensors["audio"].update(text="初始化中", level="idle", reason=R_INIT,
                                             detail="正在開啟音訊設備")
            stop_event = self.stop_event
            run_id = self.run_id
        if self.simulated:
            self.sim_temp = sim_devices.SimTemperature(faults=self.sim_faults)

        self._log_start(experiment_id, recorder, info, p, raw, pf, com_port, audio_sel)
        self.publish("reset", {"run_id": run_id, "audio_meta": self.audio_meta()})
        self.push_state()

        threads = {"timer": threading.Thread(target=self._timer_worker,
                                             args=(stop_event, p["duration"]), name="timer", daemon=True)}
        if enabled["temp"]:
            threads["temp"] = threading.Thread(target=self._temp_worker,
                                               args=(stop_event, com_port, recorder),
                                               name="temp", daemon=True)
        if enabled["audio"]:
            threads["audio"] = threading.Thread(
                target=self._audio_worker,
                args=(stop_event, -1 if "audio" in self.simulated else parse_audio_device_index(audio_sel),
                      p["sample_rate"], p["update_interval"], recorder),
                name="audio", daemon=True)
        if enabled["distance"]:
            threads["distance"] = threading.Thread(target=self._rangefinder_worker,
                                                   args=(stop_event, p["distance_interval"], p["refl_mode"],
                                                         recorder),
                                                   name="rangefinder", daemon=True)
        if enabled["spectrometer"]:
            threads["spectrometer"] = threading.Thread(
                target=self._spectrometer_worker,
                args=(stop_event, p["spec_interval"], p["spec_integration_ms"], p["history_duration"], recorder),
                name="spectrometer", daemon=True)
        with self.lock:
            self.threads = threads
        for t in threads.values():
            t.start()
        return {"ok": True, "state": self.snapshot()}, 200

    def _log_start(self, experiment_id, recorder, info, p, raw, pf, com_port, audio_sel):
        """開始監測的完整紀錄：實驗 id、各感測器啟用狀態與裝置、所有參數。"""
        enabled = pf["enabled"]
        slog(INFO, SRC_SYSTEM, f"開始監測 {experiment_id}；資料夾 {recorder.rel_dir()}；"
             f"啟用 {'、'.join(SENSOR_NAMES[k] for k in SENSOR_NAMES if enabled[k])}")
        slog(INFO, SRC_SYSTEM, f"參數：{json.dumps(info['parameters'], ensure_ascii=False)}；"
             f"原始輸入 {json.dumps({k: str(raw.get(k, '')) for k in DEFAULT_PARAMS}, ensure_ascii=False)}")
        slog(INFO, SRC_SYSTEM, f"平台 {info['platform']['os']}，Python {info['platform']['python']}"
             + (f"；模擬 {','.join(info['simulated'])}{'（含故障模擬）' if self.sim_faults else ''}"
                if info["simulated"] else ""))
        dev = info["devices"]
        for key, label in (("temp", dev["temperature_com_port"]), ("audio", dev["audio_device"]),
                           ("distance", dev["rangefinder"]), ("spectrometer", dev["spectrometer"])):
            if enabled[key]:
                slog(INFO, SENSOR_NAMES[key], f"啟用：{label}")
            else:
                r = pf["diag"][key]
                slog(WARN, SENSOR_NAMES[key], f"未啟用 [{r['reason']}] {r['detail']}")
        if enabled["temp"] and "temp" not in self.simulated:
            slog(INFO, SENSOR_NAMES["temp"], f"COM 埠 {com_port}，57600 8N1，每 1 秒讀一次")
        if enabled["audio"]:
            slog(INFO, SENSOR_NAMES["audio"], f"設備選擇 {audio_sel or '(模擬)'}，採樣率 {p['sample_rate']} Hz，"
                 f"更新間隔 {p['update_interval']} 秒")
        if enabled["distance"]:
            slog(INFO, SENSOR_NAMES["distance"], f"間隔 {p['distance_interval']} 秒，"
                 f"反射模式 {REFL_MODE_LABELS[p['refl_mode']]}，基準 {BASIC_REF} mm")
        if enabled["spectrometer"]:
            slog(INFO, SENSOR_NAMES["spectrometer"], f"量測間隔 {p['spec_interval']} 秒，"
                 f"積分時間 {p['spec_integration_ms']:g} ms，平均 1 次，軟體觸發")

    def _on_write_error(self, key, exc):
        """ExperimentRecorder 寫檔失敗（例如磁碟已滿）時呼叫；每個檔案只通知一次。"""
        with self.lock:
            first = key not in self.write_failures
            self.write_failures.add(key)
        if first:
            self.notify("error", "寫檔失敗",
                        f"{key} 資料寫入失敗（磁碟可能已滿或被拔除）：{exc}\n監測仍在進行，請盡快釋放空間或停止監測。")

    # ---------------- 停止與存檔 ----------------
    def request_stop(self, reason="manual"):
        """非阻塞：切換到 stopping，交給 finalizer thread 等待 worker 結束並存檔。"""
        with self.lock:
            if self.phase != "running" or self.finalizing:
                return False
            self.finalizing = True
            slog(INFO, SRC_SYSTEM, f"收到停止請求：{STOP_REASONS.get(reason, reason)}")
            self.phase = "stopping"
            self.status_text = "停止中（儲存資料）"
            self.stop_event.set()
        self.push_state()
        threading.Thread(target=self._finalize, args=(reason,), name="finalize", daemon=True).start()
        return True

    def _finalize(self, reason):
        with self.lock:
            threads = dict(self.threads)
            recorder = self.recorder
        for name, t in threads.items():
            if t is threading.current_thread():
                continue
            # 音訊 worker 可能正卡在 record_audio(update_interval) 內，給足時間讓它自行關閉串流
            t.join(timeout=5 if name in ("audio", "distance", "spectrometer") else 2)
            if t.is_alive():
                slog(WARN, SRC_SYSTEM, f"{name} 執行緒未在時限內結束")
                if recorder:
                    recorder.add_error(f"{name} 執行緒未在時限內結束")
        try:
            self._save_all(recorder, reason)
        except Exception as e:  # noqa: BLE001
            slog(ERROR, SRC_SYSTEM, f"存檔流程錯誤: {e}", e)
            self.notify("warning", "警告", f"數據儲存失敗: {e}")
            if recorder:
                recorder.add_error(f"存檔流程錯誤: {e}")
        slog(INFO, SRC_SYSTEM, "停止流程結束，實驗 log 關閉")
        if recorder:
            recorder.close_log()  # 先關實驗 log 再切到 stopped，避免下一次開始時兩份 log 重疊
        with self.lock:
            self.phase = "stopped"
            self.status_text = "已停止"
            self.finalizing = False
            self.threads = {}
            # 溫度/距離保留最後讀值（同 main_csv）；音訊「錄音中」在停止後會誤導，改為已停止
            if self.enabled["audio"]:
                self.sensors["audio"]["text"] = "已停止"
                self.sensors["audio"]["level"] = "off"
            for key, s in self.sensors.items():
                # 正常/待機/初始化中的感測器標為已停止；失敗中的保留最後的具體原因供事後查看
                if self.enabled.get(key) and s["reason"] in (R_OK, R_IDLE, R_INIT):
                    s["reason"], s["detail"] = R_STOPPED, "監測已停止"
        self.push_state()

    def _save_all(self, recorder, reason):
        if recorder is None:
            return
        # 1) 先關閉所有串流檔案（worker 正常結束時已各自關閉，這裡保險再關一次）並寫出結束資訊
        recorder.close_all()
        end_ts = time.time()
        with self.lock:
            start_ts = self.start_time
            sr = self.sample_rate
        recorder.update(status="raw_saved", end_time=iso_now(end_ts), end_time_unix=end_ts,
                        duration_s=round(end_ts - start_ts, 3), stop_reason=STOP_REASONS.get(reason, reason))
        exp_id = recorder.experiment_id
        messages = [f"資料夾: {recorder.rel_dir()}"]
        summary = {f["key"]: f for f in recorder.summary()["files"]}
        slog(INFO, SRC_SYSTEM, f"原始資料已關檔，實際時長 {end_ts - start_ts:.1f} 秒")
        with self.lock:
            for key, t in self._track.items():  # 停止時仍在失敗的感測器：留下最後的原因
                if t["fail_start"] is not None:
                    s = self.sensors[key]
                    slog(WARN, SENSOR_NAMES.get(key, key), f"停止時仍在失敗 [{s['reason']}]："
                         f"持續 {end_ts - t['fail_start']:.1f} 秒，累計 {t['fails']} 次；{s['detail']}")
        for csv_key, label in (("temperature", "溫度"), ("distance", "距離"), ("spectrometer", "光譜儀")):
            if csv_key in summary:
                f = summary[csv_key]
                messages.append(f"{label} {f['rows']} 筆")
                slog(INFO, label, f"存檔 {f['name']}：{f['rows']} 筆，其中失敗 {f.get('fail_rows', 0)} 筆，{f['bytes']} bytes"
                     + (f"；寫檔失敗：{f['write_error']}" if f.get("write_error") else ""))

        # 2) 由完整 WAV 分塊產生頻譜 CSV（格式與 save_spectrogram_to_csv 相同）
        audio = summary.get("audio")
        if audio and audio.get("frames", 0) > 0:
            messages.append(f"音訊 {audio['seconds']:.1f} 秒")
            slog(INFO, "音訊", f"存檔 {audio['name']}：{audio['seconds']:.1f} 秒，{audio['frames']} frames，"
                 f"{audio['channels']} ch，{audio['sample_rate']} Hz，{audio['bytes']} bytes"
                 + (f"；寫檔失敗：{audio['write_error']}" if audio.get("write_error") else ""))
            wav_path = recorder.path(audio["name"])
            n_cols = chunked_spectrogram.n_spec_columns(audio["frames"] * audio["channels"])
            if n_cols >= 2:
                csv_name = f"spectrogram_{exp_id}.csv"
                # 以相對路徑呼叫（main() 已 chdir 到專案目錄），_metadata.txt 的 Data File 欄位才與原程式相同
                csv_rel = os.path.relpath(recorder.path(csv_name), BASE_DIR)
                self._set_spec_job("running", 0.0)
                last = [0.0]

                def progress(frac):
                    if frac - last[0] >= 0.02 or frac >= 1:
                        last[0] = frac
                        self._set_spec_job("running", frac)

                try:
                    r = chunked_spectrogram.save_spectrogram_csv_from_wav(
                        wav_path, csv_rel, exp_id, sample_rate=sr, progress=progress)
                    recorder.add_file("spectrogram", csv_name, rows=r["n_times"], freq_bins=r["n_freqs"])
                    recorder.add_file("spectrogram_metadata", os.path.basename(r["metadata"]))
                    recorder.update(spectrogram={"status": "done", "rows": r["n_times"],
                                                 "seconds_to_generate": round(r["seconds"], 2)})
                    self._set_spec_job("done", 1.0)
                    messages.append(f"數據已儲存至: {csv_name}")
                    slog(INFO, "音訊", f"頻譜 CSV 完成 {csv_name}：{r['n_times']} 列 × {r['n_freqs']} 頻率點，"
                         f"耗時 {r['seconds']:.1f} 秒")
                except Exception as e:  # noqa: BLE001
                    slog(ERROR, "音訊", f"頻譜 CSV 產生失敗: {e}", e)
                    recorder.update(spectrogram={"status": "error", "error": str(e)})
                    self._set_spec_job("error", None, str(e))
                    self.notify("warning", "頻譜 CSV 產生失敗",
                                f"{e}\n原始音訊已保存於 {audio['name']}，可稍後執行：\n"
                                f"uv run python app/chunked_spectrogram.py {recorder.rel_dir()}")
            else:
                recorder.update(spectrogram={"status": "skipped", "reason": "音訊太短"})
                slog(WARN, "音訊", "音訊太短，略過頻譜 CSV")
        elif self.enabled.get("audio"):
            self.notify("warning", "警告", "沒有錄製到音訊數據", src="音訊")

        recorder.update(status="completed")
        with self.lock:
            self.last_result = {
                "experiment_id": exp_id,
                "dir": recorder.rel_dir(),
                "files": [f["name"] for f in recorder.summary()["files"]],
                "time": time.time(),
            }
        self.notify("success", "完成", "監測完成！\n" + "\n".join(messages))

    def _set_spec_job(self, status, progress, error=None):
        with self.lock:
            self.spectrogram_job = {"status": status, "progress": progress, "error": error}
            if status == "running":
                pct = int(round((progress or 0) * 100))
                self.status_text = f"頻譜 CSV 產生中… {pct}%"
        self.push_state()

    # ---------------- Workers ----------------
    def _timer_worker(self, stop_event, duration):
        """對應 main_csv 的執行時間顯示與 duration 自動停止（音訊與 timer-only 共用）。"""
        last_push = 0.0
        last_rec = 0.0
        while not stop_event.is_set():
            elapsed = time.time() - self.start_time
            with self.lock:
                self.elapsed = elapsed
            if duration > 0 and elapsed >= duration:
                with self.lock:
                    self.elapsed = float(duration)
                self.request_stop("timeout")
                return
            now = time.time()
            if now - last_push >= 0.25:
                last_push = now
                tick = {"elapsed": round(elapsed, 2), "elapsed_int": int(elapsed), "duration": duration}
                if now - last_rec >= 1.0 and self.recorder is not None:
                    last_rec = now
                    tick["recording"] = self.recorder.summary()  # 讓前端即時看到檔案大小／筆數
                self.publish("tick", tick)
                self.flush_series()
            stop_event.wait(0.1)
        self.flush_series()

    def _temp_worker(self, stop_event, com_port, recorder):
        """對應 main_csv.start_temperature_monitoring（每 1 秒一次）；每筆讀值（含失敗）寫入 temperature CSV。"""
        consecutive_failures = 0
        disconnected = False
        read_temp = self.sim_temp.read_diag if "temp" in self.simulated else read_temperature_diag

        def log(value, status):
            now = time.time()
            recorder.write_row("temperature", [iso_now(now), f"{now - self.start_time:.3f}",
                                               "" if value is None else f"{float(value):.3f}", status],
                               ok=status == "ok")

        try:
            while not stop_event.is_set():
                try:
                    temp, reason, detail = read_temp(com_port)
                    if temp is not None:
                        consecutive_failures = 0
                        if disconnected:
                            disconnected = False
                            self.notify("info", "通知", "溫度感測器已重新連接", src="溫度")
                        log(temp, "ok")
                        self.add_series_point("temp", round(float(temp), 2))
                        self.set_sensor("temp", text=f"{temp:.1f} °C", value=round(float(temp), 1), level="ok")
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_FAILURES and not disconnected:
                            disconnected = True
                            self.notify("warning", "警告", "溫度感測器可能已斷線，監測將繼續但不會讀取溫度數據",
                                        src="溫度")
                        if disconnected:
                            log(None, "感測器斷線")
                            self.add_series_point("temp", None)
                            self.set_sensor("temp", text="感測器斷線", value=None, level="error",
                                            reason=reason, detail=detail)
                        else:
                            log(None, "讀取失敗")
                            self.add_series_point("temp", None)
                            self.set_sensor("temp", text="讀取失敗", value=None, level="warn",
                                            reason=reason, detail=detail)
                except Exception as e:  # noqa: BLE001
                    self._log_exception_once("temp", e, "溫度讀取錯誤")
                    consecutive_failures += 1
                    if consecutive_failures == MAX_FAILURES and not disconnected:
                        disconnected = True
                        self.notify("warning", "警告", f"溫度感測器錯誤: {e}\n監測將繼續但不會讀取溫度數據",
                                    src="溫度")
                    log(None, "連接錯誤")
                    self.add_series_point("temp", None)
                    self.set_sensor("temp", text="連接錯誤", value=None,
                                    level="error" if disconnected else "warn",
                                    reason=R_ERROR, detail=f"讀取溫度時發生例外：{e}")
                stop_event.wait(1)
        except BaseException as e:  # noqa: BLE001  worker 意外死掉也要留下紀錄並關檔
            slog(ERROR, "溫度", f"溫度 worker 異常結束: {e}", e)
            recorder.add_error(f"溫度 worker 異常結束: {e}")
            self.notify("error", "溫度監測中斷", f"溫度 worker 異常結束: {e}", src="溫度")
        finally:
            recorder.close("temperature")

    def _audio_worker(self, stop_event, device_index, sample_rate, update_interval, recorder):
        """對應 main_csv.start_audio_monitoring。每段錄音（含逾時補零的片段）都寫入 WAV 保持時間對齊。"""
        audio_rec = None
        init_diag = None  # 初始化階段失敗時的 (reason, detail)
        try:
            if device_index is None:
                init_diag = diagnose_audio_device(None)
                raise Exception("無效的音訊設備")
            recorder_cls = sim_devices.SimAudioRecorder if "audio" in self.simulated else AudioRecorder
            sim_kw = {"faults": self.sim_faults} if "audio" in self.simulated else {}
            try:
                audio_rec = recorder_cls(sample_rate=sample_rate, channels=None, chunk=1024,
                                         verbose=True, device_index=device_index, **sim_kw)
            except SystemExit as e:
                # AudioRecorder 找不到/打不開設備時會 sys.exit(1)，在此轉成錯誤事件，並直接查 pyaudio 找出原因
                init_diag = diagnose_audio_device(device_index)
                raise RuntimeError(f"音訊錄製器初始化失敗（AudioRecorder 結束碼 {e.code}），"
                                   "請檢查設備是否被佔用或麥克風權限") from None
            exp_id = recorder.experiment_id
            recorder.open_wav("audio", f"audio_{exp_id}.wav", audio_rec.channels, sample_rate)
            recorder.update(audio={"device_index": audio_rec.device_index, "channels": audio_rec.channels,
                                   "sample_rate": sample_rate, "sample_format": "int16"})
            slog(INFO, "音訊", f"初始化完成：設備 {audio_rec.device_index}，{audio_rec.channels} 聲道，{sample_rate} Hz，"
                 f"WAV audio_{exp_id}.wav")
            self.set_sensor("audio", text=f"錄音中（設備 {audio_rec.device_index}，{audio_rec.channels} 聲道）",
                            level="ok")
            error_notified = False
            zero_seconds = 0.0
            last_ok_push = time.time()

            while not stop_event.is_set():
                try:
                    chunk = audio_rec.record_audio(update_interval)
                    if chunk is not None and len(chunk) > 0:
                        recorder.write_audio("audio", chunk)
                        self._ingest_audio(chunk, sample_rate)
                        if not np.any(chunk):
                            zero_seconds += len(chunk) / sample_rate
                        else:
                            zero_seconds = 0.0
                        if zero_seconds >= 3 and not self.zero_warned:
                            self.zero_warned = True
                            self.notify("warning", "警告", "連續 3 秒錄到的音訊全為 0，"
                                        "可能是麥克風權限未開啟或設備靜音", src="音訊")
                            self.set_sensor("audio", text="訊號全為 0", level="warn", reason=R_NO_DATA,
                                            detail="訊號全為 0：麥克風權限未開、靜音或未接")
                        elif zero_seconds == 0 and self.zero_warned:
                            self.zero_warned = False
                            self.set_sensor("audio", text="錄音中", level="ok")
                        elif zero_seconds == 0 and not error_notified and time.time() - last_ok_push >= 1:
                            # 正常錄音時每秒刷新一次 last_ok，狀態面板的「最後正常」才不會一直累加
                            last_ok_push = time.time()
                            self.set_sensor("audio", level="ok")
                    if error_notified:
                        error_notified = False
                        self.notify("info", "通知", "音訊設備已恢復正常", src="音訊")
                        self.set_sensor("audio", text="錄音中", level="ok")
                except Exception as audio_error:  # noqa: BLE001
                    self._log_exception_once("audio", audio_error, "音訊錄製錯誤")
                    if not error_notified:
                        error_notified = True
                        recorder.add_error(f"音訊錄製錯誤: {audio_error}")
                        self.notify("warning", "警告",
                                    f"音訊錄製出現錯誤: {audio_error}\n監測將繼續但音訊數據可能不完整", src="音訊")
                    # 每次失敗都更新（累加 fail_count）；text/level 與原本相同
                    self.set_sensor("audio", text="錄製錯誤", level="warn", reason=R_ERROR,
                                    detail=f"錄音過程發生錯誤：{audio_error}")
                    stop_event.wait(update_interval)
        except BaseException as e:  # noqa: BLE001
            slog(ERROR, "音訊", f"音訊監測錯誤: {e}", e)
            recorder.add_error(f"音訊監測錯誤: {e}")
            self.notify("warning", "警告", f"音訊監測出現問題: {e}\n監測將繼續但不會有音訊數據", src="音訊")
            reason, detail = init_diag or (R_ERROR, f"音訊初始化失敗：{e}")
            self.set_sensor("audio", text="初始化失敗", level="error", reason=reason, detail=detail)
        finally:
            recorder.close("audio")  # 補寫 WAV header 並關檔
            if audio_rec is not None:
                try:
                    audio_rec.close()
                except BaseException as e:  # noqa: BLE001
                    slog(WARN, "音訊", f"關閉音訊錄音器錯誤: {e}", e)

    def _ingest_audio(self, chunk, sample_rate):
        """只供畫面顯示：頻譜圖新欄（有上限的 deque）。存檔由 WAV 負責。"""
        chunk = np.asarray(chunk, dtype=np.int16)
        with self.lock:
            buf = np.concatenate((self.spec_pending, chunk))

        # 頻譜圖：增量計算新樣本產生的 specgram 欄（NFFT=256、noverlap=128）
        cols_payload = None
        if len(buf) >= NFFT:
            n_cols = (len(buf) - NFFT) // HOP + 1
            used = (n_cols - 1) * HOP + NFFT
            spec, sfreqs, _ = mlab.specgram(buf[:used], NFFT=NFFT, Fs=sample_rate, noverlap=NOVERLAP)
            q = spec_to_int16_db(spec)
            with self.lock:
                start_index = self.spec_total
                for row in q:
                    self.spec_cols.append(row)
                self.spec_total += q.shape[0]
                self.spec_freqs = sfreqs
                self.spec_pending = buf[n_cols * HOP:]
            cols_payload = {"start": start_index, "count": int(q.shape[0]),
                            "b64": base64.b64encode(q.tobytes()).decode("ascii")}
        else:
            with self.lock:
                self.spec_pending = buf

        with self.lock:
            run_id = self.run_id
        self.publish("audio", {"run_id": run_id, "spec": cols_payload})

    def _rangefinder_worker(self, stop_event, interval, refl_mode, recorder):
        """對應 main_csv.start_rangefinder_monitoring；每次讀取（含無效值與錯誤）都即時寫入 distance CSV。

        Status 為儀器回傳的 FloatResult（VALID / +RANGEOVER / -RANGEOVER / WAITING / ALARM / INVALID），
        讀取例外時為 "ERROR: <訊息>"；非 VALID 的列 Absolute/Relative 留空，方便事後以 Timestamp 對齊。
        """
        device = None
        t0 = None  # Elapsed(s) 以第一次讀取（不論是否有效）為 0

        def log(status, rel=None):
            nonlocal t0
            now = time.time()
            if t0 is None:
                t0 = now
            if rel is None:
                recorder.write_row("distance", [now, f"{now - t0:.3f}", "", "", status], ok=False)
            else:
                recorder.write_row("distance", [now, f"{now - t0:.3f}", f"{(BASIC_REF + rel) * 1000:.3f}", f"{rel * 1000:.3f}", status])

        try:
            if "distance" in self.simulated:
                device = sim_devices.SimLKIF2Device(faults=self.sim_faults)
            else:
                device = LKIF2Device(LKIF_DLL_PATH)
            rc = device.open()
            if rc != RC_OK:
                raise RuntimeError(f"測距儀開啟失敗: 0x{rc:X}")
            # initialize_rangefinder（main_csv.py L618）
            device.stop_measure()
            slog(INFO, "距離", "ABLE 校正中...")
            device.dll.LKIF2_SetAbleMode(OUT_NO, LKIF_ABLEMODE_AUTO)
            device.dll.LKIF2_AbleStart(OUT_NO)
            stop_event.wait(2)
            device.dll.LKIF2_AbleStop()
            slog(INFO, "距離", "Auto-zero")
            device.stop_measure()
            device.dll.LKIF2_SetZeroSingle(OUT_NO, 1)
            slog(INFO, "距離", "測距儀初始化完成（零點與取樣參數已設定）")
            # 參數設定
            device.stop_measure()
            device.dll.LKIF2_SetSamplingCycle(OUT_NO, SAMPLING_US)
            device.dll.LKIF2_SetRange(OUT_NO, RANGE_CODE)
            device.dll.LKIF2_SetReflectionMode(OUT_NO, refl_mode)
            device.dll.LKIF2_SetBasicPoint(OUT_NO, 0)
            device.start_measure()
            slog(INFO, "距離", f"開始量測：取樣 {SAMPLING_US} µs，反射模式 {REFL_MODE_LABELS[refl_mode]}，讀取間隔 {interval} 秒")

            consecutive_failures = 0
            disconnected = False
            while not stop_event.is_set():
                try:
                    d = device.read_single(OUT_NO)
                    if d["FloatResult"] == "VALID":
                        rel = d["Value"]
                        absolute = (BASIC_REF + rel) * 1000  # µm
                        log("VALID", rel)
                        self.add_series_point("distance", round(float(absolute), 3))
                        consecutive_failures = 0
                        if disconnected:
                            disconnected = False
                            self.notify("info", "通知", "測距儀已重新連接", src="距離")
                        self.set_sensor("distance", text=f"{absolute:.1f} µm",
                                        value=round(float(absolute), 1), level="ok")
                    else:
                        status = d["FloatResult"]
                        log(f"Unknown({d['RawStatus']})" if status == "Unknown" else status)
                        self.add_series_point("distance", None)
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_FAILURES and not disconnected:
                            disconnected = True
                            self.notify("warning", "警告", "測距儀可能已斷線，監測將繼續但不會讀取距離數據",
                                        src="距離")
                        reason, detail = RANGEFINDER_STATUS_DIAG.get(
                            status, (R_ERROR, f"量測值無效（{status}）：檢查目標表面與感測頭設定"))
                        if disconnected:
                            self.set_sensor("distance", text="感測器斷線", value=None, level="error",
                                            reason=reason, detail=detail)
                        else:
                            self.set_sensor("distance", text="讀取失敗", value=None, level="warn",
                                            reason=reason, detail=detail)
                except Exception as e:  # noqa: BLE001
                    self._log_exception_once("distance", e, "測距儀讀取錯誤")
                    log(f"ERROR: {e}")
                    self.add_series_point("distance", None)
                    consecutive_failures += 1
                    if consecutive_failures == MAX_FAILURES and not disconnected:
                        disconnected = True
                        self.notify("warning", "警告", f"測距儀錯誤: {e}\n監測將繼續但不會讀取距離數據", src="距離")
                    self.set_sensor("distance", text="連接錯誤", value=None,
                                    level="error" if disconnected else "warn",
                                    reason=R_ERROR, detail=f"通訊錯誤：USB 可能中斷（{e}）")
                stop_event.wait(interval)
        except BaseException as e:  # noqa: BLE001
            slog(ERROR, "距離", f"測距儀監測錯誤: {e}", e)
            recorder.add_error(f"測距儀監測錯誤: {e}")
            self.notify("warning", "警告", f"測距儀監測出現問題: {e}\n監測將繼續但不會有距離數據", src="距離")
            reason, detail = classify_rangefinder_error(e)
            self.set_sensor("distance", text="初始化失敗", value=None, level="error",
                            reason=reason, detail=detail)
        finally:
            recorder.close("distance")
            if device is not None:
                try:
                    device.close()
                except BaseException:  # noqa: BLE001
                    pass

    @staticmethod
    def _opt_max_cols(interval, history_duration):
        """光譜熱圖歷史保留筆數：顯示時間窗（history_duration）內的筆數，上限 SPEC_HISTORY_MAX。"""
        return min(SPEC_HISTORY_MAX, max(2, math.ceil(history_duration / interval) + 1))

    def _ingest_spectrum(self, t, counts):
        """只供畫面顯示：把一筆光譜（counts 為 None 代表讀取失敗）降採樣後存進歷史並以 SSE 推送。
        存檔由 spectrometer CSV 負責。"""
        with self.lock:
            meta = self.opt_meta
        if meta is None:
            return
        if counts is None:
            bins = full = None
        else:
            b, f = bin_spectrum_max(counts, meta["n_bins"])
            bins, full = b.tobytes(), f.tobytes()
        t = round(t, 2)
        with self.lock:
            idx = self.opt_total
            self.opt_total += 1
            self.opt_cols.append((t, bins))
            if full is not None:
                self.opt_latest = {"t": t, "full": full}
            run_id = self.run_id
        def enc(b):
            return None if b is None else base64.b64encode(b).decode("ascii")

        self.publish("spectrum", {"run_id": run_id, "idx": idx, "t": t, "bins": enc(bins), "full": enc(full)})

    def _spectrometer_worker(self, stop_event, interval, integration_ms, history_duration, recorder):
        """光譜儀：每 interval 秒量測一次；每筆（含失敗）即時寫入 spectrometer CSV。

        CSV：第一行 Timestamp,Elapsed(s),Status 後接各像素波長（nm，3 位小數）；之後每筆一列，強度為原始 counts
        （未扣暗光）。失敗列強度欄留空、Status 寫「原因代碼: 訊息」。連續失敗 MAX_FAILURES 次判定斷線，恢復時通知。
        """
        device = None
        n = 0  # 像素數（CSV 欄數）

        def log(counts, status):
            now = time.time()
            elapsed = f"{now - self.start_time:.3f}"
            if counts is None:
                recorder.write_row("spectrometer", [iso_now(now), elapsed, status] + [""] * n, ok=False)
            else:
                recorder.write_row("spectrometer", [iso_now(now), elapsed, status] + [f"{v:.2f}" for v in counts])

        try:
            if "spectrometer" in self.simulated:
                device = sim_devices.SimSpectrometer(faults=self.sim_faults)
            else:
                device = spectrometer_driver.open_spectrometer()  # 延遲 import；缺 PyQt5／DLL／裝置都在這裡丟例外
            wl = np.asarray(device.wavelength, dtype=float)
            n = len(wl)
            n_bins = min(SPEC_BINS, n)
            exp_id = recorder.experiment_id
            recorder.open_csv("spectrometer", f"spectrometer_{exp_id}.csv",
                              ["Timestamp", "Elapsed(s)", "Status"] + [f"{w:.3f}" for w in wl], encoding="utf-8-sig")
            simulated = "spectrometer" in self.simulated
            recorder.update(spectrometer={
                "pixels": n, "wavelength_min_nm": round(float(wl.min()), 3), "wavelength_max_nm": round(float(wl.max()), 3),
                "integration_ms": integration_ms, "interval_s": interval, "averages": 1, "trigger": "software",
                "intensity": "原始 counts（未扣暗光）", "csv_columns": 3 + n, "simulated": simulated})
            meta = {"run_id": self.run_id, "wavelength": [round(float(w), 3) for w in wl], "n_pixels": n,
                    "n_bins": n_bins, "wl_min": float(wl.min()), "wl_max": float(wl.max()),
                    "interval": interval, "integration_ms": integration_ms, "simulated": simulated,
                    "display_s": min(history_duration, self._opt_max_cols(interval, history_duration) * interval),
                    "max_cols": self._opt_max_cols(interval, history_duration), "saturation": SPEC_MAX_COUNTS}
            with self.lock:
                self.opt_meta = meta
            self.publish("spec_meta", meta)
            slog(INFO, "光譜儀", f"初始化完成：{n} 像素，{wl.min():.2f}–{wl.max():.2f} nm，積分 {integration_ms:g} ms，"
                 f"間隔 {interval} 秒，CSV spectrometer_{exp_id}.csv（{3 + n} 欄）")
            self.set_sensor("spectrometer", text="量測中", level="ok")

            consecutive_failures = 0
            disconnected = False
            next_run = time.time()
            while not stop_event.is_set():
                try:
                    counts = device.measure(integration_ms)
                    log(counts, "ok")
                    self._ingest_spectrum(time.time() - self.start_time, counts)
                    consecutive_failures = 0
                    if disconnected:
                        disconnected = False
                        self.notify("info", "通知", "光譜儀已重新連接", src="光譜儀")
                    peak = int(np.argmax(counts))
                    self.set_sensor("spectrometer", text=f"最強峰 {wl[peak]:.1f} nm", value=round(float(counts[peak]), 1),
                                    level="ok")
                except Exception as e:  # noqa: BLE001
                    reason, detail = classify_spectrometer_error(e)
                    self._log_exception_once("spectrometer", e, "光譜儀讀取錯誤")
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_FAILURES and not disconnected:
                        disconnected = True
                        self.notify("warning", "警告", f"光譜儀可能已斷線（{REASON_LABELS[reason]}），"
                                    "監測將繼續但不會讀取光譜數據", src="光譜儀")
                    log(None, f"{reason}: {e}")
                    self._ingest_spectrum(time.time() - self.start_time, None)
                    self.set_sensor("spectrometer", text="感測器斷線" if disconnected else "讀取失敗", value=None,
                                    level="error" if disconnected else "warn", reason=reason, detail=detail)
                # 以固定節奏量測（量測本身耗時算在間隔內）；落後太多時不補跑，直接從現在重新計時
                next_run += interval
                delay = next_run - time.time()
                if delay < 0:
                    next_run = time.time()
                    delay = 0
                stop_event.wait(delay)
        except BaseException as e:  # noqa: BLE001
            slog(ERROR, "光譜儀", f"光譜儀監測錯誤: {e}", e)
            recorder.add_error(f"光譜儀監測錯誤: {e}")
            self.notify("warning", "警告", f"光譜儀監測出現問題: {e}\n監測將繼續但不會有光譜數據", src="光譜儀")
            reason, detail = classify_spectrometer_error(e)
            self.set_sensor("spectrometer", text="初始化失敗", value=None, level="error", reason=reason, detail=detail)
        finally:
            recorder.close("spectrometer")
            if device is not None:
                try:
                    device.close()
                except BaseException as e:  # noqa: BLE001
                    slog(WARN, "光譜儀", f"關閉光譜儀錯誤: {e}", e)

    # ---------------- 關閉伺服器 ----------------
    def shutdown(self, reason="sigint"):
        with self.lock:
            if self.shutdown_done:
                return
            self.shutdown_done = True
            running = self.phase == "running"
        slog(INFO, SRC_SYSTEM, f"伺服器關閉中（{STOP_REASONS.get(reason, reason)}）：停止所有監測、關閉檔案並釋放硬體…")
        if running:
            self.request_stop(reason)
        # 等待 finalizer：原始資料（WAV/CSV）會先關檔，接著產生頻譜 CSV（1 小時錄音約 6 秒）
        deadline = time.time() + 900
        notified = False
        while time.time() < deadline:
            with self.lock:
                if self.phase not in ("running", "stopping"):
                    break
                job = self.spectrogram_job
            if job and job.get("status") == "running" and not notified:
                notified = True
                slog(INFO, SRC_SYSTEM, "正在產生頻譜 CSV（原始資料已安全存檔）。若要略過可再按一次 Ctrl+C，"
                     "之後用 uv run python app/chunked_spectrogram.py <實驗資料夾> 補產生。")
            time.sleep(0.1)
        slog(INFO, SRC_SYSTEM, "伺服器已關閉")


service = MonitorService()
app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"),
            static_folder=os.path.join(APP_DIR, "static"))


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.errorhandler(Exception)
def handle_unexpected(e):
    """路由裡未處理的例外：寫進 log（含 traceback）並回 500 JSON；HTTP 錯誤（404 等）照原樣回傳。"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    slog(ERROR, SRC_SYSTEM, f"未處理的例外 {request.method} {request.path}: {e}", e)
    return jsonify({"ok": False, "errors": [f"伺服器內部錯誤: {e}"]}), 500


@app.route("/")
def index():
    return render_template("monitor.html")


@app.get("/api/devices")
def api_devices():
    kind = request.args.get("kind", "all")
    out = {}
    if kind in ("all", "com"):
        out["com"] = list_com_ports()
    if kind in ("all", "audio"):
        out["audio"] = list_audio_devices()
    return jsonify(out)


@app.post("/api/preflight")
def api_preflight():
    raw = request.get_json(silent=True) or {}
    # 監測中再跑預檢會重開測距儀 DLL，干擾正在量測的 worker
    if service.phase in ("running", "stopping"):
        return jsonify({"ok": False, "errors": ["監測已在進行中"], "warnings": []}), 409
    result = service.preflight(raw)
    result.pop("parsed", None)
    return jsonify(result)


@app.post("/api/start")
def api_start():
    raw = request.get_json(silent=True) or {}
    body, code = service.start(raw, bool(raw.get("confirm")))
    return jsonify(body), code


@app.post("/api/stop")
def api_stop():
    accepted = service.request_stop("manual")
    return jsonify({"ok": accepted, "state": service.snapshot()}), (202 if accepted else 409)


@app.get("/api/state")
def api_state():
    return jsonify(service.full_state())


@app.get("/api/spectrogram")
def api_spectrogram():
    start, count, n_freq, data = service.spectrogram_bulk()
    resp = Response(data, mimetype="application/octet-stream")
    resp.headers["X-Spec-Start"] = str(start)
    resp.headers["X-Spec-Count"] = str(count)
    resp.headers["X-Spec-NFreq"] = str(n_freq)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/stream")
def api_stream():
    q = service.subscribe()

    def gen():
        try:
            yield "retry: 2000\n\n"
            yield f"event: state\ndata: {json.dumps(service.snapshot(), ensure_ascii=False)}\n\n"
            while True:
                try:
                    yield q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            service.unsubscribe(q)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


_exit_reason = {"value": "sigint"}
_console_handlers = []


def _make_signal_handler(reason):
    def handler(signum, frame):  # noqa: ARG001
        _exit_reason["value"] = reason
        raise KeyboardInterrupt
    return handler


def _install_console_close_handler():
    """Windows：點視窗 X（CTRL_CLOSE_EVENT）Python 不會轉成 signal，需用 SetConsoleCtrlHandler。
    handler 在另一個執行緒執行；先停止監測並存檔再回傳（系統約數秒後會強制結束行程）。"""
    import ctypes
    from ctypes import wintypes

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(ctrl_type):
        if ctrl_type in (2, 5, 6):  # CTRL_CLOSE_EVENT / CTRL_LOGOFF_EVENT / CTRL_SHUTDOWN_EVENT
            _exit_reason["value"] = "console_close"
            service.shutdown("console_close")
            return True
        return False  # Ctrl+C / Ctrl+Break 仍交給 Python 原本的處理

    cb = handler_type(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True)
    _console_handlers.append(cb)  # 必須持有參照，否則 callback 會被回收


def main():
    parser = argparse.ArgumentParser(description="感測器整合系統（網頁版）")
    parser.add_argument("--host", default=HOST, help=f"監聽位址（預設 {HOST}）")
    parser.add_argument("--port", type=int, default=PORT, help=f"連接埠（預設 {PORT}）")
    parser.add_argument("--simulate", default="",
                        help="以模擬資料取代指定感測器：temp,distance,audio,spectrometer（別名 spectrum/spec/avaspec）"
                             "或 all（預設不模擬；音訊需明確列出）")
    parser.add_argument("--simulate-faults", action="store_true",
                        help="模擬感測器定期連續失敗 12 次再恢復，用來測試斷線／恢復通知")
    args = parser.parse_args()
    try:
        service.simulated = sim_devices.parse_simulate(args.simulate)
    except ValueError as e:
        parser.error(str(e))
    service.sim_faults = bool(args.simulate_faults and service.simulated)

    # 以專案根目錄為工作目錄（與 main_csv.py 相同；資料路徑本身已是絕對路徑）
    os.chdir(BASE_DIR)
    setup_logging()
    # worker thread 裡沒接住的例外也要進 log（預設只會印到 stderr）
    threading.excepthook = lambda a: slog(ERROR, SRC_SYSTEM, f"執行緒 {a.thread.name if a.thread else '?'} "
                                                           f"未處理的例外: {a.exc_value}",
                                          a.exc_value)
    # 輸出導到檔案時也即時寫出；Windows 主控台若不是 UTF-8，無法編碼的字元以 ? 取代而不是讓程式崩潰
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    sys.stderr.reconfigure(errors="replace")
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        # LKIF2.dll 依賴 drivers/ 裡的 CmnLib.dll / KeyUsbDrv.dll
        os.add_dll_directory(DRIVERS_DIR)
    atexit.register(lambda: service.shutdown(_exit_reason["value"]))
    # 從背景 shell 啟動時 SIGINT 可能被繼承為「忽略」，明確恢復成 KeyboardInterrupt
    signal.signal(signal.SIGINT, _make_signal_handler("sigint"))
    signal.signal(signal.SIGTERM, _make_signal_handler("sigterm"))
    if hasattr(signal, "SIGBREAK"):  # Windows：Ctrl+Break
        signal.signal(signal.SIGBREAK, _make_signal_handler("sigbreak"))
    if hasattr(signal, "SIGHUP"):  # macOS/Linux：關閉 Terminal 視窗
        signal.signal(signal.SIGHUP, _make_signal_handler("sighup"))
    if os.name == "nt":
        try:
            _install_console_close_handler()
        except Exception as e:  # noqa: BLE001
            slog(ERROR, SRC_SYSTEM, f"安裝主控台關閉 handler 失敗: {e}", e)
    slog(INFO, SRC_SYSTEM, f"感測器整合系統（網頁版）啟動：http://{args.host}:{args.port}（{platform.platform()}，"
         f"Python {platform.python_version()}）")
    slog(INFO, SRC_SYSTEM, f"資料存到 Sensor_Data/EXP_<時間>/，伺服器 log 在 {os.path.relpath(LOG_DIR, BASE_DIR)}/。按 Ctrl+C 結束。")
    if service.simulated:
        slog(WARN, SRC_SYSTEM, f"*** 模擬模式：{', '.join(sorted(service.simulated))} 使用模擬資料（不是真實量測）"
             f"{'，含故障模擬' if service.sim_faults else ''} ***")
    try:
        # 不開 debug reloader：reloader 會啟動兩個行程，硬體會被雙開
        app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    except BaseException as e:
        _exit_reason["value"] = "error"
        slog(ERROR, SRC_SYSTEM, f"伺服器未處理的例外: {e}", e)
    finally:
        service.shutdown(_exit_reason["value"])


if __name__ == "__main__":
    main()
