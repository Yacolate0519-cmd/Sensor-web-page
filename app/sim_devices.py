"""模擬感測器：讓沒有硬體的電腦也能測試完整流程。只在 `main.py --simulate ...` 時使用。

真實硬體路徑（temp_py_package.continuous_read、rangefinder.LKIF2Device、signal_package.AudioRecorder）
完全不經過這個模組。模擬資料在網頁、experiment_<id>.json 與實驗資料夾內的 SIMULATED.txt 都會清楚標示。

故障模式（--simulate-faults）：每隔一段時間連續失敗 FAULT_LENGTH 次（≥10），以觸發
「感測器斷線」→「已重新連接」的通知流程。
"""

import math
import random
import threading
import time

import numpy as np

from rangefinder.constants import RC_OK

SIM_KEYS = ("temp", "distance", "audio")
FAULT_EVERY_S = 45     # 每 45 秒進入一次故障
FAULT_LENGTH = 12      # 連續失敗 12 次（超過斷線門檻 10 次）
FAULT_STEP = 4         # 溫度故障期間每 4 次失敗換一種原因，方便在 UI 看到不同的失敗分類


def parse_simulate(value):
    """'temp,distance' / 'all' / '' → set。音訊只有明確列出（或 all）才模擬。"""
    if not value:
        return set()
    items = {v.strip().lower() for v in value.split(",") if v.strip()}
    if "all" in items:
        return set(SIM_KEYS)
    alias = {"temperature": "temp", "rangefinder": "distance", "mic": "audio"}
    items = {alias.get(v, v) for v in items}
    unknown = items - set(SIM_KEYS)
    if unknown:
        raise ValueError(f"未知的模擬項目：{', '.join(sorted(unknown))}（可用：temp, distance, audio, all）")
    return items


TEMP_FAULTS = (
    ("no_data", "（模擬）埠已開啟但感測器無回應：檢查感測器電源、RS485 A/B 接線、站號(0x03)/鮑率(57600)"),
    ("wiring", "（模擬）回應不完整或格式/CRC 錯誤：A/B 線可能接反、接觸不良或受干擾"),
)
# 故障期間輪流回傳的測距儀 FloatResult。測距儀每 0.1 秒讀一次，以次數計的故障只會維持約 1 秒、肉眼看不到，
# 所以改用時間：每輪從第 FAULT_EVERY_S 秒起，每種狀態維持 DISTANCE_FAULT_EACH_S 秒
DISTANCE_FAULTS = ("WAITING", "ALARM", "+RANGEOVER")
DISTANCE_FAULT_EACH_S = 4
# 音訊故障排程（每 FAULT_EVERY_S 秒一輪，與溫度/距離錯開）：先全 0 一段，再丟 1 秒例外
AUDIO_SILENT_AT, AUDIO_SILENT_S = 15, 8   # 全 0 滿 3 秒才觸發 no_data，因此約可看到 5 秒
AUDIO_ERROR_AT, AUDIO_ERROR_S = 30, 4


class _FaultClock:
    """faults=True 時，每 FAULT_EVERY_S 秒之後的 FAULT_LENGTH 次呼叫回傳失敗。"""

    def __init__(self, faults):
        self.faults = faults
        self.t0 = time.time()
        self.remaining = 0
        self.next_fault = self.t0 + FAULT_EVERY_S

    def failing(self):
        if not self.faults:
            return False
        if self.remaining == 0 and time.time() >= self.next_fault:
            self.remaining = FAULT_LENGTH
        if self.remaining > 0:
            self.remaining -= 1
            if self.remaining == 0:
                self.next_fault = time.time() + FAULT_EVERY_S
            return True
        return False


