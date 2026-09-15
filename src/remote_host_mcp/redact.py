"""Secret redaction for outbound call records (接入方案 §6.4).

Redaction runs **on the VPS, before anything crosses the network**. Shipping raw
command text and output to the Hub and redacting there would mean the plaintext
credential already left the host, which defeats the purpose.

The switch is the Hub-pushed ``redactEnabled`` flag and defaults to on; an
operator has to opt out explicitly. Redaction is best-effort pattern matching,
not a guarantee — it lowers the value of the collected log as a credential
store, it does not make collecting ``cat .env`` output safe.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")

# Key/value assignments, including the JSON form (`"password": "..."`) that
# argsText is serialized into. An opening quote is consumed with the key so the
# value class can exclude quotes and separators, which keeps the surrounding
# JSON punctuation (and therefore valid JSON) intact.
_KEY_VALUE_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|password|secret)[\"']?\s*[=:]\s*[\"']?)"
    r"([^\s,;\"']{8,})"
)

# Capability keys (`gwk_`) and agent keys (`agt_`) are the same family — the Hub
# normalizes `agt_` to `gwk_` before fingerprinting — so both are scrubbed.
_PREFIX_KEY_RE = re.compile(r"\b(?:gwk|agt)_[A-Za-z0-9_-]{20,}")

_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")

_HEX_BLOB_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PEM_RE, "[REDACTED:pem]"),
    (_BEARER_RE, "Bearer [REDACTED]"),
    (_KEY_VALUE_RE, r"\1[REDACTED]"),
    (_PREFIX_KEY_RE, "[REDACTED:gwk]"),
    (_OPENAI_KEY_RE, "[REDACTED:sk]"),
    (_HEX_BLOB_RE, "[REDACTED:hex]"),
)


def redact_text(text: str | None) -> str | None:
    """Apply every redaction rule to one string."""
    if not text:
        return text
    redacted = text
    for pattern, replacement in _RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


# Fields that can carry operator-supplied secrets.
_REDACTABLE_FIELDS = ("argsText", "outputPreview", "outputRef")


def redact_event(event: Mapping[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    """Return a copy of one report event with secrets removed.

    ``enabled=False`` returns the event untouched, which is the explicit
    "不脱敏" opt-out the operator can choose from the Hub.
    """
    payload = dict(event)
    if not enabled:
        return payload
    for field in _REDACTABLE_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            payload[field] = redact_text(value)
    return payload
