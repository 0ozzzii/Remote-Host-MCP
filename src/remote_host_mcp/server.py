from __future__ import annotations

import getpass
import importlib.metadata
import logging
import os
import platform
import socket
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.extension import Extension
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .config import ConfigError, Settings
from .executor import run_shell
from .filesystem import (
    chmod_path as chmod_path_impl,
    copy_path as copy_path_impl,
    hash_file as hash_file_impl,
    list_directory as list_directory_impl,
    make_directory as make_directory_impl,
    move_path as move_path_impl,
    path_info as path_info_impl,
    read_file_chunk as read_file_chunk_impl,
    read_text_file as read_text_file_impl,
    remove_path as remove_path_impl,
    write_text_file as write_text_file_impl,
)
from .models import (
    DownloadInfoResult,
    ExecResult,
    FileWriteResult,
    HashFileResult,
    ListDirectoryResult,
    PathActionResult,
    PathInfoResult,
    ReadFileChunkResult,
    ReadTextFileResult,
    StatusResult,
    UploadBeginResult,
    UploadChunkResult,
    UploadFinishResult,
    UploadStatusResult,
)
from .transfer import (
    abort_upload as abort_upload_impl,
    begin_upload as begin_upload_impl,
    download_chunk as download_chunk_impl,
    download_info as download_info_impl,
    finish_upload as finish_upload_impl,
    upload_chunk as upload_chunk_impl,
    upload_status as upload_status_impl,
)

_STARTED = time.monotonic()


