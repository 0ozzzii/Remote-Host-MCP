from __future__ import annotations

import json
import re
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one occurrence, found {count}: {old[:80]!r}")
    write(path, text.replace(old, new, 1))


def replace_between(path: str, start: str, end: str, replacement: str) -> None:
    text = read(path)
    start_at = text.index(start)
    end_at = text.index(end, start_at)
    write(path, text[:start_at] + replacement.rstrip() + "\n\n" + text[end_at:])


# ---------------------------------------------------------------------------
# Linux atomic publish primitives. Fail closed if renameat2 is unavailable;
# this project targets Linux and must not silently fall back to racy check+rename.
# ---------------------------------------------------------------------------
write(
    "src/remote_host_mcp/atomic_fs.py",
    dedent('''\
    from __future__ import annotations

    import ctypes
    import errno
    import os

    _RENAME_NOREPLACE = 1
    _RENAME_EXCHANGE = 2
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _RENAMEAT2 = getattr(_LIBC, "renameat2", None)
    if _RENAMEAT2 is not None:
        _RENAMEAT2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        _RENAMEAT2.restype = ctypes.c_int


    def renameat2_available() -> bool:
        return _RENAMEAT2 is not None


    def _renameat2(src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str, flags: int) -> None:
        if _RENAMEAT2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is required for race-free filesystem publishing")
        result = _RENAMEAT2(
            src_dir_fd,
            os.fsencode(src_name),
            dst_dir_fd,
            os.fsencode(dst_name),
            flags,
        )
        if result != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), dst_name)


    def rename_noreplace(src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str) -> None:
        """Atomically publish src only if dst does not exist."""
        _renameat2(src_dir_fd, src_name, dst_dir_fd, dst_name, _RENAME_NOREPLACE)


    def rename_exchange(src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str) -> None:
        """Atomically exchange two existing directory entries."""
        _renameat2(src_dir_fd, src_name, dst_dir_fd, dst_name, _RENAME_EXCHANGE)
    '''),
)

