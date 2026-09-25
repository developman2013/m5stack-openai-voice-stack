# Realtime + local HA MCP implementation

This checkout starts from 4f2a7db. It reuses the local sibling checkout's
`ha_mcp.py`, without modifying that checkout. A test add-on and device firmware
were deployed on 2026-09-25; see the deployment record below.

## Implemented

- Atom microphone 16 kHz PCM is converted to 24 kHz for Realtime; wake audio
  remains 16 kHz. Browser input remains 24 kHz.
- Explicit Realtime audio formats and configured output voice.
- VAD commits and manual commits have one response creation path; duplicate
  committed events are ignored. Multiple tools run after response completion,
  followed by one continuation.
- Short final audio buffers play even below the normal prebuffer threshold.
- Replies without audio also release the device into follow-up listening.
- Audio backlog is capped at 30 seconds; stalled playback closes the session.
- MCP requests have a 30-second read timeout.
- Local MCP tool discovery (including pagination), function-name mapping,
  calls, result/error forwarding and optional HA prompt context.
- Removed arbitrary HA REST service endpoint and hard-coded REST tools.
- Gateway token required before opening OpenAI or accessing MCP.
- Wake-word listening does not open an OpenAI connection. Activation opens a
  session; follow-up timeout closes it. An inactivity watchdog and a 55-minute
  maximum also close it. Firmware reconnects to local wake-word listening.
- Continuous follow-up after spoken replies; configure `continuous_conversation`
  false to retain question-only follow-up. Default follow-up window: five seconds.
- Button during thinking/playback stops the conversation (and discards context).
  After reconnection, hold again to speak. This is not voice barge-in.

## Setup for a device test

1. Enable Home Assistant MCP Server and expose intended entities to Assist.
2. Configure the add-on MCP URL and credentials. Supervisor-token compatibility
   with MCP must be verified on the target HA installation; if rejected, use a
   dedicated HA long-lived token. A model API key is also required.
3. Generate a random gateway token and set it both in add-on options and the
   ignored `firmware/include/firmware_config.h` alongside Wi-Fi/gateway settings.
4. Start openWakeWord if using Hey Jarvis; use button input for initial tests.
5. Build and flash over USB. The firmware replaces ESPHome; preserve the current
   ESPHome YAML/firmware and a recovery method first. Use real local credentials for deployment; never commit them.
6. Test one short answer, a long answer, multiple follow-ups, one exposed light,
   a failed tool call, button stop, and Wi-Fi/gateway recovery.

## Verification

Run from repository root:

```
python3 -m venv .venv
.venv/bin/pip install -r home-assistant-addon/requirements.txt pytest pytest-asyncio
PYTHONPATH=home-assistant-addon .venv/bin/pytest -q home-assistant-addon/tests
```

Nine automated tests currently pass with mock provider and device connections.

Firmware: install PlatformIO, copy the example config to the ignored local
config, then run `pio run -d firmware`. Build tested with espressif32 7.0.1.

## Remaining acceptance work

- More microphone/speaker latency and acoustic testing across network conditions.
- More acceptance coverage for device actions through Home Assistant MCP.
- Robust voice barge-in needs full-duplex audio/AEC and conversation truncation;
  the current firmware intentionally switches microphone/speaker modes.
- Broader reconnect/long-running soak tests,
  explicit device playback acknowledgements, improved resampling filter quality.
- Current device transport is plain WebSocket on the trusted LAN. Gateway token
  is not encryption. The legacy USE_TLS setting is not implemented.
- Browser demo microphone requires localhost or HTTPS.
- Deployment remains manual; restore previous firmware to return to ESPHome Assist.

## Live acceptance

The stack has been exercised on an Atom Echo against a Home Assistant MCP
Server and a live Realtime session. Push-to-talk, spoken output, MCP discovery,
gateway authentication, reconnects, and short responses completed successfully.
Keep device-specific addresses, credentials, MAC addresses, and flash backups
outside the repository.
