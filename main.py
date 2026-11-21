"""한국어 음성 서버: 기존 파일 API와 스트리밍 API를 같은 포트로 제공한다.

POST /tts JSON → WAV, POST /stt 파일 → JSON text 규약을 유지한다.
/tts/stream과 /stt/live는 streaming.py가 처리한다. /tts는 엔진·문장·속도별로 캐시한다.

엔진은 이름으로 고른다(ENGINES). `baseline`은 espeak-ng(한국어)이고 항상 있다. 새 엔진은 그 옆에 자기 이름으로 더한다.
요청에 engine이 없으면 TTS_ENGINE(기본 baseline)으로 읽는다. 어느 엔진이 읽었는지는 응답의 X-TTS-Engine에 적는다.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import subprocess
from email import policy
from email.parser import BytesParser
from functools import lru_cache

from aiohttp import web

from gemini import GeminiError, synth_gemini, transcribe_gemini
from media import InvalidMedia, extract_audio

HOST = os.environ.get("TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_PORT", "8766"))
VOICE = os.environ.get("TTS_VOICE", "ko")
BASE_WPM = 160  # espeak-ng words per minute at speed 1.0
MAX_CHARS = 300
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
MAX_JSON_BYTES = 64 * 1024


def synth_baseline(text: str, speed: float) -> bytes:
    """WAV bytes of the sentence, read by espeak-ng."""
    wpm = max(80, min(300, int(BASE_WPM * speed)))
    out = subprocess.run(["espeak-ng", "-v", VOICE, "-s", str(wpm), "--stdout", text],
                         capture_output=True, timeout=10, check=True)
    return _sized(out.stdout)


def _sized(wav: bytes) -> bytes:
    """espeak-ng writes a stream: its header does not carry the real length. Players want it."""
    data = wav.find(b"data")
    if not wav.startswith(b"RIFF") or data < 0:
        return wav
    buf = bytearray(wav)
    struct.pack_into("<I", buf, 4, len(buf) - 8)
    struct.pack_into("<I", buf, data + 4, len(buf) - data - 8)
    return bytes(buf)


# name -> a function (sentence, speed) -> audio bytes. `baseline` stays; a new engine is added under its own name.
ENGINES = {"baseline": synth_baseline, "gemini": synth_gemini}
DEFAULT_ENGINE = os.environ.get("TTS_ENGINE", "baseline")
if DEFAULT_ENGINE not in ENGINES:
    raise SystemExit(f"TTS_ENGINE={DEFAULT_ENGINE!r}: one of {sorted(ENGINES)}")


@lru_cache(maxsize=128)
def synthesize(engine: str, text: str, speed: float) -> bytes:
    return ENGINES[engine](text, speed)


class BadRequest(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def uploaded_media(content_type: str, body: bytes) -> bytes:
    """Read one multipart `file`, or accept a raw audio/video request body."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime == "multipart/form-data":
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii") + body
        )
        if not message.is_multipart() or message.defects:
            raise BadRequest("invalid multipart upload")
        parts = list(message.iter_parts())
        files = [part for part in parts if part.get_param("name", header="content-disposition") == "file"]
        if len(files) != 1 or any(part.defects for part in parts):
            raise BadRequest("send exactly one file in the 'file' field")
        if files[0].is_multipart():
            raise BadRequest("nested multipart files are not supported")
        data = files[0].get_payload(decode=True)
    elif mime in {"video/mp4", "video/webm", "audio/mp4", "audio/m4a", "audio/x-m4a",
                  "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3", "audio/aac",
                  "audio/ogg", "audio/flac", "audio/x-flac", "audio/webm", "application/octet-stream"}:
        data = body
    else:
        raise BadRequest("use multipart/form-data with a file, or an audio/video Content-Type", 415)
    if not data:
        raise BadRequest("file is empty")
    return data


def json_response(value: dict, status: int = 200) -> web.Response:
    return web.Response(
        body=json.dumps(value, ensure_ascii=False).encode("utf-8"),
        status=status,
        content_type="application/json",
        charset="utf-8",
    )


@web.middleware
async def batch_errors(request: web.Request, handler):
    """Keep legacy errors stable; streaming handlers own their post-header errors."""
    if request.path not in {"/tts", "/stt", "/health"}:
        return await handler(request)
    try:
        return await handler(request)
    except BadRequest as exc:
        response = json_response({"error": str(exc)}, exc.status)
        # Do not drain an oversized or incomplete upload before responding.
        response.force_close()
        return response
    except (UnicodeError, json.JSONDecodeError):
        return json_response({"error": "invalid request encoding or JSON"}, 400)
    except InvalidMedia as exc:
        return json_response({"error": str(exc)}, 400 if request.path == "/stt" else 502)
    except asyncio.TimeoutError:
        response = json_response({"error": "request body timed out"}, 408)
        response.force_close()
        return response
    except GeminiError as exc:
        return json_response({"error": str(exc)}, 502)
    except web.HTTPException:
        raise
    except Exception:
        # Never send raw exceptions from subprocesses or authenticated requests.
        return json_response({"error": "speech processing failed"}, 502)


