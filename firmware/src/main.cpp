#include <Arduino.h>
#include <ArduinoJson.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <driver/i2s.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <Adafruit_NeoPixel.h>
#include <algorithm>
#include <deque>
#include <vector>

#include "firmware_config.h"

namespace {

using firmware_config::AUDIO_SAMPLE_RATE;
using firmware_config::GATEWAY_HOST;
using firmware_config::GATEWAY_PATH;
using firmware_config::GATEWAY_PORT;
using firmware_config::MAX_PLAYBACK_QUEUE_CHUNKS;
using firmware_config::MIC_GAIN;
using firmware_config::MIC_FRAME_SAMPLES;
using firmware_config::MIC_SAMPLE_RATE;
using firmware_config::USE_TLS;
using firmware_config::WIFI_PASSWORD;
using firmware_config::WIFI_SSID;

constexpr gpio_num_t PIN_BUTTON = GPIO_NUM_39;
constexpr gpio_num_t PIN_LED = GPIO_NUM_27;
constexpr gpio_num_t PIN_I2S_LRCLK = GPIO_NUM_33;
constexpr gpio_num_t PIN_I2S_BCLK = GPIO_NUM_19;
constexpr gpio_num_t PIN_MIC_DIN = GPIO_NUM_23;
constexpr gpio_num_t PIN_SPK_DOUT = GPIO_NUM_22;

constexpr i2s_port_t I2S_PORT_AUDIO = I2S_NUM_0;

constexpr uint32_t BUTTON_DEBOUNCE_MS = 30;
constexpr uint32_t WIFI_RETRY_MS = 5000;
constexpr uint32_t WS_RECONNECT_MS = 2000;
constexpr size_t PLAYBACK_MAX_BUFFER_CHUNKS = MAX_PLAYBACK_QUEUE_CHUNKS;
constexpr size_t PLAYBACK_START_BUFFER_CHUNKS = 8;
constexpr uint32_t PLAYBACK_IDLE_RESET_MS = 250;
constexpr uint32_t WAKE_COMMAND_DELAY_MS = 300;
constexpr uint32_t DEFAULT_FOLLOW_UP_TIMEOUT_MS = 5000;
constexpr uint8_t AUDIO_FRAME_WAKE = 0;
constexpr uint8_t AUDIO_FRAME_COMMAND = 1;
constexpr size_t MAX_OUTBOUND_AUDIO_CHUNKS = 12;

enum class DeviceState {
  Booting,
  WiFiConnecting,
  Idle,
  Listening,
  Thinking,
  Playing,
  Error,
};

enum class AudioMode {
  None,
  Microphone,
  Speaker,
};

WebSocketsClient ws;
Adafruit_NeoPixel led(1, PIN_LED, NEO_GRB + NEO_KHZ800);

portMUX_TYPE stateMux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE playbackMux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE audioMux = portMUX_INITIALIZER_UNLOCKED;
SemaphoreHandle_t audioIoMutex = nullptr;

DeviceState deviceState = DeviceState::Booting;
bool wsConnected = false;
bool listening = false;
bool wakeListening = false;
bool buttonPressed = false;
bool responsePlaybackComplete = false;
bool followUpRequested = false;
uint32_t lastButtonChangeMs = 0;
AudioMode audioMode = AudioMode::None;
uint32_t outboundAudioChunksQueued = 0;
uint32_t outboundAudioChunksSent = 0;
uint32_t commandAudioStartsAtMs = 0;
uint32_t followUpDeadlineMs = 0;
uint32_t followUpTimeoutMs = DEFAULT_FOLLOW_UP_TIMEOUT_MS;

std::deque<std::vector<uint8_t>> playbackQueue;
std::deque<String> outboundQueue;
std::deque<std::vector<uint8_t>> outboundAudioQueue;

constexpr char FIRMWARE_VERSION[] = "rt-gw-1.1";

void stopAudioI2S();
bool configureMicrophoneI2S();
bool configureSpeakerI2S();
void requestPlaybackAudio(size_t slots);
void startWakeListening();
void startFollowUpListening();

void setState(DeviceState nextState) {
  portENTER_CRITICAL(&stateMux);
  deviceState = nextState;
  portEXIT_CRITICAL(&stateMux);
}

void setLedColor(uint8_t r, uint8_t g, uint8_t b) {
  led.setPixelColor(0, led.Color(r, g, b));
  led.show();
}

void refreshLed() {
  DeviceState current;
  portENTER_CRITICAL(&stateMux);
  current = deviceState;
  portEXIT_CRITICAL(&stateMux);

  switch (current) {
    case DeviceState::Booting:
    case DeviceState::WiFiConnecting:
      setLedColor(32, 16, 0);
      break;
    case DeviceState::Idle:
      setLedColor(0, 24, 24);
      break;
    case DeviceState::Listening:
      setLedColor(0, 0, 64);
      break;
    case DeviceState::Thinking:
      setLedColor(32, 0, 32);
      break;
    case DeviceState::Playing:
      setLedColor(0, 48, 0);
      break;
    case DeviceState::Error:
      setLedColor(64, 0, 0);
      break;
  }
}

void sendJson(const JsonDocument& doc) {
  String payload;
  serializeJson(doc, payload);
  ws.sendTXT(payload);
}

void enqueueOutbound(String&& payload) {
  portENTER_CRITICAL(&playbackMux);
  outboundQueue.emplace_back(std::move(payload));
  if (outboundQueue.size() > 64) {
    outboundQueue.pop_front();
  }
  portEXIT_CRITICAL(&playbackMux);
}

bool dequeueOutbound(String& payload) {
  bool hasPayload = false;
  portENTER_CRITICAL(&playbackMux);
  if (!outboundQueue.empty()) {
    payload = std::move(outboundQueue.front());
    outboundQueue.pop_front();
    hasPayload = true;
  }
  portEXIT_CRITICAL(&playbackMux);
  return hasPayload;
}

void enqueueOutboundAudio(const int16_t* samples, size_t sampleCount,
                          bool commandAudio) {
  std::vector<uint8_t> frame(1 + sampleCount * sizeof(int16_t));
  frame[0] = commandAudio ? AUDIO_FRAME_COMMAND : AUDIO_FRAME_WAKE;
  memcpy(frame.data() + 1, samples, sampleCount * sizeof(int16_t));

  portENTER_CRITICAL(&playbackMux);
  if (outboundAudioQueue.size() >= MAX_OUTBOUND_AUDIO_CHUNKS) {
    outboundAudioQueue.pop_front();
  }
  outboundAudioQueue.emplace_back(std::move(frame));
  portEXIT_CRITICAL(&playbackMux);
}

bool dequeueOutboundAudio(std::vector<uint8_t>& frame) {
  bool hasFrame = false;
  portENTER_CRITICAL(&playbackMux);
  if (!outboundAudioQueue.empty()) {
    frame = std::move(outboundAudioQueue.front());
    outboundAudioQueue.pop_front();
    hasFrame = true;
  }
  portEXIT_CRITICAL(&playbackMux);
  return hasFrame;
}

void clearOutboundAudio() {
  portENTER_CRITICAL(&playbackMux);
  outboundAudioQueue.clear();
  portEXIT_CRITICAL(&playbackMux);
}

bool hasOutboundAudio() {
  portENTER_CRITICAL(&playbackMux);
  const bool hasAudio = !outboundAudioQueue.empty();
  portEXIT_CRITICAL(&playbackMux);
  return hasAudio;
}

void sendCommit() {
  JsonDocument doc;
  doc["type"] = "commit";
  String payload;
  serializeJson(doc, payload);
  enqueueOutbound(std::move(payload));
  setState(DeviceState::Thinking);
  refreshLed();
}

void startListening() {
  if (!wsConnected) {
    Serial.println("[voice] websocket is not connected");
    setState(DeviceState::Error);
    refreshLed();
    return;
  }

  if (deviceState == DeviceState::Playing || deviceState == DeviceState::Thinking) {
    listening = false;
    wakeListening = false;
    followUpRequested = false;
    responsePlaybackComplete = false;
    clearOutboundAudio();
    portENTER_CRITICAL(&playbackMux);
    playbackQueue.clear();
    portEXIT_CRITICAL(&playbackMux);
    stopAudioI2S();
    enqueueOutbound(String("{\"type\":\"end_conversation\"}"));
    setState(DeviceState::Idle);
    refreshLed();
    return;
  }
  enqueueOutbound(String("{\"type\":\"begin\"}"));
  wakeListening = false;
  responsePlaybackComplete = false;
  followUpRequested = false;
  followUpDeadlineMs = 0;
  AudioMode currentAudioMode;
  portENTER_CRITICAL(&audioMux);
  currentAudioMode = audioMode;
  portEXIT_CRITICAL(&audioMux);
  if (currentAudioMode != AudioMode::Microphone) {
    if (!configureMicrophoneI2S()) {
      setState(DeviceState::Error);
      refreshLed();
      return;
    }
  }
  listening = true;
  commandAudioStartsAtMs = 0;
  outboundAudioChunksQueued = 0;
  outboundAudioChunksSent = 0;
  setState(DeviceState::Listening);
  refreshLed();
  Serial.println("[voice] listening started");
}

void startWakeListening() {
  if (!wsConnected || listening || wakeListening) {
    return;
  }

  AudioMode currentAudioMode;
  portENTER_CRITICAL(&audioMux);
  currentAudioMode = audioMode;
  portEXIT_CRITICAL(&audioMux);
  if (currentAudioMode != AudioMode::Microphone && !configureMicrophoneI2S()) {
    setState(DeviceState::Error);
    refreshLed();
    return;
  }

  wakeListening = true;
  commandAudioStartsAtMs = 0;
  followUpRequested = false;
  followUpDeadlineMs = 0;
  outboundAudioChunksQueued = 0;
  outboundAudioChunksSent = 0;
  setState(DeviceState::Idle);
  refreshLed();
  Serial.println("[wake] listening for Hey Jarvis");
}

void startFollowUpListening() {
  if (!wsConnected || listening || wakeListening) {
    return;
  }

  AudioMode currentAudioMode;
  portENTER_CRITICAL(&audioMux);
  currentAudioMode = audioMode;
  portEXIT_CRITICAL(&audioMux);
  if (currentAudioMode != AudioMode::Microphone && !configureMicrophoneI2S()) {
    setState(DeviceState::Error);
    refreshLed();
    return;
  }

  listening = true;
  commandAudioStartsAtMs = 0;
  followUpDeadlineMs = millis() + followUpTimeoutMs;
  outboundAudioChunksQueued = 0;
  outboundAudioChunksSent = 0;
  setState(DeviceState::Listening);
  refreshLed();
  Serial.printf("[follow-up] listening for %lu ms\n",
                static_cast<unsigned long>(followUpTimeoutMs));
}

void stopListeningAndCommit() {
  if (!listening) {
    return;
  }

  listening = false;
  commandAudioStartsAtMs = 0;
  followUpDeadlineMs = 0;
  stopAudioI2S();
  sendCommit();
  Serial.println("[voice] listening stopped; commit sent");
}

void enqueuePlayback(std::vector<uint8_t>&& chunk) {
  if (chunk.empty()) {
    return;
  }

  size_t queuedChunks = 0;
  portENTER_CRITICAL(&playbackMux);
  if (playbackQueue.size() >= PLAYBACK_MAX_BUFFER_CHUNKS) {
    queuedChunks = playbackQueue.size();
    portEXIT_CRITICAL(&playbackMux);
    Serial.printf("[playback.enqueue] overflow at %u chunks, dropping newest chunk\n",
                  static_cast<unsigned>(queuedChunks));
    return;
  }
  playbackQueue.emplace_back(std::move(chunk));
  queuedChunks = playbackQueue.size();
  portEXIT_CRITICAL(&playbackMux);

  static uint32_t lastEnqueueMs = 0;
  const uint32_t now = millis();
  if (lastEnqueueMs != 0) {
    const uint32_t gapMs = now - lastEnqueueMs;
    if (gapMs > 120) {
      Serial.printf("[playback.enqueue] gap=%lu ms queued=%u\n",
                    static_cast<unsigned long>(gapMs),
                    static_cast<unsigned>(queuedChunks));
    }
  }
  lastEnqueueMs = now;
}

void requestPlaybackAudio(size_t slots) {
  if (!wsConnected || slots == 0) {
    return;
  }

  JsonDocument doc;
  doc["type"] = "audio_request";
  doc["slots"] = slots;
  String payload;
  serializeJson(doc, payload);
  enqueueOutbound(std::move(payload));
}

bool dequeuePlayback(std::vector<uint8_t>& chunk) {
  bool hasChunk = false;
  portENTER_CRITICAL(&playbackMux);
  if (!playbackQueue.empty()) {
    chunk = std::move(playbackQueue.front());
    playbackQueue.pop_front();
    hasChunk = true;
  }
  portEXIT_CRITICAL(&playbackMux);
  return hasChunk;
}

bool stopAudioI2SLocked() {
  bool shouldUninstall = false;
  portENTER_CRITICAL(&audioMux);
  shouldUninstall = audioMode != AudioMode::None;
  audioMode = AudioMode::None;
  portEXIT_CRITICAL(&audioMux);

  if (shouldUninstall) {
    const esp_err_t err = i2s_driver_uninstall(I2S_PORT_AUDIO);
    if (err != ESP_OK) {
      Serial.printf("[audio] uninstall failed: %d\n", static_cast<int>(err));
      return false;
    }
  }
  return true;
}

void stopAudioI2S() {
  if (audioIoMutex == nullptr ||
      xSemaphoreTake(audioIoMutex, pdMS_TO_TICKS(500)) != pdTRUE) {
    Serial.println("[audio] timed out waiting to stop I2S");
    return;
  }
  if (!stopAudioI2SLocked()) {
    xSemaphoreGive(audioIoMutex);
    return;
  }
  xSemaphoreGive(audioIoMutex);
}

bool configureMicrophoneI2S() {
  if (audioIoMutex == nullptr ||
      xSemaphoreTake(audioIoMutex, pdMS_TO_TICKS(500)) != pdTRUE) {
    Serial.println("[audio] timed out waiting for microphone I2S");
    return false;
  }

  if (!stopAudioI2SLocked()) {
    xSemaphoreGive(audioIoMutex);
    return false;
  }

  const i2s_config_t config = {
      .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
      .sample_rate = static_cast<uint32_t>(MIC_SAMPLE_RATE),
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = static_cast<int>(MIC_FRAME_SAMPLES),
      .use_apll = true,
      .tx_desc_auto_clear = false,
      .fixed_mclk = 0,
      .mclk_multiple = I2S_MCLK_MULTIPLE_DEFAULT,
      .bits_per_chan = I2S_BITS_PER_CHAN_16BIT,
  };

  i2s_pin_config_t pins{};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_I2S_BCLK;
  pins.ws_io_num = PIN_I2S_LRCLK;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_DIN;

  esp_err_t err = i2s_driver_install(I2S_PORT_AUDIO, &config, 0, nullptr);
  if (err == ESP_OK) err = i2s_set_pin(I2S_PORT_AUDIO, &pins);
  if (err == ESP_OK) err = i2s_zero_dma_buffer(I2S_PORT_AUDIO);
  if (err == ESP_OK) {
    portENTER_CRITICAL(&audioMux);
    audioMode = AudioMode::Microphone;
    portEXIT_CRITICAL(&audioMux);
  } else {
    i2s_driver_uninstall(I2S_PORT_AUDIO);
  }
  xSemaphoreGive(audioIoMutex);

  if (err != ESP_OK) {
    Serial.printf("[audio] microphone setup failed: %d\n", static_cast<int>(err));
    return false;
  }
  Serial.println("[audio] configured microphone");
  return true;
}

bool configureSpeakerI2S() {
  if (audioIoMutex == nullptr ||
      xSemaphoreTake(audioIoMutex, pdMS_TO_TICKS(500)) != pdTRUE) {
    Serial.println("[audio] timed out waiting for speaker I2S");
    return false;
  }

  if (!stopAudioI2SLocked()) {
    xSemaphoreGive(audioIoMutex);
    return false;
  }

  const i2s_config_t config = {
      .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX),
      .sample_rate = static_cast<uint32_t>(AUDIO_SAMPLE_RATE),
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
      .dma_buf_count = 8,
      .dma_buf_len = 512,
      .use_apll = false,
      .tx_desc_auto_clear = true,
      .fixed_mclk = 0,
      .mclk_multiple = I2S_MCLK_MULTIPLE_DEFAULT,
      .bits_per_chan = I2S_BITS_PER_CHAN_16BIT,
  };

