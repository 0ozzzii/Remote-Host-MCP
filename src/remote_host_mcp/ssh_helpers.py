from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import shlex
import shutil
import signal
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_fs import rename_noreplace
from .config import Settings
from .models import SshCheckResult, SshExecResult, SshTransferResult, ToolErrorInfo
from .secure_paths import opened_beneath, opened_parent_beneath

logger = logging.getLogger("remote_host_mcp.ssh")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+=,@%:-]+$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


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


@dataclass(slots=True)
class _RunOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    terminated_by: str | None
    truncated: bool
    output_bytes_returned: int
    output_bytes_total: int


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
    await proc.wait()
    return terminated_by


def _safe_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "SSH_AUTH_SOCK"):
        value = os.getenv(key)
        if value:
            env[key] = value
    return env


def _redact(text: str, settings: Settings) -> str:
    if settings.path_key:
        return text.replace(settings.path_key, "[REDACTED]")
    return text


async def _run_process(
    argv: list[str],
    *,
    timeout_ms: int,
    settings: Settings,
    input_data: bytes | None = None,
) -> _RunOutcome:
    started = time.monotonic()
    budget = _Budget(settings.max_output_bytes)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=_safe_env(),
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout_task = asyncio.create_task(_drain(proc.stdout, budget))
    stderr_task = asyncio.create_task(_drain(proc.stderr, budget))
    timed_out = False
    terminated_by: str | None = None
    try:
        if input_data is not None and proc.stdin is not None:
            proc.stdin.write(input_data)
            await proc.stdin.drain()
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout_ms / 1000)
        except asyncio.TimeoutError:
            timed_out = True
            terminated_by = await _terminate_group(proc, settings.kill_grace_ms)
        except asyncio.CancelledError:
            await _terminate_group(proc, settings.kill_grace_ms)
            raise
        stdout_b, stderr_b = await asyncio.gather(stdout_task, stderr_task)
    except Exception:
        if proc.returncode is None:
            await _terminate_group(proc, settings.kill_grace_ms)
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    return _RunOutcome(
        returncode=proc.returncode,
        stdout=_redact(stdout_b.decode("utf-8", errors="replace"), settings),
        stderr=_redact(stderr_b.decode("utf-8", errors="replace"), settings),
        duration_ms=duration_ms,
        timed_out=timed_out,
        terminated_by=terminated_by,
        truncated=budget.truncated,
        output_bytes_returned=budget.used,
        output_bytes_total=budget.seen,
    )


def _validate_host(host: str) -> str:
    value = host.strip()
    if not value or value.startswith("-") or any(char.isspace() for char in value):
        raise ValueError("host must be an SSH config alias, DNS hostname, or IP address")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if not _HOST_RE.fullmatch(value) or ".." in value:
        raise ValueError("host must be an SSH config alias, DNS hostname, or IP address")
    return value


def _validate_port(port: int | None) -> int | None:
    if port is not None and (port < 1 or port > 65535):
        raise ValueError("port must be between 1 and 65535")
    return port


def _validate_remote_path(path: str) -> str:
    if not _REMOTE_PATH_RE.fullmatch(path) or "//" in path:
        raise ValueError("remote_path must be an absolute path using conservative POSIX filename characters")
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("remote_path must not contain . or .. components")
    if len(path.encode("utf-8")) > 4096:
        raise ValueError("remote_path is too long")
    return path


def _client_binary(name: str) -> str:
    binary = shutil.which(name)
    if not binary:
        raise ValueError(f"OpenSSH client binary '{name}' is not installed")
    return binary


def _ssh_options(connect_timeout_seconds: int) -> list[str]:
    if connect_timeout_seconds < 1 or connect_timeout_seconds > 30:
        raise ValueError("connect_timeout_seconds must be between 1 and 30")
    # Command-line options take precedence over ~/.ssh/config for these safety
    # properties while still allowing config aliases, IdentityFile, ProxyJump,
    # User/Port and ssh-agent based authentication to be pre-provisioned by the operator.
    return [
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no",
        "-o", "LogLevel=ERROR",
        "-o", f"ConnectTimeout={connect_timeout_seconds}",
    ]


