"""Bounded Gemini streaming TTS and per-client Live STT bridges.

Only this server sees the API key. Clients send/receive PCM, not Gemini messages.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
from dataclasses import dataclass
import json
import math
import os
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web, WSMsgType

MAX_JSON_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_PCM_FRAME_BYTES = 64 * 1024
INPUT_RATE = 16000
OUTPUT_RATE = 24000


@dataclass(frozen=True)
class StreamConfig:
    tts_url: str = "https://generativelanguage.googleapis.com/v1beta/interactions"
    live_url: str = ("wss://generativelanguage.googleapis.com/ws/"
                     "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
    setup_timeout: float = 20
    final_timeout: float = 20
    idle_timeout: float = 90
    session_timeout: float = 600
    recording_timeout: float = 120
    disconnect_poll: float = 0.1


CLIENT_SESSION_KEY = web.AppKey("speech_http_client", aiohttp.ClientSession)
STREAM_CONFIG_KEY = web.AppKey("speech_stream_config", StreamConfig)
LIVE_SESSIONS_KEY = web.AppKey("speech_live_sessions", set)


class StreamError(Exception):
    """A bounded error message safe for clients and logs."""


def _json(value, status=200):
    return web.json_response(value, status=status, headers={"Cache-Control": "no-store"})


def _key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise StreamError("GEMINI_API_KEY is not configured on the server.")
    if not key.isascii() or any(ord(c) < 33 for c in key):
        raise StreamError("GEMINI_API_KEY is invalid.")
    return key


def _timeout():
    try:
        value = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "60"))
        if not math.isfinite(value) or not 1 <= value <= 300:
            raise ValueError
        return value
    except ValueError:
        raise StreamError("GEMINI_TIMEOUT_SECONDS must be between 1 and 300.") from None


def _status_error(status):
    if status in (401, 403):
        return "Gemini authentication or access failed."
    if status == 429:
        return "Gemini quota or rate limit exceeded."
    return "Gemini connection or request failed."


def _same_origin(request):
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # Native clients do not send an Origin header.
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == request.host
    except ValueError:
        return False


async def _read_tts_input(request):
    if request.content_type != "application/json":
        raise web.HTTPBadRequest(text="Content-Type must be application/json.")
    if request.content_length is not None and request.content_length > MAX_JSON_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_JSON_BYTES, actual_size=request.content_length)
    body = bytearray()
    async with asyncio.timeout(15):
        async for chunk in request.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > MAX_JSON_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_JSON_BYTES, actual_size=len(body))
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise StreamError("Invalid JSON request.") from None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise StreamError("text must be a string.")
    text = data["text"].strip()
    if not 1 <= len(text) <= 300:
        raise StreamError("text must contain 1 to 300 characters.")
    if data.get("engine", "gemini") != "gemini":
        raise StreamError("Streaming supports only engine=gemini; use /tts for baseline.")
    speed = data.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed != 1.0:
        raise StreamError("Streaming supports speed=1.0 only; adjust playback speed on the client.")
    return text


async def _sse_events(content):
    """Parse SSE across arbitrary UTF-8/chunk boundaries, with bounded buffering."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer, event, lines = "", "", []
    wire_bytes, event_bytes = 0, 0
    async for chunk in content.iter_any():
        wire_bytes += len(chunk)
        if wire_bytes > MAX_OUTPUT_BYTES * 2:
            raise StreamError("Gemini stream exceeded the size limit.")
        buffer += decoder.decode(chunk)
        if len(buffer) > MAX_EVENT_BYTES:
            raise StreamError("Gemini stream event exceeded the size limit.")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            event_bytes += len(line)
            if event_bytes > MAX_EVENT_BYTES:
                raise StreamError("Gemini stream event exceeded the size limit.")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
            elif not line:
                if lines:
                    yield event, json.loads("\n".join(lines))
                event, lines, event_bytes = "", [], 0
    buffer += decoder.decode(final=True)
    if buffer.startswith("data:"):
        lines.append(buffer[5:].removeprefix(" ").rstrip("\r"))
    if lines:
        yield event, json.loads("\n".join(lines))


async def _write_event(response, value):
    await response.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))


async def _wait_for_disconnect(request, poll):
    while request.transport is not None and not request.transport.is_closing():
        await asyncio.sleep(poll)


