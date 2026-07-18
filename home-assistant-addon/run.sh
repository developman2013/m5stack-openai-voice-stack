#!/usr/bin/with-contenv bashio
export OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
export OPENAI_MODEL="$(bashio::config 'openai_model')"
export OPENAI_VOICE="$(bashio::config 'openai_voice')"
export ASSISTANT_INSTRUCTIONS="$(bashio::config 'instructions')"
export HOME_ASSISTANT_URL="$(bashio::config 'home_assistant_url')"
HOME_ASSISTANT_TOKEN="$(bashio::config 'home_assistant_token')"
if [ -z "$HOME_ASSISTANT_TOKEN" ] && [ -n "${SUPERVISOR_TOKEN:-}" ]; then
  HOME_ASSISTANT_TOKEN="$SUPERVISOR_TOKEN"
fi
export HOME_ASSISTANT_TOKEN
export LISTEN_PORT="$(bashio::config 'listen_port')"
export LAST_INPUT_PCM_PATH="$(bashio::config 'last_input_pcm_path')"
export WAKE_WORD_ENABLED="$(bashio::config 'wake_word_enabled')"
export WAKE_WORD_HOST="$(bashio::config 'wake_word_host')"
export WAKE_WORD_PORT="$(bashio::config 'wake_word_port')"
export WAKE_WORD_NAME="$(bashio::config 'wake_word_name')"
export WAKE_AUDIO_GAIN="$(bashio::config 'wake_audio_gain')"
export FOLLOW_UP_TIMEOUT_MS="$(bashio::config 'follow_up_timeout_ms')"
exec python3 -m app.main
