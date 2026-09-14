from __future__ import annotations

import asyncio
import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from remote_host_mcp.agent_ops import (
    LineEdit,
    apply_line_patch,
    artifact_bundle,
    artifact_info,
    artifact_preview,
    file_diff,
    host_capabilities,
    inspect_paths,
    lease_acquire,
    lease_list,
    lease_release,
    lease_status,
    run_argv,
    snapshot_create,
    snapshot_delete,
    snapshot_list,
    snapshot_restore,
    wait_condition,
)
from remote_host_mcp.config import Settings


def make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("RHMCP_AUTH_MODE", "capability")
    monkeypatch.setenv("RHMCP_PATH_KEY", "a" * 48)
    monkeypatch.setenv("RHMCP_PUBLIC_HOST", "host.example.com")
    monkeypatch.setenv("RHMCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("RHMCP_STATE_DIR", str(tmp_path / ".state"))
    return Settings.from_env()


def test_capabilities_are_bounded_and_secret_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("RHMCP_PATH_KEY", "a" * 48)
    result = host_capabilities(settings)
    assert str(tmp_path) in result.allowed_roots
    assert result.max_sync_timeout_ms == settings.max_timeout_ms
    assert result.max_inline_artifact_bytes == 8 * 1024 * 1024
    assert "a" * 48 not in result.model_dump_json()


@pytest.mark.asyncio
async def test_exec_argv_does_not_invoke_shell_or_inherit_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    marker = tmp_path / "should-not-exist"
    literal = f"$(touch {marker})"
    result = await run_argv(
        ["python", "-c", "import sys; print(sys.argv[1])", literal],
        cwd=str(tmp_path),
        timeout_ms=5000,
        stdin_text=None,
        env_allowlist=[],
        settings=settings,
    )
    assert result.success is True
    assert literal in result.stdout
    assert not marker.exists()

    rejected = await run_argv(
        ["python", "-c", "print('x')"],
        cwd=str(tmp_path),
        timeout_ms=5000,
        stdin_text=None,
        env_allowlist=["RHMCP_PATH_KEY"],
        settings=settings,
    )
    assert rejected.success is False
    assert rejected.error is not None
    assert rejected.error.code == "INVALID_ENV_ALLOWLIST"


@pytest.mark.asyncio
async def test_wait_condition_file_and_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    target = tmp_path / "log.txt"

    async def writer() -> None:
        await asyncio.sleep(0.05)
        target.write_text("started\nREADY\n", encoding="utf-8")

    task = asyncio.create_task(writer())
    exists = await wait_condition(
        "file_exists",
        path=str(target),
        pid=None,
        expected_start_ticks=None,
        size_bytes=None,
        job_id=None,
        host=None,
        port=None,
        pattern=None,
        timeout_ms=2000,
        poll_ms=100,
        log_tail_bytes=4096,
        settings=settings,
    )
    await task
    assert exists.satisfied is True

    contains = await wait_condition(
        "log_contains",
        path=str(target),
        pid=None,
        expected_start_ticks=None,
        size_bytes=None,
        job_id=None,
        host=None,
        port=None,
        pattern="READY",
        timeout_ms=0,
        poll_ms=100,
        log_tail_bytes=4096,
        settings=settings,
    )
    assert contains.satisfied is True


def test_artifact_info_preview_and_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    text = tmp_path / "result.csv"
    text.write_text("x,y\n1,2\n", encoding="utf-8")
    binary = tmp_path / "report.pdf"
    binary.write_bytes(b"%PDF-1.7\nsynthetic\n")

    info = artifact_info(str(binary), True, settings)
    assert info.mime_type == "application/pdf"
    assert info.sha256 == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert info.inline_eligible is True

    preview = artifact_preview(str(text), 1024, settings)
    assert preview.binary is False
    assert "x,y" in (preview.preview or "")

    bundle_path = tmp_path / "artifacts.zip"
    bundle = artifact_bundle([str(text), str(binary)], str(bundle_path), 20, 1024 * 1024, settings)
    assert bundle.entries == 2
    assert bundle_path.exists()
    with zipfile.ZipFile(bundle_path) as archive:
        assert sorted(archive.namelist()) == ["report.pdf", "result.csv"]


def test_snapshot_restore_and_delete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config = project / "config.ini"
    config.write_text("mode=old\n", encoding="utf-8")
    nested = project / "sub"
    nested.mkdir()
    (nested / "data.txt").write_text("before\n", encoding="utf-8")

    snap = snapshot_create([str(project)], "before-edit", 8 * 1024 * 1024, 100, settings)
    assert snapshot_list(settings).snapshots[0].snapshot_id == snap.snapshot_id

    config.write_text("mode=new\n", encoding="utf-8")
    (nested / "data.txt").unlink()
    (project / "extra.txt").write_text("extra\n", encoding="utf-8")

    restored = snapshot_restore(snap.snapshot_id, settings)
    assert restored.restored == [str(project)]
    assert config.read_text(encoding="utf-8") == "mode=old\n"
    assert (nested / "data.txt").read_text(encoding="utf-8") == "before\n"
    assert not (project / "extra.txt").exists()

    deleted = snapshot_delete(snap.snapshot_id, settings)
    assert deleted.deleted is True
    assert snapshot_list(settings).snapshots == []


def test_snapshot_rejects_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    target = root / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        snapshot_create([str(root)], None, 1024 * 1024, 100, settings)


def test_leases_are_exclusive_and_releasable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    first = lease_acquire("project:model-a", "agent-1", 60, settings)
    assert first.acquired is True and first.lease_id
    second = lease_acquire("project:model-a", "agent-2", 60, settings)
    assert second.acquired is False
    assert second.holder == "agent-1"
    assert lease_status("project:model-a", settings).active is True
    assert len(lease_list(settings).leases) == 1
    released = lease_release(first.lease_id, settings)
    assert released.released is True
    assert lease_status("project:model-a", settings).active is False


def test_inspect_diff_and_cas_patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    path = tmp_path / "config.txt"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    inspected = inspect_paths([str(path), str(tmp_path / "missing")], include_sha256=True, max_text_bytes=1024, hash_max_bytes=1024 * 1024, settings=settings)
    assert inspected.items[0].ok is True
    assert inspected.items[0].sha256
    assert inspected.items[1].ok is False

    proposed = "alpha\nBETA\ngamma\n"
    diff = file_diff(str(path), proposed, max_file_bytes=1024 * 1024, max_diff_bytes=65536, settings=settings)
    assert "-beta" in diff.diff
    assert "+BETA" in diff.diff

    write = apply_line_patch(
        str(path),
        diff.current_sha256,
        [LineEdit(start_line=2, delete_lines=1, replacement="BETA\n")],
        mode=0o644,
        max_file_bytes=1024 * 1024,
        settings=settings,
    )
    assert write.success is True
    assert path.read_text(encoding="utf-8") == proposed

    with pytest.raises(ValueError, match="changed"):
        apply_line_patch(
            str(path),
            diff.current_sha256,
            [LineEdit(start_line=2, delete_lines=1, replacement="again\n")],
            mode=0o644,
            max_file_bytes=1024 * 1024,
            settings=settings,
        )