async def stream_tts(request):
    if not _same_origin(request):
        return _json({"error": "Cross-origin requests are not allowed."}, 403)
    try:
        text = await _read_tts_input(request)
    except web.HTTPRequestEntityTooLarge:
        return _json({"error": "Request body is too large."}, 413)
    except (web.HTTPBadRequest, StreamError) as exc:
        return _json({"error": exc.text if isinstance(exc, web.HTTPBadRequest) else str(exc)}, 400)
    except asyncio.TimeoutError:
        return _json({"error": "Request body timed out."}, 408)
    try:
        key, timeout = _key(), _timeout()
    except StreamError as exc:
        return _json({"error": str(exc)}, 503)
    config, client = request.app[STREAM_CONFIG_KEY], request.app[CLIENT_SESSION_KEY]
    payload = {
        "model": os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"),
        "store": False, "stream": True,
        "input": [{"type": "user_input", "content": [{
            "type": "text", "text": text, "annotations": [{
                "type": "speech_metadata", "style": "자연스럽고 또렷한 한국어로 읽어 주세요."}]}]}],
        "response_format": {"type": "audio", "mime_type": "audio/l16"},
        "generation_config": {"speech_config": [{"voice": os.environ.get("GEMINI_VOICE", "Kore")}]},
    }
    response = web.StreamResponse(headers={
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no",
        "X-TTS-Engine": "gemini", "X-Content-Type-Options": "nosniff",
    })
    watcher = pump = None
    try:
        async with client.post(config.tts_url, json=payload, headers={
            "x-goog-api-key": key, "Api-Revision": "2026-05-20", "Accept": "text/event-stream",
        }, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as upstream:
            if upstream.status != 200:
                return _json({"error": _status_error(upstream.status)}, 502)
            if upstream.content_type != "text/event-stream":
                return _json({"error": "Gemini returned an unsupported stream format."}, 502)
            await response.prepare(request)

            async def forward():
                audio_bytes = 0
                async for name, data in _sse_events(upstream.content):
                    if not isinstance(data, dict):
                        raise StreamError("Gemini returned an invalid stream event.")
                    kind = data.get("event_type", name)
                    if kind in ("error", "interaction.failed", "interaction.cancelled"):
                        raise StreamError("Gemini could not complete speech generation.")
                    delta = data.get("delta", {})
                    if kind == "step.delta" and isinstance(delta, dict) and delta.get("type") == "audio":
                        encoded = delta.get("data")
                        if not isinstance(encoded, str):
                            raise StreamError("Gemini returned invalid audio data.")
                        pcm = base64.b64decode(encoded, validate=True)
                        if not pcm or len(pcm) % 2:
                            raise StreamError("Gemini returned invalid PCM16 audio.")
                        audio_bytes += len(pcm)
                        if audio_bytes > MAX_OUTPUT_BYTES:
                            raise StreamError("Gemini audio exceeded the size limit.")
                        await _write_event(response, {"type": "audio", "data": encoded,
                            "sample_rate": OUTPUT_RATE, "channels": 1, "encoding": "pcm_s16le"})
                    elif kind == "interaction.completed":
                        if data.get("interaction", {}).get("status") != "completed" or not audio_bytes:
                            raise StreamError("Gemini did not return a complete audio stream.")
                        await _write_event(response, {"type": "done"})
                        return
                raise StreamError("Gemini audio stream ended before completion.")

            pump = asyncio.create_task(forward())
            watcher = asyncio.create_task(_wait_for_disconnect(request, config.disconnect_poll))
            done, _ = await asyncio.wait((pump, watcher), return_when=asyncio.FIRST_COMPLETED)
            if pump in done:
                await pump
            else:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)) and (
                request.transport is None or request.transport.is_closing()):
            return response
        message = str(exc) if isinstance(exc, StreamError) else (
            "Gemini speech generation timed out." if isinstance(exc, asyncio.TimeoutError)
            else "Gemini speech generation failed.")
        if not response.prepared:
            return _json({"error": message}, 502)
        try:
            await _write_event(response, {"type": "error", "message": message})
        except (ConnectionError, RuntimeError):
            pass
    finally:
        for task in (watcher, pump):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (watcher, pump) if task), return_exceptions=True)
    return response


