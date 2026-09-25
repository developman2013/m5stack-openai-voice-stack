from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import struct
import time
from contextlib import suppress
from collections import deque
from typing import Any, Optional

from .audio import MicrophoneResampler
from .ha_mcp import HomeAssistantMcpClient

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from wyoming.audio import AudioChunk, AudioStart
from wyoming.event import async_read_event, async_write_event
from wyoming.wake import Detect, Detection

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-realtime-mini")
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "cedar")
ASSISTANT_INSTRUCTIONS = os.getenv(
    "ASSISTANT_INSTRUCTIONS",
    "You are a concise, helpful smart home voice assistant. "
    "Always answer in Russian unless the user clearly speaks another language. "
    "Keep answers short and natural for spoken conversation.",
)
HOME_ASSISTANT_URL = os.getenv("HOME_ASSISTANT_URL", "http://supervisor/core")
HOME_ASSISTANT_TOKEN = os.getenv("HOME_ASSISTANT_TOKEN", "")
HA_MCP_URL = os.getenv("HA_MCP_URL", f"{HOME_ASSISTANT_URL.rstrip('/')}/api/mcp")
HA_MCP_ENABLED = os.getenv("HA_MCP_ENABLED", "true").lower() == "true"
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8765"))
REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
AUDIO_DELTA_SLICE_BYTES = 1920
# Thirty seconds of 24 kHz mono PCM. Fail closed if playback stops draining.
MAX_AUDIO_BACKLOG_BYTES = 24000 * 2 * 30
SESSION_IDLE_SECONDS = int(os.getenv("SESSION_IDLE_SECONDS", "120"))
CONTINUOUS_CONVERSATION = os.getenv("CONTINUOUS_CONVERSATION", "true").lower() == "true"
MAX_DEVICE_AUDIO_SLOTS = int(os.getenv("MAX_DEVICE_AUDIO_SLOTS", "32"))
LAST_INPUT_PCM_PATH = os.getenv("LAST_INPUT_PCM_PATH", "/tmp/openai-last-input.pcm")
WAKE_WORD_ENABLED = os.getenv("WAKE_WORD_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WAKE_WORD_HOST = os.getenv("WAKE_WORD_HOST", "core-openwakeword")
WAKE_WORD_PORT = int(os.getenv("WAKE_WORD_PORT", "10400"))
WAKE_WORD_NAME = os.getenv("WAKE_WORD_NAME", "hey_jarvis")
WAKE_AUDIO_GAIN = max(1, int(os.getenv("WAKE_AUDIO_GAIN", "8")))
FOLLOW_UP_TIMEOUT_MS = max(1000, int(os.getenv("FOLLOW_UP_TIMEOUT_MS", "5000")))

HA_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "expect_follow_up",
        "description": (
            "Mark that the spoken reply asks the user a real question, requests "
            "clarification, or requires confirmation and should keep listening."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },

]

# The ESP32 WebSockets client has a small inbound-message limit. Realtime
# session and response metadata can contain every MCP schema and exceed it.
# Only these compact control events are useful to the firmware; response audio
# is delivered separately as bounded binary chunks.
DEVICE_EVENT_TYPES = {
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "conversation.item.input_audio_transcription.completed",
    "response.output_audio_transcript.delta",
    "response.output_audio_transcript.done",
    "error",
}

app = FastAPI(title="OpenAI Voice Gateway")