def _destination(host: str, user: str | None) -> str:
    return f"{user}@{host}" if user else host


def _remote_spec(host: str, user: str | None, path: str) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    destination = f"{user}@{formatted_host}" if user else formatted_host
    return f"{destination}:{path}"


async def _run_ssh_script(
    *,
    host: str,
    user: str | None,
    port: int | None,
    connect_timeout_seconds: int,
    timeout_ms: int,
    script: bytes,
    settings: Settings,
) -> _RunOutcome:
    ssh = _client_binary("ssh")
    argv = [ssh, *_ssh_options(connect_timeout_seconds)]
    if port is not None:
        argv += ["-p", str(port)]
    argv += [_destination(host, user), "sh", "-s", "--"]
    return await _run_process(argv, timeout_ms=timeout_ms, settings=settings, input_data=script)


async def ssh_check(
    host: str,
    user: str | None,
    port: int | None,
    connect_timeout_seconds: int,
    settings: Settings,
) -> SshCheckResult:
    host = _validate_host(host)
    port = _validate_port(port)
    outcome = await _run_ssh_script(
        host=host,
        user=user,
        port=port,
        connect_timeout_seconds=connect_timeout_seconds,
        timeout_ms=max(5000, connect_timeout_seconds * 1000 + 3000),
        script=b"exit 0\n",
        settings=settings,
    )
    success = (not outcome.timed_out) and outcome.returncode == 0
    return SshCheckResult(
        success=success,
        host=host,
        port=port,
        user=user,
        duration_ms=outcome.duration_ms,
        error=None if success else ToolErrorInfo(code="SSH_CONNECT_FAILED", message="SSH connection/authentication failed"),
    )


async def ssh_exec(
    host: str,
    user: str | None,
    port: int | None,
    command: str,
    connect_timeout_seconds: int,
    timeout_ms: int | None,
    settings: Settings,
) -> SshExecResult:
    host = _validate_host(host)
    port = _validate_port(port)
    command_bytes = command.encode("utf-8")
    if not command.strip() or b"\x00" in command_bytes or len(command_bytes) > settings.max_command_bytes:
        raise ValueError("command is empty, contains NUL, or exceeds the configured UTF-8 byte limit")
    timeout = settings.default_timeout_ms if timeout_ms is None else timeout_ms
    if timeout < 1000 or timeout > settings.max_timeout_ms:
        raise ValueError(f"timeout_ms must be between 1000 and {settings.max_timeout_ms}")
    outcome = await _run_ssh_script(
        host=host,
        user=user,
        port=port,
        connect_timeout_seconds=connect_timeout_seconds,
        timeout_ms=timeout,
        # Keep arbitrary remote shell text out of the local ssh process argv.
        script=command_bytes + b"\n",
        settings=settings,
    )
    success = (not outcome.timed_out) and outcome.returncode == 0
    error = None
    if outcome.timed_out:
        error = ToolErrorInfo(code="SSH_TIMEOUT", message="Remote command exceeded timeout and the local SSH client was terminated")
    elif outcome.returncode != 0:
        error = ToolErrorInfo(code="SSH_NONZERO_EXIT", message=f"Remote command exited with code {outcome.returncode}")
    logger.info(json.dumps({
        "event": "ssh_exec",
        "host": host,
        "port": port,
        "command_sha256": hashlib.sha256(command_bytes).hexdigest()[:16],
        "command_bytes": len(command_bytes),
        "duration_ms": outcome.duration_ms,
        "exit_code": outcome.returncode,
        "timed_out": outcome.timed_out,
        "truncated": outcome.truncated,
    }, separators=(",", ":")))
    return SshExecResult(
        success=success,
        host=host,
        port=port,
        user=user,
        exit_code=outcome.returncode,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        duration_ms=outcome.duration_ms,
        timed_out=outcome.timed_out,
        terminated_by=outcome.terminated_by,
        truncated=outcome.truncated,
        output_bytes_returned=outcome.output_bytes_returned,
        output_bytes_total=outcome.output_bytes_total,
        error=error,
    )