async def read_body(request: web.Request, limit: int) -> bytes:
    if request.headers.get("Transfer-Encoding"):
        raise BadRequest("chunked uploads are not supported; send Content-Length")
    lengths = request.headers.getall("Content-Length", [])
    if not lengths:
        raise BadRequest("Content-Length is required", 411)
    if len(lengths) != 1:
        raise BadRequest("invalid Content-Length")
    try:
        length = int(lengths[0])
    except ValueError:
        raise BadRequest("invalid Content-Length") from None
    if length <= 0:
        raise BadRequest("request body is empty")
    if length > limit:
        raise BadRequest("request body is too large", 413)
    if request.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise BadRequest("compressed request bodies are not supported", 415)
    try:
        return await asyncio.wait_for(request.content.readexactly(length), timeout=15)
    except asyncio.IncompleteReadError:
        raise BadRequest("incomplete request body") from None


async def health(request: web.Request) -> web.Response:
    return json_response({
        "ok": True, "engines": sorted(ENGINES), "default_engine": DEFAULT_ENGINE, "voice": VOICE,
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "stt": {"engine": "gemini", "language": "ko-KR", "max_upload_mb": 20,
                "max_seconds": MAX_AUDIO_SECONDS},
        "streaming": {
            "tts": {"path": "/tts/stream", "transport": "http-ndjson", "sample_rate": 24000,
                    "channels": 1, "encoding": "pcm_s16le"},
            "stt": {"path": "/stt/live", "transport": "websocket", "sample_rate": 16000,
                    "channels": 1, "encoding": "pcm_s16le", "max_seconds": MAX_AUDIO_SECONDS},
        },
    })


async def tts(request: web.Request) -> web.Response:
    req = json.loads((await read_body(request, MAX_JSON_BYTES)).decode("utf-8"))
    if not isinstance(req, dict) or not isinstance(req.get("text"), str):
        raise BadRequest("text must be a string")
    text = req["text"].strip()[:MAX_CHARS]
    if not text:
        raise BadRequest("text is empty")
    try:
        if isinstance(req.get("speed"), bool):
            raise ValueError
        speed = float(req.get("speed", 1.0))
    except (ValueError, TypeError):
        raise BadRequest("speed must be a number from 0.5 to 2.0") from None
    if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise BadRequest("speed must be a number from 0.5 to 2.0")
    engine = req.get("engine") or DEFAULT_ENGINE
    if not isinstance(engine, str) or engine not in ENGINES:
        raise BadRequest(f"unknown engine; choose from {sorted(ENGINES)}")
    if engine == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        raise BadRequest("GEMINI_API_KEY is not configured on the server", 503)
    # Legacy engines, FFmpeg and urllib are synchronous. They must not block
    # live sessions while another user waits for a whole file.
    audio = await asyncio.to_thread(synthesize, engine, text, round(speed, 2))
    print(f"[tts] {len(audio)} bytes chars={len(text)} speed={speed} engine={engine}", flush=True)
    return web.Response(body=audio, content_type="audio/wav", headers={"X-TTS-Engine": engine})


def transcribe_uploaded(data: bytes) -> str:
    wav = extract_audio(data, max_seconds=MAX_AUDIO_SECONDS)
    return transcribe_gemini(wav)


async def stt(request: web.Request) -> web.Response:
    body = await read_body(request, MAX_UPLOAD_BYTES)
    data = uploaded_media(request.headers.get("Content-Type", ""), body)
    if not os.environ.get("GEMINI_API_KEY"):
        raise BadRequest("GEMINI_API_KEY is not configured on the server", 503)
    text = await asyncio.to_thread(transcribe_uploaded, data)
    print(f"[stt] input_bytes={len(data)} output_chars={len(text)}", flush=True)
    return json_response({"text": text, "language": "ko-KR"})


async def not_found(request: web.Request) -> web.Response:
    return json_response({"error": "not found"}, 404)


def create_app() -> web.Application:
    from streaming import setup_streaming

    app = web.Application(client_max_size=MAX_UPLOAD_BYTES, middlewares=[batch_errors])
    app.router.add_get("/health", health)
    app.router.add_post("/tts", tts)
    app.router.add_post("/stt", stt)
    setup_streaming(app)
    app.router.add_route("*", "/{tail:.*}", not_found)
    return app


if __name__ == "__main__":
    print(f"[tts] listening on {HOST}:{PORT} voice={VOICE} engines={sorted(ENGINES)} default={DEFAULT_ENGINE}", flush=True)
    web.run_app(create_app(), host=HOST, port=PORT, access_log=None, handler_cancellation=True)
