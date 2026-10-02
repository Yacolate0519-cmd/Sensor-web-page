#!/bin/bash
# Double-click launcher (opens in Terminal automatically). Real logic lives in start_monitor.sh.
SRC="${BASH_SOURCE[0]}"
while [ -L "$SRC" ]; do
  DIR="$(cd -P "$(dirname "$SRC")" && pwd)"; SRC="$(readlink "$SRC")"
  case "$SRC" in /*) ;; *) SRC="$DIR/$SRC" ;; esac
done
exec bash "$(cd -P "$(dirname "$SRC")" && pwd)/start_monitor.sh" "$@"
