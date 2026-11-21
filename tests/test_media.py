"""Real FFmpeg checks of upload extraction and TTS speed conversion."""

import io
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from media import InvalidMedia, change_speed, extract_audio


def make_wav(seconds=1.0, rate=24000):
    samples = bytearray()
    for index in range(int(seconds * rate)):
        samples.extend(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * index / rate))))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(samples))
    return output.getvalue()


def wav_info(data):
    with wave.open(io.BytesIO(data), "rb") as wav:
        return wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes() / wav.getframerate()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class MediaTests(unittest.TestCase):
    def test_wav_is_resampled_to_stt_format(self):
        audio = extract_audio(make_wav())
        rate, channels, width, seconds = wav_info(audio)
        self.assertEqual((rate, channels, width), (16000, 1, 2))
        self.assertAlmostEqual(seconds, 1.0, places=2)
        self.assertEqual(struct.unpack_from("<I", audio, 4)[0] + 8, len(audio))

    def test_invalid_or_empty_file_is_rejected(self):
        with self.assertRaisesRegex(InvalidMedia, "empty"):
            extract_audio(b"")
        with self.assertRaisesRegex(InvalidMedia, "Invalid or unsupported"):
            extract_audio(b"this is not a media file")

    def test_playlist_input_is_rejected(self):
        with self.assertRaisesRegex(InvalidMedia, "Invalid or unsupported"):
            extract_audio(b"#EXTM3U\n#EXTINF:1,\nhttps://example.invalid/audio.wav\n")

    def test_overlong_file_is_rejected_instead_of_truncated(self):
        with self.assertRaisesRegex(InvalidMedia, "exceeds 1 seconds"):
            extract_audio(make_wav(seconds=1.5), max_seconds=1)

    def _mp4(self, with_audio):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fixture.mp4"
            args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=1"]
            if with_audio:
                source = Path(directory) / "audio.wav"
                source.write_bytes(make_wav())
                args.extend(["-i", str(source), "-c:a", "aac", "-shortest"])
            args.extend(["-c:v", "mpeg4", str(output)])
            subprocess.run(args, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            return output.read_bytes()

    def test_mp4_audio_track_is_extracted(self):
        audio = extract_audio(self._mp4(with_audio=True))
        rate, channels, width, seconds = wav_info(audio)
        self.assertEqual((rate, channels, width), (16000, 1, 2))
        self.assertGreater(seconds, 0.9)
        self.assertLess(seconds, 1.2)

    def test_silent_mp4_is_rejected(self):
        with self.assertRaisesRegex(InvalidMedia, "no audio track"):
            extract_audio(self._mp4(with_audio=False))

    def test_speed_preserves_sample_format_and_changes_duration(self):
        source = make_wav(seconds=2.0)
        self.assertIs(change_speed(source, 1.0), source)
        slow = change_speed(source, 0.7)
        fast = change_speed(source, 1.3)
        self.assertEqual(wav_info(slow)[:3], (24000, 1, 2))
        self.assertEqual(wav_info(fast)[:3], (24000, 1, 2))
        self.assertAlmostEqual(wav_info(slow)[3], 2 / 0.7, delta=0.1)
        self.assertAlmostEqual(wav_info(fast)[3], 2 / 1.3, delta=0.1)
        self.assertEqual(struct.unpack_from("<I", slow, 4)[0] + 8, len(slow))

    def test_invalid_speed_is_rejected(self):
        for speed in (0, 3, float("nan"), float("inf")):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                change_speed(make_wav(), speed)


if __name__ == "__main__":
    unittest.main()
