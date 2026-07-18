# Firmware

PlatformIO firmware for `M5Stack Atom Echo` that talks directly to the local voice gateway over WebSocket.

## Features

- push-to-talk using the device button
- realtime microphone streaming to the gateway
- streamed playback of assistant audio responses
- separate build, flash, and monitor scripts for a more stable workflow

## Files

- `platformio.ini`: PlatformIO environment and dependencies
- `include/firmware_config.example.h`: template for local Wi-Fi and gateway settings
- `include/firmware_config.h`: local untracked config used for actual builds
- `src/main.cpp`: main firmware logic
- `scripts/build_firmware.sh`: build helper
- `scripts/flash_firmware.sh`: direct flashing helper
- `scripts/monitor_firmware.sh`: raw serial monitor

## Setup

1. Copy `include/firmware_config.example.h` to `include/firmware_config.h`.
2. Fill in Wi-Fi credentials and gateway address.
3. Build and flash the firmware.

## Working with local debug mode

When the gateway runs locally on your development machine instead of inside Home Assistant:

- set `GATEWAY_HOST` in `include/firmware_config.h` to the LAN IP or hostname of that machine
- keep `GATEWAY_PORT` as `8765` unless you changed the local debug port
- reflash the device after changing the gateway target

When you switch back to the Home Assistant add-on, point `GATEWAY_HOST` back to your HA host again.

## Stable flashing workflow

```bash
./scripts/build_firmware.sh
./scripts/flash_firmware.sh /dev/cu.usbserial-615291F27C
./scripts/monitor_firmware.sh /dev/cu.usbserial-615291F27C
```

This avoids the usual `pio run -t upload` instability by separating build, flash, and serial monitoring.

## Hardware pins

- board: `m5stack-atom`
- button: `GPIO39`
- LED: `GPIO27`
- I2S LRCLK: `GPIO33`
- I2S BCLK: `GPIO19`
- microphone DIN: `GPIO23`
- speaker DOUT: `GPIO22`
