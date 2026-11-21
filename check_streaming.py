"""Opt-in real API check: stream TTS, then transcribe its audio in real time.

    python check_streaming.py --url http://127.0.0.1:8766

Calls the configured server, which incurs Gemini usage. Never reads an API key.
Requires requirements.txt and FFmpeg. Timing is network arrival, not playback.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import time
import wave
from pathlib import Path

import aiohttp

from media import extract_audio


async def message(ws):
    item = await asyncio.wait_for(ws.receive(), 25)
    if item.type != aiohttp.WSMsgType.TEXT:
        raise RuntimeError("Live STT connection closed before completion")
    data = json.loads(item.data)
    if data.get("type") == "error":
        raise RuntimeError(data.get("message", "Live STT failed"))
    return data


async def stream_tts(client, base, text, output):
    started = time.perf_counter()
    first = last = None
    audio = bytearray()
    chunks = 0
    completed = False
    async with client.post(base + "/tts/stream", json={"text": text}) as response:
        if response.status != 200:
            raise RuntimeError(f"TTS returned HTTP {response.status}")
        if response.content_type != "application/x-ndjson":
            raise RuntimeError("Expected an NDJSON TTS stream")
        async for line in response.content:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "error":
                raise RuntimeError(event.get("message", "TTS failed"))
            if event.get("type") == "done":
                completed = True
                break
            if event.get("type") != "audio":
                continue
            if (event.get("sample_rate"), event.get("channels"), event.get("encoding")) != (24000, 1, "pcm_s16le"):
                raise RuntimeError("Unexpected streamed audio format")
            chunk = base64.b64decode(event["data"], validate=True)
            if not chunk or len(chunk) % 2:
                raise RuntimeError("Invalid PCM audio chunk")
            now = time.perf_counter() - started
            first = now if first is None else first
            last = now
            audio.extend(chunk)
            chunks += 1
            if len(audio) > 16 * 1024 * 1024:
                raise RuntimeError("TTS output exceeds limit")
    if not completed or not audio:
        raise RuntimeError("TTS ended without audio and a successful done event")
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(audio)
    return {
        "first_audio_seconds": round(first, 3),
        "last_audio_seconds": round(last, 3),
        "complete_seconds": round(time.perf_counter() - started, 3),
        "audio_seconds": round(len(audio) / 48000, 3),
        "audio_chunks": chunks,
    }


async def transcribe_turn(ws, pcm):
    await ws.send_json({"type": "start"})
    while (await message(ws)).get("type") != "started":
        pass
    began = time.perf_counter()
    stopped = None
    first = None
    finals = []
    interims = 0

    async def receive():
        nonlocal first, interims
        while True:
            event = await message(ws)
            kind = event.get("type")
            if kind in {"interim", "final"} and event.get("text"):
                if first is None:
                    first = time.perf_counter() - began
                if kind == "interim":
                    interims += 1
                else:
                    finals.append(event["text"])
            if kind == "done":
                if stopped is None:
                    raise RuntimeError("STT completed before stop")
                return time.perf_counter() - stopped

    receiver = asyncio.create_task(receive())
    try:
        for offset in range(0, len(pcm), 3200):
            await ws.send_bytes(pcm[offset:offset + 3200])
            due = began + min(offset + 3200, len(pcm)) / 32000
            await asyncio.sleep(max(0, due - time.perf_counter()))
            if receiver.done():
                await receiver
                raise RuntimeError("STT completed during input")
        stopped = time.perf_counter()
        await ws.send_json({"type": "stop"})
        final_delay = await receiver
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
    if not finals:
        raise RuntimeError("STT returned no final transcription")
    return {
        "first_text_seconds": round(first, 3),
        "final_after_stop_seconds": round(final_delay, 3),
        "interim_events": interims,
        "text": "".join(finals),
    }


async def run(args):
    base = args.url.rstrip("/")
    args.out.mkdir(parents=True, exist_ok=True)
    output = args.out / "streaming-tts.wav"
    report = {"url": base, "input_text": args.text}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as client:
        report["tts"] = await stream_tts(client, base, args.text, output)
        normalized = await asyncio.to_thread(extract_audio, output.read_bytes())
        with wave.open(io.BytesIO(normalized), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
        opened = time.perf_counter()
        async with client.ws_connect(base + "/stt/live", max_msg_size=1024 * 1024) as ws:
            while True:
                event = await message(ws)
                if event.get("type") == "status" and event.get("status") == "ready":
                    report["live_ready_seconds"] = round(time.perf_counter() - opened, 3)
                    break
            report["live_stt"] = []
            for _ in range(args.turns):
                report["live_stt"].append(await transcribe_turn(ws, pcm))
    result = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (args.out / "result.json").write_text(result, encoding="utf-8")
    print(result, end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--text", default="안녕하세요. 해봄입니다. 설정 버튼을 눌러 주세요.")
    parser.add_argument("--turns", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--out", type=Path, default=Path("runs/streaming"))
    options = parser.parse_args()
    try:
        asyncio.run(run(options))
    except (RuntimeError, ValueError, aiohttp.ClientError, TimeoutError) as error:
        parser.exit(1, f"Streaming check failed: {error}\n")
