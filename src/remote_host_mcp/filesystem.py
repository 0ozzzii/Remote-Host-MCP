from __future__ import annotations

import base64
import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path

from .config import Settings
from .models import (
    DirectoryEntry,
    FileWriteResult,
    HashFileResult,
    ListDirectoryResult,
    PathActionResult,
    PathInfoResult,
    ReadFileChunkResult,
    ReadTextFileResult,
)
from .secure_paths import open_beneath, opened_beneath, opened_parent_beneath

_SHA256_HEX_LEN = 64


def _is_sha256(value: str) -> bool:
    if len(value) != _SHA256_HEX_LEN:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _ensure_mode(mode: int) -> int:
    if mode < 0 or mode > 0o777:
        raise ValueError("mode must be between 0 and 0o777")
    return mode


def _path_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode):
        return "char_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "other"


def _reject_root_removal(path: Path, settings: Settings) -> None:
    for root in settings.allowed_roots:
        if path == root:
            raise ValueError("Refusing to remove a configured allowed root")


def _default_allowed_directory(settings: Settings) -> Path:
    try:
        return settings.resolve_allowed_path(None, default=Path.cwd())
    except ValueError:
        return settings.allowed_roots[0]


def _fstat_regular(fd: int, *, message: str = "Path is not a regular file") -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(message)
    return info


def _lstat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _read_fd_all(fd: int, chunk_size: int = 1024 * 1024) -> bytes:
    out = bytearray()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, chunk_size)
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def list_directory(path: str | None, limit: int, settings: Settings) -> ListDirectoryResult:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    display = _default_allowed_directory(settings) if path is None else Path(path).expanduser()
    with opened_beneath(
        settings,
        str(display),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    ) as dir_fd:
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            raise ValueError("Path is not a directory")
        names = sorted(os.listdir(dir_fd), key=str.lower)
        entries: list[DirectoryEntry] = []
        truncated = len(names) > limit
        for name in names[:limit]:
            try:
                item_stat = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                kind = _path_type(item_stat.st_mode)
                entries.append(
                    DirectoryEntry(
                        name=name,
                        type=kind,
                        size=item_stat.st_size if kind == "file" else None,
                        modified_ns=item_stat.st_mtime_ns,
                    )
                )
            except OSError:
                entries.append(DirectoryEntry(name=name, type="unreadable"))
    return ListDirectoryResult(path=str(settings.resolve_allowed_path(str(display))), entries=entries, truncated=truncated)


def path_info(path: str, settings: Settings) -> PathInfoResult:
    normalized = Path(os.path.normpath(str(Path(path).expanduser())))
    if normalized in settings.allowed_roots:
        item_stat = os.lstat(normalized)
        entry = normalized
        target = None
    else:
        with opened_parent_beneath(settings, str(normalized)) as (parent_fd, name, entry):
            item_stat = _lstat_at(parent_fd, name)
            if item_stat is None:
                raise ValueError("Path does not exist")
            target = os.readlink(name, dir_fd=parent_fd) if stat.S_ISLNK(item_stat.st_mode) else None
    kind = _path_type(item_stat.st_mode)
    return PathInfoResult(
        path=str(entry),
        type=kind,
        size=item_stat.st_size if kind == "file" else None,
        mode=f"{stat.S_IMODE(item_stat.st_mode):04o}",
        uid=item_stat.st_uid,
        gid=item_stat.st_gid,
        modified_ns=item_stat.st_mtime_ns,
        symlink_target=target,
    )


def read_text_file(path: str, start_line: int, max_lines: int, max_bytes: int, settings: Settings) -> ReadTextFileResult:
    if start_line < 1:
        raise ValueError("start_line must be >= 1")
    if max_lines < 1 or max_lines > 5000:
        raise ValueError("max_lines must be between 1 and 5000")
    if max_bytes < 256 or max_bytes > 131072:
        raise ValueError("max_bytes must be between 256 and 131072")

    with opened_beneath(settings, path, os.O_RDONLY) as fd:
        _fstat_regular(fd)
        pieces: list[bytes] = []
        used = 0
        returned = 0
        truncated = False
        with os.fdopen(os.dup(fd), "rb", closefd=True) as handle:
            for lineno, line in enumerate(handle, start=1):
                if lineno < start_line:
                    continue
                if returned >= max_lines:
                    truncated = True
                    break
                remaining = max_bytes - used
                if remaining <= 0:
                    truncated = True
                    break
                if len(line) > remaining:
                    pieces.append(line[:remaining])
                    used += remaining
                    returned += 1
                    truncated = True
                    break
                pieces.append(line)
                used += len(line)
                returned += 1
            if not truncated and handle.read(1):
                truncated = True

    text = b"".join(pieces).decode("utf-8", errors="replace")
    return ReadTextFileResult(
        path=str(settings.resolve_allowed_path(path)),
        start_line=start_line,
        lines_returned=returned,
        text=text,
        truncated=truncated,
    )


