from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import stat
from pathlib import Path

from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent

from .config import Settings
from .secure_paths import opened_beneath

DEFAULT_INLINE_BYTES = 4 * 1024 * 1024
MAX_INLINE_BYTES = 8 * 1024 * 1024
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _validate_limit(max_bytes: int) -> int:
    if max_bytes < 1 or max_bytes > MAX_INLINE_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_INLINE_BYTES}")
    return max_bytes


def _detect_mime(path: Path, data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(path.name, strict=False)
    return guessed or "application/octet-stream"


def _read_fd_bounded(fd: int, max_bytes: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Artifact path is not a regular file")
    if info.st_size > max_bytes:
        raise ValueError(
            f"Artifact is {info.st_size} bytes, above inline limit {max_bytes}; use download_info/download_chunk instead"
        )
    out = bytearray()
    while True:
        chunk = os.read(fd, min(65536, max_bytes + 1 - len(out)))
        if not chunk:
            break
        out.extend(chunk)
        if len(out) > max_bytes:
            raise ValueError(
                f"Artifact grew beyond inline limit {max_bytes}; use download_info/download_chunk instead"
            )
    return bytes(out)


def file_artifact(path: str, max_bytes: int, settings: Settings) -> list[TextContent | ImageContent | EmbeddedResource]:
    """Return one allowed file inline as MCP image content or an embedded binary resource."""
    limit = _validate_limit(max_bytes)
    display_path = Path(path).expanduser()
    with opened_beneath(settings, path, os.O_RDONLY) as fd:
        data = _read_fd_bounded(fd, limit)
    mime = _detect_mime(display_path, data)
    digest = hashlib.sha256(data).hexdigest()
    metadata = TextContent(
        type="text",
        text=f"artifact name={display_path.name} mime={mime} bytes={len(data)} sha256={digest}",
    )
    encoded = base64.b64encode(data).decode("ascii")
    if mime in _IMAGE_MIMES:
        return [metadata, ImageContent(type="image", data=encoded, mime_type=mime)]
    resource = BlobResourceContents(
        uri=f"rhmcp-artifact://sha256/{digest}",
        blob=encoded,
        mime_type=mime,
    )
    return [metadata, EmbeddedResource(type="resource", resource=resource)]
