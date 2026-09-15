"""Structured per-call audit log (JSONL).

One line per MCP tool invocation, written at the tool dispatch layer so the
record carries the real ``toolName`` and its arguments — the transport layer
sees only opaque JSON-RPC frames and cannot supply either.

The log is the *only* artifact this module produces. It has no knowledge of
CF-MCP-HUB: a separate process (``remote_host_mcp.report_agent``) tails the file
and ships it. Reporting is allowed to fail, so it must not be able to slow down
or break the execution engine.

Records are written **unredacted**; redaction is applied on the reporting path
so the Hub-pushed ``redactEnabled`` switch stays authoritative and can be
flipped without regenerating history. The file is created 0600 inside the
0700 state directory.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .client_context import ClientContext, current_client_context

logger = logging.getLogger("remote_host_mcp.audit")

PROVIDER = "rhmcp"

# Local pre-truncation. The Hub truncates again at the same limits; doing it
# here keeps a single huge ``cat`` from filling the local log.
ARGS_TEXT_LIMIT_BYTES = 16 * 1024
OUTPUT_PREVIEW_LIMIT_BYTES = 4 * 1024

_TOOL_CALL_METHOD = "tools/call"
_ERROR_CODE_MAX = 64


def truncate_utf8(text: str, limit_bytes: int) -> str:
    """Truncate to at most ``limit_bytes`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    return encoded[:limit_bytes].decode("utf-8", errors="ignore")


def new_event_id() -> str:
    return f"cal_{secrets.token_hex(6)}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def error_code_for(exc: BaseException) -> str:
    """Reduce an exception to a short identifier, never a message.

    The Hub rejects anything outside ``/^[a-z0-9_]{1,64}$/`` and long text would
    leak host detail into the report, so only the class name survives. CamelCase
    is split on word boundaries (``HTTPError`` -> ``http_error``).
    """
    name = type(exc).__name__
    out: list[str] = []
    for index, char in enumerate(name):
        if not char.isupper():
            out.append(char.lower() if char.isalnum() else "_")
            continue
        if index:
            previous = name[index - 1]
            following = name[index + 1] if index + 1 < len(name) else ""
            if previous.islower() or previous.isdigit() or (previous.isupper() and following.islower()):
                out.append("_")
        out.append(char.lower())
    code = "".join(out).strip("_") or "error"
    while "__" in code:
        code = code.replace("__", "_")
    return code[:_ERROR_CODE_MAX]


@dataclass(slots=True)
class AuditEvent:
    """One tool invocation, shaped for the Hub ``/agent/v1/report`` schema."""

    id: str
    provider: str
    toolName: str
    status: str
    startedAt: str
    durationMs: int | None = None
    exitCode: int | None = None
    errorCode: str | None = None
    clientIp: str | None = None
    clientCountry: str | None = None
    clientCity: str | None = None
    clientRegion: str | None = None
    clientOrg: str | None = None
    clientAsn: str | None = None
    clientUa: str | None = None
    argsText: str | None = None
    outputBytes: int | None = None
    outputPreview: str | None = None
    outputRef: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def build(
        cls,
        *,
        tool_name: str,
        status: str,
        started_at: str,
        duration_ms: int | None,
        arguments: Any = None,
        client: ClientContext | None = None,
        output: str | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
    ) -> "AuditEvent":
        context = client if client is not None else current_client_context()
        args_text: str | None = None
        if arguments is not None:
            try:
                args_text = truncate_utf8(
                    json.dumps(arguments, ensure_ascii=False, default=str),
                    ARGS_TEXT_LIMIT_BYTES,
                )
            except (TypeError, ValueError):
                args_text = None
        output_bytes: int | None = None
        output_preview: str | None = None
        if output is not None:
            output_bytes = len(output.encode("utf-8"))
            output_preview = truncate_utf8(output, OUTPUT_PREVIEW_LIMIT_BYTES)
        event = cls(
            id=new_event_id(),
            provider=PROVIDER,
            toolName=tool_name,
            status=status,
            startedAt=started_at,
            durationMs=duration_ms,
            exitCode=exit_code,
            errorCode=error_code,
            argsText=args_text,
            outputBytes=output_bytes,
            outputPreview=output_preview,
            outputRef=None,
        )
        for key, value in context.as_audit_fields().items():
            setattr(event, key, value)
        return event


@dataclass(slots=True)
class _State:
    handle: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class AuditWriter:
    """Append-only JSONL sink. Never raises into the caller's code path."""

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self._state = _State()
        self._open_failed = False

    def _handle(self) -> Any:
        state = self._state
        if state.handle is not None:
            return state.handle
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        state.handle = handle
        return handle

    def record(self, event: AuditEvent) -> bool:
        """Append one event. Returns False when auditing is off or the write failed."""
        if not self.enabled:
            return False
        try:
            line = json.dumps(event.as_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            payload = line.encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.warning("audit event serialization failed: %s", type(exc).__name__)
            return False
        try:
            with self._state.lock:
                handle = self._handle()
                os.write(handle, payload)
            return True
        except OSError as exc:
            if not self._open_failed:
                self._open_failed = True
                logger.warning("audit log write failed (%s): auditing continues without persistence", type(exc).__name__)
            return False

    def close(self) -> None:
        state = self._state
        with state.lock:
            handle, state.handle = state.handle, None
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass


def _content_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if content is None and isinstance(result, Mapping):
        content = result.get("content")
    if not content:
        return ""
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, Mapping):
            text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _structured(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is None and isinstance(result, Mapping):
        structured = result.get("structuredContent", result.get("structured_content"))
    return structured


def _is_error_result(result: Any) -> bool:
    flag = getattr(result, "is_error", None)
    if flag is None and isinstance(result, Mapping):
        flag = result.get("isError", result.get("is_error"))
    return bool(flag)


def _extract_exit_code(result: Any) -> int | None:
    structured = _structured(result)
    if structured is None:
        return None
    for key in ("exit_code", "exitCode"):
        if isinstance(structured, Mapping):
            value = structured.get(key)
        else:
            value = getattr(structured, key, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


class AuditMiddleware:
    """Context-tier middleware recording every ``tools/call`` as one JSONL line.

    Sits at the MCP tool dispatch layer: ``ctx.params`` carries the tool name and
    its arguments before validation, and the returned handler result carries the
    output. Transport-level instrumentation is deliberately avoided — it cannot
    see tool semantics.
    """

    def __init__(self, writer: AuditWriter) -> None:
        self.writer = writer

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "method", None) != _TOOL_CALL_METHOD or getattr(ctx, "request_id", None) is None:
            return await call_next(ctx)

        params = getattr(ctx, "params", None) or {}
        tool_name = params.get("name") if isinstance(params, Mapping) else None
        if not isinstance(tool_name, str) or not tool_name:
            return await call_next(ctx)

        started_at = utc_now_iso()
        started = time.monotonic()
        try:
            result = await call_next(ctx)
        except BaseException as exc:
            self._emit(
                tool_name=tool_name,
                params=params,
                started_at=started_at,
                started=started,
                status="error",
                error_code=error_code_for(exc),
            )
            raise
        self._emit(
            tool_name=tool_name,
            params=params,
            started_at=started_at,
            started=started,
            status="error" if _is_error_result(result) else "ok",
            result=result,
        )
        return result

    def _emit(
        self,
        *,
        tool_name: str,
        params: Any,
        started_at: str,
        started: float,
        status: str,
        result: Any = None,
        error_code: str | None = None,
    ) -> None:
        try:
            arguments = params.get("arguments") if isinstance(params, Mapping) else None
            event = AuditEvent.build(
                tool_name=tool_name,
                status=status,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                arguments=arguments,
                output=_content_text(result) if result is not None else None,
                exit_code=_extract_exit_code(result) if result is not None else None,
                error_code=error_code,
            )
            self.writer.record(event)
        except Exception as exc:  # audit must never break execution
            logger.warning("audit emission failed: %s", type(exc).__name__)
