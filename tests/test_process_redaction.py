from __future__ import annotations

from contextlib import contextmanager
import inspect
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from mcp import Client
import pytest

from remote_host_mcp import system_helpers
from remote_host_mcp.app import build_server
from remote_host_mcp.config import Settings
from remote_host_mcp.system_helpers import _redact_argv, process_info, process_list


FAKE_SECRETS = (
    "FAKE_SECRET_1",
    "FAKE_SECRET_2",
    "FAKE_SECRET_3",
    "FAKE_SECRET_4",
    "FAKE_SECRET_5",
    "FAKE_SECRET_6",
    "FAKE_SECRET_7",
    "FAKE_SECRET_8",
    "FAKE_SECRET_9",
    "FAKE_SECRET_10",
)


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("RHMCP_AUTH_MODE", "capability")
    monkeypatch.setenv("RHMCP_PATH_KEY", "r" * 48)
    monkeypatch.setenv("RHMCP_PUBLIC_HOST", "host.example.com")
    monkeypatch.setenv("RHMCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("RHMCP_STATE_DIR", str(tmp_path / ".state"))
    return Settings.from_env()


def _assert_no_fake_secret(text: str) -> None:
    for secret in FAKE_SECRETS:
        assert secret not in text


def _serialize_tool_result(result: object) -> str:
    model_dump_json = getattr(result, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(by_alias=True)
    return json.dumps(result, ensure_ascii=False, default=str, sort_keys=True)


@contextmanager
def _synthetic_secret_process():
    credential_url = (
        "https://demo:FAKE_SECRET_5@example.invalid/path"
        "?access_token=FAKE_SECRET_6&safe=ok&key=FAKE_SECRET_7"
    )
    argv = [
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        "safe-marker",
        "-t",
        "plain-value",
        "--timeout",
        "30",
        "--token",
        "FAKE_SECRET_1",
        "middle-marker",
        "--ToKeN=FAKE_SECRET_2",
        "--PASSWORD",
        "FAKE_SECRET_3",
        "--api_key",
        "FAKE_SECRET_4",
        credential_url,
        "--credential",
        "FAKE_SECRET_8",
        "--endpoint=https://user:FAKE_SECRET_9@example.invalid/x?password=FAKE_SECRET_10",
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if Path(f"/proc/{proc.pid}/cmdline").exists():
                break
            time.sleep(0.01)
        assert proc.poll() is None
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_argv_redaction_covers_flags_urls_and_preserves_safe_diagnostics() -> None:
    argv = [
        "demo",
        "safe-marker",
        "-t",
        "plain-value",
        "--timeout",
        "30",
        "--token",
        "FAKE_SECRET_1",
        "middle-marker",
        "--ToKeN=FAKE_SECRET_2",
        "--PASSWORD",
        "FAKE_SECRET_3",
        "--api-key",
        "FAKE_SECRET_4",
        "https://demo:FAKE_SECRET_5@example.invalid/path?auth_token=FAKE_SECRET_6&safe=ok&key=FAKE_SECRET_7",
        "--credential",
        "FAKE_SECRET_8",
        "--endpoint=https://user:FAKE_SECRET_9@example.invalid/x?password=FAKE_SECRET_10",
    ]
    rendered = " ".join(_redact_argv(argv))

    _assert_no_fake_secret(rendered)
    assert rendered.count("<redacted>") >= 8
    assert "safe-marker" in rendered
    assert "middle-marker" in rendered
    assert "-t plain-value" in rendered
    assert "--timeout 30" in rendered
    assert "example.invalid" in rendered
    assert "safe=ok" in rendered


def test_process_list_and_process_info_never_serialize_synthetic_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with _synthetic_secret_process() as proc:
        info = process_info(proc.pid)
        listed = process_list(500)
        matching = next((row for row in listed.processes if row.pid == proc.pid), None)
        assert matching is not None

        serialized = info.model_dump_json() + listed.model_dump_json() + caplog.text
        _assert_no_fake_secret(serialized)
        assert "safe-marker" in info.cmdline
        assert "middle-marker" in info.cmdline
        assert "plain-value" in info.cmdline
        assert "<redacted>" in info.cmdline

    source = inspect.getsource(system_helpers)
    assert "/environ" not in source


@pytest.mark.asyncio
async def test_mcp_process_responses_logs_and_errors_do_not_contain_synthetic_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    server = build_server(_settings(monkeypatch, tmp_path))

    with _synthetic_secret_process() as proc:
        identity = process_info(proc.pid)
        async with Client(server) as client:
            info_result = await client.call_tool("process_info", {"pid": proc.pid})
            list_result = await client.call_tool("process_list", {"limit": 500})
            error_result = await client.call_tool(
                "process_signal",
                {
                    "pid": proc.pid,
                    "expected_start_ticks": identity.start_ticks + 1,
                    "signal_name": "TERM",
                },
            )

        assert not info_result.is_error
        assert not list_result.is_error
        assert error_result.is_error
        serialized = (
            _serialize_tool_result(info_result)
            + _serialize_tool_result(list_result)
            + _serialize_tool_result(error_result)
            + caplog.text
        )
        _assert_no_fake_secret(serialized)

        structured = info_result.structured_content or {}
        assert structured.get("pid") == proc.pid
        assert structured.get("start_ticks") == identity.start_ticks
        assert structured.get("executable")
        assert "safe-marker" in str(structured.get("cmdline", ""))
        assert "<redacted>" in str(structured.get("cmdline", ""))
        assert proc.poll() is None