  i2s_pin_config_t pins{};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_I2S_BCLK;
  pins.ws_io_num = PIN_I2S_LRCLK;
  pins.data_out_num = PIN_SPK_DOUT;
  pins.data_in_num = I2S_PIN_NO_CHANGE;

  esp_err_t err = i2s_driver_install(I2S_PORT_AUDIO, &config, 0, nullptr);
  if (err == ESP_OK) err = i2s_set_pin(I2S_PORT_AUDIO, &pins);
  if (err == ESP_OK) err = i2s_zero_dma_buffer(I2S_PORT_AUDIO);
  if (err == ESP_OK) {
    portENTER_CRITICAL(&audioMux);
    audioMode = AudioMode::Speaker;
    portEXIT_CRITICAL(&audioMux);
  } else {
    i2s_driver_uninstall(I2S_PORT_AUDIO);
  }
  xSemaphoreGive(audioIoMutex);

  if (err != ESP_OK) {
    Serial.printf("[audio] speaker setup failed: %d\n", static_cast<int>(err));
    return false;
  }
  Serial.println("[audio] configured speaker");
  return true;
}

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);
  delay(100);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.setMinSecurity(WIFI_AUTH_WPA2_PSK);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  setState(DeviceState::WiFiConnecting);
  refreshLed();

  uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print('.');
    if (millis() - started > WIFI_RETRY_MS) {
      Serial.println("\n[wifi] retrying connection");
      started = millis();
    }
  }

  Serial.printf("\n[wifi] connected: %s\n", WiFi.localIP().toString().c_str());
  setState(DeviceState::Idle);
  refreshLed();
}

