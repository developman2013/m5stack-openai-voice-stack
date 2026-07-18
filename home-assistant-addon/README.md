# Home Assistant Add-on

This add-on exposes a local WebSocket gateway for the M5Stack firmware and bridges it to OpenAI Realtime.

## Main pieces

- `app/main.py`: realtime gateway, browser debug page, and Home Assistant tool bridge
- `config.yaml`: add-on metadata and user-configurable options
- `run.sh`: container startup script

## Required options

- `openai_api_key`
- `openai_model`
- `openai_voice`

## Optional options

- `instructions`
- `home_assistant_url`
- `home_assistant_token`
- `listen_port`
- `last_input_pcm_path`

If `home_assistant_token` is empty and the add-on runs inside Home Assistant, it can fall back to `SUPERVISOR_TOKEN`.

## Local debug mode

You can run the gateway locally without rebuilding the Home Assistant add-on on every change.

1. Copy `.env.local.example` to `.env.local`.
2. Fill in your OpenAI API key. Add a Home Assistant long-lived access token only if you want Home Assistant tool calls to work in local mode.
3. Start the gateway with `./run-local.sh`.
4. Open `http://localhost:8765/` for the browser debug page.

Notes:

- Local mode uses the same `app/main.py` entry point as the add-on.
- By default the last captured input audio is written to `/tmp/openai-last-input.pcm`.
- `HOME_ASSISTANT_TOKEN` is optional for pure voice debugging, but required for `get_entity_state`, `search_entities`, and `call_home_assistant_service`.
- For the real device, point `firmware/include/firmware_config.h` to the machine running this local gateway, not to `homeassistant.local`.
