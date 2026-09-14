from __future__ import annotations

import os
import re
from pathlib import Path

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def _valid_commit(value: str | None) -> str | None:
    value = (value or "").strip()
    return value.lower() if _COMMIT_RE.fullmatch(value) else None


def _valid_ref(value: str | None) -> str | None:
    value = (value or "").strip()
    if not _REF_RE.fullmatch(value) or ".." in value:
        return None
    return value


def _git_dir() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    candidate = root / ".git"
    return candidate if candidate.is_dir() else None


def _read_git() -> tuple[str | None, str | None]:
    git = _git_dir()
    if git is None:
        return None, None
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not head.startswith("ref: "):
        return _valid_commit(head), None
    ref = _valid_ref(head[5:].strip())
    if ref is None:
        return None, None
    ref_path = git / ref
    try:
        commit = _valid_commit(ref_path.read_text(encoding="utf-8").strip())
    except OSError:
        commit = None
    if commit is None:
        try:
            packed = (git / "packed-refs").read_text(encoding="utf-8")
        except OSError:
            packed = ""
        for line in packed.splitlines():
            if line.startswith("#") or line.startswith("^"):
                continue
            fields = line.split(" ", 1)
            if len(fields) == 2 and fields[1] == ref:
                commit = _valid_commit(fields[0])
                break
    return commit, ref


def get_build_provenance() -> tuple[str | None, str | None]:
    commit = _valid_commit(os.getenv("RHMCP_BUILD_COMMIT") or os.getenv("DSW_MCP_BUILD_COMMIT"))
    ref = _valid_ref(os.getenv("RHMCP_BUILD_REF") or os.getenv("DSW_MCP_BUILD_REF"))
    git_commit, git_ref = _read_git()
    return commit or git_commit, ref or git_ref
