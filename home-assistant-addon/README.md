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

If `home_assistant_token` is empty and the add-on runs inside Home Assistant, it can fall back to `SUPERVISOR_TOKEN`.
