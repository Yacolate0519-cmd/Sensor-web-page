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
from flask import Flask, Response, jsonify, redirect, render_template, request  # noqa: E402

from rangefinder import LKIF2Device  # noqa: E402
from rangefinder.constants import FLOAT_RESULT, LKIF_ABLEMODE_AUTO, RC_CODES, RC_OK, rc_text  # noqa: E402
from rangefinder.core import LKIFError  # noqa: E402
import chunked_spectrogram  # noqa: E402
import history  # noqa: E402
import sim_devices  # noqa: E402
import spectrometer_driver  # 只定義包裝類別；真正的驅動延遲到預檢／worker 啟動才 import
from signal_package import AudioRecorder  # noqa: E402
from temp_py_package import list_candidate_ports  # noqa: E402
from temp_py_package.reader import check_port_present, find_temperature_port, read_tx600n_diag
from truplasma_package import MEASUREMENTS, PARAMS, SimTruPlasma, TruPlasmaController, open_serial  # noqa: E402
from truplasma_package.params import RECIPE_LIMITS, plan_recipe  # noqa: E402
from truplasma_package.protocol import ALARM_TEXT, PULSE_MODES, TEST_VERSION_CODES  # noqa: E402

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
# 暫時失誤 vs 真故障（FailureTracker）：連續失敗達 ESCALATE_FAILS 次「且」持續至少 ESCALATE_S 秒才升級為真故障
# （WARN／ERROR＋通知）；之前只計數，恢復時寫一行 INFO。每 LOG_SUMMARY_S 秒最多逐筆寫 TRANSIENT_LOG_MAX 行短暫失誤。
ESCALATE_FAILS = MAX_FAILURES
ESCALATE_S = 5.0
TRANSIENT_LOG_MAX = 5
EVENTS_MAX = 500
PREFLIGHT_DEDUP_S = 600         # 這段時間內預檢結果完全相同就不再寫 log                # experiment JSON 的 events 筆數上限
# 溫度計校正（2026-10-22，TX-600N T1 套熱縮套，燒杯對玻璃溫度計，24 °C 與 38 °C 兩點線性擬合，殘差 ≤ 0.06 °C）：
# 真實 = GAIN × 讀值 + OFFSET。只在 24~38 °C 驗證過，超出範圍是外插。模擬資料不套用。
# 原始資料：Sensor_Data/calibration/tx600n_sleeve_20261022_*.csv
TEMP_CAL_GAIN = 1.0239
TEMP_CAL_OFFSET = -0.531
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

# 實驗標籤：開始監測前由使用者填寫，寫入 experiment_<id>.json 的 experiment_labels。
# (表單 key, JSON key, 中文名稱, 必填)；試片另見 SPECIMEN_SHAPES
LABEL_FIELDS = [
    ("label_metal", "metal", "金屬種類", True),
    ("label_electrolyte", "electrolyte", "電解液配方與濃度", True),
    ("label_additive", "additive", "添加物", False),
]
# 電源模式目前固定，不在介面顯示；之後要開放選擇再改回 LABEL_FIELDS
POWER_MODE = "雙極脈衝"
# 試片形狀 → (中文名稱, [(表單 key, JSON key, 中文名稱)])；長度 mm、面積 mm²，必填且須 > 0
SPECIMEN_SHAPES = {
    "square": ("正方形", [("label_specimen_length", "length_mm", "長"),
                          ("label_specimen_width", "width_mm", "寬"),
                          ("label_specimen_thickness", "thickness_mm", "厚")]),
    "circle": ("圓形", [("label_specimen_area", "area_mm2", "面積"),
                        ("label_specimen_thickness", "thickness_mm", "厚")]),
}
DEFAULT_PARAMS.update({form_key: "" for form_key, *_ in LABEL_FIELDS})
DEFAULT_PARAMS.update({k: "" for _, dims in SPECIMEN_SHAPES.values() for k, *_ in dims})
DEFAULT_PARAMS["label_specimen_shape"] = "square"

# 溫度 COM 欄位留空或填這些值＝開始前自動偵測
AUTO_COM_VALUES = ("", "auto", "自動", "自動偵測")

REFL_MODE_LABELS = {0: "0-漫反射", 1: "1-鏡面反射"}
STOP_REASONS = {"manual": "手動停止", "sigint": "Ctrl+C", "sigterm": "SIGTERM", "sigbreak": "Ctrl+Break",
                "sighup": "關閉終端機視窗", "console_close": "關閉主控台視窗",
                "error": "錯誤", "timeout": "計時結束"}

# 正式實驗流程（docs/plans/experiment-ux-flow.md「已決定（2026-10-31）」）：
# 標籤 → 電源參數＋實驗時間 → 檢驗Sensor與電供（試收）→ 確認 → 伺服器端依序：開始錄製 → 基線 → 主繼電器 → 輸出 ON → 計時
EXP_BASELINE_S = 5.0             # 開始錄製後、送出電源開啟前的基線秒數（電源關閉）
SENSOR_CHECK_S = 3.0             # 「檢驗Sensor與電供」試收秒數
SENSOR_CHECK_INIT_GRACE_S = 5.0  # 試收時間到時仍在初始化（例如測距儀 ABLE 約 2.7 秒）的感測器最多再等幾秒
EXP_OUTPUT_ON_TIMEOUT_S = 25.0   # 送出開啟後多久沒進入「輸出中」就放棄（controller 自己另有繼電器 10 秒、輸出 5 秒逾時）
EXP_OFF_TIMEOUT_S = 8.0          # 結束時等電源回報輸出與主繼電器都關閉的上限，逾時改送緊急關閉
EXP_MAX_S = 24 * 3600            # 實驗時間上限
# 「檢驗」時自動重設一次的鎖存警報白名單：61613 ActSourceFail 是電腦停止送心跳 4–5 秒後電源自鎖（斷線、伺服器重啟、
# 進出手動模式都會觸發，多半不是真問題）；Test version 韌體訊息不是故障。其他代碼（短路、過溫、冷卻水…）可能是真問題，不自動重設。
AUTO_RESET_ALARM_CODES = frozenset({61613} | set(TEST_VERSION_CODES))
AUTO_RESET_WAIT_S = 3.0         # 送出重設後，等警報清掉的最長秒數
CHECK_VALID_S = 300              # 「檢驗Sensor與電供」結果的有效期限；超過就要重新檢驗
RUN_MODES = {"experiment": "正式實驗", "sensor_test": "Sensor 測試模式（非正式實驗，不控制電源）"}
# 正式實驗的結果標記（寫進 experiment_<id>.json 的 result.code）
EXP_RESULTS = {
    "completed": "完成",
    "aborted_user": "中止（使用者）",
    "aborted_psu_alarm": "中止（電源警報）",
    "aborted_interlock": "中止（Interlock）",
    "aborted_comm_lost": "中止（通訊中斷）",
    "aborted_psu_start_failed": "中止（電源無法開啟輸出）",
    "aborted_psu_off": "中止（電源自行關閉輸出）",
    "aborted_system": "中止（程式關閉或錄製被外部停止）",
}
# 實驗中（正式實驗未結束前）不能做的電源操作的錯誤訊息
EXP_PSU_LOCKED = "正式實驗進行中，電源參數與輸出已由實驗流程鎖定；要停止請按「緊急停止」或「結束實驗」"

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


def split_detail(detail):
    """「短現象：原因與建議」→ (短現象, 原因與建議)；沒有冒號時整段都是短現象。"""
    head, sep, tail = (detail or "").partition("：")
    return (head, tail) if sep else (detail or "", "")


class FailureTracker:
    """一個感測器（或電源通訊）的失敗紀錄，用來分開暫時失誤和真故障。

    「失敗期」從第一次失敗開始，到下一次成功結束。連續失敗達 ESCALATE_FAILS 次且持續至少 ESCALATE_S 秒
    （或呼叫端已自行確認，例如音訊連續 3 秒全 0）才升級；升級前只計數，不寫 WARN、不通知。
    fail()／ok() 回傳要寫的事件，實際寫 log／通知由 MonitorService 做。呼叫端負責加鎖。
    """

    def __init__(self):
        self.ep = None              # 目前的失敗期
        self.last_ok = None
        self.transients = 0         # 本次實驗短暫失誤的段數、總失敗筆數、最長持續、錯誤碼分布
        self.transient_fails = 0
        self.transient_longest = 0.0
        self.transient_codes = collections.Counter()
        self.escalations = 0
        self.window_end = 0.0       # 短暫失誤逐筆 log 的節流視窗
        self.window_logged = 0
        self.suppressed_n = 0       # 視窗內未逐筆寫的短暫失誤段數與錯誤碼分布
        self.suppressed = collections.Counter()
        self.tb_logged = set()      # 已寫過 traceback 的例外（同一種只寫一次）

    @property
    def escalated(self):
        return bool(self.ep and self.ep["escalated"])

    def fail(self, now, reason, code, comm, detail, confirmed=False, since=None):
        """since：呼叫端已知的失敗起點（例如音訊全 0 從 3 秒前開始），新的失敗期從那時算起。"""
        ep = self.ep
        if ep is None:
            ep = self.ep = {"start": since or now, "fails": 0, "codes": collections.Counter(), "seq": [], "reason": reason,
                            "comm": comm, "detail": detail, "escalated": False, "next_summary": 0.0}
        changed = reason != ep["reason"]
        ep["fails"] += 1
        ep["codes"][code] += 1
        if (not ep["seq"] or ep["seq"][-1] != code) and len(ep["seq"]) < 8:
            ep["seq"].append(code)
        ep.update(reason=reason, comm=comm, detail=detail, last_code=code)
        if not ep["escalated"]:
            if confirmed or (ep["fails"] >= ESCALATE_FAILS and now - ep["start"] >= ESCALATE_S):
                ep["escalated"] = True
                ep["next_summary"] = now + LOG_SUMMARY_S
                self.escalations += 1
                return "escalate"
            return None
        if changed:
            return "change"
        if now >= ep["next_summary"]:
            ep["next_summary"] = now + LOG_SUMMARY_S
            return "summary"
        return None

    def ok(self, now):
        """成功一次：回傳 (事件, 失敗期) 或 None。事件是 "recover"（真故障恢復）或 "transient"（短暫失誤）。"""
        ep, self.ep = self.ep, None
        self.last_ok = now
        if ep is None:
            return None
        ep["duration"] = now - ep["start"]
        if ep["escalated"]:
            return "recover", ep
        self.transients += 1
        self.transient_fails += ep["fails"]
        self.transient_longest = max(self.transient_longest, ep["duration"])
        self.transient_codes.update(ep["codes"])
        return "transient", ep

    def transient_logged(self, now, ep):
        """短暫失誤要不要逐筆寫：每 LOG_SUMMARY_S 秒最多 TRANSIENT_LOG_MAX 行，其餘併成一行摘要。
        回傳 (要不要寫這一行, 上一個視窗被省略的錯誤碼分布或 None)。"""
        flushed = None
        if now >= self.window_end:
            flushed = self.flush_suppressed()
            self.window_end, self.window_logged = now + LOG_SUMMARY_S, 0
        if self.window_logged < TRANSIENT_LOG_MAX:
            self.window_logged += 1
            return True, flushed
        self.suppressed_n += 1
        self.suppressed.update(ep["codes"])
        return False, flushed

    def flush_suppressed(self):
        """取出被省略的短暫失誤 (段數, 錯誤碼分布)；沒有時回傳 None。"""
        if not self.suppressed_n:
            return None
        out = (self.suppressed_n, self.suppressed)
        self.suppressed_n, self.suppressed = 0, collections.Counter()
        return out

    @staticmethod
    def dist(codes):
        return "、".join(f"{c} {n}" for c, n in codes.most_common())
# 電源供應器（TruPlasma）不是實驗啟動時才開的感測器，不放進 SENSOR_NAMES（那裡的 key 會被拿去對 enabled / sensors 取值）
PSU_NAME = "電源"
PSU_DEFAULT_PORT = "COM8"
PSU_LOCAL_ADDRS = ("127.0.0.1", "::1")
PSU_CSV_HEADER = (["Timestamp", "Elapsed(s)", "Phase", "Uset_V", "Iset_A", "Pset_kW",
                   "Uact_V", "Iact_peak_A", "Pact_kW"]
                  + [name for _, name in MEASUREMENTS]
                  + ["RelaysOn", "PowerOn", "Interlock", "AlarmActive", "WarningActive", "AlarmCode",
                     "Mode", "Regulation", "ArcRate_per_s", "ArcCnt_Imax", "ArcCnt_UxI", "ArcCnt_dU",
                     "ArcCnt_Usag", "ArcCnt_ArcBurst", "Bits0", "Bits1", "Bits2", "Bits3"])


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
    """對應 main_csv.refresh_com_ports：候選埠排序後回傳；預設值留空＝自動偵測。"""
    try:
        ports = [
            {"device": p.device, "description": p.description or ""}
            for p in list_candidate_ports()
        ]
        # 預設留空＝自動偵測（開始前探測哪個 USB 轉序列埠接著溫度計）；清單仍列出所有埠供手動指定
        return {"ports": ports, "default": "", "error": None}
    except Exception as e:  # noqa: BLE001
        slog(ERROR, SRC_SYSTEM, f"刷新COM端口錯誤: {e}", e)
        return {"ports": [], "default": "", "error": str(e)}


# 預設麥克風：名稱含這些關鍵字的第一個輸入設備（不分大小寫）；可用環境變數 SENSOR_AUDIO_PREFER（逗號分隔）覆寫。
# 實驗室收音用 RØDE VideoMic NTG。PyAudio 依 index 列出時 MME 排最前面，MME 可接受任意採樣率（WASAPI 共享模式不行）。
PREFERRED_AUDIO_KEYWORDS = tuple(
    k.strip() for k in os.environ.get("SENSOR_AUDIO_PREFER", "RØDE,RODE").split(",") if k.strip())


def fix_device_name(name):
    """Windows 的 PyAudio 會把 UTF-8 裝置名稱當 cp1252 解（RØDE → RÃ˜DE）；能還原就還原，否則原樣回傳。"""
    try:
        return name.encode("cp1252").decode("utf-8")
    except UnicodeError:
        return name


