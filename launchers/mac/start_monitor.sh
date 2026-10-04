#!/bin/bash
# Sensor Monitor launcher (macOS). Run from Terminal, or via "Sensor Monitor.app" / start_monitor.command.
# Env: SENSOR_MONITOR_PORT (default 5002), SENSOR_MONITOR_NO_BROWSER=1 (skip opening the browser).
# Extra arguments are passed to main.py (e.g. --simulate temp,distance).

# --- repo root, derived from this script's real location (not cwd) ---
SRC="${BASH_SOURCE[0]}"
while [ -L "$SRC" ]; do
  DIR="$(cd -P "$(dirname "$SRC")" && pwd)"
  SRC="$(readlink "$SRC")"
  case "$SRC" in /*) ;; *) SRC="$DIR/$SRC" ;; esac
done
HERE="$(cd -P "$(dirname "$SRC")" && pwd)"
ROOT="$(cd -P "$HERE/../.." && pwd)"
cd "$ROOT" || { echo "找不到專案資料夾：$ROOT"; exit 1; }

PORT="${SENSOR_MONITOR_PORT:-5002}"
export PATH="$PATH:$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
pause_exit() { say ""; printf '按 Enter 鍵關閉…'; { read -r _ </dev/tty; } 2>/dev/null || read -r _; exit "${1:-0}"; }

# Is a monitor server already answering on this port?
is_monitor() { curl -s -m 2 "http://127.0.0.1:$1/api/state" 2>/dev/null | grep -q '"phase"'; }
# Is anything listening on this port?
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
open_browser() {
  if [ -n "$SENSOR_MONITOR_NO_BROWSER" ]; then say "（略過開啟瀏覽器：$1）"; return; fi
  open "$1" && say "已用預設瀏覽器開啟 $1"
}

say "=== 感測器監測（Sensor Monitor）==="
say "專案資料夾：$ROOT"

# --- 1. uv ---
if ! command -v uv >/dev/null 2>&1; then
  say ""
  say "找不到 uv（本程式使用 uv 管理 Python 與套件）。"
  say "可以自動安裝：會從 astral.sh 下載並安裝到 ~/.local/bin，需要網路，不需要管理員權限。"
  printf '是否現在自動安裝 uv？[Y/n] '
  read -r ans
  case "$ans" in
    ""|y|Y|yes|YES|Yes)
      if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        export PATH="$HOME/.local/bin:$PATH"
      fi
      if ! command -v uv >/dev/null 2>&1; then
        say ""; say "uv 安裝失敗。請檢查網路後重試，或手動安裝："
        say "  curl -LsSf https://astral.sh/uv/install.sh | sh"
        say "安裝完成後，重新開啟本程式。"
        pause_exit 1
      fi
      ;;
    *)
      say ""
      say "已取消。請手動安裝 uv 後再重新開啟本程式："
      say "  curl -LsSf https://astral.sh/uv/install.sh | sh"
      say "  （或 brew install uv）"
      pause_exit 0
      ;;
  esac
fi

# --- 2. already running? ---
if is_monitor "$PORT"; then
  say "監測服務已在 port $PORT 執行中，不再重複啟動。"
  open_browser "http://127.0.0.1:$PORT"
  pause_exit 0
fi

# --- 3. dependencies ---
say ""
say "同步套件（uv sync）；第一次執行需要網路，可能要幾分鐘…"
if ! uv sync; then
  say ""
  say "[錯誤] 套件同步失敗。請檢查："
  say "  1. 網路是否連線（第一次執行必須上網下載 Python 與套件）"
  say "  2. 若訊息提到 pyaudio / portaudio：先執行 brew install portaudio 再重試"
  say "  3. 仍失敗請把上面的訊息截圖給負責人"
  pause_exit 1
fi

# --- 4. pick a port ---
tries=0
while true; do
  if is_monitor "$PORT"; then
    say "監測服務已在 port $PORT 執行中，不再重複啟動。"
    open_browser "http://127.0.0.1:$PORT"
    pause_exit 0
  fi
  if port_in_use "$PORT"; then
    say "port $PORT 被其他程式占用，改試 $((PORT + 1))…"
    PORT=$((PORT + 1)); tries=$((tries + 1))
    if [ "$tries" -ge 20 ]; then say "[錯誤] 找不到可用的 port。"; pause_exit 1; fi
    continue
  fi
  break
done
URL="http://127.0.0.1:$PORT"

# --- 5. start server and wait for HTTP ---
say ""
say "========================================================"
say " 啟動監測服務：$URL"
say " 關閉此視窗即停止監測服務；監測中請先在網頁按「停止」。"
say " （若直接關閉視窗或按 Ctrl+C，系統會嘗試先停止監測並存檔。）"
say "========================================================"
say ""
uv run python main.py --port "$PORT" "$@" &
SERVER_PID=$!
forward() { kill -INT "$SERVER_PID" 2>/dev/null; }
trap forward INT TERM HUP

ready=0
for _ in $(seq 1 60); do
  kill -0 "$SERVER_PID" 2>/dev/null || break
  if curl -s -m 1 -o /dev/null "$URL/api/state"; then ready=1; break; fi
  sleep 0.5
done
if [ "$ready" = 1 ]; then
  open_browser "$URL"
else
  say "[警告] 服務尚未在預期時間內回應（或已結束），請查看上方訊息。"
fi

# wait (re-wait after a forwarded signal so the server can finish saving)
while kill -0 "$SERVER_PID" 2>/dev/null; do wait "$SERVER_PID"; done
wait "$SERVER_PID" 2>/dev/null
code=$?
say ""
say "監測服務已停止。資料在 Sensor_Data 資料夾。"
pause_exit "$code"
