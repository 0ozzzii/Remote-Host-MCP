from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from remote_host_mcp.audit import (
    ARGS_TEXT_LIMIT_BYTES,
    OUTPUT_PREVIEW_LIMIT_BYTES,
    AuditEvent,
    AuditMiddleware,
    AuditWriter,
    error_code_for,
    truncate_utf8,
)
from remote_host_mcp.client_context import ClientContext, bind_client_context


class _FakeContext:
    def __init__(self, params: object, method: str = "tools/call", request_id: str | None = "1") -> None:
        self.params = params
        self.method = method
        self.request_id = request_id


class _FakeResult:
    def __init__(self, text: str, *, is_error: bool = False, structured: object | None = None) -> None:
        self.content = [type("Text", (), {"text": text})()]
        self.structured_content = structured
        self.is_error = is_error


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_truncate_utf8_counts_bytes_not_characters() -> None:
    assert truncate_utf8("abc", 16) == "abc"
    # 4 bytes per emoji: a 3-byte budget must not emit a broken character.
    assert truncate_utf8("😀😀", 4) == "😀"
    assert truncate_utf8("😀😀", 3) == ""
    assert len(truncate_utf8("x" * 100, 10).encode()) == 10


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ToolError", "tool_error"),
        ("TimeoutError", "timeout_error"),
        ("ValueError", "value_error"),
        ("HTTPError", "http_error"),
    ],
)
def test_error_code_is_a_short_identifier(name: str, expected: str) -> None:
    exc = type(name, (Exception,), {})("secret detail that must not leak")
    code = error_code_for(exc)
    assert code == expected
    assert "secret" not in code
    assert len(code) <= 64


def test_error_code_sanitizes_odd_class_names() -> None:
    exc = type("Weird-Name.X", (Exception,), {})()
    code = error_code_for(exc)
    assert code
    assert all(char in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in code)


def test_writer_appends_jsonl_with_restrictive_permissions(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "calls.jsonl"
    writer = AuditWriter(path)
    assert writer.record(AuditEvent.build(tool_name="exec", status="ok", started_at="2026-09-15T10:00:00.000Z", duration_ms=1))
    assert writer.record(AuditEvent.build(tool_name="job_run", status="error", started_at="2026-09-15T10:00:01.000Z", duration_ms=2))
    writer.close()

    lines = _read_lines(path)
    assert [line["toolName"] for line in lines] == ["exec", "job_run"]
    assert all(line["provider"] == "rhmcp" for line in lines)
    assert all(line["id"].startswith("cal_") for line in lines)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writer_is_a_noop_when_disabled(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path, enabled=False)
    assert writer.record(AuditEvent.build(tool_name="exec", status="ok", started_at="t", duration_ms=0)) is False
    writer.close()
    assert not path.exists()


def test_writer_never_raises_on_unwritable_path(tmp_path: Path) -> None:
    # A directory where the log file should be: every write must fail quietly.
    blocked = tmp_path / "calls.jsonl"
    blocked.mkdir()
    writer = AuditWriter(blocked)
    assert writer.record(AuditEvent.build(tool_name="exec", status="ok", started_at="t", duration_ms=0)) is False
    writer.close()


# ------------------------------------------------------------------ rotation


def _filler(tool_name: str, *, size: int = 200) -> AuditEvent:
    return AuditEvent.build(
        tool_name=tool_name,
        status="ok",
        started_at="t",
        duration_ms=1,
        arguments={"command": "x" * size},
    )


def test_writer_rotates_once_the_size_cap_is_reached(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path, max_bytes=1024, keep_files=3)
    for _ in range(20):
        assert writer.record(_filler("exec")) is True
    writer.close()

    assert (tmp_path / "calls.jsonl.1").exists()
    # The whole point: no single file may grow past the cap.
    assert path.stat().st_size <= 1024
    assert (tmp_path / "calls.jsonl.1").stat().st_size <= 1024


def test_writer_keeps_at_most_n_generations(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path, max_bytes=512, keep_files=2)
    for _ in range(50):
        writer.record(_filler("exec"))
    writer.close()

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "calls.jsonl",
        "calls.jsonl.1",
        "calls.jsonl.2",
    ]


def test_writer_appends_to_the_new_file_after_rotation(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path, max_bytes=512, keep_files=3)
    for _ in range(10):
        writer.record(_filler("old"))
    assert (tmp_path / "calls.jsonl.1").exists()

    assert writer.record(_filler("newest")) is True
    writer.close()

    assert [line["toolName"] for line in _read_lines(path)][-1] == "newest"


def test_writer_survives_a_failed_rotation(tmp_path: Path) -> None:
    # A directory where the first rotated generation belongs: every rename fails.
    (tmp_path / "calls.jsonl.1").mkdir()
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path, max_bytes=256, keep_files=1)
    for _ in range(5):
        assert writer.record(_filler("exec")) is True
    writer.close()

    # Rotation is best-effort; the records still land in the log that is open.
    assert len(_read_lines(path)) == 5


def test_writer_rotation_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path, max_bytes=None, keep_files=3)
    for _ in range(20):
        writer.record(_filler("exec"))
    writer.close()

    assert [p.name for p in tmp_path.iterdir()] == ["calls.jsonl"]


