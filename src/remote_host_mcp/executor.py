from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .models import ExecResult, ToolErrorInfo

logger = logging.getLogger("remote_host_mcp.exec")


@dataclass(slots=True)
class _Budget:
    limit: int
    used: int = 0
    seen: int = 0
    truncated: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def keep(self, data: bytes) -> bytes:
        async with self.lock:
            self.seen += len(data)
            remaining = max(0, self.limit - self.used)
            if remaining <= 0:
                if data:
                    self.truncated = True
                return b""
            chunk = data[:remaining]
            self.used += len(chunk)
            if len(data) > remaining:
                self.truncated = True
            return chunk


async def _drain(stream: asyncio.StreamReader | None, budget: _Budget) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        data = await stream.read(8192)
        if not data:
            break
        kept = await budget.keep(data)
        if kept:
            chunks.append(kept)
    return b"".join(chunks)


def _safe_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    # Preserve useful host/runtime variables while excluding MCP/Tunnel credentials.
    for key in list(env):
        upper = key.upper()
        if upper in {
            "RHMCP_PATH_KEY",
            "DSW_MCP_PATH_KEY",
            "CLOUDFLARED_TOKEN",
            "CF_TUNNEL_TOKEN",
            "TUNNEL_TOKEN",
            "CFD_TOKEN",
        }:
            env.pop(key, None)
    # Short non-interactive exec should be context-friendly. PTY sessions deliberately
    # do not use this profile and keep TERM=xterm-256color.
    env["TERM"] = "dumb"
    env["PAGER"] = "cat"
    env["GIT_PAGER"] = "cat"
    env["SYSTEMD_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    return env


def _redact(text: str, settings: Settings) -> str:
    if settings.path_key:
        text = text.replace(settings.path_key, "[REDACTED]")
    return text


async def _terminate_group(proc: asyncio.subprocess.Process, grace_ms: int) -> str | None:
    if proc.returncode is not None:
        return None
    terminated_by = "SIGTERM"
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return terminated_by
    try:
        await asyncio.wait_for(proc.wait(), grace_ms / 1000)
        return terminated_by
    except asyncio.TimeoutError:
        terminated_by = "SIGKILL"
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except ProcessLookupError:
        pass
    return terminated_by


async def run_shell(command: str, timeout_ms: int | None, cwd: str | None, settings: Settings) -> ExecResult:
    started = time.monotonic()
    command_bytes = command.encode("utf-8")

    try:
        if cwd is None:
            try:
                safe_cwd = settings.resolve_allowed_path(None, default=Path.cwd())
            except ValueError:
                safe_cwd = settings.allowed_roots[0]
        else:
            safe_cwd = settings.resolve_allowed_path(cwd)
    except ValueError:
        return ExecResult(
            success=False,
            duration_ms=0,
            cwd=str(Path.cwd()),
            error=ToolErrorInfo(code="INVALID_CWD", message="cwd is outside RHMCP_ALLOWED_ROOTS"),
        )

    if not command.strip() or "\x00" in command or len(command_bytes) > settings.max_command_bytes:
        return ExecResult(
            success=False,
            duration_ms=0,
            cwd=str(safe_cwd),
            error=ToolErrorInfo(code="INVALID_COMMAND", message="Command is empty, contains NUL, or exceeds the UTF-8 byte limit"),
        )

    timeout = settings.default_timeout_ms if timeout_ms is None else timeout_ms
    if timeout < 1000 or timeout > settings.max_timeout_ms:
        return ExecResult(
            success=False,
            duration_ms=0,
            cwd=str(safe_cwd),
            error=ToolErrorInfo(
                code="INVALID_TIMEOUT",
                message=f"timeout_ms must be between 1000 and {settings.max_timeout_ms}; use job_start for longer work",
            ),
        )

    shell = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
    budget = _Budget(settings.max_output_bytes)
    proc: asyncio.subprocess.Process | None = None
    stdout_task: asyncio.Task[bytes] | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    timed_out = False
    terminated_by: str | None = None

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            executable=shell,
            cwd=str(safe_cwd),
            env=_safe_env(settings),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_task = asyncio.create_task(_drain(proc.stdout, budget))
        stderr_task = asyncio.create_task(_drain(proc.stderr, budget))

        try:
            await asyncio.wait_for(proc.wait(), timeout / 1000)
        except asyncio.TimeoutError:
            timed_out = True
            terminated_by = await _terminate_group(proc, settings.kill_grace_ms)
        except asyncio.CancelledError:
            # MCP/HTTP cancellation must not strand a still-running short exec. This
            # does not make an already-completed-but-unacknowledged shell replay-safe;
            # callers must never blindly replay arbitrary shell after ambiguity.
            terminated_by = await _terminate_group(proc, settings.kill_grace_ms)
            if stdout_task is not None and stderr_task is not None:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            logger.warning(
                json.dumps(
                    {
                        "event": "exec_cancelled",
                        "command_sha256": hashlib.sha256(command_bytes).hexdigest()[:16],
                        "command_bytes": len(command_bytes),
                        "cwd": str(safe_cwd),
                        "terminated_by": terminated_by,
                    },
                    separators=(",", ":"),
                )
            )
            raise

        assert stdout_task is not None and stderr_task is not None
        stdout_b, stderr_b = await asyncio.gather(stdout_task, stderr_task)
        stdout = _redact(stdout_b.decode("utf-8", errors="replace"), settings)
        stderr = _redact(stderr_b.decode("utf-8", errors="replace"), settings)
        duration_ms = int((time.monotonic() - started) * 1000)
        success = (not timed_out) and proc.returncode == 0
        error = None
        if timed_out:
            error = ToolErrorInfo(code="TIMEOUT", message="Command exceeded timeout and its process group was terminated")
        elif proc.returncode != 0:
            error = ToolErrorInfo(code="NONZERO_EXIT", message=f"Command exited with code {proc.returncode}")

        logger.info(
            json.dumps(
                {
                    "event": "exec",
                    "command_sha256": hashlib.sha256(command_bytes).hexdigest()[:16],
                    "command_bytes": len(command_bytes),
                    "cwd": str(safe_cwd),
                    "duration_ms": duration_ms,
                    "exit_code": proc.returncode,
                    "timed_out": timed_out,
                    "truncated": budget.truncated,
                    "output_bytes_total": budget.seen,
                    "output_bytes_returned": budget.used,
                },
                separators=(",", ":"),
            )
        )

        return ExecResult(
            success=success,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            terminated_by=terminated_by,
            truncated=budget.truncated,
            output_bytes_returned=budget.used,
            output_bytes_total=budget.seen,
            cwd=str(safe_cwd),
            error=error,
        )
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError) as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning("exec setup failed: %s", type(exc).__name__)
        if proc is not None and proc.returncode is None:
            await _terminate_group(proc, settings.kill_grace_ms)
        return ExecResult(
            success=False,
            duration_ms=duration_ms,
            output_bytes_returned=budget.used,
            output_bytes_total=budget.seen,
            cwd=str(safe_cwd),
            error=ToolErrorInfo(code="EXEC_SETUP_FAILED", message="Shell process could not be started"),
        )