class LiveSession:
    def __init__(self, request, browser, client, config, key):
        self.request, self.browser, self.client, self.config, self.key = request, browser, client, config, key
        self.upstream = None
        self.state = "connecting"
        self.began = time.monotonic()
        self.deadline = self.began + config.setup_timeout
        self.turn_bytes = 0
        self.has_final = self.generated = False
        self.tasks = []

    async def send(self, value):
        # A browser can disconnect between the closed check and write. Never
        # allow that race to re-raise a key-bearing upstream exception context.
        if not self.browser.closed:
            try:
                await self.browser.send_json(value)
            except (ConnectionError, aiohttp.ClientConnectionError, RuntimeError):
                pass

    async def finish_turn(self):
        if self.state == "finishing" and self.generated and self.has_final:
            self.state = "idle"
            self.deadline = time.monotonic() + self.config.idle_timeout
            await self.send({"type": "done"})
            await self.send({"type": "status", "status": "ready"})

    async def browser_messages(self):
        async for message in self.browser:
            if message.type == WSMsgType.BINARY:
                data = message.data
                if self.state != "active":
                    raise StreamError("Send start and wait for started before sending audio.")
                if not data or len(data) > MAX_PCM_FRAME_BYTES or len(data) % 2:
                    raise StreamError("Audio must be non-empty PCM16 frames of at most 64 KiB.")
                self.turn_bytes += len(data)
                if self.turn_bytes > INPUT_RATE * 2 * self.config.recording_timeout:
                    raise StreamError("Audio exceeds the 120 second recording limit.")
                await self.upstream.send_json({"realtimeInput": {"audio": {
                    "data": base64.b64encode(data).decode("ascii"), "mimeType": "audio/pcm;rate=16000"}}})
            elif message.type == WSMsgType.TEXT:
                if len(message.data) > 1024:
                    raise StreamError("Control message is too large.")
                try:
                    data = json.loads(message.data)
                    kind = data.get("type") if isinstance(data, dict) else None
                except ValueError:
                    raise StreamError("Invalid JSON control message.") from None
                if kind == "cancel":
                    return
                if kind == "start":
                    if self.state != "idle":
                        raise StreamError("Wait for ready before starting another turn.")
                    self.state = "active"
                    self.has_final = self.generated = False
                    self.turn_bytes = 0
                    self.deadline = time.monotonic() + self.config.recording_timeout
                    await self.upstream.send_json({"realtimeInput": {"activityStart": {}}})
                    await self.send({"type": "started"})
                elif kind == "stop":
                    if self.state != "active":
                        raise StreamError("stop requires an active recording.")
                    self.state = "finishing"
                    self.deadline = time.monotonic() + self.config.final_timeout
                    await self.upstream.send_json({"realtimeInput": {"activityEnd": {}}})
                    await self.finish_turn()
                else:
                    raise StreamError("Unsupported control message.")
            elif message.type == WSMsgType.ERROR:
                raise StreamError("Client WebSocket failed.")

    async def upstream_messages(self):
        async for message in self.upstream:
            if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                if message.type == WSMsgType.ERROR:
                    raise StreamError("Gemini connection failed.")
                continue
            try:
                data = json.loads(message.data)
            except (ValueError, UnicodeError):
                raise StreamError("Gemini returned invalid transcription data.") from None
            if not isinstance(data, dict):
                raise StreamError("Gemini returned invalid transcription data.")
            if data.get("error"):
                raise StreamError("Gemini transcription request failed.")
            if "goAway" in data:
                raise StreamError("Gemini session renewal is required; reconnect.")
            if "setupComplete" in data and self.state == "connecting":
                self.state = "idle"
                self.deadline = time.monotonic() + self.config.idle_timeout
                await self.send({"type": "status", "status": "ready",
                    "setup_ms": round((time.monotonic() - self.began) * 1000)})
                continue
            if self.state not in ("active", "finishing"):
                continue
            content = data.get("serverContent", {})
            if not isinstance(content, dict):
                raise StreamError("Gemini returned invalid transcription data.")
            interim = content.get("interimInputTranscription", {})
            final = content.get("inputTranscription", {})
            if isinstance(interim, dict) and isinstance(interim.get("text"), str) and interim["text"]:
                await self.send({"type": "interim", "text": interim["text"]})
            if isinstance(final, dict) and isinstance(final.get("text"), str):
                self.has_final = True
                await self.send({"type": "final", "text": final["text"]})
            if content.get("generationComplete") or content.get("turnComplete"):
                self.generated = True
            await self.finish_turn()
        raise StreamError("Gemini connection closed; reconnect.")

    async def monitor(self):
        while True:
            now = time.monotonic()
            if now - self.began >= self.config.session_timeout:
                raise StreamError("Live session expired; reconnect.")
            if now >= self.deadline:
                if self.state == "idle":
                    await self.send({"type": "status", "status": "disconnected",
                                     "message": "Idle connection closed; reconnect when needed."})
                    return
                messages = {
                    "connecting": "Gemini Live setup timed out.",
                    "active": "Recording exceeded the 120 second limit.",
                    "finishing": "No complete final transcript received; reconnect and retry.",
                }
                raise StreamError(messages[self.state])
            await asyncio.sleep(min(self.config.disconnect_poll, max(0.01, self.deadline - now)))

    async def run(self):
        await self.send({"type": "status", "status": "connecting"})
        browser_task = asyncio.create_task(self.browser_messages())
        timer_task = asyncio.create_task(self.monitor())
        # ws_connect is not given a key-bearing URL: query parameters are built
        # internally and never returned/logged, even when a handshake fails.
        connect_task = asyncio.create_task(self.client.ws_connect(
            self.config.live_url, params={"key": self.key},
            timeout=aiohttp.ClientWSTimeout(ws_close=2), max_msg_size=MAX_EVENT_BYTES,
            autoping=True, heartbeat=20,
        ).__aenter__())
        self.tasks = [browser_task, timer_task, connect_task]
        try:
            done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            if connect_task not in done:
                for task in done:
                    await task
                return
            self.upstream = await connect_task
            if browser_task.done() or timer_task.done():
                for task in (browser_task, timer_task):
                    if task.done():
                        await task
                return
            await self.upstream.send_json({"setup": {
                "model": "models/" + os.environ.get("GEMINI_LIVE_STT_MODEL", "gemini-3.5-transcribe-live"),
                "generationConfig": {"responseModalities": ["TEXT"]},
                "inputAudioTranscription": {"languageCodes": ["ko-KR"], "mode": "VERBATIM"},
                "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
            }})
            upstream_task = asyncio.create_task(self.upstream_messages())
            self.tasks.append(upstream_task)
            done, _ = await asyncio.wait((browser_task, timer_task, upstream_task),
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, StreamError) else "Gemini Live connection failed."
            await self.send({"type": "error", "message": message})
        finally:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.upstream is None and connect_task.done() and not connect_task.cancelled():
                try:
                    self.upstream = connect_task.result()
                except Exception:
                    pass
            if self.upstream is not None:
                try:
                    await self.upstream.close()
                except (ConnectionError, aiohttp.ClientConnectionError, RuntimeError):
                    pass
            try:
                await self.browser.close()
            except (ConnectionError, RuntimeError):
                pass