void handleTextMessage(const char* payload) {
  JsonDocument doc;
  auto error = deserializeJson(doc, payload);
  if (error) {
    Serial.printf("[ws] invalid json: %s\n", error.c_str());
    return;
  }

  const char* type = doc["type"] | "";
  if (strcmp(type, "wake_word.detected") == 0) {
    if (!wakeListening) {
      return;
    }
    wakeListening = false;
    listening = true;
    commandAudioStartsAtMs = millis() + WAKE_COMMAND_DELAY_MS;
    followUpDeadlineMs = 0;
    responsePlaybackComplete = false;
    outboundAudioChunksQueued = 0;
    outboundAudioChunksSent = 0;
    setState(DeviceState::Listening);
    refreshLed();
    Serial.printf("[wake] detected: %s\n", doc["name"] | "unknown");
    return;
  }

  if (strcmp(type, "input_audio_buffer.speech_stopped") == 0) {
    if (listening) {
      listening = false;
      commandAudioStartsAtMs = 0;
      followUpDeadlineMs = 0;
      stopAudioI2S();
      setState(DeviceState::Thinking);
      refreshLed();
      Serial.println("[voice] speech stopped by server VAD");
    }
    return;
  }

  if (strcmp(type, "input_audio_buffer.speech_started") == 0) {
    if (listening && followUpDeadlineMs != 0) {
      followUpDeadlineMs = 0;
      Serial.println("[follow-up] speech detected");
    }
    return;
  }

  if (strcmp(type, "gateway.playback_complete") == 0) {
    responsePlaybackComplete = true;
    followUpRequested = doc["follow_up"] | false;
    followUpTimeoutMs = std::max(
        static_cast<uint32_t>(1000),
        std::min(static_cast<uint32_t>(doc["timeout_ms"] | DEFAULT_FOLLOW_UP_TIMEOUT_MS),
                 static_cast<uint32_t>(30000)));
    Serial.println("[playback] gateway delivery complete");
    return;
  }

  if (strcmp(type, "conversation.item.input_audio_transcription.completed") == 0) {
    Serial.printf("[user] %s\n", doc["transcript"] | "");
    setState(DeviceState::Thinking);
    refreshLed();
    return;
  }

  if (strcmp(type, "response.output_audio_transcript.delta") == 0) {
    Serial.printf("[assistant.partial] %s\n", doc["delta"] | "");
    return;
  }

  if (strcmp(type, "response.output_audio_transcript.done") == 0) {
    Serial.printf("[assistant] %s\n", doc["transcript"] | "");
    return;
  }

  if (strcmp(type, "error") == 0) {
    Serial.printf("[ws.error] %s\n", payload);
    setState(DeviceState::Error);
    refreshLed();
  }
}

