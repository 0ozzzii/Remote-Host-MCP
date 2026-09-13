from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one occurrence, found {text.count(old)} for {old!r}")
    write(path, text.replace(old, new, 1))


def replace_between(path: str, start: str, end: str, new: str) -> None:
    text = read(path)
    a = text.index(start)
    b = text.index(end, a)
    write(path, text[:a] + new + text[b:])


# SSH helper hardening: user validation, symbolic errno, and pre-download size check.
replace_once("src/remote_host_mcp/ssh_helpers.py", "import asyncio\nimport hashlib", "import asyncio\nimport errno\nimport hashlib")
replace_once(
    "src/remote_host_mcp/ssh_helpers.py",
    '_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")\n_REMOTE_PATH_RE',
    '_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")\n_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")\n_REMOTE_PATH_RE',
)
replace_once(
    "src/remote_host_mcp/ssh_helpers.py",
    "def _validate_port(port: int | None) -> int | None:\n",
    "def _validate_user(user: str | None) -> str | None:\n    if user is None:\n        return None\n    value = user.strip()\n    if not _USER_RE.fullmatch(value):\n        raise ValueError(\"user must be a conservative OpenSSH username\")\n    return value\n\n\ndef _validate_port(port: int | None) -> int | None:\n",
)
for function_name in ("ssh_check", "ssh_exec", "ssh_upload", "ssh_download"):
    marker = f"async def {function_name}("
    text = read("src/remote_host_mcp/ssh_helpers.py")
    start = text.index(marker)
    host_line = "    host = _validate_host(host)\n"
    pos = text.index(host_line, start) + len(host_line)
    text = text[:pos] + "    user = _validate_user(user)\n" + text[pos:]
    write("src/remote_host_mcp/ssh_helpers.py", text)
replace_once("src/remote_host_mcp/ssh_helpers.py", "if exc.errno == 17:", "if exc.errno == errno.EEXIST:")

replace_between(
    "src/remote_host_mcp/ssh_helpers.py",
    "async def _remote_sha256(\n",
    "async def _cleanup_remote_temp(\n",
    dedent('''\
    async def _remote_file_info(
        *, host: str, user: str | None, port: int | None, remote_path: str,
        connect_timeout_seconds: int, timeout_ms: int, settings: Settings,
    ) -> tuple[int | None, str | None, _RunOutcome]:
        quoted = shlex.quote(remote_path)
        script = f"set -eu\\nstat -c '%s' -- {quoted}\\nsha256sum -- {quoted} | cut -d ' ' -f1\\n".encode()
        outcome = await _run_ssh_script(
            host=host,
            user=user,
            port=port,
            connect_timeout_seconds=connect_timeout_seconds,
            timeout_ms=timeout_ms,
            script=script,
            settings=settings,
        )
        lines = [line.strip() for line in outcome.stdout.splitlines() if line.strip()]
        if outcome.returncode != 0 or len(lines) < 2 or not lines[0].isdigit():
            return None, None, outcome
        size = int(lines[0])
        digest = lines[1].lower()
        if not _SHA256_RE.fullmatch(digest):
            return None, None, outcome
        return size, digest, outcome


    '''),
)
replace_once(
    "src/remote_host_mcp/ssh_helpers.py",
    "        remote_digest, hash_outcome = await _remote_sha256(\n",
    "        remote_size, remote_digest, hash_outcome = await _remote_file_info(\n",
)
replace_once(
    "src/remote_host_mcp/ssh_helpers.py",
    "        if hash_outcome.returncode != 0 or remote_digest != digest:\n",
    "        if hash_outcome.returncode != 0 or remote_size != size or remote_digest != digest:\n",
)
replace_once(
    "src/remote_host_mcp/ssh_helpers.py",
    "        remote_digest, hash_outcome = await _remote_sha256(\n",
    "        remote_size, remote_digest, hash_outcome = await _remote_file_info(\n",
)
replace_once(
    "src/remote_host_mcp/ssh_helpers.py",
    "        if hash_outcome.returncode != 0 or remote_digest is None:\n",
    "        if hash_outcome.returncode != 0 or remote_size is None or remote_digest is None:\n",
)
replace_once(
    "src/remote_host_mcp/ssh_helpers.py",
    "        scp = _client_binary(\"scp\")\n",
    "        if remote_size > settings.max_transfer_bytes:\n            raise ValueError(f\"Remote file exceeds transfer limit {settings.max_transfer_bytes}\")\n        scp = _client_binary(\"scp\")\n",
)