def list_audio_devices():
    """對應 main_csv.refresh_audio_devices：只列出有輸入通道的設備。"""
    try:
        p = pyaudio.PyAudio()
        devices = []
        try:
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if info["maxInputChannels"] > 0:
                    name = fix_device_name(info["name"])
                    devices.append({
                        "index": i,
                        "name": name,
                        "display_name": f"{i}: {name}",
                        "channels": int(info["maxInputChannels"]),
                        "sample_rate": float(info["defaultSampleRate"]),
                    })
        finally:
            p.terminate()
        if devices:
            preferred = next((d for d in devices
                              if any(k.lower() in d["name"].lower() for k in PREFERRED_AUDIO_KEYWORDS)), devices[0])
            return {"devices": devices, "status": "ok", "placeholder": None,
                    "default": preferred["display_name"]}
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
    """把 LKIF2Device 建構/open 的例外分類成 (reason, detail)。FileNotFoundError 是 OSError 子類，要先判斷。
    detail 會顯示在畫面上，只放可行動的說明，不含原始例外文字（原始例外由呼叫端寫 log）。"""
    if isinstance(exc, FileNotFoundError):
        return R_DRIVER, "找不到 drivers/LKIF2.dll：確認 drivers 資料夾內有 LKIF2.dll、CmnLib.dll、KeyUsbDrv.dll"
    if isinstance(exc, AttributeError):
        return R_DRIVER, "此系統不是 Windows，無法載入 KEYENCE DLL"
    if isinstance(exc, OSError):
        return R_DRIVER, "DLL 載入失敗：缺少相依 DLL（CmnLib.dll／KeyUsbDrv.dll）或 Python 與 DLL 位元（32/64）不符"
    if isinstance(exc, RuntimeError) and "OpenDevice" in str(exc):
        return R_NOT_FOUND, "LK-G5000 未連線：檢查 USB 線、控制器電源、KEYENCE USB 驅動"
    return R_ERROR, "測距儀發生未預期的錯誤：詳細內容見實驗 log"


def classify_spectrometer_error(exc):
    """把光譜儀驅動 import／初始化／量測的例外分類成 (reason, detail)（沿用 R_* 代碼）。
    detail 會顯示在畫面上，不含原始例外文字（原始例外由呼叫端寫 log）。"""
    msg = str(exc)
    if isinstance(exc, ImportError):
        if getattr(exc, "name", None) == "PyQt5" or "PyQt5" in msg:
            return R_DRIVER, "缺少 PyQt5：光譜儀驅動需要 PyQt5（uv sync --extra spectrometer）"
        return R_DRIVER, "光譜儀驅動載入失敗：詳細內容見實驗 log"
    if isinstance(exc, FileNotFoundError):
        return R_DRIVER, "找不到 AvaSpec 驅動檔：Windows 需要 drivers/avaspecx64.dll，macOS／Linux 需安裝 AvaSpec 原生函式庫"
    if isinstance(exc, OSError):
        return R_DRIVER, f"AvaSpec DLL 載入失敗：缺少相依檔，或 Python 與 DLL 位元（32/64）不符"
    if isinstance(exc, AttributeError):
        return R_DRIVER, "此系統無法載入 AvaSpec 驅動"
    if "沒有找到光譜儀" in msg or "無可用設備" in msg:
        return R_NOT_FOUND, "找不到光譜儀：檢查 USB 線、電源與 AvaSpec 驅動（裝置管理員是否看得到 AvaSpec）"
    if "等待超時" in msg:
        return R_NO_DATA, "光譜儀沒有回傳資料（等待超時）：確認光譜儀未被其他程式佔用，並檢查積分時間與觸發設定"
    return R_ERROR, "光譜儀發生未預期的錯誤：詳細內容見實驗 log"


# 測距儀讀的 OUT 0 對應的感測頭：2026-10-30 實測感測頭接 HEAD A 時 OUT 0 全是 ALARM，換到 HEAD B 後才有 VALID
RANGEFINDER_HEAD = "B"
# 測距儀 FloatResult 非 VALID 時的分類（Unknown/INVALID 另外處理）。格式「短現象：原因與建議」，
# ALARM 是量測值無效（USB 仍連著、每次都讀得到），不可寫成斷線或 USB 中斷
RANGEFINDER_STATUS_DIAG = {
    "WAITING": (R_NO_DATA, "控制器尚未送出量測值（WAITING）：剛開始量測的前一兩筆屬正常；持續出現時確認控制器在量測模式"),
    "+RANGEOVER": (R_OUT_OF_RANGE, "目標超出量測範圍（過遠，+RANGEOVER）：調整感測頭與目標的距離"),
    "-RANGEOVER": (R_OUT_OF_RANGE, "目標超出量測範圍（過近，-RANGEOVER）：調整感測頭與目標的距離"),
    "ALARM": (R_WIRING, f"HEAD {RANGEFINDER_HEAD} 收不到反射光（ALARM）：檢查距離、角度、表面反光，或換 HEAD"),
}


# 確定是通訊層錯誤的 RC 碼（對照表裡的裝置類錯誤）。0x2003、0x2008 等未知碼意思待查 LKIF2.h，不算確定
RANGEFINDER_COMM_RC = {0x2000, 0x2001}


def rangefinder_read_error(exc):
    """讀值函式丟出的例外 → (code, detail, 是否確定為通訊層錯誤)。「USB 可能中斷」只用在 RANGEFINDER_COMM_RC，
    並附錯誤碼；未知碼照實寫「意思待查」，不猜原因。"""
    if isinstance(exc, LKIFError):
        code = f"RC=0x{exc.rc:04X}"
        if exc.rc in RANGEFINDER_COMM_RC:
            return code, (f"讀值函式回傳通訊層錯誤 {code}（{rc_text(exc.rc)}）：USB 可能中斷；"
                          "檢查 USB 線與控制器電源"), True
        if exc.rc in RC_CODES:
            return code, (f"控制器回報錯誤 {code}（{rc_text(exc.rc)}）：持續出現時確認控制器設定，"
                          "或停止後重新開始量測"), False
        return code, (f"讀值失敗 {code}（{rc_text(exc.rc)}，意思待查 KEYENCE LKIF2.h）："
                      "持續出現時檢查 USB 線、控制器電源與設定"), False
    return type(exc).__name__, (f"讀值時發生程式例外（{type(exc).__name__}: {exc}）：不是儀器回報的錯誤碼；"
                                "持續發生時 traceback 會寫進實驗 log"), False


def diagnose_audio_device(index):
    """AudioRecorder 因 sys.exit(1) 失敗後，直接用 pyaudio 查出原因，回傳 (reason, detail)。"""
    if index is None:
        return R_NOT_FOUND, "未選擇有效的音訊輸入設備"
    try:
        p = pyaudio.PyAudio()
    except BaseException as e:  # noqa: BLE001
        slog(ERROR, "音訊", f"PortAudio/PyAudio 初始化失敗: {e}", e)
        return R_DRIVER, "音訊系統（PortAudio/PyAudio）初始化失敗：確認音訊驅動與 PyAudio 已正確安裝"
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


# 套用配方後讀回比對與顯示的通道：脈衝模式、低頻模式、低頻設定值／頻率設定值、佔空比，加上 Off Time（雙極時由 pos/neg 換算後一併寫入比對）
RECIPE_READBACK_CHANNELS = (33, 140, 1100, 51130, 51131, 650)
RECIPE_INCOMPLETE = "上次套用配方沒有完成讀回比對（部分參數可能已寫入）"


def recipe_readback_errors(readback, recipe):
    """配方寫入後的讀回比對：回傳不一致的通道（依模式只比對該寫的 ch33/140/1100 或 51130/51131，雙極再加 ch650），
    每項列出通道、寫入值、讀回值。"""
    names = {p.channel: p.name for p in PARAMS}
    return [f"{names.get(ch, 'ch' + str(ch))}（ch{ch}）寫入 {value:g}，讀回 "
            f"{'讀不到' if readback.get(ch, readback.get(str(ch))) is None else f'{readback.get(ch, readback.get(str(ch))):g}'}"
            for ch, value in plan_recipe(readback, recipe)]


def parse_exp_time(value):
    """實驗時間：「分:秒」（例 10:00、1:30）或秒數（例 600）→ 秒（float）；不合法丟 ValueError（中文訊息）。"""
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("實驗時間為必填（分:秒 或 秒）")
    try:
        if ":" in text:
            m, s = text.split(":", 1)
            minutes, seconds = int(m.strip() or "0"), float(s.strip())
            if minutes < 0 or not 0 <= seconds < 60:
                raise ValueError
            sec = minutes * 60 + seconds
        else:
            sec = float(text)
    except ValueError:
        raise ValueError(f"實驗時間格式錯誤：{text}（請輸入 分:秒，例如 10:00，或秒數）") from None
    if not (math.isfinite(sec) and 0 < sec <= EXP_MAX_S):
        raise ValueError(f"實驗時間必須大於 0 且不超過 {EXP_MAX_S // 3600} 小時")
    return sec