# ---------------------------------------------------------------------------
# Filesystem data-integrity fixes.
# ---------------------------------------------------------------------------
replace_once(
    "src/remote_host_mcp/filesystem.py",
    "from .config import Settings\n",
    "from .atomic_fs import rename_exchange, rename_noreplace\nfrom .config import Settings\n",
)
replace_once(
    "src/remote_host_mcp/filesystem.py",
    "def _read_fd_all(fd: int, chunk_size: int = 1024 * 1024) -> bytes:\n    out = bytearray()\n    os.lseek(fd, 0, os.SEEK_SET)\n    while True:\n        chunk = os.read(fd, chunk_size)\n        if not chunk:\n            break\n        out.extend(chunk)\n    return bytes(out)\n",
    "def _read_fd_all(fd: int, chunk_size: int = 1024 * 1024) -> bytes:\n    out = bytearray()\n    os.lseek(fd, 0, os.SEEK_SET)\n    while True:\n        chunk = os.read(fd, chunk_size)\n        if not chunk:\n            break\n        out.extend(chunk)\n    return bytes(out)\n\n\ndef _hash_fd(fd: int) -> tuple[str, int]:\n    digest = hashlib.sha256()\n    total = 0\n    os.lseek(fd, 0, os.SEEK_SET)\n    while True:\n        chunk = os.read(fd, 1024 * 1024)\n        if not chunk:\n            break\n        digest.update(chunk)\n        total += len(chunk)\n    return digest.hexdigest(), total\n\n\ndef _content_identity(info: os.stat_result) -> tuple[int, int, int, int]:\n    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)\n\n\ndef _inode_identity(info: os.stat_result) -> tuple[int, int]:\n    return (info.st_dev, info.st_ino)\n",
)
replace_between(
    "src/remote_host_mcp/filesystem.py",
    "def read_text_file(",
    "def read_file_chunk(",
    dedent('''\
    def read_text_file(path: str, start_line: int, max_lines: int, max_bytes: int, settings: Settings) -> ReadTextFileResult:
        if start_line < 1:
            raise ValueError("start_line must be >= 1")
        if max_lines < 1 or max_lines > 5000:
            raise ValueError("max_lines must be between 1 and 5000")
        if max_bytes < 256 or max_bytes > 131072:
            raise ValueError("max_bytes must be between 256 and 131072")

        # Scan in fixed-size chunks. In particular, do not use file iteration/readline:
        # a hostile single line can otherwise allocate far beyond max_bytes before
        # the caller's byte budget is applied.
        with opened_beneath(settings, path, os.O_RDONLY) as fd:
            _fstat_regular(fd)
            output = bytearray()
            used = 0
            returned = 0
            line_no = 1
            in_selected_line = False
            truncated = False

            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                pos = 0
                while pos < len(chunk):
                    if line_no < start_line:
                        newline = chunk.find(b"\\n", pos)
                        if newline < 0:
                            pos = len(chunk)
                        else:
                            line_no += 1
                            pos = newline + 1
                        continue

                    if returned >= max_lines or used >= max_bytes:
                        truncated = True
                        break

                    if not in_selected_line:
                        returned += 1
                        in_selected_line = True

                    newline = chunk.find(b"\\n", pos)
                    end = len(chunk) if newline < 0 else newline + 1
                    segment = chunk[pos:end]
                    remaining = max_bytes - used
                    if len(segment) > remaining:
                        output.extend(segment[:remaining])
                        used += remaining
                        truncated = True
                        break

                    output.extend(segment)
                    used += len(segment)
                    pos = end
                    if newline >= 0:
                        line_no += 1
                        in_selected_line = False
                        if returned >= max_lines:
                            if pos < len(chunk) or os.read(fd, 1):
                                truncated = True
                            break
                if truncated:
                    break

        return ReadTextFileResult(
            path=str(settings.resolve_allowed_path(path)),
            start_line=start_line,
            lines_returned=returned,
            text=bytes(output).decode("utf-8", errors="replace"),
            truncated=truncated,
        )
    '''),
)
replace_between(
    "src/remote_host_mcp/filesystem.py",
    "def hash_file(",
    "def write_text_file(",
    dedent('''\
    def hash_file(path: str, settings: Settings) -> HashFileResult:
        with opened_beneath(settings, path, os.O_RDONLY) as fd:
            _fstat_regular(fd)
            digest, total = _hash_fd(fd)
        return HashFileResult(
            path=str(settings.resolve_allowed_path(path)),
            algorithm="sha256",
            digest=digest,
            bytes_hashed=total,
        )
    '''),
)
replace_between(
    "src/remote_host_mcp/filesystem.py",
    "def write_text_file(",
    "def _root_for_lexical(",
    dedent('''\
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

            expected: str | None = None
            expected_identity: tuple[int, int, int, int] | None = None
            if expected_sha256 is not None:
                expected = expected_sha256.lower()
                if not _is_sha256(expected):
                    raise ValueError("expected_sha256 must be a 64-character hexadecimal SHA-256")
                if not existed:
                    raise ValueError("expected_sha256 requires an existing destination file")
                current_fd = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                try:
                    _fstat_regular(current_fd, message="Destination changed and is not a regular file")
                    current_digest, _ = _hash_fd(current_fd)
                    current_info = os.fstat(current_fd)
                    expected_identity = _content_identity(current_info)
                finally:
                    os.close(current_fd)
                if current_digest != expected:
                    raise ValueError("Destination SHA-256 no longer matches expected_sha256")

            tmp_name = f".{name}.rhmcp-write-{secrets.token_hex(8)}"
            fd: int | None = None
            preserve_tmp = False
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    mode,
                    dir_fd=parent_fd,
                )
                os.fchmod(fd, mode)
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("Short write while writing file")
                    view = view[written:]
                os.fsync(fd)
                new_inode = _inode_identity(os.fstat(fd))
                os.close(fd)
                fd = None

                if expected is not None:
                    # RENAME_EXCHANGE makes the exact object that was replaced
                    # available under tmp_name. Verify that object, not a pathname
                    # observed before the commit. If a concurrent writer won the
                    # race, exchange back so their version remains the destination.
                    try:
                        rename_exchange(parent_fd, tmp_name, parent_fd, name)
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            raise ValueError("Destination disappeared during expected_sha256 guarded replacement") from exc
                        raise
                    old_fd = os.open(
                        tmp_name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_fd,
                    )
                    try:
                        old_info = _fstat_regular(old_fd, message="Destination changed during guarded replacement")
                        old_digest, _ = _hash_fd(old_fd)
                        old_identity = _content_identity(old_info)
                    finally:
                        os.close(old_fd)
                    if old_digest != expected or old_identity != expected_identity:
                        current_new = _lstat_at(parent_fd, name)
                        if current_new is not None and _inode_identity(current_new) == new_inode:
                            rename_exchange(parent_fd, tmp_name, parent_fd, name)
                            os.fsync(parent_fd)
                        else:
                            # Never destroy an unknown concurrent writer. The old
                            # destination is preserved under the hidden temp name for
                            # operator recovery instead of being unlinked blindly.
                            preserve_tmp = True
                        raise ValueError("Destination changed during expected_sha256 guarded replacement")
                    os.unlink(tmp_name, dir_fd=parent_fd)
                elif overwrite:
                    os.replace(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                else:
                    try:
                        rename_noreplace(parent_fd, tmp_name, parent_fd, name)
                    except OSError as exc:
                        if exc.errno == errno.EEXIST:
                            raise ValueError("Destination appeared during commit and overwrite=false") from exc
                        raise
                os.fsync(parent_fd)
            finally:
                if fd is not None:
                    os.close(fd)
                if not preserve_tmp:
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
    '''),
)
replace_once(
    "src/remote_host_mcp/filesystem.py",
    "                if last and (created or exist_ok):\n                    os.fchmod(next_fd, mode)\n",
    "                if last and created:\n                    os.fchmod(next_fd, mode)\n",
)
replace_between(
    "src/remote_host_mcp/filesystem.py",
    "def move_path(",
    "def _copy_file_at(",
    dedent('''\
    def move_path(source: str, destination: str, overwrite: bool, settings: Settings) -> PathActionResult:
        with opened_parent_beneath(settings, source) as (src_fd, src_name, src_path):
            src_stat = _lstat_at(src_fd, src_name)
            if src_stat is None:
                raise ValueError("Source does not exist")
            with opened_parent_beneath(settings, destination) as (dst_fd, dst_name, dst_path):
                if src_path == dst_path:
                    return PathActionResult(success=True, action="move", path=str(src_path), destination=str(dst_path))

                dst_stat = _lstat_at(dst_fd, dst_name)
                if dst_stat is None:
                    try:
                        rename_noreplace(src_fd, src_name, dst_fd, dst_name)
                        os.fsync(dst_fd)
                        if src_fd != dst_fd:
                            os.fsync(src_fd)
                        return PathActionResult(success=True, action="move", path=str(src_path), destination=str(dst_path))
                    except OSError as exc:
                        if exc.errno == errno.EXDEV:
                            raise ValueError("Secure cross-filesystem move is not supported; copy then remove explicitly") from exc
                        if exc.errno != errno.EEXIST:
                            raise
                        dst_stat = _lstat_at(dst_fd, dst_name)

                if dst_stat is not None and not overwrite:
                    raise ValueError("Destination exists and overwrite=false")
                if dst_stat is None:
                    raise ValueError("Destination changed during move")
                if stat.S_ISDIR(dst_stat.st_mode) != stat.S_ISDIR(src_stat.st_mode):
                    raise ValueError("Source and destination types are incompatible")
                if stat.S_ISDIR(dst_stat.st_mode):
                    check_fd = os.open(
                        dst_name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=dst_fd,
                    )
                    try:
                        if os.listdir(check_fd):
                            raise ValueError("Refusing to overwrite a non-empty destination directory")
                    finally:
                        os.close(check_fd)

                src_inode = _inode_identity(src_stat)
                try:
                    rename_exchange(src_fd, src_name, dst_fd, dst_name)
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise ValueError("Secure cross-filesystem move is not supported; copy then remove explicitly") from exc
                    raise
                try:
                    _remove_tree_at(src_fd, src_name)
                except Exception:
                    current_dst = _lstat_at(dst_fd, dst_name)
                    current_src = _lstat_at(src_fd, src_name)
                    if (
                        current_dst is not None
                        and current_src is not None
                        and _inode_identity(current_dst) == src_inode
                    ):
                        rename_exchange(src_fd, src_name, dst_fd, dst_name)
                        os.fsync(dst_fd)
                        if src_fd != dst_fd:
                            os.fsync(src_fd)
                    raise
                os.fsync(dst_fd)
                if src_fd != dst_fd:
                    os.fsync(src_fd)
        return PathActionResult(success=True, action="move", path=str(src_path), destination=str(dst_path))
    '''),
)
replace_between(
    "src/remote_host_mcp/filesystem.py",
    "def copy_path(",
    "def remove_path(",
    dedent('''\
    def copy_path(source: str, destination: str, recursive: bool, overwrite: bool, settings: Settings) -> PathActionResult:
        with opened_parent_beneath(settings, source) as (src_fd, src_name, src_path):
            src_stat = _lstat_at(src_fd, src_name)
            if src_stat is None:
                raise ValueError("Source does not exist")
            if stat.S_ISDIR(src_stat.st_mode) and not recursive:
                raise ValueError("Source is a directory; recursive=true is required")
            with opened_parent_beneath(settings, destination) as (dst_fd, dst_name, dst_path):
                if src_path == dst_path:
                    raise ValueError("Source and destination must differ")
                initial_dst = _lstat_at(dst_fd, dst_name)
                if initial_dst is not None and not overwrite:
                    raise ValueError("Destination exists and overwrite=false")

                staging_name = f".{dst_name}.rhmcp-copy-{secrets.token_hex(8)}.tmp"
                committed = False
                preserve_staging = False
                try:
                    # Build the complete copy beside the destination. A failed copy
                    # can therefore never destroy the previous destination.
                    _copy_tree_at(src_fd, src_name, dst_fd, staging_name)
                    staged = _lstat_at(dst_fd, staging_name)
                    if staged is None:
                        raise RuntimeError("Staged copy disappeared before commit")
                    staged_inode = _inode_identity(staged)
                    os.fsync(dst_fd)

                    current_dst = _lstat_at(dst_fd, dst_name)
                    if current_dst is None:
                        try:
                            rename_noreplace(dst_fd, staging_name, dst_fd, dst_name)
                            committed = True
                        except OSError as exc:
                            if exc.errno != errno.EEXIST:
                                raise
                            current_dst = _lstat_at(dst_fd, dst_name)
                    if not committed:
                        if current_dst is None:
                            raise ValueError("Destination changed during copy commit")
                        if not overwrite:
                            raise ValueError("Destination appeared during copy and overwrite=false")
                        rename_exchange(dst_fd, staging_name, dst_fd, dst_name)
                        committed = True
                        try:
                            _remove_tree_at(dst_fd, staging_name)
                        except Exception:
                            # Restore the old destination if the just-published copy is
                            # still exactly the object we staged. Never delete an
                            # unknown concurrent writer merely to make cleanup succeed.
                            now = _lstat_at(dst_fd, dst_name)
                            old = _lstat_at(dst_fd, staging_name)
                            if now is not None and old is not None and _inode_identity(now) == staged_inode:
                                rename_exchange(dst_fd, staging_name, dst_fd, dst_name)
                                committed = False
                            else:
                                preserve_staging = True
                            raise
                    os.fsync(dst_fd)
                finally:
                    if not committed and not preserve_staging:
                        try:
                            if _lstat_at(dst_fd, staging_name) is not None:
                                _remove_tree_at(dst_fd, staging_name)
                        except OSError:
                            pass
        return PathActionResult(success=True, action="copy", path=str(src_path), destination=str(dst_path))
    '''),
)