# Typed SSH results.
replace_once(
    "src/remote_host_mcp/models.py",
    "class SystemInfoResult(BaseModel):\n",
    dedent('''\
    class SshCheckResult(BaseModel):
        success: bool
        host: str
        port: int | None = Field(default=None, ge=1, le=65535)
        user: str | None = None
        duration_ms: int = Field(ge=0)
        error: ToolErrorInfo | None = None


    class SshExecResult(BaseModel):
        success: bool
        host: str
        port: int | None = Field(default=None, ge=1, le=65535)
        user: str | None = None
        exit_code: int | None = None
        stdout: str = ""
        stderr: str = ""
        duration_ms: int = Field(ge=0)
        timed_out: bool = False
        terminated_by: str | None = None
        truncated: bool = False
        output_bytes_returned: int = Field(default=0, ge=0)
        output_bytes_total: int = Field(default=0, ge=0)
        error: ToolErrorInfo | None = None


    class SshTransferResult(BaseModel):
        success: bool
        direction: str
        host: str
        port: int | None = Field(default=None, ge=1, le=65535)
        user: str | None = None
        local_path: str
        remote_path: str
        bytes_transferred: int = Field(ge=0)
        sha256: str | None = None
        duration_ms: int = Field(ge=0)
        replaced: bool = False
        error: ToolErrorInfo | None = None


    class SystemInfoResult(BaseModel):
    '''),
)

