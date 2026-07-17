#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
ENV_NAME="${ENV_NAME:-m5stack-atom-echo}"
PORT="${PORT:-${1:-/dev/cu.usbserial-615291F27C}}"
BAUD="${BAUD:-115200}"
ESPTOOL_DIR="${ESPTOOL_DIR:-$HOME/.platformio/packages/tool-esptoolpy}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[flash] Python not found at $PYTHON_BIN" >&2
  exit 1
fi

BUILD_DIR="$ROOT_DIR/.pio/build/$ENV_NAME"
BOOTLOADER="$BUILD_DIR/bootloader.bin"
PARTITIONS="$BUILD_DIR/partitions.bin"
FIRMWARE="$BUILD_DIR/firmware.bin"

for file in "$BOOTLOADER" "$PARTITIONS" "$FIRMWARE"; do
  if [[ ! -f "$file" ]]; then
    echo "[flash] missing build artifact: $file" >&2
    echo "[flash] run scripts/build_firmware.sh first" >&2
    exit 1
  fi
done

if [[ ! -e "$PORT" ]]; then
  echo "[flash] serial port not found: $PORT" >&2
  exit 1
fi

echo "[flash] root: $ROOT_DIR"
echo "[flash] env:  $ENV_NAME"
echo "[flash] port: $PORT"
echo "[flash] baud: $BAUD"
echo "[flash] using build:"
echo "         $BOOTLOADER"
echo "         $PARTITIONS"
echo "         $FIRMWARE"

"$PYTHON_BIN" - <<'PY' "$ESPTOOL_DIR" "$PORT" "$BAUD" "$BOOTLOADER" "$PARTITIONS" "$FIRMWARE"
import sys

esptool_dir, port, baud, bootloader, partitions, firmware = sys.argv[1:]

sys.path.insert(0, esptool_dir + "/_contrib")
sys.path.insert(0, esptool_dir)

import esptool  # type: ignore

sys.argv = [
    "esptool.py",
    "--chip",
    "esp32",
    "--port",
    port,
    "--baud",
    baud,
    "--before",
    "default_reset",
    "--after",
    "hard_reset",
    "write_flash",
    "-z",
    "--flash_mode",
    "dio",
    "--flash_freq",
    "40m",
    "--flash_size",
    "detect",
    "0x1000",
    bootloader,
    "0x8000",
    partitions,
    "0x10000",
    firmware,
]
esptool.main()
PY

echo "[flash] done"
