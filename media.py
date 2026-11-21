"""Bounded local media conversion for uploaded audio/video and TTS output."""

from __future__ import annotations

import io
import json
import math
import subprocess
import tempfile
import wave
from pathlib import Path


MAX_INPUT_BYTES = 25 * 1024 * 1024
_INPUT_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,wav,mp3,ogg,flac,aac"
_TIMEOUT_SECONDS = 30
_WAV_OVERHEAD = 64 * 1024


class InvalidMedia(ValueError):
    """A safe, client-facing explanation of an unusable media upload."""


def _run(command: list[str], timeout: int = _TIMEOUT_SECONDS) -> bytes:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise InvalidMedia("Media processing timed out.") from exc
    except subprocess.CalledProcessError as exc:
        raise InvalidMedia("Invalid or unsupported media file.") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg and ffprobe must be installed.") from exc
    return result.stdout


def _check_input(data: bytes) -> None:
    if not data:
        raise InvalidMedia("Media file is empty.")
    if len(data) > MAX_INPUT_BYTES:
        raise InvalidMedia("Media file exceeds the 25 MiB size limit.")


def _wav_info(data: bytes) -> tuple[int, int, int, int]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            result = (wav.getnframes(), wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
            if result[0] <= 0 or result[1] <= 0:
                raise InvalidMedia("Media file contains no audio samples.")
            return result
    except (wave.Error, EOFError) as exc:
        raise InvalidMedia("Invalid WAV audio.") from exc


def extract_audio(data: bytes, max_seconds: int = 120) -> bytes:
    """Extract the first audio track as 16 kHz mono PCM16 WAV.

    Reject overlong uploads instead of returning a truncated transcript. The
    decode time/size caps also cover streams with missing or false durations.
    No user-supplied path or URL is passed to the media tools.
    """
    _check_input(data)
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be a positive finite number")
    duration_error = f"Media duration exceeds {max_seconds:g} seconds."
    decode_limit = max_seconds + 1
    output_limit = math.ceil(decode_limit * 16000 * 2) + _WAV_OVERHEAD
    with tempfile.TemporaryDirectory(prefix="haebom-media-") as directory:
        source = Path(directory) / "input.media"
        output = Path(directory) / "audio.wav"
        source.write_bytes(data)
        probe_bytes = _run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", _INPUT_FORMATS,
            "-show_entries", "stream=codec_type,duration:format=duration",
            "-of", "json", str(source),
        ])
        try:
            probe = json.loads(probe_bytes)
            streams = probe.get("streams", [])
            if not any(stream.get("codec_type") == "audio" for stream in streams):
                raise InvalidMedia("Media file has no audio track.")
            for entry in [probe.get("format", {}), *streams]:
                try:
                    duration = float(entry.get("duration", "nan"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(duration) and duration > max_seconds:
                    raise InvalidMedia(duration_error)
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise InvalidMedia("Invalid or unsupported media file.") from exc

        _run([
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", _INPUT_FORMATS,
            "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-t", str(decode_limit), "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-fs", str(output_limit), str(output),
        ])
        if output.stat().st_size >= output_limit:
            raise InvalidMedia(duration_error)
        audio = output.read_bytes()
        frames, rate, _, _ = _wav_info(audio)
        if frames / rate > max_seconds:
            raise InvalidMedia(duration_error)
        return audio


def change_speed(wav: bytes, speed: float) -> bytes:
    """Apply deterministic TTS speed control without shifting the voice pitch."""
    if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise ValueError("speed must be between 0.5 and 2.0")
    if speed == 1.0:
        return wav
    _check_input(wav)
    frames, rate, channels, _ = _wav_info(wav)
    output_limit = math.ceil((frames / speed + rate) * channels * 2) + _WAV_OVERHEAD
    with tempfile.TemporaryDirectory(prefix="haebom-speed-") as directory:
        source = Path(directory) / "input.wav"
        output = Path(directory) / "output.wav"
        source.write_bytes(wav)
        _run([
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", "wav",
            "-i", str(source), "-map", "0:a:0", "-af", f"atempo={speed}",
            "-c:a", "pcm_s16le", "-ar", str(rate), "-ac", str(channels),
            "-fs", str(output_limit), str(output),
        ])
        if output.stat().st_size >= output_limit:
            raise InvalidMedia("Converted audio exceeds the size limit.")
        audio = output.read_bytes()
        _wav_info(audio)
        return audio