def read_file_chunk(path: str, offset: int, max_bytes: int, settings: Settings) -> ReadFileChunkResult:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if max_bytes < 1 or max_bytes > settings.max_file_chunk_bytes:
        raise ValueError(f"max_bytes must be between 1 and {settings.max_file_chunk_bytes}")
    with opened_beneath(settings, path, os.O_RDONLY) as fd:
        file_stat = _fstat_regular(fd)
        size = file_stat.st_size
        if offset > size:
            raise ValueError("offset is beyond end of file")
        os.lseek(fd, offset, os.SEEK_SET)
        data = os.read(fd, max_bytes)
    next_offset = offset + len(data)
    return ReadFileChunkResult(
        path=str(settings.resolve_allowed_path(path)),
        offset=offset,
        bytes_returned=len(data),
        data_base64=base64.b64encode(data).decode("ascii"),
        next_offset=next_offset,
        eof=next_offset >= size,
    )


def hash_file(path: str, settings: Settings) -> HashFileResult:
    digest = hashlib.sha256()
    total = 0
    with opened_beneath(settings, path, os.O_RDONLY) as fd:
        _fstat_regular(fd)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return HashFileResult(
        path=str(settings.resolve_allowed_path(path)),
        algorithm="sha256",
        digest=digest.hexdigest(),
        bytes_hashed=total,
    )