class _NullRecorder:
    """「檢驗Sensor與電供」試收用：介面同 ExperimentRecorder，但什麼都不寫（試收資料不存檔）。"""
    experiment_id = "SENSOR_CHECK"

    def __getattr__(self, name):
        return lambda *a, **k: None


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

    def add_event(self, kind, source, message, **fields):
        """感測器／電源的重要事件（升級為真故障、恢復）寫進 JSON 的 events（最多 EVENTS_MAX 筆）。"""
        with self.lock:
            events = self.info.setdefault("events", [])
            if len(events) >= EVENTS_MAX:
                return
            events.append({"time": iso_now(), "kind": kind, "source": source, "message": message, **fields})
        self.write_json()

    def note_code(self, source, code, value=None, text="", raw=None, level=None):
        """儀器回報的原始代碼（測距儀 RC 碼／FloatResult、電源警報碼）照實記進 JSON 的 device_codes：
        每個 (來源, 代碼) 一筆，含數值、程式的翻譯、儀器原文、次數與首末時間。
        只在第一次出現或出現新原文時寫檔；次數在之後任何一次 write_json（最晚是停止存檔）一起寫出。"""
        now = iso_now()
        with self.lock:
            codes = self.info.setdefault("device_codes", [])
            entry = next((c for c in codes if c["source"] == source and c["code"] == code), None)
            dirty = entry is None
            if entry is None:
                entry = {"source": source, "code": code, "value": value, "text": text, "level": level,
                         "raw": [], "count": 0, "first_time": now}
                codes.append(entry)
            entry["count"] += 1
            entry["last_time"] = now
            if raw and raw not in entry["raw"] and len(entry["raw"]) < 20:
                entry["raw"].append(raw)
                dirty = True
        if dirty:
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
        self._pf_raw = {}            # 預檢失敗的原始例外文字（併進該感測器的預檢那一行）
        self._pf_logged = (None, 0.0)  # 上一次寫進 log 的預檢結果與時間（同樣結果不重複寫）
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
        # psu_u_a / psu_u_b、psu_i_a / psu_i_b：A、B 兩路平均電壓與電流（雙極脈衝下 u_act / i_act_peak 會低估）。只在實驗錄製中且電源已連線時累積
        self.series = {"temp": [], "distance": [], "psu_u_a": [], "psu_u_b": [], "psu_i_a": [], "psu_i_b": []}
        self.series_sent = {"temp": 0, "distance": 0, "psu_u_a": 0, "psu_u_b": 0, "psu_i_a": 0, "psu_i_b": 0}  # 已透過 SSE 送出的筆數

        # 光譜儀（Optical Spectrum）：波長軸 meta、降採樣歷史（熱圖用）、最新一筆完整解析度（即時光譜用）
        self.opt_meta = None
        self.opt_cols = collections.deque(maxlen=SPEC_HISTORY_MAX)  # (elapsed 秒, bins bytes 或 None=讀取失敗)
        self.opt_total = 0      # 自本輪開始的筆數（全域索引，含失敗筆）
        self.opt_latest = None  # {"t": elapsed, "full": uint16 bytes}，最後一筆成功的完整光譜

        self.notices = collections.deque(maxlen=30)
        self.notice_seq = 0

        # 電源供應器（TruPlasma）：獨立於實驗的開始／停止，只由使用者明確連線／斷線；啟動伺服器時不自動連線。
        # controller 的 callback 都在它自己的 thread 上跑。
        self.psu_port = ""
        self.psu_lock = threading.Lock()       # 連線流程（避免兩個請求同時 connect）
        self._psu_csv_lock = threading.Lock()  # 懶開／寫入／關閉 truplasma CSV 串行化
        self._psu_csv_rec = None               # 目前已為哪個 ExperimentRecorder 開過 CSV
        self.psu = TruPlasmaController(self._psu_open_port, on_cycle=self._psu_on_cycle,
                                       on_event=self._psu_on_event, on_alarm=self._psu_on_alarm)

        # 正式實驗流程（exp_check / exp_start / exp_end / exp_estop）
        self.run_mode = None          # 目前／上一次錄製的模式：experiment | sensor_test
        self.exp = None               # 目前／上一次正式實驗的流程狀態（dict，見 exp_start）；stage == "done" 表示已結束
        self._check_result = None     # 最近一次「檢驗Sensor與電供」的結果；開始實驗時使用，開始後清掉（每場都要重新檢驗）
        self.psu_recipe_errors = []   # 最近一次套用配方的讀回不一致項目；非空時拒絕開啟輸出，直到重新套用且讀回一致
        self.exp_lock = threading.Lock()  # 檢驗與開始互斥（緊急停止不拿這個鎖，任何時候都能立即執行）
        self._estop_lock = threading.RLock()  # 緊急停止「設旗標＋關輸出」與流程「檢查旗標＋開輸出」互斥（只包住不阻塞的呼叫）
        self.exp_baseline_s = EXP_BASELINE_S  # 測試可縮短；預設 5 秒
        self.sensor_check_s = SENSOR_CHECK_S  # 測試可縮短；預設 3 秒
        self._valid_hits = None       # 試收期間各感測器收到有效數據的次數（None＝不在試收）

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

    def notify(self, level, title, message, src=SRC_SYSTEM, log=True):
        """取代 messagebox：level = info | warning | error | success。同時寫進 log（src 為來源欄）；
        log=False 用在呼叫端已經寫了一行更完整的 log（並註明已通知）的情況，避免同一件事寫兩行。"""
        with self.lock:
            self.notice_seq += 1
            notice = {"id": self.notice_seq, "level": level, "title": title,
                      "message": message, "time": time.time()}
            self.notices.append(notice)
        if log:
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

    def _tracker(self, key):
        """呼叫時已持有 self.lock。"""
        tr = self._track.get(key)
        if tr is None:
            tr = self._track[key] = FailureTracker()
        return tr

    def _log_exception_once(self, key, exc, what):
        """電源資料處理等非讀值迴圈的例外：同一種例外訊息只寫一次完整 traceback，避免逐筆洗版。"""
        sig = f"{type(exc).__name__}: {exc}"
        with self.lock:
            tr = self._tracker(key)
            if sig in tr.tb_logged:
                return
            tr.tb_logged.add(sig)
        slog(ERROR, SENSOR_NAMES.get(key, key), f"{what}: {sig}", exc)

    def set_sensor(self, key, text=None, value=None, level=None, push=True, reason=None, detail=None):
        """更新單一感測器。level=="ok" 自動視為正常（reason=ok、記錄 last_ok、清除失敗計數），
        並結束目前的失敗期（寫恢復或短暫失誤的 log）；否則帶 reason 的呼叫只記錄原因／說明
        （初始化失敗等終止性錯誤，由呼叫端自己寫 log）。讀值失敗請用 sensor_failed。"""
        event = None
        with self.lock:
            s = self.sensors[key]
            prev = s["reason"]
            if text is not None:
                s["text"] = text
            s["value"] = value if key != "audio" else s.get("value")
            if level is not None:
                s["level"] = level
            if level == "ok":
                s.update(reason=R_OK, detail="", last_ok=time.time(), fail_count=0)
                self._pf_shown.discard(key)
                event = self._tracker(key).ok(time.time())
                if event is None and prev != R_OK:
                    slog(INFO, SENSOR_NAMES.get(key, key), f"狀態：正常（原先為{REASON_LABELS.get(prev, prev)}）")
            elif reason is not None:
                s["reason"] = reason
                s["detail"] = detail or ""
                if reason not in NON_FAILURE_REASONS:
                    s["fail_count"] += 1
                self._pf_shown.discard(key)
        if event:
            self._failure_event(key, SENSOR_NAMES.get(key, key), *event)
        if push:
            self.push_state()

    def sensor_failed(self, key, reason, detail, code, comm=False, exc=None, confirmed=False, text=None, push=True,
                      since=None):
        """一次讀值失敗。code 是儀器回報的原始代碼（測距儀 ALARM、RC=0x2003…），用來統計分布；
        comm=True 表示通訊層錯誤（升級時記 ERROR，否則 WARN）。回傳是否已升級為真故障。
        升級前狀態卡顯示「讀取失敗」，升級後顯示 text（預設「通訊中斷」／「沒有有效值」）。"""
        now = time.time()
        with self.lock:
            tr = self._tracker(key)
            action = tr.fail(now, reason, code, comm, detail, confirmed, since)
            ep = tr.ep
            escalated = ep["escalated"]
            s = self.sensors[key]
            s.update(value=None, reason=reason, detail=detail or "", fail_count=ep["fails"], level="error" if escalated else "warn",
                     text=(text or ("通訊中斷" if comm else "沒有有效值")) if escalated else "讀取失敗")
            self._pf_shown.discard(key)
        if action:
            self._failure_event(key, SENSOR_NAMES.get(key, key), action, ep, exc)
        if push:
            self.push_state()
        return escalated

    def _failure_event(self, key, name, action, ep, exc=None):
        """FailureTracker 回傳的事件：寫 log、通知、實驗 JSON。不可在持有 self.lock 時呼叫。"""
        now = time.time()
        with self.lock:
            tr = self._tracker(key)
            rec = self.recorder if self.phase == "running" else None
            last_ok = tr.last_ok
        dur = ep.get("duration", now - ep["start"])
        dist = FailureTracker.dist(ep["codes"])
        summary, _ = split_detail(ep["detail"])
        level = ERROR if ep["comm"] else WARN
        if action == "escalate":
            sig = f"{type(exc).__name__}: {exc}" if exc is not None else None
            with self.lock:
                first_tb = sig is not None and sig not in tr.tb_logged
                if first_tb:
                    tr.tb_logged.add(sig)
            count = f"連續 {ep['fails']} 次、" if ep["fails"] > 1 else ""  # 呼叫端確認的（音訊全 0）只有時間
            slog(level, name, f"持續失敗（已跳通知）：{ep['detail']}（{count}已 {dur:.1f} 秒；{dist}）",
                 exc if first_tb else None)
            self.notify("error" if level == ERROR else "warning", f"{name}異常",
                        f"{name}：{summary}，{count}已 {dur:.0f} 秒，持續重試中（原因與建議見狀態卡）",
                        src=name, log=False)
            if rec:
                rec.add_event("escalated", name, ep["detail"], fails=ep["fails"], seconds=round(dur, 1),
                              codes=dict(ep["codes"]), reason=ep["reason"])
        elif action == "change":
            slog(level, name, f"失敗原因改變：{ep['detail']}（本段已 {ep['fails']} 次、{dur:.0f} 秒；{dist}）")
        elif action == "summary":
            ok_text = time.strftime("%H:%M:%S", time.localtime(last_ok)) if last_ok else "本次尚未成功"
            slog(level, name, f"仍在失敗：累計 {ep['fails']} 次，已持續 {dur:.0f} 秒（{dist}）；"
                 f"最後成功 {ok_text}；{ep['detail']}")
        elif action == "recover":
            slog(INFO, name, f"恢復正常（已跳通知）：失敗 {ep['fails']} 次、持續 {dur:.1f} 秒（{dist}）")
            self.notify("info", f"{name}已恢復", f"{name}已恢復正常（失敗 {ep['fails']} 次、持續 {dur:.0f} 秒）",
                        src=name, log=False)
            if rec:
                rec.add_event("recovered", name, f"恢復正常：失敗 {ep['fails']} 次、持續 {dur:.1f} 秒",
                              fails=ep["fails"], seconds=round(dur, 1), codes=dict(ep["codes"]))
        elif action == "transient":
            with self.lock:
                write, flushed = tr.transient_logged(now, ep)
                n = tr.transients
            if flushed:
                slog(INFO, name, f"過去 {LOG_SUMMARY_S} 秒另有 {flushed[0]} 次短暫失誤未逐筆記錄"
                     f"（{FailureTracker.dist(flushed[1])}）")
            if write:
                slog(INFO, name, f"短暫讀取失敗 {ep['fails']} 次、{dur:.1f} 秒後恢復（{'→'.join(ep['seq'])}）："
                     f"{summary}；本次第 {n} 次短暫失誤，未通知")

    def _failure_summaries(self, end_ts):
        """停止時：每個感測器（與電源通訊）仍在失敗的寫一行，短暫失誤彙總寫一行；回傳寫進 JSON 的摘要。"""
        out = {}
        with self.lock:
            items = [(PSU_NAME if k == "psu" else SENSOR_NAMES.get(k, k), tr) for k, tr in self._track.items()]
        for name, tr in items:
            with self.lock:
                ep, flushed = tr.ep, tr.flush_suppressed()
            if flushed:
                slog(INFO, name, f"另有 {flushed[0]} 次短暫失誤未逐筆記錄（{FailureTracker.dist(flushed[1])}）")
            entry = {}
            if ep is not None:
                dur = end_ts - ep["start"]
                slog(WARN if ep["escalated"] else INFO, name,
                     f"停止時仍在失敗：持續 {dur:.1f} 秒，累計 {ep['fails']} 次（{FailureTracker.dist(ep['codes'])}）；"
                     f"{ep['detail']}")
                entry["failing_at_stop"] = {"seconds": round(dur, 1), "fails": ep["fails"],
                                            "escalated": ep["escalated"], "codes": dict(ep["codes"]),
                                            "detail": ep["detail"]}
            if tr.transients:
                slog(INFO, name, f"短暫失誤彙總：{tr.transients} 次（共 {tr.transient_fails} 筆失敗），"
                     f"最長持續 {tr.transient_longest:.1f} 秒；錯誤碼 {FailureTracker.dist(tr.transient_codes)}")
                entry["transient"] = {"count": tr.transients, "fails": tr.transient_fails,
                                      "longest_s": round(tr.transient_longest, 2), "codes": dict(tr.transient_codes)}
            if tr.escalations:
                entry["escalations"] = tr.escalations
            if entry:
                out[name] = entry
        return out

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
                "mode": self.run_mode,
                "experiment": self._exp_public(),
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
            self._pf_raw["distance"] = f"{type(e).__name__}: {e}"  # 原始例外併進預檢結果那一行
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
            self._pf_raw["spectrometer"] = f"{type(e).__name__}: {e}"
            return False, "• 光譜儀初始化失敗，光譜儀監測將被停用", classify_spectrometer_error(e)

    def validate_params(self, raw, require_labels=True):
        """對應 main_csv.py L312-318 的參數轉型；失敗丟出 ValueError。
        require_labels=False（Sensor 測試模式）：標籤沒填或不完整時存成 None，不擋開始。"""
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
        try:
            parsed["labels"] = self.validate_labels(raw)
        except ValueError:
            if require_labels:
                raise
            parsed["labels"] = None
        return parsed

    @staticmethod
    def validate_labels(raw):
        """實驗標籤：必填欄位不可空白；選填欄位留空時存成 None。
        試片存成 {"shape": "square"|"circle", <尺寸>_mm: float, ...}。"""
        labels = {}
        for form_key, json_key, label, required in LABEL_FIELDS:
            value = str(raw.get(form_key, "") or "").strip()
            if not value and required:
                raise ValueError(f"實驗標籤未填: {label}為必填")
            labels[json_key] = value or None
        shape = str(raw.get("label_specimen_shape", "") or "").strip()
        if shape not in SPECIMEN_SHAPES:
            raise ValueError("實驗標籤未填: 試片形狀須為正方形或圓形")
        shape_name, dims = SPECIMEN_SHAPES[shape]
        specimen = {"shape": shape}
        for form_key, json_key, label in dims:
            value = str(raw.get(form_key, "") or "").strip()
            if not value:
                raise ValueError(f"實驗標籤未填: 試片（{shape_name}）{label}為必填")
            try:
                num = float(value)
            except ValueError:
                raise ValueError(f"實驗標籤錯誤: 試片{label}必須是數字") from None
            if not (math.isfinite(num) and num > 0):
                raise ValueError(f"實驗標籤錯誤: 試片{label}必須大於 0")
            specimen[json_key] = num
        return {"metal": labels.pop("metal"), "specimen": specimen, **labels, "power_mode": POWER_MODE}

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

    def preflight(self, raw, require_labels=True, require_sensor=True):
        """require_sensor=False（正式實驗）：電源才是必要元件，四個感測器都不能用也不算錯誤（只列為缺少）。"""
        warnings = []
        sys_warnings = []  # 不屬於單一感測器的警告（磁碟空間）；感測器的已寫在各自的預檢行，不再重複
        self._pf_raw = {}
        enabled = {"temp": True, "audio": True, "distance": True, "spectrometer": True}
        sim = self.simulated
        com = str(raw.get("com_port", "") or "").strip()
        # diag：每個感測器預檢時的 (reason, detail)；被停用者在狀態面板顯示「未啟用」並帶出底層原因
        diag = {k: (R_OK, "") for k in enabled}
        if com.lower() in AUTO_COM_VALUES and "temp" not in sim:
            try:
                found, tried = find_temperature_port(skip=self.psu_busy_ports())
            except Exception as e:  # noqa: BLE001
                found, tried = None, []
                slog(ERROR, SENSOR_NAMES["temp"], f"自動偵測 COM 埠失敗: {e}", e)
            if found:
                com = found
                slog(INFO, SENSOR_NAMES["temp"], f"自動偵測：溫度計在 {found}（已探測 {'、'.join(tried)}）")
            else:
                detail = (f"自動偵測找不到溫度計：已探測 {'、'.join(tried)}，都沒有正確回應；"
                          "檢查感測器電源、RS485 A/B 接線、站號(0x03)/鮑率(57600)，或手動輸入 COM 埠") if tried \
                    else "自動偵測找不到任何 USB 轉序列埠：USB-RS485 轉接器未插上，或驅動未安裝"
                warnings.append(f"• {detail}，溫度監測將被停用")
                enabled["temp"] = False
                diag["temp"] = (R_NOT_FOUND, detail)
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
        if require_sensor and not any(enabled.values()):
            errors.append("至少需要啟用一個感測器才能開始監測")
        parsed = None
        try:
            parsed = self.validate_params(raw, require_labels)
        except ValueError as e:
            errors.append(str(e))

        disk = None
        try:
            free, per_hour, disk_warn = self.disk_check()
            disk = {"free_bytes": free, "est_bytes_per_hour": per_hour}
            if disk_warn:
                warnings.append(disk_warn)
                sys_warnings.append(disk_warn)
        except OSError as e:
            errors.append(f"無法建立存檔資料夾 {DATA_DIR}: {e}")

        lines = [(INFO if enabled[k] and r == R_OK else WARN, SENSOR_NAMES[k],
                  f"預檢：{'可用' if enabled[k] else '停用'} [{r}] {REASON_LABELS[r]}" + (f"：{d}" if d else "")
                  + (f"（原始：{self._pf_raw[k]}）" if k in self._pf_raw else ""))
                 for k, (r, d) in diag.items()]
        lines += [(WARN, SRC_SYSTEM, "預檢警告：" + w.lstrip("• ")) for w in sys_warnings]
        lines += [(ERROR, SRC_SYSTEM, "預檢錯誤：" + e) for e in errors]
        off = "、".join(SENSOR_NAMES[k] for k in enabled if not enabled[k])
        lines.append((ERROR if errors else WARN if off else INFO, SRC_SYSTEM,
                      f"預檢結果：{'未通過' if errors else '通過，但停用 ' + off if off else '通過'}（啟用 "
                      f"{'、'.join(SENSOR_NAMES[k] for k in enabled if enabled[k]) or '無'}；警告 {len(warnings)} 項）"))
        # 網頁先呼叫 /api/preflight、確認後 /api/start 再檢查一次：結果相同就只記第一次
        now = time.time()
        if lines != self._pf_logged[0] or now - self._pf_logged[1] > PREFLIGHT_DEDUP_S:
            for level, src, msg in lines:
                slog(level, src, msg)
        self._pf_logged = (lines, now)
        return {"ok": not errors, "errors": errors, "warnings": warnings,
                "enabled": enabled, "parsed": parsed, "com_port": com if enabled["temp"] else "",
                "disk": disk, "diag": self.preflight_reasons}

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
                    s.update(text="預檢異常", value=None, reason=r, detail=d, fail_count=0,
                             level="error" if r in (R_DRIVER, R_NOT_FOUND, R_ERROR) else "warn")
                    self._pf_shown.add(k)
                elif k in self._pf_shown:
                    s.update(text={"temp": "-- °C", "distance": "-- µm", "audio": "--", "spectrometer": "--"}[k],
                             level="idle", reason=R_IDLE, detail="")
                    self._pf_shown.discard(k)
        self.push_state()

    @staticmethod
    def _disabled_detail(name, detail):
        """「未啟用」的說明：帶出底層分類原因（例如 此系統不是 Windows…）。狀態卡本身已標名稱，不再重複。"""
        return detail or f"{name}未啟用"

    # ---------------- 開始 ----------------
    def start(self, raw, confirmed, mode="sensor_test", extra_info=None):
        """開始錄製感測器（不控制電源）。mode：sensor_test（獨立入口 /api/start）或 experiment（由 exp_start 呼叫，
        電源由實驗流程控制）；extra_info 併進 experiment_<id>.json。"""
        with self.lock:
            if self.phase in ("running", "stopping", "checking"):
                return {"ok": False, "errors": ["監測已在進行中" if self.phase != "checking" else "正在檢驗Sensor與電供"]}, 409
        pf = self.preflight(raw, require_labels=mode != "sensor_test", require_sensor=mode != "experiment")
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
        com_port = pf["com_port"]  # 自動偵測時是偵測到的埠
        audio_sel = str(raw.get("audio_device") or "")
        info = {
            "experiment_id": experiment_id,
            # 正式實驗與 Sensor 測試模式的資料必須能區分（docs/plans/experiment-ux-flow.md 已釐清 2026-11-02）
            "mode": mode,
            "mode_label": RUN_MODES.get(mode, mode),
            # 電源資料怎麼來的：controlled＝由正式實驗流程控制；passive＝測試模式只被動記錄其他流程（手動模式或上一場）
            # 已建立的連線，測試模式沒有送任何命令；none＝沒有電源資料
            "psu_logging": ("controlled" if mode == "experiment" else "passive" if self.psu.connected else "none"),
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
            "experiment_labels": p["labels"],
            "enabled": dict(enabled),
            # 模擬資料絕不能被誤認為真實量測：這裡列出所有模擬來源
            "simulated": sorted(k for k in self.simulated if enabled.get(k)
                                or (k == "psu" and self.psu.connected)),
            "simulate_faults": self.sim_faults,
            # 寫進實驗紀錄，之後才知道 Temperature(C) 套了哪一組校正（RawTemperature(C) 是溫度計原始讀值）
            "temperature_calibration": (None if "temp" in self.simulated or not enabled["temp"] else
                                        {"gain": TEMP_CAL_GAIN, "offset": TEMP_CAL_OFFSET, "date": "2026-10-22",
                                         "valid_range_c": [24, 38]}),
            "devices": {
                "temperature_com_port": (("模擬（sim_devices.SimTemperature）" if "temp" in self.simulated
                                          else com_port) if enabled["temp"] else None),
                "audio_device": (("模擬（sim_devices.SimAudioRecorder）" if "audio" in self.simulated
                                  else audio_sel) if enabled["audio"] else None),
                "rangefinder": (("模擬（sim_devices.SimLKIF2Device）" if "distance" in self.simulated
                                 else "KEYENCE LK-G5000 (LKIF2.dll)") if enabled["distance"] else None),
                "spectrometer": (("模擬（sim_devices.SimSpectrometer）" if "spectrometer" in self.simulated
                                  else "AvaSpec-ULS2048L (avaspecx64.dll)") if enabled["spectrometer"] else None),
                "power_supply": self._psu_device_label() if self.psu.connected else None,
            },
            "platform": {"os": platform.platform(), "python": platform.python_version()},
            "spectrogram": None,
            "spectrometer": None,  # 光譜儀 worker 開啟裝置後補上 metadata（波長範圍、像素數、積分時間…）
        }
        info.update(extra_info or {})
        recorder = None
        try:
            recorder = ExperimentRecorder(experiment_id, info)
            recorder.on_write_error = self._on_write_error
            recorder.attach_log()
            if info["simulated"]:
                self._write_simulated_txt(recorder, info)
            if enabled["temp"]:
                recorder.open_csv("temperature", f"temperature_{experiment_id}.csv",
                                  ["Timestamp", "Elapsed(s)", "Temperature(C)", "Status", "RawTemperature(C)"],
                                  encoding="utf-8-sig")
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
            if self.phase in ("running", "stopping", "checking"):
                recorder.close_all()
                recorder.close_log()
                return {"ok": False, "errors": ["監測已在進行中"]}, 409
            self.run_id += 1
            self.recorder = recorder
            self.run_mode = mode
            self.spectrogram_job = None
            self.write_failures = set()
            self.params = {k: str(raw.get(k, DEFAULT_PARAMS[k])) for k in DEFAULT_PARAMS}
            self.params["com_port"] = com_port  # 畫面顯示實際使用的埠（自動偵測的結果）
            self.enabled = enabled
            self.duration = p["duration"]
            self._reset_buffers(p)
            self._psu_csv_rec = None
            self.last_result = None
            self.elapsed = 0.0
            self.start_time = start_ts
            self.stop_event = threading.Event()
            self.finalizing = False
            self._reset_sensor_display()
            parts = [name for key, name in (("temp", "溫度"), ("audio", "音訊"), ("distance", "距離"),
                                     ("spectrometer", "光譜"))
                     if enabled[key]]
            self.status_text = (f"{'Sensor 測試模式（非正式實驗）' if mode == 'sensor_test' else '實驗錄製中'}"
                                f" ({'+'.join(parts) or '無感測器'})")
            self.phase = "running"
            self._show_disabled(enabled, pf["diag"])
            stop_event = self.stop_event
            run_id = self.run_id
        if self.simulated:
            self.sim_temp = sim_devices.SimTemperature(faults=self.sim_faults)

        self._log_start(experiment_id, recorder, info, p, raw, pf, com_port, audio_sel)
        alarm = self.psu.snapshot()["alarm"]
        if alarm:  # 開始前就存在的電源訊息（例如連線時就有的代碼），代碼改變才會再回呼，這裡先記一次
            self._psu_on_alarm(alarm, before_start=True)
        self.publish("reset", {"run_id": run_id, "audio_meta": self.audio_meta()})
        self.push_state()

        threads = {"timer": threading.Thread(target=self._timer_worker,
                                             args=(stop_event, p["duration"]), name="timer", daemon=True)}
        threads.update(self._make_workers(stop_event, enabled, p, com_port, audio_sel, recorder))
        with self.lock:
            self.threads = threads
        for t in threads.values():
            t.start()
        return {"ok": True, "state": self.snapshot()}, 200

    def _reset_buffers(self, p):
        """開始新一輪（錄製或試收）前清空顯示用緩衝。呼叫端持有 self.lock。"""
        self.sample_rate = p["sample_rate"]
        self.history_duration = p["history_duration"]
        self.spec_pending = np.array([], dtype=np.int16)
        self.spec_cols = collections.deque(maxlen=self.audio_meta()["max_cols"])
        self.spec_total = 0
        self.spec_freqs = None
        self.series = {"temp": [], "distance": [], "psu_u_a": [], "psu_u_b": [], "psu_i_a": [], "psu_i_b": []}
        self.series_sent = {"temp": 0, "distance": 0, "psu_u_a": 0, "psu_u_b": 0, "psu_i_a": 0, "psu_i_b": 0}
        self.opt_meta = None
        self.opt_cols = collections.deque(maxlen=self._opt_max_cols(p["spec_interval"], p["history_duration"]))
        self.opt_total = 0
        self.opt_latest = None
        self.zero_warned = False

    def _show_disabled(self, enabled, diag):
        """未啟用的感測器在狀態卡顯示「未啟用」與預檢原因；音訊顯示初始化中。呼叫端持有 self.lock。"""
        for key, name in SENSOR_NAMES.items():
            if not enabled[key]:
                r = diag[key]
                self.sensors[key].update(
                    text="未啟用", level="off", reason=R_DISABLED,
                    detail=self._disabled_detail(name, r["detail"]))
        if enabled["audio"]:
            self.sensors["audio"].update(text="初始化中", level="idle", reason=R_INIT,
                                         detail="正在開啟音訊設備")

    def _make_workers(self, stop_event, enabled, p, com_port, audio_sel, recorder):
        """建立（不啟動）各感測器 worker thread；錄製與「檢驗Sensor與電供」試收共用。"""
        threads = {}
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
        return threads

    def _write_simulated_txt(self, recorder, info):
        with open(recorder.path("SIMULATED.txt"), "w", encoding="utf-8-sig") as f:
            f.write("本實驗資料夾含模擬資料，不是真實量測！\n"
                    "THIS FOLDER CONTAINS SIMULATED DATA, NOT REAL MEASUREMENTS.\n"
                    f"模擬來源 / simulated: {', '.join(info['simulated'])}\n"
                    f"故障模擬 / faults: {self.sim_faults}\n")

    # ---------------- 電源供應器（TruPlasma） ----------------
    def _psu_open_port(self):
        """controller 的 port_factory（在 controller thread 上呼叫）：模擬時回傳模擬器，否則開真實 COM 埠。"""
        if "psu" in self.simulated:
            return SimTruPlasma(faults=self.sim_faults)
        return open_serial(self.psu_port)

    def _psu_device_label(self):
        return "模擬（truplasma_package.SimTruPlasma）" if "psu" in self.simulated else self.psu_port

    def psu_busy_ports(self):
        """電源連線中使用的 COM 埠；溫度計自動偵測要跳過，不能去探測它。"""
        return (self.psu_port,) if self.psu_port and self.psu.phase != "disconnected" else ()

    def psu_connect(self, port):
        """連線（已連線／連線中則不動作）。等第一輪通訊完成才回傳；失敗時丟 RuntimeError（訊息給使用者看）。"""
        port = (port or PSU_DEFAULT_PORT).strip()
        with self.psu_lock:
            if self.psu.phase != "disconnected":
                return
            t = self.psu.thread
            if t and t.is_alive():  # 上一次的 thread 還在收尾（controller.connect 會直接忽略）
                t.join(2)
            self.psu_port = port
            self.psu.connect()
        deadline = time.monotonic() + 8
        while self.psu.phase == "connecting" and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.psu.phase == "disconnected":
            raise RuntimeError(self.psu.error or f"無法連線電源（{port}）")

    def _psu_on_event(self, level, msg):
        """controller 的事件：controller 自己寫一行（來源「系統」），這裡再寫一行（來源「電源」），兩行等級相同。
        "quiet"（Test version 家族等）只寫 INFO、不跳通知。"""
        try:
            if level == "quiet":
                slog(INFO, PSU_NAME, f"{msg}（未通知）")
                return
            self.notify(level if level in ("info", "warning", "error", "success") else "info", PSU_NAME, msg,
                        src=PSU_NAME)
        except Exception as e:  # noqa: BLE001  callback 不可丟例外
            slog(ERROR, PSU_NAME, f"電源事件通知失敗: {e}", e)

    def _psu_on_alarm(self, alarm, before_start=False):
        """電源警報／警告代碼改變時：實驗錄製中就把代碼、翻譯與電源原文照實寫進 experiment JSON。"""
        with self.lock:
            rec = self.recorder if self.phase == "running" else None
        if rec is None:
            return
        try:
            rec.note_code(PSU_NAME, str(alarm["code"]), value=alarm["code"], text=alarm.get("text", ""),
                          raw=alarm.get("raw") or None, level=alarm.get("level"))
            if before_start:
                rec.update(psu_message_at_start=dict(alarm))
        except OSError as e:
            slog(ERROR, PSU_NAME, f"電源代碼寫入實驗 JSON 失敗: {e}", e)

    def _psu_on_cycle(self, snap):
        """controller thread 每輪呼叫：推 SSE，實驗錄製中再寫 CSV 與曲線。不可丟例外、不可卡住。"""
        try:
            self.publish("psu", snap)
            self._psu_track_comm(snap)
            self._psu_record(snap)
        except Exception as e:  # noqa: BLE001
            self._log_exception_once("psu", e, "電源資料處理錯誤")

    def _psu_track_comm(self, snap):
        """電源通訊的暫時失誤／真故障（同感測器的 FailureTracker）：heartbeat 沒回應時每輪記一次失敗。
        輸出開著時連續 3 次失敗由 controller 自己關輸出並通知；這裡補上輸出關閉時也會發生的長時間無回應。"""
        event = None
        with self.lock:
            tr = self._tracker("psu")
            if not snap.get("connected"):
                tr.ep = None  # 斷線由 controller 的「電源連線中斷」事件回報
                return
            now = time.time()
            if snap.get("fail_count"):
                detail = f"電源沒有回應 heartbeat（{snap.get('error') or '逾時'}）：確認電源開機、RS232/USB 轉接線"
                action = tr.fail(now, R_NO_DATA, snap.get("error") or "逾時", True, detail)
                if action:
                    event = (action, tr.ep)
            else:
                event = tr.ok(now)
        if event:
            self._failure_event("psu", PSU_NAME, *event)

    def _psu_record(self, snap):
        st = snap.get("status")
        if not snap.get("connected") or st is None:
            return
        with self.lock:
            rec = self.recorder if self.phase == "running" else None
            start = self.start_time
        if rec is None:
            return
        now = time.time()
        sp = snap["setpoints"]
        alarm = snap.get("alarm") or {}
        meas = snap.get("measurements") or {}
        counts = st.get("arc_counts") or {}
        row = ([iso_now(now), f"{now - start:.3f}", snap["phase"], sp["u"], sp["i"], sp["p"],
                st["u_act"], st["i_act_peak"], st["p_act"]]
               + ["" if meas.get(name) is None else meas[name] for _, name in MEASUREMENTS]
               + [int(st["relays_on"]), int(st["power_on"]), int(st["interlock"]), int(st["alarm_active"]),
                  int(st["warning_active"]), alarm.get("code", ""), st["mode"], st["regulation"], st["arc_rate"]]
               + [counts.get(k, "") for k in ("Imax", "UxI", "dU", "Usag", "ArcBurst")]
               + list(st["bits"]))
        with self._psu_csv_lock:
            with self.lock:  # 停止流程會先把 phase 改掉再收尾，這裡重查才不會在關檔後又開新檔
                if self.phase != "running" or self.recorder is not rec:
                    return
            if self._psu_csv_rec is not rec:
                self._psu_csv_rec = rec  # 開檔失敗也只試一次，避免每輪洗版
                try:
                    rec.open_csv("truplasma", f"truplasma_{rec.experiment_id}.csv", PSU_CSV_HEADER,
                                 encoding="utf-8-sig")
                except OSError as e:
                    slog(ERROR, PSU_NAME, f"無法建立電源 CSV: {e}", e)
                    rec.add_error(f"truplasma CSV 建立失敗: {e}")
                    return
                self._psu_mark_recorder(rec)
            rec.write_row("truplasma", row)
        self.add_series_point("psu_u_a", meas.get("Uavg_A_V"))
        self.add_series_point("psu_u_b", meas.get("Uavg_B_V"))
        self.add_series_point("psu_i_a", meas.get("Iavg_A_A"))
        self.add_series_point("psu_i_b", meas.get("Iavg_B_A"))

    def _psu_mark_recorder(self, rec):
        """電源在實驗中途才連線時，補寫裝置與模擬標示（實驗開始時已連線的在 start() 就寫好了）。"""
        info = rec.info
        sim = list(info.get("simulated") or [])
        if "psu" in self.simulated and "psu" not in sim:
            sim.append("psu")
            info["simulated"] = sim
            self._write_simulated_txt(rec, info)
        fields = {"devices": dict(info.get("devices") or {}, power_supply=self._psu_device_label())}
        if info.get("mode") == "sensor_test":
            fields["psu_logging"] = "passive"  # 測試模式中途才連上的電源：一樣只被動記錄
        rec.update(**fields)

    def _psu_close_csv(self, recorder):
        with self._psu_csv_lock:  # 等進行中的懶開／寫入做完，之後 phase 已不是 running，不會再開
            if self._psu_csv_rec is recorder:
                recorder.close("truplasma")
                self._psu_csv_rec = None

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
                        f"{key} 資料寫入失敗（磁碟可能已滿或被拔除）。\n監測仍在進行，請盡快釋放空間或停止監測。")

    # ---------------- 正式實驗流程 ----------------
    # 設計見 docs/plans/experiment-ux-flow.md（以「已決定（2026-10-31）」與「已釐清（2026-11-02）」為準）。
    # 安全原則：電源相關異常一律讓輸出關閉、錄製繼續；感測器異常不停電源。開始順序在伺服器端執行，瀏覽器關掉也照跑。
    def _mark_valid(self, key):
        """worker 收到一筆有效數據時呼叫；只在「檢驗Sensor與電供」試收期間計數。"""
        with self.lock:
            if self._valid_hits is not None:
                self._valid_hits[key] = self._valid_hits.get(key, 0) + 1

    def exp_active(self):
        """正式實驗是否進行中（從開始錄製到存檔完成）；這段期間電源參數與輸出鎖定。"""
        with self.lock:
            return self.exp is not None and self.exp["stage"] != "done"

    def _exp_public(self):
        """snapshot 用的實驗流程狀態（不含 recorder 物件）。呼叫端可持有 self.lock。"""
        e = self.exp
        if e is None:
            return None
        out = {k: v for k, v in e.items() if k != "recorder"}
        out["active"] = e["stage"] != "done"
        out["result_label"] = EXP_RESULTS.get(e["result"]) if e["result"] else None
        on_at, off_at = e["output_on_at"], e["output_off_at"]
        out["on_elapsed"] = round(((off_at or time.time()) - on_at), 2) if on_at else None
        out["remaining"] = (round(max(0.0, e["duration_s"] - (time.time() - on_at)), 2)
                            if on_at and not off_at else None)
        return out

    def _exp_parse(self, raw):
        """驗證標籤、感測器參數、電源參數與實驗時間。回傳 (errors, recipe, duration_s)。"""
        errors = []
        try:
            self.validate_params(raw)  # 標籤（必填）與感測器參數
        except ValueError as e:
            errors.append(str(e))
        recipe = None
        r = raw.get("recipe")
        try:
            plan_recipe({}, r)  # 模式、頻率、佔空比、U/I/P 範圍（同電源面板的「套用設定」）
            recipe = {"mode": r["mode"], "freq_hz": float(r["freq_hz"]),
                      **({"pos_pct": float(r["pos_pct"]), "neg_pct": float(r["neg_pct"])} if r["mode"] == "bipulse"
                         else {"duty_pct": float(r["duty_pct"])}),
                      "u": float(r["u"]), "i": float(r["i"]), "p": float(r.get("p", RECIPE_LIMITS["p_default"]))}
            if recipe["u"] <= 0 or recipe["i"] <= 0:
                raise ValueError("電壓上限與電流上限都必須大於 0，否則電源不會輸出")
        except (ValueError, TypeError, KeyError) as e:
            errors.append(f"電源參數錯誤：{e}")
            recipe = None
        duration = None
        try:
            duration = parse_exp_time(raw.get("exp_time"))
        except ValueError as e:
            errors.append(str(e))
        return errors, recipe, duration

    def _exp_apply_recipe(self, recipe):
        """把電源參數寫入電源並讀回比對（只能在輸出關閉時）。不一致丟 RuntimeError；成功回傳讀回值。"""
        psu = self.psu
        if not psu.connected:
            raise RuntimeError("電源未連線")
        if psu.phase != "idle":
            raise RuntimeError(f"電源目前「{psu.snapshot()['phase_text']}」，參數只能在輸出關閉時寫入")
        self.psu_recipe_errors = [RECIPE_INCOMPLETE]  # 中途失敗（部分通道可能已寫入）也要擋住開啟輸出
        current = psu.read_params()
        for ch, value in plan_recipe(current, recipe):
            psu.write_param(ch, value)  # write_param 自己會讀回比對，不一致丟 RuntimeError
        with psu.lock:
            if psu.phase != "idle":
                raise RuntimeError("電源輸出已開啟，取消寫入")
            psu.set_setpoints(recipe["u"], recipe["i"], recipe["p"])
        readback = psu.read_params()
        mismatch = recipe_readback_errors(readback, recipe)
        snap = psu.snapshot()
        sp, meas = snap["setpoints"], snap["measurements"]
        for k, label in (("u", "電壓上限"), ("i", "電流上限"), ("p", "功率上限")):
            if abs(sp[k] - recipe[k]) > 1e-6:
                mismatch.append(f"{label}讀回 {sp[k]:g}，應為 {recipe[k]:g}")
        self.psu_recipe_errors = mismatch
        if mismatch:
            raise RuntimeError("電源參數寫入後讀回不一致，不能開啟輸出：" + "；".join(mismatch))
        # actual：電源回報的實際頻率／佔空比（ch51140／51141，每輪心跳輪詢的量測值）
        return {"mode": recipe["mode"], "freq_hz": recipe["freq_hz"],
                **({"pos_pct": recipe["pos_pct"], "neg_pct": recipe["neg_pct"]} if recipe["mode"] == "bipulse"
                   else {"duty_pct": recipe["duty_pct"]}),
                "setpoints": dict(sp), "read_at": iso_now(),
                "channels": {str(ch): readback.get(ch) for ch in RECIPE_READBACK_CHANNELS},
                "actual": {"freq_hz": meas.get("Freq_Hz"), "duty_pct": meas.get("Duty_pct")}}

    def _psu_auto_reset_alarm(self):
        """「檢驗」前的鎖存警報自動重設（只一次）。回傳 None＝沒動作；"cleared"／"failed"＝已重設且清掉／仍未清除。
        只在警報啟用、輸出 idle、且代碼屬於 AUTO_RESET_ALARM_CODES 時才送重設；代碼讀不到就不動。"""
        end = time.monotonic() + 1.5
        snap = self.psu.snapshot()
        while time.monotonic() < end and (snap["status"] is None or (snap["status"]["alarm_active"]
                                                                     and snap["alarm"] is None)):
            time.sleep(0.1)
            snap = self.psu.snapshot()
        st, alarm = snap["status"], snap["alarm"]
        if st is None or not st["alarm_active"] or snap["phase"] != "idle" or alarm is None:
            return None
        code = alarm["code"]
        if code not in AUTO_RESET_ALARM_CODES:
            return None
        try:
            self.psu.reset_alarms()
        except RuntimeError:
            return None
        end = time.monotonic() + AUTO_RESET_WAIT_S
        while time.monotonic() < end:
            time.sleep(0.1)
            now = self.psu.snapshot()["status"]
            if now is not None and not now["alarm_active"]:
                break
        cleared = not (self.psu.snapshot()["status"] or {}).get("alarm_active", True)
        meaning = ALARM_TEXT.get(code) or ("Test version 韌體訊息" if code in TEST_VERSION_CODES else "")
        slog(INFO if cleared else WARN, PSU_NAME,
             f"檢驗時自動重設警報 {code}（{meaning}）：" + ("已清除" if cleared else "重設後仍未清除"))
        return "cleared" if cleared else "failed"

    def _psu_ready_errors(self, auto_reset=None):
        """電源能不能開始：已連線、輸出關閉、RS232 控制、Ready、無警報、Interlock 正常、通訊正常。
        auto_reset="failed"＝剛自動重設過警報仍未清除，警報訊息加註。"""
        snap = self.psu.snapshot()
        st = snap["status"]
        if not snap["connected"]:
            return [f"電源未連線{('：' + snap['error']) if snap['error'] else ''}"]
        errs = []
        if snap["phase"] != "idle":
            errs.append(f"電源目前「{snap['phase_text']}」，開始前輸出必須是關閉的")
        if st is None:
            errs.append("尚未收到電源狀態")
            return errs
        if not st["rs_control"]:
            errs.append("電源不是 RS232 控制：網頁無法控制，請確認電源面板的控制來源")
        if not st["ready"]:
            errs.append("電源未就緒（Ready = 0）")
        if st["alarm_active"]:
            a = snap["alarm"]
            errs.append("電源有警報" + (f" {a['code']}：{a['text']}（原文：{a['raw'] or '無'}）" if a else "")
                        + "，請先排除原因"
                        + ("（已自動重設一次仍未清除，請檢查 RS232 線／COM 埠，或到「電供設定手動模式」處理）"
                           if auto_reset == "failed" else ""))
        if st["interlock"]:
            errs.append("Interlock 觸發中（上蓋開關或安全迴路），請檢查安全迴路")
        if snap["fail_count"]:
            errs.append(f"電源通訊不穩：最近 {snap['fail_count']} 次沒有回應（{snap['error'] or '逾時'}）")
        return errs

    def exp_check(self, raw):
        """「檢驗Sensor與電供」：連線並寫入／讀回電源參數、檢查電源狀態，四個感測器試收 sensor_check_s 秒。
        電源不正常 → 不能開始；感測器異常 → 列出問題，可選「仍要開始」。"""
        with self.exp_lock:
            with self.lock:
                if self.phase in ("running", "stopping", "checking"):
                    return {"ok": False, "errors": ["監測已在進行中" if self.phase != "checking" else "正在檢驗Sensor與電供"]}, 409
                prev = (self.phase, self.status_text)
                self.phase, self.status_text = "checking", "檢驗Sensor與電供 中…"
                self._check_result = None  # 重新檢驗（不論成敗）都讓上一次的結果作廢
            self.push_state()
            try:
                errors, recipe, duration = self._exp_parse(raw)
                if errors:
                    return {"ok": False, "errors": errors}, 400
                psu_errors = []
                readback = None
                if not self.psu.connected:
                    try:
                        self.psu_connect(str(raw.get("psu_port") or ""))
                    except RuntimeError as e:
                        psu_errors.append(f"電源連線失敗：{e}（檢查電源主開關、RS232 線、COM 埠是否被 PVDPower 佔用）")
                auto_reset = self._psu_auto_reset_alarm() if self.psu.connected else None
                if self.psu.connected:
                    try:
                        readback = self._exp_apply_recipe(recipe)
                    except (ValueError, RuntimeError, OSError) as e:
                        psu_errors.append(str(e) or "電源沒有回應（逾時）")
                sensors, sys_errors = self._sensor_trial(raw)
                if self.psu.connected or not psu_errors:
                    psu_errors += [e for e in self._psu_ready_errors(auto_reset) if e not in psu_errors]
            finally:
                with self.lock:
                    if self.phase == "checking":
                        self.phase, self.status_text = prev
                self.push_state()
        missing = [dict(key=k, **v) for k, v in sensors.items() if not v["ok"]]
        result = {"ok": True, "psu_ok": not psu_errors, "psu_errors": psu_errors, "sensors": sensors,
                  "missing": missing, "system_errors": sys_errors, "recipe": recipe, "readback": readback,
                  "duration_s": duration, "can_start": not psu_errors and not sys_errors,
                  "check_s": self.sensor_check_s, "baseline_s": self.exp_baseline_s}
        self._check_result = dict(result, time=time.time())
        for k, v in sensors.items():
            slog(INFO if v["ok"] else WARN, SENSOR_NAMES[k],
                 f"檢驗Sensor與電供：{'正常' if v['ok'] else '異常'}（有效數據 {v['valid']} 筆）"
                 + ("" if v["ok"] else f" [{v['reason']}] {v['detail']}"))
        slog(INFO if result["can_start"] else ERROR, PSU_NAME,
             "檢驗Sensor與電供：電源" + ("正常" if not psu_errors else "不正常，不能開始：" + "；".join(psu_errors))
             + (f"；讀回 {json.dumps(readback, ensure_ascii=False)}" if readback else ""))
        return result, 200

    def _sensor_trial(self, raw):
        """試收：照開始錄製的方式啟動各感測器 worker，但不寫任何檔案（_NullRecorder）。
        回傳 ({key: {ok, reason, reason_label, detail, valid, name}}, 系統錯誤)。"""
        pf = self.preflight(raw, require_sensor=False)
        sys_errors = list(pf["errors"])
        p, enabled, diag = pf["parsed"], pf["enabled"], pf["diag"]
        hits = {k: 0 for k in SENSOR_NAMES}
        if p is not None and any(enabled.values()):
            stop_event = threading.Event()
            with self.lock:
                self.run_id += 1
                self._reset_buffers(p)
                self.enabled = enabled
                self.params = {k: str(raw.get(k, DEFAULT_PARAMS[k])) for k in DEFAULT_PARAMS}
                self.params["com_port"] = pf["com_port"]
                self.elapsed = 0.0
                self.start_time = time.time()
                self._reset_sensor_display()
                self._show_disabled(enabled, diag)
                self._valid_hits = dict(hits)
                run_id = self.run_id
            if self.simulated:
                self.sim_temp = sim_devices.SimTemperature(faults=self.sim_faults)
            self.publish("reset", {"run_id": run_id, "audio_meta": self.audio_meta()})
            self.push_state()
            threads = self._make_workers(stop_event, enabled, p, pf["com_port"],
                                         str(raw.get("audio_device") or ""), _NullRecorder())
            for t in threads.values():
                t.start()
            t0 = time.time()
            try:
                while True:
                    now = time.time()
                    with self.lock:
                        self.elapsed = now - t0
                        pending = [k for k in enabled if enabled[k] and not self._valid_hits.get(k)
                                   and self.sensors[k]["reason"] in (R_IDLE, R_INIT)]
                    if now - t0 >= self.sensor_check_s and (
                            not pending or now - t0 >= self.sensor_check_s + SENSOR_CHECK_INIT_GRACE_S):
                        break
                    self.flush_series()
                    time.sleep(0.1)
            finally:
                with self.lock:
                    hits = dict(self._valid_hits)
                    sensors_now = json.loads(json.dumps(self.sensors))
                stop_event.set()
                for name, t in threads.items():
                    t.join(timeout=5 if name in ("audio", "distance", "spectrometer") else 2)
                with self.lock:
                    self._valid_hits = None
                self.flush_series()
        else:
            with self.lock:
                sensors_now = json.loads(json.dumps(self.sensors))
        out = {}
        sec = f"{self.sensor_check_s:g}"
        for k, name in SENSOR_NAMES.items():
            s = sensors_now[k]
            if not enabled[k]:
                reason, detail = diag[k]["reason"], self._disabled_detail(name, diag[k]["detail"])
                if reason == R_OK:
                    reason = R_DISABLED
            elif hits[k] and s["level"] != "error":
                reason, detail = R_OK, ""
            elif s["reason"] not in NON_FAILURE_REASONS:
                reason, detail = s["reason"], s["detail"] or f"{sec} 秒內沒有收到有效數據"
            elif k == "audio":
                reason, detail = R_NO_DATA, (f"{sec} 秒內錄到的訊號全為 0：麥克風權限未開、設備靜音或未接；"
                                             "檢查 Windows 隱私權設定與麥克風開關")
            else:
                reason, detail = R_NO_DATA, f"{sec} 秒內沒有收到有效數據：檢查接線、電源與驅動"
            out[k] = {"name": name, "ok": reason == R_OK, "reason": reason, "reason_label": REASON_LABELS.get(reason, reason),
                      "detail": detail, "valid": hits[k]}
        return out, sys_errors

    def exp_start(self, raw):
        """開始正式實驗：再次寫入／讀回電源參數並檢查電源，開始錄製，之後由 _exp_sequence 在伺服器端依序執行
        基線 → 主繼電器 → 等就緒 → 輸出 ON → 計時。電源不正常一律不開始（也不錄製）。
        一接受開始請求就先登錄實驗（stage="preparing"），從這一刻起電源由實驗流程獨佔：手動面板的輸出與參數
        修改在伺服器端一律拒絕；之後任何一步失敗就釋放。"""
        with self.exp_lock:
            with self.lock:
                if self.phase in ("running", "stopping", "checking"):
                    return {"ok": False, "errors": ["監測已在進行中" if self.phase != "checking" else "正在檢驗Sensor與電供"]}, 409
            if raw.get("confirm_output") is not True:
                return {"ok": False, "errors": ["開始前必須確認輸出高壓（confirm_output）"]}, 400
            errors, recipe, duration = self._exp_parse(raw)
            if errors:
                return {"ok": False, "errors": errors}, 400
            chk = self._check_result
            if chk is None:
                return {"ok": False, "errors": ["請先按「檢驗Sensor與電供」"]}, 400
            if time.time() - chk["time"] > CHECK_VALID_S:
                self._check_result = None
                return {"ok": False, "errors": [f"檢驗結果已超過 {CHECK_VALID_S // 60:g} 分鐘，請重新「檢驗Sensor與電供」"]}, 400
            if not chk["can_start"]:
                return {"ok": False, "errors": ["上次檢驗電源不正常，請排除後重新「檢驗Sensor與電供」"]
                        + chk["psu_errors"] + chk["system_errors"], "psu_errors": chk["psu_errors"]}, 400
            if chk["recipe"] != recipe or chk["duration_s"] != duration:
                return {"ok": False, "errors": ["電源參數或實驗時間和檢驗時不同，請重新「檢驗Sensor與電供」"]}, 400
            if chk["missing"] and raw.get("confirm_missing") is not True:
                return {"ok": False, "needs_confirm": True, "missing": chk["missing"],
                        "errors": [f"{m['name']}：{m['detail']}" for m in chk["missing"]]}, 409
            # 先登錄、上鎖，再做讀回比對與開始錄製
            exp = {"id": None, "recorder": None, "stage": "preparing",
                   "message": "準備開始：寫入並讀回電源參數、開始錄製",
                   "duration_s": duration, "baseline_s": self.exp_baseline_s, "recipe": recipe, "readback": None,
                   "missing": list(chk["missing"]), "result": None, "result_detail": "", "fault": None,
                   "record_start": None, "output_on_at": None, "output_off_at": None, "output_off_cmd_at": None,
                   "end_req": False, "estop": False, "stopping": False}
            with self.lock:
                prev_exp, self.exp = self.exp, exp
            self.push_state()
            started = False
            try:
                body, code = self._exp_prepare(exp, raw, chk, recipe, duration)
                started = code == 200
                return body, code
            finally:
                if not started:
                    with self.lock:
                        if self.exp is exp:
                            self.exp = prev_exp  # 釋放鎖定，畫面回到上一場
                    self.push_state()

    def _exp_prepare(self, exp, raw, chk, recipe, duration):
        """exp_start 的後半（實驗已登錄、電源已鎖定）：讀回比對 → 開始錄製 → 啟動流程 thread。"""
        psu_errors = []
        readback = None
        if not self.psu.connected:
            psu_errors.append("電源未連線：請重新「檢驗Sensor與電供」")
        else:
            try:
                readback = self._exp_apply_recipe(recipe)
            except (ValueError, RuntimeError, OSError) as e:
                psu_errors.append(str(e) or "電源沒有回應（逾時）")
            psu_errors += [e for e in self._psu_ready_errors() if e not in psu_errors]
        if psu_errors:
            slog(ERROR, PSU_NAME, "開始實驗被拒絕（電源不正常）：" + "；".join(psu_errors))
            return {"ok": False, "errors": psu_errors, "psu_errors": psu_errors}, 400
        if exp["estop"]:
            return {"ok": False, "errors": ["準備開始時按了緊急停止，已取消開始（沒有開始錄製）"]}, 409
        extra = {
            "power_supply_settings": {"requested": recipe, "readback_before_start": readback},
            "experiment_time_s": duration,
            "baseline_s": self.exp_baseline_s,
            "sensor_check": chk["sensors"],
            "result": None,
            "missing_sensors": chk["missing"],
            "output": {"on_time": None, "off_command_time": None, "off_time": None, "timer_s": None, "on_s": None,
                       "record_to_on_s": None},
        }
        body, code = self.start(raw, True, mode="experiment", extra_info=extra)
        if code != 200:
            return body, code
        with self.lock:
            recorder = self.recorder
            enabled = dict(self.enabled)
            start_ts = self.start_time
        # 試收時正常、開始時的預檢卻停用的感測器也算缺少
        missing = list(chk["missing"])
        for k, name in SENSOR_NAMES.items():
            if not enabled[k] and all(m["key"] != k for m in missing):
                missing.append({"key": k, "name": name, "ok": False, "reason": R_DISABLED,
                                "reason_label": REASON_LABELS[R_DISABLED], "detail": "開始錄製時預檢停用", "valid": 0})
        with self.lock:
            exp.update(id=recorder.experiment_id, recorder=recorder, readback=readback, missing=missing,
                       record_start=start_ts)
            if not exp["estop"]:  # 準備中按了緊急停止：錄製已開始就照緊急停止語意（不開輸出、錄製繼續）
                exp["stage"] = "baseline"
                exp["message"] = f"錄製基線中（電源關閉，{self.exp_baseline_s:g} 秒後開啟輸出）"
            self._check_result = None  # 每一場都要重新檢驗
        if missing != chk["missing"]:
            recorder.update(missing_sensors=missing)
        self._exp_update(exp)
        slog(INFO, SRC_SYSTEM, f"正式實驗 {exp['id']} 開始：實驗時間 {duration:g} 秒，基線 {self.exp_baseline_s:g} 秒；"
             f"電源參數 {json.dumps(recipe, ensure_ascii=False)}"
             + (f"；缺少感測器 {'、'.join(m['name'] for m in missing)}（使用者確認仍要開始）" if missing else ""))
        threading.Thread(target=self._exp_sequence, args=(exp,), name="exp_sequence", daemon=True).start()
        return {"ok": True, "state": self.snapshot()}, 200

    def _exp_recording(self, exp):
        with self.lock:
            return self.phase == "running" and self.recorder is exp["recorder"]

    def _exp_update(self, exp, stage=None, message=None, **fields):
        """更新流程狀態（畫面）並把需要的欄位寫進 manifest。
        緊急停止之後階段只會停在 estop（存檔完成時由 _finalize 改成 done），其他流程不能再把它蓋掉。"""
        with self.lock:
            if exp["estop"] and stage not in (None, "estop"):
                stage = message = None
            if stage:
                exp["stage"] = stage
            if message is not None:
                exp["message"] = message
            exp.update(fields)
            out = exp["output_on_at"], exp["output_off_at"]
            cmd = exp["output_off_cmd_at"] or out[1]
            # off_command_time：送出關閉（定時器到／結束／異常／緊急停止）的時間；off_time：電源回報輸出與主繼電器都已關閉
            output = {"on_time": iso_now(out[0]) if out[0] else None,
                      "off_command_time": iso_now(cmd) if out[0] and cmd else None,
                      "off_time": iso_now(out[1]) if out[1] else None,
                      "timer_s": round(cmd - out[0], 3) if out[0] and cmd else None,
                      "on_s": round(out[1] - out[0], 3) if out[0] and out[1] else None,
                      "record_to_on_s": round(out[0] - exp["record_start"], 3) if out[0] else None}
            result = ({"code": exp["result"], "label": EXP_RESULTS[exp["result"]], "detail": exp["result_detail"],
                       "time": exp.get("result_time")} if exp["result"] else None)
            rec = exp["recorder"]
        if rec is not None:  # 準備中（還沒開始錄製）沒有 manifest
            try:
                rec.update(output=output, result=result)
            except OSError as e:
                slog(ERROR, SRC_SYSTEM, f"實驗流程狀態寫入 JSON 失敗: {e}", e)
        self.push_state()

    def _exp_set_result(self, exp, code, detail):
        """結果標記：第一個原因為準（例如緊急停止後電源回報關閉，仍記為中止-使用者）。回傳是否由這次設定。"""
        with self.lock:
            if exp["result"]:
                return False
            exp["result"], exp["result_detail"], exp["result_time"] = code, detail, iso_now()
        slog(INFO if code == "completed" else WARN, SRC_SYSTEM, f"實驗結果：{EXP_RESULTS[code]}（{detail}）")
        return True

    def _psu_force_off(self):
        """電源保險關閉：輸出相關階段一律送緊急關閉（輸出與主繼電器同一幀放掉）；已關閉時不動作。"""
        if self.psu.phase in ("relays", "powering", "on", "stopping"):
            self.psu.emergency_stop()

    def _exp_mark_off_cmd(self, exp):
        """輸出開著時記下送出關閉的時間，並在背景等電源回報關閉後記 off_time。呼叫端不可持有 self.lock。"""
        with self.lock:
            if exp["output_on_at"] is None or exp["output_off_at"] is not None or exp["output_off_cmd_at"] is not None:
                return
            exp["output_off_cmd_at"] = time.time()
        threading.Thread(target=self._exp_watch_off, args=(exp,), name="exp_watch_off", daemon=True).start()

    def _exp_watch_off(self, exp):
        deadline = time.monotonic() + EXP_OFF_TIMEOUT_S
        while self.psu.phase not in ("idle", "disconnected") and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.psu.phase not in ("idle", "disconnected"):
            slog(WARN, PSU_NAME, f"{EXP_OFF_TIMEOUT_S:g} 秒內電源沒有回報輸出關閉，再送一次緊急關閉")
            self.psu.emergency_stop()
            return  # off_time 留空＝沒有收到電源的關閉回報
        with self.lock:
            if exp["output_off_at"] is not None:
                return
        self._exp_update(exp, output_off_at=time.time())

    def _psu_fault_cause(self):
        """輸出非預期關閉時判斷原因 → (結果代碼, 說明)。警報代碼由下一輪輪詢讀到，最多等 1.5 秒。"""
        if self.shutdown_done:
            return "aborted_system", "伺服器關閉，已關閉電源輸出"
        snap = self.psu.snapshot()
        if not snap["connected"] or snap["fail_count"]:
            return "aborted_comm_lost", (f"與電源通訊中斷（{snap['error'] or '沒有回應'}）；"
                                         "電源會在 4–5 秒內自行關閉輸出（ActSourceFail）")
        st = snap["status"] or {}
        if st.get("alarm_active"):
            end = time.monotonic() + 1.5
            while snap["alarm"] is None and time.monotonic() < end:
                time.sleep(0.1)
                snap = self.psu.snapshot()
            a = snap["alarm"]
            return "aborted_psu_alarm", ("電源警報，輸出已關閉" + (f"：{a['code']} {a['text']}（原文：{a['raw'] or '無'}）"
                                                          if a else "（警報代碼未讀到）"))
        if st.get("interlock"):
            return "aborted_interlock", "Interlock 觸發（上蓋開關或安全迴路被打開），電源已關閉輸出"
        return "aborted_psu_off", f"電源回報輸出已關閉：{snap['error'] or '原因不明'}"

    def _exp_sequence(self, exp):
        try:
            self._exp_run(exp)
        except Exception as e:  # noqa: BLE001  流程程式例外：先關輸出，錄製繼續，讓使用者結束本場
            slog(ERROR, SRC_SYSTEM, f"實驗流程例外: {e}", e)
            self._psu_force_off()
            self._exp_fault(exp, "aborted_system", f"實驗流程程式例外：{e}")

    def _exp_run(self, exp):
        psu = self.psu
        # 1) 基線：感測器已在錄、電源關閉
        t_on = exp["record_start"] + exp["baseline_s"]
        while time.time() < t_on:
            if exp["estop"]:
                return
            if exp["end_req"]:
                self._exp_set_result(exp, "aborted_user", "開始輸出前使用者結束（未開啟輸出）")
                self._exp_update(exp, "ending", "結束中（未開啟輸出）")
                self.request_stop("manual")
                return
            if not self._exp_recording(exp):
                self._exp_set_result(exp, "aborted_system", "錄製在開始輸出前被外部停止")
                return
            time.sleep(0.05)
        # 2) 主繼電器 → 等就緒 → 輸出 ON（順序與逾時由 controller 執行）
        if exp["estop"]:
            return
        self._exp_update(exp, "starting", "接通主繼電器、等待電源就緒")
        slog(INFO, PSU_NAME, f"基線 {time.time() - exp['record_start']:.1f} 秒完成，送出開啟輸出")
        # 「檢查緊急停止＋開啟」與 exp_estop 的「設旗標＋緊急關閉」用同一把鎖：緊急停止若在開啟前到，就不開；
        # 在開啟後到，controller 已在 relays 階段，emergency_stop 一定會生效（idle 時它不會動作）
        start_err = None
        with self._estop_lock:
            if exp["estop"]:
                return
            try:
                psu.power_on()
            except RuntimeError as e:
                start_err = e
        if start_err is not None:
            return self._exp_fault(exp, "aborted_psu_start_failed", f"電源無法開啟輸出：{start_err}")
        deadline = time.monotonic() + EXP_OUTPUT_ON_TIMEOUT_S
        while True:
            if exp["estop"]:
                self._psu_force_off()  # 保險：exp_estop 已關過一次
                return
            if not self._exp_recording(exp):
                self._psu_force_off()
                self._exp_set_result(exp, "aborted_system", "錄製在電源啟動中被外部停止，已關閉輸出")
                return
            if exp["end_req"]:
                return self._exp_finish(exp, "aborted_user", "電源啟動中使用者結束")
            phase = psu.phase
            if phase == "on":
                break
            if phase not in ("relays", "powering"):  # controller 已自行取消（繼電器／輸出逾時、警報、Interlock、通訊）
                code, why = self._psu_fault_cause()
                return self._exp_fault(exp, "aborted_psu_start_failed" if code in ("aborted_psu_off",) else code,
                                       f"電源無法開啟輸出：{why}")
            if time.monotonic() > deadline:
                self._psu_force_off()
                return self._exp_fault(exp, "aborted_psu_start_failed",
                                       f"{EXP_OUTPUT_ON_TIMEOUT_S:g} 秒內電源沒有進入輸出狀態，已關閉輸出")
            time.sleep(0.05)
        # 3) 輸出中：計時
        on_at = time.time()
        self._exp_update(exp, "on", f"輸出中（實驗時間 {exp['duration_s']:g} 秒）", output_on_at=on_at)
        slog(INFO, SRC_SYSTEM, f"電源輸出 ON，開始計時 {exp['duration_s']:g} 秒（錄製開始後 {on_at - exp['record_start']:.2f} 秒）")
        while True:
            if exp["estop"]:
                self._psu_force_off()
                return
            if not self._exp_recording(exp):
                self._psu_force_off()
                self._exp_set_result(exp, "aborted_system", "錄製在輸出中被外部停止，已關閉輸出")
                return
            if psu.phase != "on":
                code, why = self._psu_fault_cause()
                return self._exp_fault(exp, code, why)
            elapsed = time.time() - on_at
            if elapsed >= exp["duration_s"]:
                return self._exp_finish(exp, "completed", f"實驗時間 {exp['duration_s']:g} 秒到")
            if exp["end_req"]:
                return self._exp_finish(exp, "aborted_user", f"使用者按「結束實驗」提前結束"
                                        f"（通電 {elapsed:.1f} 秒／設定 {exp['duration_s']:g} 秒）")
            time.sleep(0.05)

    def _exp_finish(self, exp, code, detail):
        """正常結束：輸出 OFF → 主繼電器 OFF（controller 依序執行，等兩者都回報關閉）→ 立刻停止錄製 → 存檔。
        結束原因在這時就定下；結束中按了緊急停止 → 照緊急停止語意：輸出立即關閉、錄製繼續，等使用者按「停止錄製」。"""
        with self.lock:
            on = exp["output_on_at"] is not None and exp["output_off_at"] is None
        self._exp_set_result(exp, code, detail)
        self._exp_update(exp, "ending", "關閉輸出中", **({"output_off_cmd_at": time.time()} if on else {}))
        self.psu.power_off()
        deadline = time.monotonic() + EXP_OFF_TIMEOUT_S
        while self.psu.phase not in ("idle", "disconnected") and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.psu.phase not in ("idle", "disconnected"):
            slog(WARN, PSU_NAME, f"{EXP_OFF_TIMEOUT_S:g} 秒內電源沒有回報輸出與主繼電器關閉，改送緊急關閉")
            self.psu.emergency_stop()
        with self._estop_lock:
            estopped = exp["estop"]
            if not estopped:
                exp["stopping"] = True  # 之後的緊急停止只關電源，不再改階段（錄製已在停止）
        if estopped:
            if on and self.psu.phase in ("idle", "disconnected"):
                with self.lock:
                    pending = exp["output_off_at"] is None
                if pending:
                    self._exp_update(exp, output_off_at=time.time())
            slog(INFO, SRC_SYSTEM, "結束中按了緊急停止：輸出已關閉，錄製繼續，等使用者按「停止錄製」")
            return
        self._exp_update(exp, "ending", "輸出已關閉，停止錄製並存檔", **({"output_off_at": time.time()} if on else {}))
        slog(INFO, SRC_SYSTEM, "電源輸出與主繼電器已關閉，停止錄製")
        self.request_stop("timeout" if code == "completed" else "manual")

    def _exp_fault(self, exp, code, detail):
        """電源相關異常：保險再關一次輸出，錄製繼續；使用者只能「結束這場」（不提供重設警報後續跑）。"""
        with self.lock:
            if exp["estop"] or exp["stage"] == "done":
                return
        self._psu_force_off()
        self._exp_mark_off_cmd(exp)
        self._exp_set_result(exp, code, detail)
        label = EXP_RESULTS[code]
        self._exp_update(exp, "psu_fault", f"{label}：{detail}。感測器繼續錄製，請按「結束這場」",
                         fault={"code": code, "message": detail})
        self.notify("error", "實驗中止", f"{label}：{detail}\n感測器繼續錄製，請按「結束這場」存檔", src=PSU_NAME)

    def exp_estop(self):
        """緊急停止：電源立即 OFF（不論實驗狀態）；正式實驗中則感測器繼續錄，由使用者按「停止錄製」結束。
        設旗標與送出緊急關閉在 _estop_lock 內完成，和流程 thread 的「檢查旗標＋開啟輸出」互斥（見 _exp_run）。"""
        with self._estop_lock:
            with self.lock:
                exp = self.exp if self.exp is not None and self.exp["stage"] != "done" else None
                if exp is not None:
                    exp["estop"] = True  # 先設旗標再關輸出，流程 thread 才不會把它誤判成電源異常或再開輸出
                    stopping = exp["stopping"]
            if self.psu.connected:
                self.psu.emergency_stop()
        if exp is None:
            return False
        self._exp_mark_off_cmd(exp)
        if stopping:  # 結束流程已在停止錄製：只關電源，錄製照原本流程存檔
            slog(WARN, SRC_SYSTEM, "緊急停止：電源已關閉（錄製已在停止中）")
            return True
        with self.lock:
            if exp["result"] and "緊急停止" not in exp["result_detail"]:
                exp["result_detail"] += "；結束中按下緊急停止"
        self._exp_set_result(exp, "aborted_user", "緊急停止")
        self._exp_update(exp, "estop", "已緊急停止：電源輸出已關閉，感測器繼續錄製；按「停止錄製」結束本場")
        self.notify("warning", "緊急停止", "電源輸出已關閉；感測器繼續錄製，按「停止錄製」結束本場", src=PSU_NAME)
        return True

    def exp_end(self):
        """「結束實驗」／「結束這場」／「停止錄製」。輸出中（或基線、啟動中）→ 交給流程 thread 依序關閉後停止錄製；
        已中止（電源異常、緊急停止）→ 直接停止錄製。準備中（還沒開始錄製）不接受。回傳是否接受。"""
        with self.lock:
            exp = self.exp if self.exp is not None and self.exp["stage"] != "done" else None
            if exp is None or exp["stage"] == "preparing":
                return False
            stage = exp["stage"]
            if stage in ("baseline", "starting", "on"):
                exp["end_req"] = True
                return True
        if stage == "ending":
            return True
        self._psu_force_off()  # 保險：中止後輸出應已關閉
        return self.request_stop("manual")

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
            if recorder:
                self._psu_close_csv(recorder)
            self._save_all(recorder, reason)
        except Exception as e:  # noqa: BLE001
            slog(ERROR, SRC_SYSTEM, f"存檔流程錯誤: {e}", e)
            self.notify("warning", "警告", "數據儲存失敗，詳細內容見實驗 log")
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
            if self.exp is not None and self.exp["recorder"] is recorder:
                self.exp["stage"] = "done"  # 正式實驗結束：解除電源鎖定
                if self.exp["result"] == "completed":
                    self.exp["message"] = "實驗完成，資料已存檔"
                else:
                    self.exp["message"] = f"{EXP_RESULTS.get(self.exp['result'], '已結束')}，資料已存檔"
            # 溫度/距離保留最後讀值（同 main_csv）；音訊「錄音中」在停止後會誤導，改為已停止
            if self.enabled["audio"]:
                self.sensors["audio"]["text"] = "已停止"
                self.sensors["audio"]["level"] = "off"
            for key, s in self.sensors.items():
                # 正常/待機/初始化中的感測器標為已停止；失敗中的保留最後的具體原因供事後查看
                if self.enabled.get(key) and s["reason"] in (R_OK, R_IDLE, R_INIT):
                    s["reason"], s["detail"] = R_STOPPED, "監測已停止"
        if self.psu.phase in ("relays", "powering", "on"):  # 手動或自動停止都提醒；不替使用者關高壓輸出
            self.notify("warning", "警告", "監測已停止，但電源仍在輸出中：請確認是否要關閉輸出", src=PSU_NAME)
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
        failures = self._failure_summaries(end_ts)  # 停止時仍在失敗的留下最後原因；短暫失誤彙總成一行
        if failures:
            recorder.update(failure_summary=failures)
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
                                f"原始音訊已保存於 {audio['name']}，可稍後執行：\n"
                                f"uv run python app/chunked_spectrogram.py {recorder.rel_dir()}")
            else:
                recorder.update(spectrogram={"status": "skipped", "reason": "音訊太短"})
                slog(WARN, "音訊", "音訊太短，略過頻譜 CSV")
        elif self.enabled.get("audio"):
            self.notify("warning", "警告", "沒有錄製到音訊數據", src="音訊")

        with self.lock:
            exp = self.exp if self.exp is not None and self.exp["recorder"] is recorder else None
            mode = self.run_mode
        if exp is not None:
            # 正式實驗：結果標記還沒定（伺服器關閉等外部停止）時標為中止-系統；摘要列出結果與缺少的感測器
            self._exp_set_result(exp, "aborted_system", f"錄製被外部停止（{STOP_REASONS.get(reason, reason)}）")
            self._exp_update(exp)
            messages.insert(0, f"結果：{EXP_RESULTS[exp['result']]}（{exp['result_detail']}）")
            if exp["missing"]:
                messages.insert(1, "缺少感測器：" + "、".join(f"{m['name']}（{m['detail']}）" for m in exp["missing"]))
        elif mode == "sensor_test":
            messages.insert(0, "Sensor 測試模式（非正式實驗）")
        # 頂層 status 與結果標記一致：正式實驗中止的場次記 aborted（檔案仍已完整存檔，原因見 result）
        recorder.update(status="aborted" if exp is not None and exp["result"] != "completed" else "completed")
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
        read_tx = self.sim_temp.read_tx600n if "temp" in self.simulated else read_tx600n_diag

        def read_temp(port):
            """讀 T1；溫度計回報 Err（探棒未接、斷線）或單位／類型設錯時視為讀取失敗，
            不把 Err 時暫存器裡的假值（例如 1380.0）當成溫度。"""
            data, reason, detail = read_tx(port)
            if data is None:
                return None, reason, detail
            t1, _, err, _ = tx600n_reading(data)
            if t1 is None:
                return None, (R_WIRING if data["t1_err"] else R_ERROR), err
            return t1, R_OK, ""

        calibrate = "temp" not in self.simulated

        def log(value, status, raw=None):
            now = time.time()
            recorder.write_row("temperature", [iso_now(now), f"{now - self.start_time:.3f}",
                                               "" if value is None else f"{float(value):.3f}", status,
                                               "" if raw is None else f"{float(raw):.3f}"],
                               ok=status == "ok")

        try:
            while not stop_event.is_set():
                try:
                    temp, reason, detail = read_temp(com_port)
                    if temp is not None:
                        raw = temp
                        if calibrate:
                            temp = TEMP_CAL_GAIN * float(raw) + TEMP_CAL_OFFSET
                        log(temp, "ok", raw)
                        self._mark_valid("temp")
                        self.add_series_point("temp", round(float(temp), 2))
                        self.set_sensor("temp", text=f"{temp:.1f} °C", value=round(float(temp), 1), level="ok")
                    else:
                        escalated = self.sensor_failed("temp", reason, detail, code=reason,
                                                       comm=reason in (R_BUSY, R_ERROR, R_NOT_FOUND, R_DRIVER))
                        log(None, "感測器斷線" if escalated else "讀取失敗")
                        self.add_series_point("temp", None)
                except Exception as e:  # noqa: BLE001
                    self.sensor_failed("temp", R_ERROR, f"讀取溫度時發生程式例外（{type(e).__name__}: {e}）："
                                       "不是溫度計回報的錯誤；持續發生時 traceback 會寫進實驗 log",
                                       code=type(e).__name__, comm=True, exc=e)
                    log(None, "連接錯誤")
                    self.add_series_point("temp", None)
                stop_event.wait(1)
        except BaseException as e:  # noqa: BLE001  worker 意外死掉也要留下紀錄並關檔
            slog(ERROR, "溫度", f"溫度 worker 異常結束: {e}", e)
            recorder.add_error(f"溫度 worker 異常結束: {e}")
            self.notify("error", "溫度監測中斷",
                        "溫度：監測執行緒意外中止，之後不會再有溫度數據（例外與 traceback 見實驗 log）", src="溫度")
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
            failing = False  # 錄音例外中（成功一次就回報恢復）
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
                            self._mark_valid("audio")
                        if zero_seconds >= 3:
                            # 呼叫端已確認持續 3 秒，直接升級；之後每段都記一次失敗，讓「仍在失敗」摘要能計數
                            self.zero_warned = True
                            self.sensor_failed("audio", R_NO_DATA, "連續 3 秒錄到的訊號全為 0："
                                               "麥克風權限未開、設備靜音或未接；檢查 Windows 隱私權設定與麥克風開關",
                                               code="全為 0", confirmed=True, text="訊號全為 0",
                                               since=time.time() - zero_seconds)
                        elif zero_seconds == 0 and (self.zero_warned or failing):
                            self.zero_warned = failing = False
                            self.set_sensor("audio", text="錄音中", level="ok")
                        elif zero_seconds == 0 and time.time() - last_ok_push >= 1:
                            # 正常錄音時每秒刷新一次 last_ok，狀態面板的「最後正常」才不會一直累加
                            last_ok_push = time.time()
                            self.set_sensor("audio", level="ok")
                except Exception as audio_error:  # noqa: BLE001
                    failing = True
                    self.sensor_failed("audio", R_ERROR,
                                       f"錄音時發生錯誤（{type(audio_error).__name__}: {audio_error}）："
                                       "檢查麥克風是否被拔除或被其他程式佔用；持續發生時 traceback 會寫進實驗 log",
                                       code=type(audio_error).__name__, comm=True, exc=audio_error, text="錄製錯誤")
                    stop_event.wait(update_interval)
        except BaseException as e:  # noqa: BLE001
            slog(ERROR, "音訊", f"音訊監測錯誤: {e}", e)
            recorder.add_error(f"音訊監測錯誤: {e}")
            self.notify("warning", "音訊初始化失敗",
                        "音訊：初始化失敗，本次實驗沒有音訊數據；其他感測器照常（原因與建議見狀態卡）", src="音訊")
            reason, detail = init_diag or (R_ERROR, "音訊初始化失敗：詳細內容見實驗 log")
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

        def log(status, rel=None):
            # Timestamp 與 Elapsed(s) 和其他 CSV 一致：ISO 時間、以實驗開始為 0（測距儀初始化約 2.7 秒，這段沒有列）
            now = time.time()
            head = [iso_now(now), f"{now - self.start_time:.3f}"]
            if rel is None:
                recorder.write_row("distance", head + ["", "", status], ok=False)
            else:
                recorder.write_row("distance", head + [f"{(BASIC_REF + rel) * 1000:.3f}", f"{rel * 1000:.3f}", status])

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

            while not stop_event.is_set():
                try:
                    d = device.read_single(OUT_NO)
                    if d["FloatResult"] == "VALID":
                        rel = d["Value"]
                        absolute = (BASIC_REF + rel) * 1000  # µm
                        log("VALID", rel)
                        self._mark_valid("distance")
                        self.add_series_point("distance", round(float(absolute), 3))
                        self.set_sensor("distance", text=f"{absolute:.1f} µm",
                                        value=round(float(absolute), 1), level="ok")
                    else:
                        status = d["FloatResult"]
                        code = f"Unknown({d['RawStatus']})" if status == "Unknown" else status
                        log(code)
                        recorder.note_code("距離", code, value=d["RawStatus"], level="warning",
                                           text=f"FloatResult {d['RawStatus']}（{FLOAT_RESULT.get(d['RawStatus'], 'Unknown')}）："
                                                + split_detail(RANGEFINDER_STATUS_DIAG.get(status, (None, "量測值無效"))[1])[0])
                        self.add_series_point("distance", None)
                        reason, detail = RANGEFINDER_STATUS_DIAG.get(
                            status, (R_ERROR, f"量測值無效（{status}）：檢查目標表面與感測頭設定"))
                        self.sensor_failed("distance", reason, detail, code=code)
                except Exception as e:  # noqa: BLE001
                    log(f"ERROR: {e}")
                    code, detail, usb = rangefinder_read_error(e)
                    if isinstance(e, LKIFError):
                        recorder.note_code("距離", code, value=e.rc, text=rc_text(e.rc), raw=str(e), level="error")
                    self.add_series_point("distance", None)
                    # 讀值函式失敗一律算通訊類（升級時記 ERROR）；狀態卡只有 RC 0x2xxx 才寫「通訊中斷」
                    self.sensor_failed("distance", R_ERROR, detail, code=code, comm=True, exc=e,
                                       text="通訊中斷" if usb else "讀值錯誤")
                stop_event.wait(interval)
        except BaseException as e:  # noqa: BLE001
            slog(ERROR, "距離", f"測距儀監測錯誤: {e}", e)
            recorder.add_error(f"測距儀監測錯誤: {e}")
            self.notify("warning", "距離初始化失敗",
                        "距離：測距儀初始化失敗，本次實驗沒有距離數據；其他感測器照常（原因與建議見狀態卡）", src="距離")
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
        （未扣暗光）。失敗列強度欄留空、Status 寫「原因代碼: 訊息」。失敗的升級與恢復通知由 sensor_failed 處理。
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

            next_run = time.time()
            while not stop_event.is_set():
                try:
                    counts = device.measure(integration_ms)
                    log(counts, "ok")
                    self._mark_valid("spectrometer")
                    self._ingest_spectrum(time.time() - self.start_time, counts)
                    peak = int(np.argmax(counts))
                    self.set_sensor("spectrometer", text=f"最強峰 {wl[peak]:.1f} nm", value=round(float(counts[peak]), 1),
                                    level="ok")
                except Exception as e:  # noqa: BLE001
                    reason, detail = classify_spectrometer_error(e)
                    log(None, f"{reason}: {e}")
                    self._ingest_spectrum(time.time() - self.start_time, None)
                    self.sensor_failed("spectrometer", reason, f"{detail}（{type(e).__name__}: {e}）",
                                       code=reason, comm=reason != R_NO_DATA, exc=e)
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
            self.notify("warning", "光譜儀初始化失敗",
                        "光譜儀：初始化失敗，本次實驗沒有光譜數據；其他感測器照常（原因與建議見狀態卡）", src="光譜儀")
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
        # 電源第一個處理：disconnect 會先把輸出關掉再放掉 COM 埠（不必等實驗存檔）
        try:
            if self.psu.connected:
                try:
                    self.psu.disconnect()
                except KeyboardInterrupt:  # 第二次 Ctrl+C／SIGTERM：仍等它送完關閉輸出的命令（最多 3 秒）
                    self.psu.disconnect()
                slog(INFO, PSU_NAME, "伺服器關閉：已關閉電源輸出並斷線")
        except Exception as e:  # noqa: BLE001
            slog(ERROR, PSU_NAME, f"關閉電源連線失敗: {e}", e)
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


