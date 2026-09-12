from __future__ import annotations

from pydantic import BaseModel, Field


class ToolErrorInfo(BaseModel):
    code: str
    message: str


class ExecResult(BaseModel):
    success: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = Field(ge=0)
    timed_out: bool = False
    terminated_by: str | None = None
    truncated: bool = False
    output_bytes_returned: int = Field(default=0, ge=0)
    output_bytes_total: int = Field(default=0, ge=0)
    cwd: str
    error: ToolErrorInfo | None = None


class StatusResult(BaseModel):
    online: bool
    service: str
    version: str
    build_commit: str | None = None
    build_ref: str | None = None
    hostname: str
    pid: int
    user: str
    cwd: str
    python_version: str
    mcp_sdk_version: str
    uptime_seconds: float = Field(ge=0)


class DirectoryEntry(BaseModel):
    name: str
    type: str
    size: int | None = Field(default=None, ge=0)
    modified_ns: int | None = Field(default=None, ge=0)


class ListDirectoryResult(BaseModel):
    path: str
    entries: list[DirectoryEntry]
    truncated: bool


class ReadTextFileResult(BaseModel):
    path: str
    start_line: int = Field(ge=1)
    lines_returned: int = Field(ge=0)
    text: str
    truncated: bool


class PathInfoResult(BaseModel):
    path: str
    type: str
    size: int | None = Field(default=None, ge=0)
    mode: str
    uid: int = Field(ge=0)
    gid: int = Field(ge=0)
    modified_ns: int = Field(ge=0)
    symlink_target: str | None = None


class ReadFileChunkResult(BaseModel):
    path: str
    offset: int = Field(ge=0)
    bytes_returned: int = Field(ge=0)
    data_base64: str
    next_offset: int = Field(ge=0)
    eof: bool


class FileWriteResult(BaseModel):
    success: bool
    path: str
    bytes_written: int = Field(ge=0)
    sha256: str
    mode: str
    atomic: bool = True
    replaced: bool = False


class PathActionResult(BaseModel):
    success: bool
    action: str
    path: str
    destination: str | None = None


class HashFileResult(BaseModel):
    path: str
    algorithm: str
    digest: str
    bytes_hashed: int = Field(ge=0)


class UploadBeginResult(BaseModel):
    upload_id: str
    path: str
    total_size: int = Field(ge=0)
    expected_sha256: str
    next_offset: int = Field(ge=0)
    max_chunk_bytes: int = Field(gt=0)
    expires_at: int = Field(gt=0)


class UploadChunkResult(BaseModel):
    upload_id: str
    accepted: bool
    duplicate: bool = False
    bytes_received: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    complete: bool


class UploadStatusResult(BaseModel):
    upload_id: str
    path: str
    status: str
    total_size: int = Field(ge=0)
    bytes_received: int = Field(ge=0)
    expected_sha256: str
    final_sha256: str | None = None
    next_offset: int = Field(ge=0)
    expires_at: int = Field(gt=0)


class UploadFinishResult(BaseModel):
    success: bool
    upload_id: str
    path: str
    bytes_written: int = Field(ge=0)
    sha256: str
    mode: str
    replaced: bool = False
    already_committed: bool = False


class DownloadInfoResult(BaseModel):
    path: str
    size: int = Field(ge=0)
    modified_ns: int = Field(ge=0)
    sha256: str | None = None
    max_chunk_bytes: int = Field(gt=0)


class JobStartResult(BaseModel):
    job_id: str
    status: str
    cwd: str
    command_sha256: str
    command_bytes: int = Field(ge=1)
    created_at: int = Field(gt=0)
    idempotent_reuse: bool = False


class JobRunResult(BaseModel):
    """Compatibility result for job_run when MCP Tasks is not negotiated."""

    job_id: str
    status: str
    idempotent_reuse: bool = False


class JobStatusResult(BaseModel):
    job_id: str
    status: str
    cwd: str
    created_at: int = Field(gt=0)
    started_at: int | None = None
    completed_at: int | None = None
    heartbeat_at: int | None = None
    last_output_at: int | None = None
    worker_pid: int | None = Field(default=None, gt=0)
    worker_start_ticks: int | None = Field(default=None, ge=0)
    child_pid: int | None = Field(default=None, gt=0)
    child_start_ticks: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    timed_out: bool = False
    cancel_requested: bool = False
    terminated_by: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    command_sha256: str
    command_bytes: int = Field(ge=1)
    error: str | None = None


