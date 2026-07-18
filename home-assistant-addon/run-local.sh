#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env.local}"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export OPENAI_MODEL="${OPENAI_MODEL:-gpt-realtime-mini}"
export OPENAI_VOICE="${OPENAI_VOICE:-cedar}"
export ASSISTANT_INSTRUCTIONS="${ASSISTANT_INSTRUCTIONS:-You are a concise, helpful smart home voice assistant. Always answer in Russian unless the user clearly speaks another language. Keep answers short and natural for spoken conversation.}"
export HOME_ASSISTANT_URL="${HOME_ASSISTANT_URL:-http://homeassistant.local:8123}"
export LISTEN_PORT="${LISTEN_PORT:-8765}"
export LAST_INPUT_PCM_PATH="${LAST_INPUT_PCM_PATH:-/tmp/openai-last-input.pcm}"
export WAKE_WORD_ENABLED="${WAKE_WORD_ENABLED:-false}"
export WAKE_WORD_HOST="${WAKE_WORD_HOST:-core-openwakeword}"
export WAKE_WORD_PORT="${WAKE_WORD_PORT:-10400}"
export WAKE_WORD_NAME="${WAKE_WORD_NAME:-hey_jarvis}"
export WAKE_AUDIO_GAIN="${WAKE_AUDIO_GAIN:-8}"
export FOLLOW_UP_TIMEOUT_MS="${FOLLOW_UP_TIMEOUT_MS:-5000}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "OPENAI_API_KEY is required. Copy .env.local.example to .env.local and fill it in." >&2
  exit 1
fi

cd "$SCRIPT_DIR"
if [ -x "$VENV_PYTHON" ]; then
  exec "$VENV_PYTHON" -m app.main
fi

exec python3 -m app.main