# ---------------------------------------------------------------------------
# TX-600N 讀值解析
# ---------------------------------------------------------------------------
TX600N_VALID_RANGE = (-200.0, 1372.0)  # K 型熱電偶量程；超出（例如 Err 時的 1380.0）視為無效


def tx600n_reading(data):
    """read_tx600n_diag 的 data → (t1 或 None, t2 或 None, T1 錯誤說明, T2 錯誤說明)；說明為空字串表示正常。"""
    common = []
    if data["unit_f"]:
        common.append("溫度計單位是 °F：請在 TX-600N 切回 °C")
    if data["tc_type"] != "K":
        common.append(f"熱電偶類型設成 {data['tc_type']} 型：LP 測棒是 K 型，請在 TX-600N 切回 K")
    out = []
    for ch in ("t1", "t2"):
        errs = list(common)
        if data[f"{ch}_err"]:
            errs.insert(0, f"{ch.upper()} Err（探棒未接、斷線或超出量程）")
        elif not TX600N_VALID_RANGE[0] <= data[ch] <= TX600N_VALID_RANGE[1]:
            errs.insert(0, f"{ch.upper()} 讀值 {data[ch]:.1f} 超出 K 型量程")
        out.append((None if errs else round(float(data[ch]), 2), "；".join(errs)))
    return out[0][0], out[1][0], out[0][1], out[1][1]


