from __future__ import annotations

from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .agent_ops import (
    ArtifactBundleResult,
    ArtifactInfoResult,
    ArtifactPreviewResult,
    FileDiffResult,
    HostCapabilitiesResult,
    InspectPathsResult,
    LeaseAcquireResult,
    LeaseListResult,
    LeaseReleaseResult,
    LeaseStatusResult,
    LineEdit,
    SnapshotDeleteResult,
    SnapshotInfo,
    SnapshotListResult,
    SnapshotRestoreResult,
    WaitConditionResult,
    apply_line_patch as apply_line_patch_impl,
    artifact_bundle as artifact_bundle_impl,
    artifact_info as artifact_info_impl,
    artifact_preview as artifact_preview_impl,
    file_diff as file_diff_impl,
    host_capabilities as host_capabilities_impl,
    inspect_paths as inspect_paths_impl,
    lease_acquire as lease_acquire_impl,
    lease_list as lease_list_impl,
    lease_release as lease_release_impl,
    lease_status as lease_status_impl,
    run_argv as run_argv_impl,
    snapshot_create as snapshot_create_impl,
    snapshot_delete as snapshot_delete_impl,
    snapshot_list as snapshot_list_impl,
    snapshot_restore as snapshot_restore_impl,
    wait_condition as wait_condition_impl,
)
from .config import Settings
from .models import ExecResult, FileWriteResult


