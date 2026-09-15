from __future__ import annotations

import pytest

from remote_host_mcp.redact import redact_event, redact_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'",
            "curl -H 'Authorization: Bearer [REDACTED]'",
        ),
        ("gwk_" + "a" * 32, "[REDACTED:gwk]"),
        ("agt_" + "b" * 32, "[REDACTED:gwk]"),
        ("sk-" + "c" * 24, "[REDACTED:sk]"),
        ("export API_KEY=hunter2hunter2", "export API_KEY=[REDACTED]"),
        ("token=abcdefghijkl", "token=[REDACTED]"),
        ("password: supersecret1", "password: [REDACTED]"),
        ("SECRET=zzzzzzzzzzzz", "SECRET=[REDACTED]"),
        ("d41d8cd98f00b204e9800998ecf8427e", "[REDACTED:hex]"),
    ],
)
def test_rule_from_the_spec_table_matches(raw: str, expected: str) -> None:
    assert redact_text(raw) == expected


def test_pem_private_key_block_is_collapsed() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdef\n"
        "morebase64materialhere\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert redact_text(f"key file:\n{pem}\nend") == "key file:\n[REDACTED:pem]\nend"


def test_json_shaped_assignment_keeps_valid_json() -> None:
    import json

    raw = json.dumps({"password": "hunter2hunter2", "command": "ls -la"})
    redacted = redact_text(raw)
    parsed = json.loads(redacted)
    assert parsed["password"] == "[REDACTED]"
    assert parsed["command"] == "ls -la"


def test_short_values_are_left_alone() -> None:
    """Below the 8-character floor the rule would eat ordinary words."""
    assert redact_text("token=short") == "token=short"


def test_ordinary_commands_are_untouched() -> None:
    for command in ("ls -la /tmp", "systemctl status nginx", "docker ps --format json"):
        assert redact_text(command) == command


def test_redaction_is_idempotent() -> None:
    once = redact_text("export TOKEN=abcdefghijkl and gwk_" + "x" * 30)
    assert redact_text(once) == once


def test_event_redacts_only_the_free_text_fields() -> None:
    event = {
        "toolName": "exec",
        "clientIp": "203.0.113.7",
        "argsText": '{"command": "cat .env"}',
        "outputPreview": "API_KEY=hunter2hunter2",
        "outputRef": None,
    }
    redacted = redact_event(event)
    assert redacted["argsText"] == '{"command": "cat .env"}'
    assert redacted["outputPreview"] == "API_KEY=[REDACTED]"
    assert redacted["clientIp"] == "203.0.113.7"
    assert redacted["toolName"] == "exec"


def test_event_redaction_can_be_explicitly_disabled() -> None:
    event = {"outputPreview": "API_KEY=hunter2hunter2"}
    assert redact_event(event, enabled=False)["outputPreview"] == "API_KEY=hunter2hunter2"


def test_event_redaction_does_not_mutate_the_input() -> None:
    event = {"outputPreview": "token=abcdefghijkl"}
    redact_event(event)
    assert event["outputPreview"] == "token=abcdefghijkl"


def test_none_passes_through() -> None:
    assert redact_text(None) is None
    assert redact_text("") == ""
