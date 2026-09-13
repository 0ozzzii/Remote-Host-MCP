from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import time

from mcp import Client
import pytest

from dsw_direct_mcp.config import Settings
from dsw_direct_mcp.executor import run_shell
from dsw_direct_mcp.jobs import (
    atomic_json_write,
    cancel_job,
    job_read,
    job_status,
    proc_identity_alive,
    recover_jobs,
    start_job,
)
from dsw_direct_mcp.mature_server import _terminal_status, build_server
from dsw_direct_mcp.system_helpers import process_info, process_signal, service_status, system_info
from dsw_direct_mcp.terminal import TerminalManager
from dsw_direct_mcp.transfer import begin_upload, upload_chunk


TERMINAL_JOB_STATES = {"completed", "failed", "canceled", "timed_out", "interrupted", "start_failed"}


def make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("DSW_MCP_PATH_KEY", "m" * 48)
    monkeypatch.setenv("DSW_MCP_PUBLIC_HOST", "direct.example.com")
    monkeypatch.setenv("DSW_MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("DSW_MCP_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("DSW_MCP_MAX_TIMEOUT_MS", "120000")
    monkeypatch.setenv("DSW_MCP_MAX_OUTPUT_BYTES", "4096")
    monkeypatch.setenv("DSW_MCP_MAX_FILE_CHUNK_BYTES", "16384")
    monkeypatch.setenv("DSW_MCP_MAX_TRANSFER_BYTES", str(1024 * 1024))
    monkeypatch.setenv("DSW_MCP_JOB_HEARTBEAT_SECONDS", "1")
    monkeypatch.setenv("DSW_MCP_JOB_LOG_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setenv("DSW_MCP_JOB_READ_MAX_BYTES", "65536")
    monkeypatch.setenv("DSW_MCP_TERMINAL_BUFFER_BYTES", "65536")
    monkeypatch.setenv("DSW_MCP_TERMINAL_READ_MAX_BYTES", "65536")
    return Settings.from_env()


def wait_job(job_id: str, settings: Settings, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    last = job_status(job_id, settings)
    while time.monotonic() < deadline:
        last = job_status(job_id, settings)
        if last.status in TERMINAL_JOB_STATES:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {last}")


def wait_job_state(job_id: str, settings: Settings, states: set[str], *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = job_status(job_id, settings)
        if current.status in states:
            return current
        time.sleep(0.05)
    raise AssertionError(f"job never reached {states}")


def wait_job_output_observed(job_id: str, settings: Settings, *, timeout: float = 4.0):
    deadline = time.monotonic() + timeout
    current = job_status(job_id, settings)
    while time.monotonic() < deadline:
        current = job_status(job_id, settings)
        if current.last_output_at is not None:
            return current
        time.sleep(0.05)
    raise AssertionError(f"job output was never observed by heartbeat: {current}")


def wait_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"path was not created: {path}")


def _proc_is_gone_or_zombie(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return True
    try:
        raw = stat.read_text(encoding="utf-8")
    except OSError:
        return True
    close = raw.rfind(")")
    if close < 0:
        return False
    fields = raw[close + 2 :].split()
    return bool(fields and fields[0] == "Z")


def wait_proc_gone(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _proc_is_gone_or_zombie(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"process still running: {pid}")


def test_mature_config_cloudflare_safe_limits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    assert settings.max_timeout_ms == 90_000
    assert settings.default_timeout_ms == 30_000
    assert settings.max_request_body_bytes >= 786_432
    assert settings.job_heartbeat_seconds == 1


@pytest.mark.asyncio
async def test_exec_clean_profile_output_hygiene_and_disconnect_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    clean = await run_shell('printf "%s|%s|%s" "$TERM" "${NO_COLOR:-}" "${PAGER:-}"', 2000, str(tmp_path), settings)
    assert clean.success
    assert clean.stdout == "dumb|1|cat"

    huge = await run_shell("python -c \"print('z'*20000)\"", 3000, str(tmp_path), settings)
    assert huge.success and huge.truncated
    assert huge.output_bytes_total > huge.output_bytes_returned
    assert huge.output_bytes_returned <= settings.max_output_bytes

    pid_file = tmp_path / "exec.pid"
    task = asyncio.create_task(run_shell(f"echo $$ > {pid_file}; sleep 30", 30_000, str(tmp_path), settings))
    await asyncio.to_thread(wait_path, pid_file)
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(wait_proc_gone, pid)


def test_upload_malformed_base64(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    payload = b"abc"
    begin = begin_upload(
        str(tmp_path / "bad.bin"),
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        0o644,
        False,
        settings,
    )
    with pytest.raises(ValueError, match="valid base64"):
        upload_chunk(begin.upload_id, 0, "%%%%", None, settings)


def test_job_success_nonzero_cursor_idempotency_and_no_command_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    command = "printf 'out-one\\nout-two\\n'; printf 'err-one\\n' >&2"
    first = start_job(command, str(tmp_path), None, "idem-success", settings)
    reused = start_job(command, str(tmp_path), None, "idem-success", settings)
    assert reused.job_id == first.job_id and reused.idempotent_reuse is True
    with pytest.raises(ValueError, match="different job request"):
        start_job("printf different", str(tmp_path), None, "idem-success", settings)

    done = wait_job(first.job_id, settings)
    assert done.status == "completed" and done.exit_code == 0
    job_dir = settings.state_dir / "jobs" / first.job_id
    assert not (job_dir / "command.bin").exists()
    durable_text = (job_dir / "metadata.json").read_text(encoding="utf-8") + (job_dir / "spec.json").read_text(encoding="utf-8")
    assert command not in durable_text
    assert done.command_sha256 == hashlib.sha256(command.encode()).hexdigest()

    part1 = job_read(first.job_id, 0, 0, 4, settings)
    assert part1.stdout == "out-"
    part2 = job_read(first.job_id, part1.next_stdout_offset, part1.next_stderr_offset, 65536, settings)
    assert "one" in part2.stdout and "err-one" in (part1.stderr + part2.stderr)
    assert part2.job_done

    failed = start_job("printf boom >&2; exit 7", str(tmp_path), None, None, settings)
    failed_status = wait_job(failed.job_id, settings)
    assert failed_status.status == "failed" and failed_status.exit_code == 7
    assert "boom" in job_read(failed.job_id, 0, 0, 65536, settings).stderr


def test_job_timeout_heartbeat_restart_observation_and_pid_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    running = start_job("printf heartbeat; sleep 3", str(tmp_path), None, "restart-observe", settings)
    observed = wait_job_state(running.job_id, settings, {"running"})
    assert observed.child_pid and observed.child_start_ticks is not None
    assert proc_identity_alive(observed.child_pid, observed.child_start_ticks)
    assert not proc_identity_alive(observed.child_pid, observed.child_start_ticks + 1)

    after_heartbeat = wait_job_output_observed(running.job_id, settings)
    assert after_heartbeat.heartbeat_at is not None
    assert after_heartbeat.last_output_at is not None

    settings_after_restart = Settings.from_env()
    recovery = recover_jobs(settings_after_restart)
    assert recovery["reconciled"] >= 1
    recovered = job_status(running.job_id, settings_after_restart)
    assert recovered.job_id == running.job_id
    assert recovered.child_pid == observed.child_pid
    assert recovered.status in {"running", "completed"}
    wait_job(running.job_id, settings_after_restart)

    timeout_job = start_job("sleep 30", str(tmp_path), 1000, None, settings)
    timed = wait_job(timeout_job.job_id, settings)
    assert timed.status == "timed_out" and timed.timed_out
    assert timed.terminated_by in {"SIGTERM", "SIGKILL"}


def test_job_cancel_kills_process_group_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    child_file = tmp_path / "job-child.pid"
    command = f"sleep 30 & child=$!; echo $child > {child_file}; wait $child"
    started = start_job(command, str(tmp_path), None, None, settings)
    wait_job_state(started.job_id, settings, {"running"})
    wait_path(child_file)
    child_pid = int(child_file.read_text(encoding="utf-8").strip())

    final = asyncio.run(cancel_job(started.job_id, settings))
    assert final.status == "canceled"
    wait_proc_gone(child_pid)


def test_job_recovery_never_replays_orphan_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    root = settings.state_dir / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "idempotency").mkdir(exist_ok=True)
    job_id = "a" * 32
    job_dir = root / job_id
    job_dir.mkdir()
    command = f"touch {tmp_path / 'MUST_NOT_EXIST'}"
    (job_dir / "command.bin").write_text(command, encoding="utf-8")
    now = int(time.time())
    atomic_json_write(
        job_dir / "metadata.json",
        {
            "job_id": job_id,
            "status": "starting",
            "cwd": str(tmp_path),
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "heartbeat_at": now,
            "last_output_at": None,
            "stdout_size": 0,
            "stderr_size": 0,
            "worker_pid": 99999999,
            "worker_start_ticks": 1,
            "child_pid": None,
            "child_start_ticks": None,
            "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "command_bytes": len(command.encode()),
            "timeout_ms": None,
            "idempotency_hash": None,
        },
    )
    atomic_json_write(job_dir / "spec.json", {"cwd": str(tmp_path)})

    recovery = recover_jobs(settings)
    assert recovery["interrupted"] >= 1
    assert job_status(job_id, settings).status == "interrupted"
    assert not (job_dir / "command.bin").exists()
    assert not (tmp_path / "MUST_NOT_EXIST").exists()


@pytest.mark.asyncio
async def test_terminal_real_pty_cwd_env_unicode_resize_screen_ctrl_c_and_concurrency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    manager = TerminalManager(settings)
    opened = await manager.open(str(tmp_path), 100, 30)
    terminal_id = opened.terminal_id
    sub = tmp_path / "sub"
    sub.mkdir()
    try:
        changed = await manager.exec(terminal_id, f"cd {sub}; export DSWD_TEST='hello世界'", 3000)
        assert changed.completed and changed.exit_code == 0
        check = await manager.exec(terminal_id, "printf '%s|%s' \"$PWD\" \"$DSWD_TEST\"", 3000)
        assert check.completed and check.exit_code == 0
        assert f"{sub}|hello世界" in check.output

        resized = await manager.resize(terminal_id, 132, 44)
        assert resized.success
        info = await manager.status(terminal_id)
        assert (info.cols, info.rows) == (132, 44)

        ansi = await manager.exec(terminal_id, "printf '\\033[2J\\033[HSCREEN_OK'", 3000)
        assert ansi.completed
        screen = await manager.screen_snapshot(terminal_id)
        assert "SCREEN_OK" in screen.display

        running = await manager.exec(terminal_id, "sleep 30", 100)
        assert running.still_running
        with pytest.raises(ValueError, match="already has a foreground"):
            await manager.exec(terminal_id, "printf should-not-run", 100)
        signaled = await manager.signal(terminal_id, "INT")
        assert signaled.success
        deadline = time.monotonic() + 5
        status = _terminal_status(manager, terminal_id)
        while time.monotonic() < deadline and status.foreground_completed is not True:
            await asyncio.sleep(0.05)
            status = _terminal_status(manager, terminal_id)
        assert status.foreground_completed is True
        assert status.foreground_exit_code is not None
    finally:
        try:
            await manager.close(terminal_id)
        except ValueError:
            pass


@pytest.mark.asyncio
async def test_terminal_ring_buffer_and_child_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    manager = TerminalManager(settings)
    opened = await manager.open(str(tmp_path), 80, 24)
    tid = opened.terminal_id
    try:
        result = await manager.exec(tid, "python -c \"print('x'*100000)\"", 5000)
        assert result.completed
        info = await manager.status(tid)
        assert info.buffer_end - info.buffer_start <= settings.terminal_buffer_bytes
        assert info.buffer_start > 0

        await manager.write(tid, "exit", True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = await manager.status(tid)
            if not info.alive:
                break
            await asyncio.sleep(0.05)
        assert info.alive is False
    finally:
        try:
            await manager.close(tid)
        except ValueError:
            pass


@pytest.mark.asyncio
async def test_terminal_idle_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    object.__setattr__(settings, "terminal_idle_seconds", 1)
    manager = TerminalManager(settings)
    opened = await manager.open(str(tmp_path), 80, 24)
    await asyncio.sleep(16.0)
    listed = await manager.list()
    assert all(item.terminal_id != opened.terminal_id for item in listed.terminals)


def test_process_identity_system_info_and_service_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    current = process_info(os.getpid())
    assert current.pid == os.getpid() and current.start_ticks > 0 and current.alive
    with pytest.raises(ValueError, match="identity mismatch"):
        process_signal(current.pid, current.start_ticks + 1, "TERM")

    info = system_info(settings)
    assert info.hostname and info.disk_total_bytes > 0 and info.state_dir == str(settings.state_dir)
    with pytest.raises(ValueError, match="unsupported characters"):
        service_status("ssh;touch /tmp/nope")
    with pytest.raises(ValueError, match="unsupported characters"):
        service_status("-ssh")


@pytest.mark.asyncio
async def test_mature_mcp_tool_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    server = build_server(settings)
    async with Client(server) as client:
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        expected = {
            "status", "list_directory", "path_info", "read_text_file", "read_file_chunk", "hash_file",
            "write_text_file", "make_directory", "move_path", "copy_path", "remove_path", "chmod_path",
            "upload_begin", "upload_chunk", "upload_status", "upload_finish", "upload_abort",
            "download_info", "download_chunk", "file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download",
            "host_capabilities", "wait_condition", "artifact_info", "artifact_preview", "artifact_bundle", "exec_argv", "snapshot_create", "snapshot_list", "snapshot_restore", "snapshot_delete", "lease_acquire", "lease_status", "lease_list", "lease_release", "inspect_paths", "file_diff", "apply_patch", "exec",
            "job_start", "job_status", "job_read", "job_cancel", "job_list", "job_cleanup",
            "terminal_open", "terminal_exec", "terminal_write", "terminal_read", "terminal_status",
            "terminal_screen", "terminal_resize", "terminal_signal", "terminal_list", "terminal_close",
            "process_list", "process_info", "process_signal", "service_status", "service_action", "system_info",
        }
        assert expected <= set(tools)
        assert tools["file_artifact"].output_schema is None
        assert all(tool.output_schema is not None for name, tool in tools.items() if name != "file_artifact")
        assert tools["job_start"].annotations.destructive_hint is True
        assert tools["job_start"].annotations.idempotent_hint is False
        assert tools["job_start"].annotations.open_world_hint is True
        assert tools["job_status"].annotations.read_only_hint is True
        assert tools["job_read"].annotations.read_only_hint is True
        assert tools["terminal_exec"].annotations.destructive_hint is True
        assert tools["terminal_exec"].annotations.idempotent_hint is False
        assert tools["process_signal"].annotations.destructive_hint is True
        assert tools["service_action"].annotations.destructive_hint is True
        assert tools["system_info"].annotations.read_only_hint is True

        sys_result = await client.call_tool("system_info", {})
        assert not sys_result.is_error
        assert sys_result.structured_content["hostname"]


def test_static_hygiene_no_fuzzy_kill_or_plaintext_command_logging() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "dsw_direct_mcp"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in sorted(src.glob("*.py")))
    forbidden_fuzzy_kill = "pkill" + " -f"
    assert forbidden_fuzzy_kill not in combined
    assert '"command": command' not in combined
    assert re.search(r"\bprint\s*\(\s*command\b", combined) is None
