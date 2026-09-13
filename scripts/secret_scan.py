#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAX_BLOB_BYTES = 2 * 1024 * 1024

PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("openai-style-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
)

GENERIC_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|private[_-]?key|secret|token|path[_-]?key)\b"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{24,})"
)

SAFE_MARKERS = (
    "replace_",
    "replace-with",
    "paste_",
    "paste-",
    "example",
    "dummy",
    "placeholder",
    "your_",
    "your-",
    "not-a-real",
    "not_real",
)

SAFE_PATH_PREFIXES = (
    ".git/",
    ".venv/",
    "backups/",
    "logs/",
    "secrets/",
    ".runtime/",
)


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    kind: str
    preview: str


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {ch: value.count(ch) for ch in set(value)}
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def looks_like_fixture(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in SAFE_MARKERS):
        return True
    if len(set(value)) <= 2:
        return True
    if value.startswith("${") or value.startswith("$("):
        return True
    return False


def redact(value: str) -> str:
    value = value.strip()
    if len(value) <= 12:
        return "[REDACTED]"
    return f"{value[:4]}…{value[-4:]}"


def scan_text(source: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PROVIDER_PATTERNS:
            match = pattern.search(line)
            if match:
                candidate = match.group(0)
                if not looks_like_fixture(candidate):
                    findings.append(Finding(source, lineno, kind, redact(candidate)))

        for match in GENERIC_ASSIGNMENT.finditer(line):
            candidate = match.group(1)
            if looks_like_fixture(candidate):
                continue
            if len(candidate) >= 32 and shannon_entropy(candidate) >= 3.8:
                findings.append(Finding(source, lineno, "high-entropy-secret-assignment", redact(candidate)))
    return findings


def tracked_files(root: Path) -> Iterable[tuple[str, bytes]]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    for raw_name in proc.stdout.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", "replace")
        if name.startswith(SAFE_PATH_PREFIXES):
            continue
        path = root / name
        try:
            if not path.is_file() or path.stat().st_size > MAX_BLOB_BYTES:
                continue
            yield name, path.read_bytes()
        except OSError:
            continue


def history_blobs(root: Path) -> Iterable[tuple[str, bytes]]:
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    seen: set[str] = set()
    for entry in objects:
        oid, _, path = entry.partition(" ")
        if not path or oid in seen or path.startswith(SAFE_PATH_PREFIXES):
            continue
        seen.add(oid)
        obj_type = subprocess.run(
            ["git", "cat-file", "-t", oid],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.strip()
        if obj_type != "blob":
            continue
        size_raw = subprocess.run(
            ["git", "cat-file", "-s", oid],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.strip()
        if not size_raw.isdigit() or int(size_raw) > MAX_BLOB_BYTES:
            continue
        blob = subprocess.run(
            ["git", "cat-file", "blob", oid],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        yield f"history:{oid[:12]}:{path}", blob


def decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def run_scan(root: Path, include_history: bool) -> list[Finding]:
    findings: list[Finding] = []
    sources: list[Iterable[tuple[str, bytes]]] = [tracked_files(root)]
    if include_history:
        sources.append(history_blobs(root))
    for source_group in sources:
        for name, payload in source_group:
            text = decode_text(payload)
            if text is None:
                continue
            findings.extend(scan_text(name, text))
    unique: dict[tuple[str, int, str], Finding] = {}
    for finding in findings:
        unique[(finding.source, finding.line, finding.kind)] = finding
    return sorted(unique.values(), key=lambda item: (item.source, item.line, item.kind))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan tracked files and optionally Git history for likely committed secrets.")
    parser.add_argument("--git-history", action="store_true", help="scan all reachable Git blobs in addition to the current tree")
    parser.add_argument("--root", default=".", help="repository root")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        print(f"error: {root} is not a Git working tree", file=sys.stderr)
        return 2

    findings = run_scan(root, args.git_history)
    if findings:
        print("Potential committed secrets detected:", file=sys.stderr)
        for item in findings:
            print(f"  {item.source}:{item.line}: {item.kind}: {item.preview}", file=sys.stderr)
        print("Rotate/revoke any real credential before rewriting history. Do not add live values to allowlists.", file=sys.stderr)
        return 1

    scope = "current tree + Git history" if args.git_history else "current tree"
    print(f"Secret scan passed ({scope}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
