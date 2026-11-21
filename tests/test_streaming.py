"""Exercise actual HTTP/WS boundaries against deterministic local Gemini doubles."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

import streaming

PCM = b'\x01\x00\xff\x7f' * 400
SECRET = 'test-key-must-never-be-returned'


def event(kind, **fields):
    return ('event: ' + kind + '\r\ndata: ' + json.dumps({
        'event_type': kind, **fields}, ensure_ascii=False) + '\r\n\r\n').encode()


def audio_event():
    return event('step.delta', delta={'type': 'audio', 'data': base64.b64encode(PCM).decode()})


def completion():
    return event('interaction.completed', interaction={'status': 'completed'})


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'GEMINI_API_KEY': SECRET,
                                           'GEMINI_TIMEOUT_SECONDS': '60'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tts_mode = 'normal'
        self.live_mode = 'normal'
        self.release = asyncio.Event()
        self.upstream_closed = asyncio.Event()
        self.tts_requests = []
        self.live_setups = []
        self.received_pcm = []
        self.live_connections = []
        upstream = web.Application()
        upstream.router.add_post('/tts', self.fake_tts)
        upstream.router.add_get('/live', self.fake_live)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        config = streaming.StreamConfig(
            tts_url=str(self.upstream.make_url('/tts')),
            live_url=str(self.upstream.make_url('/live')),
            setup_timeout=1, final_timeout=.3, idle_timeout=2,
            session_timeout=5, recording_timeout=2, disconnect_poll=.01)
        self.app = web.Application()
        streaming.setup_streaming(self.app, config)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        for ws in self.live_connections:
            await ws.close()
        await self.upstream.close()

    async def fake_tts(self, request):
        self.assertEqual(request.headers['x-goog-api-key'], SECRET)
        self.assertEqual(request.headers['Api-Revision'], '2026-05-20')
        self.tts_requests.append(await request.json())
        if self.tts_mode == 'auth_error':
            return web.json_response({'error': SECRET}, status=403)
        if self.tts_mode == 'redirect':
            raise web.HTTPFound('/must-not-follow?key=' + SECRET)
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        try:
            if self.tts_mode == 'malformed':
                await response.write(b'data: ' + SECRET.encode() + b'\n\n')
            elif self.tts_mode == 'error_event':
                await response.write(event('error', message=SECRET))
            elif self.tts_mode == 'bad_pcm':
                await response.write(event('step.delta', delta={'type': 'audio', 'data': 'AA=='}))
            else:
                raw = audio_event()
                if self.tts_mode == 'fragmented':
                    raw = event('step.start', text='한글') + raw + completion()
                    for offset in range(0, len(raw), 7):
                        await response.write(raw[offset:offset + 7])
                else:
                    await response.write(raw)
                    if self.tts_mode in ('gated', 'disconnect'):
                        while not self.release.is_set():
                            if request.transport is None or request.transport.is_closing():
                                self.upstream_closed.set()
                                return response
                            await asyncio.sleep(.01)
                    if self.tts_mode != 'incomplete':
                        await response.write(completion())
        except ConnectionResetError:
            self.upstream_closed.set()
        except asyncio.CancelledError:
            self.upstream_closed.set()
            raise
        return response

    async def fake_live(self, request):
        self.assertEqual(request.query.get('key'), SECRET)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.live_connections.append(ws)
        connection = len(self.live_connections)
        turn = 0
        try:
            setup = await ws.receive_json()
            self.live_setups.append(setup)
            if self.live_mode == 'setup_error':
                await ws.send_json({'error': {'message': SECRET}})
            else:
                # Google frequently sends JSON in binary WebSocket messages.
                await ws.send_bytes(b'{"setupComplete":{}}')
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                body = json.loads(message.data)['realtimeInput']
                if 'activityStart' in body:
                    turn += 1
                elif 'audio' in body:
                    self.received_pcm.append(base64.b64decode(body['audio']['data']))
                    self.assertEqual(body['audio']['mimeType'], 'audio/pcm;rate=16000')
                    await ws.send_json({'serverContent': {'interimInputTranscription': {'text': '안녕'}}})
                elif 'activityEnd' in body:
                    final = {'serverContent': {'inputTranscription': {'text': f'안녕하세요 {connection}-{turn}'}}}
                    done = {'serverContent': {'generationComplete': True}}
                    if self.live_mode in ('done_first', 'delayed_final', 'missing_final'):
                        await ws.send_json(done)
                        if self.live_mode == 'delayed_final':
                            await asyncio.sleep(.1)
                        if self.live_mode != 'missing_final':
                            await ws.send_json(final)
                    elif self.live_mode == 'empty_final':
                        await ws.send_json({'serverContent': {'inputTranscription': {'text': ''}}})
                        await ws.send_json(done)
                    else:
                        await ws.send_json(final)
                        await ws.send_json(done)
        finally:
            self.upstream_closed.set()
        return ws

    async def collect_tts(self, body=None):
        async with self.client.post('/tts/stream', json=body or {'text': '안녕하세요'}) as response:
            raw = await response.text()
            return response.status, response.headers, raw

    async def connect_live(self):
        ws = await self.client.ws_connect('/stt/live')
        self.assertEqual((await ws.receive_json())['status'], 'connecting')
        ready = await ws.receive_json()
        self.assertEqual(ready['status'], 'ready')
        self.assertGreaterEqual(ready['setup_ms'], 0)
        return ws

    async def turn(self, ws):
        await ws.send_json({'type': 'start'})
        self.assertEqual(await ws.receive_json(), {'type': 'started'})
        await ws.send_bytes(PCM)
        self.assertEqual(await ws.receive_json(), {'type': 'interim', 'text': '안녕'})
        await ws.send_json({'type': 'stop'})
        messages = []
        while True:
            msg = await asyncio.wait_for(ws.receive_json(), 2)
            messages.append(msg)
            if msg['type'] == 'done':
                break
        self.assertEqual(await ws.receive_json(), {'type': 'status', 'status': 'ready'})
        return messages

    async def test_tts_emits_first_audio_before_upstream_completion(self):
        self.tts_mode = 'gated'
        async with self.client.post('/tts/stream', json={'text': '안녕하세요'}) as response:
            self.assertEqual(response.status, 200)
            first = json.loads(await asyncio.wait_for(response.content.readline(), .5))
            self.assertEqual(first, {'type': 'audio', 'data': base64.b64encode(PCM).decode(),
                                    'sample_rate': 24000, 'channels': 1, 'encoding': 'pcm_s16le'})
            self.assertFalse(self.release.is_set())
            self.release.set()
            self.assertEqual(json.loads(await response.content.readline()), {'type': 'done'})
        request = self.tts_requests[0]
        self.assertTrue(request['stream'])
        self.assertFalse(request['store'])
        self.assertEqual(request['response_format']['mime_type'], 'audio/l16')

    async def test_fragmented_sse_and_utf8_are_reassembled(self):
        self.tts_mode = 'fragmented'
        status, _, raw = await self.collect_tts()
        self.assertEqual(status, 200)
        self.assertEqual([json.loads(line)['type'] for line in raw.splitlines()], ['audio', 'done'])

    async def test_tts_missing_completion_never_emits_done(self):
        self.tts_mode = 'incomplete'
        _, _, raw = await self.collect_tts()
        types = [json.loads(line)['type'] for line in raw.splitlines()]
        self.assertEqual(types, ['audio', 'error'])

    async def test_tts_upstream_errors_are_safe_before_and_after_headers(self):
        for mode in ('auth_error', 'redirect', 'malformed', 'error_event', 'bad_pcm'):
            with self.subTest(mode=mode):
                self.tts_mode = mode
                status, _, raw = await self.collect_tts()
                self.assertNotIn(SECRET, raw)
                self.assertNotIn('"done"', raw)
                self.assertIn('error', raw)
                self.assertEqual(status, 502 if mode in ('auth_error', 'redirect') else 200)

    async def test_tts_validation_preserves_batch_specific_options(self):
        invalid = [None, [], {'text': ''}, {'text': 'x' * 301}, {'text': 1},
                   {'text': '안녕', 'speed': .9}, {'text': '안녕', 'speed': True},
                   {'text': '안녕', 'engine': 'baseline'}]
        for data in invalid:
            async with self.client.post('/tts/stream', json=data) as response:
                self.assertEqual(response.status, 400, data)
                self.assertIn('error', await response.json())
        self.assertFalse(self.tts_requests)
        status, _, _ = await self.collect_tts({'text': '안녕', 'speed': 1, 'engine': 'gemini'})
        self.assertEqual(status, 200)

    async def test_tts_request_size_and_origin_rejected(self):
        async with self.client.post('/tts/stream', data=b'x' * (64 * 1024 + 1),
                                    headers={'Content-Type': 'application/json'}) as response:
            self.assertEqual(response.status, 413)
        async with self.client.post('/tts/stream', json={'text': '안녕'},
                                    headers={'Origin': 'https://other.example'}) as response:
            self.assertEqual(response.status, 403)
        self.assertFalse(self.tts_requests)

    async def test_missing_key_and_invalid_timeout_are_safe(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': ''}):
            self.assertEqual((await self.collect_tts())[0], 503)
            async with self.client.get('/stt/live') as response:
                self.assertEqual(response.status, 503)
        with patch.dict(os.environ, {'GEMINI_TIMEOUT_SECONDS': 'nan'}):
            self.assertEqual((await self.collect_tts())[0], 503)

    async def test_tts_disconnect_cancels_upstream_while_it_is_quiet(self):
        self.tts_mode = 'disconnect'
        response = await self.client.post('/tts/stream', json={'text': '안녕'})
        await response.content.readline()
        response.close()
        await asyncio.wait_for(self.upstream_closed.wait(), 1)

    async def test_live_final_then_generation_and_connection_reuse(self):
        ws = await self.connect_live()
        first = await self.turn(ws)
        second = await self.turn(ws)
        self.assertEqual(first[0]['text'], '안녕하세요 1-1')
        self.assertEqual(second[0]['text'], '안녕하세요 1-2')
        self.assertEqual(len(self.live_connections), 1)
        self.assertEqual(self.received_pcm, [PCM, PCM])
        setup = self.live_setups[0]['setup']
        self.assertEqual(setup['inputAudioTranscription'], {'languageCodes': ['ko-KR'], 'mode': 'VERBATIM'})
        self.assertTrue(setup['realtimeInputConfig']['automaticActivityDetection']['disabled'])
        await ws.close()

    async def test_live_generation_before_final_and_delayed_final(self):
        for mode in ('done_first', 'delayed_final'):
            with self.subTest(mode=mode):
                self.live_mode = mode
                ws = await self.connect_live()
                messages = await self.turn(ws)
                self.assertEqual([m['type'] for m in messages], ['final', 'done'])
                await ws.close()

    async def test_live_sessions_do_not_share_transcripts(self):
        a, b = await self.connect_live(), await self.connect_live()
        ra, rb = await asyncio.gather(self.turn(a), self.turn(b))
        self.assertEqual(ra[0]['text'], '안녕하세요 1-1')
        self.assertEqual(rb[0]['text'], '안녕하세요 2-1')
        await a.close()
        await b.close()

    async def test_live_missing_final_errors_without_fabricated_text_or_done(self):
        self.live_mode = 'missing_final'
        ws = await self.connect_live()
        await ws.send_json({'type': 'start'})
        await ws.receive_json()
        await ws.send_json({'type': 'stop'})
        msg = await asyncio.wait_for(ws.receive_json(), 1)
        self.assertEqual(msg['type'], 'error')
        self.assertNotIn(SECRET, json.dumps(msg))
        self.assertIn((await ws.receive()).type, (WSMsgType.CLOSE, WSMsgType.CLOSED))

    async def test_live_explicit_empty_final_is_a_valid_completed_turn(self):
        self.live_mode = 'empty_final'
        ws = await self.connect_live()
        messages = await self.turn(ws)
        self.assertEqual(messages, [{'type': 'final', 'text': ''}, {'type': 'done'}])
        await ws.close()

    async def test_live_invalid_pcm_and_commands_fail_and_close(self):
        for kind in ('odd', 'audio_before_start', 'bad_json', 'unsupported'):
            with self.subTest(kind=kind):
                ws = await self.connect_live()
                if kind == 'odd':
                    await ws.send_json({'type': 'start'})
                    await ws.receive_json()
                    await ws.send_bytes(b'\x00')
                elif kind == 'audio_before_start':
                    await ws.send_bytes(PCM)
                else:
                    await ws.send_str('invalid' if kind == 'bad_json' else '{"type":"other"}')
                self.assertEqual((await ws.receive_json())['type'], 'error')
                self.assertIn((await ws.receive()).type, (WSMsgType.CLOSE, WSMsgType.CLOSED))
                await ws.close()

    async def test_live_cancel_closes_upstream_and_can_open_new_connection(self):
        ws = await self.connect_live()
        await ws.send_json({'type': 'cancel'})
        self.assertIn((await ws.receive()).type, (WSMsgType.CLOSE, WSMsgType.CLOSED))
        await asyncio.wait_for(self.upstream_closed.wait(), 1)
        other = await self.connect_live()
        self.assertEqual(len(self.live_connections), 2)
        await other.close()

    async def test_live_disconnect_closes_upstream(self):
        ws = await self.connect_live()
        await ws.close()
        await asyncio.wait_for(self.upstream_closed.wait(), 1)

    async def test_live_setup_failure_never_leaks_raw_message(self):
        self.live_mode = 'setup_error'
        ws = await self.client.ws_connect('/stt/live')
        self.assertEqual((await ws.receive_json())['status'], 'connecting')
        msg = await ws.receive_json()
        self.assertEqual(msg['type'], 'error')
        self.assertNotIn(SECRET, json.dumps(msg))
        await ws.close()

    async def test_live_connection_error_racing_browser_disconnect_does_not_escape(self):
        # The upstream exception deliberately contains a credential-bearing URL.
        # A second exception while notifying the vanished browser must not escape
        # with that exception as its __context__ into aiohttp's request logger.
        browser = MagicMock()
        browser.closed = False
        browser.send_json = AsyncMock(side_effect=[None, ConnectionResetError("gone")])
        browser.close = AsyncMock()
        async def waiting_messages():
            await asyncio.Event().wait()
            yield None
        browser.__aiter__.side_effect = waiting_messages
        client = MagicMock()
        client.ws_connect.return_value.__aenter__ = AsyncMock(
            side_effect=RuntimeError("wss://upstream.invalid/?key=" + SECRET))
        session = streaming.LiveSession(None, browser, client, streaming.StreamConfig(), SECRET)
        await asyncio.wait_for(session.run(), 1)
        self.assertEqual(browser.send_json.await_count, 2)
        self.assertEqual(browser.send_json.await_args.args[0],
                         {'type': 'error', 'message': 'Gemini Live connection failed.'})
        browser.close.assert_awaited_once()

    async def test_live_requires_upgrade(self):
        async with self.client.get('/stt/live') as response:
            self.assertEqual(response.status, 426)


if __name__ == '__main__':
    unittest.main()
