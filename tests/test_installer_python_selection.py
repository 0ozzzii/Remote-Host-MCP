from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMON = ROOT / "installer" / "lib" / "common.sh"


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


def test_explicit_supported_python_is_used_for_all_installer_calls() -> None:
    py = shlex.quote(sys.executable)
    common = shlex.quote(str(COMMON))
    result = run_bash(
        f"RHMCP_PYTHON_BIN={py}; source {common}; require_python; "
        "python3 -c 'import sys; assert sys.version_info >= (3, 10)'"
    )
    assert result.returncode == 0, result.stderr


def test_selector_skips_old_python3_in_path_when_supported_system_python_exists(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then echo 'Python 3.9.2'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    common = shlex.quote(str(COMMON))
    result = run_bash(
        f"source {common}; require_python; printf '%s\\n' \"$RHMCP_PYTHON_BIN\"; "
        "python3 -c 'import sys; assert sys.version_info >= (3, 10)'",
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "RHMCP_PYTHON_BIN": ""},
    )
    assert result.returncode == 0, result.stderr
    selected = result.stdout.strip().splitlines()[-1]
    assert pathlib.Path(selected).resolve() != fake.resolve()


def test_python_39_failure_is_clean_and_actionable_without_traceback(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "python3-old"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then echo 'Python 3.9.2'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    common = shlex.quote(str(COMMON))
    result = run_bash(
        f"source {common}; require_python",
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RHMCP_PYTHON_BIN": "",
            "RHMCP_PYTHON_CANDIDATES": str(fake),
        },
    )
    assert result.returncode != 0
    assert "Python >= 3.10" in result.stderr
    assert "NEXT / 下一步" in result.stderr
    assert "Traceback" not in result.stderr
    assert "AssertionError" not in result.stderr
