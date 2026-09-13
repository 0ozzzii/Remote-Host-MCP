from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NEW_TOOLS = [
    "host_capabilities",
    "wait_condition",
    "artifact_info",
    "artifact_preview",
    "artifact_bundle",
    "exec_argv",
    "snapshot_create",
    "snapshot_list",
    "snapshot_restore",
    "snapshot_delete",
    "lease_acquire",
    "lease_status",
    "lease_list",
    "lease_release",
    "inspect_paths",
    "file_diff",
    "apply_patch",
]
READ_ONLY_NEW = [
    "host_capabilities",
    "wait_condition",
    "artifact_info",
    "artifact_preview",
    "snapshot_list",
    "lease_status",
    "lease_list",
    "inspect_paths",
    "file_diff",
]
HIGH_RISK_NEW = ["exec_argv", "snapshot_restore", "snapshot_delete", "apply_patch"]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count < 1:
        raise RuntimeError(f"{path}: replacement anchor not found: {old!r}")
    write(path, text.replace(old, new, 1))


replace_once("VERSION", "0.2.0-alpha.3\n", "0.2.0-alpha.4\n")
replace_once("src/remote_host_mcp/__init__.py", '__version__ = "0.2.0a3"', '__version__ = "0.2.0a4"')
replace_once("pyproject.toml", 'version = "0.2.0a3"', 'version = "0.2.0a4"')

replace_once(
    "src/remote_host_mcp/host_server.py",
    "from .artifact_helpers import file_artifact as file_artifact_impl\n",
    "from .artifact_helpers import file_artifact as file_artifact_impl\nfrom .agent_surface import register_agent_ops\n",
)
replace_once(
    "src/remote_host_mcp/host_server.py",
    "    # ------------------------- Process / service / system -------------------------\n",
    "    # ------------------------- Agent-native planning / safety -------------------------\n    register_agent_ops(mcp, settings)\n\n    # ------------------------- Process / service / system -------------------------\n",
)

manifest_path = ROOT / "tests/tool_manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if len(manifest) != 48 or "ssh_download" not in manifest or any(name in manifest for name in NEW_TOOLS):
    raise RuntimeError(f"unexpected pre-transform tool manifest: count={len(manifest)}")
index = manifest.index("ssh_download") + 1
manifest[index:index] = NEW_TOOLS
if len(manifest) != 65 or len(set(manifest)) != 65:
    raise RuntimeError("transformed tool manifest is not exactly 65 unique tools")
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

openai_path = "tests/test_openai_mcp_contract.py"
expected_insert = "".join(f'    "{name}",\n' for name in NEW_TOOLS)
replace_once(
    openai_path,
    '    "ssh_download",\n    "exec",\n',
    '    "ssh_download",\n' + expected_insert + '    "exec",\n',
)
readonly_insert = "".join(f'    "{name}",\n' for name in READ_ONLY_NEW)
replace_once(
    openai_path,
    '    "ssh_check",\n    "job_status",\n',
    '    "ssh_check",\n' + readonly_insert + '    "job_status",\n',
)
replace_once(
    openai_path,
    'OPEN_WORLD_TOOLS = {"exec", "job_run", "job_start", "terminal_exec", "terminal_write", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download"}',
    'OPEN_WORLD_TOOLS = {"exec", "exec_argv", "job_run", "job_start", "terminal_exec", "terminal_write", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download"}',
)
high_risk_insert = "".join(f'    "{name}",\n' for name in HIGH_RISK_NEW)
replace_once(
    openai_path,
    '    "ssh_download",\n    "exec",\n',
    '    "ssh_download",\n' + high_risk_insert + '    "exec",\n',
)

mature_insert = ", ".join(f'"{name}"' for name in NEW_TOOLS)
replace_once(
    "tests/test_mature.py",
    '            "download_info", "download_chunk", "file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download", "exec",\n',
    '            "download_info", "download_chunk", "file_artifact", "ssh_check", "ssh_exec", "ssh_upload", "ssh_download",\n'
    f'            {mature_insert}, "exec",\n',
)
for path in ("tests/test_audit_closure.py", "tests/test_rhmcp_branding.py"):
    text = read(path)
    if "== 48" not in text:
        raise RuntimeError(f"{path}: missing alpha3 48-tool count assertion")
    write(path, text.replace("== 48", "== 65"))