class SimTemperature:
    """取代 continuous_read(port)：25 ± 0.5 °C 緩慢飄移加雜訊；失敗時回傳 None（同真實函式）。
    read_diag 是對應 read_temperature_diag 的診斷版（web 端模擬時使用）。"""

    def __init__(self, faults=False, seed=None):
        self.rng = random.Random(seed)
        self.t0 = time.time()
        self.clock = _FaultClock(faults)
        self.fail_n = 0

    def read_diag(self, port=None):  # 簽名與 reader.read_temperature_diag 相同
        """診斷版：回傳 (溫度或 None, reason, detail)；故障期間輪流回 no_data / wiring。"""
        time.sleep(0.05)  # 模擬序列埠往返時間
        if self.clock.failing():
            reason, detail = TEMP_FAULTS[(self.fail_n // FAULT_STEP) % len(TEMP_FAULTS)]
            self.fail_n += 1
            return None, reason, detail
        return self._value(), "ok", ""

    def read(self, port=None):  # noqa: ARG002  簽名與 continuous_read 相同
        time.sleep(0.05)  # 模擬序列埠往返時間
        if self.clock.failing():
            return None
        return self._value()

    def _value(self):
        t = time.time() - self.t0
        return 25.0 + 0.5 * math.sin(2 * math.pi * t / 600) + self.rng.gauss(0, 0.03)


class _SimDll:
    """LKIF2.dll 的替身：所有 LKIF2_* 呼叫都回傳 RC_OK。"""

    def __getattr__(self, name):
        if not name.startswith("LKIF2_"):
            raise AttributeError(name)
        return lambda *args: RC_OK


class SimLKIF2Device:
    """取代 rangefinder.LKIF2Device：open/close/start/stop/read_single 介面相同。

    相對位移 = 0.8 mm·sin(2πt/30 s) + 雜訊；絕對值由 web_monitor 加上 BASIC_REF（50 mm）。
    """

    def __init__(self, dll_path=None, faults=False, seed=None):  # noqa: ARG002
        self.dll = _SimDll()
        self.rng = random.Random(seed)
        self.t0 = time.time()
        self.faults = faults
        self.opened = False

    def open(self):
        self.opened = True
        return RC_OK

    def close(self):
        self.opened = False
        return RC_OK

    def stop_measure(self):
        return RC_OK

    def start_measure(self):
        return RC_OK

    def read_single(self, out_no=0):
        t = time.time() - self.t0
        if self.faults and t >= FAULT_EVERY_S:
            k = int((t % FAULT_EVERY_S) // DISTANCE_FAULT_EACH_S)
            if k < len(DISTANCE_FAULTS):
                return {"OutNo": out_no, "RawStatus": 2, "FloatResult": DISTANCE_FAULTS[k], "Value": 0.0}
        value = 0.8 * math.sin(2 * math.pi * t / 30) + self.rng.gauss(0, 0.005)
        return {"OutNo": out_no, "RawStatus": 0, "FloatResult": "VALID", "Value": value}


class SimAudioRecorder:
    """取代 signal_package.AudioRecorder：產生 440 Hz + 掃頻 + 雜訊的 int16 單聲道訊號，即時節奏輸出。"""

    def __init__(self, sample_rate=22050, channels=None, chunk=1024, verbose=True, device_index=None,  # noqa: ARG002
                 faults=False):
        self.faults = faults
        self.t0 = time.time()
        self.sample_rate = sample_rate
        self.chunk = chunk
        self.channels = 1
        self.device_index = -1  # 標示為模擬設備
        self.n = 0
        self.rng = np.random.default_rng()
        self._lock = threading.Lock()
        self._next = time.time()

    def record_audio(self, duration=1):
        num_chunks = int(self.sample_rate / self.chunk * duration)  # 與 AudioRecorder 相同的樣本數
        n = max(1, num_chunks) * self.chunk
        with self._lock:
            t = (self.n + np.arange(n)) / self.sample_rate
            self.n += n
        sweep = 300 + 200 * np.sin(2 * np.pi * t / 20)
        x = 2500 * np.sin(2 * np.pi * 440 * t) + 1200 * np.sin(2 * np.pi * sweep * t) + 400 * self.rng.standard_normal(n)
        # 依真實時間節奏輸出，避免比實際錄音快
        self._next += n / self.sample_rate
        delay = self._next - time.time()
        if delay > 0:
            time.sleep(delay)
        else:
            self._next = time.time()
        if self.faults:
            phase = (time.time() - self.t0) % FAULT_EVERY_S
            if AUDIO_ERROR_AT <= phase < AUDIO_ERROR_AT + AUDIO_ERROR_S:
                time.sleep(0.2)  # 模擬讀取逾時的等待，避免例外迴圈空轉
                raise RuntimeError("（模擬）音訊串流讀取逾時")
            if AUDIO_SILENT_AT <= phase < AUDIO_SILENT_AT + AUDIO_SILENT_S:
                return np.zeros(n, dtype=np.int16)  # 模擬麥克風靜音／權限未開（連續 3 秒全 0 觸發 no_data）
        return np.clip(x, -32768, 32767).astype(np.int16)

    def close(self):
        pass