app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"),
            static_folder=os.path.join(APP_DIR, "static"))


def _live_experiment_id():
    """目前錄製中的實驗 ID（沒有則 None），歷史頁用來分辨「錄製中」與「中斷」。"""
    with service.lock:
        rec = service.recorder
        return rec.experiment_id if rec is not None and service.phase in ("running", "stopping") else None


history.init(app, DATA_DIR, _live_experiment_id)  # 實驗紀錄瀏覽（唯讀）：/history 與 /api/history/*


@app.before_request
def restrict_remote_clients():
    """用 --host 0.0.0.0 開放給其他機器（例如 Tailscale 上的手機）時，遠端只能「瀏覽實驗紀錄」。
    開始／停止監測、電源、校正、即時串流等一律只准本機，避免沒有驗證的控制端點暴露出去。"""
    if request.remote_addr in PSU_LOCAL_ADDRS:
        return None
    if request.path == "/":
        return redirect("/history")
    if request.method in ("GET", "HEAD") and (request.path == "/history" or request.path.startswith(("/api/history/", "/static/"))):
        return None
    return jsonify({"ok": False, "errors": ["遠端只能瀏覽實驗紀錄；其他功能請在接儀器的電腦上操作"]}), 403


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
    if service.phase in ("running", "stopping", "checking"):
        return jsonify({"ok": False, "errors": ["監測已在進行中"], "warnings": []}), 409
    # Sensor 測試模式（mode=sensor_test）標籤可不填
    result = service.preflight(raw, require_labels=raw.get("mode") != "sensor_test")
    result.pop("parsed", None)
    return jsonify(result)


