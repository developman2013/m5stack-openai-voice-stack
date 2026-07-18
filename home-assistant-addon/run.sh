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
exec python3 -m app.main
