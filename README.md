# M5Stack OpenAI Voice Stack

Realtime voice stack for `M5Stack Atom Echo` with two parts:

- `firmware/`: PlatformIO firmware for the device
- `home-assistant-addon/`: Home Assistant add-on that bridges the device to OpenAI Realtime and Home Assistant APIs

## What it does

- push-to-talk on the M5Stack button
- streams microphone audio over WebSocket to the local gateway
- gets realtime spoken responses back from OpenAI
- allows the model to call Home Assistant services

## Repository layout

- `firmware/README.md`: device build and flashing notes
- `home-assistant-addon/config.yaml`: add-on metadata and options schema
- `home-assistant-addon/app/main.py`: gateway server

## First setup

1. In `firmware/include`, copy `firmware_config.example.h` to `firmware_config.h`.
2. Fill in your Wi-Fi name, password, and gateway host in `firmware_config.h`.
3. Build and flash the firmware from the `firmware/` directory.
4. Copy `home-assistant-addon/` into your Home Assistant local add-ons folder.
5. Install the add-on in Home Assistant and fill in the OpenAI settings.

## Notes

- The repository intentionally does not include local secrets.
- The firmware keeps `firmware_config.h` untracked on purpose.
- The add-on is designed to run inside Home Assistant, but the gateway app can also be started manually for debugging.