# Register artifact + SSH tools.
replace_once(
    "src/remote_host_mcp/host_server.py",
    "from mcp.types import ToolAnnotations\n",
    "from mcp.types import EmbeddedResource, ImageContent, TextContent, ToolAnnotations\n",
)
replace_once(
    "src/remote_host_mcp/host_server.py",
    "    ServiceStatusResult,\n    SystemInfoResult,\n",
    "    ServiceStatusResult,\n    SshCheckResult,\n    SshExecResult,\n    SshTransferResult,\n    SystemInfoResult,\n",
)
replace_once(
    "src/remote_host_mcp/host_server.py",
    "from .tasks_extension import DurableJobsTasksExtension\n",
    "from .artifact_helpers import file_artifact as file_artifact_impl\nfrom .ssh_helpers import (\n    ssh_check as ssh_check_impl,\n    ssh_download as ssh_download_impl,\n    ssh_exec as ssh_exec_impl,\n    ssh_upload as ssh_upload_impl,\n)\nfrom .tasks_extension import DurableJobsTasksExtension\n",
)
tool_block = dedent('''\
        # ------------------------- Artifacts / SSH -------------------------
        @mcp.tool(
            title="Return host file to the MCP client",
            annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
            structured_output=False,
        )
        async def file_artifact(
            path: Annotated[str, Field(description="Absolute regular-file path inside RHMCP_ALLOWED_ROOTS.")],
            max_bytes: Annotated[int, Field(ge=1, le=8388608, description="Maximum raw bytes returned inline. Larger files should use download_info/download_chunk.")] = 4194304,
        ) -> list[TextContent | ImageContent | EmbeddedResource]:
            """Return raster images as MCP ImageContent and other small binary files as embedded resources."""
            return file_artifact_impl(path, max_bytes, settings)

        @mcp.tool(
            title="Check preconfigured SSH target",
            annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True),
            structured_output=True,
        )
        async def ssh_check(
            host: Annotated[str, Field(min_length=1, max_length=253, description="SSH config alias, DNS hostname, or IP address. Host keys and credentials must already be provisioned on the MCP host.")],
            user: Annotated[str | None, Field(description="Optional SSH username; normally omit when the SSH config alias already supplies User.")] = None,
            port: Annotated[int | None, Field(ge=1, le=65535, description="Optional SSH port; normally omit when the SSH config alias already supplies Port.")] = None,
            connect_timeout_seconds: Annotated[int, Field(ge=1, le=30, description="TCP/SSH connection timeout.")] = 10,
        ) -> SshCheckResult:
            """Verify a strict, noninteractive OpenSSH connection. Password prompts and unknown host keys fail closed."""
            return await ssh_check_impl(host, user, port, connect_timeout_seconds, settings)

        @mcp.tool(
            title="Run command through strict OpenSSH",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
            structured_output=True,
        )
        async def ssh_exec(
            host: Annotated[str, Field(min_length=1, max_length=253, description="Preconfigured SSH target alias, DNS hostname, or IP address.")],
            command: Annotated[str, Field(min_length=1, description="Remote shell command. Sent over SSH stdin so command text is not placed in the local ssh process argv.")],
            user: Annotated[str | None, Field(description="Optional SSH username.")] = None,
            port: Annotated[int | None, Field(ge=1, le=65535, description="Optional SSH port.")] = None,
            connect_timeout_seconds: Annotated[int, Field(ge=1, le=30, description="SSH connection timeout.")] = 10,
            timeout_ms: Annotated[int | None, Field(description="Remote command deadline, bounded by the synchronous execution limit.")] = None,
        ) -> SshExecResult:
            """Execute once via system OpenSSH using BatchMode and strict host-key checking; no password/key material is accepted by this tool."""
            return await ssh_exec_impl(host, user, port, command, connect_timeout_seconds, timeout_ms, settings)

        @mcp.tool(
            title="Upload allowed local file over strict SCP",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
            structured_output=True,
        )
        async def ssh_upload(
            host: Annotated[str, Field(min_length=1, max_length=253, description="Preconfigured SSH target.")],
            local_path: Annotated[str, Field(description="Regular source file inside RHMCP_ALLOWED_ROOTS.")],
            remote_path: Annotated[str, Field(description="Absolute conservative POSIX destination path on the SSH target.")],
            user: Annotated[str | None, Field(description="Optional SSH username.")] = None,
            port: Annotated[int | None, Field(ge=1, le=65535, description="Optional SSH port.")] = None,
            connect_timeout_seconds: Annotated[int, Field(ge=1, le=30, description="SSH connection timeout.")] = 10,
            timeout_ms: Annotated[int, Field(ge=1000, le=3600000, description="Transfer deadline.")] = 300000,
            overwrite: Annotated[bool, Field(description="Permit atomic replacement of an existing remote destination.")] = False,
        ) -> SshTransferResult:
            """Freeze the local source, SCP to remote staging, publish, then verify SHA-256."""
            return await ssh_upload_impl(host, user, port, local_path, remote_path, connect_timeout_seconds, timeout_ms, overwrite, settings)

        @mcp.tool(
            title="Download remote file over strict SCP",
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
            structured_output=True,
        )
        async def ssh_download(
            host: Annotated[str, Field(min_length=1, max_length=253, description="Preconfigured SSH target.")],
            remote_path: Annotated[str, Field(description="Absolute conservative POSIX source path on the SSH target.")],
            local_path: Annotated[str, Field(description="Final destination inside RHMCP_ALLOWED_ROOTS.")],
            user: Annotated[str | None, Field(description="Optional SSH username.")] = None,
            port: Annotated[int | None, Field(ge=1, le=65535, description="Optional SSH port.")] = None,
            connect_timeout_seconds: Annotated[int, Field(ge=1, le=30, description="SSH connection timeout.")] = 10,
            timeout_ms: Annotated[int, Field(ge=1000, le=3600000, description="Transfer deadline.")] = 300000,
            overwrite: Annotated[bool, Field(description="Permit atomic replacement of an existing local regular file.")] = False,
            mode: Annotated[int, Field(ge=0, le=511, description="Final local POSIX permission bits.")] = 420,
        ) -> SshTransferResult:
            """Check remote size/hash, SCP to private staging, verify SHA-256, then publish inside allowed roots."""
            return await ssh_download_impl(host, user, port, remote_path, local_path, connect_timeout_seconds, timeout_ms, overwrite, mode, settings)

    ''')
# dedent to column zero, then restore the function body's four-space indentation.
tool_block = "".join(("    " + line if line.strip() else line) for line in tool_block.splitlines(keepends=True))
replace_once(
    "src/remote_host_mcp/host_server.py",
    "    # ------------------------- Process / service / system -------------------------\n",
    tool_block + "    # ------------------------- Process / service / system -------------------------\n",
)

