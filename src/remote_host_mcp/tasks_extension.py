from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import mcp.types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import require_client_extension
from pydantic import Field

from .config import Settings
from .jobs import cancel_job, job_status, start_job

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
_MODERN_PROTOCOL = frozenset({"2026-07-28"})


class _TaskIdParams(types.RequestParams):
    task_id: str = Field(alias="taskId", min_length=32, max_length=32)


class _TaskUpdateParams(_TaskIdParams):
    input_responses: dict[str, Any] = Field(default_factory=dict, alias="inputResponses")


def _iso(epoch: int | None) -> str:
    value = int(epoch or 0)
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _client_supports_tasks(ctx: ServerRequestContext[Any, Any]) -> bool:
    capabilities = ctx.session.client_capabilities
    declared = capabilities.extensions if capabilities else None
    return bool(declared and TASKS_EXTENSION_ID in declared)


def _terminal_job_result(status: Any) -> dict[str, Any]:
    return {
        "job_id": status.job_id,
        "status": status.status,
        "exit_code": status.exit_code,
        "timed_out": status.timed_out,
        "terminated_by": status.terminated_by,
        "duration_ms": status.duration_ms,
    }


def _call_tool_result(status: Any) -> dict[str, Any]:
    structured = _terminal_job_result(status)
    is_error = status.status != "completed" or status.exit_code not in {0, None}
    return {
        "resultType": "complete",
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
    }


def _task_payload(status: Any, settings: Settings, *, create: bool = False) -> dict[str, Any]:
    job_state = status.status
    if job_state in {"starting", "queued", "running", "cancel_requested"}:
        task_state = "working"
    elif job_state == "canceled":
        task_state = "cancelled"
    else:
        # MCP Tasks reserves `failed` for JSON-RPC execution failures. A shell
        # non-zero exit, timeout, interruption, or lost worker is a completed
        # tools/call whose CallToolResult has isError=true.
        task_state = "completed"

    updated = (
        status.completed_at
        or status.heartbeat_at
        or status.last_output_at
        or status.started_at
        or status.created_at
    )
    if status.completed_at is None:
        ttl_ms: int | None = None
    else:
        lifetime_ms = max(0, int(status.completed_at - status.created_at) * 1000)
        ttl_ms = lifetime_ms + settings.job_retention_seconds * 1000

    payload: dict[str, Any] = {
        "resultType": "task" if create else "complete",
        "taskId": status.job_id,
        "status": task_state,
        "statusMessage": f"Remote Host MCP durable job: {job_state}",
        "createdAt": _iso(status.created_at),
        "lastUpdatedAt": _iso(updated),
        "ttlMs": ttl_ms,
        "pollIntervalMs": max(500, settings.job_heartbeat_seconds * 1000),
    }
    if task_state == "completed":
        payload["result"] = _call_tool_result(status)
    return payload


class DurableJobsTasksExtension(Extension):
    """MCP 2026-07-28 Tasks adapter over the durable Job engine.

    MCP SDK v2.2.0 provides the generic extension mechanism but not a built-in
    Tasks implementation. Remote Host MCP therefore implements the task wire as a
    narrow adapter: `job_run` is task-augmented when the caller advertises the
    Tasks extension, while the existing `job_*` tools remain unchanged. Task IDs
    are the same cryptographically-random durable job IDs, so polling survives a
    Remote Host MCP restart without a second persistence layer.
    """

    identifier = TASKS_EXTENSION_ID

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def settings(self) -> dict[str, Any]:
        return {}

    def methods(self) -> list[MethodBinding]:
        return [
            MethodBinding("tasks/get", _TaskIdParams, self._get, protocol_versions=_MODERN_PROTOCOL),
            MethodBinding("tasks/update", _TaskUpdateParams, self._update, protocol_versions=_MODERN_PROTOCOL),
            MethodBinding("tasks/cancel", _TaskIdParams, self._cancel, protocol_versions=_MODERN_PROTOCOL),
        ]

    async def intercept_tool_call(
        self,
        params: types.CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if params.name != "job_run" or not _client_supports_tasks(ctx):
            return await call_next(ctx)

        args = dict(params.arguments or {})
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return await call_next(ctx)
        cwd = args.get("cwd")
        timeout_ms = args.get("timeout_ms")
        idempotency_key = args.get("idempotency_key")
        started = start_job(command, cwd, timeout_ms, idempotency_key, self._settings)
        status = job_status(started.job_id, self._settings)
        return _task_payload(status, self._settings, create=True)

    async def _get(self, ctx: ServerRequestContext[Any, Any], params: _TaskIdParams) -> HandlerResult:
        require_client_extension(ctx, TASKS_EXTENSION_ID)
        status = job_status(params.task_id, self._settings)
        return _task_payload(status, self._settings, create=False)

    async def _update(self, ctx: ServerRequestContext[Any, Any], params: _TaskUpdateParams) -> HandlerResult:
        require_client_extension(ctx, TASKS_EXTENSION_ID)
        # Shell jobs never enter input_required. Unknown/already-satisfied
        # inputResponse keys are ignored after confirming that the task exists.
        job_status(params.task_id, self._settings)
        return {"resultType": "complete"}

    async def _cancel(self, ctx: ServerRequestContext[Any, Any], params: _TaskIdParams) -> HandlerResult:
        require_client_extension(ctx, TASKS_EXTENSION_ID)
        await cancel_job(params.task_id, self._settings)
        return {"resultType": "complete"}