def _state_dir(settings: Settings) -> Path:
    root = settings.state_dir / "ssh"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _freeze_allowed_file(path: str, settings: Settings) -> tuple[Path, int, str]:
    with opened_beneath(settings, path, os.O_RDONLY, no_symlinks=True) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Path is not a regular file")
        if info.st_size > settings.max_transfer_bytes:
            raise ValueError(f"File exceeds transfer limit {settings.max_transfer_bytes}")
        stage = _state_dir(settings) / f"input-{secrets.token_hex(12)}"
        out_fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(out_fd, view)
                    if written <= 0:
                        raise OSError("Short write while staging SSH upload")
                    view = view[written:]
            os.fsync(out_fd)
        except Exception:
            stage.unlink(missing_ok=True)
            raise
        finally:
            os.close(out_fd)
    return stage, total, digest.hexdigest()


async def _remote_sha256(
    *, host: str, user: str | None, port: int | None, remote_path: str,
    connect_timeout_seconds: int, timeout_ms: int, settings: Settings,
) -> tuple[str | None, _RunOutcome]:
    script = f"set -eu\nsha256sum -- {shlex.quote(remote_path)} | cut -d ' ' -f1\n".encode()
    outcome = await _run_ssh_script(
        host=host,
        user=user,
        port=port,
        connect_timeout_seconds=connect_timeout_seconds,
        timeout_ms=timeout_ms,
        script=script,
        settings=settings,
    )
    digest = outcome.stdout.strip().splitlines()[0].strip().lower() if outcome.returncode == 0 and outcome.stdout.strip() else None
    if digest is not None and not _SHA256_RE.fullmatch(digest):
        digest = None
    return digest, outcome


async def _cleanup_remote_temp(
    *, host: str, user: str | None, port: int | None, remote_temp: str,
    connect_timeout_seconds: int, settings: Settings,
) -> None:
    try:
        await _run_ssh_script(
            host=host,
            user=user,
            port=port,
            connect_timeout_seconds=connect_timeout_seconds,
            timeout_ms=15000,
            script=f"rm -f -- {shlex.quote(remote_temp)}\n".encode(),
            settings=settings,
        )
    except Exception:
        pass


