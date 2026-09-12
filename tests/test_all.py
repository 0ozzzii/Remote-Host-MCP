from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest
from mcp import Client

from dsw_direct_mcp.config import ConfigError, Settings
from dsw_direct_mcp.executor import run_shell
from dsw_direct_mcp.filesystem import list_directory, read_text_file
from dsw_direct_mcp.server import build_server


def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, output_bytes: int = 4096) -> Settings:
    monkeypatch.setenv("DSW_MCP_PATH_KEY", "k" * 48)
    monkeypatch.setenv("DSW_MCP_PUBLIC_HOST", "direct.example.com")
    monkeypatch.setenv("DSW_MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("DSW_MCP_STATE_DIR", str(tmp_path / ".dswd-state"))
    monkeypatch.setenv("DSW_MCP_MAX_OUTPUT_BYTES", str(output_bytes))
    return Settings.from_env()


def test_config_accepts_reserved_bootstrap_hostname(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DSW_MCP_PATH_KEY", "k" * 48)
    monkeypatch.setenv("DSW_MCP_PUBLIC_HOST", "mcp.invalid")
    monkeypatch.setenv("DSW_MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("DSW_MCP_STATE_DIR", str(tmp_path / ".dswd-state"))
    assert Settings.from_env().public_host == "mcp.invalid"


def test_shell_scripts_parse() -> None:
    root = Path(__file__).resolve().parents[1]
    scripts = [root / "rmcp", *sorted((root / "scripts").glob("*.sh"))]
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_global_rmcp_registration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / "bin"
    env = os.environ.copy()
    env["RMCP_BIN_DIR"] = str(bin_dir)
    subprocess.run(
        ["bash", str(root / "scripts" / "register-command.sh")],
        check=True,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    launcher = bin_dir / "rmcp"
    assert launcher.exists()
    resolved = subprocess.check_output([str(launcher), "--root"], text=True).strip()
    assert resolved == str(root)


def test_config_rejects_public_bind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings(monkeypatch, tmp_path)
    monkeypatch.setenv("DSW_MCP_BIND_HOST", "0.0.0.0")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_filesystem_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = settings(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    listing = list_directory(str(tmp_path), 10, s)
    assert any(e.name == "a.txt" for e in listing.entries)
    out = read_text_file(str(tmp_path / "a.txt"), 2, 1, 1024, s)
    assert out.text == "two\n"
    assert out.truncated is True
    with pytest.raises(ValueError):
        s.resolve_allowed_path("/etc/passwd")


@pytest.mark.asyncio
async def test_executor_success_error_timeout_and_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = settings(monkeypatch, tmp_path)
    ok = await run_shell("printf hello", 2000, str(tmp_path), s)
    assert ok.success and ok.stdout == "hello" and ok.exit_code == 0

    bad = await run_shell("echo bad >&2; exit 7", 2000, str(tmp_path), s)
    assert not bad.success and bad.exit_code == 7 and bad.error and bad.error.code == "NONZERO_EXIT"

    timeout = await run_shell("sleep 5", 1000, str(tmp_path), s)
    assert not timeout.success and timeout.timed_out and timeout.terminated_by in {"SIGTERM", "SIGKILL"}

    cap = await run_shell("python -c \"print('x'*10000)\"", 3000, str(tmp_path), s)
    assert cap.success and cap.truncated and len(cap.stdout.encode()) <= 4096


@pytest.mark.asyncio
async def test_mcp_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = build_server(settings(monkeypatch, tmp_path))
    async with Client(server) as client:
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        expected = {
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
        }
        assert expected <= set(tools)
        assert all(tool.output_schema is not None for tool in tools.values())
        assert tools["status"].annotations.read_only_hint is True
        assert tools["download_chunk"].annotations.read_only_hint is True
        assert tools["upload_chunk"].annotations.idempotent_hint is True
        assert tools["upload_finish"].annotations.destructive_hint is True
        assert tools["exec"].annotations.read_only_hint is False
        assert tools["exec"].annotations.destructive_hint is True
        assert tools["exec"].annotations.idempotent_hint is False
        assert tools["exec"].annotations.open_world_hint is True
        result = await client.call_tool("status", {})
        assert not result.is_error
        assert result.structured_content["online"] is True
