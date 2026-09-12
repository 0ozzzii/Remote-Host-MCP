from __future__ import annotations

import argparse
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .jobs import atomic_json_write, job_lock, proc_start_ticks, read_json


def _patch_meta(job_dir: Path, updates: dict[str, object]) -> None:
    with job_lock(job_dir):
        meta = read_json(job_dir / "metadata.json")
        meta.update(updates)
        atomic_json_write(job_dir / "metadata.json", meta)


def _write_result(job_dir: Path, payload: dict[str, object]) -> None:
    with job_lock(job_dir):
        atomic_json_write(job_dir / "result.json", payload)
        meta = read_json(job_dir / "metadata.json")
        meta["status"] = payload["status"]
        meta["started_at"] = payload.get("started_at")
        meta["completed_at"] = payload.get("completed_at")
        atomic_json_write(job_dir / "metadata.json", meta)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _drain_stream(stream: BinaryIO, path: Path, limit: int, marker: Path, state: dict[str, bool]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    kept = 0
    truncated = False
    try:
        while True:
            data = stream.read(65536)
            if not data:
                break
            remaining = max(0, limit - kept)
            if remaining:
                chunk = data[:remaining]
                _write_all(fd, chunk)
                kept += len(chunk)
            if len(data) > remaining:
                truncated = True
                if not marker.exists():
                    marker.write_text("truncated\n", encoding="utf-8")
                    try:
                        os.chmod(marker, 0o600)
                    except OSError:
                        pass
        os.fsync(fd)
    finally:
        os.close(fd)
        try:
            stream.close()
        except OSError:
            pass
        state["truncated"] = truncated


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _terminate_process_group(proc: subprocess.Popen[bytes], grace_ms: int) -> str | None:
    if proc.poll() is not None:
        return None
    terminated_by = "SIGTERM"
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return terminated_by

    deadline = time.monotonic() + grace_ms / 1000
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return terminated_by
        time.sleep(0.05)

    terminated_by = "SIGKILL"
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return terminated_by


def _record_start_failure(job_dir: Path, started_at: int | None, started_mono: float, exc: BaseException) -> None:
    completed_at = int(time.time())
    _write_result(
        job_dir,
        {
            "status": "start_failed",
            "exit_code": None,
            "timed_out": False,
            "terminated_by": None,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": max(0, int((time.monotonic() - started_mono) * 1000)),
            "stdout_truncated": False,
            "stderr_truncated": False,
            "error": f"job worker failed: {type(exc).__name__}",
        },
    )


def run(job_dir: Path) -> int:
    started_mono = time.monotonic()
    started_at: int | None = None
    command_path = job_dir / "command.bin"
    try:
        spec = read_json(job_dir / "spec.json")
        command = command_path.read_text(encoding="utf-8")
        # The arbitrary command is intentionally ephemeral. Durable state stores only
        # its hash/byte length, never the long-term plaintext command.
        command_path.unlink(missing_ok=True)
        cwd = str(spec["cwd"])
        timeout_ms = spec.get("timeout_ms")
        kill_grace_ms = int(spec["kill_grace_ms"])
        max_log_bytes = int(spec["max_log_bytes"])
        heartbeat_seconds = max(1, int(spec.get("heartbeat_seconds", 5)))

        if (job_dir / "cancel.request").exists():
            now = int(time.time())
            _write_result(
                job_dir,
                {
                    "status": "canceled",
                    "exit_code": None,
                    "timed_out": False,
                    "terminated_by": None,
                    "started_at": None,
                    "completed_at": now,
                    "duration_ms": 0,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "error": None,
                },
            )
            return 0

        shell = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
        proc = subprocess.Popen(
            command,
            executable=shell,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        started_at = int(time.time())
        _patch_meta(
            job_dir,
            {
                "status": "running",
                "started_at": started_at,
                "heartbeat_at": started_at,
                "child_pid": proc.pid,
                "child_start_ticks": proc_start_ticks(proc.pid),
            },
        )

        stdout_state = {"truncated": False}
        stderr_state = {"truncated": False}
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        assert proc.stdout is not None and proc.stderr is not None
        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(proc.stdout, stdout_path, max_log_bytes, job_dir / "stdout.truncated", stdout_state),
            name="rhmcp-job-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(proc.stderr, stderr_path, max_log_bytes, job_dir / "stderr.truncated", stderr_state),
            name="rhmcp-job-stderr",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        canceled = False
        terminated_by: str | None = None
        deadline = None if timeout_ms is None else time.monotonic() + int(timeout_ms) / 1000
        next_heartbeat = time.monotonic()
        last_sizes = (0, 0)
        last_output_at: int | None = None

        while proc.poll() is None:
            now_mono = time.monotonic()
            if now_mono >= next_heartbeat:
                stdout_size = _file_size(stdout_path)
                stderr_size = _file_size(stderr_path)
                now_epoch = int(time.time())
                sizes = (stdout_size, stderr_size)
                if sizes != last_sizes:
                    last_output_at = now_epoch
                    last_sizes = sizes
                _patch_meta(
                    job_dir,
                    {
                        "heartbeat_at": now_epoch,
                        "last_output_at": last_output_at,
                        "stdout_size": stdout_size,
                        "stderr_size": stderr_size,
                    },
                )
                next_heartbeat = now_mono + heartbeat_seconds

            if (job_dir / "cancel.request").exists():
                canceled = True
                terminated_by = _terminate_process_group(proc, kill_grace_ms)
                break
            if deadline is not None and now_mono >= deadline:
                timed_out = True
                terminated_by = _terminate_process_group(proc, kill_grace_ms)
                break
            time.sleep(0.1)

        try:
            exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminated_by = _terminate_process_group(proc, kill_grace_ms) or terminated_by
            exit_code = proc.wait()

        # Cancellation may race with process exit: cancel_job can create the durable
        # request marker and signal the process group before this worker's polling loop
        # gets another iteration. The marker is authoritative for classifying that
        # requested termination and prevents it from being mislabeled as a failure.
        if not timed_out and (job_dir / "cancel.request").exists():
            canceled = True
            if terminated_by is None and exit_code is not None and exit_code < 0:
                try:
                    terminated_by = signal.Signals(-exit_code).name
                except ValueError:
                    terminated_by = "SIGNAL"

        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        completed_at = int(time.time())
        duration_ms = max(0, int((time.monotonic() - started_mono) * 1000))
        final_stdout = _file_size(stdout_path)
        final_stderr = _file_size(stderr_path)
        if (final_stdout, final_stderr) != last_sizes:
            last_output_at = completed_at
        _patch_meta(
            job_dir,
            {
                "heartbeat_at": completed_at,
                "last_output_at": last_output_at,
                "stdout_size": final_stdout,
                "stderr_size": final_stderr,
            },
        )

        if canceled:
            status = "canceled"
        elif timed_out:
            status = "timed_out"
        elif exit_code == 0:
            status = "completed"
        else:
            status = "failed"

        _write_result(
            job_dir,
            {
                "status": status,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "terminated_by": terminated_by,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "stdout_truncated": bool(stdout_state["truncated"]),
                "stderr_truncated": bool(stderr_state["truncated"]),
                "error": None,
            },
        )
        return 0
    except BaseException as exc:
        try:
            command_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _record_start_failure(job_dir, started_at, started_mono, exc)
        except Exception:
            pass
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="dsw-direct-job-worker")
    parser.add_argument("--job-dir", required=True)
    args = parser.parse_args()
    raise SystemExit(run(Path(args.job_dir).resolve()))


if __name__ == "__main__":
    main()
