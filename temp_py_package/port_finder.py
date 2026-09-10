"""跨平台自動偵測溫度感測器所在的序列埠。

Windows 只會列出 COMx，挑選相對單純；macOS 與 Linux 則會混入
/dev/cu.debug-console、藍牙等系統虛擬埠，若盲選列舉結果的第一個必定失敗。
本模組以「是否為實體 USB 轉序列晶片」為判準替候選埠排序。
"""

import serial.tools.list_ports

# 常見 USB 轉序列晶片的 VID
KNOWN_USB_SERIAL_VIDS = {
    0x067B,  # Prolific PL2303
    0x0403,  # FTDI
    0x1A86,  # QinHeng CH340/CH341
    0x10C4,  # Silicon Labs CP210x
    0x0483,  # STMicroelectronics
    0x2341,  # Arduino
}

# 系統虛擬埠的名稱特徵，這些永遠不會是感測器
EXCLUDE_KEYWORDS = ('bluetooth', 'debug-console', 'wlan-debug')


def _is_excluded(port):
    """判斷是否為系統虛擬埠。"""
    text = f"{port.device} {port.description or ''}".lower()
    return any(keyword in text for keyword in EXCLUDE_KEYWORDS)


def _rank(port):
    """排序權重，數字越小越可能是感測器。"""
    if port.vid in KNOWN_USB_SERIAL_VIDS:
        return 0  # 已知的 USB 轉序列晶片
    if port.vid is not None:
        return 1  # 其他實體 USB 裝置
    return 2      # 沒有 VID，多半是系統虛擬埠


def list_candidate_ports():
    """回傳可能是感測器的序列埠物件，最可能的排在最前面。"""
    candidates = [
        port for port in serial.tools.list_ports.comports()
        if not _is_excluded(port)
    ]
    return sorted(candidates, key=lambda port: (_rank(port), port.device))


def find_sensor_port(default=None):
    """回傳最可能是感測器的序列埠名稱，找不到時回傳 default。"""
    candidates = list_candidate_ports()
    return candidates[0].device if candidates else default