# ---------------------------------------------------------------------------
# Transfer commit/abort race semantics.
# ---------------------------------------------------------------------------
replace_once(
    "src/remote_host_mcp/transfer.py",
    "import binascii\nimport fcntl\n",
    "import binascii\nimport errno\nimport fcntl\n",
)
replace_once(
    "src/remote_host_mcp/transfer.py",
    "from .config import Settings\nfrom .filesystem import hash_file, read_file_chunk\n",
    "from .atomic_fs import rename_noreplace\nfrom .config import Settings\nfrom .filesystem import read_file_chunk\n",
)
replace_once(
    "src/remote_host_mcp/transfer.py",
    "            os.replace(staging_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)\n            os.fsync(parent_fd)\n\n        meta[\"status\"] = \"committed\"",
    "            if bool(meta[\"overwrite\"]):\n                if target_stat is None:\n                    try:\n                        rename_noreplace(parent_fd, staging_name, parent_fd, target_name)\n                        replaced = False\n                    except OSError as exc:\n                        if exc.errno != errno.EEXIST:\n                            raise\n                        latest = _lstat_at(parent_fd, target_name)\n                        if latest is not None and stat.S_ISLNK(latest.st_mode):\n                            raise ValueError(\"Refusing to replace a symlink upload destination\") from exc\n                        if latest is not None and not stat.S_ISREG(latest.st_mode):\n                            raise ValueError(\"Destination appeared and is not a regular file\") from exc\n                        os.replace(staging_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)\n                        replaced = True\n                else:\n                    os.replace(staging_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)\n                    replaced = True\n            else:\n                try:\n                    rename_noreplace(parent_fd, staging_name, parent_fd, target_name)\n                    replaced = False\n                except OSError as exc:\n                    if exc.errno == errno.EEXIST:\n                        raise ValueError(\"Destination appeared after upload began and overwrite=false\") from exc\n                    raise\n            os.fsync(parent_fd)\n\n        meta[\"status\"] = \"committed\"",
)
replace_between(
    "src/remote_host_mcp/transfer.py",
    "def download_info(",
    "def download_chunk(",
    dedent('''\
    def download_info(path: str, include_sha256: bool, settings: Settings) -> DownloadInfoResult:
        fd = open_beneath(settings, path, os.O_RDONLY)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Path is not a regular file")
            digest: str | None = None
            if include_sha256:
                hasher = hashlib.sha256()
                os.lseek(fd, 0, os.SEEK_SET)
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                digest = hasher.hexdigest()
        finally:
            os.close(fd)
        return DownloadInfoResult(
            path=str(settings.resolve_allowed_path(path)),
            size=info.st_size,
            modified_ns=info.st_mtime_ns,
            sha256=digest,
            max_chunk_bytes=settings.max_file_chunk_bytes,
        )
    '''),
)

