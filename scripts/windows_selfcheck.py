"""感測器整合系統：Windows 環境自我檢查。

在專案資料夾執行：
    uv run python scripts/windows_selfcheck.py            # 一般檢查
    uv run python scripts/windows_selfcheck.py --mongo    # 另外檢查 MongoDB 連線
    uv run python scripts/windows_selfcheck.py --port 5002

每一項輸出 OK / WARN / FAIL 與排錯建議。也可以在 macOS 上執行（測距儀那項會顯示不支援）。
"""

import argparse
import os
import platform
import shutil
import socket
import struct
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

results = []


def report(status, name, detail="", hint=""):
    results.append(status)
    print(f"[{status:4}] {name}" + (f"：{detail}" if detail else ""))
    if hint and status != "OK":
        for line in hint.splitlines():
            print(f"         → {line}")


def check(name, hint=""):
    """裝飾器：函式回傳 (status, detail)；拋例外時記為 FAIL。"""
    def deco(fn):
        def run(*a, **k):
            try:
                status, detail = fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                status, detail = "FAIL", f"{type(e).__name__}: {e}"
            report(status, name, detail, hint)
        return run
    return deco


@check("Python 版本與位元數", "請安裝 64 位元 Python 3.12（uv python install 3.12），LKIF2.dll 是 64 位元 DLL。")
def check_python():
    bits = struct.calcsize("P") * 8
    detail = f"Python {platform.python_version()}，{bits} 位元，{platform.platform()}"
    ok = sys.version_info >= (3, 12) and bits == 64
    return ("OK" if ok else "FAIL"), detail


@check("uv 指令", "請安裝 uv：PowerShell 執行 irm https://astral.sh/uv/install.ps1 | iex，然後重新開啟終端機。")
def check_uv():
    path = shutil.which("uv")
    in_venv = sys.prefix != sys.base_prefix
    if path:
        return "OK", f"{path}（目前{'在' if in_venv else '不在'}專案虛擬環境中）"
    return ("WARN" if in_venv else "FAIL"), "PATH 中找不到 uv"


@check("Python 套件", "在專案資料夾執行 uv sync 安裝相依套件。")
def check_packages():
    from importlib import metadata
    missing, versions = [], []
    for mod, dist in (("numpy", "numpy"), ("matplotlib", "matplotlib"), ("flask", "flask"),
                      ("pyaudio", "pyaudio"), ("serial", "pyserial")):
        try:
            __import__(mod)
            versions.append(f"{mod} {metadata.version(dist)}")
        except Exception as e:  # noqa: BLE001
            missing.append(f"{mod}（{e}）")
    if missing:
        return "FAIL", "缺少：" + "、".join(missing)
    return "OK", "、".join(versions)


@check("COM 埠（溫度感測器）",
       "找不到 COM 埠時：\n1) 確認 USB 轉 RS485 轉換器已接上，並安裝驅動（常見晶片 CH340 / FTDI / CP210x / PL2303）\n"
       "2) 開啟「裝置管理員 → 連接埠 (COM 和 LPT)」確認出現 COMx\n3) 換一個 USB 孔或線材")
def check_com():
    import serial.tools.list_ports
    from temp_py_package import list_candidate_ports
    all_ports = list(serial.tools.list_ports.comports())
    cands = list_candidate_ports()
    if not all_ports:
        return "WARN", "系統沒有任何序列埠"
    desc = "；".join(f"{p.device}（{p.description}）" for p in all_ports)
    first = cands[0].device if cands else "無"
    return "OK", f"{desc}；網頁預設選擇：{first}"


@check("音訊輸入設備（麥克風）",
       "1) Windows「設定 → 隱私權與安全性 → 麥克風」開啟「麥克風存取」與「讓桌面應用程式存取麥克風」\n"
       "2) 「聲音設定 → 輸入」確認裝置未停用、音量不是 0\n3) 關閉其他占用麥克風的程式（Teams、Zoom）")
def check_audio():
    import numpy as np
    import pyaudio
    p = pyaudio.PyAudio()
    try:
        devs = [(i, p.get_device_info_by_index(i)) for i in range(p.get_device_count())]
        inputs = [(i, d) for i, d in devs if d["maxInputChannels"] > 0]
        if not inputs:
            return "FAIL", "找不到任何輸入設備"
        names = "；".join(f"{i}: {d['name']}" for i, d in inputs)
        # 試錄 0.5 秒，確認不是全零（權限被擋時常見）
        idx = inputs[0][0]
        stream = p.open(format=pyaudio.paInt16, channels=1, rate=22050, input=True,
                        frames_per_buffer=1024, input_device_index=idx)
        data = b"".join(stream.read(1024, exception_on_overflow=False) for _ in range(11))
        stream.stop_stream()
        stream.close()
        peak = int(np.abs(np.frombuffer(data, dtype="<i2")).max())
        if peak == 0:
            return "WARN", f"{names}；設備 {idx} 試錄 0.5 秒全為 0（可能是麥克風權限或靜音）"
        return "OK", f"{names}；設備 {idx} 試錄峰值 {peak}"
    finally:
        p.terminate()


