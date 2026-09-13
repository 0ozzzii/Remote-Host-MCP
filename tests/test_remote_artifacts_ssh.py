from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from mcp import Client
from mcp.types import EmbeddedResource, ImageContent
import pytest

from remote_host_mcp.app import build_server
from remote_host_mcp.artifact_helpers import file_artifact
from remote_host_mcp.config import Settings
import remote_host_mcp.ssh_helpers as ssh_helpers


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("RHMCP_AUTH_MODE", "capability")
    monkeypatch.setenv("RHMCP_PATH_KEY", "a" * 48)
    monkeypatch.setenv("RHMCP_PUBLIC_HOST", "host.example.com")
    monkeypatch.setenv("RHMCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("RHMCP_STATE_DIR", str(tmp_path / ".state"))
    return Settings.from_env()


def test_file_artifact_returns_native_image_and_embedded_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = settings(monkeypatch, tmp_path)
    image = tmp_path / "shot.png"
    image.write_bytes(PNG)
    blocks = file_artifact(str(image), 1024 * 1024, cfg)
    image_blocks = [block for block in blocks if isinstance(block, ImageContent)]
    assert len(image_blocks) == 1
    assert image_blocks[0].mime_type == "image/png"
    assert base64.b64decode(image_blocks[0].data) == PNG

    blob = tmp_path / "result.bin"
    blob.write_bytes(b"artifact-binary")
    blocks = file_artifact(str(blob), 1024 * 1024, cfg)
    resources = [block for block in blocks if isinstance(block, EmbeddedResource)]
    assert len(resources) == 1
    assert base64.b64decode(resources[0].resource.blob) == b"artifact-binary"

    with pytest.raises(ValueError, match="inline limit"):
        file_artifact(str(blob), 4, cfg)


@pytest.mark.asyncio
async def test_exec_generated_png_can_be_returned_as_native_mcp_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    target = tmp_path / "generated.png"
    encoded = base64.b64encode(PNG).decode("ascii")
    async with Client(build_server(cfg)) as client:
        result = await client.call_tool(
            "exec",
            {"command": f"printf %s {encoded} | base64 -d > {target}"},
        )
        assert result.structured_content is not None
        assert result.structured_content["success"] is True
        artifact = await client.call_tool("file_artifact", {"path": str(target)})
    images = [content for content in artifact.content if isinstance(content, ImageContent)]
    assert len(images) == 1
    assert base64.b64decode(images[0].data) == PNG


@pytest.mark.asyncio
async def test_ssh_exec_uses_strict_noninteractive_options_and_stdin_not_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def fake_run(argv: list[str], *, timeout_ms: int, settings: Settings, input_data: bytes | None = None):
        captured["argv"] = list(argv)
        captured["input"] = input_data
        return ssh_helpers._RunOutcome(
            returncode=0, stdout="ok", stderr="", duration_ms=3, timed_out=False,
            terminated_by=None, truncated=False, output_bytes_returned=2, output_bytes_total=2,
        )

    monkeypatch.setattr(ssh_helpers, "_client_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ssh_helpers, "_run_process", fake_run)
    secret = "SSH_SECRET_123"
    result = await ssh_helpers.ssh_exec(
        "build-host", None, None, f"printf {secret}", 5, 5000, cfg
    )
    assert result.success
    argv = captured["argv"]
    assert isinstance(argv, list)
    argv_text = " ".join(argv)
    assert secret not in argv_text
    assert "BatchMode=yes" in argv_text
    assert "PasswordAuthentication=no" in argv_text
    assert "KbdInteractiveAuthentication=no" in argv_text
    assert "StrictHostKeyChecking=yes" in argv_text
    assert captured["input"] == f"printf {secret}\n".encode()
    with pytest.raises(ValueError, match="username"):
        await ssh_helpers.ssh_check("build-host", "bad user", None, 5, cfg)


@pytest.mark.asyncio
async def test_canonical_surface_has_65_tools_and_no_ssh_credential_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = settings(monkeypatch, tmp_path)
    expected = json.loads((Path(__file__).parent / "tool_manifest.json").read_text(encoding="utf-8"))
    async with Client(build_server(cfg)) as client:
        listed = await client.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert sorted(tools) == sorted(expected)
    assert len(tools) == 65
    for name in ("file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download"):
        assert name in tools
    forbidden = {"password", "private_key", "identity_file", "known_hosts", "known_hosts_file", "xauthority"}
    for name in ("ssh_check", "ssh_exec", "ssh_upload", "ssh_download"):
        props = set(tools[name].input_schema.get("properties", {}))
        assert not (props & forbidden)
        assert tools[name].input_schema.get("additionalProperties") is False