def write_text_file(
    path: str,
    text: str,
    overwrite: bool,
    expected_sha256: str | None,
    mode: int,
    settings: Settings,
) -> FileWriteResult:
    mode = _ensure_mode(mode)
    data = text.encode("utf-8")
    with opened_parent_beneath(settings, path) as (parent_fd, name, target):
        existing = _lstat_at(parent_fd, name)
        existed = existing is not None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError("Refusing to write through a symlink destination")
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("Destination exists and is not a regular file")
        if existed and not overwrite:
            raise ValueError("Destination exists and overwrite=false")
        if expected_sha256 is not None:
            expected = expected_sha256.lower()
            if not _is_sha256(expected):
                raise ValueError("expected_sha256 must be a 64-character hexadecimal SHA-256")
            if not existed:
                raise ValueError("expected_sha256 requires an existing destination file")
            current = hash_file(str(target), settings).digest
            if current != expected:
                raise ValueError("Destination SHA-256 no longer matches expected_sha256")

        tmp_name = f".{name}.rhmcp-write-{secrets.token_hex(8)}"
        fd: int | None = None
        try:
            fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=parent_fd)
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("Short write while writing file")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            # Re-check destination immediately before the atomic commit. A symlink
            # swapped in after the first check is replaced, never followed.
            latest = _lstat_at(parent_fd, name)
            if latest is not None and not overwrite and not existed:
                raise ValueError("Destination appeared after validation and overwrite=false")
            if latest is not None and not (stat.S_ISREG(latest.st_mode) or stat.S_ISLNK(latest.st_mode)):
                raise ValueError("Destination changed to an unsupported file type")
            os.replace(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass

    return FileWriteResult(
        success=True,
        path=str(target),
        bytes_written=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        mode=f"{mode:04o}",
        atomic=True,
        replaced=existed,
    )


def _root_for_lexical(path: Path, settings: Settings) -> tuple[Path, tuple[str, ...]]:
    for root in settings.allowed_roots:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        return root, rel.parts
    raise ValueError("Path is outside RHMCP_ALLOWED_ROOTS")


def make_directory(path: str, parents: bool, exist_ok: bool, mode: int, settings: Settings) -> PathActionResult:
    mode = _ensure_mode(mode)
    target = Path(os.path.normpath(str(Path(path).expanduser())))
    if not target.is_absolute():
        raise ValueError("Path must be absolute")
    root, parts = _root_for_lexical(target, settings)
    if not parts:
        if exist_ok:
            return PathActionResult(success=True, action="mkdir", path=str(root))
        raise FileExistsError(str(root))

    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    current_fd = root_fd
    try:
        for index, component in enumerate(parts):
            last = index == len(parts) - 1
            if not parents and not last:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            else:
                try:
                    os.mkdir(component, mode if last else 0o755, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    created = False
                    if last and not exist_ok:
                        raise
                next_fd = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
                if last and (created or exist_ok):
                    os.fchmod(next_fd, mode)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)
    return PathActionResult(success=True, action="mkdir", path=str(target))


def _remove_tree_at(parent_fd: int, name: str) -> None:
    item = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        for child in os.listdir(child_fd):
            _remove_tree_at(child_fd, child)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def move_path(source: str, destination: str, overwrite: bool, settings: Settings) -> PathActionResult:
    with opened_parent_beneath(settings, source) as (src_fd, src_name, src_path):
        src_stat = _lstat_at(src_fd, src_name)
        if src_stat is None:
            raise ValueError("Source does not exist")
        with opened_parent_beneath(settings, destination) as (dst_fd, dst_name, dst_path):
            dst_stat = _lstat_at(dst_fd, dst_name)
            if dst_stat is not None and not overwrite:
                raise ValueError("Destination exists and overwrite=false")
            if dst_stat is not None:
                if stat.S_ISDIR(dst_stat.st_mode) != stat.S_ISDIR(src_stat.st_mode):
                    raise ValueError("Source and destination types are incompatible")
                if stat.S_ISDIR(dst_stat.st_mode):
                    check_fd = os.open(dst_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=dst_fd)
                    try:
                        if os.listdir(check_fd):
                            raise ValueError("Refusing to overwrite a non-empty destination directory")
                    finally:
                        os.close(check_fd)
                    os.rmdir(dst_name, dir_fd=dst_fd)
            try:
                os.replace(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise ValueError("Secure cross-filesystem move is not supported; copy then remove explicitly") from exc
                raise
            os.fsync(dst_fd)
    return PathActionResult(success=True, action="move", path=str(src_path), destination=str(dst_path))


def _copy_file_at(src_parent: int, src_name: str, dst_parent: int, dst_name: str, mode: int) -> None:
    src_fd = os.open(src_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=src_parent)
    try:
        _fstat_regular(src_fd, message="Source changed and is not a regular file")
        dst_fd = os.open(
            dst_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=dst_parent,
        )
        try:
            while True:
                chunk = os.read(src_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(dst_fd, view)
                    if written <= 0:
                        raise OSError("Short write while copying file")
                    view = view[written:]
            os.fchmod(dst_fd, mode)
            os.fsync(dst_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _copy_tree_at(src_parent: int, src_name: str, dst_parent: int, dst_name: str) -> None:
    src_stat = os.stat(src_name, dir_fd=src_parent, follow_symlinks=False)
    mode = stat.S_IMODE(src_stat.st_mode)
    if stat.S_ISLNK(src_stat.st_mode):
        os.symlink(os.readlink(src_name, dir_fd=src_parent), dst_name, dir_fd=dst_parent)
        return
    if stat.S_ISREG(src_stat.st_mode):
        _copy_file_at(src_parent, src_name, dst_parent, dst_name, mode)
        return
    if not stat.S_ISDIR(src_stat.st_mode):
        raise ValueError("Only regular files, directories, and symlinks can be copied")
    os.mkdir(dst_name, mode=mode, dir_fd=dst_parent)
    src_fd = os.open(src_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=src_parent)
    dst_fd = os.open(dst_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=dst_parent)
    try:
        for child in os.listdir(src_fd):
            _copy_tree_at(src_fd, child, dst_fd, child)
        os.fsync(dst_fd)
    finally:
        os.close(dst_fd)
        os.close(src_fd)


def copy_path(source: str, destination: str, recursive: bool, overwrite: bool, settings: Settings) -> PathActionResult:
    with opened_parent_beneath(settings, source) as (src_fd, src_name, src_path):
        src_stat = _lstat_at(src_fd, src_name)
        if src_stat is None:
            raise ValueError("Source does not exist")
        if stat.S_ISDIR(src_stat.st_mode) and not recursive:
            raise ValueError("Source is a directory; recursive=true is required")
        with opened_parent_beneath(settings, destination) as (dst_fd, dst_name, dst_path):
            dst_stat = _lstat_at(dst_fd, dst_name)
            if dst_stat is not None:
                if not overwrite:
                    raise ValueError("Destination exists and overwrite=false")
                if stat.S_ISDIR(dst_stat.st_mode):
                    _remove_tree_at(dst_fd, dst_name)
                else:
                    os.unlink(dst_name, dir_fd=dst_fd)
            try:
                _copy_tree_at(src_fd, src_name, dst_fd, dst_name)
            except Exception:
                try:
                    if _lstat_at(dst_fd, dst_name) is not None:
                        _remove_tree_at(dst_fd, dst_name)
                except OSError:
                    pass
                raise
            os.fsync(dst_fd)
    return PathActionResult(success=True, action="copy", path=str(src_path), destination=str(dst_path))


def remove_path(path: str, recursive: bool, settings: Settings) -> PathActionResult:
    target = Path(os.path.normpath(str(Path(path).expanduser())))
    _reject_root_removal(target, settings)
    with opened_parent_beneath(settings, str(target)) as (parent_fd, name, normalized):
        item = _lstat_at(parent_fd, name)
        if item is None:
            raise ValueError("Path does not exist")
        if stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode):
            if recursive:
                _remove_tree_at(parent_fd, name)
            else:
                os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    return PathActionResult(success=True, action="remove", path=str(normalized))


def chmod_path(path: str, mode: int, settings: Settings) -> PathActionResult:
    mode = _ensure_mode(mode)
    info = path_info(path, settings)
    if info.type == "symlink":
        raise ValueError("Refusing chmod on a symlink")
    fd = open_beneath(settings, path, os.O_RDONLY, no_symlinks=True)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    return PathActionResult(success=True, action=f"chmod:{mode:04o}", path=info.path)
