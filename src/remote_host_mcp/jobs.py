from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .models import JobCleanupResult, JobListResult, JobReadResult, JobStartResult, JobStatusResult

_JOB_ID_LEN = 32
_TERMINAL_STATES = {"completed", "failed", "canceled", "timed_out", "interrupted", "start_failed"}
_SECRET_ENV_NAMES = {
    "RHMCP_PATH_KEY",
    "DSW_MCP_PATH_KEY",
    "CLOUDFLARED_TOKEN",
    "CF_TUNNEL_TOKEN",
    "TUNNEL_TOKEN",
    "CFD_TOKEN",
}


def _jobs_root(settings: Settings) -> Path:
    root = settings.state_dir / "jobs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "idempotency").mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(settings.state_dir, 0o700)
        os.chmod(root, 0o700)
        os.chmod(root / "idempotency", 0o700)
    except OSError:
        pass
    return root


def _validate_job_id(job_id: str) -> str:
    if len(job_id) != _JOB_ID_LEN:
        raise ValueError("Invalid job_id")
    try:
        int(job_id, 16)
    except ValueError as exc:
        raise ValueError("Invalid job_id") from exc
    return job_id.lower()


def _job_dir(job_id: str, settings: Settings) -> Path:
    return _jobs_root(settings) / _validate_job_id(job_id)


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def job_lock(job_dir: Path) -> Iterator[None]:
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _flock(job_dir / ".lock"):
        yield


@contextmanager
def _global_job_lock(settings: Settings) -> Iterator[None]:
    with _flock(_jobs_root(settings) / ".manager.lock"):
        yield


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"State file is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"State file is invalid: {path.name}")
    return value


