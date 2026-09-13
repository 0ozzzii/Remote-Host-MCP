from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMON = ROOT / "installer" / "lib" / "common.sh"
STATE = ROOT / "installer" / "lib" / "state.sh"
PORTS = ROOT / "installer" / "lib" / "ports.sh"
UNINSTALL = ROOT / "installer" / "lib" / "uninstall.sh"
LIFECYCLE = ROOT / "installer" / "lib" / "lifecycle.sh"


def run_bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def make_old_python(path: pathlib.Path) -> pathlib.Path:
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then echo 'Python 3.9.2'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def make_fake_private_archive(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    payload = tmp_path / "payload"
    binary = payload / "python" / "bin" / "python3"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then echo 'Python 3.11.16'; exit 0; fi\n"
        "if [ \"${1:-}\" = \"-c\" ]; then\n"
        "  case \"${2:-}\" in *sys.version_info*) echo '3.11.16';; esac\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    archive = tmp_path / "private-python.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload / "python", arcname="python")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def private_env(archive: pathlib.Path, digest: str, old_python: pathlib.Path) -> dict[str, str]:
    return {
        "RHMCP_TESTING": "1",
        "RHMCP_PYTHON_BIN": "",
        "RHMCP_PYTHON_CANDIDATES": str(old_python),
        "RHMCP_PRIVATE_PYTHON_CHOICE": "1",
        "RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE": str(archive),
        "RHMCP_PRIVATE_PYTHON_TEST_SHA256": digest,
    }


def test_pin_is_exact_and_not_latest() -> None:
    text = (ROOT / "installer" / "lib" / "private_python.sh").read_text(encoding="utf-8")
    assert "RHMCP_PRIVATE_PYTHON_VERSION='3.11.16'" in text
    assert "RHMCP_PRIVATE_PYTHON_RELEASE='20260901'" in text
    assert "64427febea27864d136db46c8efe968eb6fa5ca2813ce1dca4bb95aec31cb2e4" in text
    assert "unknown-linux-gnu-install_only_stripped.tar.gz" in text
    assert "latest-release" not in text


def test_supported_system_python_bypasses_private_bootstrap() -> None:
    result = run_bash(
        f"RHMCP_PYTHON_BIN={subprocess.list2cmdline([os.sys.executable])}; source {COMMON}; "
        "require_python; test \"$RHMCP_PRIVATE_PYTHON_ACTIVE\" = false"
    )
    assert result.returncode == 0, result.stderr


def test_explicit_invalid_python_fails_closed_without_private_fallback(tmp_path: pathlib.Path) -> None:
    old = make_old_python(tmp_path / "python39")
    result = run_bash(
        f"RHMCP_PYTHON_BIN={old}; export RHMCP_PYTHON_BIN; source {COMMON}; require_python",
        env={"RHMCP_PRIVATE_PYTHON_CHOICE": "1"},
    )
    assert result.returncode != 0
    assert "Configured Python did not satisfy" in result.stderr
    assert "Install Remote Host MCP private Python" not in result.stdout


