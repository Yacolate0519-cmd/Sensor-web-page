"""感測器整合系統（網頁版）啟動入口。

在專案根目錄執行：
    uv run python main.py                      → http://127.0.0.1:5002
    uv run python main.py --port 5010 --simulate temp,distance
所有參數原樣交給 app/web_monitor.py（--host / --port / --simulate / --simulate-faults），
說明見 uv run python main.py --help。
"""

import os
import sys

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import web_monitor  # noqa: E402

if __name__ == "__main__":
    web_monitor.main()