void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      wsConnected = false;
      portENTER_CRITICAL(&playbackMux);
      playbackQueue.clear();
      portEXIT_CRITICAL(&playbackMux);
      responsePlaybackComplete = false;
      clearOutboundAudio();
      stopAudioI2S();
      listening = false;
      wakeListening = false;
      commandAudioStartsAtMs = 0;
      followUpRequested = false;
      followUpDeadlineMs = 0;
      setState(DeviceState::Error);
      refreshLed();
      Serial.println("[ws] disconnected");
      break;

    case WStype_CONNECTED:
      wsConnected = true;
      requestPlaybackAudio(PLAYBACK_MAX_BUFFER_CHUNKS);
      Serial.printf("[ws] connected to %s\n", payload);
      startWakeListening();
      break;

    case WStype_TEXT:
      handleTextMessage(reinterpret_cast<const char*>(payload));
      break;

    case WStype_BIN: {
      wakeListening = false;
      listening = false;
      commandAudioStartsAtMs = 0;
      followUpDeadlineMs = 0;
      std::vector<uint8_t> chunk(payload, payload + length);
      enqueuePlayback(std::move(chunk));
      setState(DeviceState::Playing);
      refreshLed();
      break;
    }

    default:
      break;
  }
}

void microphoneTask(void*) {
  std::vector<int16_t> samples(MIC_FRAME_SAMPLES);
  uint32_t chunkCount = 0;
  int32_t dcEstimate = 0;
  uint32_t readErrorCount = 0;

  while (true) {
    AudioMode currentAudioMode;
    portENTER_CRITICAL(&audioMux);
    currentAudioMode = audioMode;
    portEXIT_CRITICAL(&audioMux);

    if (!wsConnected || (!listening && !wakeListening) ||
        currentAudioMode != AudioMode::Microphone) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    if (xSemaphoreTake(audioIoMutex, pdMS_TO_TICKS(200)) != pdTRUE) {
      continue;
    }
    portENTER_CRITICAL(&audioMux);
    currentAudioMode = audioMode;
    portEXIT_CRITICAL(&audioMux);
    if ((!listening && !wakeListening) ||
        currentAudioMode != AudioMode::Microphone) {
      xSemaphoreGive(audioIoMutex);
      continue;
    }

    size_t bytesRead = 0;
    esp_err_t err = i2s_read(I2S_PORT_AUDIO, samples.data(),
                             samples.size() * sizeof(int16_t), &bytesRead,
                             pdMS_TO_TICKS(100));
    xSemaphoreGive(audioIoMutex);
    if (err == ESP_ERR_TIMEOUT) {
      continue;
    }
    if (err != ESP_OK || bytesRead == 0) {
      readErrorCount++;
      if (readErrorCount <= 10 || readErrorCount % 25 == 0) {
        Serial.printf("[mic.read] err=%d bytes=%u mode=%d listening=%d\n",
                      static_cast<int>(err), static_cast<unsigned>(bytesRead),
                      static_cast<int>(currentAudioMode), listening ? 1 : 0);
      }
      continue;
    }
    readErrorCount = 0;

    const size_t sampleCount = bytesRead / sizeof(int16_t);
    if (sampleCount == 0) {
      Serial.println("[mic.read] sampleCount=0");
      continue;
    }

    int16_t sampleMin = INT16_MAX;
    int16_t sampleMax = INT16_MIN;
    int64_t deltaSum = 0;
    int16_t lastSample = 0;
    int64_t absSum = 0;

    for (size_t i = 0; i < sampleCount; ++i) {
      const int16_t raw = samples[i];
      sampleMin = std::min(sampleMin, raw);
      sampleMax = std::max(sampleMax, raw);
      if (i > 0) {
        deltaSum += std::abs(static_cast<int32_t>(raw) - lastSample);
      }
      lastSample = raw;
      dcEstimate += (static_cast<int32_t>(raw) - dcEstimate) / 128;
      const int32_t centered = static_cast<int32_t>(raw) - dcEstimate;
      int32_t amplified = centered * MIC_GAIN;
      if (amplified > INT16_MAX) amplified = INT16_MAX;
      if (amplified < INT16_MIN) amplified = INT16_MIN;
      samples[i] = static_cast<int16_t>(amplified);
      absSum += std::abs(static_cast<int32_t>(samples[i]));
    }

    chunkCount++;
    if ((listening && chunkCount % 25 == 0) ||
        (!listening && (chunkCount <= 3 || chunkCount % 250 == 0))) {
      Serial.printf(
          "[mic] min=%d max=%d delta=%ld avg_abs=%ld first=%d,%d,%d,%d\n",
          sampleMin, sampleMax, static_cast<long>(deltaSum),
          static_cast<long>(absSum / sampleCount), samples[0], samples[1], samples[2],
          samples[3]);
    }

    if (!listening && !wakeListening) {
      continue;
    }
    if (listening && commandAudioStartsAtMs != 0 &&
        static_cast<int32_t>(millis() - commandAudioStartsAtMs) < 0) {
      continue;
    }
    const bool commandAudio = listening;
    outboundAudioChunksQueued++;
    if (commandAudio &&
        (outboundAudioChunksQueued <= 3 || outboundAudioChunksQueued % 25 == 0)) {
      Serial.printf("[mic.send] queued binary chunk=%lu bytes=%u\n",
                    static_cast<unsigned long>(outboundAudioChunksQueued),
                    static_cast<unsigned>(sampleCount * sizeof(int16_t)));
    }
    enqueueOutboundAudio(samples.data(), sampleCount, commandAudio);
  }
}