# ---------------------------------------------------------------------------
# Configuration conflict detection: ambiguous dual namespaces now fail closed
# without ever printing either value.
# ---------------------------------------------------------------------------
replace_between(
    "src/remote_host_mcp/config.py",
    "def _raw(",
    "def _int(",
    dedent('''\
    def _raw(suffix: str, default: str | None = None) -> str | None:
        """Read migration-compatible configuration without silent conflicts."""
        canonical_name = f"{_CANONICAL_PREFIX}{suffix}"
        legacy_name = f"{_LEGACY_PREFIX}{suffix}"
        canonical = os.getenv(canonical_name)
        legacy = os.getenv(legacy_name)
        if canonical is not None and legacy is not None and canonical != legacy:
            raise ConfigError(
                f"Conflicting configuration: {canonical_name} and {legacy_name} are both set differently; "
                "remove one namespace or make the values identical"
            )
        if canonical is not None:
            return canonical
        if legacy is not None:
            return legacy
        return default
    '''),
)
replace_once(
    "src/remote_host_mcp/config.py",
    "    @property\n    def public_url(self) -> str:\n        return f\"https://{self.public_host}{self.mcp_path}\"\n",
    "    @property\n    def public_url(self) -> str:\n        return f\"https://{self.public_host}{self.mcp_path}\"\n\n    @property\n    def redacted_public_url(self) -> str:\n        if self.auth_mode == \"oauth\":\n            return self.public_url\n        return f\"https://{self.public_host}/mcp/[REDACTED]\"\n",
)
replace_once(
    "src/remote_host_mcp/compat_env.py",
    "import os\n\n_SUFFIXES",
    "import os\n\nfrom .config import ConfigError\n\n_SUFFIXES",
)
replace_once(
    "src/remote_host_mcp/compat_env.py",
    "def apply_env_compat() -> None:\n    \"\"\"Map RHMCP_* to the legacy DSW_MCP_* implementation namespace.\n\n    This is intentionally one-way. A deployed DSW profile can keep its existing\n    DSW_MCP_* file unchanged, while new generic installations use RHMCP_*.\n    \"\"\"\n    for suffix in _SUFFIXES:\n        generic = f\"RHMCP_{suffix}\"\n        legacy = f\"DSW_MCP_{suffix}\"\n        if legacy not in os.environ and generic in os.environ:\n            os.environ[legacy] = os.environ[generic]\n",
    "def env_namespace_summary() -> dict[str, str]:\n    \"\"\"Report only namespace provenance, never configuration values.\"\"\"\n    summary: dict[str, str] = {}\n    for suffix in _SUFFIXES:\n        generic = f\"RHMCP_{suffix}\"\n        legacy = f\"DSW_MCP_{suffix}\"\n        generic_value = os.environ.get(generic)\n        legacy_value = os.environ.get(legacy)\n        if generic_value is not None and legacy_value is not None:\n            summary[suffix] = \"both-equal\" if generic_value == legacy_value else \"conflict\"\n        elif generic_value is not None:\n            summary[suffix] = \"RHMCP\"\n        elif legacy_value is not None:\n            summary[suffix] = \"DSW_MCP\"\n        else:\n            summary[suffix] = \"default\"\n    return summary\n\n\ndef apply_env_compat() -> None:\n    \"\"\"Map RHMCP_* to legacy names only when the mapping is unambiguous.\"\"\"\n    for suffix in _SUFFIXES:\n        generic = f\"RHMCP_{suffix}\"\n        legacy = f\"DSW_MCP_{suffix}\"\n        generic_value = os.environ.get(generic)\n        legacy_value = os.environ.get(legacy)\n        if generic_value is not None and legacy_value is not None and generic_value != legacy_value:\n            raise ConfigError(\n                f\"Conflicting configuration: {generic} and {legacy} are both set differently; \"\n                \"remove one namespace or make the values identical\"\n            )\n        if legacy_value is None and generic_value is not None:\n            os.environ[legacy] = generic_value\n",
)

