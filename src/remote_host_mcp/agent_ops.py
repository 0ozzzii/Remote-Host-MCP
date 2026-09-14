from __future__ import annotations

import asyncio
import difflib
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .atomic_fs import rename_exchange, rename_noreplace
from .config import Settings
from .filesystem import path_info as path_info_impl
from .filesystem import write_text_file as write_text_file_impl
from .jobs import job_status as job_status_impl
from .models import ExecResult, FileWriteResult, ToolErrorInfo
from .secure_paths import opened_beneath, opened_parent_beneath

_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_TEXT_MIMES = {"application/json", "application/xml", "application/javascript", "application/x-sh", "text/csv", "text/markdown"}
_TERMINAL_JOB_STATES = {"completed", "failed", "canceled", "timed_out", "interrupted", "start_failed"}
_SAFE_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_ENV_RE = re.compile(r"(TOKEN|SECRET|PASS(?:WORD|WD)?|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|COOKIE|SESSION|AUTH)", re.IGNORECASE)
_SNAPSHOT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_LEASE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_BASE_ENV_NAMES = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")


class RootCapacity(BaseModel):
    path: str
    free_bytes: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class HostCapabilitiesResult(BaseModel):
    systemd_present: bool
    systemd_operational: bool
    gui_available: bool
    screenshot_tools: list[str]
    container_runtimes: list[str]
    gpu_available: bool
    gpu_tools: list[str]
    developer_tools: list[str]
    ssh_client: bool
    allowed_roots: list[str]
    root_capacity: list[RootCapacity]
    state_dir: str
    max_sync_timeout_ms: int = Field(gt=0)
    max_transfer_bytes: int = Field(gt=0)
    max_inline_artifact_bytes: int = Field(gt=0)
    durable_jobs: bool = True
    persistent_pty: bool = True
    structured_argv_exec: bool = True
    snapshots: bool = True
    leases: bool = True
    bounded_batch_inspect: bool = True
    structured_patch: bool = True


class WaitConditionResult(BaseModel):
    condition: str
    satisfied: bool
    elapsed_ms: int = Field(ge=0)
    attempts: int = Field(ge=1)
    detail: str | None = None


class ArtifactInfoResult(BaseModel):
    path: str
    name: str
    mime_type: str
    size: int = Field(ge=0)
    modified_ns: int = Field(ge=0)
    sha256: str | None = None
    inline_eligible: bool
    image: bool
    text_like: bool


class ArtifactPreviewResult(BaseModel):
    path: str
    mime_type: str
    size: int = Field(ge=0)
    preview: str | None = None
    truncated: bool = False
    binary: bool = False


class ArtifactBundleResult(BaseModel):
    path: str
    entries: int = Field(ge=0)
    bytes_source: int = Field(ge=0)
    bytes_bundle: int = Field(ge=0)
    sha256: str


class SnapshotEntry(BaseModel):
    path: str
    type: Literal["file", "directory"]
    size: int = Field(ge=0)


class SnapshotInfo(BaseModel):
    snapshot_id: str
    label: str | None = None
    created_at: int = Field(gt=0)
    entries: list[SnapshotEntry]
    total_bytes: int = Field(ge=0)
    total_nodes: int = Field(ge=0)


class SnapshotListResult(BaseModel):
    snapshots: list[SnapshotInfo]


class SnapshotRestoreResult(BaseModel):
    snapshot_id: str
    restored: list[str]
    per_path_atomic: bool = True


class SnapshotDeleteResult(BaseModel):
    snapshot_id: str
    deleted: bool


class LeaseAcquireResult(BaseModel):
    acquired: bool
    resource: str
    lease_id: str | None = None
    holder: str | None = None
    expires_at: int | None = None


class LeaseStatusResult(BaseModel):
    active: bool
    resource: str
    holder: str | None = None
    expires_at: int | None = None


class LeaseReleaseResult(BaseModel):
    lease_id: str
    released: bool


class LeaseListResult(BaseModel):
    leases: list[LeaseStatusResult]


class PathInspection(BaseModel):
    path: str
    ok: bool
    type: str | None = None
    size: int | None = Field(default=None, ge=0)
    mode: str | None = None
    modified_ns: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    hash_skipped: bool = False
    text_preview: str | None = None
    preview_truncated: bool = False
    error: str | None = None


class InspectPathsResult(BaseModel):
    items: list[PathInspection]


class FileDiffResult(BaseModel):
    path: str
    current_sha256: str
    proposed_sha256: str
    diff: str
    truncated: bool = False


class LineEdit(BaseModel):
    start_line: int = Field(ge=1, description="1-based first line to replace or insertion position.")
    delete_lines: int = Field(ge=0, description="Number of existing lines to delete from start_line.")
    replacement: str = Field(description="Exact replacement text inserted at start_line.")


class _SnapshotBudget:
    def __init__(self, max_bytes: int, max_nodes: int) -> None:
        self.max_bytes = max_bytes
        self.max_nodes = max_nodes
        self.bytes = 0
        self.nodes = 0

    def add_node(self) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise ValueError(f"Snapshot exceeds max_nodes={self.max_nodes}")

    def add_bytes(self, count: int) -> None:
        self.bytes += count
        if self.bytes > self.max_bytes:
            raise ValueError(f"Snapshot exceeds max_total_bytes={self.max_bytes}")


