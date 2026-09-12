from __future__ import annotations

import ctypes
import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol


class _PathSettings(Protocol):
    allowed_roots: tuple[Path, ...]

    def _is_under_allowed_root(self, path: Path) -> bool: ...
    def resolve_allowed_path(self, raw: str | None, *, default: Path | None = None) -> Path: ...
    def resolve_allowed_entry(self, raw: str) -> Path: ...


# Linux openat2(2). The syscall number is 437 on x86_64 and the asm-generic
# architectures used by current ModelScope/Linux deployments (including arm64).
_SYS_OPENAT2 = 437
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08

_LIBC = ctypes.CDLL(None, use_errno=True)


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_ulonglong),
        ("mode", ctypes.c_ulonglong),
        ("resolve", ctypes.c_ulonglong),
    ]


def _lexical_root_and_relative(settings: _PathSettings, raw: str) -> tuple[Path, Path, Path]:
    path = Path(os.path.normpath(str(Path(raw).expanduser())))
    if not path.is_absolute():
        raise ValueError("Path must be absolute")
    for root in settings.allowed_roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        return root, relative, path
    raise ValueError("Path is outside RHMCP_ALLOWED_ROOTS")


def _openat2(dir_fd: int, relative: Path, flags: int, mode: int, *, no_symlinks: bool = False) -> int:
    rel = os.fsencode(str(relative) if str(relative) else ".")
    resolve = _RESOLVE_BENEATH | _RESOLVE_NO_MAGICLINKS
    if no_symlinks:
        resolve |= _RESOLVE_NO_SYMLINKS
    how = _OpenHow(flags=flags, mode=mode, resolve=resolve)
    result = _LIBC.syscall(
        ctypes.c_long(_SYS_OPENAT2),
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(rel),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fsdecode(rel))
    return int(result)


def _fd_resolved_path(fd: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}")).resolve(strict=False)
    except OSError:
        return None


def _verify_fd_beneath(fd: int, settings: _PathSettings) -> None:
    resolved = _fd_resolved_path(fd)
    if resolved is not None and not settings._is_under_allowed_root(resolved):
        os.close(fd)
        raise ValueError("Opened path escaped RHMCP_ALLOWED_ROOTS")


def _open_canonical_with_kernel(
    settings: _PathSettings,
    safe: Path,
    flags: int,
    mode: int,
) -> int | None:
    """Open an already canonical allowed target without allowing new symlinks.

    RESOLVE_BENEATH intentionally rejects absolute symlink targets even when they
    ultimately point back inside the same allowed root. To preserve the old V2
    behavior safely, we resolve such a link once, verify the canonical target is
    allowed, then reopen that canonical path under the allowed-root dirfd with
    RESOLVE_NO_SYMLINKS. A concurrent symlink swap therefore cannot redirect use.
    """

    root, relative, _lexical = _lexical_root_and_relative(settings, str(safe))
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, root_flags)
    try:
        try:
            return _openat2(
                root_fd,
                relative,
                flags | getattr(os, "O_CLOEXEC", 0),
                mode,
                no_symlinks=True,
            )
        except OSError as exc:
            if exc.errno in {errno.ENOSYS, errno.EINVAL, errno.EPERM}:
                return None
            if exc.errno in {errno.EXDEV, errno.ELOOP}:
                raise ValueError("Canonical path changed during secure open") from exc
            raise
    finally:
        os.close(root_fd)


def open_beneath(
    settings: _PathSettings,
    raw: str,
    flags: int,
    mode: int = 0,
    *,
    no_symlinks: bool = False,
) -> int:
    """Open an allowed path with a kernel-enforced root boundary when possible.

    Linux openat2(RESOLVE_BENEATH|RESOLVE_NO_MAGICLINKS) makes path validation and
    path use one kernel operation, closing the classic resolve-then-open TOCTOU gap.
    A conservative fallback is retained for kernels/seccomp profiles without
    openat2; it performs the previous canonical validation plus a post-open fd
    target check. The fallback never weakens the configured allowed-root policy.
    """

    root, relative, lexical = _lexical_root_and_relative(settings, raw)
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, root_flags)
    canonical_override: Path | None = None
    try:
        try:
            return _openat2(root_fd, relative, flags | getattr(os, "O_CLOEXEC", 0), mode, no_symlinks=no_symlinks)
        except OSError as exc:
            if exc.errno in {errno.EXDEV, errno.ELOOP}:
                if no_symlinks:
                    raise ValueError("Path resolution escaped the allowed root or traversed a forbidden symlink") from exc
                # RESOLVE_BENEATH rejects absolute symlink targets by design. If
                # canonical resolution proves the target remains allowed, reopen
                # the canonical target with NO_SYMLINKS to avoid a TOCTOU redirect.
                canonical_override = settings.resolve_allowed_path(str(lexical))
                fd = _open_canonical_with_kernel(settings, canonical_override, flags, mode)
                if fd is not None:
                    return fd
            elif exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.EPERM}:
                raise
            # ENOSYS: old kernel. EINVAL: unsupported resolve flags/ABI. EPERM can
            # occur under a seccomp policy that blocks openat2. These cases fall
            # through to the conservative compatibility path below.
    finally:
        os.close(root_fd)

    if no_symlinks:
        safe = settings.resolve_allowed_entry(str(lexical))
        fallback_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    else:
        safe = canonical_override or settings.resolve_allowed_path(str(lexical))
        fallback_flags = flags | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(safe, fallback_flags, mode)
    _verify_fd_beneath(fd, settings)
    return fd


@contextmanager
def opened_beneath(
    settings: _PathSettings,
    raw: str,
    flags: int,
    mode: int = 0,
    *,
    no_symlinks: bool = False,
) -> Iterator[int]:
    fd = open_beneath(settings, raw, flags, mode, no_symlinks=no_symlinks)
    try:
        yield fd
    finally:
        os.close(fd)


def open_parent_beneath(settings: _PathSettings, raw: str) -> tuple[int, str, Path]:
    """Return a stable allowed parent dirfd, basename, and normalized display path."""

    _root, _relative, lexical = _lexical_root_and_relative(settings, raw)
    if lexical in settings.allowed_roots:
        raise ValueError("Configured allowed roots do not have a mutable parent entry")
    parent = lexical.parent
    fd = open_beneath(
        settings,
        str(parent),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        no_symlinks=False,
    )
    return fd, lexical.name, lexical


@contextmanager
def opened_parent_beneath(settings: _PathSettings, raw: str) -> Iterator[tuple[int, str, Path]]:
    fd, name, lexical = open_parent_beneath(settings, raw)
    try:
        yield fd, name, lexical
    finally:
        os.close(fd)


def openat2_supported(settings: _PathSettings) -> bool:
    """Best-effort runtime capability probe used for status/tests; no path is modified."""

    root = settings.allowed_roots[0]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, flags)
    try:
        try:
            fd = _openat2(root_fd, Path("."), flags, 0)
        except OSError as exc:
            if exc.errno in {errno.ENOSYS, errno.EINVAL, errno.EPERM}:
                return False
            raise
        else:
            os.close(fd)
            return True
    finally:
        os.close(root_fd)
