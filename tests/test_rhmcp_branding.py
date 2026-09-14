from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mcp import Client
import pytest

from remote_host_mcp.app import build_server
from remote_host_mcp.branding import PRODUCT_DESCRIPTION, PRODUCT_NAME
from remote_host_mcp.config import Settings
from remote_host_mcp.tasks_extension import _task_payload


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("RHMCP_AUTH_MODE", "capability")
    monkeypatch.setenv("RHMCP_PATH_KEY", "r" * 48)
    monkeypatch.setenv("RHMCP_PUBLIC_HOST", "host.example.com")
    monkeypatch.setenv("RHMCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("RHMCP_STATE_DIR", str(tmp_path / ".state"))
    return Settings.from_env()


def _model_visible_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@pytest.mark.asyncio
async def test_canonical_app_has_generic_identity_and_tool_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = build_server(_settings(monkeypatch, tmp_path))

    lowlevel = server._lowlevel_server  # pinned MCP SDK 2.2.x compatibility seam
    assert lowlevel.name == PRODUCT_NAME
    assert lowlevel.title == PRODUCT_NAME
    assert lowlevel.description == PRODUCT_DESCRIPTION

    async with Client(server) as client:
        listed = await client.list_tools()
        status = await client.call_tool("status", {})

    assert len(listed.tools) == 65
    visible = _model_visible_text(
        [
            {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in listed.tools
        ]
    )
    assert "DSW" not in visible
    assert "DSW_MCP_" not in visible
    assert "DSWD" not in visible

    assert status.structured_content is not None
    assert status.structured_content["service"] == PRODUCT_NAME
    assert status.structured_content["version"] == "0.2.0a4"


def test_canonical_health_route_replaces_legacy_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = build_server(_settings(monkeypatch, tmp_path))
    health = [route for route in server._custom_starlette_routes if getattr(route, "path", None) == "/health"]
    assert len(health) == 1
    assert getattr(health[0], "name", None) == "rhmcp-health"


def test_tasks_status_message_is_generic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    status = SimpleNamespace(
        job_id="a" * 32,
        status="starting",
        exit_code=None,
        timed_out=False,
        terminated_by=None,
        duration_ms=None,
        completed_at=None,
        heartbeat_at=100,
        last_output_at=None,
        started_at=100,
        created_at=100,
    )
    payload = _task_payload(status, settings, create=True)
    assert payload["statusMessage"] == "Remote Host MCP durable job: starting"
    assert "DSW" not in _model_visible_text(payload)


def test_legacy_environment_remains_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Existing DSWD 2.x .env files remain valid during migration; clean standalone
    # installations and all validation messages use the canonical RHMCP_* names.
    for suffix in ("AUTH_MODE", "PATH_KEY", "PUBLIC_HOST", "ALLOWED_ROOTS", "STATE_DIR"):
        monkeypatch.delenv(f"RHMCP_{suffix}", raising=False)
    monkeypatch.setenv("DSW_MCP_AUTH_MODE", "capability")
    monkeypatch.setenv("DSW_MCP_PATH_KEY", "l" * 48)
    monkeypatch.setenv("DSW_MCP_PUBLIC_HOST", "legacy.example.com")
    monkeypatch.setenv("DSW_MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("DSW_MCP_STATE_DIR", str(tmp_path / ".legacy-state"))
    settings = Settings.from_env()
    assert settings.path_key == "l" * 48
    assert settings.public_host == "legacy.example.com"