def _sha256_fd(fd: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _sha256_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _mime_from(path: Path, prefix: bytes) -> str:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(path.name, strict=False)
    return guessed or "application/octet-stream"


def _text_like(mime: str, prefix: bytes) -> bool:
    if b"\x00" in prefix:
        return False
    return mime.startswith("text/") or mime in _TEXT_MIMES


def _safe_systemd_operational() -> tuple[bool, bool]:
    binary = shutil.which("systemctl")
    if not binary:
        return False, False
    try:
        proc = subprocess.run([binary, "show", "--no-pager", "--property=Version", "--value"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
        return True, proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True, False


def host_capabilities(settings: Settings) -> HostCapabilitiesResult:
    systemd_present, systemd_operational = _safe_systemd_operational()
    screenshot_candidates = ("gnome-screenshot", "scrot", "import", "grim", "xwd", "ffmpeg")
    container_candidates = ("docker", "podman", "nerdctl")
    gpu_candidates = ("nvidia-smi", "rocm-smi")
    developer_candidates = ("bash", "python", "python3", "git", "gcc", "g++", "make", "cmake", "java", "node", "comsol", "abaqus")
    root_capacity: list[RootCapacity] = []
    for root in settings.allowed_roots:
        try:
            fs = os.statvfs(root)
            root_capacity.append(RootCapacity(path=str(root), free_bytes=fs.f_bavail * fs.f_frsize, total_bytes=fs.f_blocks * fs.f_frsize))
        except OSError:
            root_capacity.append(RootCapacity(path=str(root), free_bytes=0, total_bytes=0))
    gpu_tools = [name for name in gpu_candidates if shutil.which(name)]
    return HostCapabilitiesResult(
        systemd_present=systemd_present,
        systemd_operational=systemd_operational,
        gui_available=bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")),
        screenshot_tools=[name for name in screenshot_candidates if shutil.which(name)],
        container_runtimes=[name for name in container_candidates if shutil.which(name)],
        gpu_available=bool(gpu_tools or Path("/dev/nvidiactl").exists() or Path("/dev/kfd").exists()),
        gpu_tools=gpu_tools,
        developer_tools=[name for name in developer_candidates if shutil.which(name)],
        ssh_client=shutil.which("ssh") is not None,
        allowed_roots=[str(path) for path in settings.allowed_roots],
        root_capacity=root_capacity,
        state_dir=str(settings.state_dir),
        max_sync_timeout_ms=settings.max_timeout_ms,
        max_transfer_bytes=settings.max_transfer_bytes,
        max_inline_artifact_bytes=8 * 1024 * 1024,
    )


async def _probe_condition(condition: str, *, path: str | None, pid: int | None, expected_start_ticks: int | None, size_bytes: int | None, job_id: str | None, host: str | None, port: int | None, pattern: str | None, log_tail_bytes: int, settings: Settings) -> tuple[bool, str | None]:
    if condition == "file_exists":
        if not path:
            raise ValueError("path is required for file_exists")
        try:
            path_info_impl(path, settings)
            return True, "path exists"
        except (OSError, ValueError):
            return False, None
    if condition == "file_not_exists":
        if not path:
            raise ValueError("path is required for file_not_exists")
        try:
            path_info_impl(path, settings)
            return False, None
        except (OSError, ValueError):
            return True, "path is absent"
    if condition == "file_size_at_least":
        if not path or size_bytes is None:
            raise ValueError("path and size_bytes are required for file_size_at_least")
        try:
            info = path_info_impl(path, settings)
        except (OSError, ValueError):
            return False, None
        if info.type != "file" or info.size is None:
            raise ValueError("path must be a regular file")
        return info.size >= size_bytes, f"size={info.size}"
    if condition == "process_exit":
        if pid is None or expected_start_ticks is None:
            raise ValueError("pid and expected_start_ticks are required for process_exit")
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return True, "process is absent"
        close = raw.rfind(")")
        fields = raw[close + 2 :].split() if close >= 0 else []
        if len(fields) <= 19:
            return True, "process identity is unavailable"
        try:
            current_ticks = int(fields[19])
        except ValueError:
            return True, "process identity is unavailable"
        if current_ticks != expected_start_ticks:
            return True, "PID was reused"
        return False, None
    if condition == "job_terminal":
        if not job_id:
            raise ValueError("job_id is required for job_terminal")
        status = job_status_impl(job_id, settings)
        return status.status in _TERMINAL_JOB_STATES, f"status={status.status}"
    if condition == "port_open":
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("port_open is restricted to loopback hosts")
        if port is None or port < 1 or port > 65535:
            raise ValueError("valid port is required for port_open")
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
            del reader
            writer.close()
            await writer.wait_closed()
            return True, f"{host}:{port} accepts TCP"
        except (OSError, asyncio.TimeoutError):
            return False, None
    if condition == "log_contains":
        if not path or pattern is None:
            raise ValueError("path and pattern are required for log_contains")
        if len(pattern.encode("utf-8")) > 512:
            raise ValueError("pattern must be <= 512 UTF-8 bytes")
        try:
            with opened_beneath(settings, path, os.O_RDONLY) as fd:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("log path is not a regular file")
                start = max(0, info.st_size - log_tail_bytes)
                os.lseek(fd, start, os.SEEK_SET)
                data = os.read(fd, log_tail_bytes)
        except FileNotFoundError:
            return False, None
        text = data.decode("utf-8", errors="replace")
        return pattern in text, f"scanned_tail_bytes={len(data)}"
    raise ValueError(f"Unsupported wait condition: {condition}")


async def wait_condition(condition: str, *, path: str | None, pid: int | None, expected_start_ticks: int | None, size_bytes: int | None, job_id: str | None, host: str | None, port: int | None, pattern: str | None, timeout_ms: int, poll_ms: int, log_tail_bytes: int, settings: Settings) -> WaitConditionResult:
    if timeout_ms < 0 or timeout_ms > settings.max_timeout_ms:
        raise ValueError(f"timeout_ms must be between 0 and {settings.max_timeout_ms}")
    if poll_ms < 100 or poll_ms > 5000:
        raise ValueError("poll_ms must be between 100 and 5000")
    if log_tail_bytes < 256 or log_tail_bytes > 1_048_576:
        raise ValueError("log_tail_bytes must be between 256 and 1048576")
    started = time.monotonic()
    attempts = 0
    last_detail: str | None = None
    while True:
        attempts += 1
        satisfied, detail = await _probe_condition(condition, path=path, pid=pid, expected_start_ticks=expected_start_ticks, size_bytes=size_bytes, job_id=job_id, host=host, port=port, pattern=pattern, log_tail_bytes=log_tail_bytes, settings=settings)
        if detail is not None:
            last_detail = detail
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if satisfied or elapsed_ms >= timeout_ms:
            return WaitConditionResult(condition=condition, satisfied=satisfied, elapsed_ms=elapsed_ms, attempts=attempts, detail=last_detail)
        await asyncio.sleep(min(poll_ms, max(0, timeout_ms - elapsed_ms)) / 1000)


def artifact_info(path: str, include_sha256: bool, settings: Settings) -> ArtifactInfoResult:
    display = Path(path).expanduser()
    with opened_beneath(settings, path, os.O_RDONLY) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Artifact path is not a regular file")
        prefix = os.read(fd, 64)
        digest = _sha256_fd(fd)[0] if include_sha256 else None
    mime = _mime_from(display, prefix)
    return ArtifactInfoResult(path=str(settings.resolve_allowed_path(path)), name=display.name, mime_type=mime, size=info.st_size, modified_ns=info.st_mtime_ns, sha256=digest, inline_eligible=info.st_size <= 8 * 1024 * 1024, image=mime in _IMAGE_MIMES, text_like=_text_like(mime, prefix))


def artifact_preview(path: str, max_bytes: int, settings: Settings) -> ArtifactPreviewResult:
    if max_bytes < 256 or max_bytes > 131_072:
        raise ValueError("max_bytes must be between 256 and 131072")
    display = Path(path).expanduser()
    with opened_beneath(settings, path, os.O_RDONLY) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Artifact path is not a regular file")
        data = os.read(fd, max_bytes + 1)
    prefix = data[:64]
    mime = _mime_from(display, prefix)
    binary = not _text_like(mime, prefix)
    preview = None if binary else data[:max_bytes].decode("utf-8", errors="replace")
    return ArtifactPreviewResult(path=str(settings.resolve_allowed_path(path)), mime_type=mime, size=info.st_size, preview=preview, truncated=info.st_size > max_bytes or len(data) > max_bytes, binary=binary)


def _exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _collect_bundle_entries(source: Path, *, root_name: str, settings: Settings, entries: list[tuple[str, str]], max_entries: int, max_total_bytes: int, total_bytes: list[int]) -> None:
    resolved = settings.resolve_allowed_path(str(source))
    st = os.lstat(resolved)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"Symlinks are not accepted in artifact bundles: {resolved}")
    if stat.S_ISREG(st.st_mode):
        entries.append((root_name, str(resolved)))
        total_bytes[0] += st.st_size
    elif stat.S_ISDIR(st.st_mode):
        with opened_beneath(settings, str(resolved), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)) as dir_fd:
            for name in sorted(os.listdir(dir_fd), key=str.lower):
                item = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                child = resolved / name
                if stat.S_ISLNK(item.st_mode):
                    raise ValueError(f"Symlinks are not accepted in artifact bundles: {child}")
                arc = f"{root_name}/{name}"
                if stat.S_ISDIR(item.st_mode):
                    _collect_bundle_entries(child, root_name=arc, settings=settings, entries=entries, max_entries=max_entries, max_total_bytes=max_total_bytes, total_bytes=total_bytes)
                elif stat.S_ISREG(item.st_mode):
                    entries.append((arc, str(child)))
                    total_bytes[0] += item.st_size
                else:
                    raise ValueError(f"Unsupported file type in artifact bundle: {child}")
                if len(entries) > max_entries:
                    raise ValueError(f"Artifact bundle exceeds max_entries={max_entries}")
                if total_bytes[0] > max_total_bytes:
                    raise ValueError(f"Artifact bundle exceeds max_total_bytes={max_total_bytes}")
    else:
        raise ValueError(f"Unsupported artifact source type: {resolved}")


def artifact_bundle(paths: list[str], destination: str, max_entries: int, max_total_bytes: int, settings: Settings) -> ArtifactBundleResult:
    if not paths or len(paths) > 20:
        raise ValueError("paths must contain between 1 and 20 items")
    if max_entries < 1 or max_entries > 5000:
        raise ValueError("max_entries must be between 1 and 5000")
    if max_total_bytes < 1 or max_total_bytes > settings.max_transfer_bytes:
        raise ValueError(f"max_total_bytes must be between 1 and {settings.max_transfer_bytes}")
    entries: list[tuple[str, str]] = []
    total_bytes = [0]
    used_names: set[str] = set()
    for index, raw in enumerate(paths):
        source = settings.resolve_allowed_path(raw)
        name = source.name or f"root-{index}"
        if name in used_names:
            name = f"{index}-{name}"
        used_names.add(name)
        _collect_bundle_entries(source, root_name=name, settings=settings, entries=entries, max_entries=max_entries, max_total_bytes=max_total_bytes, total_bytes=total_bytes)
        if len(entries) > max_entries:
            raise ValueError(f"Artifact bundle exceeds max_entries={max_entries}")
        if total_bytes[0] > max_total_bytes:
            raise ValueError(f"Artifact bundle exceeds max_total_bytes={max_total_bytes}")
    with opened_parent_beneath(settings, destination) as (parent_fd, name, entry):
        if _exists_at(parent_fd, name):
            raise ValueError("Artifact bundle destination already exists")
        temp_name = f".{name}.bundle-{secrets.token_hex(6)}"
        fd = os.open(temp_name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        try:
            handle = os.fdopen(fd, "w+b", closefd=False)
            try:
                with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    streamed_total = 0
                    for arcname, source_raw in entries:
                        with opened_beneath(settings, source_raw, os.O_RDONLY) as source_fd:
                            info = os.fstat(source_fd)
                            if not stat.S_ISREG(info.st_mode):
                                raise ValueError(f"Bundle source changed type: {source_raw}")
                            with archive.open(arcname, "w") as archived:
                                while True:
                                    chunk = os.read(source_fd, 1024 * 1024)
                                    if not chunk:
                                        break
                                    streamed_total += len(chunk)
                                    if streamed_total > max_total_bytes:
                                        raise ValueError("Bundle sources changed beyond byte budget")
                                    archived.write(chunk)
                handle.flush()
                os.fsync(fd)
            finally:
                handle.close()
            rename_noreplace(parent_fd, temp_name, parent_fd, name)
            os.fsync(parent_fd)
        except Exception:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    final_path = Path(entry)
    digest, bundle_bytes = _sha256_path(final_path)
    return ArtifactBundleResult(path=str(final_path), entries=len(entries), bytes_source=total_bytes[0], bytes_bundle=bundle_bytes, sha256=digest)


class _OutputBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.seen = 0
        self.truncated = False
        self.lock = asyncio.Lock()

    async def keep(self, data: bytes) -> bytes:
        async with self.lock:
            self.seen += len(data)
            remaining = max(0, self.limit - self.used)
            kept = data[:remaining]
            self.used += len(kept)
            if len(data) > remaining:
                self.truncated = True
            return kept


async def _drain_stream(stream: asyncio.StreamReader | None, budget: _OutputBudget) -> bytes:
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
    signal_name = "SIGTERM"
    try:
        os.killpg(proc.pid, 15)
    except ProcessLookupError:
        return signal_name
    try:
        await asyncio.wait_for(proc.wait(), grace_ms / 1000)
        return signal_name
    except asyncio.TimeoutError:
        signal_name = "SIGKILL"
    try:
        os.killpg(proc.pid, 9)
    except ProcessLookupError:
        pass
    await proc.wait()
    return signal_name


def _redact(text: str, settings: Settings) -> str:
    return text.replace(settings.path_key, "[REDACTED]") if settings.path_key else text


def _exec_error(code: str, message: str, cwd: str | None, settings: Settings) -> ExecResult:
    if cwd is None:
        try:
            safe_cwd = settings.resolve_allowed_path(None, default=Path.cwd())
        except ValueError:
            safe_cwd = settings.allowed_roots[0]
    else:
        safe_cwd = Path(cwd)
    return ExecResult(success=False, duration_ms=0, cwd=str(safe_cwd), error=ToolErrorInfo(code=code, message=message))


async def run_argv(argv: list[str], *, cwd: str | None, timeout_ms: int | None, stdin_text: str | None, env_allowlist: list[str], settings: Settings) -> ExecResult:
    started = time.monotonic()
    if not argv or len(argv) > 64:
        return _exec_error("INVALID_ARGV", "argv must contain between 1 and 64 items", cwd, settings)
    total_bytes = 0
    for item in argv:
        if not isinstance(item, str) or not item or "\x00" in item:
            return _exec_error("INVALID_ARGV", "argv items must be non-empty strings without NUL", cwd, settings)
        encoded = item.encode("utf-8")
        if len(encoded) > 4096:
            return _exec_error("INVALID_ARGV", "each argv item must be <= 4096 UTF-8 bytes", cwd, settings)
        total_bytes += len(encoded)
    if total_bytes > settings.max_command_bytes:
        return _exec_error("INVALID_ARGV", "combined argv exceeds command byte limit", cwd, settings)
    try:
        safe_cwd = settings.resolve_allowed_path(cwd) if cwd else settings.resolve_allowed_path(None, default=Path.cwd())
    except ValueError:
        safe_cwd = settings.allowed_roots[0]
        return ExecResult(success=False, duration_ms=0, cwd=str(safe_cwd), error=ToolErrorInfo(code="INVALID_CWD", message="cwd is outside RHMCP_ALLOWED_ROOTS"))
    timeout = settings.default_timeout_ms if timeout_ms is None else timeout_ms
    if timeout < 1000 or timeout > settings.max_timeout_ms:
        return ExecResult(success=False, duration_ms=0, cwd=str(safe_cwd), error=ToolErrorInfo(code="INVALID_TIMEOUT", message=f"timeout_ms must be between 1000 and {settings.max_timeout_ms}"))
    env: dict[str, str] = {}
    for name in _BASE_ENV_NAMES:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.update({"TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": "cat", "NO_COLOR": "1"})
    if len(env_allowlist) > 32:
        return _exec_error("INVALID_ENV_ALLOWLIST", "env_allowlist accepts at most 32 names", str(safe_cwd), settings)
    for name in env_allowlist:
        if not _SAFE_ENV_NAME_RE.fullmatch(name) or _SECRET_ENV_RE.search(name) or name.upper().startswith(("RHMCP_", "DSW_MCP_", "CF_")):
            return _exec_error("INVALID_ENV_ALLOWLIST", f"environment name is not eligible for inheritance: {name}", str(safe_cwd), settings)
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    stdin_bytes = None if stdin_text is None else stdin_text.encode("utf-8")
    if stdin_bytes is not None and len(stdin_bytes) > 65_536:
        return _exec_error("INVALID_STDIN", "stdin_text must be <= 65536 UTF-8 bytes", str(safe_cwd), settings)
    proc: asyncio.subprocess.Process | None = None
    stdout_task: asyncio.Task[bytes] | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    timed_out = False
    terminated_by: str | None = None
    budget = _OutputBudget(settings.max_output_bytes)
    try:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(safe_cwd), env=env, stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        stdout_task = asyncio.create_task(_drain_stream(proc.stdout, budget))
        stderr_task = asyncio.create_task(_drain_stream(proc.stderr, budget))
        if stdin_bytes is not None and proc.stdin is not None:
            proc.stdin.write(stdin_bytes)
            await proc.stdin.drain()
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout / 1000)
        except asyncio.TimeoutError:
            timed_out = True
            terminated_by = await _terminate_group(proc, settings.kill_grace_ms)
        except asyncio.CancelledError:
            await _terminate_group(proc, settings.kill_grace_ms)
            if stdout_task and stderr_task:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        assert stdout_task is not None and stderr_task is not None
        stdout_b, stderr_b = await asyncio.gather(stdout_task, stderr_task)
        stdout = _redact(stdout_b.decode("utf-8", errors="replace"), settings)
        stderr = _redact(stderr_b.decode("utf-8", errors="replace"), settings)
        duration_ms = int((time.monotonic() - started) * 1000)
        error = None
        if timed_out:
            error = ToolErrorInfo(code="TIMEOUT", message="Process exceeded timeout and its process group was terminated")
        elif proc.returncode != 0:
            error = ToolErrorInfo(code="NONZERO_EXIT", message=f"Process exited with code {proc.returncode}")
        return ExecResult(success=(not timed_out and proc.returncode == 0), exit_code=proc.returncode, stdout=stdout, stderr=stderr, duration_ms=duration_ms, timed_out=timed_out, terminated_by=terminated_by, truncated=budget.truncated, output_bytes_returned=budget.used, output_bytes_total=budget.seen, cwd=str(safe_cwd), error=error)
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError):
        if proc is not None and proc.returncode is None:
            await _terminate_group(proc, settings.kill_grace_ms)
        return ExecResult(success=False, duration_ms=int((time.monotonic() - started) * 1000), output_bytes_returned=budget.used, output_bytes_total=budget.seen, cwd=str(safe_cwd), error=ToolErrorInfo(code="EXEC_SETUP_FAILED", message="Process could not be started"))


