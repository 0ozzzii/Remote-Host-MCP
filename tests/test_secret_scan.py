from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts/secret_scan.py")
assert SPEC and SPEC.loader
secret_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(secret_scan)


def test_detects_private_key_and_provider_token() -> None:
    text = """-----BEGIN OPENSSH PRIVATE KEY-----\ngithub_token=ghp_1234567890abcdefghijklmnopqrstuv\n"""
    findings = secret_scan.scan_text("fixture", text)
    kinds = {item.kind for item in findings}
    assert "private-key" in kinds
    assert "github-token" in kinds


def test_allows_documented_placeholders_and_repeated_ci_fixture() -> None:
    text = """RHMCP_PATH_KEY=REPLACE_WITH_48_PLUS_RANDOM_CHARS\nRHMCP_PATH_KEY=rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr\nCFD_TOKEN=PASTE_TUNNEL_TOKEN_HERE\n"""
    assert secret_scan.scan_text("fixture", text) == []


def test_detects_high_entropy_generic_secret_assignment() -> None:
    findings = secret_scan.scan_text(
        "fixture",
        "client_secret=Qw7pK4vN9xT2mR6yH8cL3sJ5dF1aZ0uB7eG9kM2p\n",
    )
    assert any(item.kind == "high-entropy-secret-assignment" for item in findings)


def test_redaction_does_not_echo_full_candidate() -> None:
    candidate = "Qw7pK4vN9xT2mR6yH8cL3sJ5dF1aZ0uB7eG9kM2p"
    preview = secret_scan.redact(candidate)
    assert candidate not in preview
    assert "…" in preview