# ---------------------------------------------------------------------------
# Terminal semantic-parser memory bound.
# ---------------------------------------------------------------------------
replace_once(
    "src/remote_host_mcp/terminal.py",
    "_RHMCP_OSC_RE = re.compile(rb\"\\x1b\\]133;([CD]);id=([a-f0-9]{24})(?:;rc=(-?\\d+))?\\x07\")\n",
    "_RHMCP_OSC_RE = re.compile(rb\"\\x1b\\]133;([CD]);id=([a-f0-9]{24})(?:;rc=(-?\\d+))?\\x07\")\n_RHMCP_OSC_TAIL_MAX_BYTES = 256\n",
)
replace_once(
    "src/remote_host_mcp/terminal.py",
    "        return cleaned\n\n    def append_output",
    "        if len(self.osc_tail) > _RHMCP_OSC_TAIL_MAX_BYTES:\n            # A malformed OSC prefix without BEL must not grow an unbounded hidden\n            # buffer. Flush it as ordinary output, retaining only a possible split\n            # prefix for the next read.\n            tail = self.osc_tail\n            keep = b\"\"\n            max_prefix = min(len(_RHMCP_OSC_PREFIX) - 1, len(tail))\n            for size in range(max_prefix, 0, -1):\n                if tail[-size:] == _RHMCP_OSC_PREFIX[:size]:\n                    keep = tail[-size:]\n                    break\n            cleaned += tail[: len(tail) - len(keep)] if keep else tail\n            self.osc_tail = keep\n        return cleaned\n\n    def append_output",
)
replace_once(
    "src/remote_host_mcp/terminal.py",
    "            with self.condition:\n                self.buffer.extend(tail)\n                self.condition.notify_all()\n",
    "            with self.condition:\n                self.buffer.extend(tail)\n                excess = len(self.buffer) - self.max_buffer_bytes\n                if excess > 0:\n                    del self.buffer[:excess]\n                    self.buffer_start += excess\n                self.condition.notify_all()\n",
)

# ---------------------------------------------------------------------------
# Durable-job no-replay tombstones and process-group identity checks.
# ---------------------------------------------------------------------------
replace_once(
    "src/remote_host_mcp/jobs.py",
    "def proc_identity_alive(pid: int | None, start_ticks: int | None) -> bool:\n    if pid is None or pid <= 0:\n        return False\n    current = proc_start_ticks(pid)\n    if current is None:\n        return False\n    if start_ticks is None:\n        return True\n    return current == start_ticks\n",
    "def proc_identity_alive(pid: int | None, start_ticks: int | None) -> bool:\n    if pid is None or pid <= 0:\n        return False\n    current = proc_start_ticks(pid)\n    if current is None:\n        return False\n    if start_ticks is None:\n        return True\n    return current == start_ticks\n\n\ndef proc_group_identity_alive(pid: int | None, start_ticks: int | None) -> bool:\n    if not proc_identity_alive(pid, start_ticks):\n        return False\n    assert pid is not None\n    try:\n        return os.getpgid(pid) == pid\n    except (OSError, ProcessLookupError):\n        return False\n",
)
replace_once(
    "src/remote_host_mcp/jobs.py",
    "                idem_file.unlink(missing_ok=True)\n\n        if _active_job_count(settings)",
    "                raise ValueError(\n                    \"idempotency_key refers to a cleaned durable job and cannot be replayed\"\n                )\n\n        if _active_job_count(settings)",
)
replace_once(
    "src/remote_host_mcp/jobs.py",
    "    if proc_identity_alive(pid, start_ticks):\n        # start_new_session=True makes the job leader PID its process-group ID.\n",
    "    if proc_group_identity_alive(pid, start_ticks):\n        # start_new_session=True makes the job leader PID its process-group ID.\n",
)
replace_once(
    "src/remote_host_mcp/jobs.py",
    "        if not proc_identity_alive(meta.get(\"child_pid\"), meta.get(\"child_start_ticks\")):\n            break\n",
    "        if not proc_group_identity_alive(meta.get(\"child_pid\"), meta.get(\"child_start_ticks\")):\n            break\n",
)
replace_once(
    "src/remote_host_mcp/jobs.py",
    "    if proc_identity_alive(meta.get(\"child_pid\"), meta.get(\"child_start_ticks\")):\n        try:\n            os.killpg(int(meta[\"child_pid\"]), signal.SIGKILL)\n",
    "    if proc_group_identity_alive(meta.get(\"child_pid\"), meta.get(\"child_start_ticks\")):\n        try:\n            os.killpg(int(meta[\"child_pid\"]), signal.SIGKILL)\n",
)
replace_once(
    "src/remote_host_mcp/jobs.py",
    "                    if stored.get(\"job_id\") == job_id:\n                        mapping.unlink(missing_ok=True)\n",
    "                    if stored.get(\"job_id\") == job_id:\n                        stored[\"tombstone\"] = True\n                        stored[\"cleaned_at\"] = int(time.time())\n                        atomic_json_write(mapping, stored)\n",
)

