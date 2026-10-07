"""感測器整合系統：網頁版（取代 main_csv.py 的 Tkinter 介面）。

啟動（在專案根目錄）：uv run python main.py  →  http://127.0.0.1:5002
（也可直接 uv run python app/web_monitor.py，兩者參數相同）

架構
- MonitorService（單例）持有所有監測狀態，以 threading.Lock 保護。
- 溫度／音訊／測距儀各一條 worker thread，另有一條計時 thread 負責執行時間與自動停止；
  行為對齊 main_csv.py（輪詢間隔、連續 10 次失敗才報斷線、恢復通知、停止時存檔）。
- 前端透過 REST 下指令，透過 SSE (/api/stream) 接收狀態、圖表資料與通知。
- 監測在伺服器端持續進行，頁面重整後由 /api/state 與 /api/spectrogram 復原畫面。
- 存檔：開始時就建立 Sensor_Data/EXP_<id>/，所有感測器資料邊錄邊寫（WAV / CSV 每筆 flush），
  停止時再由完整 WAV 分塊產生 spectrogram_<id>.csv（chunked_spectrogram.py，格式與原版相同）。
"""

import argparse
import atexit
import base64
import collections
import csv
import datetime
import json
import math
import os
import queue
import platform
import shutil
import signal
import sys
import threading
import time
import traceback
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
from signal_package import AudioRecorder  # noqa: E402
from temp_py_package import continuous_read, list_candidate_ports  # noqa: E402

DATA_DIR = os.path.join(BASE_DIR, "Sensor_Data")
LKIF_DLL_PATH = os.path.join(DRIVERS_DIR, "LKIF2.dll")

HOST = "127.0.0.1"
PORT = 5002

# 與 main_csv.py / signal_package.plot_spectrogram 相同
NFFT = 256
NOVERLAP = 128
HOP = NFFT - NOVERLAP
MAX_FAILURES = 10
SERIES_MAX_POINTS = 2000       # 溫度／距離歷史送給前端時降採樣的點數上限
SSE_QUEUE_MAX = 300
MAX_DISPLAY_SECONDS = 600       # 頻譜圖顯示長度上限（只影響記憶體中的顯示緩衝）
MIN_FREE_BYTES = 2 * 1024 ** 3  # 剩餘空間低於 2 GB 時預檢警告
# 每小時檔案大小估計（22050 Hz、單聲道、預設間隔；1 小時實測）
EST_BYTES_PER_HOUR = {"wav": 158_760_044, "spectrogram": 297_639_967, "temperature": 200_000,
                      "distance": 2_000_000}

# 測距儀固定參數（main_csv.py L67-70）
BASIC_REF = 50.0
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
}

REFL_MODE_LABELS = {0: "0-漫反射", 1: "1-鏡面反射"}
STOP_REASONS = {"manual": "手動停止", "sigint": "Ctrl+C", "sigterm": "SIGTERM", "sigbreak": "Ctrl+Break",
                "sighup": "關閉終端機視窗", "console_close": "關閉主控台視窗",
                "error": "錯誤", "timeout": "計時結束"}


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
        print(f"刷新COM端口錯誤: {e}")
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
        print(f"刷新音訊設備錯誤: {e}")
        return {"devices": [], "status": "error", "placeholder": AUDIO_FAILED, "default": AUDIO_FAILED}


