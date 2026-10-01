"""模擬感測器：讓沒有硬體的電腦也能測試完整流程。只在 `web_monitor.py --simulate ...` 時使用。

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
    """取代 continuous_read(port)：25 ± 0.5 °C 緩慢飄移加雜訊；失敗時回傳 None（同真實函式）。"""

    def __init__(self, faults=False, seed=None):
        self.rng = random.Random(seed)
        self.t0 = time.time()
        self.clock = _FaultClock(faults)

    def read(self, port=None):  # noqa: ARG002  簽名與 continuous_read 相同
        time.sleep(0.05)  # 模擬序列埠往返時間
        if self.clock.failing():
            return None
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
        self.clock = _FaultClock(faults)
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
        if self.clock.failing():
            return {"OutNo": out_no, "RawStatus": 2, "FloatResult": "ALARM", "Value": 0.0}
        t = time.time() - self.t0
        value = 0.8 * math.sin(2 * math.pi * t / 30) + self.rng.gauss(0, 0.005)
        return {"OutNo": out_no, "RawStatus": 0, "FloatResult": "VALID", "Value": value}


class SimAudioRecorder:
    """取代 signal_package.AudioRecorder：產生 440 Hz + 掃頻 + 雜訊的 int16 單聲道訊號，即時節奏輸出。"""

    def __init__(self, sample_rate=22050, channels=None, chunk=1024, verbose=True, device_index=None):  # noqa: ARG002
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
        return np.clip(x, -32768, 32767).astype(np.int16)

    def close(self):
        pass
