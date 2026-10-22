"""溫度計（RiXEN TX-600N）單獨測試：每秒讀一次 T1、T2，印在終端機上，按 Ctrl+C 結束時印出統計。

在專案資料夾執行：
    uv run python scripts/test_thermometer.py                  # 自動找 COM 埠
    uv run python scripts/test_thermometer.py --port COM10
    uv run python scripts/test_thermometer.py --csv out.csv    # 同時存成 CSV

不需要開網頁、不會碰其他感測器。溫度計要停在即時量測畫面；監測網頁正在讀溫度計時不要同時執行。
協定（說明書 p.11-13）：57600 8N1、Modbus RTU 03H、站號 03H；0000h=T1、0001h=T2（(Data-4000)/10），
0006h=狀態（bit2 T1-Error、bit3 T2-Error、bit5 °F、Hi byte 熱電偶類型）。
"""

import argparse
import csv
import datetime
import statistics
import sys
import time

import serial
import serial.tools.list_ports


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def request(start, count, slave=0x03):
    frame = bytes([slave, 0x03, start >> 8, start & 0xFF, count >> 8, count & 0xFF])
    c = crc16(frame)
    return frame + bytes([c & 0xFF, c >> 8])


def read_once(ser):
    """回傳 (T1 或 None, T2 或 None, 說明)。None 表示該通道 Err。"""
    ser.reset_input_buffer()
    ser.write(request(0x0000, 7))
    r = ser.read(3 + 14 + 2)
    if not r:
        return None, None, "沒有回應"
    if len(r) != 19 or r[2] != 14 or crc16(r[:-2]) != (r[-2] | r[-1] << 8):
        return None, None, f"回應錯誤（{len(r)} bytes）"
    w = [(r[3 + 2 * i] << 8) | r[4 + 2 * i] for i in range(7)]
    lo = w[6] & 0xFF
    t1 = None if lo & 0x04 else (w[0] - 4000) / 10
    t2 = None if lo & 0x08 else (w[1] - 4000) / 10
    note = []
    if lo & 0x20:
        note.append("單位是 °F")
    if w[6] >> 8:
        note.append(f"熱電偶類型代碼 {w[6] >> 8}（不是 K）")
    return t1, t2, "；".join(note)


def find_port():
    for p in serial.tools.list_ports.comports():
        if p.vid is None:
            continue
        try:
            with serial.Serial(p.device, 57600, timeout=1) as ser:
                t1, t2, note = read_once(ser)
            if note != "沒有回應" and not note.startswith("回應錯誤"):
                return p.device
        except Exception:  # noqa: BLE001  打不開就換下一個
            pass
    return None


def fmt(v):
    return "  Err " if v is None else f"{v:6.1f}"


def main():
    ap = argparse.ArgumentParser(description="TX-600N 溫度計單獨測試")
    ap.add_argument("--port", help="COM 埠，例如 COM10（不填就自動找）")
    ap.add_argument("--interval", type=float, default=1.0, help="讀取間隔秒數（預設 1）")
    ap.add_argument("--csv", help="同時把讀值存成這個 CSV 檔")
    args = ap.parse_args()
    # Windows 主控台不是 UTF-8 時，無法編碼的字元以 ? 取代，不讓程式崩潰
    sys.stdout.reconfigure(line_buffering=True, errors="replace")

    port = args.port or find_port()
    if not port:
        sys.exit("找不到溫度計：確認 TX-600N 已開機、USB 線已接，或用 --port 指定")
    print(f"溫度計在 {port}，每 {args.interval:g} 秒讀一次，Ctrl+C 結束\n")
    print("時間        T1(°C)  T2(°C)  備註")

    out = None
    if args.csv:
        out = open(args.csv, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(out)
        writer.writerow(["Timestamp", "T1(C)", "T2(C)", "Note"])

    t1s, t2s, fails = [], [], 0
    try:
        while True:
            t0 = time.time()
            # 每次讀取都重新開埠：溫度計關機再開後，一直開著的埠會收不到回應
            try:
                with serial.Serial(port, 57600, bytesize=8, parity="N", stopbits=1, timeout=1) as ser:
                    t1, t2, note = read_once(ser)
            except serial.SerialException as e:
                t1, t2, note = None, None, f"無法開啟 {port}：{e}"
            now = datetime.datetime.now()
            if t1 is None and t2 is None:
                fails += 1
            if t1 is not None:
                t1s.append(t1)
            if t2 is not None:
                t2s.append(t2)
            print(f"{now:%H:%M:%S}    {fmt(t1)}  {fmt(t2)}  {note}")
            if out:
                writer.writerow([now.isoformat(timespec="milliseconds"), t1, t2, note])
                out.flush()
            time.sleep(max(0.0, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()

    print("\n統計：")
    for name, vals in (("T1", t1s), ("T2", t2s)):
        if vals:
            sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
            print(f"  {name}: {len(vals)} 筆，平均 {statistics.mean(vals):.2f}，最小 {min(vals):.1f}，"
                  f"最大 {max(vals):.1f}，標準差 {sd:.3f} °C")
        else:
            print(f"  {name}: 沒有有效讀值（Err 或沒插探棒）")
    if fails:
        print(f"  通訊失敗 {fails} 次")


if __name__ == "__main__":
    main()