# ---------------------------------------------------------------------------
# Schema precision and top-level additionalProperties=false.
# ---------------------------------------------------------------------------
replace_once(
    "src/remote_host_mcp/host_server.py",
    "from typing import Annotated\n",
    "from typing import Annotated, Literal\n",
)
replace_once(
    "src/remote_host_mcp/host_server.py",
    "signal_name: Annotated[str, Field(description=\"INT, QUIT, TSTP, TERM, HUP, or CONT.\")],",
    "signal_name: Annotated[Literal[\"INT\", \"QUIT\", \"TSTP\", \"TERM\", \"HUP\", \"CONT\", \"SIGINT\", \"SIGQUIT\", \"SIGTSTP\", \"SIGTERM\", \"SIGHUP\", \"SIGCONT\"], Field(description=\"Validated terminal signal.\")],",
)
replace_once(
    "src/remote_host_mcp/host_server.py",
    "signal_name: Annotated[str, Field(description=\"INT, HUP, TERM, KILL, STOP, or CONT.\")],",
    "signal_name: Annotated[Literal[\"INT\", \"HUP\", \"TERM\", \"KILL\", \"STOP\", \"CONT\", \"SIGINT\", \"SIGHUP\", \"SIGTERM\", \"SIGKILL\", \"SIGSTOP\", \"SIGCONT\"], Field(description=\"Validated process signal.\")],",
)
replace_once(
    "src/remote_host_mcp/host_server.py",
    "action: Annotated[str, Field(description=\"start, stop, or restart.\")],",
    "action: Annotated[Literal[\"start\", \"stop\", \"restart\"], Field(description=\"Validated systemd action.\")],",
)
# Tighten common opaque identifiers and hash strings on the model-facing tool schema.
server_text = read("src/remote_host_mcp/server.py")
server_text = server_text.replace(
    'Field(min_length=64, max_length=64, description="Expected final SHA-256 hex digest.")',
    'Field(min_length=64, max_length=64, pattern=r"^[A-Fa-f0-9]{64}$", description="Expected final SHA-256 hex digest.")',
)
server_text = server_text.replace(
    'Field(min_length=32, max_length=32, description="Upload handle',
    'Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$", description="Upload handle',
)
server_text = server_text.replace(
    'annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),\n        structured_output=True,\n    )\n    async def upload_abort',
    'annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),\n        structured_output=True,\n    )\n    async def upload_abort',
)
server_text = server_text.replace(
    'Field(description="Allow replacing/merging an existing destination.")',
    'Field(description="Allow atomically replacing an existing destination after the complete copy is staged.")',
)
write("src/remote_host_mcp/server.py", server_text)

replace_once(
    "src/remote_host_mcp/branding.py",
    "        tool.parameters = _rewrite_schema(tool.parameters)\n",
    "        parameters = _rewrite_schema(tool.parameters)\n        if isinstance(parameters, dict) and parameters.get(\"type\") == \"object\":\n            parameters[\"additionalProperties\"] = False\n        tool.parameters = parameters\n",
)

