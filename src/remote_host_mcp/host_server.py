from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from .auth import build_oauth_components
from .config import ConfigError, Settings
from .jobs import (
    cancel_job as cancel_job_impl,
    cleanup_job as cleanup_job_impl,
    job_read as job_read_impl,
    job_status as job_status_impl,
    list_jobs as list_jobs_impl,
    recover_jobs,
    start_job as start_job_impl,
)
from .models import (
    JobCleanupResult,
    JobListResult,
    JobReadResult,
    JobRunResult,
    JobStartResult,
    JobStatusResult,
    ProcessInfoResult,
    ProcessListResult,
    ProcessSignalResult,
    ServiceActionResult,
    ServiceStatusResult,
    SystemInfoResult,
    TerminalActionResult,
    TerminalExecResult,
    TerminalListResult,
    TerminalOpenResult,
    TerminalReadResult,
    TerminalStatusResult,
    TerminalWriteResult,
)
from .server import build_server as build_base_server
from .system_helpers import (
    process_info as process_info_impl,
    process_list as process_list_impl,
    process_signal as process_signal_impl,
    service_action as service_action_impl,
    service_status as service_status_impl,
    system_info as system_info_impl,
)
from .tasks_extension import DurableJobsTasksExtension
from .terminal import TerminalManager
from .terminal_models import TerminalScreenResult

logger = logging.getLogger("remote_host_mcp.host")


def _terminal_status(manager: TerminalManager, terminal_id: str) -> TerminalStatusResult:
    # Keep the completed PendingCommand observable until the next terminal_exec so a
    # caller can recover an exit code after an earlier still_running response.
    session = manager._session(terminal_id)  # package-private by design
    pending = session.pending
    return TerminalStatusResult(
        terminal=session.info(),
        foreground_command_id=(pending.command_id if pending is not None else None),
        foreground_completed=(pending.completed if pending is not None else None),
        foreground_exit_code=(pending.exit_code if pending is not None and pending.completed else None),
        foreground_completion_source=(pending.completion_source if pending is not None and pending.completed else None),
    )