replace_once(
    "README.md",
    "**Version: `0.2.0-alpha.3`** (`0.2.0a3` in Python packaging)",
    "**Version: `0.2.0-alpha.4`** (`0.2.0a4` in Python packaging)",
)
replace_once(
    "README.md",
    "- direct MCP image/binary artifact return for small allowed files\n- strict preconfigured OpenSSH command and SCP transfer tools\n",
    "- direct MCP image/binary artifact return for small allowed files\n"
    "- artifact metadata/preview/bounded ZIP bundling for Agent result handoff\n"
    "- Agent capability discovery and bounded condition waits\n"
    "- shell-free structured argv execution with minimal non-secret environment inheritance\n"
    "- scoped persistent rollback snapshots and TTL coordination leases\n"
    "- bounded multi-path inspection plus SHA-guarded structured text patching\n"
    "- strict preconfigured OpenSSH command and SCP transfer tools\n",
)

doc = ROOT / "docs/AGENT_NATIVE_OPS.md"
if doc.exists():
    raise RuntimeError("docs/AGENT_NATIVE_OPS.md already exists unexpectedly")
doc.write_text(
    "# Agent-native operations (0.2.0-alpha.4)\n\n"
    "Remote Host MCP 0.2.0-alpha.4 adds a bounded Agent-oriented layer on top of the existing shell, filesystem, durable jobs, PTY, process/service, artifact, and SSH surfaces. The goal is fewer speculative calls, safer mutations, and better result handoff rather than SSH feature parity.\n\n"
    "## New tools\n\n"
    "- `host_capabilities`: secret-free capability matrix for planning.\n"
    "- `wait_condition`: bounded waits for file/job/process/loopback-port/log conditions.\n"
    "- `artifact_info`, `artifact_preview`, `artifact_bundle`: metadata, bounded text preview, and no-clobber ZIP bundling.\n"
    "- `exec_argv`: direct argv execution without shell parsing, using a minimal child environment plus an explicit non-secret allowlist.\n"
    "- `snapshot_create`, `snapshot_list`, `snapshot_restore`, `snapshot_delete`: persistent scoped file rollback points.\n"
    "- `lease_acquire`, `lease_status`, `lease_list`, `lease_release`: TTL coordination leases for multi-Agent work.\n"
    "- `inspect_paths`: at most 20 explicit paths with bounded metadata/hash/text preview; no host-wide recursion.\n"
    "- `file_diff`, `apply_patch`: bounded unified diff and SHA-guarded line edits with atomic final publication.\n\n"
    "## Safety boundaries\n\n"
    "- All path-bearing tools remain constrained by `RHMCP_ALLOWED_ROOTS`; snapshots are internal to `RHMCP_STATE_DIR`.\n"
    "- Snapshot creation rejects symlinks and special files and is bounded by path count, node count, and total bytes. Restore is atomic per captured top-level path; it is not a single cross-path transaction.\n"
    "- `wait_condition(port_open)` is loopback-only and therefore is not a remote port scanner. Log matching is literal and bounded.\n"
    "- `exec_argv` never invokes a shell. It caps argv/stdin/output/time and rejects secret-like environment names from explicit inheritance.\n"
    "- Leases automatically expire and are coordination primitives, not authorization controls.\n"
    "- Batch inspection never recursively walks directories. Hashing and previews have explicit per-file budgets.\n"
    "- `apply_patch` requires the previously observed SHA-256; a concurrent edit causes a fail-closed mismatch.\n"
    "- Artifact bundles reject symlinks/special files and never overwrite an existing destination.\n\n"
    "These tools supplement rather than replace the lower-level primitives. Agents should prefer `exec_argv` over shell `exec` when shell syntax is unnecessary, use snapshots before multi-file risky edits, and use durable jobs for work that may exceed the synchronous request deadline.\n",
    encoding="utf-8",
)

Path(__file__).unlink(missing_ok=True)