@check("KEYENCE 測距儀 LKIF2.dll",
       "1) 需要 64 位元 Python 搭配 64 位元 LKIF2.dll（本專案附的版本），且 CmnLib.dll、KeyUsbDrv.dll 要在同一資料夾\n"
       "2) 安裝 KEYENCE LK-Navigator / USB 驅動，確認裝置管理員中有 LK-G5000\n"
       "3) 「OpenDevice 失敗」表示 DLL 正常但沒有連上控制器：檢查 USB 線與控制器電源")
def check_lkif():
    if os.name != "nt":
        return "WARN", "非 Windows 系統，無法載入 LKIF2.dll（此項僅在 Windows 有意義）"
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(ROOT)
    from rangefinder import LKIF2Device
    dev = LKIF2Device(os.path.join(ROOT, "LKIF2.dll"))
    try:
        dev.open()
    except RuntimeError as e:
        return "WARN", f"DLL 載入成功，但開啟裝置失敗：{e}"
    dev.close()
    return "OK", "DLL 載入並成功開啟裝置"


@check("存檔資料夾寫入與剩餘空間",
       "請確認專案資料夾不是唯讀（例如不要放在需要管理員權限的 C:\\Program Files），並保留至少 2 GB 空間。")
def check_disk():
    data_dir = os.path.join(ROOT, "Sensor_Data")
    os.makedirs(data_dir, exist_ok=True)
    test = os.path.join(data_dir, f"_selfcheck_{int(time.time())}.csv")
    with open(test, "w", newline="", encoding="utf-8-sig") as f:
        f.write("測試,123\r\n")
    with open(test, encoding="utf-8-sig") as f:
        ok = f.read().startswith("測試")
    os.remove(test)
    free = shutil.disk_usage(data_dir).free
    per_hour = 460 * 1024 ** 2
    detail = f"{data_dir} 可寫入；剩餘 {free / 1024 ** 3:.1f} GB（以預設設定約可錄 {free / per_hour:.0f} 小時）"
    if not ok:
        return "FAIL", "寫入後讀回內容不符"
    return ("OK" if free >= 2 * 1024 ** 3 else "WARN"), detail


@check("網頁連接埠", "連接埠被占用時：關閉占用的程式，或改用其他埠，例如 uv run python web_monitor.py --port 5003")
def check_port(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # 與 Flask/werkzeug 相同設定，避免伺服器剛關閉時 TIME_WAIT 殘留造成誤報
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        return "WARN", f"127.0.0.1:{port} 已被占用（{e}）；若是 web_monitor 本身正在執行則正常"
    finally:
        s.close()
    return "OK", f"127.0.0.1:{port} 可用"


@check("MongoDB（選用）",
       "找不到 MongoDB（localhost:27017）時資料仍會完整存成檔案。若要使用：\n"
       "1) 安裝 MongoDB Community Server（安裝時勾選 Install as a Service）\n"
       "2) 執行 services.msc 確認服務「MongoDB」已啟動\n3) 或在網頁中關閉「同時寫入 MongoDB」")
def check_mongo():
    import pymongo
    c = pymongo.MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)
    try:
        info = c.server_info()
        return "OK", f"MongoDB {info.get('version')} 連線成功"
    except Exception as e:  # noqa: BLE001
        return "FAIL", str(e).split(",")[0][:160]
    finally:
        c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mongo", action="store_true", help="同時檢查 MongoDB 連線")
    ap.add_argument("--port", type=int, default=5002)
    args = ap.parse_args()
    print(f"專案資料夾：{ROOT}\n")
    check_python()
    check_uv()
    check_packages()
    check_com()
    check_audio()
    check_lkif()
    check_disk()
    check_port(args.port)
    if args.mongo:
        check_mongo()
    else:
        report("OK", "MongoDB（選用）", "未檢查（預設關閉；加 --mongo 參數可檢查）")
    print(f"\n結果：OK {results.count('OK')}、WARN {results.count('WARN')}、FAIL {results.count('FAIL')}")
    return 1 if "FAIL" in results else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
