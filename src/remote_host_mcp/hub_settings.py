"""CF-MCP-HUB agent configuration (``RHMCP_HUB_*``).

These variables are written into ``rhmcp.env`` by the Hub install script, so the
names are a fixed contract with the Hub side — do not rename them.

Two distinct consumers:

* the RHMCP execution server reads only the audit settings, and must never fail
  to start because the Hub block is absent or malformed (``strict=False``);
* the sidecar reporting agent reads the whole block and is expected to fail
  loudly on misconfiguration (``strict=True``).

The Hub is optional by design: with no ``RHMCP_HUB_BASE_URL`` the execution
server still writes its local audit log, it just has nothing to report to.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("remote_host_mcp.hub")

REPORT_LEVELS = ("meta", "tool", "full")
OUTPUT_MODES = ("r2", "local", "preview")

AGENT_KEY_PREFIX = "agt_"

DEFAULT_AUDIT_SUBPATH = Path("audit") / "calls.jsonl"
DEFAULT_REPORT_INTERVAL_SECONDS = 30
DEFAULT_REPORT_BATCH_SIZE = 50
DEFAULT_REPORT_MAX_BATCH = 200
DEFAULT_HEARTBEAT_SECONDS = 60

# Local audit-log rotation. 32 MiB / 3 generations bounds the log at ~128 MiB
# no matter how long the host runs.
DEFAULT_AUDIT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_AUDIT_KEEP_FILES = 3


class HubConfigError(ValueError):
    pass


def _env(suffix: str) -> str | None:
    value = os.environ.get(f"RHMCP_HUB_{suffix}")
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_bool(suffix: str, default: bool, *, strict: bool) -> bool:
    raw = _env(suffix)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    if strict:
        raise HubConfigError(f"RHMCP_HUB_{suffix} must be true or false")
    logger.warning("ignoring invalid RHMCP_HUB_%s; using default %s", suffix, default)
    return default


def _env_int(suffix: str, default: int, minimum: int, maximum: int, *, strict: bool) -> int:
    raw = _env(suffix)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        if strict:
            raise HubConfigError(f"RHMCP_HUB_{suffix} must be an integer") from None
        logger.warning("ignoring invalid RHMCP_HUB_%s; using default %s", suffix, default)
        return default
    if not minimum <= value <= maximum:
        if strict:
            raise HubConfigError(f"RHMCP_HUB_{suffix} must be between {minimum} and {maximum}")
        logger.warning("ignoring out-of-range RHMCP_HUB_%s; using default %s", suffix, default)
        return default
    return value


def _env_choice(suffix: str, default: str, allowed: tuple[str, ...], *, strict: bool) -> str:
    raw = _env(suffix)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in allowed:
        return lowered
    if strict:
        raise HubConfigError(f"RHMCP_HUB_{suffix} must be one of {list(allowed)}")
    logger.warning("ignoring invalid RHMCP_HUB_%s; using default %s", suffix, default)
    return default


def _env_https_url(suffix: str, *, strict: bool) -> str | None:
    raw = _env(suffix)
    if raw is None:
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        if strict:
            raise HubConfigError(
                f"RHMCP_HUB_{suffix} must be an absolute https URL without a fragment"
            )
        logger.warning("ignoring invalid RHMCP_HUB_%s", suffix)
        return None
    return raw.rstrip("/")


def default_state_dir() -> Path:
    raw = os.environ.get("RHMCP_STATE_DIR") or os.environ.get("DSW_MCP_STATE_DIR")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser().resolve(strict=False)
    return Path.home() / ".local" / "state" / "remote-host-mcp"


@dataclass(frozen=True, slots=True)
class HubSettings:
    base_url: str | None = None
    host_id: str | None = None
    agent_key: str | None = None
    report_level: str = "tool"
    redact_enabled: bool = True
    output_mode: str = "preview"

    report_enabled: bool = True
    audit_enabled: bool = True
    audit_log_path: Path | None = None
    audit_max_bytes: int = DEFAULT_AUDIT_MAX_BYTES
    audit_keep_files: int = DEFAULT_AUDIT_KEEP_FILES

    report_interval_seconds: int = DEFAULT_REPORT_INTERVAL_SECONDS
    report_batch_size: int = DEFAULT_REPORT_BATCH_SIZE
    report_max_batch: int = DEFAULT_REPORT_MAX_BATCH
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS

    @property
    def configured(self) -> bool:
        """True when there is enough information to talk to a Hub at all."""
        return bool(self.base_url and self.agent_key and self.host_id)

    @property
    def reporting_active(self) -> bool:
        return self.report_enabled and self.configured

    def resolved_audit_log_path(self) -> Path:
        if self.audit_log_path is not None:
            return self.audit_log_path
        return default_state_dir() / DEFAULT_AUDIT_SUBPATH

    @classmethod
    def from_env(cls, *, strict: bool = False) -> "HubSettings":
        agent_key = _env("AGENT_KEY")
        if agent_key is not None and not agent_key.startswith(AGENT_KEY_PREFIX):
            if strict:
                raise HubConfigError(f"RHMCP_HUB_AGENT_KEY must start with {AGENT_KEY_PREFIX!r}")
            logger.warning("RHMCP_HUB_AGENT_KEY does not start with %r", AGENT_KEY_PREFIX)
            agent_key = None

        audit_log_path: Path | None = None
        raw_path = _env("AUDIT_LOG_PATH")
        if raw_path is not None:
            candidate = Path(raw_path).expanduser()
            if candidate.is_absolute():
                audit_log_path = candidate.resolve(strict=False)
            elif strict:
                raise HubConfigError("RHMCP_HUB_AUDIT_LOG_PATH must be an absolute path")
            else:
                logger.warning("ignoring relative RHMCP_HUB_AUDIT_LOG_PATH")

        return cls(
            base_url=_env_https_url("BASE_URL", strict=strict),
            host_id=_env("HOST_ID"),
            agent_key=agent_key,
            report_level=_env_choice("REPORT_LEVEL", "tool", REPORT_LEVELS, strict=strict),
            redact_enabled=_env_bool("REDACT_ENABLED", True, strict=strict),
            output_mode=_env_choice("OUTPUT_MODE", "preview", OUTPUT_MODES, strict=strict),
            report_enabled=_env_bool("REPORT_ENABLED", True, strict=strict),
            audit_enabled=_env_bool("AUDIT_ENABLED", True, strict=strict),
            audit_log_path=audit_log_path,
            audit_max_bytes=_env_int(
                "AUDIT_MAX_BYTES", DEFAULT_AUDIT_MAX_BYTES, 64 * 1024, 4 * 1024 * 1024 * 1024, strict=strict
            ),
            audit_keep_files=_env_int("AUDIT_KEEP_FILES", DEFAULT_AUDIT_KEEP_FILES, 0, 100, strict=strict),
            report_interval_seconds=_env_int(
                "REPORT_INTERVAL_SECONDS", DEFAULT_REPORT_INTERVAL_SECONDS, 1, 3600, strict=strict
            ),
            report_batch_size=_env_int(
                "REPORT_BATCH_SIZE", DEFAULT_REPORT_BATCH_SIZE, 1, DEFAULT_REPORT_MAX_BATCH, strict=strict
            ),
            report_max_batch=_env_int(
                "REPORT_MAX_BATCH", DEFAULT_REPORT_MAX_BATCH, 1, 1000, strict=strict
            ),
            heartbeat_seconds=_env_int("HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS, 5, 86_400, strict=strict),
        )