async def ssh_upload(
    host: str,
    user: str | None,
    port: int | None,
    local_path: str,
    remote_path: str,
    connect_timeout_seconds: int,
    timeout_ms: int,
    overwrite: bool,
    settings: Settings,
) -> SshTransferResult:
    host = _validate_host(host)
    port = _validate_port(port)
    remote_path = _validate_remote_path(remote_path)
    if timeout_ms < 1000 or timeout_ms > 3_600_000:
        raise ValueError("timeout_ms must be between 1000 and 3600000")
    frozen, size, digest = await asyncio.to_thread(_freeze_allowed_file, local_path, settings)
    parent = posixpath.dirname(remote_path) or "/"
    remote_temp = _validate_remote_path(
        posixpath.join(parent, f".{posixpath.basename(remote_path)}.rhmcp-{secrets.token_hex(8)}.part")
    )
    started = time.monotonic()
    try:
        scp = _client_binary("scp")
        argv = [scp, *_ssh_options(connect_timeout_seconds), "-q"]
        if port is not None:
            argv += ["-P", str(port)]
        argv += ["--", str(frozen), _remote_spec(host, user, remote_temp)]
        copy_outcome = await _run_process(argv, timeout_ms=timeout_ms, settings=settings)
        if copy_outcome.timed_out or copy_outcome.returncode != 0:
            await _cleanup_remote_temp(
                host=host, user=user, port=port, remote_temp=remote_temp,
                connect_timeout_seconds=connect_timeout_seconds, settings=settings,
            )
            return SshTransferResult(
                success=False, direction="upload", host=host, port=port, user=user,
                local_path=str(settings.resolve_allowed_path(local_path)), remote_path=remote_path,
                bytes_transferred=0, sha256=digest, duration_ms=int((time.monotonic() - started) * 1000),
                replaced=False, error=ToolErrorInfo(code="SSH_TRANSFER_FAILED", message="SCP upload to remote staging failed"),
            )
        if overwrite:
            commit_script = f"set -eu\nmv -fT -- {shlex.quote(remote_temp)} {shlex.quote(remote_path)}\n"
        else:
            commit_script = (
                f"set -eu\nln -- {shlex.quote(remote_temp)} {shlex.quote(remote_path)}\n"
                f"rm -f -- {shlex.quote(remote_temp)}\n"
            )
        commit = await _run_ssh_script(
            host=host, user=user, port=port, connect_timeout_seconds=connect_timeout_seconds,
            timeout_ms=min(timeout_ms, 60000), script=commit_script.encode(), settings=settings,
        )
        if commit.timed_out or commit.returncode != 0:
            await _cleanup_remote_temp(
                host=host, user=user, port=port, remote_temp=remote_temp,
                connect_timeout_seconds=connect_timeout_seconds, settings=settings,
            )
            return SshTransferResult(
                success=False, direction="upload", host=host, port=port, user=user,
                local_path=str(settings.resolve_allowed_path(local_path)), remote_path=remote_path,
                bytes_transferred=0, sha256=digest, duration_ms=int((time.monotonic() - started) * 1000),
                replaced=False, error=ToolErrorInfo(code="SSH_REMOTE_COMMIT_FAILED", message="Remote publication failed"),
            )
        remote_digest, hash_outcome = await _remote_sha256(
            host=host, user=user, port=port, remote_path=remote_path,
            connect_timeout_seconds=connect_timeout_seconds, timeout_ms=min(timeout_ms, 60000), settings=settings,
        )
        if hash_outcome.returncode != 0 or remote_digest != digest:
            return SshTransferResult(
                success=False, direction="upload", host=host, port=port, user=user,
                local_path=str(settings.resolve_allowed_path(local_path)), remote_path=remote_path,
                bytes_transferred=size, sha256=digest, duration_ms=int((time.monotonic() - started) * 1000),
                replaced=overwrite, error=ToolErrorInfo(code="SSH_HASH_MISMATCH", message="Remote SHA-256 verification failed after upload"),
            )
        return SshTransferResult(
            success=True, direction="upload", host=host, port=port, user=user,
            local_path=str(settings.resolve_allowed_path(local_path)), remote_path=remote_path,
            bytes_transferred=size, sha256=digest, duration_ms=int((time.monotonic() - started) * 1000), replaced=overwrite,
        )
    finally:
        frozen.unlink(missing_ok=True)


