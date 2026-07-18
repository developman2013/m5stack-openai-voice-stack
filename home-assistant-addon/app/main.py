from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from contextlib import suppress
from collections import deque
from typing import Any, Optional

import httpx
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

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
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8765"))
REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
AUDIO_DELTA_SLICE_BYTES = 1920
MAX_PENDING_AUDIO_EVENTS = 6
MAX_DEVICE_AUDIO_SLOTS = int(os.getenv("MAX_DEVICE_AUDIO_SLOTS", "32"))
LAST_INPUT_PCM_PATH = os.getenv("LAST_INPUT_PCM_PATH", "/tmp/openai-last-input.pcm")

HA_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "get_entity_state",
        "description": "Read the current state and attributes of a Home Assistant entity.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Full Home Assistant entity id, for example light.kitchen.",
                }
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_entities",
        "description": "Search Home Assistant entities by a loose text query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Human description such as 'kitchen light' or 'bedroom speaker'.",
                },
                "domain": {
                    "type": "string",
                    "description": "Optional domain filter such as light, switch, climate, media_player.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "call_home_assistant_service",
        "description": "Call a Home Assistant service to control the home.",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Service domain such as light, switch, media_player, climate, cover.",
                },
                "service": {
                    "type": "string",
                    "description": "Service name such as turn_on, turn_off, set_temperature, play_media.",
                },
                "entity_id": {
                    "type": ["string", "array"],
                    "description": "Optional target entity id or list of entity ids.",
                    "items": {"type": "string"},
                },
                "data": {
                    "type": "object",
                    "description": "Optional service data payload.",
                    "additionalProperties": True,
                },
            },
            "required": ["domain", "service"],
            "additionalProperties": False,
        },
    },
]

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
    }


def ha_headers() -> dict[str, str]:
    if not HOME_ASSISTANT_TOKEN:
        raise HTTPException(status_code=400, detail="home_assistant_token is empty")
    return {
        "Authorization": f"Bearer {HOME_ASSISTANT_TOKEN}",
        "Content-Type": "application/json",
    }


async def ha_get(path: str) -> tuple[int, Any]:
    url = f"{HOME_ASSISTANT_URL}{path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers=ha_headers())
    try:
        body: Any = response.json()
    except Exception:
        body = {"text": response.text}
    return response.status_code, body


async def ha_post(path: str, payload: Optional[dict] = None) -> tuple[int, Any]:
    url = f"{HOME_ASSISTANT_URL}{path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, headers=ha_headers(), json=payload or {})
    try:
        body: Any = response.json()
    except Exception:
        body = {"text": response.text}
    return response.status_code, body


@app.post("/ha/service/{domain}/{service}")
async def call_ha_service(domain: str, service: str, payload: Optional[dict] = None):
    status_code, body = await ha_post(f"/api/services/{domain}/{service}", payload)
    return JSONResponse(status_code=status_code, content=body)


@app.get("/")
async def index():
    return HTMLResponse(PAGE)


async def configure_openai(ws):
    log(f"configuring OpenAI realtime session for model={OPENAI_MODEL} voice={OPENAI_VOICE}")
    await ws.send(
        json.dumps(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "audio": {
                        "input": {
                            "turn_detection": None,
                        },
                    },
                    "instructions": ASSISTANT_INSTRUCTIONS
                    + "\n\nYou can use Home Assistant tools during the conversation."
                    + "\n- Use search_entities if you do not know the exact entity id."
                    + "\n- Use get_entity_state before acting if status matters."
                    + "\n- Use call_home_assistant_service for control actions."
                    + "\n- Ask a short confirmation before ambiguous or impactful actions."
                    + "\n- Reply in Russian by default."
                    + "\n- Keep spoken replies concise, usually one or two short sentences."
                    + "\n- Avoid long introductions and avoid lists unless the user asks for them.",
                    "tools": HA_TOOL_DEFINITIONS,
                    "tool_choice": "auto",
                },
            }
        )
    )


async def run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_entity_state":
        entity_id = str(arguments["entity_id"])
        status_code, body = await ha_get(f"/api/states/{entity_id}")
        return {
            "ok": 200 <= status_code < 300,
            "status_code": status_code,
            "result": body,
        }

    if name == "search_entities":
        query = str(arguments["query"]).strip().lower()
        domain = str(arguments.get("domain", "")).strip().lower()
        status_code, body = await ha_get("/api/states")
        if not (200 <= status_code < 300) or not isinstance(body, list):
            return {"ok": False, "status_code": status_code, "error": body}

        query_parts = [part for part in query.replace("_", " ").split() if part]
        matches: list[dict[str, Any]] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id", ""))
            if domain and not entity_id.startswith(f"{domain}."):
                continue
            friendly_name = str(item.get("attributes", {}).get("friendly_name", ""))
            haystack = f"{entity_id} {friendly_name}".lower().replace("_", " ")
            if all(part in haystack for part in query_parts):
                matches.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "state": item.get("state"),
                    }
                )
        return {"ok": True, "count": len(matches), "matches": matches[:15]}

    if name == "call_home_assistant_service":
        domain = str(arguments["domain"])
        service = str(arguments["service"])
        payload = dict(arguments.get("data") or {})
        entity_id = arguments.get("entity_id")
        if entity_id is not None:
            payload["entity_id"] = entity_id
        status_code, body = await ha_post(f"/api/services/{domain}/{service}", payload)
        return {
            "ok": 200 <= status_code < 300,
            "status_code": status_code,
            "result": body,
        }

    return {"ok": False, "error": f"Unknown tool: {name}"}


