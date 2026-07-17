#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIO_BIN="${PIO_BIN:-$ROOT_DIR/.venv/bin/pio}"
ENV_NAME="${ENV_NAME:-m5stack-atom-echo}"

if [[ ! -x "$PIO_BIN" ]]; then
  echo "[build] PlatformIO not found at $PIO_BIN" >&2
  exit 1
fi

echo "[build] root: $ROOT_DIR"
echo "[build] env:  $ENV_NAME"
echo "[build] pio:  $PIO_BIN"

cd "$ROOT_DIR"
"$PIO_BIN" run -e "$ENV_NAME" "$@"

echo "[build] done"
echo "[build] firmware: $ROOT_DIR/.pio/build/$ENV_NAME/firmware.bin"
