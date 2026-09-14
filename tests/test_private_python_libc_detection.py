from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMON = ROOT / "installer" / "lib" / "common.sh"


def run_bash(script: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def test_positive_glibc_signal_wins_over_installed_musl_loader(tmp_path: pathlib.Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    getconf = tools / "getconf"
    getconf.write_text("#!/bin/sh\necho 'glibc 2.31'\n", encoding="utf-8")
    getconf.chmod(0o755)

    result = run_bash(
        f'''source {COMMON}; _private_python_platform_supported; printf '%s\n' "$RHMCP_PRIVATE_PYTHON_ARTIFACT"''',
        env={
            "RHMCP_TESTING": "1",
            "RHMCP_PRIVATE_PYTHON_TEST_MUSL_LOADER_PRESENT": "1",
            "PATH": f"{tools}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cpython-3.11.16+20260901-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"


def test_musl_loader_remains_last_resort_when_probes_are_inconclusive(tmp_path: pathlib.Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("getconf", "ldd"):
        probe = tools / name
        probe.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        probe.chmod(0o755)

    result = run_bash(
        f'''source {COMMON}; _private_python_platform_supported; printf '%s\n' "$RHMCP_PRIVATE_PYTHON_ARTIFACT"''',
        env={
            "RHMCP_TESTING": "1",
            "RHMCP_PRIVATE_PYTHON_TEST_MUSL_LOADER_PRESENT": "1",
            "PATH": f"{tools}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cpython-3.11.16+20260901-x86_64-unknown-linux-musl_install_only_stripped.tar.gz".replace("musl_", "musl-")