@app.post("/api/start")
def api_start():
    """Sensor 測試模式：只錄感測器，完全不碰電源（不連線、不送命令）；manifest 標 mode = sensor_test。"""
    raw = request.get_json(silent=True) or {}
    body, code = service.start(raw, bool(raw.get("confirm")), mode="sensor_test")
    return jsonify(body), code


@app.post("/api/stop")
def api_stop():
    # 正式實驗中改走實驗流程（先關輸出與主繼電器再停止錄製）
    accepted = service.exp_end() if service.exp_active() else service.request_stop("manual")
    return jsonify({"ok": accepted, "state": service.snapshot()}), (202 if accepted else 409)


# ---------------------------------------------------------------------------
# 正式實驗流程：只准本機（restrict_remote_clients 已擋遠端）
# ---------------------------------------------------------------------------
@app.post("/api/exp/check")
def api_exp_check():
    body, code = service.exp_check(request.get_json(silent=True) or {})
    return jsonify(body), code


@app.post("/api/exp/start")
def api_exp_start():
    body, code = service.exp_start(request.get_json(silent=True) or {})
    return jsonify(body), code


@app.post("/api/exp/end")
def api_exp_end():
    accepted = service.exp_end()
    return jsonify({"ok": accepted, "state": service.snapshot()}), (202 if accepted else 409)


