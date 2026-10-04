"""Windows launcher helper (stdlib only): called by launchers/windows/start_monitor.ps1 (via start_monitor.bat) with `uv run python`.

- If a web_monitor is already answering on the port: just open the browser.
- If the port is used by another program: use the next free port.
- Start main.py (app/web_monitor.py) in this console, wait until HTTP answers (<= 30 s), then open the browser.
Env: SENSOR_MONITOR_PORT (default 5002), SENSOR_MONITOR_NO_BROWSER=1.
Extra args are passed to main.py. Also works on macOS/Linux (used for testing).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_PORT = int(os.environ.get("SENSOR_MONITOR_PORT", "5002"))
HOST = "127.0.0.1"


def is_monitor(port):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/state", timeout=2) as r:
            return "phase" in json.load(r)
    except Exception:  # noqa: BLE001
        return False


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((HOST, port)) == 0


def open_browser(url):
    if os.environ.get("SENSOR_MONITOR_NO_BROWSER"):
        print(f"（略過開啟瀏覽器：{url}）")
        return
    webbrowser.open(url)
    print(f"已用預設瀏覽器開啟 {url}")


def main():
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    sys.stderr.reconfigure(errors="replace")
    port = START_PORT
    for _ in range(20):
        if is_monitor(port):
            print(f"監測服務已在 port {port} 執行中，不再重複啟動。")
            open_browser(f"http://{HOST}:{port}")
            return 0
        if not port_in_use(port):
            break
        print(f"port {port} 被其他程式占用，改試 {port + 1}…")
        port += 1
    else:
        print("[錯誤] 找不到可用的 port。")
        return 1
    url = f"http://{HOST}:{port}"

    print("=" * 56)
    print(f" 啟動監測服務：{url}")
    print(" 關閉此視窗即停止監測服務；監測中請先在網頁按「停止」。")
    print(" （若直接關閉視窗或按 Ctrl+C，系統會嘗試先停止監測並存檔。）")
    print("=" * 56)
    proc = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--port", str(port), *sys.argv[1:]],
                            cwd=str(ROOT))
    ready = False
    deadline = time.time() + 30
    try:
        while time.time() < deadline and proc.poll() is None:
            if is_monitor(port):
                ready = True
                break
            time.sleep(0.5)
        if ready:
            open_browser(url)
        else:
            print("[警告] 服務尚未在預期時間內回應（或已結束），請查看上方訊息。")
    except KeyboardInterrupt:
        pass
    # Ctrl+C reaches the server too (same console); keep waiting so it can finish saving.
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            print("收到 Ctrl+C，等待伺服器停止監測並存檔…")


if __name__ == "__main__":
    sys.exit(main())
