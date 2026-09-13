from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_bash(script: str, *, env: dict[str, str] | None = None, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_preflight_rejects_missing_venv_before_product_writes(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    python = fake / "python3"
    python.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == -m && \"${2:-}\" == venv ]]; then echo missing-venv >&2; exit 2; fi\n"
        "exec /usr/bin/python3 \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    marker = tmp_path / "product-write"
    result = run_bash(
        f"""
        source installer/lib/common.sh
        if python_venv_preflight; then touch {marker!s}; exit 9; fi
        test ! -e {marker!s}
        """,
        env={"PATH": f"{fake}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_bounded_readiness_accepts_delayed_server(tmp_path: pathlib.Path) -> None:
    port = free_port()
    server = tmp_path / "delayed.py"
    server.write_text(
        textwrap.dedent(
            """
            import http.server, socketserver, sys, time
            time.sleep(2)
            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == '/health':
                        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
                    else:
                        self.send_response(404); self.end_headers()
                def log_message(self, *_): pass
            with socketserver.TCPServer(('127.0.0.1', int(sys.argv[1])), H) as s:
                s.handle_request()
            """
        ),
        encoding="utf-8",
    )
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/readiness.sh
        python3 {server!s} {port} & pid=$!
        trap 'kill "$pid" 2>/dev/null || true' EXIT
        wait_local_health {port} 6 1
        wait "$pid"
        """,
        timeout=12,
    )
    assert result.returncode == 0, result.stderr


def test_resource_registry_tracks_ownership_classes(tmp_path: pathlib.Path) -> None:
    registry = tmp_path / "ownership.env"
    result = run_bash(
        f"""
        die() {{ echo "$*" >&2; exit 1; }}
        source installer/lib/state.sh
        OWNERSHIP_STATE={registry!s}
        record_resource alpha /tmp/a created
        record_resource beta /tmp/b shared
        test "$(resource_value alpha PATH)" = /tmp/a
        test "$(resource_value alpha OWNERSHIP)" = created
        resource_owned alpha
        ! resource_owned beta
        """
    )
    assert result.returncode == 0, result.stderr
    text = registry.read_text(encoding="utf-8")
    assert "RESOURCE_ALPHA_OWNERSHIP=created" in text
    assert "RESOURCE_BETA_OWNERSHIP=shared" in text


def test_release_metadata_freezes_non_null_build_provenance(tmp_path: pathlib.Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/state.sh
        SOURCE_ROOT={ROOT!s}
        RMCP_VERSION=$(cat VERSION)
        RHMCP_REQUESTED_REF=test/ref
        RHMCP_RESOLVED_COMMIT=0123456789abcdef0123456789abcdef01234567
        resolve_build_provenance
        write_release_metadata {release!s}
        test "$REQUESTED_REF" = test/ref
        test "$RESOLVED_COMMIT" = 0123456789abcdef0123456789abcdef01234567
        grep -q '^RHMCP_BUILD_COMMIT=0123456789abcdef0123456789abcdef01234567$' {release!s}/.rhmcp-release.env
        grep -q '^RHMCP_BUILD_REF=test/ref$' {release!s}/.rhmcp-release.env
        """
    )
    assert result.returncode == 0, result.stderr


def test_release_metadata_is_not_affected_by_bash_dynamic_scope(tmp_path: pathlib.Path) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    staging.mkdir()
    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/state.sh
        RMCP_VERSION=0.2.0-alpha.4
        RELEASE_ID=test-release
        RESOLVED_COMMIT=0123456789abcdef0123456789abcdef01234567
        REQUESTED_REF=test/ref
        caller() {{
          local release={final!s}
          write_release_metadata {staging!s}
        }}
        caller
        test -f {staging!s}/.rhmcp-release.env
        test ! -e {final!s}/.rhmcp-release.env
        """
    )
    assert result.returncode == 0, result.stderr


def test_prefix_uninstall_preserves_recovery_then_purge_removes_namespace(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "rhmcp"
    config = base / "config"
    state = base / "state"
    logs = base / "logs"
    secrets = base / "secrets"
    backups = base / "backups"
    runtime = base / "runtime"
    releases = base / "releases"
    release = releases / "v1"
    cli = tmp_path / "rmcp-bin"
    for path in (config, state, logs, secrets, backups, runtime, release):
        path.mkdir(parents=True, exist_ok=True)
    (config / "install-state.env").write_text("RHMCP_INSTALL_STATUS=COMPLETE\n", encoding="utf-8")
    (secrets / "keep.secret").write_text("not-a-real-secret", encoding="utf-8")
    (backups / "keep.backup").write_text("backup", encoding="utf-8")
    (logs / "keep.log").write_text("log", encoding="utf-8")
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    current = base / "current"
    current.symlink_to(release)
    ownership = config / "resource-ownership.env"

    result = run_bash(
        f"""
        source installer/lib/common.sh
        source installer/lib/ports.sh
        source installer/lib/state.sh
        source installer/lib/uninstall.sh
        INSTALL_MODE=prefix
        CODE_BASE={base!s}
        CONFIG_DIR={config!s}
        STATE_DIR={state!s}
        LOG_DIR={logs!s}
        SECRET_DIR={secrets!s}
        BACKUP_DIR={backups!s}
        RUNTIME_DIR={runtime!s}
        RELEASES_DIR={releases!s}
        CURRENT_LINK={current!s}
        INSTALL_STATE={config!s}/install-state.env
        OWNERSHIP_STATE={ownership!s}
        RHMCP_LOCAL_PORT=65534
        record_resource rmcp_cli {cli!s} created
        uninstall_apply false
        test ! -e {releases!s}
        test ! -e {current!s}
        test ! -e {state!s}
        test ! -e {runtime!s}
        test ! -e {cli!s}
        test -e {config!s}
        test -e {secrets!s}/keep.secret
        test -e {backups!s}/keep.backup
        test -e {logs!s}/keep.log
        grep -q '^RHMCP_INSTALL_STATUS=UNINSTALLED$' {config!s}/install-state.env
        uninstall_apply true
        stray_check true
        test ! -e {base!s}
        """,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_external_probe_parsers_handle_nested_results() -> None:
    result = run_bash(
        r"""
        source installer/lib/external_probe.sh
        http='{"node1":[[["ok",0.1,"1.1.1.1",200,"OK"]]],"node2":[["ok",0.2,"2.2.2.2",200,"OK"]],"node3":[["bad",0.1,"3.3.3.3",403,"Forbidden"]]}'
        parsed=$(printf '%s' "$http" | _parse_checkhost_http_result 200)
        test "$parsed" = '2:1:3'
        tcp='{"node1":[[{"time":0.01}]],"node2":[{"time":0.02}],"node3":[{"error":"timeout"}]}'
        parsed=$(printf '%s' "$tcp" | _parse_checkhost_tcp_result)
        test "$parsed" = '2 3'
        """
    )
    assert result.returncode == 0, result.stderr


def test_installer_contract_contains_full_lifecycle_and_secret_hygiene() -> None:
    install = (ROOT / "installer/install.sh").read_text(encoding="utf-8")
    rmcp = (ROOT / "scripts/rmcp.sh").read_text(encoding="utf-8")
    uninstall = (ROOT / "installer/lib/uninstall.sh").read_text(encoding="utf-8")
    tls = (ROOT / "installer/lib/tls.sh").read_text(encoding="utf-8")
    state = (ROOT / "installer/lib/state.sh").read_text(encoding="utf-8")
    validator = (ROOT / "installer/validate_mcp.py").read_text(encoding="utf-8")

    for stage in ("PRECHECK", "PREPARE", "RELEASE", "RUNTIME", "SERVICE", "LOCAL_READY", "INGRESS", "TLS", "PUBLIC_READY", "MCP_VERIFY", "COMPLETE"):
        assert stage in install or stage in state
    for command in ("status", "doctor", "repair", "resume", "uninstall", "stray-check"):
        assert command in rmcp
    assert "--dry-run" in rmcp
    assert "--purge" in rmcp
    assert "created|reused|shared" in state
    assert "nginx_site_link" in uninstall
    assert "cert_renew_timer" in uninstall
    assert "--preferred-profile shortlived" in tls
    assert "--ip-address" in tls
    assert "renew --dry-run" in tls
    assert "RHMCP_VALIDATE_TOOL_COUNT=65" in install
    assert 'EXPECTED_TOOLS = int(os.getenv("RHMCP_VALIDATE_TOOL_COUNT", "65"))' in validator
    forbidden_persisted_fields = (
        "printf 'RHMCP_PATH_KEY=",
        "printf 'RHMCP_VALIDATION_BEARER_TOKEN=",
        "printf 'RHMCP_CF_DNS_TOKEN=",
        "printf 'RHMCP_OAUTH_CLIENT_SECRET=",
    )
    for forbidden in forbidden_persisted_fields:
        assert forbidden not in state
