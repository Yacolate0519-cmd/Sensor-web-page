import serial
import time
from .crc import calculate_crc
from .frame import build_request_frame
from .parser import parse_response, convert_raw_to_temperature
from .port_finder import list_candidate_ports

def read_temperature(ser, slave_addr=0x03, start_addr=0x0000, num_words=1):
    """
    發送讀取溫度的指令，並回傳轉換後的溫度值
    :param ser: 已開啟的 serial 連線
    :param slave_addr: 從站地址
    :param start_addr: 起始讀取位址
    :param num_words: 讀取字數 (預設讀取 1 word 即 T1 資料)
    :return: 轉換後的溫度值 (若失敗則傳回 None)
    """
    # 建構指令封包
    frame = build_request_frame(slave_addr, start_addr, num_words)
    ser.write(frame)
    # 根據範例，預計回應 7 個 byte (若讀取 1 word)
    response = ser.read(7)
    if len(response) < 7:
        return None
    raw_value = parse_response(response)
    if raw_value is None:
        return None
    return convert_raw_to_temperature(raw_value)

def continuous_read(port, baudrate=57600):
    """
    每呼叫一次連接 serial port、讀取一次溫度資料並回傳讀取結果
    :param port: Serial port
    :param baudrate: 傳輸速率 (預設 57600)
    :return: 溫度數值 (若失敗則回傳 None)
    """
    if not port:
        # serial.Serial(None) 不會拋錯，會延後到 write 時才失敗，因此先擋掉
        print("未指定 serial port")
        return None

    try:
        ser = serial.Serial(port, baudrate, bytesize=8, parity='N', stopbits=1, timeout=1)
    except Exception as e:
        print("無法開啟 serial port:", e)
        return None

    try:
        temp = read_temperature(ser)
        return temp
    finally:
        ser.close()


# ---------------------------------------------------------------------------
# 診斷版讀取：回傳 (溫度或 None, 原因代碼, 說明)；continuous_read 維持原樣（legacy 在用）
# ---------------------------------------------------------------------------
_BUSY_HINTS = ("permissionerror", "access is denied", "拒絕存取", "errno 13", "errno 16")


def check_port_present(port):
    """port 是否存在於系統 COM 埠清單。存在回傳 None；不存在回傳 (reason, detail)。"""
    from serial.tools import list_ports

    names = {p.device.upper() for p in list_ports.comports()}
    if str(port).upper() in names:
        return None
    detail = f"找不到 {port}：USB-RS485 轉接器未插上，或驅動（CH340/FTDI 等）未安裝"
    if not names:
        detail += "（系統目前完全偵測不到任何 COM 埠）"
    return "not_found", detail


def probe_temperature_port(port, baudrate=57600, slave_addr=0x03, timeout=1):
    """
    自動偵測用：送一次讀取，回應的長度、站號、功能碼、位元組數、CRC 全部正確才算是溫度計。
    （一般讀值沿用不驗 CRC 的舊邏輯；偵測要嚴格，才不會把其他序列裝置的回應誤認成溫度計。）
    :return: 溫度（是溫度計）或 None
    """
    try:
        ser = serial.Serial(port, baudrate, bytesize=8, parity='N', stopbits=1, timeout=timeout)
    except Exception:  # noqa: BLE001  打不開（被占用、拔掉）就當作不是
        return None
    try:
        ser.reset_input_buffer()
        ser.write(build_request_frame(slave_addr, 0x0000, 1))
        response = ser.read(7)
    except Exception:  # noqa: BLE001
        return None
    finally:
        ser.close()
    if len(response) != 7 or response[0] != slave_addr or response[1] != 0x03 or response[2] != 2:
        return None
    crc = calculate_crc(response[:5])
    if response[5] != (crc & 0xFF) or response[6] != (crc >> 8):
        return None
    return convert_raw_to_temperature(parse_response(response))


def find_temperature_port(baudrate=57600):
    """
    逐一探測 USB 轉序列埠（有 VID 者；不碰主機板內建 COM），回傳 (找到的埠或 None, 已探測的埠清單)。
    COM 號碼會隨 USB 孔改變（例如沒有序號的 Prolific），所以每次開始前重新找。
    """
    tried = []
    for p in list_candidate_ports():
        if p.vid is None:
            continue
        tried.append(p.device)
        if probe_temperature_port(p.device, baudrate) is not None:
            return p.device, tried
    return None, tried


def read_temperature_diag(port, baudrate=57600):
    """
    與 continuous_read 相同的一次讀取，但失敗時指出原因。
    :return: (溫度或 None, reason, detail)；成功時 reason="ok"、detail=""
    """
    if not port:
        return None, "disabled", "未選擇 COM 埠"

    missing = check_port_present(port)
    if missing:
        return (None, *missing)

    try:
        ser = serial.Serial(port, baudrate, bytesize=8, parity='N', stopbits=1, timeout=1)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if any(h in msg.lower() for h in _BUSY_HINTS):
            return None, "busy", f"{port} 被其他程式佔用或無權限：關閉其他使用此 COM 埠的程式後重試（{msg}）"
        return None, "error", f"無法開啟 {port}：{msg}"

    try:
        frame = build_request_frame(0x03, 0x0000, 1)
        ser.write(frame)
        response = ser.read(7)
    except Exception as e:  # noqa: BLE001
        return None, "error", f"讀寫 {port} 時發生錯誤：{e}"
    finally:
        ser.close()

    if len(response) == 0:
        return None, "no_data", "埠已開啟但感測器無回應：檢查感測器電源、RS485 A/B 接線、站號(0x03)/鮑率(57600)"
    # 與 read_temperature 一致：不額外驗證 CRC（既有硬體路徑從未驗過，擅自加會把原本可用的讀值變失敗）；
    # 不足 7 byte 或 parse_response 回 None 才視為接線/訊號異常
    raw_value = parse_response(response)
    if raw_value is None:
        return None, "wiring", "回應不完整或格式/CRC 錯誤：A/B 線可能接反、接觸不良或受干擾"
    return convert_raw_to_temperature(raw_value), "ok", ""