def proc_start_ticks(pid: int) -> int | None:
    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2 :].split()
    # After the comm field, fields[0] is proc stat field 3. starttime is field 22.
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def proc_identity_alive(pid: int | None, start_ticks: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    current = proc_start_ticks(pid)
    if current is None:
        return False
    if start_ticks is None:
        return True
    return current == start_ticks


def proc_group_identity_alive(pid: int | None, start_ticks: int | None) -> bool:
    if not proc_identity_alive(pid, start_ticks):
        return False
    assert pid is not None
    try:
        return os.getpgid(pid) == pid
    except (OSError, ProcessLookupError):
        return False


def _safe_child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in _SECRET_ENV_NAMES:
            env.pop(key, None)
    # Durable jobs are non-interactive. Keep their output context-friendly without
    # applying this profile to real PTY sessions.
    env["TERM"] = "dumb"
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    env["SYSTEMD_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    return env


def _command_digest(command: str) -> tuple[str, int]:
    raw = command.encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _redact(text: str, settings: Settings) -> str:
    return text.replace(settings.path_key, "[REDACTED]") if settings.path_key else text


def _patch_meta(job_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    with job_lock(job_dir):
        meta = read_json(job_dir / "metadata.json")
        meta.update(updates)
        atomic_json_write(job_dir / "metadata.json", meta)
        return meta


def _active_job_count(settings: Settings) -> int:
    count = 0
    root = _jobs_root(settings)
    for child in root.iterdir():
        if not child.is_dir() or len(child.name) != _JOB_ID_LEN:
            continue
        try:
            status = job_status(child.name, settings)
        except ValueError:
            continue
        if status.status not in _TERMINAL_STATES:
            count += 1
    return count


def _idem_path(idempotency_key: str, settings: Settings) -> tuple[Path, str]:
    if not idempotency_key or len(idempotency_key.encode("utf-8")) > 256:
        raise ValueError("idempotency_key must be 1..256 UTF-8 bytes")
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return _jobs_root(settings) / "idempotency" / f"{digest}.json", digest


def _job_fingerprint(command_sha256: str, cwd: str, timeout_ms: int | None) -> str:
    raw = json.dumps(
        {"command_sha256": command_sha256, "cwd": cwd, "timeout_ms": timeout_ms},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def start_job(
    command: str,
    cwd: str | None,
    timeout_ms: int | None,
    idempotency_key: str | None,
    settings: Settings,
) -> JobStartResult:
    command_sha256, command_bytes = _command_digest(command)
    if not command.strip() or "\x00" in command or command_bytes > settings.max_command_bytes:
        raise ValueError("Command is empty, contains NUL, or exceeds RHMCP_MAX_COMMAND_BYTES")
    if cwd is None:
        try:
            safe_cwd = settings.resolve_allowed_path(None, default=Path.cwd())
        except ValueError:
            safe_cwd = settings.allowed_roots[0]
    else:
        safe_cwd = settings.resolve_allowed_path(cwd)
    if not safe_cwd.exists() or not safe_cwd.is_dir():
        raise ValueError("cwd is not an existing allowed directory")
    if timeout_ms is not None and (timeout_ms < 1000 or timeout_ms > settings.job_max_timeout_ms):
        raise ValueError(f"timeout_ms must be null or between 1000 and {settings.job_max_timeout_ms}")

    fingerprint = _job_fingerprint(command_sha256, str(safe_cwd), timeout_ms)
    with _global_job_lock(settings):
        idem_file: Path | None = None
        idem_hash: str | None = None
        if idempotency_key is not None:
            idem_file, idem_hash = _idem_path(idempotency_key, settings)
            if idem_file.exists():
                mapping = read_json(idem_file)
                existing_id = str(mapping.get("job_id", ""))
                if mapping.get("fingerprint") != fingerprint:
                    raise ValueError("idempotency_key was already used for a different job request")
                existing_dir = _job_dir(existing_id, settings)
                if existing_dir.exists():
                    existing = job_status(existing_id, settings)
                    return JobStartResult(
                        job_id=existing.job_id,
                        status=existing.status,
                        cwd=existing.cwd,
                        command_sha256=existing.command_sha256,
                        command_bytes=existing.command_bytes,
                        created_at=existing.created_at,
                        idempotent_reuse=True,
                    )
                raise ValueError(
                    "idempotency_key refers to a cleaned durable job and cannot be replayed"
                )

        if _active_job_count(settings) >= settings.max_jobs:
            raise ValueError(f"Active job limit reached ({settings.max_jobs})")

        job_id = secrets.token_hex(16)
        job_dir = _job_dir(job_id, settings)
        job_dir.mkdir(mode=0o700)
        now = int(time.time())
        meta: dict[str, Any] = {
            "job_id": job_id,
            "status": "starting",
            "cwd": str(safe_cwd),
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "heartbeat_at": now,
            "last_output_at": None,
            "stdout_size": 0,
            "stderr_size": 0,
            "worker_pid": None,
            "worker_start_ticks": None,
            "child_pid": None,
            "child_start_ticks": None,
            "command_sha256": command_sha256,
            "command_bytes": command_bytes,
            "timeout_ms": timeout_ms,
            "idempotency_hash": idem_hash,
        }
        command_path = job_dir / "command.bin"
        spec_path = job_dir / "spec.json"
        fd = os.open(command_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(command.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        atomic_json_write(
            spec_path,
            {
                "job_id": job_id,
                "cwd": str(safe_cwd),
                "timeout_ms": timeout_ms,
                "kill_grace_ms": settings.kill_grace_ms,
                "max_log_bytes": settings.job_log_max_bytes,
                "heartbeat_seconds": settings.job_heartbeat_seconds,
            },
        )
        atomic_json_write(job_dir / "metadata.json", meta)
        if idem_file is not None:
            atomic_json_write(idem_file, {"job_id": job_id, "fingerprint": fingerprint, "created_at": now})

        try:
            worker = subprocess.Popen(
                [sys.executable, "-m", "remote_host_mcp.job_worker", "--job-dir", str(job_dir)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_safe_child_env(),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            command_path.unlink(missing_ok=True)
            result = {
                "status": "start_failed",
                "exit_code": None,
                "timed_out": False,
                "terminated_by": None,
                "started_at": None,
                "completed_at": int(time.time()),
                "duration_ms": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "error": f"worker launch failed: {type(exc).__name__}",
            }
            atomic_json_write(job_dir / "result.json", result)
            _patch_meta(job_dir, {"status": "start_failed", "completed_at": result["completed_at"]})
            return JobStartResult(
                job_id=job_id,
                status="start_failed",
                cwd=str(safe_cwd),
                command_sha256=command_sha256,
                command_bytes=command_bytes,
                created_at=now,
            )

        _patch_meta(
            job_dir,
            {
                "worker_pid": worker.pid,
                "worker_start_ticks": proc_start_ticks(worker.pid),
                "heartbeat_at": int(time.time()),
            },
        )
        return JobStartResult(
            job_id=job_id,
            status="starting",
            cwd=str(safe_cwd),
            command_sha256=command_sha256,
            command_bytes=command_bytes,
            created_at=now,
        )


def _result_or_none(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / "result.json"
    if not path.exists():
        return None
    try:
        return read_json(path)
    except ValueError:
        return None


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def job_status(job_id: str, settings: Settings) -> JobStatusResult:
    job_dir = _job_dir(job_id, settings)
    if not job_dir.exists() or not (job_dir / "metadata.json").exists():
        raise ValueError("Unknown job_id")
    with job_lock(job_dir):
        meta = read_json(job_dir / "metadata.json")
        result = _result_or_none(job_dir)
        cancel_requested = (job_dir / "cancel.request").exists()

        if result is not None:
            status_value = str(result.get("status", meta.get("status", "failed")))
        else:
            child_alive = proc_identity_alive(meta.get("child_pid"), meta.get("child_start_ticks"))
            worker_alive = proc_identity_alive(meta.get("worker_pid"), meta.get("worker_start_ticks"))
            if child_alive and worker_alive:
                status_value = "cancel_requested" if cancel_requested else "running"
            elif child_alive and not worker_alive:
                # The command still exists, but its durable worker/heartbeat owner is gone.
                # Do not replay it. Keep it cancellable through the recorded process identity.
                status_value = "cancel_requested" if cancel_requested else "lost"
            elif worker_alive:
                status_value = "cancel_requested" if cancel_requested else "starting"
            else:
                status_value = "interrupted"
                if meta.get("status") not in _TERMINAL_STATES:
                    meta["status"] = "interrupted"
                    meta["completed_at"] = int(time.time())
                    atomic_json_write(job_dir / "metadata.json", meta)
                # If the server died between persisting command.bin and launching the
                # worker, never retain or replay that arbitrary command on recovery.
                try:
                    (job_dir / "command.bin").unlink(missing_ok=True)
                except OSError:
                    pass

        completed_at = result.get("completed_at") if result else meta.get("completed_at")
        return JobStatusResult(
            job_id=job_id,
            status=status_value,
            cwd=str(meta["cwd"]),
            created_at=int(meta["created_at"]),
            started_at=(result.get("started_at") if result else meta.get("started_at")),
            completed_at=completed_at,
            heartbeat_at=meta.get("heartbeat_at"),
            last_output_at=meta.get("last_output_at"),
            worker_pid=meta.get("worker_pid"),
            worker_start_ticks=meta.get("worker_start_ticks"),
            child_pid=meta.get("child_pid"),
            child_start_ticks=meta.get("child_start_ticks"),
            exit_code=(result.get("exit_code") if result else None),
            timed_out=bool(result.get("timed_out", False)) if result else False,
            cancel_requested=cancel_requested,
            terminated_by=(result.get("terminated_by") if result else None),
            duration_ms=(result.get("duration_ms") if result else None),
            stdout_bytes=_log_size(job_dir / "stdout.log"),
            stderr_bytes=_log_size(job_dir / "stderr.log"),
            stdout_truncated=(bool(result.get("stdout_truncated", False)) if result else (job_dir / "stdout.truncated").exists()),
            stderr_truncated=(bool(result.get("stderr_truncated", False)) if result else (job_dir / "stderr.truncated").exists()),
            command_sha256=str(meta["command_sha256"]),
            command_bytes=int(meta["command_bytes"]),
            error=(result.get("error") if result else None),
        )


def _read_log(path: Path, offset: int, max_bytes: int) -> tuple[bytes, int, bool]:
    if offset < 0:
        raise ValueError("Log offset must be >= 0")
    size = _log_size(path)
    if offset > size:
        raise ValueError("Log offset is beyond currently retained data")
    if not path.exists():
        return b"", offset, True
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(max_bytes)
    next_offset = offset + len(data)
    return data, next_offset, next_offset >= size


def job_read(
    job_id: str,
    stdout_offset: int,
    stderr_offset: int,
    max_bytes: int,
    settings: Settings,
) -> JobReadResult:
    if max_bytes < 1 or max_bytes > settings.job_read_max_bytes:
        raise ValueError(f"max_bytes must be between 1 and {settings.job_read_max_bytes}")
    status = job_status(job_id, settings)
    job_dir = _job_dir(job_id, settings)
    stdout_b, next_stdout, stdout_end = _read_log(job_dir / "stdout.log", stdout_offset, max_bytes)
    stderr_b, next_stderr, stderr_end = _read_log(job_dir / "stderr.log", stderr_offset, max_bytes)
    return JobReadResult(
        job_id=job_id,
        status=status.status,
        stdout=_redact(stdout_b.decode("utf-8", errors="replace"), settings),
        stderr=_redact(stderr_b.decode("utf-8", errors="replace"), settings),
        stdout_offset=stdout_offset,
        stderr_offset=stderr_offset,
        next_stdout_offset=next_stdout,
        next_stderr_offset=next_stderr,
        stdout_at_end=stdout_end,
        stderr_at_end=stderr_end,
        job_done=status.status in _TERMINAL_STATES,
        stdout_truncated=status.stdout_truncated,
        stderr_truncated=status.stderr_truncated,
    )


async def cancel_job(job_id: str, settings: Settings) -> JobStatusResult:
    status = job_status(job_id, settings)
    if status.status in _TERMINAL_STATES:
        return status
    job_dir = _job_dir(job_id, settings)
    request_path = job_dir / "cancel.request"
    fd = os.open(request_path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)

    with job_lock(job_dir):
        meta = read_json(job_dir / "metadata.json")
    pid = meta.get("child_pid")
    start_ticks = meta.get("child_start_ticks")
    if proc_group_identity_alive(pid, start_ticks):
        # start_new_session=True makes the job leader PID its process-group ID.
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + settings.kill_grace_ms / 1000
    while time.monotonic() < deadline:
        current = job_status(job_id, settings)
        if current.status in _TERMINAL_STATES:
            return current
        with job_lock(job_dir):
            meta = read_json(job_dir / "metadata.json")
        if not proc_group_identity_alive(meta.get("child_pid"), meta.get("child_start_ticks")):
            break
        await asyncio.sleep(0.1)

    with job_lock(job_dir):
        meta = read_json(job_dir / "metadata.json")
    if proc_group_identity_alive(meta.get("child_pid"), meta.get("child_start_ticks")):
        try:
            os.killpg(int(meta["child_pid"]), signal.SIGKILL)
        except ProcessLookupError:
            pass

    for _ in range(20):
        await asyncio.sleep(0.1)
        current = job_status(job_id, settings)
        if current.status in _TERMINAL_STATES:
            return current
    return job_status(job_id, settings)


def list_jobs(limit: int, settings: Settings) -> JobListResult:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    rows: list[JobStatusResult] = []
    for child in _jobs_root(settings).iterdir():
        if not child.is_dir() or len(child.name) != _JOB_ID_LEN:
            continue
        try:
            rows.append(job_status(child.name, settings))
        except ValueError:
            continue
    rows.sort(key=lambda item: item.created_at, reverse=True)
    return JobListResult(jobs=rows[:limit], truncated=len(rows) > limit)


def cleanup_job(job_id: str, settings: Settings) -> JobCleanupResult:
    job_dir = _job_dir(job_id, settings)
    if not job_dir.exists():
        return JobCleanupResult(success=True, job_id=job_id, removed=False)
    status = job_status(job_id, settings)
    if status.status not in _TERMINAL_STATES:
        raise ValueError("Refusing to clean up a job that is still active")
    with _global_job_lock(settings):
        meta = read_json(job_dir / "metadata.json")
        idem_hash = meta.get("idempotency_hash")
        if isinstance(idem_hash, str) and len(idem_hash) == 64:
            mapping = _jobs_root(settings) / "idempotency" / f"{idem_hash}.json"
            if mapping.exists():
                try:
                    stored = read_json(mapping)
                    if stored.get("job_id") == job_id:
                        stored["tombstone"] = True
                        stored["cleaned_at"] = int(time.time())
                        atomic_json_write(mapping, stored)
                except ValueError:
                    pass
        shutil.rmtree(job_dir)
    return JobCleanupResult(success=True, job_id=job_id, removed=True)


def recover_jobs(settings: Settings) -> dict[str, int]:
    """Reconcile persisted job state without ever replaying arbitrary shell.

    Active workers/children are merely observed through PID+starttime identity.
    Dead in-flight jobs become interrupted, orphan command.bin files are removed,
    and completed jobs older than the configured retention window are cleaned.
    """
    now = int(time.time())
    reconciled = 0
    interrupted = 0
    removed = 0
    root = _jobs_root(settings)
    for child in list(root.iterdir()):
        if not child.is_dir() or len(child.name) != _JOB_ID_LEN:
            continue
        try:
            status = job_status(child.name, settings)
        except ValueError:
            continue
        reconciled += 1
        if status.status == "interrupted":
            interrupted += 1
        if status.status in _TERMINAL_STATES:
            finished_at = status.completed_at or status.created_at
            if now - finished_at >= settings.job_retention_seconds:
                try:
                    cleanup_job(child.name, settings)
                    removed += 1
                except ValueError:
                    pass
    return {"reconciled": reconciled, "interrupted": interrupted, "removed": removed}
