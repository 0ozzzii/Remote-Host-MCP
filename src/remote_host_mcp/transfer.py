from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .filesystem import hash_file, read_file_chunk
from .models import (
    DownloadInfoResult,
    ReadFileChunkResult,
    UploadBeginResult,
    UploadChunkResult,
    UploadFinishResult,
    UploadStatusResult,
)
from .secure_paths import open_beneath, opened_parent_beneath

_UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _ensure_state(settings: Settings) -> Path:
    root = settings.state_dir / "uploads"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(settings.state_dir, 0o700)
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _validate_upload_id(upload_id: str) -> str:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("Invalid upload_id")
    return upload_id


def _meta_path(upload_id: str, settings: Settings) -> Path:
    return _ensure_state(settings) / f"{_validate_upload_id(upload_id)}.json"


def _lock_path(upload_id: str, settings: Settings) -> Path:
    return _ensure_state(settings) / f"{_validate_upload_id(upload_id)}.lock"


@contextmanager
def _upload_lock(upload_id: str, settings: Settings) -> Iterator[None]:
    path = _lock_path(upload_id, settings)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_meta(meta: dict[str, Any], settings: Settings) -> None:
    path = _meta_path(str(meta["upload_id"]), settings)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    payload = json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_meta(upload_id: str, settings: Settings) -> dict[str, Any]:
    path = _meta_path(upload_id, settings)
    if not path.exists():
        raise ValueError("Unknown upload_id")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Upload metadata is unreadable") from exc
    if meta.get("upload_id") != upload_id:
        raise ValueError("Upload metadata identity mismatch")
    return meta


def _validate_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("SHA-256 must be a 64-character hexadecimal string")
    return value.lower()


def _validate_mode(mode: int) -> int:
    if mode < 0 or mode > 0o777:
        raise ValueError("mode must be between 0 and 0o777")
    return mode


def _lstat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validated_meta_paths(meta: dict[str, Any], settings: Settings) -> tuple[Path, Path]:
    upload_id = _validate_upload_id(str(meta.get("upload_id", "")))
    target_raw = str(meta.get("target_path", ""))
    staging_raw = str(meta.get("staging_path", ""))
    with opened_parent_beneath(settings, target_raw) as (_target_fd, target_name, target):
        with opened_parent_beneath(settings, staging_raw) as (_staging_fd, staging_name, staging):
            expected_name = f".{target_name}.rhmcp-upload-{upload_id}.part"
            if staging.parent != target.parent or staging_name != expected_name:
                raise ValueError("Upload metadata staging path is invalid")
    return target, staging


def _open_staging(staging: Path, flags: int, settings: Settings) -> int:
    fd = open_beneath(settings, str(staging), flags, no_symlinks=True)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Upload staging path is not a regular file")
        return fd
    except Exception:
        os.close(fd)
        raise


