#pragma once

namespace firmware_config {

constexpr char WIFI_SSID[] = "YOUR_WIFI_SSID";
constexpr char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";

constexpr char GATEWAY_HOST[] = "homeassistant.local";
constexpr uint16_t GATEWAY_PORT = 8765;
constexpr char GATEWAY_PATH[] = "/ws";

constexpr bool USE_TLS = false;
constexpr char GATEWAY_TLS_FINGERPRINT[] = "";

constexpr uint32_t MIC_SAMPLE_RATE = 16000;
constexpr uint32_t AUDIO_SAMPLE_RATE = 24000;
constexpr size_t MIC_FRAME_SAMPLES = 1024;
constexpr size_t MAX_PLAYBACK_QUEUE_CHUNKS = 32;
constexpr uint8_t MIC_GAIN = 1;

}  // namespace firmware_config