# ---------------------------------------------------------------------------
# Build provenance and read-only doctor diagnostics.
# ---------------------------------------------------------------------------
write(
    "src/remote_host_mcp/provenance.py",
    dedent('''\
    from __future__ import annotations

    import os
    import re
    from pathlib import Path

    _COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
    _REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


    def _valid_commit(value: str | None) -> str | None:
        value = (value or "").strip()
        return value.lower() if _COMMIT_RE.fullmatch(value) else None


    def _valid_ref(value: str | None) -> str | None:
        value = (value or "").strip()
        if not _REF_RE.fullmatch(value) or ".." in value:
            return None
        return value


    def _git_dir() -> Path | None:
        root = Path(__file__).resolve().parents[2]
        candidate = root / ".git"
        return candidate if candidate.is_dir() else None


    def _read_git() -> tuple[str | None, str | None]:
        git = _git_dir()
        if git is None:
            return None, None
        try:
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return None, None
        if not head.startswith("ref: "):
            return _valid_commit(head), None
        ref = _valid_ref(head[5:].strip())
        if ref is None:
            return None, None
        ref_path = git / ref
        try:
            commit = _valid_commit(ref_path.read_text(encoding="utf-8").strip())
        except OSError:
            commit = None
        if commit is None:
            try:
                packed = (git / "packed-refs").read_text(encoding="utf-8")
            except OSError:
                packed = ""
            for line in packed.splitlines():
                if line.startswith("#") or line.startswith("^"):
                    continue
                fields = line.split(" ", 1)
                if len(fields) == 2 and fields[1] == ref:
                    commit = _valid_commit(fields[0])
                    break
        return commit, ref


    def get_build_provenance() -> tuple[str | None, str | None]:
        commit = _valid_commit(os.getenv("RHMCP_BUILD_COMMIT") or os.getenv("DSW_MCP_BUILD_COMMIT"))
        ref = _valid_ref(os.getenv("RHMCP_BUILD_REF") or os.getenv("DSW_MCP_BUILD_REF"))
        git_commit, git_ref = _read_git()
        return commit or git_commit, ref or git_ref
    '''),
)
replace_once(
    "src/remote_host_mcp/models.py",
    "    version: str\n    hostname: str\n",
    "    version: str\n    build_commit: str | None = None\n    build_ref: str | None = None\n    hostname: str\n",
)
replace_once(
    "src/remote_host_mcp/server.py",
    "from .models import (\n",
    "from .provenance import get_build_provenance\nfrom .models import (\n",
)
replace_once(
    "src/remote_host_mcp/server.py",
    "        return StatusResult(\n            online=True,\n            service=\"DSW Direct Control\",\n            version=__version__,\n",
    "        build_commit, build_ref = get_build_provenance()\n        return StatusResult(\n            online=True,\n            service=\"DSW Direct Control\",\n            version=__version__,\n            build_commit=build_commit,\n            build_ref=build_ref,\n",
)
replace_once(
    "src/remote_host_mcp/server.py",
    "    async def health(_request: Request) -> Response:\n        return JSONResponse(\n            {\n                \"ok\": True,\n                \"service\": \"DSW Direct Control\",\n                \"version\": __version__,\n                \"transport\": \"streamable-http\",\n            },",
    "    async def health(_request: Request) -> Response:\n        build_commit, build_ref = get_build_provenance()\n        return JSONResponse(\n            {\n                \"ok\": True,\n                \"service\": \"DSW Direct Control\",\n                \"version\": __version__,\n                \"build_commit\": build_commit,\n                \"build_ref\": build_ref,\n                \"transport\": \"streamable-http\",\n            },",
)
replace_once(
    "src/remote_host_mcp/branding.py",
    "from . import __version__\n",
    "from . import __version__\nfrom .provenance import get_build_provenance\n",
)
replace_once(
    "src/remote_host_mcp/branding.py",
    "async def _health(_request: Request) -> Response:\n    return JSONResponse(\n        {\n            \"ok\": True,\n            \"service\": PRODUCT_NAME,\n            \"version\": __version__,\n            \"transport\": \"streamable-http\",\n        },",
    "async def _health(_request: Request) -> Response:\n    build_commit, build_ref = get_build_provenance()\n    return JSONResponse(\n        {\n            \"ok\": True,\n            \"service\": PRODUCT_NAME,\n            \"version\": __version__,\n            \"build_commit\": build_commit,\n            \"build_ref\": build_ref,\n            \"transport\": \"streamable-http\",\n        },",
)
write(
    "src/remote_host_mcp/doctor.py",
    dedent('''\
    from __future__ import annotations

    import json
    import os
    from pathlib import Path
    from typing import Any

    from . import __version__
    from .atomic_fs import renameat2_available
    from .compat_env import env_namespace_summary
    from .config import ConfigError, Settings
    from .provenance import get_build_provenance
    from .secure_paths import openat2_supported
    from .system_helpers import _systemd_manager_capability


    def collect_report(settings: Settings, *, sources: dict[str, str] | None = None) -> dict[str, Any]:
        build_commit, build_ref = get_build_provenance()
        try:
            openat2 = openat2_supported(settings)
        except (OSError, ValueError):
            openat2 = False
        systemd = _systemd_manager_capability()
        state_parent = settings.state_dir if settings.state_dir.exists() else settings.state_dir.parent
        return {
            "service": "Remote Host MCP",
            "version": __version__,
            "build": {"commit": build_commit, "ref": build_ref},
            "configuration": {
                "auth_mode": settings.auth_mode,
                "public_host": settings.public_host,
                "public_url": settings.redacted_public_url,
                "bind_host": settings.bind_host,
                "port": settings.port,
                "state_dir": str(settings.state_dir),
                "allowed_roots": [str(root) for root in settings.allowed_roots],
                "namespace_sources": sources if sources is not None else env_namespace_summary(),
            },
            "capabilities": {
                "openat2": openat2,
                "renameat2": renameat2_available(),
                "state_dir_parent_writable": os.access(state_parent, os.W_OK),
                "allowed_roots_exist": all(Path(root).exists() for root in settings.allowed_roots),
                "systemd": {
                    "systemctl_present": systemd.systemctl_present,
                    "pid1_is_systemd": systemd.pid1_is_systemd,
                    "manager_reachable": systemd.manager_reachable,
                    "operational": systemd.operational,
                    "reason": systemd.reason,
                },
            },
        }


    def main() -> None:
        sources = env_namespace_summary()
        try:
            settings = Settings.from_env()
        except ConfigError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
            raise SystemExit(2) from exc
        report = collect_report(settings, sources=sources)
        report["ok"] = True
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))


    if __name__ == "__main__":
        main()
    '''),
)

