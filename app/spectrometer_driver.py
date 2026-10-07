"""AvaSpec-ULS2048L 光譜儀驅動的薄包裝（延遲 import）。

sensors/spectrum_py_package/ 的 avaspec.py 在 import 時就會載入 DLL，且匯入 PyQt5，
所以這裡**只在預檢／worker 啟動時**才 import；失敗（缺 PyQt5、缺 DLL…）只會丟出例外，
由 web_monitor.classify_spectrometer_error 分類，不會影響整個 app 啟動。

不修改 sensors/spectrum_py_package/。原驅動的 Spectrometer.measure_once() 把積分時間寫死 50 ms，
這裡改用同一套底層呼叫（prepare_measure → 回呼 → poll_scan）重做一次，讓積分時間可由網頁參數設定。
介面與 sim_devices.SimSpectrometer 相同：wavelength、measure(integration_ms)、close()。
"""

import time

import numpy as np

POLL_S = 0.02          # 輪詢間隔（舊程式 0.5 秒，會把每筆量測拖慢）
MIN_TIMEOUT_S = 5.0    # 與原驅動 poll_for_scan 相同的等待上限


class AvaSpecDevice:
    """真實光譜儀。建構時會 import 驅動並初始化（AVS_Init / AVS_Activate）。"""

    def __init__(self):
        # 延遲 import：缺 PyQt5 → ImportError；缺 DLL → OSError／FileNotFoundError
        from spectrum_py_package.avaspec import AVS_MeasureCallbackFunc, MeasConfigType
        from spectrum_py_package.spectrometer import Spectrometer

        self._meas_config_cls = MeasConfigType
        self._callback_cls = AVS_MeasureCallbackFunc
        self._spec = Spectrometer()  # 找不到裝置時丟 RuntimeError（「沒有找到光譜儀!」「無可用設備!」）
        self.wavelength = np.asarray(self._spec.get_wavelength(), dtype=float)
        self._cb = None  # 持有回呼參照，避免被回收

    def measure(self, integration_ms):
        """單次量測，回傳原始 counts（float 陣列，長度 = 像素數）。失敗丟 RuntimeError。"""
        spec = self._spec
        spec.data_ready = False
        cfg = self._meas_config_cls()
        cfg.m_StartPixel = 0
        cfg.m_StopPixel = spec.config.pixels - 1
        cfg.m_IntegrationTime = float(integration_ms)
        cfg.m_NrAverages = 1
        cfg.m_Trigger_m_Mode = 0  # 軟體觸發
        if spec.avs.prepare_measure(cfg) != 0:
            raise RuntimeError("AVS_PrepareMeasure 失敗")
        self._cb = self._callback_cls(spec.handle_newdata)
        if spec.avs.start_measurement(self._cb) != 0:
            raise RuntimeError("AVS_MeasureCallback 啟動失敗")
        deadline = time.time() + max(MIN_TIMEOUT_S, integration_ms / 1000.0 * 3 + 2)
        while not spec.data_ready:
            if time.time() > deadline:
                raise RuntimeError("等待超時")
            spec.avs.poll_scan()
            time.sleep(POLL_S)
        return np.asarray(spec.get_spectral_data(), dtype=float).copy()

    def close(self):
        self._spec.close_spectrometer()


def open_spectrometer():
    """開啟真實光譜儀（預檢與 worker 共用）。"""
    return AvaSpecDevice()
