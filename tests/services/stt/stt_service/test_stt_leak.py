import os
import tempfile

import pytest

from services.stt.stt_service import STTError, STTService


def test_stt_local_transcribe_leak_on_error():
    service = STTService()

    class MockWhisper:
        def transcribe(self, *args, **kwargs):
            raise ValueError("Simulated transcribe error")

    service._get_whisper = lambda: MockWhisper()

    # Track WebM files in the temp directory before running transcription
    temp_dir = tempfile.gettempdir()
    webm_before = {f for f in os.listdir(temp_dir) if f.endswith(".webm")}

    # User-safe errors are raised while the underlying exception stays private.
    with pytest.raises(STTError) as exc_info:
        service._transcribe_local(b"dummy_audio_data")

    # Track WebM files in the temp directory after running transcription
    webm_after = {f for f in os.listdir(temp_dir) if f.endswith(".webm")}

    assert exc_info.value.code == "transcription_failed"

    # Assert that no new temp files were leaked
    leaked = webm_after - webm_before
    assert len(leaked) == 0, f"Leaked files: {leaked}"
