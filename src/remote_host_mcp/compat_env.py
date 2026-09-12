"""Environment compatibility for Remote Host MCP.

RHMCP_* is the public generic namespace. Existing DSWD deployments may keep
DSW_MCP_* during migration; explicit legacy values win if both are set.
"""

from __future__ import annotations

import os

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


def apply_env_compat() -> None:
    """Map RHMCP_* to the legacy DSW_MCP_* implementation namespace.

    This is intentionally one-way. A deployed DSW profile can keep its existing
    DSW_MCP_* file unchanged, while new generic installations use RHMCP_*.
    """
    for suffix in _SUFFIXES:
        generic = f"RHMCP_{suffix}"
        legacy = f"DSW_MCP_{suffix}"
        if legacy not in os.environ and generic in os.environ:
            os.environ[legacy] = os.environ[generic]