async def live_stt(request):
    if not _same_origin(request):
        return _json({"error": "Cross-origin requests are not allowed."}, 403)
    try:
        key = _key()
    except StreamError as exc:
        return _json({"error": str(exc)}, 503)
    browser = web.WebSocketResponse(max_msg_size=MAX_PCM_FRAME_BYTES,
                                    timeout=2, heartbeat=20)
    if not browser.can_prepare(request).ok:
        return _json({"error": "WebSocket upgrade required."}, 426)
    await browser.prepare(request)
    session = LiveSession(request, browser, request.app[CLIENT_SESSION_KEY],
                          request.app[STREAM_CONFIG_KEY], key)
    request.app[LIVE_SESSIONS_KEY].add(browser)
    try:
        await session.run()
    finally:
        request.app[LIVE_SESSIONS_KEY].discard(browser)
    return browser


async def _client_context(app):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20)) as client:
        app[CLIENT_SESSION_KEY] = client
        app[LIVE_SESSIONS_KEY] = set()
        yield


async def _shutdown_live(app):
    await asyncio.gather(*(ws.close(code=1001, message=b"Server shutdown")
                          for ws in list(app[LIVE_SESSIONS_KEY])), return_exceptions=True)


def setup_streaming(app, config=None):
    """Register streaming routes and own one pooled upstream HTTP client."""
    app[STREAM_CONFIG_KEY] = config or StreamConfig()
    app.cleanup_ctx.append(_client_context)
    app.on_shutdown.append(_shutdown_live)
    app.router.add_post("/tts/stream", stream_tts)
    app.router.add_get("/stt/live", live_stt)
