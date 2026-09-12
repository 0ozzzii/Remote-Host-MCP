from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from dsw_direct_mcp.config import Settings
from dsw_direct_mcp.filesystem import (
    chmod_path,
    copy_path,
    hash_file,
    make_directory,
    move_path,
    path_info,
    read_file_chunk,
    remove_path,
    write_text_file,
)
from dsw_direct_mcp.transfer import (
    abort_upload,
    begin_upload,
    download_chunk,
    download_info,
    finish_upload,
    upload_chunk,
    upload_status,
)


def make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("DSW_MCP_PATH_KEY", "u" * 48)
    monkeypatch.setenv("DSW_MCP_PUBLIC_HOST", "direct.example.com")
    monkeypatch.setenv("DSW_MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("DSW_MCP_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("DSW_MCP_MAX_FILE_CHUNK_BYTES", "16384")
    monkeypatch.setenv("DSW_MCP_MAX_TRANSFER_BYTES", str(1024 * 1024))
    monkeypatch.setenv("DSW_MCP_UPLOAD_TTL_SECONDS", "3600")
    return Settings.from_env()


def test_atomic_text_write_hash_guard_and_binary_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    target = tmp_path / "script.py"

    first = write_text_file(str(target), "print('one')\n", False, None, 0o640, settings)
    assert first.success is True
    assert first.atomic is True
    assert first.replaced is False
    assert target.read_text(encoding="utf-8") == "print('one')\n"
    assert oct(target.stat().st_mode & 0o777) == "0o640"
    assert hash_file(str(target), settings).digest == first.sha256

    with pytest.raises(ValueError, match="overwrite=false"):
        write_text_file(str(target), "nope\n", False, None, 0o644, settings)

    with pytest.raises(ValueError, match="no longer matches"):
        write_text_file(str(target), "two\n", True, "0" * 64, 0o644, settings)

    second = write_text_file(str(target), "print('two')\n", True, first.sha256, 0o644, settings)
    assert second.replaced is True
    assert target.read_text(encoding="utf-8") == "print('two')\n"

    chunk = read_file_chunk(str(target), 3, 5, settings)
    assert base64.b64decode(chunk.data_base64) == target.read_bytes()[3:8]
    assert chunk.next_offset == 8


def test_native_path_operations_and_symlink_safety(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    src_dir = tmp_path / "src"
    make_directory(str(src_dir), False, False, 0o750, settings)
    src = src_dir / "a.txt"
    src.write_text("hello", encoding="utf-8")

    chmod_path(str(src), 0o600, settings)
    assert src.stat().st_mode & 0o777 == 0o600

    copied = tmp_path / "copied.txt"
    copy_path(str(src), str(copied), False, False, settings)
    assert copied.read_text(encoding="utf-8") == "hello"

    moved = tmp_path / "moved.txt"
    move_path(str(copied), str(moved), False, settings)
    assert moved.exists() and not copied.exists()

    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "outside-link"
    link.symlink_to(outside)
    try:
        info = path_info(str(link), settings)
        assert info.type == "symlink"
        assert info.symlink_target == str(outside)
        with pytest.raises(ValueError):
            read_file_chunk(str(link), 0, 10, settings)
        with pytest.raises(ValueError, match="symlink"):
            chmod_path(str(link), 0o600, settings)
        remove_path(str(link), False, settings)
        assert not link.exists() and outside.exists()
    finally:
        outside.unlink(missing_ok=True)

    with pytest.raises(ValueError, match="allowed root"):
        remove_path(str(tmp_path), True, settings)


def test_resumable_upload_duplicate_retry_commit_and_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    payload = (b"0123456789abcdef" * 3000) + b"tail"
    expected = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "payload.bin"

    begin = begin_upload(str(target), len(payload), expected, 0o640, False, settings)
    assert begin.next_offset == 0
    assert begin.max_chunk_bytes == 16384
    assert not target.exists()

    offset = 0
    while offset < len(payload):
        raw = payload[offset : offset + 12000]
        encoded = base64.b64encode(raw).decode("ascii")
        chunk_hash = hashlib.sha256(raw).hexdigest()
        result = upload_chunk(begin.upload_id, offset, encoded, chunk_hash, settings)
        assert result.accepted is True
        if offset == 0:
            duplicate = upload_chunk(begin.upload_id, offset, encoded, chunk_hash, settings)
            assert duplicate.duplicate is True
            assert duplicate.next_offset == result.next_offset
            bad = base64.b64encode(b"x" * len(raw)).decode("ascii")
            with pytest.raises(ValueError, match="Conflicting retry"):
                upload_chunk(begin.upload_id, offset, bad, None, settings)
        offset = result.next_offset

    state = upload_status(begin.upload_id, settings)
    assert state.status == "open"
    assert state.bytes_received == len(payload)
    assert not target.exists()

    finished = finish_upload(begin.upload_id, settings)
    assert finished.success is True
    assert finished.sha256 == expected
    assert finished.already_committed is False
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o777 == 0o640

    again = finish_upload(begin.upload_id, settings)
    assert again.already_committed is True
    assert again.sha256 == expected

    info = download_info(str(target), True, settings)
    assert info.size == len(payload)
    assert info.sha256 == expected
    out = bytearray()
    offset = 0
    while True:
        part = download_chunk(str(target), offset, 10000, settings)
        out.extend(base64.b64decode(part.data_base64))
        offset = part.next_offset
        if part.eof:
            break
    assert bytes(out) == payload


def test_upload_rejects_gaps_hash_mismatch_and_abort(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    payload = b"abcdef"
    target = tmp_path / "bad.bin"

    begin = begin_upload(str(target), len(payload), hashlib.sha256(payload).hexdigest(), 0o644, False, settings)
    with pytest.raises(ValueError, match="Expected offset"):
        upload_chunk(begin.upload_id, 1, base64.b64encode(b"a").decode("ascii"), None, settings)
    with pytest.raises(ValueError, match="Chunk SHA-256 mismatch"):
        upload_chunk(begin.upload_id, 0, base64.b64encode(payload).decode("ascii"), "0" * 64, settings)

    upload_chunk(begin.upload_id, 0, base64.b64encode(payload).decode("ascii"), None, settings)
    finished = finish_upload(begin.upload_id, settings)
    assert finished.sha256 == hashlib.sha256(payload).hexdigest()

    wrong_target = tmp_path / "wrong.bin"
    wrong = begin_upload(str(wrong_target), len(payload), "0" * 64, 0o644, False, settings)
    upload_chunk(wrong.upload_id, 0, base64.b64encode(payload).decode("ascii"), None, settings)
    with pytest.raises(ValueError, match="Final SHA-256 mismatch"):
        finish_upload(wrong.upload_id, settings)
    assert not wrong_target.exists()
    assert upload_status(wrong.upload_id, settings).status == "hash_mismatch"
    aborted = abort_upload(wrong.upload_id, settings)
    assert aborted.status == "aborted"


def test_transfer_limits_and_outside_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    target = tmp_path / "large.bin"
    with pytest.raises(ValueError, match="total_size"):
        begin_upload(str(target), settings.max_transfer_bytes + 1, "0" * 64, 0o644, False, settings)
    with pytest.raises(ValueError):
        begin_upload("/etc/dswd-test.bin", 0, hashlib.sha256(b"").hexdigest(), 0o644, False, settings)

    target.write_bytes(b"abc")
    with pytest.raises(ValueError, match="max_bytes"):
        read_file_chunk(str(target), 0, settings.max_file_chunk_bytes + 1, settings)