PAGE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OpenAI Voice Gateway</title>
  <style>
    body { font: 16px/1.4 -apple-system, BlinkMacSystemFont, sans-serif; max-width: 860px; margin: 0 auto; padding: 24px; background: #101418; color: #f4f7fb; }
    button { margin-right: 8px; margin-bottom: 8px; padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; }
    #log { white-space: pre-wrap; background: #1a2128; padding: 16px; border-radius: 12px; min-height: 220px; }
  </style>
</head>
<body>
  <h1>OpenAI Voice Gateway</h1>
  <p>Press Connect, allow microphone access, speak, then press Commit if the turn is not auto-detected quickly enough.</p>
  <div>
    <button id="connect">Connect</button>
    <button id="commit">Commit turn</button>
    <button id="disconnect">Disconnect</button>
  </div>
  <div id="log"></div>
  <script>
    const logBox = document.getElementById("log");
    const log = (msg) => { logBox.textContent += msg + "\\n"; logBox.scrollTop = logBox.scrollHeight; };
    let ws, stream, inputCtx, outputCtx, source, processor, playbackCursor = 0;

    function playPcm16(base64) {
      const raw = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
      const pcm = new Int16Array(raw.buffer);
      const audio = new Float32Array(pcm.length);
      for (let i = 0; i < pcm.length; i++) audio[i] = Math.max(-1, Math.min(1, pcm[i] / 32768));
      outputCtx = outputCtx || new AudioContext({ sampleRate: 24000 });
      const buffer = outputCtx.createBuffer(1, audio.length, 24000);
      buffer.copyToChannel(audio, 0);
      const node = outputCtx.createBufferSource();
      node.buffer = buffer;
      node.connect(outputCtx.destination);
      const startAt = Math.max(outputCtx.currentTime, playbackCursor);
      node.start(startAt);
      playbackCursor = startAt + buffer.duration;
      node.onended = () => {
        if (playbackCursor <= outputCtx.currentTime + 0.05) playbackCursor = outputCtx.currentTime;
      };
    }

    async function connect() {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
        log("Error: microphone access requires a secure page (HTTPS or localhost).");
        log("Open this gateway through a secure Home Assistant URL, then try Connect again.");
        return;
      }

      ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
      ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "response.output_audio.delta" && data.delta) playPcm16(data.delta);
        if (data.type === "conversation.item.input_audio_transcription.completed") log("You: " + data.transcript);
        if (data.type === "response.output_audio_transcript.delta" && data.delta) log("Assistant(partial): " + data.delta);
        if (data.type === "response.output_audio_transcript.done" && data.transcript) log("Assistant: " + data.transcript);
        if (data.type === "error") log("Error: " + JSON.stringify(data));
      };
      await new Promise((resolve, reject) => {
        ws.onopen = resolve;
        ws.onerror = reject;
      });

      ws.send(JSON.stringify({ type: "auth", token: window.prompt("Gateway token") || "" }));
      ws.send(JSON.stringify({ type: "begin" }));
      playbackCursor = 0;
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      inputCtx = new AudioContext({ sampleRate: 24000 });
      source = inputCtx.createMediaStreamSource(stream);
      processor = inputCtx.createScriptProcessor(4096, 1, 1);
      source.connect(processor);
      processor.connect(inputCtx.destination);
      processor.onaudioprocess = (event) => {
        if (!ws || ws.readyState !== 1) return;
        const input = event.inputBuffer.getChannelData(0);
        const pcm = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) {
          const s = Math.max(-1, Math.min(1, input[i]));
          pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        const bytes = new Uint8Array(pcm.buffer);
        let binary = "";
        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
        ws.send(JSON.stringify({ type: "append_audio", audio: btoa(binary) }));
      };
      log("Connected");
    }

    function commit() {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "commit" }));
    }

    function disconnect() {
      if (processor) processor.disconnect();
      if (source) source.disconnect();
      if (stream) stream.getTracks().forEach((track) => track.stop());
      if (ws) ws.close();
      playbackCursor = 0;
      log("Disconnected");
    }

    document.getElementById("connect").onclick = connect;
    document.getElementById("commit").onclick = commit;
    document.getElementById("disconnect").onclick = disconnect;
  </script>
