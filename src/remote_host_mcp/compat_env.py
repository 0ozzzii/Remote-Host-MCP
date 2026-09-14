"""Environment compatibility for Remote Host MCP.

RHMCP_* is the public generic namespace. Existing DSWD deployments may keep
DSW_MCP_* during migration; explicit legacy values win if both are set.
"""

from __future__ import annotations

import os

from .config import ConfigError

_SUFFIXES = (
    "AUTH_MODE",
    "PATH_KEY",
    "PUBLIC_HOST",
    "BIND_HOST",
    "PORT",
    "DEFAULT_TIMEOUT_MS",
    "MAX_TIMEOUT_MS",
    "MAX_COMMAND_BYTES",
    "MAX_OUTPUT_BYTES",
    "KILL_GRACE_MS",
    "ALLOWED_ROOTS",
    "STATE_DIR",
    "MAX_FILE_CHUNK_BYTES",
    "MAX_TRANSFER_BYTES",
    "UPLOAD_TTL_SECONDS",
    "MAX_JOBS",
    "JOB_MAX_TIMEOUT_MS",
    "JOB_LOG_MAX_BYTES",
    "JOB_READ_MAX_BYTES",
    "JOB_RETENTION_SECONDS",
    "JOB_HEARTBEAT_SECONDS",
    "TERMINAL_MAX_SESSIONS",
    "TERMINAL_BUFFER_BYTES",
    "TERMINAL_READ_MAX_BYTES",
    "TERMINAL_IDLE_SECONDS",
    "TERMINAL_MAX_LIFETIME_SECONDS",
    "TERMINAL_WAIT_MAX_MS",
    "TERMINAL_OSC133",
    "TASKS_EXTENSION",
    "OAUTH_ISSUER",
    "OAUTH_JWKS_URL",
    "OAUTH_AUDIENCE",
    "OAUTH_SCOPES",
    "OAUTH_ALGORITHMS",
    "JSON_RESPONSE",
    "STATELESS_HTTP",
    "MAX_REQUEST_BODY_BYTES",
)


def env_namespace_summary() -> dict[str, str]:
    """Report only namespace provenance, never configuration values."""
    summary: dict[str, str] = {}
    for suffix in _SUFFIXES:
        generic = f"RHMCP_{suffix}"
        legacy = f"DSW_MCP_{suffix}"
        generic_value = os.environ.get(generic)
        legacy_value = os.environ.get(legacy)
        if generic_value is not None and legacy_value is not None:
            summary[suffix] = "both-equal" if generic_value == legacy_value else "conflict"
        elif generic_value is not None:
            summary[suffix] = "RHMCP"
        elif legacy_value is not None:
            summary[suffix] = "DSW_MCP"
        else:
            summary[suffix] = "default"
    return summary


def apply_env_compat() -> None:
    """Map RHMCP_* to legacy names only when the mapping is unambiguous."""
    for suffix in _SUFFIXES:
        generic = f"RHMCP_{suffix}"
        legacy = f"DSW_MCP_{suffix}"
        generic_value = os.environ.get(generic)
        legacy_value = os.environ.get(legacy)
        if generic_value is not None and legacy_value is not None and generic_value != legacy_value:
            raise ConfigError(
                f"Conflicting configuration: {generic} and {legacy} are both set differently; "
                "remove one namespace or make the values identical"
            )
        if legacy_value is None and generic_value is not None:
            os.environ[legacy] = generic_value