# Exact canonical tool manifest: 43 existing + 5 new tools.
manifest_path = ROOT / "tests" / "tool_manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
for name in ("file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download"):
    if name in manifest:
        raise RuntimeError(f"duplicate manifest tool: {name}")
insert_at = manifest.index("exec")
manifest[insert_at:insert_at] = ["file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download"]
if len(manifest) != 48:
    raise RuntimeError(f"unexpected tool count: {len(manifest)}")
write("tests/tool_manifest.json", json.dumps(manifest, indent=2) + "\n")

# Alpha3 release identity.
replace_once("pyproject.toml", 'version = "0.2.0a2"', 'version = "0.2.0a3"')
replace_once("src/remote_host_mcp/__init__.py", '__version__ = "0.2.0a2"', '__version__ = "0.2.0a3"')
write("VERSION", "0.2.0-alpha.3\n")
for test_path in (ROOT / "tests").glob("*.py"):
    text = test_path.read_text(encoding="utf-8")
    text = text.replace("0.2.0a2", "0.2.0a3").replace("0.2.0-alpha.2", "0.2.0-alpha.3")
    test_path.write_text(text, encoding="utf-8")

validation = read(".github/workflows/validation.yml")
validation = validation.replace("0.2.0a2", "0.2.0a3").replace("0.2.0-alpha.2", "0.2.0-alpha.3")
validation = validation.replace("43-tool surface", "48-tool surface")
validation = validation.replace("assert len(tools) == 43, len(tools)", "assert len(tools) == 48, len(tools)")
validation = validation.replace(
    'required={"status","system_info","job_run","job_start","terminal_open","process_info","process_signal","upload_begin","service_action"}',
    'required={"status","system_info","job_run","job_start","terminal_open","process_info","process_signal","upload_begin","service_action","file_artifact","ssh_check","ssh_exec","ssh_upload","ssh_download"}',
)
write(".github/workflows/validation.yml", validation)

# Product copy and installer diagnostics.
readme = read("README.md")
readme = readme.replace("**Version: `0.2.0-alpha.1`** (`0.2.0a1` in Python packaging)", "**Version: `0.2.0-alpha.3`** (`0.2.0a3` in Python packaging)", 1)
readme = readme.replace(
    "- chunked/resumable upload and download primitives\n",
    "- chunked/resumable upload and download primitives\n- direct MCP image/binary artifact return for small allowed files\n- strict preconfigured OpenSSH command and SCP transfer tools\n",
    1,
)
write("README.md", readme)
replace_once(
    "src/remote_host_mcp/branding.py",
    '    "with shell, PTY, filesystem, transfer, durable jobs, process and service tools."\n',
    '    "with shell, PTY, filesystem, transfer, artifacts, SSH, durable jobs, process and service tools."\n',
)
replace_once(
    "installer/install.sh",
    "  command_exists tar && ok 'tar' || die 'tar is required / 需要 tar'\n",
    "  command_exists tar && ok 'tar' || die 'tar is required / 需要 tar'\n  command_exists ssh && ok 'OpenSSH client' || warn 'ssh client not found; ssh_* tools will fail closed until OpenSSH is installed'\n  command_exists scp && ok 'SCP client' || warn 'scp client not found; ssh_upload/ssh_download will fail closed until OpenSSH is installed'\n",
)

