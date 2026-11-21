"""Network-free tests for the Gemini REST contract and response validation."""

import base64
import io
import json
import os
import struct
import unittest
import urllib.error
import urllib.request
import wave
from unittest.mock import patch

import gemini


def sample_wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 160)
    return buffer.getvalue()


def response_with(*parts):
    return {"status": "completed", "steps": [{"type": "model_output", "content": list(parts)}]}


def audio_part(wav=None):
    return {
        "type": "audio",
        "mime_type": "audio/wav",
        "data": base64.b64encode(sample_wav() if wav is None else wav).decode("ascii"),
    }


class GeminiTests(unittest.TestCase):
    def setUp(self):
        # Never read a developer's real API key or make a network request.
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-only-key"}, clear=True).start()
        self.open = patch.object(gemini._OPENER, "open").start()
        self.tempo = patch.object(gemini, "change_speed", side_effect=lambda wav, speed: wav).start()

    def respond(self, value):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        response = io.BytesIO(raw)
        self.open.return_value = response
        return response

    def payload(self):
        request = self.open.call_args.args[0]
        return request, json.loads(request.data)

    def test_tts_returns_complete_wav_and_uses_exact_numeric_tempo(self):
        wav = sample_wav()
        self.respond(response_with(audio_part(wav)))
        self.assertEqual(gemini.synth_gemini("안녕하세요.", 0.9), wav)
        self.tempo.assert_called_once_with(wav, 0.9)
        request, payload = self.payload()
        self.assertEqual(request.full_url, gemini.API_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-goog-api-key"), "unit-test-only-key")
        self.assertEqual(request.get_header("Api-revision"), "2026-05-20")
        self.assertNotIn("unit-test-only-key", request.full_url)
        self.assertNotIn(b"unit-test-only-key", request.data)
        self.assertEqual(self.open.call_args.kwargs["timeout"], 60)
        self.assertEqual(payload["model"], "gemini-3.8-flash-tts")
        self.assertFalse(payload["store"])
        text = payload["input"][0]["content"][0]
        self.assertEqual(text["text"], "안녕하세요.")
        self.assertEqual(text["annotations"][0]["type"], "speech_metadata")
        self.assertIn("한국어", text["annotations"][0]["style"])
        self.assertEqual(payload["response_format"], {"type": "audio", "mime_type": "audio/wav"})

    def test_env_model_voice_and_timeout_overrides(self):
        os.environ.update(GEMINI_TTS_MODEL="custom-tts", GEMINI_VOICE="Puck", GEMINI_TIMEOUT_SECONDS="12.5")
        self.respond(response_with(audio_part()))
        gemini.synth_gemini("안녕", 1)
        _, payload = self.payload()
        self.assertEqual(payload["model"], "custom-tts")
        self.assertEqual(payload["generation_config"]["speech_config"], [{"voice": "Puck"}])
        self.assertEqual(self.open.call_args.kwargs["timeout"], 12.5)

    def test_stt_sends_inline_wav_and_korean_verbatim_config(self):
        os.environ["GEMINI_STT_MODEL"] = "custom-stt"
        self.respond(response_with({"type": "text", "text": " 안녕하세요. "}))
        wav = sample_wav()
        self.assertEqual(gemini.transcribe_gemini(wav), "안녕하세요.")
        request, payload = self.payload()
        self.assertEqual(payload["model"], "custom-stt")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][0]["mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(payload["input"][0]["data"]), wav)
        self.assertEqual(payload["generation_config"]["transcription_config"], {
            "language_codes": ["ko-KR"], "mode": {"type": "verbatim"},
        })
        self.assertNotIn(b"unit-test-only-key", request.data)
        self.tempo.assert_not_called()

    def test_stt_reads_only_model_output_text(self):
        value = response_with({"type": "text", "text": "첫 문장."}, {"type": "text", "text": "둘째 문장."})
        value["steps"].insert(0, {"type": "user_input", "content": [{"type": "text", "text": "not output"}]})
        self.respond(value)
        self.assertEqual(gemini.transcribe_gemini(sample_wav()), "첫 문장.\n둘째 문장.")

    def test_http_failures_do_not_expose_error_body_url_or_key(self):
        cases = [(400, "rejected"), (401, "authentication"), (403, "authentication"),
                 (404, "unavailable"), (429, "quota"), (503, "temporarily")]
        for status, expected in cases:
            with self.subTest(status=status):
                self.open.side_effect = urllib.error.HTTPError(
                    "https://secret.example/?key=unit-test-only-key", status,
                    "private upstream error unit-test-only-key", {},
                    io.BytesIO(b"private upstream body unit-test-only-key"),
                )
                with self.assertRaises(gemini.GeminiError) as caught:
                    gemini.transcribe_gemini(sample_wav())
                message = str(caught.exception)
                self.assertIn(expected, message)
                self.assertIn(str(status), message)
                self.assertNotIn("unit-test-only-key", message)
                self.assertNotIn("private", message)
                self.assertNotIn("secret.example", message)

    def test_network_and_timeout_failures_are_safe(self):
        for error, expected in [(TimeoutError("secret"), "timed out"),
                                (urllib.error.URLError(TimeoutError("secret")), "timed out"),
                                (urllib.error.URLError("secret unit-test-only-key"), "connect")]:
            with self.subTest(error=type(error).__name__):
                self.open.side_effect = error
                with self.assertRaisesRegex(gemini.GeminiError, expected) as caught:
                    gemini.transcribe_gemini(sample_wav())
                self.assertNotIn("secret", str(caught.exception))

    def test_invalid_api_key_uses_structured_reason_without_exposing_body(self):
        body = json.dumps({"error": {
            "message": "private upstream unit-test-only-key",
            "details": [{"reason": "API_KEY_INVALID", "metadata": {"key": "unit-test-only-key"}}],
        }}).encode()
        self.open.side_effect = urllib.error.HTTPError(gemini.API_URL, 400, "Bad request", {}, io.BytesIO(body))
        with self.assertRaises(gemini.GeminiError) as caught:
            gemini.transcribe_gemini(sample_wav())
        self.assertEqual(str(caught.exception), "Gemini API key is invalid (HTTP 400).")

    def test_error_body_parse_is_bounded(self):
        body = json.dumps({"error": {"details": [{"reason": "API_KEY_INVALID"}], "message": "x" * 1000}}).encode()
        self.open.side_effect = urllib.error.HTTPError(gemini.API_URL, 400, "Bad request", {}, io.BytesIO(body))
        with patch.object(gemini, "MAX_ERROR_RESPONSE_BYTES", 32):
            with self.assertRaisesRegex(gemini.GeminiError, "rejected"):
                gemini.transcribe_gemini(sample_wav())

    def test_missing_or_invalid_key_fails_before_network(self):
        for key in ("", "bad\nkey", "bad key", "한글"):
            with self.subTest(key=key):
                os.environ["GEMINI_API_KEY"] = key
                with self.assertRaisesRegex(gemini.GeminiError, "GEMINI_API_KEY"):
                    gemini.transcribe_gemini(sample_wav())
        self.open.assert_not_called()

    def test_invalid_timeout_fails_before_network(self):
        for timeout in ("bad", "nan", "inf", "0", "301"):
            with self.subTest(timeout=timeout):
                os.environ["GEMINI_TIMEOUT_SECONDS"] = timeout
                with self.assertRaisesRegex(gemini.GeminiError, "GEMINI_TIMEOUT_SECONDS"):
                    gemini.transcribe_gemini(sample_wav())
        self.open.assert_not_called()

    def test_invalid_json_and_failed_interaction_are_rejected(self):
        for value in (b"not json", b"\xff", [], {"status": "failed", "error": {"message": "secret"}},
                      {"status": "in_progress"}, {"status": "completed"}):
            with self.subTest(value=repr(value)):
                self.respond(value)
                with self.assertRaises(gemini.GeminiError) as caught:
                    gemini.transcribe_gemini(sample_wav())
                self.assertNotIn("secret", str(caught.exception))

    def test_response_size_is_bounded(self):
        with patch.object(gemini, "MAX_RESPONSE_BYTES", 32):
            response = self.respond(b" " * 100)
            with self.assertRaisesRegex(gemini.GeminiError, "size limit"):
                gemini.transcribe_gemini(sample_wav())
            self.assertTrue(response.closed)

    def test_empty_or_missing_transcript_is_rejected(self):
        for value in (response_with(), response_with({"type": "text", "text": " \n "}),
                      response_with({"type": "text", "text": None})):
            with self.subTest(value=value):
                self.respond(value)
                with self.assertRaisesRegex(gemini.GeminiError, "transcript"):
                    gemini.transcribe_gemini(sample_wav())

    def test_tts_rejects_bad_base64_pcm_or_missing_audio(self):
        bad_base64 = audio_part()
        bad_base64["data"] = "%%%"
        raw_pcm = audio_part()
        raw_pcm["mime_type"] = "audio/l16"
        for value in (response_with(), response_with(bad_base64), response_with(raw_pcm),
                      response_with(audio_part(b"not a wave file"))):
            with self.subTest(value=value):
                self.respond(value)
                with self.assertRaises(gemini.GeminiError):
                    gemini.synth_gemini("안녕하세요", 1)
        self.tempo.assert_not_called()

    def test_truncated_audio_is_rejected_even_if_riff_size_was_rewritten(self):
        truncated = bytearray(sample_wav()[:-2])
        struct.pack_into("<I", truncated, 4, len(truncated) - 8)
        self.respond(response_with(audio_part(bytes(truncated))))
        with self.assertRaisesRegex(gemini.GeminiError, "complete"):
            gemini.synth_gemini("안녕", 1)
        self.tempo.assert_not_called()

    def test_tempo_output_is_also_validated(self):
        self.respond(response_with(audio_part()))
        self.tempo.side_effect = None
        self.tempo.return_value = b"invalid audio"
        with self.assertRaisesRegex(gemini.GeminiError, "PCM WAV"):
            gemini.synth_gemini("안녕", 0.9)

    def test_stt_rejects_invalid_or_oversized_wav_before_network(self):
        with self.assertRaises(gemini.GeminiError):
            gemini.transcribe_gemini(b"not audio")
        with patch.object(gemini, "MAX_INPUT_WAV_BYTES", 10):
            with self.assertRaisesRegex(gemini.GeminiError, "size limit"):
                gemini.transcribe_gemini(sample_wav())
        self.open.assert_not_called()

    def test_invalid_tts_input_fails_before_network(self):
        for text, speed in [("", 1), (None, 1), ("안녕", float("nan")), ("안녕", 0),
                            ("안녕", 0.4), ("안녕", 2.1), ("안녕", True)]:
            with self.subTest(text=text, speed=speed):
                with self.assertRaises(gemini.GeminiError):
                    gemini.synth_gemini(text, speed)
        self.open.assert_not_called()

    def test_redirect_handler_does_not_forward_credentials(self):
        request = urllib.request.Request(gemini.API_URL, headers={"x-goog-api-key": "unit-test-only-key"})
        result = gemini._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://other.example")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