def test_python39_only_host_can_select_private_bootstrap(tmp_path: pathlib.Path) -> None:
    archive, digest = make_fake_private_archive(tmp_path)
    old = make_old_python(tmp_path / "python39")
    result = run_bash(
        f"source {COMMON}; require_python; "
        "test \"$RHMCP_PRIVATE_PYTHON_ACTIVE\" = true; "
        "test \"$RHMCP_BASE_MIN_FREE_MB\" -ge 512; test \"$RHMCP_MIN_FREE_MB\" -ge 768; "
        "\"$RHMCP_PYTHON_BIN\" -c 'import sys; print(\".\".join(map(str, sys.version_info[:3])))'",
        env=private_env(archive, digest, old),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith("3.11.16")


def test_checksum_mismatch_fails_closed(tmp_path: pathlib.Path) -> None:
    archive, _ = make_fake_private_archive(tmp_path)
    old = make_old_python(tmp_path / "python39")
    env = private_env(archive, "0" * 64, old)
    result = run_bash(f"source {COMMON}; require_python", env=env)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr


def test_missing_test_archive_simulates_download_interruption(tmp_path: pathlib.Path) -> None:
    old = make_old_python(tmp_path / "python39")
    missing = tmp_path / "missing.tar.gz"
    env = private_env(missing, "0" * 64, old)
    result = run_bash(f"source {COMMON}; require_python", env=env)
    assert result.returncode != 0


def test_publish_refuses_foreign_destination(tmp_path: pathlib.Path) -> None:
    archive, digest = make_fake_private_archive(tmp_path)
    old = make_old_python(tmp_path / "python39")
    runtime = tmp_path / "runtime"
    foreign = runtime / "private-python-3.11.16"
    foreign.mkdir(parents=True)
    (foreign / "foreign.txt").write_text("keep", encoding="utf-8")
    script = (
        f"source {COMMON}; require_python; RUNTIME_DIR={runtime}; export RUNTIME_DIR; "
        "if private_python_maybe_publish; then exit 9; fi; test -f \"$RUNTIME_DIR/private-python-3.11.16/foreign.txt\""
    )
    result = run_bash(script, env=private_env(archive, digest, old))
    assert result.returncode == 0, result.stderr
    assert "Refusing to replace unowned" in result.stderr


def test_publish_records_created_ownership(tmp_path: pathlib.Path) -> None:
    archive, digest = make_fake_private_archive(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ownership = tmp_path / "ownership.env"
    script = (
        f"source {COMMON}; source {STATE}; RUNTIME_DIR={runtime}; OWNERSHIP_STATE={ownership}; "
        "export RUNTIME_DIR OWNERSHIP_STATE; _private_python_download_archive; "
        "_private_python_publish_archive \"$RHMCP_PRIVATE_PYTHON_ARCHIVE\"; "
        "test \"$(resource_value private_python OWNERSHIP)\" = created; "
        "test \"$(resource_value private_python PATH)\" = \"$RUNTIME_DIR/private-python-3.11.16\""
    )
    result = run_bash(
        script,
        env={
            "RHMCP_TESTING": "1",
            "RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE": str(archive),
            "RHMCP_PRIVATE_PYTHON_TEST_SHA256": digest,
        },
    )
    assert result.returncode == 0, result.stderr


def test_corrupt_owned_runtime_is_transactionally_replaced(tmp_path: pathlib.Path) -> None:
    archive, digest = make_fake_private_archive(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    script = f"""
source {COMMON}
RHMCP_TESTING=1
RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE={archive}
RHMCP_PRIVATE_PYTHON_TEST_SHA256={digest}
export RHMCP_TESTING RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE RHMCP_PRIVATE_PYTHON_TEST_SHA256
RUNTIME_DIR={runtime}; export RUNTIME_DIR
final="$RUNTIME_DIR/private-python-3.11.16"
mkdir -p "$final/python/bin"
_private_python_write_marker "$final"
cat > "$final/python/bin/python3" <<'EOF'
#!/bin/sh
if [ "${{1:-}}" = "-c" ]; then echo 3.11.15; fi
exit 0
EOF
chmod +x "$final/python/bin/python3"
_private_python_download_archive
_private_python_publish_archive "$RHMCP_PRIVATE_PYTHON_ARCHIVE"
_private_python_runtime_healthy "$final"
! find "$RUNTIME_DIR" -maxdepth 1 -name '.private-python-replaced.*' | grep -q .
"""
    result = run_bash(script)
    assert result.returncode == 0, result.stderr


def test_repair_rebuilds_corrupt_owned_runtime(tmp_path: pathlib.Path) -> None:
    archive, digest = make_fake_private_archive(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ownership = tmp_path / "ownership.env"
    final = runtime / "private-python-3.11.16"
    script = f"""
source {COMMON}
source {STATE}
RUNTIME_DIR={runtime}; OWNERSHIP_STATE={ownership}; export RUNTIME_DIR OWNERSHIP_STATE
mkdir -p "{final}/python/bin"
_private_python_write_marker "{final}"
cat > "{final}/python/bin/python3" <<'EOF'
#!/bin/sh
if [ "${{1:-}}" = "-c" ]; then echo 3.11.15; fi
exit 0
EOF
chmod +x "{final}/python/bin/python3"
record_resource private_python "{final}" created
private_python_repair_for_layout
_private_python_runtime_healthy "{final}"
"""
    result = run_bash(
        script,
        env={
            "RHMCP_TESTING": "1",
            "RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE": str(archive),
            "RHMCP_PRIVATE_PYTHON_TEST_SHA256": digest,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "rebuilding it" in result.stdout


def test_resume_reuses_healthy_private_runtime_without_download(tmp_path: pathlib.Path) -> None:
    archive, digest = make_fake_private_archive(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    old = make_old_python(tmp_path / "python39")
    setup = run_bash(
        f"source {COMMON}; RUNTIME_DIR={runtime}; export RUNTIME_DIR; "
        "_private_python_download_archive; _private_python_publish_archive \"$RHMCP_PRIVATE_PYTHON_ARCHIVE\"",
        env={
            "RHMCP_TESTING": "1",
            "RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE": str(archive),
            "RHMCP_PRIVATE_PYTHON_TEST_SHA256": digest,
        },
    )
    assert setup.returncode == 0, setup.stderr
    env = {
        "RHMCP_PYTHON_BIN": "",
        "RHMCP_PYTHON_CANDIDATES": str(old),
        "RHMCP_RUNTIME_DIR_PERSIST": str(runtime),
        "RHMCP_PRIVATE_PYTHON_CHOICE": "3",
    }
    result = run_bash(
        f"source {COMMON}; require_python; test \"$RHMCP_PRIVATE_PYTHON_ACTIVE\" = true; "
        "test \"$RHMCP_PYTHON_BIN\" = \"$RHMCP_RUNTIME_DIR_PERSIST/private-python-3.11.16/python/bin/python3\"",
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_diagnose_reports_private_runtime_health(tmp_path: pathlib.Path) -> None:
    archive, digest = make_fake_private_archive(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    ownership = tmp_path / "ownership.env"
    script = (
        f"source {COMMON}; source {STATE}; RUNTIME_DIR={runtime}; OWNERSHIP_STATE={ownership}; "
        "export RUNTIME_DIR OWNERSHIP_STATE; _private_python_download_archive; "
        "_private_python_publish_archive \"$RHMCP_PRIVATE_PYTHON_ARCHIVE\"; private_python_diagnose"
    )
    result = run_bash(
        script,
        env={
            "RHMCP_TESTING": "1",
            "RHMCP_PRIVATE_PYTHON_TEST_ARCHIVE": str(archive),
            "RHMCP_PRIVATE_PYTHON_TEST_SHA256": digest,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "3.11.16 (healthy, ownership=created)" in result.stdout


def test_lifecycle_wires_private_python_diagnose_and_repair() -> None:
    text = LIFECYCLE.read_text(encoding="utf-8")
    assert "private_python_diagnose" in text
    assert "private_python_repair_for_layout" in text


def test_default_uninstall_removes_product_runtime_including_private_python(tmp_path: pathlib.Path) -> None:
    runtime = tmp_path / "runtime"
    private = runtime / "private-python-3.11.16"
    private.mkdir(parents=True)
    (private / "marker").write_text("owned", encoding="utf-8")
    script = f"""
source {COMMON}
source {UNINSTALL}
CURRENT_LINK={tmp_path / 'current'}
RELEASES_DIR={tmp_path / 'releases'}
STATE_DIR={tmp_path / 'state'}
RUNTIME_DIR={runtime}
INSTALL_MODE=prefix
CODE_BASE={tmp_path / 'code'}
remove_application_payload
test ! -e "$RUNTIME_DIR"
"""
    result = run_bash(script)
    assert result.returncode == 0, result.stderr


def test_purge_stray_check_detects_private_runtime_residue(tmp_path: pathlib.Path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "private-python-3.11.16").mkdir(parents=True)
    script = f"""
source {COMMON}
source {PORTS}
source {UNINSTALL}
UNINSTALL_CHECK_PORT=not-a-port
CURRENT_LINK={tmp_path / 'missing-current'}
RELEASES_DIR={tmp_path / 'missing-releases'}
CONFIG_DIR={tmp_path / 'missing-config'}
SECRET_DIR={tmp_path / 'missing-secrets'}
STATE_DIR={tmp_path / 'missing-state'}
RUNTIME_DIR={runtime}
INSTALL_MODE=system
if stray_check true; then exit 9; fi
"""
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    assert "stray runtime dir" in result.stderr
