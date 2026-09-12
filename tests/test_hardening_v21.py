from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import jwt
import mcp.types as types
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client import advertise
from pydantic import Field

from dsw_direct_mcp.auth import OAuthJwksTokenVerifier, build_oauth_components
from dsw_direct_mcp.config import ConfigError, Settings
from dsw_direct_mcp.filesystem import read_file_chunk, write_text_file
from dsw_direct_mcp.jobs import start_job
from dsw_direct_mcp.mature_server import build_server
from dsw_direct_mcp.secure_paths import openat2_supported
from dsw_direct_mcp.system_helpers import pidfd_supported, process_info, process_signal
from dsw_direct_mcp.tasks_extension import TASKS_EXTENSION_ID
from dsw_direct_mcp.terminal import TerminalManager
from dsw_direct_mcp.transfer import begin_upload, finish_upload, upload_chunk


def make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("DSW_MCP_AUTH_MODE", "capability")
    monkeypatch.setenv("DSW_MCP_PATH_KEY", "h" * 48)
    monkeypatch.setenv("DSW_MCP_PUBLIC_HOST", "direct.example.com")
    monkeypatch.setenv("DSW_MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("DSW_MCP_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("DSW_MCP_JOB_HEARTBEAT_SECONDS", "1")
    monkeypatch.setenv("DSW_MCP_TERMINAL_BUFFER_BYTES", "65536")
    monkeypatch.setenv("DSW_MCP_TERMINAL_READ_MAX_BYTES", "65536")
    monkeypatch.setenv("DSW_MCP_TERMINAL_OSC133", "true")
    monkeypatch.setenv("DSW_MCP_TASKS_EXTENSION", "true")
    return Settings.from_env()


def test_default_capability_mode_is_v2_compatible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    assert settings.auth_mode == "capability"
    assert settings.mcp_path == "/mcp/" + "h" * 48
    assert settings.tasks_extension_enabled is True
    assert settings.terminal_osc133_enabled is True
    verifier, auth = build_oauth_components(settings)
    assert verifier is None and auth is None


def test_oauth_mode_requires_https_metadata_and_uses_stable_mcp_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("DSW_MCP_AUTH_MODE", "oauth")
    monkeypatch.delenv("DSW_MCP_PATH_KEY", raising=False)
    monkeypatch.setenv("DSW_MCP_OAUTH_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("DSW_MCP_OAUTH_JWKS_URL", "https://auth.example.com/.well-known/jwks.json")
    monkeypatch.setenv("DSW_MCP_OAUTH_AUDIENCE", "https://direct.example.com/mcp")
    monkeypatch.setenv("DSW_MCP_OAUTH_SCOPES", "dswd.read dswd.write")
    settings = Settings.from_env()
    assert settings.auth_mode == "oauth"
    assert settings.mcp_path == "/mcp"
    assert settings.public_url == "https://direct.example.com/mcp"
    verifier, auth = build_oauth_components(settings)
    assert verifier is not None and auth is not None
    assert str(auth.issuer_url).startswith("https://auth.example.com")
    assert auth.required_scopes == ["dswd.read", "dswd.write"]
    assert auth.validate_token_resource is True

    monkeypatch.delenv("DSW_MCP_OAUTH_JWKS_URL")
    with pytest.raises(ConfigError, match="DSW_MCP_OAUTH_JWKS_URL"):
        Settings.from_env()


@pytest.mark.asyncio
async def test_oauth_jwt_verifier_checks_signature_issuer_audience_and_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("DSW_MCP_AUTH_MODE", "oauth")
    monkeypatch.setenv("DSW_MCP_OAUTH_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("DSW_MCP_OAUTH_JWKS_URL", "https://auth.example.com/jwks.json")
    monkeypatch.setenv("DSW_MCP_OAUTH_AUDIENCE", "https://direct.example.com/mcp")
    settings = Settings.from_env()
    verifier = OAuthJwksTokenVerifier(settings)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    monkeypatch.setattr(
        verifier._jwks,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=public_key),
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://auth.example.com",
            "aud": "https://direct.example.com/mcp",
            "sub": "user-1",
            "client_id": "chatgpt-client",
            "scope": "dswd",
            "iat": now,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    access = await verifier.verify_token(token)
    assert access is not None
    assert access.client_id == "chatgpt-client"
    assert access.subject == "user-1"
    assert access.scopes == ["dswd"]
    assert access.resource == "https://direct.example.com/mcp"

    wrong_aud = jwt.encode(
        {
            "iss": "https://auth.example.com",
            "aud": "https://other.example.com/mcp",
            "sub": "user-1",
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    assert await verifier.verify_token(wrong_aud) is None


def test_openat2_boundary_and_symlink_behavior(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    inside = tmp_path / "inside.txt"
    inside.write_bytes(b"inside")
    inside_link = tmp_path / "inside-link"
    inside_link.symlink_to(inside)
    assert read_file_chunk(str(inside_link), 0, 16, settings).bytes_returned == 6

    outside_dir = tmp_path.parent / f"outside-{tmp_path.name}"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "secret.txt"
    outside_file.write_bytes(b"outside")
    escape = tmp_path / "escape"
    escape.symlink_to(outside_dir, target_is_directory=True)
    try:
        with pytest.raises((ValueError, OSError)):
            read_file_chunk(str(escape / "secret.txt"), 0, 16, settings)
        with pytest.raises((ValueError, OSError)):
            write_text_file(str(escape / "created.txt"), "nope", False, None, 0o644, settings)
        assert not (outside_dir / "created.txt").exists()
    finally:
        escape.unlink(missing_ok=True)
        outside_file.unlink(missing_ok=True)
        outside_dir.rmdir()

    assert isinstance(openat2_supported(settings), bool)


def test_transfer_commit_is_dirfd_anchored_and_escape_parent_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    data = b"anchored-transfer-v21"
    digest = hashlib.sha256(data).hexdigest()
    target = tmp_path / "result.bin"
    started = begin_upload(str(target), len(data), digest, 0o640, False, settings)
    chunk = upload_chunk(
        started.upload_id,
        0,
        base64.b64encode(data).decode("ascii"),
        hashlib.sha256(data).hexdigest(),
        settings,
    )
    assert chunk.complete is True
    finished = finish_upload(started.upload_id, settings)
    assert finished.success is True
    assert finished.sha256 == digest
    assert target.read_bytes() == data

    outside_dir = tmp_path.parent / f"transfer-outside-{tmp_path.name}"
    outside_dir.mkdir(exist_ok=True)
    escape = tmp_path / "upload-escape"
    escape.symlink_to(outside_dir, target_is_directory=True)
    try:
        with pytest.raises((ValueError, OSError)):
            begin_upload(str(escape / "escaped.bin"), len(data), digest, 0o600, False, settings)
        assert not (outside_dir / "escaped.bin").exists()
    finally:
        escape.unlink(missing_ok=True)
        outside_dir.rmdir()


def test_process_signal_prefers_pidfd_when_runtime_supports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = subprocess.Popen(["sleep", "30"])
    try:
        info = process_info(proc.pid)
        used_pidfd = False
        if pidfd_supported():
            original = signal.pidfd_send_signal

            def wrapped(fd: int, sig: int, *args, **kwargs):
                nonlocal used_pidfd
                used_pidfd = True
                return original(fd, sig, *args, **kwargs)

            monkeypatch.setattr(signal, "pidfd_send_signal", wrapped)
        result = process_signal(proc.pid, info.start_ticks, "TERM")
        assert result.success
        proc.wait(timeout=5)
        if pidfd_supported():
            assert used_pidfd is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_osc133_semantic_completion_is_hidden_from_terminal_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    manager = TerminalManager(settings)
    opened = await manager.open(str(tmp_path), 100, 30)
    try:
        result = await manager.exec(opened.terminal_id, "printf 'OSC_OK'", 3000)
        assert result.completed is True
        assert result.exit_code == 0
        assert result.completion_source == "osc133"
        assert "OSC_OK" in result.output
        assert "]133;" not in result.output
        status = manager._session(opened.terminal_id).pending
        assert status is None or status.completed
    finally:
        await manager.close(opened.terminal_id)


class _TaskIdParams(types.RequestParams):
    task_id: str = Field(alias="taskId")


class _TaskGetResult(types.Result):
    result_type: str = Field(alias="resultType")
    task_id: str = Field(alias="taskId")
    status: str
    status_message: str | None = Field(default=None, alias="statusMessage")
    created_at: str = Field(alias="createdAt")
    last_updated_at: str = Field(alias="lastUpdatedAt")
    ttl_ms: int | None = Field(alias="ttlMs")
    poll_interval_ms: int | None = Field(default=None, alias="pollIntervalMs")
    result: dict | None = None


class _TaskGetRequest(types.Request[_TaskIdParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    params: _TaskIdParams


@pytest.mark.asyncio
async def test_mcp_tasks_get_adapts_existing_durable_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(monkeypatch, tmp_path)
    server = build_server(settings)
    started = start_job("printf task-ok", str(tmp_path), None, "task-adapter-test", settings)

    async with Client(server, mode="auto", extensions=[advertise(TASKS_EXTENSION_ID)]) as client:
        assert TASKS_EXTENSION_ID in (client.server_capabilities.extensions or {})
        result = await client.session.send_request(
            _TaskGetRequest(params=_TaskIdParams(taskId=started.job_id)),
            _TaskGetResult,
        )
        assert result.task_id == started.job_id
        assert result.status in {"working", "completed"}
        assert result.result_type == "complete"

        deadline = time.monotonic() + 5
        while result.status == "working" and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            result = await client.session.send_request(
                _TaskGetRequest(params=_TaskIdParams(taskId=started.job_id)),
                _TaskGetResult,
            )
        assert result.status == "completed"
        assert result.result is not None
        assert result.result["structuredContent"]["status"] == "completed"