</body>
</html>
"""


def log(message: str) -> None:
    print(f"[gateway] {message}", flush=True)


class WakeWordDetector:
    def __init__(self) -> None:
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.result_task: Optional[asyncio.Task] = None
        self.retry_after = 0.0

    async def close(self) -> None:
        if self.result_task is not None:
            self.result_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.result_task
            self.result_task = None
        if self.writer is not None:
            self.writer.close()
            with suppress(Exception):
                await self.writer.wait_closed()
        self.reader = None
        self.writer = None

    async def connect(self) -> bool:
        if self.writer is not None:
            return True
        if time.monotonic() < self.retry_after:
            return False

        try:
            self.reader, self.writer = await asyncio.open_connection(
                WAKE_WORD_HOST,
                WAKE_WORD_PORT,
            )
            await async_write_event(
                Detect(names=[WAKE_WORD_NAME]).event(),
                self.writer,
            )
            await async_write_event(
                AudioStart(rate=16000, width=2, channels=1).event(),
                self.writer,
            )
            self.result_task = asyncio.create_task(async_read_event(self.reader))
            log(
                f"wake word detector armed for {WAKE_WORD_NAME} "
                f"at {WAKE_WORD_HOST}:{WAKE_WORD_PORT}"
            )
            return True
        except Exception as exc:
            log(f"wake word detector connection failed: {exc}")
            self.retry_after = time.monotonic() + 5.0
            await self.close()
            return False

    async def feed(self, audio: bytes) -> Optional[str]:
        if not audio or not await self.connect():
            return None

        assert self.writer is not None
        try:
            await async_write_event(
                AudioChunk(
                    rate=16000,
                    width=2,
                    channels=1,
                    audio=audio,
                ).event(),
                self.writer,
            )
            await asyncio.sleep(0)
            if self.result_task is None or not self.result_task.done():
                return None

            event = self.result_task.result()
            detected_name: Optional[str] = None
            if event is not None and Detection.is_type(event.type):
                detected_name = Detection.from_event(event).name or WAKE_WORD_NAME
            await self.close()
            return detected_name
        except Exception as exc:
            log(f"wake word detector stream failed: {exc}")
            self.retry_after = time.monotonic() + 2.0
            await self.close()
            return None


def analyze_pcm16(raw: bytes) -> dict[str, Any]:
    if len(raw) < 2:
        return {
            "bytes": len(raw),
            "samples": 0,
            "min": 0,
            "max": 0,
            "avg_abs": 0.0,
            "zero_ratio": 1.0,
            "first_samples": [],
        }

    sample_count = len(raw) // 2
    samples = struct.unpack("<" + "h" * sample_count, raw[: sample_count * 2])
    abs_values = [abs(sample) for sample in samples]
    zero_count = sum(1 for sample in samples if sample == 0)
    return {
        "bytes": len(raw),
        "samples": sample_count,
        "min": min(samples),
        "max": max(samples),
        "avg_abs": round(sum(abs_values) / sample_count, 2),
        "zero_ratio": round(zero_count / sample_count, 4),
        "first_samples": list(samples[:16]),
    }


def amplify_pcm16(raw: bytes, gain: int) -> bytes:
    if gain <= 1 or len(raw) < 2:
        return raw
    sample_count = len(raw) // 2
    samples = struct.unpack("<" + "h" * sample_count, raw[: sample_count * 2])
    amplified = [max(-32768, min(32767, sample * gain)) for sample in samples]
    return struct.pack("<" + "h" * sample_count, *amplified)


def decode_audio_base64(value: Any) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError) as exc:
        log(f"ignoring malformed audio chunk: {exc}")
        return b""


async def send_event_to_client(client_ws: WebSocket, event: dict[str, Any]) -> None:
    if event.get("type") == "response.output_audio.delta":
        delta = event.get("delta")
        if isinstance(delta, str):
            raw = base64.b64decode(delta)
            if len(raw) > AUDIO_DELTA_SLICE_BYTES:
                log(f"splitting audio delta of {len(raw)} bytes")
                for start in range(0, len(raw), AUDIO_DELTA_SLICE_BYTES):
                    partial = dict(event)
                    partial["delta"] = base64.b64encode(
                        raw[start:start + AUDIO_DELTA_SLICE_BYTES]
                    ).decode("ascii")
                    await client_ws.send_text(json.dumps(partial))
                return
            if raw:
                partial = dict(event)
                partial["delta"] = base64.b64encode(raw).decode("ascii")
                await client_ws.send_text(json.dumps(partial))
                return

    await client_ws.send_text(json.dumps(event))


@app.get("/health")
async def health():
    return {
        "ok": True,
        "model": OPENAI_MODEL,
        "voice": OPENAI_VOICE,
        "ha_url": HOME_ASSISTANT_URL,
        "wake_word": {
            "enabled": WAKE_WORD_ENABLED,
            "name": WAKE_WORD_NAME,
            "host": WAKE_WORD_HOST,
            "port": WAKE_WORD_PORT,
            "audio_gain": WAKE_AUDIO_GAIN,
        },
        "follow_up_timeout_ms": FOLLOW_UP_TIMEOUT_MS,
    }


@app.get("/")
async def index():
    return HTMLResponse(PAGE)


async def configure_openai(ws, tools=None, tool_prompt=""):
    log(f"configuring OpenAI realtime session for model={OPENAI_MODEL} voice={OPENAI_VOICE}")
    await ws.send(
        json.dumps(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "output_modalities": ["audio"],
                    "audio": {
                        "output": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "voice": OPENAI_VOICE,
                        },
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            # The first turn is push-to-talk. The Realtime API
                            # requires VAD to be disabled for a manual commit.
                            "turn_detection": None,
                        },
                    },
                    "instructions": ASSISTANT_INSTRUCTIONS + "\n" + tool_prompt
                    + "\n\nYou can use Home Assistant tools during the conversation."
                    + "\n- Use the provided Home Assistant tools for home state and control."
                    + "\n- Ask a short confirmation before ambiguous or impactful actions."
                    + "\n- Call expect_follow_up before asking a real question, requesting clarification, or asking for confirmation."
                    + "\n- Do not call expect_follow_up for rhetorical questions or when no user reply is needed."
                    + "\n- Reply in Russian by default."
                    + "\n- Keep spoken replies concise, usually one or two short sentences."
                    + "\n- Avoid long introductions and avoid lists unless the user asks for them.",
                    "tools": HA_TOOL_DEFINITIONS + (tools or []),
                    "tool_choice": "auto",
                },
            }
        )
    )


@app.websocket("/ws")
async def relay(client_ws: WebSocket):
    await client_ws.accept()
    authorized = bool(GATEWAY_TOKEN) and client_ws.headers.get("authorization") == f"Bearer {GATEWAY_TOKEN}"
    if not authorized:
        try:
            auth = await asyncio.wait_for(client_ws.receive_json(), timeout=5)
            import secrets
            authorized = bool(GATEWAY_TOKEN) and auth.get("type") == "auth" and secrets.compare_digest(str(auth.get("token", "")), GATEWAY_TOKEN)
        except Exception:
            authorized = False
    if not authorized:
        await client_ws.close(code=1008)
        return
    if not OPENAI_API_KEY:
        await client_ws.send_json(
            {"type": "error", "message": "openai_api_key is empty"}
        )
        await client_ws.close()
        return

    client_host = getattr(client_ws.client, "host", "unknown")
    log(f"client connected from {client_host}")
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    ha_mcp = None
    idle_detector = WakeWordDetector()
    initial_message = None
    initial_slots = 0
    last_activity = time.monotonic()
    try:
        # Idle microphone data stays local. No OpenAI session until activation.
        while True:
            message = await client_ws.receive()
            if message.get("type") == "websocket.disconnect":
                return
            binary = message.get("bytes")
            if binary:
                if binary[0] == 1:
                    initial_message = message
                    break
                if binary[0] == 0 and WAKE_WORD_ENABLED:
                    detected = await idle_detector.feed(amplify_pcm16(binary[1:], WAKE_AUDIO_GAIN))
                    if detected:
                        await client_ws.send_json({"type": "wake_word.detected", "name": detected})
                        break
                continue
            try:
                event = json.loads(message.get("text") or "{}")
            except json.JSONDecodeError:
                continue
            if event.get("type") == "audio_request":
                initial_slots = min(MAX_DEVICE_AUDIO_SLOTS, initial_slots + max(0, int(event.get("slots", 0))))
            elif event.get("type") in {"begin", "append_audio"}:
                initial_message = message if event["type"] == "append_audio" else None
                break
        await idle_detector.close()
        last_activity = time.monotonic()
        mcp_tools = []
        mcp_prompt = ""
        if HA_MCP_ENABLED:
            ha_mcp = HomeAssistantMcpClient(HA_MCP_URL, HOME_ASSISTANT_TOKEN)
            await ha_mcp.connect()
            mcp_tools = await ha_mcp.get_openai_tools()
            with suppress(Exception):
                mcp_prompt = await ha_mcp.get_prompt()
        async with websockets.connect(
            REALTIME_URL,
            additional_headers=headers,
            max_size=None,
        ) as openai_ws:
            log("connected to OpenAI realtime")
            openai_ready = asyncio.Event()
            audio_chunks = 0
            buffered_audio = bytearray()
            raw_turn_audio = bytearray()
            sent_audio_bytes = 0
            commit_requested = False
            output_audio_chunks = 0
            last_output_audio_delta_at = 0.0
            pending_audio_events: deque[bytes] = deque()
            client_send_lock = asyncio.Lock()
            device_audio_slots = initial_slots
            gateway_response_id = 0
            gateway_audio_seq = 0
            response_audio_done = False
            response_has_audio = False
            playback_complete_sent = False
            follow_up_requested = False
            wake_detector = WakeWordDetector()
            microphone_resampler = MicrophoneResampler()
            pending_tool_calls = {}
            seen_commits = set()
            browser_client = False

            await configure_openai(openai_ws, mcp_tools, mcp_prompt)

            async def send_requested_audio(
                slot_count: Optional[int] = None,
                additive: bool = False,
            ) -> None:
                nonlocal device_audio_slots, playback_complete_sent
                nonlocal follow_up_requested
                async with client_send_lock:
                    if slot_count is not None:
                        if additive:
                            device_audio_slots = min(
                                MAX_DEVICE_AUDIO_SLOTS,
                                device_audio_slots + max(0, slot_count),
                            )
                        else:
                            device_audio_slots = min(
                                MAX_DEVICE_AUDIO_SLOTS,
                                max(0, slot_count),
                            )

                    sent = 0
                    while device_audio_slots > 0 and pending_audio_events:
                        audio_chunk = pending_audio_events.popleft()
                        await client_ws.send_bytes(audio_chunk)
                        device_audio_slots -= 1
                        sent += 1

                    if (
                        response_audio_done
                        and not pending_audio_events
                        and not playback_complete_sent
                    ):
                        should_follow_up = follow_up_requested or CONTINUOUS_CONVERSATION
                        if should_follow_up:
                            # Follow-up turns are hands-free, so enable server
                            # VAD only after the push-to-talk reply is delivered.
                            await openai_ws.send(
                                json.dumps(
                                    {
                                        "type": "session.update",
                                        "session": {
                                            "type": "realtime",
                                            "audio": {
                                                "input": {
                                                    "turn_detection": {
                                                        "type": "server_vad",
                                                        "threshold": 0.5,
                                                        "prefix_padding_ms": 300,
                                                        "silence_duration_ms": 900,
                                                        "create_response": False,
                                                        "interrupt_response": False,
                                                    }
                                                }
                                            },
                                        },
                                    }
                                )
                            )
                        await client_ws.send_text(
                            json.dumps(
                                {
                                    "type": "gateway.playback_complete",
                                    "follow_up": should_follow_up,
                                    "timeout_ms": FOLLOW_UP_TIMEOUT_MS,
                                }
                            )
                        )
                        playback_complete_sent = True
                        follow_up_requested = False
                        log("all response audio delivered to device")

                if sent:
                    log(
                        f"sent {sent} audio chunks to device "
                        f"({device_audio_slots} slots remaining)"
                    )
                if pending_audio_events and device_audio_slots == 0:
                    log(
                        "gateway audio backlog: "
                        f"{len(pending_audio_events)} chunks waiting on device buffer"
                    )

            async def flush_buffered_audio() -> None:
                nonlocal buffered_audio, sent_audio_bytes
                if not buffered_audio:
                    return
                payload = base64.b64encode(buffered_audio).decode("ascii")
                chunk_bytes = len(buffered_audio)
                buffered_audio.clear()
                sent_audio_bytes += chunk_bytes
                log(f"forwarding {chunk_bytes} bytes to OpenAI (total this turn: {sent_audio_bytes})")
                await openai_ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": payload,
                        }
                    )
                )

            async def client_to_openai():
                nonlocal audio_chunks, buffered_audio, raw_turn_audio
                nonlocal browser_client, initial_message, last_activity
                nonlocal sent_audio_bytes, commit_requested
                nonlocal follow_up_requested
                try:
                    while True:
                        if initial_message is not None:
                            message, initial_message = initial_message, None
                        else:
                            message = await client_ws.receive()
                        if message.get("type") == "websocket.disconnect":
                            log(
                                "device websocket disconnected: "
                                f"code={message.get('code')} reason={message.get('reason', '')}"
                            )
                            return
                        binary_message = message.get("bytes")
                        if binary_message is not None:
                            if len(binary_message) < 2:
                                continue
                            frame_type = binary_message[0]
                            audio_data = binary_message[1:]
                            if frame_type == 1:
                                audio_data = microphone_resampler.convert(audio_data)
                            if frame_type == 1:
                                payload = {
                                    "type": "append_audio",
                                    "audio_bytes": audio_data,
                                }
                            elif frame_type == 0:
                                payload = {
                                    "type": "wake_audio",
                                    "audio_bytes": audio_data,
                                }
                            else:
                                log(f"ignoring unknown binary frame type: {frame_type}")
                                continue
                        elif message.get("text") is None:
                            continue
                        else:
                            try:
                                payload = json.loads(message["text"])
                            except json.JSONDecodeError as exc:
                                log(f"ignoring malformed client message: {exc}")
                                continue
                        kind = payload.get("type")
                        if kind == "append_audio" and binary_message is None:
                            browser_client = True
                        if not openai_ready.is_set():
                            await asyncio.wait_for(openai_ready.wait(), timeout=15)
                        if kind == "append_audio":
                            last_activity = time.monotonic()
                            if audio_chunks == 0 and sent_audio_bytes == 0 and not buffered_audio:
                                follow_up_requested = False
                                await openai_ws.send(
                                    json.dumps({"type": "input_audio_buffer.clear"})
                                )
                            audio_chunks += 1
                            decoded = payload.get("audio_bytes")
                            if decoded is None:
                                audio_b64 = str(payload.get("audio", ""))
                                decoded = decode_audio_base64(audio_b64)
                            if decoded:
                                buffered_audio.extend(decoded)
                                raw_turn_audio.extend(decoded)
                            if audio_chunks % 25 == 0:
                                log(f"received {audio_chunks} audio chunks from {client_host}")
                            if len(buffered_audio) >= 8192:
                                await flush_buffered_audio()
                        elif kind == "wake_audio":
                            if not WAKE_WORD_ENABLED:
                                continue
                            wake_audio = payload.get("audio_bytes")
                            if wake_audio is None:
                                audio_b64 = str(payload.get("audio", ""))
                                wake_audio = decode_audio_base64(audio_b64)
                            if not wake_audio:
                                continue
                            detected_name = await wake_detector.feed(
                                amplify_pcm16(
                                    wake_audio,
                                    WAKE_AUDIO_GAIN,
                                )
                            )
                            if detected_name:
                                log(
                                    f"wake word detected for {client_host}: "
                                    f"{detected_name}"
                                )
                                async with client_send_lock:
                                    await client_ws.send_text(
                                        json.dumps(
                                            {
                                                "type": "wake_word.detected",
                                                "name": detected_name,
                                            }
                                        )
                                    )
                        elif kind == "commit":
                            log(f"commit received from {client_host} after {audio_chunks} audio chunks")
                            await flush_buffered_audio()
                            if sent_audio_bytes == 0:
                                log("ignoring empty commit")
                                continue
                            try:
                                with open(LAST_INPUT_PCM_PATH, "wb") as pcm_file:
                                    pcm_file.write(raw_turn_audio)
                            except OSError as exc:
                                log(f"failed to persist turn audio: {exc}")
                            stats = analyze_pcm16(bytes(raw_turn_audio))
                            log(
                                "turn audio stats: "
                                + json.dumps(stats, ensure_ascii=True)
                            )
                            commit_requested = True
                            await asyncio.sleep(0.08)
                            await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            sent_audio_bytes = 0
                            raw_turn_audio.clear()
                            audio_chunks = 0
                        elif kind in {"cancel_follow_up", "end_conversation"}:
                            await client_ws.close(code=1000)
                            return
                        elif kind == "discard_input":
                            buffered_audio.clear()
                            raw_turn_audio.clear()
                            sent_audio_bytes = 0
                            audio_chunks = 0
                            commit_requested = False
                            await openai_ws.send(
                                json.dumps({"type": "input_audio_buffer.clear"})
                            )
                            log(f"follow-up window expired for {client_host}")
                        elif kind == "audio_request":
                            free_slots = int(payload.get("slots", 0) or 0)
                            requested_slots = max(0, free_slots)
                            if requested_slots == 0 or requested_slots >= 4:
                                log(f"device requested audio slots: {requested_slots}")
                            await send_requested_audio(requested_slots, additive=True)
                            continue
                        elif kind == "playback_status":
                            free_slots = int(payload.get("free_slots", 0) or 0)
                            await send_requested_audio(max(0, free_slots))
                            continue
                        elif kind == "response.create":
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                        elif kind in {"ping", "pong", "begin"}:
                            continue
                        else:
                            log(f"ignoring unsupported client event: {kind}")
                except (WebSocketDisconnect, RuntimeError):
                    pass
                finally:
                    await wake_detector.close()

            async def openai_to_client():
                nonlocal commit_requested, output_audio_chunks, last_output_audio_delta_at
                nonlocal gateway_response_id, gateway_audio_seq
                nonlocal audio_chunks, buffered_audio, raw_turn_audio, sent_audio_bytes
                nonlocal response_audio_done, playback_complete_sent
                nonlocal response_has_audio
                nonlocal follow_up_requested, last_activity
                async for message in openai_ws:
                    event = json.loads(message)
                    event_type = event.get("type", "unknown")
                    last_activity = time.monotonic()
                    if event_type in {
                        "session.updated",
                        "input_audio_buffer.committed",
                        "input_audio_buffer.speech_started",
                        "input_audio_buffer.speech_stopped",
                        "response.created",
                        "response.done",
                        "response.output_audio.delta",
                        "response.output_audio.done",
                        "response.output_audio_transcript.done",
                        "error",
                    }:
                        log(f"OpenAI event: {event_type}")
                    if event_type == "response.output_audio.delta":
                        now = time.monotonic()
                        output_audio_chunks += 1
                        if last_output_audio_delta_at:
                            gap_ms = (now - last_output_audio_delta_at) * 1000
                            if gap_ms > 120:
                                log(
                                    "OpenAI audio delta gap: "
                                    f"{gap_ms:.1f} ms before chunk {output_audio_chunks}"
                                )
                        last_output_audio_delta_at = now
                    if event_type == "response.created":
                        gateway_response_id += 1
                        gateway_audio_seq = 0
                        response_audio_done = False
                        response_has_audio = False
                        playback_complete_sent = False
                        log(f"gateway response_id={gateway_response_id} started")
                    if event_type == "session.updated":
                        openai_ready.set()
                        log("OpenAI realtime session is ready")
                    if event_type == "response.function_call_arguments.done":
                        pending_tool_calls[event["call_id"]] = event
                    if event_type == "response.done" and pending_tool_calls:
                        # Wait until the complete response has ended before continuing.
                        # Several tool calls in one response produce one continuation.
                        calls = list(pending_tool_calls.values())
                        pending_tool_calls.clear()
                        for call in calls:
                            name = call.get("name", "")
                            try:
                                if name == "expect_follow_up":
                                    follow_up_requested = True
                                    result = {"ok": True}
                                else:
                                    result = await ha_mcp.call_openai_tool(name, json.loads(call.get("arguments") or "{}")) if ha_mcp else {"ok": False, "error": "MCP disabled"}
                            except Exception:
                                log(f"tool failed: {name}")
                                result = {"ok": False, "error": "Tool execution failed"}
                            await openai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {"type": "function_call_output", "call_id": call["call_id"],
                                         "output": json.dumps(result)},
                            }))
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                        continue
                    if event_type == "input_audio_buffer.committed":
                        item_id = event.get("item_id")
                        if item_id and item_id not in seen_commits:
                            seen_commits.add(item_id)
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                        commit_requested = False
                        buffered_audio.clear()
                        raw_turn_audio.clear()
                        sent_audio_bytes = 0
                        audio_chunks = 0
                        microphone_resampler.reset()
                    if (
                        event_type == "error"
                        and event.get("error", {}).get("code") == "input_audio_buffer_commit_empty"
                    ):
                        commit_requested = False
                        response_audio_done = True
                        await send_requested_audio()
                        log("ignored an empty input commit and returned device to idle")
                        continue
                    if event_type == "response.done":
                        output_audio_chunks = 0
                        last_output_audio_delta_at = 0.0
                        response_audio_done = True
                    if event_type == "response.output_audio.delta":
                        response_has_audio = True
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            raw = base64.b64decode(delta)
                            if len(raw) > AUDIO_DELTA_SLICE_BYTES:
                                log(f"splitting audio delta of {len(raw)} bytes")
                            if browser_client:
                                await send_event_to_client(client_ws, event)
                                continue
                            if sum(map(len, pending_audio_events)) + len(raw) > MAX_AUDIO_BACKLOG_BYTES:
                                raise RuntimeError("Device playback stalled: audio backlog exceeded")
                            for start in range(0, len(raw), AUDIO_DELTA_SLICE_BYTES):
                                pending_audio_events.append(
                                    raw[start:start + AUDIO_DELTA_SLICE_BYTES]
                                )
                                gateway_audio_seq += 1
                            await send_requested_audio()
                            continue
                    if event_type == "response.output_audio_transcript.done":
                        transcript = str(event.get("transcript", "")).strip()
                        if transcript.endswith("?"):
                            follow_up_requested = True
                            log("follow-up enabled by assistant question")
                    if event_type == "error":
                        log(f"OpenAI error payload: {json.dumps(event)}")
                    if browser_client or event_type in DEVICE_EVENT_TYPES:
                        async with client_send_lock:
                            await send_event_to_client(client_ws, event)
                    if event_type == "response.done":
                        await send_requested_audio()
                log(
                    "OpenAI websocket ended: "
                    f"code={openai_ws.close_code} reason={openai_ws.close_reason}"
                )

            async def expire_session():
                started = time.monotonic()
                while True:
                    await asyncio.sleep(1)
                    now = time.monotonic()
                    if now - last_activity > SESSION_IDLE_SECONDS or now - started > 3300:
                        await client_ws.close(code=1000)
                        return

            tasks = [
                asyncio.create_task(client_to_openai(), name="device-input"),
                asyncio.create_task(openai_to_client(), name="openai-output"),
                asyncio.create_task(expire_session(), name="session-expiry"),
            ]
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                log(f"relay task ended: {task.get_name()} cancelled={task.cancelled()}")
                exc = task.exception()
                if exc:
                    raise exc
    except Exception as exc:
        log(f"relay error for {client_host}: {exc}")
        if client_ws.client_state.name == "CONNECTED":
            await client_ws.send_json(
                {"type": "error", "message": f"OpenAI realtime error: {exc}"}
            )
    finally:
        await idle_detector.close()
        if ha_mcp is not None:
            await ha_mcp.close()
        with suppress(Exception):
            await client_ws.close()


if __name__ == "__main__":
    import uvicorn

    log(f"starting gateway on port {LISTEN_PORT}")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=LISTEN_PORT,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
