"""Small, bounded REST adapter for Gemini Korean speech APIs.

Interactions schema: https://aistudio.google.com/api-reference/interactions-api
Inline audio: https://aistudio.google.com/docs/audio#inline-audio
Speech APIs: https://aistudio.google.com/docs/transcribe and /docs/speech-generation
"""

from __future__ import annotations

import base64
import binascii
import http.client
import io
import json
import math
import os
import socket
import struct
import urllib.error
import urllib.request
import wave

from media import change_speed

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024
MAX_INPUT_WAV_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 100_000


class GeminiError(RuntimeError):
    """An error safe to return to clients; never includes an upstream body/key."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API-key header to a redirect destination.
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _invalid_api_key(raw: bytes) -> bool:
    """Inspect a bounded structured reason without reflecting remote text."""
    if len(raw) > MAX_ERROR_RESPONSE_BYTES:
        return False
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    return isinstance(details, list) and any(
        isinstance(detail, dict) and detail.get("reason") == "API_KEY_INVALID"
        for detail in details
    )


def _request(payload: dict) -> dict:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise GeminiError("GEMINI_API_KEY is not configured.")
    if not key.isascii() or any(ord(char) < 33 for char in key):
        raise GeminiError("GEMINI_API_KEY is invalid.")
    try:
        timeout = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "60"))
    except ValueError:
        raise GeminiError("GEMINI_TIMEOUT_SECONDS must be a number from 1 to 300.") from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise GeminiError("GEMINI_TIMEOUT_SECONDS must be a number from 1 to 300.")

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": key,
            "Api-Revision": "2026-05-20",
        },
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            error_body = exc.read(MAX_ERROR_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException):
            error_body = b""
        finally:
            exc.close()
        if status == 400 and _invalid_api_key(error_body):
            message = "Gemini API key is invalid (HTTP 400)."
        elif status in (401, 403):
            message = f"Gemini authentication or access failed (HTTP {status})."
        elif status == 429:
            message = "Gemini quota or rate limit exceeded (HTTP 429)."
        elif status == 404:
            message = "Gemini model or endpoint is unavailable (HTTP 404)."
        elif status >= 500:
            message = f"Gemini service is temporarily unavailable (HTTP {status})."
        else:
            message = f"Gemini rejected the request (HTTP {status})."
        raise GeminiError(message) from None
    except (TimeoutError, socket.timeout):
        raise GeminiError("Gemini request timed out.") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise GeminiError("Gemini request timed out.") from None
        raise GeminiError("Unable to connect to Gemini.") from None
    except (OSError, http.client.HTTPException):
        raise GeminiError("Unable to read the Gemini response.") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise GeminiError("Gemini response exceeded the size limit.")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise GeminiError("Gemini returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise GeminiError("Gemini returned an invalid response.")
    if result.get("status") != "completed" or result.get("error"):
        raise GeminiError("Gemini did not complete the request.")
    return result


def _output_parts(result: dict, kind: str) -> list[dict]:
    steps = result.get("steps")
    if not isinstance(steps, list):
        raise GeminiError("Gemini returned no output steps.")
    parts = []
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            raise GeminiError("Gemini returned invalid output content.")
        parts.extend(part for part in content if isinstance(part, dict) and part.get("type") == kind)
    return parts


def _validate_wav(data: bytes) -> None:
    """Reject raw PCM, empty audio, and truncated RIFF/data payloads."""
    try:
        if not isinstance(data, bytes) or len(data) < 44:
            raise ValueError
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError
        if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
            raise ValueError
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            frame_size = audio.getnchannels() * audio.getsampwidth()
            if audio.getcomptype() != "NONE" or frames <= 0 or audio.getframerate() <= 0:
                raise ValueError
            if len(audio.readframes(frames)) != frames * frame_size:
                raise ValueError
    except (ValueError, EOFError, wave.Error, struct.error):
        raise GeminiError("Expected a complete, non-empty PCM WAV file.") from None


def synth_gemini(text: str, speed: float) -> bytes:
    """Synthesize the exact text as WAV; apply numeric tempo locally."""
    if not isinstance(text, str) or not text.strip():
        raise GeminiError("TTS text must be a non-empty string.")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise GeminiError("TTS speed must be a finite number between 0.5 and 2.0.")
    result = _request({
        "model": os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"),
        "store": False,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [{
                    "type": "speech_metadata",
                    "style": "자연스럽고 또렷한 한국어로 읽어 주세요.",
                }],
            }],
        }],
        "response_format": {"type": "audio", "mime_type": "audio/wav"},
        "generation_config": {
            "speech_config": [{"voice": os.environ.get("GEMINI_VOICE", "Kore")}],
        },
    })
    parts = _output_parts(result, "audio")
    if not parts:
        raise GeminiError("Gemini returned no speech audio.")
    # The Interactions SDK's output_audio property also selects the last block.
    part = parts[-1]
    if not isinstance(part.get("mime_type"), str) or part["mime_type"].split(";", 1)[0].lower() != "audio/wav":
        raise GeminiError("Gemini returned an unsupported audio format; WAV is required.")
    encoded = part.get("data")
    if not isinstance(encoded, str):
        raise GeminiError("Gemini returned no inline audio data.")
    try:
        wav = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise GeminiError("Gemini returned invalid audio data.") from None
    _validate_wav(wav)
    adjusted = change_speed(wav, speed)
    _validate_wav(adjusted)
    return adjusted


def transcribe_gemini(wav: bytes) -> str:
    """Transcribe bounded normalized audio inline; no hosted File API copy."""
    if not isinstance(wav, bytes) or len(wav) > MAX_INPUT_WAV_BYTES:
        raise GeminiError("STT audio exceeds the input size limit.")
    _validate_wav(wav)
    result = _request({
        "model": os.environ.get("GEMINI_STT_MODEL", "gemini-3.5-transcribe"),
        "store": False,
        "input": [{
            "type": "audio",
            "data": base64.b64encode(wav).decode("ascii"),
            "mime_type": "audio/wav",
        }],
        "generation_config": {
            "transcription_config": {
                "language_codes": ["ko-KR"],
                "mode": {"type": "verbatim"},
            },
        },
    })
    parts = _output_parts(result, "text")
    if not parts or any(not isinstance(part.get("text"), str) for part in parts):
        raise GeminiError("Gemini returned no transcript.")
    text = "\n".join(part["text"] for part in parts).strip()
    if not text:
        raise GeminiError("Gemini returned an empty transcript.")
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise GeminiError("Gemini transcript exceeded the size limit.")
    return text