def register_agent_ops(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(
        title="Inspect host capabilities for Agent planning",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def host_capabilities() -> HostCapabilitiesResult:
        """Return a secret-free capability matrix so Agents can choose valid execution paths before trying them."""
        return host_capabilities_impl(settings)

    @mcp.tool(
        title="Wait for bounded host condition",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def wait_condition(
        condition: Annotated[Literal["file_exists", "file_not_exists", "file_size_at_least", "process_exit", "job_terminal", "port_open", "log_contains"], Field(description="Bounded condition to poll without busy-looping the Agent.")],
        path: Annotated[str | None, Field(description="Allowed path used by file/log conditions.")] = None,
        pid: Annotated[int | None, Field(gt=0, description="Exact Linux PID for process_exit.")] = None,
        expected_start_ticks: Annotated[int | None, Field(ge=0, description="PID start_ticks previously returned by process_info; required for process_exit.")] = None,
        size_bytes: Annotated[int | None, Field(ge=0, description="Required minimum size for file_size_at_least.")] = None,
        job_id: Annotated[str | None, Field(description="Durable job handle for job_terminal.")] = None,
        host: Annotated[Literal["127.0.0.1", "localhost", "::1"] | None, Field(description="Loopback host for port_open; remote scanning is intentionally unsupported.")] = None,
        port: Annotated[int | None, Field(ge=1, le=65535, description="Loopback TCP port for port_open.")] = None,
        pattern: Annotated[str | None, Field(description="Literal UTF-8 substring for log_contains; regex is intentionally unsupported.")] = None,
        timeout_ms: Annotated[int, Field(ge=0, le=90000, description="Maximum wait time within the synchronous request-safe limit.")] = 30000,
        poll_ms: Annotated[int, Field(ge=100, le=5000, description="Polling interval.")] = 500,
        log_tail_bytes: Annotated[int, Field(ge=256, le=1048576, description="Maximum retained log tail scanned per poll.")] = 65536,
    ) -> WaitConditionResult:
        """Wait for one safe local condition and return once satisfied or timed out."""
        return await wait_condition_impl(condition, path=path, pid=pid, expected_start_ticks=expected_start_ticks, size_bytes=size_bytes, job_id=job_id, host=host, port=port, pattern=pattern, timeout_ms=timeout_ms, poll_ms=poll_ms, log_tail_bytes=log_tail_bytes, settings=settings)

    @mcp.tool(
        title="Inspect artifact metadata",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def artifact_info(
        path: Annotated[str, Field(description="Regular file inside RHMCP_ALLOWED_ROOTS.")],
        include_sha256: Annotated[bool, Field(description="Calculate SHA-256 now for integrity/provenance decisions.")] = True,
    ) -> ArtifactInfoResult:
        """Return MIME/size/hash and whether file_artifact can inline this allowed file."""
        return artifact_info_impl(path, include_sha256, settings)

    @mcp.tool(
        title="Preview small text artifact",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def artifact_preview(
        path: Annotated[str, Field(description="Regular file inside RHMCP_ALLOWED_ROOTS.")],
        max_bytes: Annotated[int, Field(ge=256, le=131072, description="Maximum bytes used for a text-like preview.")] = 65536,
    ) -> ArtifactPreviewResult:
        """Preview text/JSON/CSV/Markdown while returning metadata-only for binary files."""
        return artifact_preview_impl(path, max_bytes, settings)

    @mcp.tool(
        title="Bundle artifacts into no-clobber ZIP",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def artifact_bundle(
        paths: Annotated[list[str], Field(min_length=1, max_length=20, description="Allowed files/directories to include. Symlinks and special files are rejected.")],
        destination: Annotated[str, Field(description="New .zip destination inside allowed roots; existing destinations are never overwritten.")],
        max_entries: Annotated[int, Field(ge=1, le=5000, description="Maximum regular files in the bundle.")] = 500,
        max_total_bytes: Annotated[int, Field(ge=1, le=109951162777, description="Maximum total uncompressed source bytes, additionally capped by server transfer settings.")] = 536870912,
    ) -> ArtifactBundleResult:
        """Create a bounded ZIP so an Agent can return a coherent result set with download/file_artifact."""
        return artifact_bundle_impl(paths, destination, max_entries, max_total_bytes, settings)

    @mcp.tool(
        title="Execute argv without a shell",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
        structured_output=True,
    )
    async def exec_argv(
        argv: Annotated[list[str], Field(min_length=1, max_length=64, description="Executable plus arguments. No shell parsing, expansion, pipes, redirection, or command substitution occurs.")],
        cwd: Annotated[str | None, Field(description="Working directory inside allowed roots.")] = None,
        timeout_ms: Annotated[int | None, Field(description="Optional synchronous deadline; longer work should use durable jobs.")] = None,
        stdin_text: Annotated[str | None, Field(description="Optional bounded UTF-8 stdin, kept out of process argv.")] = None,
        env_allowlist: Annotated[list[str], Field(max_length=32, description="Non-secret host environment variable names explicitly inherited into the minimal child environment.")] = [],
    ) -> ExecResult:
        """Run a structured argv vector directly with bounded output and exact process-group cleanup."""
        return await run_argv_impl(argv, cwd=cwd, timeout_ms=timeout_ms, stdin_text=stdin_text, env_allowlist=env_allowlist, settings=settings)

    @mcp.tool(
        title="Create scoped rollback snapshot",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def snapshot_create(
        paths: Annotated[list[str], Field(min_length=1, max_length=20, description="Top-level allowed files/directories to snapshot. Symlinks and special files are rejected.")],
        label: Annotated[str | None, Field(description="Optional human-readable rollback label.")] = None,
        max_total_bytes: Annotated[int, Field(ge=1, le=109951162777, description="Maximum snapshot bytes, also capped by server transfer settings.")] = 536870912,
        max_nodes: Annotated[int, Field(ge=1, le=20000, description="Maximum files/directories copied into snapshot state.")] = 5000,
    ) -> SnapshotInfo:
        """Create a persistent rollback point under the MCP state directory before risky file changes."""
        return snapshot_create_impl(paths, label, max_total_bytes, max_nodes, settings)

    @mcp.tool(
        title="List rollback snapshots",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def snapshot_list() -> SnapshotListResult:
        """List persistent scoped snapshots without reading arbitrary snapshot payload bytes."""
        return snapshot_list_impl(settings)

    @mcp.tool(
        title="Restore scoped rollback snapshot",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def snapshot_restore(
        snapshot_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$", description="Snapshot handle returned by snapshot_create.")],
    ) -> SnapshotRestoreResult:
        """Restore each captured top-level path with same-parent atomic publication."""
        return snapshot_restore_impl(snapshot_id, settings)

    @mcp.tool(
        title="Delete rollback snapshot",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def snapshot_delete(
        snapshot_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$", description="Snapshot handle to discard after verification.")],
    ) -> SnapshotDeleteResult:
        """Delete one internal rollback snapshot; this never deletes the live source paths."""
        return snapshot_delete_impl(snapshot_id, settings)

    @mcp.tool(
        title="Acquire coordination lease",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def lease_acquire(
        resource: Annotated[str, Field(min_length=1, max_length=256, description="Logical resource name such as project:model-a or service:rhmcp.")],
        holder: Annotated[str | None, Field(description="Optional Agent/session label; do not put secrets here.")] = None,
        ttl_seconds: Annotated[int, Field(ge=5, le=86400, description="Automatic lease expiry.")] = 900,
    ) -> LeaseAcquireResult:
        """Acquire one persistent exclusive lease so multiple Agents do not modify the same resource concurrently."""
        return lease_acquire_impl(resource, holder, ttl_seconds, settings)

    @mcp.tool(
        title="Inspect coordination lease",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def lease_status(
        resource: Annotated[str, Field(min_length=1, max_length=256, description="Logical resource name.")],
    ) -> LeaseStatusResult:
        """Return whether one logical resource currently has an unexpired lease."""
        return lease_status_impl(resource, settings)

    @mcp.tool(
        title="List active coordination leases",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def lease_list() -> LeaseListResult:
        """List active nonexpired leases without exposing lease tokens."""
        return lease_list_impl(settings)

    @mcp.tool(
        title="Release coordination lease",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def lease_release(
        lease_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$", description="Lease token returned only to the successful acquirer.")],
    ) -> LeaseReleaseResult:
        """Release exactly one lease token; repeated release is safe."""
        return lease_release_impl(lease_id, settings)

    @mcp.tool(
        title="Inspect several allowed paths in one bounded call",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def inspect_paths(
        paths: Annotated[list[str], Field(min_length=1, max_length=20, description="Up to 20 explicit allowed paths; this tool never recursively scans the host.")],
        include_sha256: Annotated[bool, Field(description="Hash regular files only when they fit hash_max_bytes.")] = False,
        max_text_bytes: Annotated[int, Field(ge=0, le=16384, description="Per-file UTF-8 preview budget; set 0 to disable previews.")] = 4096,
        hash_max_bytes: Annotated[int, Field(ge=0, le=1073741824, description="Skip hashes for files larger than this bound.")] = 67108864,
    ) -> InspectPathsResult:
        """Batch stat/hash/small-preview work with strict count and byte bounds."""
        return inspect_paths_impl(paths, include_sha256=include_sha256, max_text_bytes=max_text_bytes, hash_max_bytes=hash_max_bytes, settings=settings)

    @mcp.tool(
        title="Diff current text file against proposed text",
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def file_diff(
        path: Annotated[str, Field(description="UTF-8 text file inside allowed roots.")],
        proposed_text: Annotated[str, Field(description="Complete proposed UTF-8 text used only for diffing; the live file is not modified.")],
        max_file_bytes: Annotated[int, Field(ge=1, le=4194304, description="Maximum current/proposed file size accepted.")] = 1048576,
        max_diff_bytes: Annotated[int, Field(ge=1024, le=262144, description="Maximum UTF-8 bytes returned in unified diff.")] = 65536,
    ) -> FileDiffResult:
        """Return current/proposed hashes plus bounded unified diff for review before mutation."""
        return file_diff_impl(path, proposed_text, max_file_bytes=max_file_bytes, max_diff_bytes=max_diff_bytes, settings=settings)

    @mcp.tool(
        title="Apply structured line patch with SHA guard",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False),
        structured_output=True,
    )
    async def apply_patch(
        path: Annotated[str, Field(description="UTF-8 text file inside allowed roots.")],
        expected_sha256: Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[A-Fa-f0-9]{64}$", description="Current file SHA-256 observed before editing; mismatch fails closed.")],
        edits: Annotated[list[LineEdit], Field(min_length=1, max_length=100, description="Non-overlapping 1-based line edits. Include exact replacement newlines when desired.")],
        mode: Annotated[int, Field(ge=0, le=511, description="Final ordinary POSIX permission bits.")] = 420,
        max_file_bytes: Annotated[int, Field(ge=1, le=4194304, description="Maximum input/output text size accepted.")] = 1048576,
    ) -> FileWriteResult:
        """Apply structured line edits only if the current file still matches expected_sha256."""
        return apply_line_patch_impl(path, expected_sha256, edits, mode=mode, max_file_bytes=max_file_bytes, settings=settings)
