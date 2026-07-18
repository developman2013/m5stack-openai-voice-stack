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
- `wake_word_enabled`
- `wake_word_host`
- `wake_word_port`
- `wake_word_name`
- `wake_audio_gain`
- `follow_up_timeout_ms`

## Wake word

The default wake word is `Hey Jarvis`. In Home Assistant OS, install and start
the official openWakeWord app before enabling wake-word mode. The gateway uses
the Wyoming endpoint at `core-openwakeword:10400` by default.

The M5Stack streams 16 kHz PCM audio to the gateway while idle. Wake-word audio
is processed locally and is not forwarded to OpenAI. The device button remains
available as a fallback push-to-talk control.

When the assistant asks a real question or requests confirmation, the device
opens a short follow-up window after playback. The default timeout is 5 seconds;
if no speech is detected, it returns to wake-word mode.

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
