"""TTS 서버: POST /tts {"text": "...", "speed": 0.9, "engine": "baseline"} → 소리(audio/wav).

앱도 artemis 서버도 모른다. 받는 것은 문장 하나뿐이다. 같은 문장과 속도에는 같은 소리가 나오므로 만든 소리를 메모리에 둔다.

엔진은 이름으로 고른다(ENGINES). `baseline`은 espeak-ng(한국어)이고 항상 있다. 새 엔진은 그 옆에 자기 이름으로 더한다.
요청에 engine이 없으면 TTS_ENGINE(기본 baseline)으로 읽는다. 어느 엔진이 읽었는지는 응답의 X-TTS-Engine에 적는다.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("TTS_PORT", "8766"))
VOICE = os.environ.get("TTS_VOICE", "ko")
BASE_WPM = 160  # espeak-ng words per minute at speed 1.0
MAX_CHARS = 300

_cache: dict[tuple[str, str, float], bytes] = {}


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
ENGINES = {"baseline": synth_baseline}
DEFAULT_ENGINE = os.environ.get("TTS_ENGINE", "baseline")
if DEFAULT_ENGINE not in ENGINES:
    raise SystemExit(f"TTS_ENGINE={DEFAULT_ENGINE!r}: one of {sorted(ENGINES)}")


class Handler(BaseHTTPRequestHandler):
    server_version = "HaebomTts/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[tts] " + (fmt % args) + "\n")

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "engines": sorted(ENGINES), "default_engine": DEFAULT_ENGINE, "voice": VOICE})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/tts":
            self._json(404, {"error": "not found"})
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
            text = str(req.get("text", "")).strip()[:MAX_CHARS]
            speed = float(req.get("speed", 1.0))
            engine = str(req.get("engine") or DEFAULT_ENGINE)
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": f"bad request: {exc}"})
            return
        if not text:
            self._json(400, {"error": "text is empty"})
            return
        if engine not in ENGINES:
            self._json(400, {"error": f"unknown engine {engine!r}; this server has {sorted(ENGINES)}"})
            return
        key = (engine, text, round(speed, 2))
        try:
            audio = _cache.get(key) or ENGINES[engine](text, speed)
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": f"synthesis failed: {exc}"})
            return
        _cache[key] = audio
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("X-TTS-Engine", engine)
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)
        print(f"[tts] {len(audio)} bytes for {text[:40]!r} speed={speed} engine={engine}", flush=True)


if __name__ == "__main__":
    print(f"[tts] listening on {HOST}:{PORT} voice={VOICE} engines={sorted(ENGINES)} default={DEFAULT_ENGINE}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