write(
    "docs/ALPHA3_FEATURE_SCOPE.md",
    dedent('''\
    # Remote Host MCP 0.2.0-alpha.3 feature scope

    Alpha3 adds five canonical tools on top of the alpha2 43-tool surface: `file_artifact`, `ssh_check`, `ssh_exec`, `ssh_upload`, and `ssh_download`.

    `file_artifact` returns PNG/JPEG/GIF/WebP files as native MCP `ImageContent` and other small binary files as embedded resources. Large files continue to use `download_info` / `download_chunk`.

    Graphical screenshots intentionally remain a two-step workflow: use the existing `exec` tool to invoke a screenshot utility already configured on the host and save the image inside an allowed root, then call `file_artifact` to return that PNG/JPEG directly to the MCP client. Remote Host MCP does not read desktop-session credential files or Xauthority material on the model's behalf.

    SSH is deliberately noninteractive and credential-minimizing. The operator pre-provisions OpenSSH config, host keys, agent or IdentityFile settings outside the MCP tool arguments. The MCP tools accept no password, private-key contents, identity-file path, or known-hosts path. They force BatchMode, disable password/keyboard-interactive authentication, and require strict host-key checking. Arbitrary remote command text is sent over SSH stdin rather than placed in the local `ssh` argv.

    Production deployment remains deferred until the feature branch passes permanent CI and a separate controlled ModelScope DSW validation.
    '''),
)
write(
    "docs/SSH_AND_ARTIFACTS.md",
    dedent('''\
    # SSH and direct MCP artifacts

    ## Direct file/image return

    `file_artifact(path, max_bytes)` reads one regular file inside `RHMCP_ALLOWED_ROOTS`. PNG, JPEG, GIF and WebP are returned as MCP `ImageContent`; other small binary files are returned as an embedded blob resource. The default inline budget is 4 MiB and the hard ceiling is 8 MiB. Larger objects use the existing resumable/chunked download tools.

    A GUI screenshot is intentionally composed from existing primitives rather than introducing desktop credential handling:

    1. `exec` invokes a screenshot utility already installed/configured for the active graphical session and writes a PNG inside an allowed root.
    2. `file_artifact` returns that PNG as native MCP image content.
    3. The temporary PNG can be removed with `remove_path` after the client has received it.

    This keeps DISPLAY/Xauthority/session setup an operator/host concern while still allowing a Chat client to receive and render the resulting image.

    ## Strict OpenSSH client tools

    - `ssh_check`: verify a preconfigured target.
    - `ssh_exec`: execute one bounded remote shell command.
    - `ssh_upload`: freeze an allowed local source, copy to remote staging, publish, then verify SHA-256.
    - `ssh_download`: check remote size/SHA-256, copy to private local staging, verify, then publish inside allowed roots.

    Security invariants:

    - no passwords, private-key contents, identity-file paths or known-hosts paths are accepted as MCP arguments;
    - system OpenSSH config / ssh-agent / host-key store are provisioned by the operator;
    - `BatchMode=yes`, `PasswordAuthentication=no`, `KbdInteractiveAuthentication=no`, `StrictHostKeyChecking=yes`, `UpdateHostKeys=no`;
    - arbitrary remote command text is fed through SSH stdin to `sh -s --`, not exposed in the local ssh argv;
    - stdout/stderr remain bounded by the normal Remote Host MCP output budget;
    - local file paths remain inside `RHMCP_ALLOWED_ROOTS`; transfer staging lives under the private state directory;
    - SSH/SCP timeout or client cancellation terminates the local process group;
    - file-transfer success requires SHA-256 agreement.

    SSH port forwarding, host-key enrollment, password prompts, interactive key enrollment, and SSH key lifecycle management are intentionally outside this alpha3 tool surface.
    '''),
)

# New focused regression tests.
write(
    "tests/test_remote_artifacts_ssh.py",
    dedent('''\
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
        assert captured["input"] == f"printf {secret}\\n".encode()
        with pytest.raises(ValueError, match="username"):
            await ssh_helpers.ssh_check("build-host", "bad user", None, 5, cfg)


    @pytest.mark.asyncio
    async def test_canonical_surface_has_48_tools_and_no_ssh_credential_arguments(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = settings(monkeypatch, tmp_path)
        expected = json.loads((Path(__file__).parent / "tool_manifest.json").read_text(encoding="utf-8"))
        async with Client(build_server(cfg)) as client:
            listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        assert sorted(tools) == sorted(expected)
        assert len(tools) == 48
        for name in ("file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download"):
            assert name in tools
        forbidden = {"password", "private_key", "identity_file", "known_hosts", "known_hosts_file", "xauthority"}
        for name in ("ssh_check", "ssh_exec", "ssh_upload", "ssh_download"):
            props = set(tools[name].input_schema.get("properties", {}))
            assert not (props & forbidden)
            assert tools[name].input_schema.get("additionalProperties") is False
    '''),
)

# This transformer is one-shot migration scaffolding, not product code.
Path(__file__).unlink(missing_ok=True)