@app.post("/api/exp/estop")
def api_exp_estop():
    service.exp_estop()
    return jsonify({"ok": True, "state": service.snapshot(), "snapshot": service.psu.snapshot()})


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


# ---------------------------------------------------------------------------
# 電源供應器（TruPlasma）：控制只准本機（接電源的電腦）；緊急關閉任何裝置都可以（它只會關閉輸出）
# ---------------------------------------------------------------------------
def _psu_can_control():
    return request.remote_addr in PSU_LOCAL_ADDRS


def _psu_call(fn, local_only=True):
    """跑一個電源操作：先檢查本機限制，再把驗證／狀態／通訊錯誤（訊息已是中文）轉成 400。"""
    if local_only and not _psu_can_control():
        return jsonify({"ok": False, "errors": ["只有在接電源的電腦本機上才能控制電源"]}), 403
    try:
        body = fn() or {}
    except (ValueError, RuntimeError, OSError) as e:  # OSError 含 ProtocolError 與逾時
        return jsonify({"ok": False, "errors": [str(e) or "電源沒有回應（逾時）"]}), 400
    return jsonify({"ok": True, **body})


def _psu_connected():
    if not service.psu.connected:
        raise RuntimeError("電源未連線")
    return service.psu


def _psu_check_unlocked():
    """正式實驗從「接受開始」到存檔完成，以及「檢驗Sensor與電供」寫入讀回參數期間，電源由流程獨佔。"""
    if service.exp_active():
        raise RuntimeError(EXP_PSU_LOCKED)
    if service.phase == "checking":
        raise RuntimeError("正在「檢驗Sensor與電供」（寫入並讀回電源參數中），請稍候再操作電源")


