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