def _snapshot_root(settings: Settings) -> Path:
    root = settings.state_dir / "snapshots"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _snapshot_dir(snapshot_id: str, settings: Settings) -> Path:
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise ValueError("Invalid snapshot_id")
    return _snapshot_root(settings) / snapshot_id


def _copy_regular_from_allowed(source: str, destination: Path, settings: Settings, budget: _SnapshotBudget) -> int:
    with opened_beneath(settings, source, os.O_RDONLY) as source_fd:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Snapshot source changed type: {source}")
        budget.add_node()
        budget.add_bytes(info.st_size)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(info.st_mode))
        try:
            with os.fdopen(fd, "wb", closefd=True) as out:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return info.st_size


def _copy_tree_from_allowed(source: str, destination: Path, settings: Settings, budget: _SnapshotBudget) -> int:
    with opened_beneath(settings, source, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)) as dir_fd:
        root_info = os.fstat(dir_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError(f"Snapshot source changed type: {source}")
        budget.add_node()
        destination.mkdir(mode=stat.S_IMODE(root_info.st_mode), exist_ok=False)
        total = 0
        for name in sorted(os.listdir(dir_fd), key=str.lower):
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            child_source = str(Path(source) / name)
            child_dest = destination / name
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Snapshots reject symlinks: {child_source}")
            if stat.S_ISDIR(info.st_mode):
                total += _copy_tree_from_allowed(child_source, child_dest, settings, budget)
            elif stat.S_ISREG(info.st_mode):
                total += _copy_regular_from_allowed(child_source, child_dest, settings, budget)
            else:
                raise ValueError(f"Snapshots reject special files: {child_source}")
        os.chmod(destination, stat.S_IMODE(root_info.st_mode))
        os.utime(destination, ns=(root_info.st_atime_ns, root_info.st_mtime_ns), follow_symlinks=False)
        return total


def _snapshot_manifest(snapshot_dir: Path) -> dict[str, Any]:
    path = snapshot_dir / "manifest.json"
    try:
        if path.stat().st_size > 1_048_576:
            raise ValueError("Snapshot manifest is too large")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Snapshot manifest is unreadable") from exc
    if not isinstance(data, dict):
        raise ValueError("Snapshot manifest is invalid")
    return data


def snapshot_create(paths: list[str], label: str | None, max_total_bytes: int, max_nodes: int, settings: Settings) -> SnapshotInfo:
    if not paths or len(paths) > 20:
        raise ValueError("paths must contain between 1 and 20 items")
    if label is not None and len(label.encode("utf-8")) > 128:
        raise ValueError("label must be <= 128 UTF-8 bytes")
    if max_total_bytes < 1 or max_total_bytes > settings.max_transfer_bytes:
        raise ValueError(f"max_total_bytes must be between 1 and {settings.max_transfer_bytes}")
    if max_nodes < 1 or max_nodes > 20_000:
        raise ValueError("max_nodes must be between 1 and 20000")
    snapshot_id = secrets.token_hex(16)
    root = _snapshot_root(settings)
    temp_dir = root / f".{snapshot_id}.tmp"
    final_dir = root / snapshot_id
    temp_dir.mkdir(mode=0o700)
    (temp_dir / "data").mkdir(mode=0o700)
    budget = _SnapshotBudget(max_total_bytes, max_nodes)
    manifest_entries: list[dict[str, Any]] = []
    result_entries: list[SnapshotEntry] = []
    seen_paths: set[str] = set()
    try:
        for index, raw in enumerate(paths):
            entry = settings.resolve_allowed_entry(raw)
            normalized = str(entry)
            if normalized in seen_paths:
                raise ValueError(f"Duplicate snapshot path: {normalized}")
            seen_paths.add(normalized)
            st = os.lstat(entry)
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"Snapshots reject symlink roots: {entry}")
            rel = f"data/{index}"
            destination = temp_dir / rel
            if stat.S_ISREG(st.st_mode):
                size = _copy_regular_from_allowed(normalized, destination, settings, budget)
                kind = "file"
            elif stat.S_ISDIR(st.st_mode):
                size = _copy_tree_from_allowed(normalized, destination, settings, budget)
                kind = "directory"
            else:
                raise ValueError(f"Snapshots accept only regular files/directories: {entry}")
            manifest_entries.append({"path": normalized, "type": kind, "snapshot_rel": rel, "size": size, "mode": stat.S_IMODE(st.st_mode)})
            result_entries.append(SnapshotEntry(path=normalized, type=kind, size=size))
        created_at = int(time.time())
        manifest = {"snapshot_id": snapshot_id, "label": label, "created_at": created_at, "entries": manifest_entries, "total_bytes": budget.bytes, "total_nodes": budget.nodes}
        manifest_path = temp_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        os.replace(temp_dir, final_dir)
        return SnapshotInfo(snapshot_id=snapshot_id, label=label, created_at=created_at, entries=result_entries, total_bytes=budget.bytes, total_nodes=budget.nodes)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _snapshot_info_from_manifest(manifest: dict[str, Any]) -> SnapshotInfo:
    entries = [SnapshotEntry(path=str(item["path"]), type=str(item["type"]), size=int(item["size"])) for item in manifest.get("entries", [])]
    return SnapshotInfo(snapshot_id=str(manifest["snapshot_id"]), label=manifest.get("label"), created_at=int(manifest["created_at"]), entries=entries, total_bytes=int(manifest.get("total_bytes", 0)), total_nodes=int(manifest.get("total_nodes", 0)))