def _psu_unlocked():
    """正式實驗進行中不能改 U/I/P、寫參數、開輸出、重設警報或斷線（只能關閉／緊急停止）。"""
    _psu_check_unlocked()
    return _psu_connected()


@app.get("/api/psu/state")
def api_psu_state():
    return jsonify({"ok": True, "snapshot": service.psu.snapshot(), "params": dict(service.psu.params),
                    "param_table": [p.to_dict() for p in PARAMS], "pulse_modes": PULSE_MODES,
                    "recipe_limits": RECIPE_LIMITS, "can_control": _psu_can_control(), "simulated": "psu" in service.simulated})


@app.post("/api/psu/connect")
def api_psu_connect():
    raw = request.get_json(silent=True) or {}

    def run():
        service.psu_connect(str(raw.get("port") or ""))
        return {"snapshot": service.psu.snapshot()}
    return _psu_call(run)


@app.post("/api/psu/disconnect")
def api_psu_disconnect():
    def run():
        _psu_check_unlocked()
        service.psu.disconnect()
        return {"snapshot": service.psu.snapshot()}
    return _psu_call(run)


@app.post("/api/psu/setpoints")
def api_psu_setpoints():
    raw = request.get_json(silent=True) or {}
    return _psu_call(lambda: {"setpoints": dict(zip("uip", _psu_unlocked().set_setpoints(
        raw.get("u"), raw.get("i"), raw.get("p"))))})


@app.post("/api/psu/recipe")
def api_psu_recipe():
    raw = request.get_json(silent=True)

    def run():
        plan_recipe({}, raw)  # 先驗證所有欄位，不寫入任何通道
        psu = _psu_unlocked()
        if psu.snapshot()["phase"] != "idle":
            raise RuntimeError("參數會寫入電源的 EEPROM，請先關閉輸出再修改")
        service.psu_recipe_errors = [RECIPE_INCOMPLETE]  # 中途失敗（部分通道可能已寫入）也要擋住開啟輸出
        current = psu.read_params()
        plan = plan_recipe(current, raw)
        written = [psu.write_param(ch, value) for ch, value in plan]
        all_channels = [33, 140, 1100 if raw["mode"] != "bipulse" and float(raw["freq_hz"]) <= 1000 else 51130, 51131]
        if raw["mode"] == "bipulse":
            all_channels.append(650)
        skipped = [{"channel": ch, "value": current[ch]} for ch in all_channels if ch not in {item[0] for item in plan}]
        # 設定上限前再次檢查，避免輸出已開啟時套用。
        with psu.lock:
            if psu.phase != "idle":
                raise RuntimeError("參數會寫入電源的 EEPROM，請先關閉輸出再修改")
            setpoints = dict(zip("uip", psu.set_setpoints(raw["u"], raw["i"], raw.get("p", RECIPE_LIMITS["p_default"]))))
        readback = psu.read_params()
        service.psu_recipe_errors = recipe_readback_errors(readback, raw)
        if service.psu_recipe_errors:
            raise RuntimeError("電源參數寫入後讀回不一致，不能開啟輸出：" + "；".join(service.psu_recipe_errors))
        meas = psu.snapshot()["measurements"]
        return {"written": written, "skipped": skipped, "setpoints": setpoints, "params": dict(psu.params),
                "readback": {str(ch): readback.get(ch) for ch in RECIPE_READBACK_CHANNELS},
                "actual": {"freq_hz": meas.get("Freq_Hz"), "duty_pct": meas.get("Duty_pct")}}
    return _psu_call(run)


@app.post("/api/psu/output")
def api_psu_output():
    raw = request.get_json(silent=True) or {}

    def run():
        if not isinstance(raw.get("on"), bool):
            raise ValueError("on 必須是 true 或 false")
        psu = _psu_unlocked() if raw["on"] else _psu_connected()  # 實驗中只能關，不能開
        if raw["on"]:
            if service.psu_recipe_errors:
                raise RuntimeError("上次套用的電源參數讀回不一致，請重新套用並確認讀回一致：" + "；".join(service.psu_recipe_errors))
            psu.power_on()
        else:
            psu.power_off()
        return {"snapshot": psu.snapshot()}
    return _psu_call(run)


@app.post("/api/psu/estop")
def api_psu_estop():
    def run():
        service.exp_estop()  # 電源已連線就立即關閉；正式實驗中同時標記中止（使用者），錄製繼續
        return {"snapshot": service.psu.snapshot()}
    return _psu_call(run, local_only=False)


@app.post("/api/psu/reset-alarms")
def api_psu_reset_alarms():
    return _psu_call(lambda: _psu_unlocked().reset_alarms())  # 實驗中不提供「重設警報後續跑」


@app.post("/api/psu/reset-counters")
def api_psu_reset_counters():
    return _psu_call(lambda: _psu_connected().reset_counters())


@app.post("/api/psu/params/read")
def api_psu_params_read():
    return _psu_call(lambda: {"params": _psu_connected().read_params()})


@app.post("/api/psu/param")
def api_psu_param():
    raw = request.get_json(silent=True) or {}

    def run():
        try:
            channel = int(raw.get("channel"))
        except (TypeError, ValueError):
            raise ValueError("channel 必須是整數") from None
        return {"result": _psu_unlocked().write_param(channel, raw.get("value")),
                "params": dict(service.psu.params)}
    return _psu_call(run)


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
                        help="以模擬資料取代指定感測器：temp,distance,audio,spectrometer（別名 spectrum/spec/avaspec）、"
                             "psu（TruPlasma 電源模擬器）或 all（預設不模擬；音訊需明確列出）")
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