@app.websocket("/ws")
async def relay(client_ws: WebSocket):
    if not OPENAI_API_KEY:
        await client_ws.accept()
        await client_ws.send_json(
            {"type": "error", "message": "openai_api_key is empty"}
        )
        await client_ws.close()
        return

    await client_ws.accept()
    client_host = getattr(client_ws.client, "host", "unknown")
    log(f"client connected from {client_host}")
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    try:
        async with websockets.connect(
            REALTIME_URL,
            additional_headers=headers,
            max_size=None,
        ) as openai_ws:
            log("connected to OpenAI realtime")
            tool_lock = asyncio.Lock()
            openai_ready = asyncio.Event()
            audio_chunks = 0
            buffered_audio = bytearray()
            raw_turn_audio = bytearray()
            sent_audio_bytes = 0
            commit_requested = False
            output_audio_chunks = 0
            last_output_audio_delta_at = 0.0
            pending_audio_events: deque[dict[str, Any]] = deque()
            client_send_lock = asyncio.Lock()
            device_audio_slots = 0
            gateway_response_id = 0
            gateway_audio_seq = 0

            await configure_openai(openai_ws)

            async def send_requested_audio(
                slot_count: Optional[int] = None,
                additive: bool = False,
            ) -> None:
                nonlocal device_audio_slots
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
                        event = pending_audio_events.popleft()
                        await client_ws.send_text(json.dumps(event))
                        device_audio_slots -= 1
                        sent += 1

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
                nonlocal sent_audio_bytes, commit_requested
                try:
                    while True:
                        message = await client_ws.receive()
                        if message.get("text") is None:
                            continue
                        payload = json.loads(message["text"])
                        kind = payload.get("type")
                        if not openai_ready.is_set():
                            await openai_ready.wait()
                        if kind == "append_audio":
                            if audio_chunks == 0 and sent_audio_bytes == 0 and not buffered_audio:
                                await openai_ws.send(
                                    json.dumps({"type": "input_audio_buffer.clear"})
                                )
                            audio_chunks += 1
                            audio_b64 = str(payload.get("audio", ""))
                            if audio_b64:
                                decoded = base64.b64decode(audio_b64)
                                buffered_audio.extend(decoded)
                                raw_turn_audio.extend(decoded)
                            if audio_chunks % 25 == 0:
                                log(f"received {audio_chunks} audio chunks from {client_host}")
                            if len(buffered_audio) >= 8192:
                                await flush_buffered_audio()
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
                        elif kind in {"ping", "pong"}:
                            continue
                        else:
                            log(f"forwarding passthrough event from device: {kind}")
                            await openai_ws.send(json.dumps(payload))
                except (WebSocketDisconnect, RuntimeError):
                    pass

            async def openai_to_client():
                nonlocal commit_requested, output_audio_chunks, last_output_audio_delta_at
                nonlocal gateway_response_id, gateway_audio_seq
                async for message in openai_ws:
                    event = json.loads(message)
                    event_type = event.get("type", "unknown")
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
                        if pending_audio_events:
                            log(
                                "dropping unsent audio from previous response: "
                                f"{len(pending_audio_events)} chunks"
                            )
                            pending_audio_events.clear()
                        log(f"gateway response_id={gateway_response_id} started")
                    if event_type == "session.updated":
                        openai_ready.set()
                        log("OpenAI realtime session is ready")
                    if event.get("type") == "response.function_call_arguments.done":
                        async with tool_lock:
                            tool_name = str(event.get("name", ""))
                            call_id = str(event.get("call_id", ""))
                            log(f"running HA tool: {tool_name}")
                            try:
                                arguments = json.loads(event.get("arguments") or "{}")
                            except json.JSONDecodeError:
                                arguments = {}
                            try:
                                tool_result = await run_tool(tool_name, arguments)
                            except HTTPException as exc:
                                tool_result = {"ok": False, "error": exc.detail}
                            await openai_ws.send(
                                json.dumps(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": call_id,
                                            "output": json.dumps(tool_result),
                                        },
                                    }
                                )
                            )
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                    if event_type == "input_audio_buffer.committed" and commit_requested:
                        log("creating response after committed audio buffer")
                        commit_requested = False
                        output_audio_chunks = 0
                        last_output_audio_delta_at = 0.0
                        pending_audio_events.clear()
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                    if (
                        event_type == "error"
                        and event.get("error", {}).get("code") == "input_audio_buffer_commit_empty"
                    ):
                        commit_requested = False
                    if event_type == "response.done":
                        output_audio_chunks = 0
                        last_output_audio_delta_at = 0.0
                    if event_type == "response.output_audio.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            raw = base64.b64decode(delta)
                            if len(raw) > AUDIO_DELTA_SLICE_BYTES:
                                log(f"splitting audio delta of {len(raw)} bytes")
                            for start in range(0, len(raw), AUDIO_DELTA_SLICE_BYTES):
                                pending_audio_events.append(
                                    {
                                        **event,
                                        "response_id": gateway_response_id,
                                        "audio_seq": gateway_audio_seq,
                                        "delta": base64.b64encode(
                                            raw[start:start + AUDIO_DELTA_SLICE_BYTES]
                                        ).decode("ascii"),
                                    }
                                )
                                gateway_audio_seq += 1
                            await send_requested_audio()
                            continue
                    if event_type == "error":
                        log(f"OpenAI error payload: {json.dumps(event)}")
                    async with client_send_lock:
                        await send_event_to_client(client_ws, event)

            tasks = [
                asyncio.create_task(client_to_openai()),
                asyncio.create_task(openai_to_client()),
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
                exc = task.exception()
                if exc:
                    raise exc
    except Exception as exc:
        log(f"relay error for {client_host}: {exc}")
        if client_ws.client_state.name == "CONNECTED":
            await client_ws.send_json(
                {"type": "error", "message": f"OpenAI realtime error: {exc}"}
            )
        raise


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