def snapshot_list(settings: Settings) -> SnapshotListResult:
    snapshots: list[SnapshotInfo] = []
    for child in _snapshot_root(settings).iterdir():
        if not child.is_dir() or not _SNAPSHOT_ID_RE.fullmatch(child.name):
            continue
        try:
            snapshots.append(_snapshot_info_from_manifest(_snapshot_manifest(child)))
        except ValueError:
            continue
    snapshots.sort(key=lambda item: item.created_at, reverse=True)
    return SnapshotListResult(snapshots=snapshots)


def _remove_at(parent_fd: int, name: str) -> None:
    proc_path = Path(f"/proc/self/fd/{parent_fd}") / name
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(proc_path)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _stage_snapshot_entry(source: Path, parent_fd: int, temp_name: str, kind: str, mode: int) -> None:
    if kind == "file":
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent_fd)
        try:
            with source.open("rb") as incoming, os.fdopen(fd, "wb", closefd=True) as out:
                shutil.copyfileobj(incoming, out, length=1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
        except Exception:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        return
    if kind == "directory":
        os.mkdir(temp_name, mode=mode, dir_fd=parent_fd)
        target = Path(f"/proc/self/fd/{parent_fd}") / temp_name
        try:
            for child in source.iterdir():
                dest = target / child.name
                if child.is_dir():
                    shutil.copytree(child, dest, symlinks=False)
                elif child.is_file():
                    shutil.copy2(child, dest, follow_symlinks=False)
                else:
                    raise ValueError("Snapshot data contains unsupported file type")
            os.chmod(target, mode)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return
    raise ValueError("Snapshot manifest contains invalid type")


def snapshot_restore(snapshot_id: str, settings: Settings) -> SnapshotRestoreResult:
    snap_dir = _snapshot_dir(snapshot_id, settings)
    manifest = _snapshot_manifest(snap_dir)
    restored: list[str] = []
    for item in manifest.get("entries", []):
        raw_path = str(item["path"])
        kind = str(item["type"])
        mode = int(item.get("mode", 0o644 if kind == "file" else 0o755))
        source = snap_dir / str(item["snapshot_rel"])
        if not source.exists():
            raise ValueError(f"Snapshot data is missing for {raw_path}")
        with opened_parent_beneath(settings, raw_path) as (parent_fd, name, entry):
            temp_name = f".{name}.restore-{secrets.token_hex(6)}"
            _stage_snapshot_entry(source, parent_fd, temp_name, kind, mode)
            try:
                if _exists_at(parent_fd, name):
                    rename_exchange(parent_fd, temp_name, parent_fd, name)
                    os.fsync(parent_fd)
                    _remove_at(parent_fd, temp_name)
                else:
                    rename_noreplace(parent_fd, temp_name, parent_fd, name)
                    os.fsync(parent_fd)
                restored.append(str(entry))
            except Exception:
                _remove_at(parent_fd, temp_name)
                raise
    return SnapshotRestoreResult(snapshot_id=snapshot_id, restored=restored)


def snapshot_delete(snapshot_id: str, settings: Settings) -> SnapshotDeleteResult:
    path = _snapshot_dir(snapshot_id, settings)
    if not path.exists():
        return SnapshotDeleteResult(snapshot_id=snapshot_id, deleted=False)
    shutil.rmtree(path)
    return SnapshotDeleteResult(snapshot_id=snapshot_id, deleted=True)


def _lease_root(settings: Settings) -> Path:
    root = settings.state_dir / "leases"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


@contextmanager
def _lease_lock(settings: Settings):
    fd = os.open(_lease_root(settings) / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _lease_registry_path(settings: Settings) -> Path:
    return _lease_root(settings) / "registry.json"


def _load_leases(settings: Settings) -> list[dict[str, Any]]:
    path = _lease_registry_path(settings)
    if not path.exists():
        return []
    try:
        if path.stat().st_size > 1_048_576:
            raise ValueError("Lease registry is too large")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Lease registry is unreadable") from exc
    if not isinstance(data, list):
        raise ValueError("Lease registry is invalid")
    now = int(time.time())
    return [item for item in data if isinstance(item, dict) and int(item.get("expires_at", 0)) > now]


def _save_leases(settings: Settings, leases: list[dict[str, Any]]) -> None:
    path = _lease_registry_path(settings)
    temp = path.with_name(f".registry-{secrets.token_hex(6)}.tmp")
    temp.write_text(json.dumps(leases, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.chmod(temp, 0o600)
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _validate_resource(resource: str) -> str:
    normalized = resource.strip()
    if not normalized or "\x00" in normalized or len(normalized.encode("utf-8")) > 256:
        raise ValueError("resource must be 1..256 UTF-8 bytes without NUL")
    return normalized


def lease_acquire(resource: str, holder: str | None, ttl_seconds: int, settings: Settings) -> LeaseAcquireResult:
    resource = _validate_resource(resource)
    if holder is not None and len(holder.encode("utf-8")) > 128:
        raise ValueError("holder must be <= 128 UTF-8 bytes")
    if ttl_seconds < 5 or ttl_seconds > 86_400:
        raise ValueError("ttl_seconds must be between 5 and 86400")
    with _lease_lock(settings):
        leases = _load_leases(settings)
        for item in leases:
            if item.get("resource") == resource:
                _save_leases(settings, leases)
                return LeaseAcquireResult(acquired=False, resource=resource, holder=item.get("holder"), expires_at=int(item["expires_at"]))
        lease_id = secrets.token_hex(16)
        expires_at = int(time.time()) + ttl_seconds
        leases.append({"lease_id": lease_id, "resource": resource, "holder": holder, "expires_at": expires_at, "created_at": int(time.time())})
        _save_leases(settings, leases)
        return LeaseAcquireResult(acquired=True, resource=resource, lease_id=lease_id, holder=holder, expires_at=expires_at)


def lease_status(resource: str, settings: Settings) -> LeaseStatusResult:
    resource = _validate_resource(resource)
    with _lease_lock(settings):
        leases = _load_leases(settings)
        _save_leases(settings, leases)
        for item in leases:
            if item.get("resource") == resource:
                return LeaseStatusResult(active=True, resource=resource, holder=item.get("holder"), expires_at=int(item["expires_at"]))
    return LeaseStatusResult(active=False, resource=resource)


def lease_list(settings: Settings) -> LeaseListResult:
    with _lease_lock(settings):
        leases = _load_leases(settings)
        _save_leases(settings, leases)
    return LeaseListResult(leases=[LeaseStatusResult(active=True, resource=str(item["resource"]), holder=item.get("holder"), expires_at=int(item["expires_at"])) for item in sorted(leases, key=lambda value: str(value.get("resource", "")))])


def lease_release(lease_id: str, settings: Settings) -> LeaseReleaseResult:
    if not _LEASE_ID_RE.fullmatch(lease_id):
        raise ValueError("Invalid lease_id")
    with _lease_lock(settings):
        leases = _load_leases(settings)
        remaining = [item for item in leases if item.get("lease_id") != lease_id]
        released = len(remaining) != len(leases)
        _save_leases(settings, remaining)
    return LeaseReleaseResult(lease_id=lease_id, released=released)


def inspect_paths(paths: list[str], *, include_sha256: bool, max_text_bytes: int, hash_max_bytes: int, settings: Settings) -> InspectPathsResult:
    if not paths or len(paths) > 20:
        raise ValueError("paths must contain between 1 and 20 items")
    if max_text_bytes < 0 or max_text_bytes > 16_384:
        raise ValueError("max_text_bytes must be between 0 and 16384")
    if hash_max_bytes < 0 or hash_max_bytes > 1_073_741_824:
        raise ValueError("hash_max_bytes must be between 0 and 1073741824")
    items: list[PathInspection] = []
    for raw in paths:
        try:
            info = path_info_impl(raw, settings)
            inspection = PathInspection(path=info.path, ok=True, type=info.type, size=info.size, mode=info.mode, modified_ns=info.modified_ns)
            if info.type == "file" and info.size is not None:
                with opened_beneath(settings, raw, os.O_RDONLY) as fd:
                    if include_sha256:
                        if info.size <= hash_max_bytes:
                            inspection.sha256 = _sha256_fd(fd)[0]
                        else:
                            inspection.hash_skipped = True
                    if max_text_bytes:
                        os.lseek(fd, 0, os.SEEK_SET)
                        data = os.read(fd, max_text_bytes + 1)
                        if b"\x00" not in data[:max_text_bytes]:
                            inspection.text_preview = data[:max_text_bytes].decode("utf-8", errors="replace")
                            inspection.preview_truncated = len(data) > max_text_bytes or info.size > max_text_bytes
            items.append(inspection)
        except (OSError, ValueError) as exc:
            items.append(PathInspection(path=raw, ok=False, error=str(exc)))
    return InspectPathsResult(items=items)


def _read_text_bounded(path: str, max_file_bytes: int, settings: Settings) -> tuple[str, str, int]:
    if max_file_bytes < 1 or max_file_bytes > 4_194_304:
        raise ValueError("max_file_bytes must be between 1 and 4194304")
    with opened_beneath(settings, path, os.O_RDONLY) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Path is not a regular file")
        if info.st_size > max_file_bytes:
            raise ValueError(f"File exceeds max_file_bytes={max_file_bytes}")
        data = bytearray()
        while True:
            chunk = os.read(fd, min(65_536, max_file_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_file_bytes:
                raise ValueError(f"File grew beyond max_file_bytes={max_file_bytes}")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("File is not valid UTF-8 text") from exc
    return text, hashlib.sha256(data).hexdigest(), info.st_size


def file_diff(path: str, proposed_text: str, *, max_file_bytes: int, max_diff_bytes: int, settings: Settings) -> FileDiffResult:
    if max_diff_bytes < 1024 or max_diff_bytes > 262_144:
        raise ValueError("max_diff_bytes must be between 1024 and 262144")
    proposed_raw = proposed_text.encode("utf-8")
    if len(proposed_raw) > max_file_bytes:
        raise ValueError("proposed_text exceeds max_file_bytes")
    current, current_sha, _ = _read_text_bounded(path, max_file_bytes, settings)
    proposed_sha = hashlib.sha256(proposed_raw).hexdigest()
    diff_text = "".join(difflib.unified_diff(current.splitlines(keepends=True), proposed_text.splitlines(keepends=True), fromfile=path, tofile=f"{path} (proposed)", n=3))
    encoded = diff_text.encode("utf-8")
    truncated = len(encoded) > max_diff_bytes
    if truncated:
        diff_text = encoded[:max_diff_bytes].decode("utf-8", errors="ignore") + "\n... [diff truncated]\n"
    return FileDiffResult(path=str(settings.resolve_allowed_path(path)), current_sha256=current_sha, proposed_sha256=proposed_sha, diff=diff_text, truncated=truncated)


def apply_line_patch(path: str, expected_sha256: str, edits: list[LineEdit], *, mode: int, max_file_bytes: int, settings: Settings) -> FileWriteResult:
    if not re.fullmatch(r"[A-Fa-f0-9]{64}", expected_sha256):
        raise ValueError("expected_sha256 must be 64 hex characters")
    if not edits or len(edits) > 100:
        raise ValueError("edits must contain between 1 and 100 operations")
    current, current_sha, _ = _read_text_bounded(path, max_file_bytes, settings)
    if current_sha.lower() != expected_sha256.lower():
        raise ValueError("File changed since expected_sha256 was observed")
    lines = current.splitlines(keepends=True)
    normalized = sorted(edits, key=lambda edit: edit.start_line)
    previous_end = 0
    for edit in normalized:
        start_index = edit.start_line - 1
        if start_index > len(lines):
            raise ValueError(f"start_line {edit.start_line} is beyond end of file")
        end_index = start_index + edit.delete_lines
        if end_index > len(lines):
            raise ValueError(f"edit at line {edit.start_line} deletes beyond end of file")
        if start_index < previous_end:
            raise ValueError("edits overlap")
        previous_end = end_index
    new_lines = list(lines)
    for edit in reversed(normalized):
        start_index = edit.start_line - 1
        end_index = start_index + edit.delete_lines
        replacement = edit.replacement.splitlines(keepends=True)
        if edit.replacement and not replacement:
            replacement = [edit.replacement]
        new_lines[start_index:end_index] = replacement
    result = "".join(new_lines)
    if len(result.encode("utf-8")) > max_file_bytes:
        raise ValueError("patched result exceeds max_file_bytes")
    return write_text_file_impl(path, result, True, expected_sha256.lower(), mode, settings)