def build_server(settings: Settings) -> MCPServer:
    try:
        recovery = recover_jobs(settings)
        if any(recovery.values()):
            logger.info("job recovery: %s", recovery)
    except Exception as exc:
        # Recovery is observation-only and never replays arbitrary shell.
        logger.warning("job recovery failed: %s", type(exc).__name__)

    extensions = [DurableJobsTasksExtension(settings)] if settings.tasks_extension_enabled else []
    token_verifier, auth = build_oauth_components(settings)
    mcp = build_base_server(
        settings,
        extensions=extensions,
        token_verifier=token_verifier,
        auth=auth,
    )
    terminal_manager = TerminalManager(settings)

    # ------------------------- Durable jobs -------------------------
    @mcp.tool(
        title="Run durable background work",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
        structured_output=True,
    )
    async def job_run(
        command: Annotated[str, Field(min_length=1, description="Arbitrary shell command to execute once in a durable detached worker.")],
        cwd: Annotated[str | None, Field(description="Initial directory inside allowed roots.")] = None,
        timeout_ms: Annotated[int | None, Field(description="Optional job deadline. Long jobs may run for hours/days within server limits.")] = None,
        idempotency_key: Annotated[str | None, Field(description="Optional retry key. Same key+request reuses the original durable job.")] = None,
    ) -> JobRunResult:
        """Start durable work with an MCP Tasks-compatible surface.

        Ordinary clients receive a durable job handle immediately. A 2026-07-28
        client that advertises io.modelcontextprotocol/tasks may instead receive a
        protocol-level task result and poll tasks/get. The underlying execution is
        the same durable Job engine in both cases.
        """
        started = start_job_impl(command, cwd, timeout_ms, idempotency_key, settings)
        return JobRunResult(
            job_id=started.job_id,
            status=started.status,
            idempotent_reuse=started.idempotent_reuse,
        )

    @mcp.tool(
        title="Start durable background job",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
        structured_output=True,
    )
    async def job_start(
        command: Annotated[str, Field(min_length=1, description="Arbitrary shell command to execute once in a durable detached worker.")],
        cwd: Annotated[str | None, Field(description="Initial directory inside allowed roots.")] = None,
        timeout_ms: Annotated[int | None, Field(description="Optional job deadline. Long jobs may run for hours/days within server limits.")] = None,
        idempotency_key: Annotated[str | None, Field(description="Optional 1..256-byte retry key. Same key+request returns the original job_id and never replays shell.")] = None,
    ) -> JobStartResult:
        """Start a detached job and return quickly with job_id.

        Use this compatibility tool when explicit durable-job control is desired.
        New MCP Tasks-aware clients may prefer job_run.
        """
        return start_job_impl(command, cwd, timeout_ms, idempotency_key, settings)

    @mcp.tool(
        title="Inspect durable job",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def job_status(
        job_id: Annotated[str, Field(min_length=32, max_length=32, description="Job handle returned by job_start/job_run.")],
    ) -> JobStatusResult:
        """Return durable job state, PID identity, heartbeat, exit status, and retained log sizes."""
        return job_status_impl(job_id, settings)

    @mcp.tool(
        title="Read incremental job logs",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def job_read(
        job_id: Annotated[str, Field(min_length=32, max_length=32, description="Job handle.")],
        stdout_offset: Annotated[int, Field(ge=0, description="Previously returned next_stdout_offset.")] = 0,
        stderr_offset: Annotated[int, Field(ge=0, description="Previously returned next_stderr_offset.")] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=1048576, description="Maximum bytes to read from each stream.")] = 65536,
    ) -> JobReadResult:
        """Read only new retained stdout/stderr bytes using independent cursors."""
        return job_read_impl(job_id, stdout_offset, stderr_offset, max_bytes, settings)

    @mcp.tool(
        title="Cancel durable job",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def job_cancel(
        job_id: Annotated[str, Field(min_length=32, max_length=32, description="Job handle.")],
    ) -> JobStatusResult:
        """Cancel exactly one job process group using PID+starttime identity: TERM, grace, then KILL."""
        return await cancel_job_impl(job_id, settings)

    @mcp.tool(
        title="List durable jobs",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def job_list(
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum newest jobs to return.")] = 50,
    ) -> JobListResult:
        """List persisted jobs without replaying or modifying active commands."""
        return list_jobs_impl(limit, settings)

    @mcp.tool(
        title="Clean finished durable job state",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def job_cleanup(
        job_id: Annotated[str, Field(min_length=32, max_length=32, description="Finished job handle.")],
    ) -> JobCleanupResult:
        """Remove retained state/logs for one terminal job. Active jobs are refused."""
        return cleanup_job_impl(job_id, settings)

    # ------------------------- Persistent real PTY -------------------------
    @mcp.tool(
        title="Open persistent PTY terminal",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_open(
        cwd: Annotated[str | None, Field(description="Initial directory inside allowed roots.")] = None,
        cols: Annotated[int, Field(ge=20, le=500, description="Terminal columns.")] = 120,
        rows: Annotated[int, Field(ge=5, le=300, description="Terminal rows.")] = 40,
    ) -> TerminalOpenResult:
        """Open a real xterm-256color PTY whose cwd/env persist across later calls."""
        return await terminal_manager.open(cwd, cols, rows)

    @mcp.tool(
        title="Run command in persistent PTY",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
        structured_output=True,
    )
    async def terminal_exec(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
        command: Annotated[str, Field(min_length=1, description="Arbitrary shell command in this existing PTY.")],
        wait_ms: Annotated[int, Field(ge=0, le=30000, description="Wait this long for completion; timeout does not kill the foreground command.")] = 5000,
    ) -> TerminalExecResult:
        """Execute one command in the persistent shell. If still_running=true, use terminal_read/status/signal."""
        return await terminal_manager.exec(terminal_id, command, wait_ms)

    @mcp.tool(
        title="Write keys/text to persistent PTY",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
        structured_output=True,
    )
    async def terminal_write(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
        data: Annotated[str, Field(description="UTF-8 bytes to type into the terminal.")],
        append_enter: Annotated[bool, Field(description="Append newline/Enter after data.")] = False,
    ) -> TerminalWriteResult:
        """Send raw text/keys to a PTY. This can execute commands and is intentionally non-idempotent."""
        return await terminal_manager.write(terminal_id, data, append_enter)

    @mcp.tool(
        title="Read incremental PTY output",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_read(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
        cursor: Annotated[int | None, Field(description="Previous next_cursor. Omit to start at retained buffer start.")] = None,
        max_bytes: Annotated[int, Field(ge=1, le=1048576, description="Maximum raw PTY bytes to return.")] = 65536,
        wait_ms: Annotated[int, Field(ge=0, le=30000, description="Short wait for new output; returns sooner when bytes arrive.")] = 0,
    ) -> TerminalReadResult:
        """Read bounded incremental raw PTY output from the session ring buffer."""
        return await terminal_manager.read(terminal_id, cursor, max_bytes, wait_ms)

    @mcp.tool(
        title="Inspect persistent PTY",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_status(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
    ) -> TerminalStatusResult:
        """Return PTY state plus the foreground command completion/exit code and completion source when available."""
        return _terminal_status(terminal_manager, terminal_id)

    @mcp.tool(
        title="Render PTY screen snapshot",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_screen(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
    ) -> TerminalScreenResult:
        """Return the pyte-rendered current terminal screen for ANSI/TUI workflows."""
        return await terminal_manager.screen_snapshot(terminal_id)

    @mcp.tool(
        title="Resize persistent PTY",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_resize(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
        cols: Annotated[int, Field(ge=20, le=500, description="New columns.")],
        rows: Annotated[int, Field(ge=5, le=300, description="New rows.")],
    ) -> TerminalActionResult:
        """Resize the real PTY and emit SIGWINCH semantics through the terminal driver."""
        return await terminal_manager.resize(terminal_id, cols, rows)

    @mcp.tool(
        title="Signal persistent PTY foreground process",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_signal(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
        signal_name: Annotated[Literal["INT", "QUIT", "TSTP", "TERM", "HUP", "CONT", "SIGINT", "SIGQUIT", "SIGTSTP", "SIGTERM", "SIGHUP", "SIGCONT"], Field(description="Validated terminal signal.")],
    ) -> TerminalActionResult:
        """Deliver a terminal control signal such as Ctrl+C/SIGINT to the foreground group."""
        return await terminal_manager.signal(terminal_id, signal_name)

    @mcp.tool(
        title="List persistent PTYs",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_list() -> TerminalListResult:
        """List PTY sessions held by this Remote Host MCP server process."""
        return await terminal_manager.list()

    @mcp.tool(
        title="Close persistent PTY",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def terminal_close(
        terminal_id: Annotated[str, Field(min_length=32, max_length=32, description="PTY handle.")],
    ) -> TerminalActionResult:
        """Close one PTY session and its shell; use job_start/job_run for calculations that must outlive a terminal."""
        return await terminal_manager.close(terminal_id)

    # ------------------------- Process / service / system -------------------------
    @mcp.tool(
        title="List Linux processes",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def process_list(
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum process rows.")] = 100,
    ) -> ProcessListResult:
        """List bounded /proc process metadata without reading process environments."""
        return process_list_impl(limit)

    @mcp.tool(
        title="Inspect Linux process",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def process_info(
        pid: Annotated[int, Field(gt=0, description="Exact Linux PID.")],
    ) -> ProcessInfoResult:
        """Return exact PID identity including /proc start ticks for PID-reuse protection."""
        return process_info_impl(pid)

    @mcp.tool(
        title="Signal exact Linux process",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def process_signal(
        pid: Annotated[int, Field(gt=0, description="Exact Linux PID.")],
        expected_start_ticks: Annotated[int, Field(ge=0, description="start_ticks previously returned by process_info.")],
        signal_name: Annotated[Literal["INT", "HUP", "TERM", "KILL", "STOP", "CONT", "SIGINT", "SIGHUP", "SIGTERM", "SIGKILL", "SIGSTOP", "SIGCONT"], Field(description="Validated process signal.")],
    ) -> ProcessSignalResult:
        """Signal one exact process using PID+starttime and pidfd when the kernel permits it."""
        return process_signal_impl(pid, expected_start_ticks, signal_name)

    @mcp.tool(
        title="Inspect systemd service",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def service_status(
        service: Annotated[str, Field(min_length=1, max_length=200, description="Exact systemd unit/service name.")],
    ) -> ServiceStatusResult:
        """Read bounded systemd state without invoking a shell. Returns available=false when systemctl is absent."""
        return service_status_impl(service)

    @mcp.tool(
        title="Start stop or restart systemd service",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def service_action(
        service: Annotated[str, Field(min_length=1, max_length=200, description="Exact systemd unit/service name.")],
        action: Annotated[Literal["start", "stop", "restart"], Field(description="Validated systemd action.")],
    ) -> ServiceActionResult:
        """Run one validated systemctl action without shell expansion or fuzzy process matching."""
        return service_action_impl(service, action)

    @mcp.tool(
        title="Inspect Linux system resources",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def system_info() -> SystemInfoResult:
        """Return bounded kernel/CPU/load/memory/disk information without exposing environment secrets."""
        return system_info_impl(settings)

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

    print(f"Remote Host MCP compatibility server listening on {settings.bind_host}:{settings.port}", file=sys.stderr)
    if settings.auth_mode == "oauth":
        print(f"Public MCP URL: {settings.public_url} (OAuth bearer required)", file=sys.stderr)
    else:
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