void playbackTask(void*) {
  bool playbackPrimed = false;
  uint32_t lastChunkPlayedMs = 0;

  while (true) {
    size_t queuedChunks = 0;
    portENTER_CRITICAL(&playbackMux);
    queuedChunks = playbackQueue.size();
    portEXIT_CRITICAL(&playbackMux);

    if (!playbackPrimed && queuedChunks > 0 &&
        queuedChunks < PLAYBACK_START_BUFFER_CHUNKS && !responsePlaybackComplete) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }

    std::vector<uint8_t> chunk;
    if (!dequeuePlayback(chunk)) {
      if (playbackPrimed && millis() - lastChunkPlayedMs > PLAYBACK_IDLE_RESET_MS) {
        playbackPrimed = false;
        Serial.println("[playback] buffer drained");
      }
      if (responsePlaybackComplete && !playbackPrimed && !listening && wsConnected) {
        responsePlaybackComplete = false;
        if (followUpRequested) {
          followUpRequested = false;
          startFollowUpListening();
        } else {
          startWakeListening();
        }
      }
      if (!listening && wsConnected && deviceState == DeviceState::Playing) {
        setState(DeviceState::Idle);
        refreshLed();
      }
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    AudioMode currentAudioMode;
    portENTER_CRITICAL(&audioMux);
    currentAudioMode = audioMode;
    portEXIT_CRITICAL(&audioMux);
    if (currentAudioMode != AudioMode::Speaker) {
      if (!configureSpeakerI2S()) {
        vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
    }

    if (!playbackPrimed) {
      playbackPrimed = true;
      Serial.printf("[playback] primed with %u queued chunks\n",
                    static_cast<unsigned>(queuedChunks));
    }

    requestPlaybackAudio(1);

    const size_t monoSamples = chunk.size() / sizeof(int16_t);
    std::vector<int16_t> stereoSamples;
    stereoSamples.reserve(monoSamples * 2);

    auto* mono = reinterpret_cast<const int16_t*>(chunk.data());
    for (size_t i = 0; i < monoSamples; ++i) {
      stereoSamples.push_back(mono[i]);
      stereoSamples.push_back(mono[i]);
    }

    if (xSemaphoreTake(audioIoMutex, pdMS_TO_TICKS(200)) != pdTRUE) {
      continue;
    }
    portENTER_CRITICAL(&audioMux);
    currentAudioMode = audioMode;
    portEXIT_CRITICAL(&audioMux);
    size_t bytesWritten = 0;
    esp_err_t err = ESP_ERR_INVALID_STATE;
    if (currentAudioMode == AudioMode::Speaker) {
      err = i2s_write(I2S_PORT_AUDIO, stereoSamples.data(),
                      stereoSamples.size() * sizeof(int16_t), &bytesWritten,
                      pdMS_TO_TICKS(200));
    }
    xSemaphoreGive(audioIoMutex);
    if (err != ESP_OK) {
      Serial.printf("[playback.write] err=%d bytes=%u\n", static_cast<int>(err),
                    static_cast<unsigned>(bytesWritten));
    }
    lastChunkPlayedMs = millis();
  }
}

void setupButton() {
  pinMode(PIN_BUTTON, INPUT);
}

void processButton() {
  bool pressedNow = digitalRead(PIN_BUTTON) == LOW;
  uint32_t now = millis();

  if (pressedNow != buttonPressed && now - lastButtonChangeMs >= BUTTON_DEBOUNCE_MS) {
    lastButtonChangeMs = now;
    buttonPressed = pressedNow;

    if (buttonPressed) {
      startListening();
    } else {
      stopListeningAndCommit();
    }
  }
}

void processFollowUpTimeout() {
  if (!listening || followUpDeadlineMs == 0 ||
      static_cast<int32_t>(millis() - followUpDeadlineMs) < 0) {
    return;
  }

  listening = false;
  followUpDeadlineMs = 0;
  stopAudioI2S();
  clearOutboundAudio();

  JsonDocument doc;
  doc["type"] = "cancel_follow_up";
  String payload;
  serializeJson(doc, payload);
  enqueueOutbound(std::move(payload));
  Serial.println("[follow-up] timed out; returning to wake word");
  startWakeListening();
}

void connectWebSocket() {
  static String authHeader = String("Authorization: Bearer ") + firmware_config::GATEWAY_TOKEN;
  ws.setExtraHeaders(authHeader.c_str());
  ws.begin(GATEWAY_HOST, GATEWAY_PORT, GATEWAY_PATH);
  ws.onEvent(webSocketEvent);
  ws.setReconnectInterval(WS_RECONNECT_MS);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("[boot] firmware=%s\n", FIRMWARE_VERSION);

  led.begin();
  led.clear();
  led.show();

  audioIoMutex = xSemaphoreCreateMutex();
  if (audioIoMutex == nullptr) {
    Serial.println("[audio] failed to create I2S mutex");
    setState(DeviceState::Error);
    refreshLed();
    return;
  }

  setState(DeviceState::Booting);
  refreshLed();

  setupButton();
  stopAudioI2S();
  connectWiFi();
  connectWebSocket();

  xTaskCreatePinnedToCore(microphoneTask, "microphone_task", 8192, nullptr, 1, nullptr, 0);
  xTaskCreatePinnedToCore(playbackTask, "playback_task", 8192, nullptr, 1, nullptr, 0);
}

void loop() {
  ws.loop();

  std::vector<uint8_t> audioFrame;
  int audioSent = 0;
  while (wsConnected && audioSent < 4 && dequeueOutboundAudio(audioFrame)) {
    ws.sendBIN(audioFrame.data(), audioFrame.size());
    if (audioFrame[0] == AUDIO_FRAME_COMMAND) {
      outboundAudioChunksSent++;
      if (outboundAudioChunksSent <= 3 || outboundAudioChunksSent % 25 == 0) {
        Serial.printf("[ws.send] binary audio chunk=%lu payload=%u\n",
                      static_cast<unsigned long>(outboundAudioChunksSent),
                      static_cast<unsigned>(audioFrame.size() - 1));
      }
    }
    audioSent++;
    delay(1);
  }

  String outbound;
  int sent = 0;
  while (wsConnected && !hasOutboundAudio() && sent < 4 &&
         dequeueOutbound(outbound)) {
    ws.sendTXT(outbound);
    sent++;
    delay(1);
  }

  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  processButton();
  processFollowUpTimeout();
  delay(5);
}