def parse_audio_device_index(selected):
    """對應 main_csv.get_selected_audio_device_index。"""
    if not selected or selected in (AUDIO_NONE, AUDIO_FAILED):
        return None
    try:
        return int(str(selected).split(":")[0])
    except (TypeError, ValueError):
        return None


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
    """

    def __init__(self, experiment_id, info):
        self.experiment_id = experiment_id
        self.dir = os.path.join(DATA_DIR, experiment_id)
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.info = info
        self.files = {}     # key -> 統計（name、rows/frames、bytes…）
        self._handles = {}  # key -> (file, writer, kind)
        self.errors = []
        self.on_write_error = None  # callback(key, exc)：寫檔失敗（磁碟滿等）時通知，不讓 worker 靜默死掉
        self.write_json()

    def _write_failed(self, key, exc):
        with self.lock:
            st = self.files.get(key)
            first = st is not None and not st.get("write_error")
        if first:
            print(f"寫入 {key} 失敗: {exc}（之後同一檔案的失敗不再逐筆列印）")
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
            self.files[key] = {"name": filename, "kind": "csv", "rows": 0, "bytes": f.tell(), "closed": False}

    def write_row(self, key, row):
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
                print(f"關閉 {key} 檔案錯誤: {e}")

    def add_file(self, key, name, **stats):
        """登記非串流檔案（例如停止後產生的 spectrogram CSV）。"""
        with self.lock:
            p = self.path(name)
            self.files[key] = {"name": name, "kind": "file", "closed": True,
                               "bytes": os.path.getsize(p) if os.path.exists(p) else 0, **stats}

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
            return {"experiment_id": self.experiment_id, "dir": self.rel_dir(),
                    "files": [dict(v, key=k) for k, v in self.files.items()]}

    def write_json(self):
        with self.lock:
            doc = dict(self.info)
            doc["files"] = {k: {kk: vv for kk, vv in v.items()} for k, v in self.files.items()}
            doc["errors"] = list(self.errors)
        target = self.path(f"experiment_{self.experiment_id}.json")
        tmp = target + ".tmp"
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
        self.enabled = {"temp": False, "audio": False, "distance": False}
        self.params = dict(DEFAULT_PARAMS)
        self.duration = -1
        self.start_time = None
        self.elapsed = 0.0
        self.last_result = None
        self.run_id = 0

        self.sensors = {}
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

    def notify(self, level, title, message):
        """取代 messagebox：level = info | warning | error | success。"""
        with self.lock:
            self.notice_seq += 1
            notice = {"id": self.notice_seq, "level": level, "title": title,
                      "message": message, "time": time.time()}
            self.notices.append(notice)
        print(f"[{level}] {title}: {message}")
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
        self.sensors = {
            "temp": {"text": "-- °C", "value": None, "level": "idle"},
            "distance": {"text": "-- mm", "value": None, "level": "idle"},
            "audio": {"text": "--", "level": "idle"},
        }

    def set_sensor(self, key, text=None, value=None, level=None, push=True):
        with self.lock:
            s = self.sensors[key]
            if text is not None:
                s["text"] = text
            s["value"] = value if key != "audio" else s.get("value")
            if level is not None:
                s["level"] = level
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
        return snap

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
        """對應 main_csv.start_monitoring 的測距儀檢查。"""
        try:
            dev = LKIF2Device(LKIF_DLL_PATH)
            rc = dev.open()
            if rc != RC_OK:
                try:
                    dev.close()
                except Exception:  # noqa: BLE001
                    pass
                return False, "• 測距儀連接失敗，距離監測將被停用"
            dev.close()
            return True, None
        except BaseException:  # noqa: BLE001  macOS 上 ctypes.WinDLL 不存在 → AttributeError
            return False, "• 測距儀初始化失敗，距離監測將被停用"

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
        enabled = {"temp": True, "audio": True, "distance": True}
        sim = self.simulated
        com = str(raw.get("com_port", "") or "").strip()
        if not com and "temp" not in sim:
            warnings.append("• 未選擇溫度感測器COM端口，溫度監測將被停用")
            enabled["temp"] = False
        audio_sel = str(raw.get("audio_device", "") or "")
        if "audio" not in sim and (not audio_sel or audio_sel in (AUDIO_NONE, AUDIO_FAILED)):
            warnings.append("• 未選擇有效的音訊設備，音訊監測將被停用")
            enabled["audio"] = False
        if "distance" not in sim:
            ok, msg = self.check_rangefinder()
            if not ok:
                warnings.append(msg)
                enabled["distance"] = False

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

        return {"ok": not errors, "errors": errors, "warnings": warnings,
                "enabled": enabled, "parsed": parsed, "disk": disk}

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
            },
            "platform": {"os": platform.platform(), "python": platform.python_version()},
            "spectrogram": None,
        }
        try:
            recorder = ExperimentRecorder(experiment_id, info)
            recorder.on_write_error = self._on_write_error
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
                                  ["Timestamp", "Elapsed(s)", "Absolute(mm)", "Relative(mm)", "Status"], encoding="utf-8-sig")
        except OSError as e:
            return {"ok": False, "errors": [f"無法建立實驗資料夾或檔案: {e}"]}, 500

        with self.lock:
            if self.phase in ("running", "stopping"):
                recorder.close_all()
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
            self.zero_warned = False
            self.last_result = None
            self.elapsed = 0.0
            self.start_time = start_ts
            self.stop_event = threading.Event()
            self.finalizing = False
            self._reset_sensor_display()
            parts = [name for key, name in (("temp", "溫度"), ("audio", "音訊"), ("distance", "距離"))
                     if enabled[key]]
            self.status_text = f"監測中 ({'+'.join(parts)})"
            self.phase = "running"
            if not enabled["temp"]:
                self.sensors["temp"].update(text="未啟用", level="off")
            if not enabled["audio"]:
                self.sensors["audio"].update(text="未啟用", level="off")
            else:
                self.sensors["audio"].update(text="初始化中", level="idle")
            if not enabled["distance"]:
                self.sensors["distance"].update(text="未啟用", level="off")
            stop_event = self.stop_event
            run_id = self.run_id
        if self.simulated:
            self.sim_temp = sim_devices.SimTemperature(faults=self.sim_faults)

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
        with self.lock:
            self.threads = threads
        for t in threads.values():
            t.start()
        return {"ok": True, "state": self.snapshot()}, 200

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
            t.join(timeout=5 if name in ("audio", "distance") else 2)
            if t.is_alive():
                print(f"警告：{name} 執行緒未在時限內結束")
                if recorder:
                    recorder.add_error(f"{name} 執行緒未在時限內結束")
        try:
            self._save_all(recorder, reason)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.notify("warning", "警告", f"數據儲存失敗: {e}")
            if recorder:
                recorder.add_error(f"存檔流程錯誤: {e}")
        with self.lock:
            self.phase = "stopped"
            self.status_text = "已停止"
            self.finalizing = False
            self.threads = {}
            # 溫度/距離保留最後讀值（同 main_csv）；音訊「錄音中」在停止後會誤導，改為已停止
            if self.enabled["audio"]:
                self.sensors["audio"]["text"] = "已停止"
                self.sensors["audio"]["level"] = "off"
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
        if "temperature" in summary:
            messages.append(f"溫度 {summary['temperature']['rows']} 筆")
        if "distance" in summary:
            messages.append(f"距離 {summary['distance']['rows']} 筆")

        # 2) 由完整 WAV 分塊產生頻譜 CSV（格式與 save_spectrogram_to_csv 相同）
        audio = summary.get("audio")
        if audio and audio.get("frames", 0) > 0:
            messages.append(f"音訊 {audio['seconds']:.1f} 秒")
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
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()
                    recorder.update(spectrogram={"status": "error", "error": str(e)})
                    self._set_spec_job("error", None, str(e))
                    self.notify("warning", "頻譜 CSV 產生失敗",
                                f"{e}\n原始音訊已保存於 {audio['name']}，可稍後執行：\n"
                                f"uv run python app/chunked_spectrogram.py {recorder.rel_dir()}")
            else:
                recorder.update(spectrogram={"status": "skipped", "reason": "音訊太短"})
        elif self.enabled.get("audio"):
            self.notify("warning", "警告", "沒有錄製到音訊數據")

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
        read_temp = self.sim_temp.read if "temp" in self.simulated else continuous_read

        def log(value, status):
            now = time.time()
            recorder.write_row("temperature", [iso_now(now), f"{now - self.start_time:.3f}",
                                               "" if value is None else f"{float(value):.3f}", status])

        try:
            while not stop_event.is_set():
                try:
                    temp = read_temp(com_port)
                    if temp is not None:
                        consecutive_failures = 0
                        if disconnected:
                            disconnected = False
                            self.notify("info", "通知", "溫度感測器已重新連接")
                        log(temp, "ok")
                        self.add_series_point("temp", round(float(temp), 2))
                        self.set_sensor("temp", text=f"{temp:.1f} °C", value=round(float(temp), 1), level="ok")
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_FAILURES and not disconnected:
                            disconnected = True
                            self.notify("warning", "警告", "溫度感測器可能已斷線，監測將繼續但不會讀取溫度數據")
                        if disconnected:
                            log(None, "感測器斷線")
                            self.add_series_point("temp", None)
                            self.set_sensor("temp", text="感測器斷線", value=None, level="error")
                        else:
                            log(None, "讀取失敗")
                            self.add_series_point("temp", None)
                            self.set_sensor("temp", text="讀取失敗", value=None, level="warn")
                except Exception as e:  # noqa: BLE001
                    print(f"溫度讀取錯誤: {e}")
                    consecutive_failures += 1
                    if consecutive_failures == MAX_FAILURES and not disconnected:
                        disconnected = True
                        self.notify("warning", "警告", f"溫度感測器錯誤: {e}\n監測將繼續但不會讀取溫度數據")
                    log(None, "連接錯誤")
                    self.add_series_point("temp", None)
                    self.set_sensor("temp", text="連接錯誤", value=None,
                                    level="error" if disconnected else "warn")
                stop_event.wait(1)
        except BaseException as e:  # noqa: BLE001  worker 意外死掉也要留下紀錄並關檔
            recorder.add_error(f"溫度 worker 異常結束: {e}")
            self.notify("error", "溫度監測中斷", f"溫度 worker 異常結束: {e}")
        finally:
            recorder.close("temperature")

    def _audio_worker(self, stop_event, device_index, sample_rate, update_interval, recorder):
        """對應 main_csv.start_audio_monitoring。每段錄音（含逾時補零的片段）都寫入 WAV 保持時間對齊。"""
        audio_rec = None
        try:
            if device_index is None:
                raise Exception("無效的音訊設備")
            recorder_cls = sim_devices.SimAudioRecorder if "audio" in self.simulated else AudioRecorder
            try:
                audio_rec = recorder_cls(sample_rate=sample_rate, channels=None, chunk=1024,
                                         verbose=True, device_index=device_index)
            except SystemExit as e:
                # AudioRecorder 找不到/打不開設備時會 sys.exit(1)，在此轉成錯誤事件
                raise RuntimeError(f"音訊錄製器初始化失敗（AudioRecorder 結束碼 {e.code}），"
                                   "請檢查設備是否被佔用或麥克風權限") from None
            exp_id = recorder.experiment_id
            recorder.open_wav("audio", f"audio_{exp_id}.wav", audio_rec.channels, sample_rate)
            recorder.update(audio={"device_index": audio_rec.device_index, "channels": audio_rec.channels,
                                   "sample_rate": sample_rate, "sample_format": "int16"})
            self.set_sensor("audio", text=f"錄音中（設備 {audio_rec.device_index}，{audio_rec.channels} 聲道）",
                            level="ok")
            error_notified = False
            zero_seconds = 0.0

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
                                        "可能是麥克風權限未開啟或設備靜音")
                            self.set_sensor("audio", text="訊號全為 0", level="warn")
                        elif zero_seconds == 0 and self.zero_warned:
                            self.zero_warned = False
                            self.set_sensor("audio", text="錄音中", level="ok")
                    if error_notified:
                        error_notified = False
                        self.notify("info", "通知", "音訊設備已恢復正常")
                        self.set_sensor("audio", text="錄音中", level="ok")
                except Exception as audio_error:  # noqa: BLE001
                    print(f"音訊錄製錯誤: {audio_error}")
                    if not error_notified:
                        error_notified = True
                        recorder.add_error(f"音訊錄製錯誤: {audio_error}")
                        self.notify("warning", "警告",
                                    f"音訊錄製出現錯誤: {audio_error}\n監測將繼續但音訊數據可能不完整")
                        self.set_sensor("audio", text="錄製錯誤", level="warn")
                    stop_event.wait(update_interval)
        except BaseException as e:  # noqa: BLE001
            print(f"音訊監測錯誤: {e}")
            recorder.add_error(f"音訊監測錯誤: {e}")
            self.notify("warning", "警告", f"音訊監測出現問題: {e}\n監測將繼續但不會有音訊數據")
            self.set_sensor("audio", text="初始化失敗", level="error")
        finally:
            recorder.close("audio")  # 補寫 WAV header 並關檔
            if audio_rec is not None:
                try:
                    audio_rec.close()
                except BaseException as e:  # noqa: BLE001
                    print(f"關閉音訊錄音器錯誤: {e}")

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
                recorder.write_row("distance", [now, f"{now - t0:.3f}", "", "", status])
            else:
                recorder.write_row("distance", [now, f"{now - t0:.3f}", f"{BASIC_REF + rel:.3f}", f"{rel:.3f}", status])

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
            print(">>> 測距儀 ABLE 校正中...")
            device.dll.LKIF2_SetAbleMode(OUT_NO, LKIF_ABLEMODE_AUTO)
            device.dll.LKIF2_AbleStart(OUT_NO)
            stop_event.wait(2)
            device.dll.LKIF2_AbleStop()
            print(">>> 測距儀 Auto-zero")
            device.stop_measure()
            device.dll.LKIF2_SetZeroSingle(OUT_NO, 1)
            print(">>> 測距儀初始化完成")
            # 參數設定
            device.stop_measure()
            device.dll.LKIF2_SetSamplingCycle(OUT_NO, SAMPLING_US)
            device.dll.LKIF2_SetRange(OUT_NO, RANGE_CODE)
            device.dll.LKIF2_SetReflectionMode(OUT_NO, refl_mode)
            device.dll.LKIF2_SetBasicPoint(OUT_NO, 0)
            device.start_measure()

            consecutive_failures = 0
            disconnected = False
            while not stop_event.is_set():
                try:
                    d = device.read_single(OUT_NO)
                    if d["FloatResult"] == "VALID":
                        rel = d["Value"]
                        absolute = BASIC_REF + rel
                        log("VALID", rel)
                        self.add_series_point("distance", round(float(absolute), 3))
                        consecutive_failures = 0
                        if disconnected:
                            disconnected = False
                            self.notify("info", "通知", "測距儀已重新連接")
                        self.set_sensor("distance", text=f"{absolute:.1f} mm",
                                        value=round(float(absolute), 1), level="ok")
                    else:
                        status = d["FloatResult"]
                        log(f"Unknown({d['RawStatus']})" if status == "Unknown" else status)
                        self.add_series_point("distance", None)
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_FAILURES and not disconnected:
                            disconnected = True
                            self.notify("warning", "警告", "測距儀可能已斷線，監測將繼續但不會讀取距離數據")
                        if disconnected:
                            self.set_sensor("distance", text="感測器斷線", value=None, level="error")
                        else:
                            self.set_sensor("distance", text="讀取失敗", value=None, level="warn")
                except Exception as e:  # noqa: BLE001
                    print(f"測距儀讀取錯誤: {e}")
                    log(f"ERROR: {e}")
                    self.add_series_point("distance", None)
                    consecutive_failures += 1
                    if consecutive_failures == MAX_FAILURES and not disconnected:
                        disconnected = True
                        self.notify("warning", "警告", f"測距儀錯誤: {e}\n監測將繼續但不會讀取距離數據")
                    self.set_sensor("distance", text="連接錯誤", value=None,
                                    level="error" if disconnected else "warn")
                stop_event.wait(interval)
        except BaseException as e:  # noqa: BLE001
            print(f"測距儀監測錯誤: {e}")
            recorder.add_error(f"測距儀監測錯誤: {e}")
            self.notify("warning", "警告", f"測距儀監測出現問題: {e}\n監測將繼續但不會有距離數據")
            self.set_sensor("distance", text="初始化失敗", value=None, level="error")
        finally:
            recorder.close("distance")
            if device is not None:
                try:
                    device.close()
                except BaseException:  # noqa: BLE001
                    pass

    # ---------------- 關閉伺服器 ----------------
    def shutdown(self, reason="sigint"):
        with self.lock:
            if self.shutdown_done:
                return
            self.shutdown_done = True
            running = self.phase == "running"
        print("正在關閉：停止所有監測、關閉檔案並釋放硬體…")
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
                print("正在產生頻譜 CSV（原始資料已安全存檔）。若要略過可再按一次 Ctrl+C，"
                      "之後用 uv run python app/chunked_spectrogram.py <實驗資料夾> 補產生。")
            time.sleep(0.1)
        print("已關閉。")


service = MonitorService()
app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"),
            static_folder=os.path.join(APP_DIR, "static"))


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
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
                        help="以模擬資料取代指定感測器：temp,distance,audio 或 all（預設不模擬；音訊需明確列出）")
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
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    print(f"感測器整合系統（網頁版）: http://{args.host}:{args.port}")
    print("資料一律存到 Sensor_Data/EXP_<時間>/。按 Ctrl+C 結束。")
    if service.simulated:
        print(f"*** 模擬模式：{', '.join(sorted(service.simulated))} 使用模擬資料（不是真實量測）"
              f"{'，含故障模擬' if service.sim_faults else ''} ***")
    try:
        # 不開 debug reloader：reloader 會啟動兩個行程，硬體會被雙開
        app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    except BaseException:
        _exit_reason["value"] = "error"
        traceback.print_exc()
    finally:
        service.shutdown(_exit_reason["value"])


if __name__ == "__main__":
    main()
