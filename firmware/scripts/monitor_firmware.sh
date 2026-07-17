#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
PORT="${PORT:-${1:-/dev/cu.usbserial-615291F27C}}"
BAUD="${BAUD:-115200}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[monitor] Python not found at $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -e "$PORT" ]]; then
  echo "[monitor] serial port not found: $PORT" >&2
  exit 1
fi

echo "[monitor] port: $PORT"
echo "[monitor] baud: $BAUD"
echo "[monitor] press Ctrl+C to stop"

"$PYTHON_BIN" - <<'PY' "$PORT" "$BAUD"
import sys
import time

import serial

port = sys.argv[1]
baud = int(sys.argv[2])

ser = serial.Serial(port, baud, timeout=0.25)
try:
    while True:
        data = ser.read(1024)
        if data:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()
        else:
            time.sleep(0.05)
except KeyboardInterrupt:
    pass
finally:
    ser.close()
PY
