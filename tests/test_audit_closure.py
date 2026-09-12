from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from mcp import Client
import pytest

from remote_host_mcp import __version__
from remote_host_mcp.app import build_server
from remote_host_mcp.config import ConfigError, Settings
from remote_host_mcp.doctor import collect_report
from remote_host_mcp.filesystem import copy_path, make_directory, read_text_file, write_text_file
import remote_host_mcp.filesystem as filesystem
from remote_host_mcp.jobs import cleanup_job, job_status, start_job
from remote_host_mcp.provenance import get_build_provenance
from remote_host_mcp.terminal import TerminalSession, _RHMCP_OSC_PREFIX, _RHMCP_OSC_TAIL_MAX_BYTES
from remote_host_mcp.transfer import abort_upload, begin_upload, finish_upload, upload_chunk
import remote_host_mcp.transfer as transfer


def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    suffixes = ["AUTH_MODE", "PATH_KEY", "PUBLIC_HOST", "ALLOWED_ROOTS", "STATE_DIR", "MAX_FILE_CHUNK_BYTES"]
    for suffix in suffixes:
        monkeypatch.delenv(f"DSW_MCP_{suffix}", raising=False)
    monkeypatch.setenv("RHMCP_AUTH_MODE", "capability")
    monkeypatch.setenv("RHMCP_PATH_KEY", "q" * 48)
    monkeypatch.setenv("RHMCP_PUBLIC_HOST", "audit.example.com")
    monkeypatch.setenv("RHMCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("RHMCP_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("RHMCP_MAX_FILE_CHUNK_BYTES", "16384")
    return Settings.from_env()


def wait_job(job_id: str, cfg: Settings, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    current = job_status(job_id, cfg)
    while time.monotonic() < deadline:
        current = job_status(job_id, cfg)
        if current.status in {"completed", "failed", "canceled", "timed_out", "interrupted", "start_failed"}:
            return current
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {current}")


def test_dual_namespace_conflict_fails_closed_without_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = settings(monkeypatch, tmp_path)
    assert cfg.max_request_body_bytes > 0
    monkeypatch.setenv("RHMCP_MAX_REQUEST_BODY_BYTES", "111111")
    monkeypatch.setenv("DSW_MCP_MAX_REQUEST_BODY_BYTES", "222222")
    with pytest.raises(ConfigError) as caught:
        Settings.from_env()
    message = str(caught.value)
    assert "RHMCP_MAX_REQUEST_BODY_BYTES" in message
    assert "DSW_MCP_MAX_REQUEST_BODY_BYTES" in message
    assert "111111" not in message and "222222" not in message


def test_existing_directory_mode_is_not_changed_by_exist_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = settings(monkeypatch, tmp_path)
    target = tmp_path / "private"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    make_directory(str(target), False, True, 0o755, cfg)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_read_text_file_uses_fixed_reads_for_huge_unbroken_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    target = tmp_path / "huge.txt"
    target.write_bytes(b"x" * (2 * 1024 * 1024))
    original = filesystem.os.read
    requested: list[int] = []

    def bounded_read(fd: int, count: int) -> bytes:
        requested.append(count)
        return original(fd, count)

    monkeypatch.setattr(filesystem.os, "read", bounded_read)
    result = read_text_file(str(target), 1, 10, 4096, cfg)
    assert len(result.text.encode()) <= 4096
    assert result.truncated is True
    assert requested and max(requested) <= 65536


def test_write_no_clobber_is_atomic_against_destination_appearance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    target = tmp_path / "race.txt"
    real = filesystem.rename_noreplace
    injected = False

    def race(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            target.write_text("competitor", encoding="utf-8")
        real(src_fd, src, dst_fd, dst)

    monkeypatch.setattr(filesystem, "rename_noreplace", race)
    with pytest.raises(ValueError, match="overwrite=false"):
        write_text_file(str(target), "ours", False, None, 0o600, cfg)
    assert target.read_text(encoding="utf-8") == "competitor"


def test_expected_sha_guard_rolls_back_concurrent_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    target = tmp_path / "cas.txt"
    target.write_text("original", encoding="utf-8")
    expected = hashlib.sha256(b"original").hexdigest()
    real = filesystem.rename_exchange
    injected = False

    def race(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            target.write_text("competitor", encoding="utf-8")
        real(src_fd, src, dst_fd, dst)

    monkeypatch.setattr(filesystem, "rename_exchange", race)
    with pytest.raises(ValueError, match="changed during expected_sha256"):
        write_text_file(str(target), "ours", True, expected, 0o600, cfg)
    assert target.read_text(encoding="utf-8") == "competitor"


def test_copy_failure_never_destroys_existing_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")

    def fail_copy(src_parent: int, src_name: str, dst_parent: int, dst_name: str) -> None:
        fd = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_parent)
        os.write(fd, b"partial")
        os.close(fd)
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(filesystem, "_copy_tree_at", fail_copy)
    with pytest.raises(OSError, match="synthetic copy failure"):
        copy_path(str(source), str(destination), False, True, cfg)
    assert destination.read_text(encoding="utf-8") == "old"


def test_copy_overwrite_publishes_complete_staged_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = settings(monkeypatch, tmp_path)
    source = tmp_path / "source-tree"
    destination = tmp_path / "dest-tree"
    source.mkdir()
    destination.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    (destination / "old.txt").write_text("old", encoding="utf-8")
    result = copy_path(str(source), str(destination), True, True, cfg)
    assert result.success
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (destination / "old.txt").exists()


def _prepare_upload(cfg: Settings, target: Path, data: bytes, overwrite: bool = False):
    started = begin_upload(str(target), len(data), hashlib.sha256(data).hexdigest(), 0o600, overwrite, cfg)
    upload_chunk(
        started.upload_id,
        0,
        base64.b64encode(data).decode("ascii"),
        hashlib.sha256(data).hexdigest(),
        cfg,
    )
    return started


def test_upload_finish_no_clobber_race_preserves_competitor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    target = tmp_path / "upload.bin"
    started = _prepare_upload(cfg, target, b"ours")
    real = transfer.rename_noreplace
    injected = False

    def race(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        nonlocal injected
        if not injected:
            injected = True
            target.write_bytes(b"competitor")
        real(src_fd, src, dst_fd, dst)

    monkeypatch.setattr(transfer, "rename_noreplace", race)
    with pytest.raises(ValueError, match="overwrite=false"):
        finish_upload(started.upload_id, cfg)
    assert target.read_bytes() == b"competitor"
    abort_upload(started.upload_id, cfg)


def test_upload_abort_never_deletes_committed_final(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = settings(monkeypatch, tmp_path)
    target = tmp_path / "committed.bin"
    started = _prepare_upload(cfg, target, b"committed")
    finish_upload(started.upload_id, cfg)
    with pytest.raises(ValueError, match="Committed upload cannot be aborted"):
        abort_upload(started.upload_id, cfg)
    assert target.read_bytes() == b"committed"


def test_osc133_incomplete_marker_tail_is_strictly_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    session = TerminalSession(
        terminal_id="a" * 32,
        child=object(),
        shell="/bin/sh",
        initial_cwd=str(tmp_path),
        cols=80,
        rows=24,
        created_at=1,
        last_activity_at=1,
        max_buffer_bytes=65536,
        state_dir=tmp_path,
        settings=cfg,
    )
    cleaned = session._strip_rhmcp_osc133(_RHMCP_OSC_PREFIX + b"x" * 10000)
    assert len(session.osc_tail) <= _RHMCP_OSC_TAIL_MAX_BYTES
    assert len(cleaned) >= 10000


def test_cleanup_leaves_idempotency_tombstone_and_never_replays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    marker = tmp_path / "ran.txt"
    command = f"printf x >> {marker}"
    started = start_job(command, str(tmp_path), None, "never-replay-after-cleanup", cfg)
    assert wait_job(started.job_id, cfg).status == "completed"
    cleanup_job(started.job_id, cfg)
    with pytest.raises(ValueError, match="cannot be replayed"):
        start_job(command, str(tmp_path), None, "never-replay-after-cleanup", cfg)
    assert marker.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_exact_tool_manifest_and_strict_top_level_input_schemas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    expected = json.loads((Path(__file__).parent / "tool_manifest.json").read_text(encoding="utf-8"))
    async with Client(build_server(cfg)) as client:
        listed = await client.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert sorted(tools) == sorted(expected)
    assert len(tools) == 43
    for tool in tools.values():
        assert tool.input_schema.get("additionalProperties") is False
    assert set(tools["service_action"].input_schema["properties"]["action"]["enum"]) == {"start", "stop", "restart"}
    assert "enum" in tools["process_signal"].input_schema["properties"]["signal_name"]
    assert "enum" in tools["terminal_signal"].input_schema["properties"]["signal_name"]


def test_doctor_and_provenance_are_machine_verifiable_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    fake_secret = cfg.path_key
    monkeypatch.setenv("RHMCP_BUILD_COMMIT", "a" * 40)
    monkeypatch.setenv("RHMCP_BUILD_REF", "refs/heads/hardening/audit-closure-20260913")
    commit, ref = get_build_provenance()
    assert commit == "a" * 40
    assert ref == "refs/heads/hardening/audit-closure-20260913"
    report = collect_report(cfg, sources={"PATH_KEY": "RHMCP"})
    encoded = json.dumps(report, sort_keys=True)
    assert fake_secret not in encoded
    assert report["configuration"]["public_url"].endswith("/mcp/[REDACTED]")
    assert report["build"]["commit"] == "a" * 40
    assert __version__ == "0.2.0a2"
