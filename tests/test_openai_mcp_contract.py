from __future__ import annotations

from pathlib import Path

from mcp import Client
import pytest

from dsw_direct_mcp.config import Settings
from dsw_direct_mcp.mature_server import build_server


EXPECTED_TOOLS = {
    "status",
    "list_directory",
    "path_info",
    "read_text_file",
    "read_file_chunk",
    "hash_file",
    "write_text_file",
    "make_directory",
    "move_path",
    "copy_path",
    "remove_path",
    "chmod_path",
    "upload_begin",
    "upload_chunk",
    "upload_status",
    "upload_finish",
    "upload_abort",
    "download_info",
    "download_chunk",
    "exec",
    "job_run",
    "job_start",
    "job_status",
    "job_read",
    "job_cancel",
    "job_list",
    "job_cleanup",
    "terminal_open",
    "terminal_exec",
    "terminal_write",
    "terminal_read",
    "terminal_status",
    "terminal_screen",
    "terminal_resize",
    "terminal_signal",
    "terminal_list",
    "terminal_close",
    "process_list",
    "process_info",
    "process_signal",
    "service_status",
    "service_action",
    "system_info",
}

READ_ONLY_TOOLS = {
    "status",
    "list_directory",
    "path_info",
    "read_text_file",
    "read_file_chunk",
    "hash_file",
    "upload_status",
    "download_info",
    "download_chunk",
    "job_status",
    "job_read",
    "job_list",
    "terminal_read",
    "terminal_status",
    "terminal_screen",
    "terminal_list",
    "process_list",
    "process_info",
    "service_status",
    "system_info",
}

OPEN_WORLD_TOOLS = {"exec", "job_run", "job_start", "terminal_exec", "terminal_write"}

HIGH_RISK_TOOLS = {
    "write_text_file",
    "move_path",
    "copy_path",
    "remove_path",
    "chmod_path",
    "upload_finish",
    "exec",
    "job_run",
    "job_start",
    "job_cancel",
    "job_cleanup",
    "terminal_exec",
    "terminal_write",
    "terminal_signal",
    "terminal_close",
    "process_signal",
    "service_action",
}


def make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("DSW_MCP_AUTH_MODE", "capability")
    monkeypatch.setenv("DSW_MCP_PATH_KEY", "m" * 48)
    monkeypatch.setenv("DSW_MCP_PUBLIC_HOST", "direct.example.com")
    monkeypatch.setenv("DSW_MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("DSW_MCP_STATE_DIR", str(tmp_path / ".state"))
    return Settings.from_env()


@pytest.mark.asyncio
async def test_openai_chatgpt_tool_contract_is_explicit_and_stable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Audit the model-visible tool contract, not just the Python handlers.

    OpenAI/ChatGPT relies on names, descriptions, JSON Schemas and MCP tool
    annotations for selection and confirmation behavior. The server still
    enforces all authorization/validation itself; these annotations are hints,
    not a security boundary.
    """
    settings = make_settings(monkeypatch, tmp_path)
    server = build_server(settings)

    async with Client(server) as client:
        listed = await client.list_tools()

    tools = {tool.name: tool for tool in listed.tools}
    assert set(tools) == EXPECTED_TOOLS

    for name, tool in tools.items():
        assert tool.title and tool.title.strip(), f"{name}: missing title"
        assert tool.description and tool.description.strip(), f"{name}: missing description"
        assert tool.input_schema is not None, f"{name}: missing input schema"
        assert tool.output_schema is not None, f"{name}: missing output schema"
        assert tool.annotations is not None, f"{name}: missing annotations"

        annotations = tool.annotations
        assert annotations.read_only_hint is not None, f"{name}: readOnlyHint unspecified"
        assert annotations.destructive_hint is not None, f"{name}: destructiveHint unspecified"
        assert annotations.idempotent_hint is not None, f"{name}: idempotentHint unspecified"
        assert annotations.open_world_hint is not None, f"{name}: openWorldHint unspecified"

        properties = tool.input_schema.get("properties", {})
        for parameter, schema in properties.items():
            assert schema.get("description"), f"{name}.{parameter}: missing parameter description"

    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].annotations.destructive_hint is False

    for name in EXPECTED_TOOLS - READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is False

    for name in OPEN_WORLD_TOOLS:
        assert tools[name].annotations.open_world_hint is True

    for name in EXPECTED_TOOLS - OPEN_WORLD_TOOLS:
        assert tools[name].annotations.open_world_hint is False

    for name in HIGH_RISK_TOOLS:
        assert tools[name].annotations.destructive_hint is True
