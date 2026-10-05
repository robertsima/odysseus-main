import io

import pytest
from fastapi import HTTPException, UploadFile

from src.upload_limits import format_byte_limit, read_upload_limited

def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


async def test_read_upload_limited_accepts_exact_limit():
    assert await read_upload_limited(_upload("ok.bin", b"abcd"), 4, "Test upload") == b"abcd"


async def test_read_upload_limited_rejects_oversized_upload():
    with pytest.raises(HTTPException) as exc:
        await read_upload_limited(_upload("too-big.bin", b"abcde"), 4, "Test upload")

    assert exc.value.status_code == 413
    assert exc.value.detail == "Test upload exceeds 4 bytes limit"


def test_upload_limit_formatting_is_human_readable():
    assert format_byte_limit(25 * 1024 * 1024) == "25 MB"
    assert format_byte_limit(512 * 1024) == "512 KB"
    assert format_byte_limit(7) == "7 bytes"

