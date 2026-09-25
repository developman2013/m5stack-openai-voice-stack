import asyncio
import base64
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.audio import MicrophoneResampler
from app import main


def test_resampling_is_packet_independent_and_preserves_timing():
    pcm = struct.pack('<16001h', *([1200] * 16001))
    whole = MicrophoneResampler().convert(pcm)
    converter = MicrophoneResampler()
    chunks = b''.join(converter.convert(pcm[i:i+137]) for i in range(0, len(pcm), 137))
    assert chunks == whole
    assert len(whole) == 24000 * 2
    assert set(struct.unpack('<24000h', whole)) == {1200}


@pytest.mark.asyncio
async def test_explicit_audio_voice_and_single_response_owner():
    ws = SimpleNamespace(send=AsyncMock())
    await main.configure_openai(ws, [])
    session = json.loads(ws.send.call_args.args[0])['session']
    assert session['audio']['input']['format']['rate'] == 24000
    assert session['audio']['output']['voice'] == main.OPENAI_VOICE
    assert session['audio']['input']['turn_detection'] is None


class Provider:
    def __init__(self):
        self.sent = []
        self.events = asyncio.Queue()
    async def send(self, text):
        self.sent.append(json.loads(text))
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def __aiter__(self): return self
    async def __anext__(self):
        value = await self.events.get()
        if value is None: raise StopAsyncIteration
        return json.dumps(value)


class Device:
    headers = {'authorization': 'Bearer test-token'}
    client = SimpleNamespace(host='test-device')
    client_state = SimpleNamespace(name='CONNECTED')
    def __init__(self):
        self.messages = asyncio.Queue()
        self.text = []
        self.audio = []
    async def accept(self): pass
    async def close(self, **kwargs): pass
    async def receive(self): return await self.messages.get()
    async def send_text(self, text): self.text.append(json.loads(text))
    async def send_json(self, value): self.text.append(value)
    async def send_bytes(self, value): self.audio.append(value)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate(): await asyncio.sleep(.001)


@pytest.mark.asyncio
async def test_two_tools_one_continuation_and_short_audio_delivery(monkeypatch):
    provider, device = Provider(), Device()
    monkeypatch.setattr(main, 'GATEWAY_TOKEN', 'test-token')
    monkeypatch.setattr(main, 'OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(main, 'HA_MCP_ENABLED', False)
    monkeypatch.setattr(main.websockets, 'connect', lambda *a, **k: provider)
    task = asyncio.create_task(main.relay(device))
    await device.messages.put({'text':json.dumps({'type':'begin'})})
    await provider.events.put({'type':'session.updated'})
    await device.messages.put({'text':json.dumps({'type':'audio_request','slots':32})})
    await provider.events.put({'type':'input_audio_buffer.committed','item_id':'turn-1'})
    await provider.events.put({'type':'input_audio_buffer.committed','item_id':'turn-1'})
    await provider.events.put({'type':'response.created'})
    for call_id in ['one','two']:
        await provider.events.put({'type':'response.function_call_arguments.done','call_id':call_id,'name':'expect_follow_up','arguments':'{}'})
    await until(lambda: len(provider.sent) >= 2)
    assert not any(x.get('type') == 'session.updated' for x in device.text)
    assert sum(x['type']=='response.create' for x in provider.sent) == 1
    await provider.events.put({'type':'response.done'})
    await until(lambda: sum(x['type']=='response.create' for x in provider.sent)==2)
    assert sum(x['type']=='conversation.item.create' for x in provider.sent)==2
    await provider.events.put({'type':'response.created'})
    await provider.events.put({'type':'response.output_audio.delta','delta':base64.b64encode(b'\0'*960).decode()})
    await provider.events.put({'type':'response.done'})
    await until(lambda: any(x['type']=='gateway.playback_complete' for x in device.text))
    assert device.audio == [b'\0'*960]
    await provider.events.put(None)
    await task


@pytest.mark.asyncio
async def test_idle_device_does_not_open_paid_session(monkeypatch):
    device = Device()
    monkeypatch.setattr(main, 'GATEWAY_TOKEN', 'test-token')
    monkeypatch.setattr(main, 'OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(main, 'WAKE_WORD_ENABLED', False)
    def forbidden(*args, **kwargs):
        raise AssertionError('OpenAI connection during idle')
    monkeypatch.setattr(main.websockets, 'connect', forbidden)
    await device.messages.put({'bytes': b'\0' + b'\0' * 2048})
    await device.messages.put({'type': 'websocket.disconnect'})
    await main.relay(device)


@pytest.mark.asyncio
async def test_unauthorized_device_cannot_open_session(monkeypatch):
    device = Device()
    device.headers = {}
    monkeypatch.setattr(main, 'GATEWAY_TOKEN', 'test-token')
    await device.messages.put({'text': '{}'})
    device.receive_json = AsyncMock(return_value={'type':'auth','token':'wrong'})
    device.close = AsyncMock()
    await main.relay(device)
    device.close.assert_awaited_once_with(code=1008)


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_silent_reply_finishes_and_stalled_playback_is_bounded(monkeypatch, overflow):
    provider, device = Provider(), Device()
    monkeypatch.setattr(main, 'GATEWAY_TOKEN', 'test-token')
    monkeypatch.setattr(main, 'OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(main, 'HA_MCP_ENABLED', False)
    monkeypatch.setattr(main, 'MAX_AUDIO_BACKLOG_BYTES', 1920)
    monkeypatch.setattr(main.websockets, 'connect', lambda *a, **k: provider)
    task = asyncio.create_task(main.relay(device))
    await device.messages.put({'text': json.dumps({'type': 'begin'})})
    await provider.events.put({'type': 'session.updated'})
    await provider.events.put({'type': 'response.created'})
    if overflow:
        # No device credits: the first delta queues, the next exceeds the cap.
        for _ in range(2):
            await provider.events.put({'type': 'response.output_audio.delta',
                                       'delta': base64.b64encode(bytes(1920)).decode()})
        await asyncio.wait_for(task, 2)
        assert any('playback stalled' in x.get('message', '') for x in device.text)
        assert device.audio == []
    else:
        await provider.events.put({'type': 'response.done'})
        await until(lambda: any(x['type'] == 'gateway.playback_complete' for x in device.text))
        assert device.audio == []
        await provider.events.put(None)
        await task