def _publish_download(source: Path, local_path: str, overwrite: bool, mode: int, settings: Settings) -> tuple[str, bool]:
    if mode < 0 or mode > 0o777:
        raise ValueError("mode must be between 0 and 0o777")
    with opened_parent_beneath(settings, local_path) as (parent_fd, name, target):
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError("Refusing to replace a symlink download destination")
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("Download destination exists and is not a regular file")
        if existing is not None and not overwrite:
            raise ValueError("Download destination exists and overwrite=false")
        stage_name = f".{name}.rhmcp-ssh-{secrets.token_hex(8)}.part"
        in_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        out_fd: int | None = None
        try:
            out_fd = os.open(
                stage_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=parent_fd,
            )
            os.fchmod(out_fd, mode)
            while True:
                chunk = os.read(in_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(out_fd, view)
                    if written <= 0:
                        raise OSError("Short write while publishing SSH download")
                    view = view[written:]
            os.fsync(out_fd)
            os.close(out_fd)
            out_fd = None
            if overwrite:
                os.replace(stage_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            else:
                try:
                    rename_noreplace(parent_fd, stage_name, parent_fd, name)
                except OSError as exc:
                    if exc.errno == 17:
                        raise ValueError("Download destination appeared during commit and overwrite=false") from exc
                    raise
            os.fsync(parent_fd)
        finally:
            os.close(in_fd)
            if out_fd is not None:
                os.close(out_fd)
            try:
                os.unlink(stage_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        return str(target), existing is not None


async def ssh_download(
    host: str,
    user: str | None,
    port: int | None,
    remote_path: str,
    local_path: str,
    connect_timeout_seconds: int,
    timeout_ms: int,
    overwrite: bool,
    mode: int,
    settings: Settings,
) -> SshTransferResult:
    host = _validate_host(host)
    port = _validate_port(port)
    remote_path = _validate_remote_path(remote_path)
    if timeout_ms < 1000 or timeout_ms > 3_600_000:
        raise ValueError("timeout_ms must be between 1000 and 3600000")
    with opened_parent_beneath(settings, local_path):
        pass
    temp = _state_dir(settings) / f"download-{secrets.token_hex(12)}"
    started = time.monotonic()
    try:
        remote_digest, hash_outcome = await _remote_sha256(
            host=host, user=user, port=port, remote_path=remote_path,
            connect_timeout_seconds=connect_timeout_seconds, timeout_ms=min(timeout_ms, 60000), settings=settings,
        )
        if hash_outcome.returncode != 0 or remote_digest is None:
            return SshTransferResult(
                success=False, direction="download", host=host, port=port, user=user,
                local_path=str(Path(local_path).expanduser()), remote_path=remote_path,
                bytes_transferred=0, sha256=None, duration_ms=int((time.monotonic() - started) * 1000),
                replaced=False, error=ToolErrorInfo(code="SSH_REMOTE_HASH_FAILED", message="Could not obtain remote SHA-256 before download"),
            )
        scp = _client_binary("scp")
        argv = [scp, *_ssh_options(connect_timeout_seconds), "-q"]
        if port is not None:
            argv += ["-P", str(port)]
        argv += ["--", _remote_spec(host, user, remote_path), str(temp)]
        copy_outcome = await _run_process(argv, timeout_ms=timeout_ms, settings=settings)
        if copy_outcome.timed_out or copy_outcome.returncode != 0:
            return SshTransferResult(
                success=False, direction="download", host=host, port=port, user=user,
                local_path=str(Path(local_path).expanduser()), remote_path=remote_path,
                bytes_transferred=0, sha256=None, duration_ms=int((time.monotonic() - started) * 1000),
                replaced=False, error=ToolErrorInfo(code="SSH_TRANSFER_FAILED", message="SCP download failed"),
            )
        fd = os.open(temp, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Downloaded object is not a regular file")
            if info.st_size > settings.max_transfer_bytes:
                raise ValueError(f"Downloaded file exceeds transfer limit {settings.max_transfer_bytes}")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
            local_digest = digest.hexdigest()
        finally:
            os.close(fd)
        if local_digest != remote_digest:
            return SshTransferResult(
                success=False, direction="download", host=host, port=port, user=user,
                local_path=str(Path(local_path).expanduser()), remote_path=remote_path,
                bytes_transferred=total, sha256=local_digest, duration_ms=int((time.monotonic() - started) * 1000),
                replaced=False, error=ToolErrorInfo(code="SSH_HASH_MISMATCH", message="Remote file changed or transfer integrity verification failed"),
            )
        published, replaced = await asyncio.to_thread(_publish_download, temp, local_path, overwrite, mode, settings)
        return SshTransferResult(
            success=True, direction="download", host=host, port=port, user=user,
            local_path=published, remote_path=remote_path, bytes_transferred=total, sha256=local_digest,
            duration_ms=int((time.monotonic() - started) * 1000), replaced=replaced,
        )
    finally:
        temp.unlink(missing_ok=True)
