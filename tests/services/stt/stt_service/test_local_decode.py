"""faster-whisper's audio decoder works against the PyAV this build installs.

Bug (2026-10-11): requirements pinned faster-whisper 1.2.1 but not PyAV, and
pip resolved PyAV 19, which dropped the ``metadata_errors`` argument that
faster-whisper 1.2.1 passes to ``av.open``. Every local transcription then
failed with TypeError before the model saw a sample; the log said only
"Local STT failed (TypeError)". No other test exercises the installed PyAV
through faster-whisper's own decode path.
"""
import importlib.util
import math
import struct
import wave

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None or importlib.util.find_spec("av") is None,
    reason="local speech-to-text is not installed in this build",
)


def _tone(path, seconds=0.5, rate=16000):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))))


def test_the_installed_pyav_decodes_a_recording_for_faster_whisper(tmp_path):
    from faster_whisper.audio import decode_audio

    path = tmp_path / "tone.wav"
    _tone(path)

    samples = decode_audio(str(path), sampling_rate=16000)

    assert len(samples) == 8000, len(samples)