class JobReadResult(BaseModel):
    job_id: str
    status: str
    stdout: str = ""
    stderr: str = ""
    stdout_offset: int = Field(ge=0)
    stderr_offset: int = Field(ge=0)
    next_stdout_offset: int = Field(ge=0)
    next_stderr_offset: int = Field(ge=0)
    stdout_at_end: bool
    stderr_at_end: bool
    job_done: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class JobListResult(BaseModel):
    jobs: list[JobStatusResult]
    truncated: bool = False


class JobCleanupResult(BaseModel):
    success: bool
    job_id: str
    removed: bool


class TerminalOpenResult(BaseModel):
    terminal_id: str
    pid: int = Field(gt=0)
    shell: str
    cwd: str
    cols: int = Field(ge=20)
    rows: int = Field(ge=5)
    created_at: int = Field(gt=0)
    cursor: int = Field(ge=0)


class TerminalReadResult(BaseModel):
    terminal_id: str
    cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    text: str
    bytes_returned: int = Field(ge=0)
    truncated_before_cursor: bool = False
    at_buffer_end: bool
    alive: bool
    exit_code: int | None = None


class TerminalWriteResult(BaseModel):
    terminal_id: str
    bytes_written: int = Field(ge=0)
    alive: bool


class TerminalExecResult(BaseModel):
    terminal_id: str
    command_id: str
    completed: bool
    still_running: bool
    exit_code: int | None = None
    completion_source: str | None = None
    output: str = ""
    cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    truncated_before_cursor: bool = False


class TerminalInfo(BaseModel):
    terminal_id: str
    pid: int = Field(gt=0)
    shell: str
    cwd: str
    cols: int = Field(ge=20)
    rows: int = Field(ge=5)
    created_at: int = Field(gt=0)
    last_activity_at: int = Field(gt=0)
    alive: bool
    busy: bool
    exit_code: int | None = None
    buffer_start: int = Field(ge=0)
    buffer_end: int = Field(ge=0)


class TerminalStatusResult(BaseModel):
    terminal: TerminalInfo
    foreground_command_id: str | None = None
    foreground_completed: bool | None = None
    foreground_exit_code: int | None = None
    foreground_completion_source: str | None = None


class TerminalListResult(BaseModel):
    terminals: list[TerminalInfo]


class TerminalActionResult(BaseModel):
    success: bool
    terminal_id: str
    action: str
    alive: bool


class ProcessInfoResult(BaseModel):
    pid: int = Field(gt=0)
    start_ticks: int = Field(ge=0)
    ppid: int = Field(ge=0)
    pgid: int = Field(ge=0)
    session_id: int = Field(ge=0)
    state: str
    uid: int | None = Field(default=None, ge=0)
    gid: int | None = Field(default=None, ge=0)
    cmdline: str
    cwd: str | None = None
    executable: str | None = None
    alive: bool = True


class ProcessListResult(BaseModel):
    processes: list[ProcessInfoResult]
    truncated: bool = False


class ProcessSignalResult(BaseModel):
    success: bool
    pid: int = Field(gt=0)
    start_ticks: int = Field(ge=0)
    signal: str
    alive_after: bool


class ServiceStatusResult(BaseModel):
    service: str
    available: bool
    systemctl_present: bool = False
    pid1_is_systemd: bool | None = None
    manager_reachable: bool = False
    operational: bool = False
    reason: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    return_code: int | None = None
    output: str = ""


class ServiceActionResult(BaseModel):
    success: bool
    service: str
    action: str
    return_code: int | None = None
    output: str = ""
    status: ServiceStatusResult


class SystemInfoResult(BaseModel):
    hostname: str
    kernel: str
    architecture: str
    cpu_count: int | None = Field(default=None, ge=1)
    load_average: list[float]
    uptime_seconds: float | None = Field(default=None, ge=0)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    disk_path: str
    disk_total_bytes: int = Field(ge=0)
    disk_used_bytes: int = Field(ge=0)
    disk_free_bytes: int = Field(ge=0)
    state_dir: str
    allowed_roots: list[str]
