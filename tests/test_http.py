"""Exercise the public HTTP contract without making Gemini requests."""

from __future__ import annotations

from contextlib import ExitStack
import asyncio
import io
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
import wave

from aiohttp.test_utils import TestClient, TestServer

import main


def sample_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 4800)
    return output.getvalue()


def multipart(parts: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "haebom-test-boundary"
    body = bytearray()
    for name, filename, data in parts:
        body.extend((
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            "Content-Type: video/mp4\r\n\r\n"
        ).encode("ascii"))
        body.extend(data)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class HttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"GEMINI_API_KEY": "test-only-not-real"}))
        self.wav = sample_wav()
        self.baseline = Mock(return_value=self.wav)
        self.gemini = Mock(return_value=self.wav)
        self.stack.enter_context(patch.dict(main.ENGINES, {
            "baseline": self.baseline, "gemini": self.gemini,
        }, clear=True))
        self.stack.enter_context(patch.object(main, "DEFAULT_ENGINE", "baseline"))
        cache = getattr(main, "_cache", None)
        if cache is not None:
            cache.clear()
        synthesize = getattr(main, "synthesize", None)
        if synthesize is not None and hasattr(synthesize, "cache_clear"):
            synthesize.cache_clear()
        self.extract = self.stack.enter_context(patch.object(main, "extract_audio", return_value=self.wav))
        self.transcribe = self.stack.enter_context(patch.object(
            main, "transcribe_gemini", return_value="설정 화면을 열어 주세요."
        ))

        self.client = TestClient(TestServer(main.create_app(), handler_cancellation=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def request(self, method, path, body=None, content_type=None):
        headers = {"Content-Type": content_type} if content_type else {}
        async with self.client.request(method, path, data=body, headers=headers) as response:
            return response.status, dict(response.headers), await response.read()

    async def post_json(self, payload):
        return await self.request("POST", "/tts", json.dumps(payload).encode("utf-8"), "application/json")

    async def test_health_keeps_existing_fields_and_reports_stt(self):
        status, _, body = await self.request("GET", "/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIn("baseline", data["engines"])
        self.assertIn("gemini", data["engines"])
        self.assertEqual(data["default_engine"], "baseline")
        self.assertEqual(data["voice"], "ko")
        self.assertTrue(data["gemini_configured"])
        self.assertIn("stt", data)
        self.assertNotIn("test-only-not-real", body.decode())

    async def test_tts_returns_complete_playable_wav_and_engine_header(self):
        status, headers, body = await self.post_json({"text": "설정을 누르세요", "engine": "gemini", "speed": 0.9})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(headers["X-TTS-Engine"], "gemini")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        with wave.open(io.BytesIO(body), "rb") as wav:
            self.assertEqual(wav.getnframes(), 4800)
        self.gemini.assert_called_once_with("설정을 누르세요", 0.9)

    async def test_baseline_does_not_require_a_gemini_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            status, headers, _ = await self.post_json({"text": "안녕하세요", "engine": "baseline"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-TTS-Engine"], "baseline")
        self.baseline.assert_called_once()
        self.gemini.assert_not_called()

    async def test_tts_rejects_invalid_json_and_non_object_payloads(self):
        for body in (b"not json", b"null", b"[]", b'"text"'):
            with self.subTest(body=body):
                status, _, _ = await self.request("POST", "/tts", body, "application/json")
                self.assertEqual(status, 400)
        self.baseline.assert_not_called()

    async def test_tts_rejects_missing_empty_or_non_string_text(self):
        for payload in ({}, {"text": "  "}, {"text": None}, {"text": 123}, {"text": ["안녕"]}):
            with self.subTest(payload=payload):
                self.assertEqual((await self.post_json(payload))[0], 400)
        self.baseline.assert_not_called()

    async def test_tts_rejects_non_finite_or_out_of_range_speed(self):
        for speed in (float("nan"), float("inf"), -1, 0, 2.1, True):
            with self.subTest(speed=speed):
                self.assertEqual((await self.post_json({"text": "안녕", "speed": speed}))[0], 400)
        self.baseline.assert_not_called()

    async def test_unknown_engine_is_a_client_error(self):
        status, _, _ = await self.post_json({"text": "안녕", "engine": "unknown"})
        self.assertEqual(status, 400)

    async def test_tts_upstream_error_does_not_expose_exception_details(self):
        self.gemini.side_effect = RuntimeError("private-upstream-value")
        status, _, body = await self.post_json({"text": "실패 요청", "engine": "gemini"})
        self.assertEqual(status, 502)
        self.assertNotIn(b"private-upstream-value", body)

    async def test_tts_audio_conversion_failure_is_a_server_error(self):
        self.gemini.side_effect = main.InvalidMedia("Media processing timed out.")
        status, _, _ = await self.post_json({"text": "변환 실패 요청", "engine": "gemini"})
        self.assertEqual(status, 502)

    async def test_stt_multipart_preserves_uploaded_binary_exactly(self):
        uploaded = b"\x00\x01binary\xff\r\n\x00payload\r\n"
        body, content_type = multipart([("file", "../recording.mp4", uploaded)])
        status, _, response = await self.request("POST", "/stt", body, content_type)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response), {"text": "설정 화면을 열어 주세요.", "language": "ko-KR"})
        self.assertEqual(self.extract.call_args.args[0], uploaded)
        self.transcribe.assert_called_once_with(self.wav)

    async def test_stt_accepts_raw_media_upload(self):
        for content_type in ("video/mp4", "audio/wav"):
            with self.subTest(content_type=content_type):
                status, _, response = await self.request("POST", "/stt", self.wav, content_type)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(response)["language"], "ko-KR")
        self.assertEqual(self.transcribe.call_count, 2)

    async def test_stt_rejects_missing_duplicate_and_empty_file(self):
        cases = [
            [("other", "clip.mp4", b"data")],
            [("file", "first.mp4", b"first"), ("file", "second.mp4", b"second")],
            [("file", "empty.mp4", b"")],
        ]
        for parts in cases:
            with self.subTest(parts=parts):
                body, content_type = multipart(parts)
                self.assertEqual((await self.request("POST", "/stt", body, content_type))[0], 400)
        self.extract.assert_not_called()
        self.transcribe.assert_not_called()

    async def test_stt_rejects_multipart_without_boundary(self):
        status, _, _ = await self.request("POST", "/stt", b"bad body", "multipart/form-data")
        self.assertEqual(status, 400)
        self.transcribe.assert_not_called()

    async def raw_request(self, headers: bytes) -> bytes:
        reader, writer = await asyncio.open_connection(self.client.server.host, self.client.server.port)
        try:
            writer.write(headers)
            await writer.drain()
            return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_stt_rejects_oversized_body_before_reading_it(self):
        response = await self.raw_request(
            b"POST /stt HTTP/1.1\r\nHost: localhost\r\nContent-Type: video/mp4\r\n"
            b"Content-Length: 22020096\r\n\r\n"
        )
        self.assertIn(b" 413 ", response.split(b"\r\n", 1)[0])
        self.extract.assert_not_called()
        self.transcribe.assert_not_called()

    async def test_batch_upload_still_requires_content_length(self):
        response = await self.raw_request(b"POST /stt HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertIn(b" 411 ", response.split(b"\r\n", 1)[0])

    async def test_batch_upload_still_rejects_chunked_body(self):
        response = await self.raw_request(
            b"POST /stt HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    async def test_invalid_media_is_a_client_error(self):
        self.extract.side_effect = main.InvalidMedia("No audio track was found")
        status, _, _ = await self.request("POST", "/stt", b"not audio", "video/mp4")
        self.assertEqual(status, 400)
        self.transcribe.assert_not_called()

    async def test_stt_upstream_error_does_not_expose_exception_details(self):
        self.transcribe.side_effect = RuntimeError("private-upstream-value")
        status, _, body = await self.request("POST", "/stt", self.wav, "audio/wav")
        self.assertEqual(status, 502)
        self.assertNotIn(b"private-upstream-value", body)

    async def test_gemini_endpoints_fail_clearly_when_key_is_not_configured(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertEqual((await self.post_json({"text": "안녕", "engine": "gemini"}))[0], 503)
            self.assertEqual((await self.request("POST", "/stt", self.wav, "audio/wav"))[0], 503)
        self.gemini.assert_not_called()
        self.transcribe.assert_not_called()

    async def test_tts_still_truncates_text_and_caches_same_request(self):
        payload = {"text": "가" * 350, "speed": 0.9}
        first = await self.post_json(payload)
        second = await self.post_json(payload)
        self.assertEqual(first[0], 200)
        self.assertEqual(second[2], first[2])
        self.baseline.assert_called_once_with("가" * 300, 0.9)

    async def test_slow_batch_synthesis_does_not_block_health(self):
        entered, release = threading.Event(), threading.Event()
        released_before_timeout = []

        def slow_synth(text, speed):
            entered.set()
            released_before_timeout.append(release.wait(3))
            return self.wav

        self.baseline.side_effect = slow_synth
        pending = asyncio.create_task(self.post_json({"text": "느린 문장"}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            status, _, _ = await asyncio.wait_for(self.request("GET", "/health"), 1)
            self.assertEqual(status, 200)
        finally:
            release.set()
            self.assertEqual((await pending)[0], 200)
        self.assertEqual(released_before_timeout, [True])

    async def test_health_advertises_streaming_without_changing_legacy_fields(self):
        status, _, body = await self.request("GET", "/health")
        streaming = json.loads(body)["streaming"]
        self.assertEqual(status, 200)
        self.assertEqual(streaming["tts"]["path"], "/tts/stream")
        self.assertEqual(streaming["stt"]["path"], "/stt/live")
        self.assertEqual(streaming["tts"]["sample_rate"], 24000)
        self.assertEqual(streaming["stt"]["sample_rate"], 16000)


if __name__ == "__main__":
    unittest.main()