def build_server(
    settings: Settings,
    *,
    extensions: Sequence[Extension] | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> MCPServer:
    mcp = MCPServer(
        "DSW Direct Control",
        title="DSW Direct Control",
        description="Direct shell, filesystem, and file-transfer tools for one ModelScope DSW instance.",
        version=__version__,
        extensions=extensions,
        token_verifier=token_verifier,
        auth=auth,
        log_level="INFO",
    )

    @mcp.tool(
        title="Check DSW status",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    async def status() -> StatusResult:
        """Return local DSW/MCP process status without modifying the machine."""
        try:
            mcp_version = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            mcp_version = "unknown"
        return StatusResult(
            online=True,
            service="DSW Direct Control",
            version=__version__,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            user=getpass.getuser(),
            cwd=str(Path.cwd()),
            python_version=platform.python_version(),
            mcp_sdk_version=mcp_version,
            uptime_seconds=round(time.monotonic() - _STARTED, 3),
        )

    @mcp.tool(
        title="List DSW directory",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def list_directory(
        path: Annotated[str | None, Field(description="Absolute directory path inside DSW_MCP_ALLOWED_ROOTS. Omit for current directory.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum entries to return.")] = 100,
    ) -> ListDirectoryResult:
        """List one local directory. Access is limited to DSW_MCP_ALLOWED_ROOTS."""
        return list_directory_impl(path, limit, settings)

    @mcp.tool(
        title="Inspect DSW path",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def path_info(
        path: Annotated[str, Field(description="Absolute path inside DSW_MCP_ALLOWED_ROOTS.")],
    ) -> PathInfoResult:
        """Return lstat-style metadata for one allowed path."""
        return path_info_impl(path, settings)

    @mcp.tool(
        title="Read DSW text file",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def read_text_file(
        path: Annotated[str, Field(description="Absolute text-file path inside DSW_MCP_ALLOWED_ROOTS.")],
        start_line: Annotated[int, Field(ge=1, description="1-based first line to return.")] = 1,
        max_lines: Annotated[int, Field(ge=1, le=5000, description="Maximum number of lines to return.")] = 400,
        max_bytes: Annotated[int, Field(ge=256, le=131072, description="Maximum UTF-8 bytes to return.")] = 65536,
    ) -> ReadTextFileResult:
        """Read a bounded UTF-8 text preview. Access is limited to DSW_MCP_ALLOWED_ROOTS."""
        return read_text_file_impl(path, start_line, max_lines, max_bytes, settings)

    @mcp.tool(
        title="Read binary file chunk",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def read_file_chunk(
        path: Annotated[str, Field(description="Absolute regular-file path inside DSW_MCP_ALLOWED_ROOTS.")],
        offset: Annotated[int, Field(ge=0, description="Zero-based byte offset.")] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=524288, description="Maximum raw bytes before base64 encoding.")] = 262144,
    ) -> ReadFileChunkResult:
        """Read a bounded binary chunk as base64. Repeating the same offset is safe."""
        return read_file_chunk_impl(path, offset, max_bytes, settings)

    @mcp.tool(
        title="Hash DSW file",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def hash_file(
        path: Annotated[str, Field(description="Absolute regular-file path inside DSW_MCP_ALLOWED_ROOTS.")],
    ) -> HashFileResult:
        """Calculate SHA-256 for one regular file."""
        return hash_file_impl(path, settings)

    @mcp.tool(
        title="Write DSW text file atomically",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def write_text_file(
        path: Annotated[str, Field(description="Absolute destination path inside DSW_MCP_ALLOWED_ROOTS.")],
        text: Annotated[str, Field(description="UTF-8 text to write.")],
        overwrite: Annotated[bool, Field(description="Allow replacing an existing regular destination file.")] = False,
        expected_sha256: Annotated[str | None, Field(description="Optional current SHA-256 guard. If supplied, destination must still match before replacement.")] = None,
        mode: Annotated[int, Field(ge=0, le=511, description="POSIX permission bits as an integer, e.g. 420=0644, 493=0755.")] = 420,
    ) -> FileWriteResult:
        """Write UTF-8 text through a same-directory temporary file, fsync, and atomic rename."""
        return write_text_file_impl(path, text, overwrite, expected_sha256, mode, settings)

    @mcp.tool(
        title="Create DSW directory",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def make_directory(
        path: Annotated[str, Field(description="Absolute directory path inside DSW_MCP_ALLOWED_ROOTS.")],
        parents: Annotated[bool, Field(description="Create missing parents like mkdir -p.")] = False,
        exist_ok: Annotated[bool, Field(description="Treat an existing directory as success.")] = False,
        mode: Annotated[int, Field(ge=0, le=511, description="POSIX permission bits as integer.")] = 493,
    ) -> PathActionResult:
        """Create one allowed directory."""
        return make_directory_impl(path, parents, exist_ok, mode, settings)

    @mcp.tool(
        title="Move DSW path",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def move_path(
        source: Annotated[str, Field(description="Absolute source path inside allowed roots.")],
        destination: Annotated[str, Field(description="Absolute destination path inside allowed roots.")],
        overwrite: Annotated[bool, Field(description="Allow replacing a compatible destination.")] = False,
    ) -> PathActionResult:
        """Move or rename a file/directory without invoking a shell."""
        return move_path_impl(source, destination, overwrite, settings)

    @mcp.tool(
        title="Copy DSW path",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def copy_path(
        source: Annotated[str, Field(description="Absolute source path inside allowed roots.")],
        destination: Annotated[str, Field(description="Absolute destination path inside allowed roots.")],
        recursive: Annotated[bool, Field(description="Required for directory copies.")] = False,
        overwrite: Annotated[bool, Field(description="Allow replacing/merging an existing destination.")] = False,
    ) -> PathActionResult:
        """Copy a regular file or, with recursive=true, a directory tree."""
        return copy_path_impl(source, destination, recursive, overwrite, settings)

    @mcp.tool(
        title="Remove DSW path",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def remove_path(
        path: Annotated[str, Field(description="Absolute path inside allowed roots. Configured root directories themselves cannot be removed.")],
        recursive: Annotated[bool, Field(description="Required to remove a non-empty directory tree.")] = False,
    ) -> PathActionResult:
        """Remove one allowed path. Recursive directory deletion must be explicit."""
        return remove_path_impl(path, recursive, settings)

    @mcp.tool(
        title="Change DSW path permissions",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def chmod_path(
        path: Annotated[str, Field(description="Absolute path inside allowed roots.")],
        mode: Annotated[int, Field(ge=0, le=511, description="POSIX permission bits as integer. Special setuid/setgid bits are intentionally not accepted.")],
    ) -> PathActionResult:
        """Set ordinary POSIX permission bits without invoking chmod through a shell."""
        return chmod_path_impl(path, mode, settings)

    @mcp.tool(
        title="Begin resumable file upload",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def upload_begin(
        path: Annotated[str, Field(description="Absolute final destination inside allowed roots.")],
        total_size: Annotated[int, Field(ge=0, description="Exact raw byte length of the final file.")],
        sha256: Annotated[str, Field(min_length=64, max_length=64, description="Expected final SHA-256 hex digest.")],
        mode: Annotated[int, Field(ge=0, le=511, description="Final POSIX permission bits as integer.")] = 420,
        overwrite: Annotated[bool, Field(description="Permit replacing an existing destination only at final commit.")] = False,
    ) -> UploadBeginResult:
        """Create a temporary upload transaction. The final destination is unchanged until upload_finish succeeds."""
        return begin_upload_impl(path, total_size, sha256, mode, overwrite, settings)

    @mcp.tool(
        title="Append resumable upload chunk",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def upload_chunk(
        upload_id: Annotated[str, Field(min_length=32, max_length=32, description="Upload handle returned by upload_begin.")],
        offset: Annotated[int, Field(ge=0, description="Raw byte offset. Must equal next_offset unless this is an exact retry.")],
        data_base64: Annotated[str, Field(description="Base64-encoded raw chunk. Recommended raw chunk size is 256 KiB.")],
        chunk_sha256: Annotated[str | None, Field(description="Optional SHA-256 of the decoded chunk for per-chunk verification.")] = None,
    ) -> UploadChunkResult:
        """Append one bounded chunk. Exact replay of an already-written offset is accepted; conflicting replay is rejected."""
        return upload_chunk_impl(upload_id, offset, data_base64, chunk_sha256, settings)

    @mcp.tool(
        title="Inspect resumable upload",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def upload_status(
        upload_id: Annotated[str, Field(min_length=32, max_length=32, description="Upload handle.")],
    ) -> UploadStatusResult:
        """Return upload state and next byte offset."""
        return upload_status_impl(upload_id, settings)

    @mcp.tool(
        title="Commit resumable upload",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def upload_finish(
        upload_id: Annotated[str, Field(min_length=32, max_length=32, description="Upload handle.")],
    ) -> UploadFinishResult:
        """Verify exact size and SHA-256, fsync, then atomically replace the final path. Repeating after success is safe."""
        return finish_upload_impl(upload_id, settings)

    @mcp.tool(
        title="Abort resumable upload",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def upload_abort(
        upload_id: Annotated[str, Field(min_length=32, max_length=32, description="Upload handle.")],
    ) -> UploadStatusResult:
        """Delete the uncommitted staging file and mark the upload aborted. A committed final file is never deleted."""
        return abort_upload_impl(upload_id, settings)

    @mcp.tool(
        title="Inspect downloadable DSW file",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def download_info(
        path: Annotated[str, Field(description="Absolute regular-file path inside allowed roots.")],
        include_sha256: Annotated[bool, Field(description="Calculate SHA-256 now. Disable for very large files when only size/chunking is needed.")] = True,
    ) -> DownloadInfoResult:
        """Return size, mtime, optional SHA-256, and the server's maximum download chunk size."""
        return download_info_impl(path, include_sha256, settings)

    @mcp.tool(
        title="Download DSW file chunk",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def download_chunk(
        path: Annotated[str, Field(description="Absolute regular-file path inside allowed roots.")],
        offset: Annotated[int, Field(ge=0, description="Zero-based byte offset.")] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=524288, description="Maximum raw bytes before base64 encoding.")] = 262144,
    ) -> ReadFileChunkResult:
        """Return one retry-safe base64 chunk from a DSW file."""
        return download_chunk_impl(path, offset, max_bytes, settings)

    @mcp.tool(
        title="Execute shell command on DSW",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
        structured_output=True,
    )
    async def exec(
        command: Annotated[str, Field(min_length=1, description="Shell command to execute once. Arbitrary shell; may be destructive.")],
        timeout_ms: Annotated[int | None, Field(description="Execution deadline in milliseconds. Server range is 1000..DSW_MCP_MAX_TIMEOUT_MS.")] = None,
        cwd: Annotated[str | None, Field(description="Initial working directory inside DSW_MCP_ALLOWED_ROOTS. Omit for server cwd.")] = None,
    ) -> ExecResult:
        """Execute one arbitrary shell command locally on DSW.

        This is a high-privilege write-capable tool. The command may modify/delete data or access the network.
        The server never retries commands automatically. On timeout it terminates the entire spawned process group.
        stdout+stderr are bounded; command text and output are not written to server logs.
        """
        return await run_shell(command, timeout_ms, cwd, settings)

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "service": "DSW Direct Control",
                "version": __version__,
                "transport": "streamable-http",
            },
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    return mcp


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(settings.state_dir, 0o700)
    except OSError:
        pass

    server = build_server(settings)
    security = TransportSecuritySettings(
        allowed_hosts=[
            settings.public_host,
            f"{settings.public_host}:*",
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ],
        allowed_origins=[
            "https://chatgpt.com",
            "https://chat.openai.com",
        ],
    )

    print(f"DSW Direct MCP listening on {settings.bind_host}:{settings.port}", file=sys.stderr)
    print(f"Public MCP URL: https://{settings.public_host}/mcp/[REDACTED]", file=sys.stderr)
    server.run(
        transport="streamable-http",
        host=settings.bind_host,
        port=settings.port,
        streamable_http_path=settings.mcp_path,
        json_response=settings.json_response,
        stateless_http=settings.stateless_http,
        max_request_body_size=settings.max_request_body_bytes,
        max_sessions=64,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