# ---------------------------------------------------------------------------
# Version the aggregate hardening candidate separately from alpha.1.
# ---------------------------------------------------------------------------
replace_once("pyproject.toml", 'version = "0.2.0a1"', 'version = "0.2.0a2"')
replace_once(
    "pyproject.toml",
    'remote-host-mcp = "remote_host_mcp.main:main"\ndsw-direct-mcp = "remote_host_mcp.main:main"',
    'remote-host-mcp = "remote_host_mcp.main:main"\nremote-host-mcp-doctor = "remote_host_mcp.doctor:main"\ndsw-direct-mcp = "remote_host_mcp.main:main"',
)
replace_once("src/remote_host_mcp/__init__.py", '__version__ = "0.2.0a1"', '__version__ = "0.2.0a2"')
write("VERSION", "0.2.0-alpha.2\n")
for test_path in (ROOT / "tests").glob("*.py"):
    text = test_path.read_text(encoding="utf-8")
    text = text.replace("0.2.0a1", "0.2.0a2").replace("0.2.0-alpha.1", "0.2.0-alpha.2")
    test_path.write_text(text, encoding="utf-8")

# ---------------------------------------------------------------------------
# Exact 43-tool manifest snapshot.
# ---------------------------------------------------------------------------
manifest = [
    "status", "list_directory", "path_info", "read_text_file", "read_file_chunk", "hash_file",
    "write_text_file", "make_directory", "move_path", "copy_path", "remove_path", "chmod_path",
    "upload_begin", "upload_chunk", "upload_status", "upload_finish", "upload_abort", "download_info",
    "download_chunk", "exec", "job_run", "job_start", "job_status", "job_read", "job_cancel", "job_list",
    "job_cleanup", "terminal_open", "terminal_exec", "terminal_write", "terminal_read", "terminal_status",
    "terminal_screen", "terminal_resize", "terminal_signal", "terminal_list", "terminal_close", "process_list",
    "process_info", "process_signal", "service_status", "service_action", "system_info",
]
write("tests/tool_manifest.json", json.dumps(manifest, indent=2) + "\n")

# ---------------------------------------------------------------------------
# Targeted audit-closure regression suite.
# ---------------------------------------------------------------------------
write(
    "tests/test_audit_closure.py",
    dedent('''\
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
    '''),
)

# ---------------------------------------------------------------------------
# Audit closure document: distinguishes fixed code from findings already safe.
# ---------------------------------------------------------------------------
write(
    "docs/AUDIT_CLOSURE_20260913.md",
    dedent('''\
    # Remote Host MCP audit closure — 2026-09-13

    This change set closes the remaining findings consolidated from the functional smoke test and the two code/security reviews. It is a code/CI candidate only; production DSW deployment is intentionally deferred.

    ## Code changes

    - Race-free `overwrite=false` publication uses Linux `renameat2(RENAME_NOREPLACE)` for text writes, copies, moves and upload finalization.
    - Guarded text replacement uses `RENAME_EXCHANGE` plus verification/rollback so a concurrent destination replacement is not destroyed.
    - `copy_path(overwrite=true)` stages the entire copy before atomic publication; failed copies leave the previous destination intact.
    - `make_directory(exist_ok=true)` no longer chmods an already-existing directory.
    - `read_text_file` scans with fixed-size `os.read` chunks so a single unbroken line cannot bypass the memory budget.
    - Generic/legacy configuration namespaces fail closed when both are present with different values; error text contains variable names, never values.
    - OSC 133 partial-marker state has a strict 256-byte ceiling.
    - Durable job cleanup leaves an idempotency tombstone; an already-consumed key cannot replay arbitrary shell after cleanup.
    - Durable job cancellation validates both PID start ticks and process-group identity before group signalling.
    - High-risk signal/action schemas expose explicit enums; canonical tool schemas reject unknown top-level arguments.
    - Status/health expose optional build commit/ref provenance. `remote-host-mcp-doctor` emits read-only, secret-free machine diagnostics.
    - Exact 43-tool manifest is pinned in `tests/tool_manifest.json`.

    ## Findings closed by verification rather than rewrite

    - `upload_abort` already refuses committed uploads and only removes staging data; regression coverage now protects that invariant.
    - Filesystem traversal already uses `openat2(RESOLVE_BENEATH|RESOLVE_NO_MAGICLINKS)` with stable dirfds and a conservative fallback; the new atomic-publish primitives close the remaining destination-entry race.
    - OAuth mode is a real external OAuth/OIDC resource-server integration using MCP SDK `AuthSettings` plus JWT issuer/audience/signature/expiry checks; capability mode does not pretend to be OAuth.
    - Short exec cancellation already terminates/reaps its process group; terminal close already terminates foreground work and joins the reader thread.
    - Durable recovery already observes persisted PID+start-ticks state and never replays `command.bin`; the new tombstone closes replay after cleanup.

    ## Release identity

    Candidate version: `0.2.0-alpha.2` / Python `0.2.0a2`.
    Production DSW remains unchanged until a separate controlled deployment and targeted post-deploy validation.
    '''),
)

# Remove this one-shot transformer and its temporary workflow from the final tree.
(Path(__file__)).unlink(missing_ok=True)
(ROOT / ".github" / "workflows" / "audit-hardening-apply.yml").unlink(missing_ok=True)