def test_event_truncates_args_and_output_locally() -> None:
    event = AuditEvent.build(
        tool_name="exec",
        status="ok",
        started_at="2026-09-15T10:00:00.000Z",
        duration_ms=5,
        arguments={"command": "x" * 100_000},
        output="y" * 100_000,
    )
    assert event.argsText is not None
    assert len(event.argsText.encode()) <= ARGS_TEXT_LIMIT_BYTES
    assert event.outputPreview is not None
    assert len(event.outputPreview.encode()) <= OUTPUT_PREVIEW_LIMIT_BYTES
    # outputBytes still reports the untruncated size.
    assert event.outputBytes == 100_000


def test_event_carries_caller_identity_and_null_geo_fields() -> None:
    with bind_client_context(ClientContext(ip="203.0.113.7", country="CN", ua="UA/1")):
        event = AuditEvent.build(tool_name="exec", status="ok", started_at="t", duration_ms=0)
    payload = event.as_dict()
    assert payload["clientIp"] == "203.0.113.7"
    assert payload["clientCountry"] == "CN"
    assert payload["clientUa"] == "UA/1"
    assert payload["clientCity"] is None
    assert payload["clientRegion"] is None
    assert payload["clientOrg"] is None
    assert payload["clientAsn"] is None
    assert payload["outputRef"] is None


def test_event_requires_only_the_documented_mandatory_fields() -> None:
    payload = AuditEvent.build(tool_name="system_info", status="ok", started_at="t", duration_ms=3).as_dict()
    assert payload["toolName"] == "system_info"
    assert payload["status"] == "ok"
    assert payload["startedAt"] == "t"
    assert payload["durationMs"] == 3


async def test_middleware_records_successful_call(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path)
    middleware = AuditMiddleware(writer)

    async def call_next(_ctx: object) -> object:
        return _FakeResult("hello world", structured={"exit_code": 0})

    ctx = _FakeContext({"name": "exec", "arguments": {"command": "ls -la"}})
    result = await middleware(ctx, call_next)
    writer.close()

    assert result is not None
    line = _read_lines(path)[0]
    assert line["toolName"] == "exec"
    assert line["status"] == "ok"
    assert line["exitCode"] == 0
    assert json.loads(line["argsText"]) == {"command": "ls -la"}
    assert line["outputPreview"] == "hello world"
    assert line["outputBytes"] == len("hello world")
    assert isinstance(line["durationMs"], int)


async def test_middleware_records_error_status_without_a_message(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path)
    middleware = AuditMiddleware(writer)

    async def call_next(_ctx: object) -> object:
        return _FakeResult("permission denied", is_error=True)

    await middleware(_FakeContext({"name": "read_text_file", "arguments": {}}), call_next)
    writer.close()
    assert _read_lines(path)[0]["status"] == "error"


async def test_middleware_records_and_reraises_exceptions(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path)
    middleware = AuditMiddleware(writer)

    class ToolError(Exception):
        pass

    async def call_next(_ctx: object) -> object:
        raise ToolError("boom: /etc/shadow contents here")

    with pytest.raises(ToolError):
        await middleware(_FakeContext({"name": "exec", "arguments": {"command": "cat /etc/shadow"}}), call_next)
    writer.close()

    line = _read_lines(path)[0]
    assert line["status"] == "error"
    assert line["errorCode"] == "tool_error"
    assert "shadow contents" not in json.dumps(line)


async def test_middleware_ignores_non_tool_calls(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path)
    middleware = AuditMiddleware(writer)

    async def call_next(_ctx: object) -> str:
        return "ok"

    assert await middleware(_FakeContext({"name": "exec"}, method="tools/list"), call_next) == "ok"
    assert await middleware(_FakeContext({"name": "exec"}, request_id=None), call_next) == "ok"
    assert await middleware(_FakeContext({"arguments": {}}), call_next) == "ok"
    writer.close()
    assert not path.exists()


async def test_middleware_failure_never_breaks_the_tool_call(tmp_path: Path) -> None:
    blocked = tmp_path / "calls.jsonl"
    blocked.mkdir()
    middleware = AuditMiddleware(AuditWriter(blocked))

    async def call_next(_ctx: object) -> object:
        return _FakeResult("still works")

    result = await middleware(_FakeContext({"name": "exec", "arguments": {}}), call_next)
    assert getattr(result, "content")[0].text == "still works"


async def test_audit_records_the_caller_when_wired_in_host_server_order(tmp_path: Path) -> None:
    """The whole point of P1+P2 together: the audit line carries the real caller.

    host_server appends ClientContextMiddleware first, which makes it the
    outermost of the two, so the identity is bound before the audit middleware
    runs. This pins that ordering.
    """
    from remote_host_mcp.client_context import ClientContextMiddleware

    path = tmp_path / "calls.jsonl"
    writer = AuditWriter(path)
    audit = AuditMiddleware(writer)
    context = ClientContextMiddleware()

    class _Request:
        headers = {"CF-Connecting-IP": "203.0.113.7", "CF-IPCountry": "CN", "User-Agent": "ChatGPT/1.0"}
        client = type("Peer", (), {"host": "127.0.0.1"})()

    ctx = _FakeContext({"name": "exec", "arguments": {"command": "ls"}})
    ctx.request = _Request()

    async def tool(_ctx: object) -> object:
        return _FakeResult("done", structured={"exit_code": 0})

    await context(ctx, lambda c: audit(c, tool))
    writer.close()

    line = _read_lines(path)[0]
    assert line["clientIp"] == "203.0.113.7"
    assert line["clientCountry"] == "CN"
    assert line["clientUa"] == "ChatGPT/1.0"
    assert line["clientCity"] is None
    assert line["toolName"] == "exec"
