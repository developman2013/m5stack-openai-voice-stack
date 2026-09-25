# M5Stack OpenAI Voice Stack

Realtime voice stack for `M5Stack Atom Echo` with two parts:

- `firmware/`: PlatformIO firmware for the device
- `home-assistant-addon/`: Home Assistant add-on that bridges the device to OpenAI Realtime and Home Assistant APIs

## What it does

- push-to-talk on the M5Stack button
- streams microphone audio over WebSocket to the local gateway
- gets realtime spoken responses back from OpenAI
- exposes Home Assistant MCP tools to the voice model
- authenticates devices with a gateway token

## Repository layout

- `firmware/README.md`: device build and flashing notes
- `home-assistant-addon/config.yaml`: add-on metadata and options schema
- `home-assistant-addon/app/main.py`: gateway server

## First setup in Home Assistant

1. Open **Settings → Add-ons → Add-on store → ⋮ → Repositories**.
2. Add `https://github.com/developman2013/m5stack-openai-voice-stack` and reload the store.
3. Open **OpenAI Voice Gateway**, install it, and start it.
4. In the add-on **Configuration** tab, set `openai_api_key`. Set a long random
   `gateway_token`; the same value is flashed to each M5Stack.
5. Keep `home_assistant_url` as `http://supervisor/core` and
   `ha_mcp_url` as `http://supervisor/core/api/mcp` when running as an add-on.
6. Open the add-on Web UI and check `/health`. It should return `"status": "ok"`.

The image is pulled from GHCR automatically. Home Assistant installations on
`amd64` and `aarch64` are supported.

## First M5Stack setup

The browser installer is available from `web/index.html` when this repository
is served as a static site. It installs a release build over USB; Chrome or
Edge is required.

1. Copy `firmware/include/firmware_config.example.h` to
   `firmware/include/firmware_config.h`.
2. Fill in Wi-Fi credentials, `GATEWAY_HOST` (the HA host name or IP), and the
   same `GATEWAY_TOKEN` configured in the add-on.
3. Build and flash the firmware from the `firmware/` directory.

Before flashing, verify that the gateway is reachable from the same LAN. The
default WebSocket endpoint is `ws://<gateway-host>:8765/ws`.

## Diagnostics

- `http://<ha-host>:8765/health` checks that the gateway process is alive and
  reports whether OpenAI, the gateway token, and MCP are configured.
- The add-on log shows WebSocket connects, wake-word events, MCP discovery, and
  Realtime errors.
- A device that connects but does not get audio usually has a mismatched token,
  gateway host, or port. Reflash after changing any of those values.

## Notes

- The repository intentionally does not include local secrets.
- The firmware keeps `firmware_config.h` untracked on purpose.
- The add-on is designed to run inside Home Assistant, but the gateway app can also be started manually for debugging.

See [implementation and verification notes](IMPLEMENTATION.md) for the current
Realtime/MCP changes, device-test setup, and remaining limitations.