def _staging_size(staging: Path, settings: Settings) -> int:
    fd = _open_staging(staging, os.O_RDONLY, settings)
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def _unlink_allowed(path: Path, settings: Settings) -> None:
    try:
        with opened_parent_beneath(settings, str(path)) as (parent_fd, name, _normalized):
            try:
                os.unlink(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
    except (OSError, ValueError):
        pass


def _expire_if_needed(meta: dict[str, Any], settings: Settings) -> dict[str, Any]:
    if meta.get("status") == "open" and int(meta["expires_at"]) < int(time.time()):
        meta["status"] = "expired"
        _, staging = _validated_meta_paths(meta, settings)
        _unlink_allowed(staging, settings)
        _write_meta(meta, settings)
    return meta


def begin_upload(
    path: str,
    total_size: int,
    expected_sha256: str,
    mode: int,
    overwrite: bool,
    settings: Settings,
) -> UploadBeginResult:
    if total_size < 0 or total_size > settings.max_transfer_bytes:
        raise ValueError(f"total_size must be between 0 and {settings.max_transfer_bytes}")
    expected = _validate_sha256(expected_sha256)
    mode = _validate_mode(mode)
    upload_id = secrets.token_hex(16)

    with opened_parent_beneath(settings, path) as (parent_fd, target_name, target):
        target_stat = _lstat_at(parent_fd, target_name)
        if target_stat is not None and stat.S_ISLNK(target_stat.st_mode):
            raise ValueError("Refusing to upload through a symlink destination")
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            raise ValueError("Destination exists and is not a regular file")
        if target_stat is not None and not overwrite:
            raise ValueError("Destination exists and overwrite=false")

        staging_name = f".{target_name}.rhmcp-upload-{upload_id}.part"
        staging = target.parent / staging_name
        fd = os.open(
            staging_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.close(fd)
        os.fsync(parent_fd)
        replaced = target_stat is not None

    now = int(time.time())
    meta: dict[str, Any] = {
        "upload_id": upload_id,
        "target_path": str(target),
        "staging_path": str(staging),
        "total_size": total_size,
        "expected_sha256": expected,
        "mode": mode,
        "overwrite": bool(overwrite),
        "status": "open",
        "created_at": now,
        "expires_at": now + settings.upload_ttl_seconds,
        "committed_at": None,
        "final_sha256": None,
        "replaced": replaced,
    }
    try:
        with _upload_lock(upload_id, settings):
            _write_meta(meta, settings)
    except Exception:
        _unlink_allowed(staging, settings)
        try:
            _lock_path(upload_id, settings).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return UploadBeginResult(
        upload_id=upload_id,
        path=str(target),
        total_size=total_size,
        expected_sha256=expected,
        next_offset=0,
        max_chunk_bytes=settings.max_file_chunk_bytes,
        expires_at=int(meta["expires_at"]),
    )


def upload_chunk(
    upload_id: str,
    offset: int,
    data_base64: str,
    chunk_sha256: str | None,
    settings: Settings,
) -> UploadChunkResult:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    try:
        data = base64.b64decode(data_base64.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("data_base64 is not valid base64") from exc
    if len(data) > settings.max_file_chunk_bytes:
        raise ValueError(f"Decoded chunk exceeds {settings.max_file_chunk_bytes} bytes")
    if chunk_sha256 is not None:
        expected_chunk = _validate_sha256(chunk_sha256)
        actual_chunk = hashlib.sha256(data).hexdigest()
        if actual_chunk != expected_chunk:
            raise ValueError("Chunk SHA-256 mismatch")

    with _upload_lock(upload_id, settings):
        meta = _expire_if_needed(_load_meta(upload_id, settings), settings)
        if meta["status"] != "open":
            raise ValueError(f"Upload is not open (status={meta['status']})")
        _, staging = _validated_meta_paths(meta, settings)

        fd = _open_staging(staging, os.O_RDWR | os.O_APPEND, settings)
        try:
            current = os.fstat(fd).st_size
            total_size = int(meta["total_size"])

            if offset < current:
                if offset + len(data) > current:
                    raise ValueError("Chunk partially overlaps existing upload data")
                read_fd = _open_staging(staging, os.O_RDONLY, settings)
                try:
                    os.lseek(read_fd, offset, os.SEEK_SET)
                    existing = os.read(read_fd, len(data))
                finally:
                    os.close(read_fd)
                if existing != data:
                    raise ValueError("Conflicting retry: bytes at this offset differ")
                return UploadChunkResult(
                    upload_id=upload_id,
                    accepted=True,
                    duplicate=True,
                    bytes_received=current,
                    next_offset=current,
                    complete=current == total_size,
                )

            if offset > current:
                raise ValueError(f"Expected offset {current}, got {offset}")
            if current + len(data) > total_size:
                raise ValueError("Chunk would exceed declared total_size")

            if data:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("Short write while appending upload chunk")
                    view = view[written:]
            os.fsync(fd)
            new_size = current + len(data)
        finally:
            os.close(fd)

        return UploadChunkResult(
            upload_id=upload_id,
            accepted=True,
            duplicate=False,
            bytes_received=new_size,
            next_offset=new_size,
            complete=new_size == total_size,
        )


def upload_status(upload_id: str, settings: Settings) -> UploadStatusResult:
    with _upload_lock(upload_id, settings):
        meta = _expire_if_needed(_load_meta(upload_id, settings), settings)
        status_value = str(meta["status"])
        if status_value == "committed":
            received = int(meta["total_size"])
        elif status_value in {"aborted", "expired"}:
            received = 0
        else:
            _, staging = _validated_meta_paths(meta, settings)
            try:
                received = _staging_size(staging, settings)
            except (OSError, ValueError):
                received = 0
        return UploadStatusResult(
            upload_id=upload_id,
            path=str(meta["target_path"]),
            status=status_value,
            total_size=int(meta["total_size"]),
            bytes_received=received,
            expected_sha256=str(meta["expected_sha256"]),
            final_sha256=meta.get("final_sha256"),
            next_offset=received,
            expires_at=int(meta["expires_at"]),
        )


def finish_upload(upload_id: str, settings: Settings) -> UploadFinishResult:
    with _upload_lock(upload_id, settings):
        meta = _expire_if_needed(_load_meta(upload_id, settings), settings)
        if meta["status"] == "committed":
            return UploadFinishResult(
                success=True,
                upload_id=upload_id,
                path=str(meta["target_path"]),
                bytes_written=int(meta["total_size"]),
                sha256=str(meta["final_sha256"]),
                mode=f"{int(meta['mode']):04o}",
                replaced=bool(meta.get("replaced", False)),
                already_committed=True,
            )
        if meta["status"] != "open":
            raise ValueError(f"Upload cannot be finalized (status={meta['status']})")

        target, staging = _validated_meta_paths(meta, settings)
        with opened_parent_beneath(settings, str(target)) as (parent_fd, target_name, stable_target):
            staging_name = staging.name
            expected_staging = stable_target.parent / staging_name
            if expected_staging != staging:
                raise ValueError("Upload staging and target parents no longer match")

            fd = os.open(
                staging_name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                file_stat = os.fstat(fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError("Upload staging path is not a regular file")
                size = file_stat.st_size
                if size != int(meta["total_size"]):
                    raise ValueError(f"Upload incomplete: expected {meta['total_size']} bytes, have {size}")

                digest = hashlib.sha256()
                os.lseek(fd, 0, os.SEEK_SET)
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                actual = digest.hexdigest()
                if actual != str(meta["expected_sha256"]):
                    meta["status"] = "hash_mismatch"
                    meta["final_sha256"] = actual
                    _write_meta(meta, settings)
                    raise ValueError("Final SHA-256 mismatch; upload was not committed")

                target_stat = _lstat_at(parent_fd, target_name)
                if target_stat is not None and stat.S_ISLNK(target_stat.st_mode):
                    raise ValueError("Refusing to replace a symlink upload destination")
                if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
                    raise ValueError("Destination appeared and is not a regular file")
                if target_stat is not None and not bool(meta["overwrite"]):
                    raise ValueError("Destination appeared after upload began and overwrite=false")
                replaced = target_stat is not None
                os.fchmod(fd, int(meta["mode"]))
                os.fsync(fd)
                staging_identity = os.fstat(fd)
            finally:
                os.close(fd)

            current_identity = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(current_identity.st_mode):
                raise ValueError("Upload staging file changed during finalization")
            if (current_identity.st_dev, current_identity.st_ino) != (staging_identity.st_dev, staging_identity.st_ino):
                raise ValueError("Upload staging file changed during finalization")

            os.replace(staging_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)

        meta["status"] = "committed"
        meta["final_sha256"] = actual
        meta["committed_at"] = int(time.time())
        meta["replaced"] = replaced
        _write_meta(meta, settings)
        return UploadFinishResult(
            success=True,
            upload_id=upload_id,
            path=str(target),
            bytes_written=size,
            sha256=actual,
            mode=f"{int(meta['mode']):04o}",
            replaced=replaced,
            already_committed=False,
        )


def abort_upload(upload_id: str, settings: Settings) -> UploadStatusResult:
    with _upload_lock(upload_id, settings):
        meta = _load_meta(upload_id, settings)
        if meta["status"] == "committed":
            raise ValueError("Committed upload cannot be aborted")
        _, staging = _validated_meta_paths(meta, settings)
        _unlink_allowed(staging, settings)
        meta["status"] = "aborted"
        _write_meta(meta, settings)
        return UploadStatusResult(
            upload_id=upload_id,
            path=str(meta["target_path"]),
            status="aborted",
            total_size=int(meta["total_size"]),
            bytes_received=0,
            expected_sha256=str(meta["expected_sha256"]),
            final_sha256=meta.get("final_sha256"),
            next_offset=0,
            expires_at=int(meta["expires_at"]),
        )


def download_info(path: str, include_sha256: bool, settings: Settings) -> DownloadInfoResult:
    fd = open_beneath(settings, path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Path is not a regular file")
    finally:
        os.close(fd)
    digest = hash_file(path, settings).digest if include_sha256 else None
    return DownloadInfoResult(
        path=str(settings.resolve_allowed_path(path)),
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
        sha256=digest,
        max_chunk_bytes=settings.max_file_chunk_bytes,
    )


def download_chunk(path: str, offset: int, max_bytes: int, settings: Settings) -> ReadFileChunkResult:
    return read_file_chunk(path, offset, max_bytes, settings)
