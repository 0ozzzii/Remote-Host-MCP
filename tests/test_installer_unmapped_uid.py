from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_common_id_wrapper_falls_back_to_numeric_uid(tmp_path: pathlib.Path) -> None:
    fake_id = tmp_path / "id"
    fake_id.write_text(
        "#!/bin/sh\n"
        "case \"${1:-}\" in\n"
        "  -un) echo 'id: cannot find name for user ID 988' >&2; exit 1 ;;\n"
        "  -u) echo 988; exit 0 ;;\n"
        "  *) exec /usr/bin/id \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; id -un',
            "_",
            str(ROOT / "installer/lib/common.sh"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "988"
    assert completed.stderr == ""


def test_common_id_wrapper_preserves_named_current_user(tmp_path: pathlib.Path) -> None:
    fake_id = tmp_path / "id"
    fake_id.write_text(
        "#!/bin/sh\n"
        "case \"${1:-}\" in\n"
        "  -un) echo fielduser; exit 0 ;;\n"
        "  -u) echo 988; exit 0 ;;\n"
        "  *) exec /usr/bin/id \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; id -un',
            "_",
            str(ROOT / "installer/lib/common.sh"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "fielduser"
    assert completed.stderr == ""
