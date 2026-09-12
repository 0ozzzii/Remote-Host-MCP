from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_EXEC_CFT_SAFE_HARD_MAX_MS = 90_000
_OAUTH_ALGORITHMS = {"RS256", "PS256", "ES256"}
_CANONICAL_PREFIX = "RHMCP_"
_LEGACY_PREFIX = "DSW_MCP_"


class ConfigError(ValueError):
    pass


def _label(suffix: str) -> str:
    return f"{_CANONICAL_PREFIX}{suffix}"


def _raw(suffix: str, default: str | None = None) -> str | None:
    """Read migration-compatible configuration.

    Existing DSWD 2.x variables intentionally win if both namespaces are present,
    so adding RHMCP_* to a live migration cannot silently override production.
    Clean standalone installations should use only RHMCP_*.
    """
    legacy = os.getenv(f"{_LEGACY_PREFIX}{suffix}")
    if legacy is not None:
        return legacy
    return os.getenv(f"{_CANONICAL_PREFIX}{suffix}", default)


def _int(suffix: str, default: int, minimum: int, maximum: int) -> int:
    name = _label(suffix)
    raw = _raw(suffix, str(default))
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bool(suffix: str, default: bool) -> bool:
    name = _label(suffix)
    raw = (_raw(suffix, "true" if default else "false") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _choice(suffix: str, default: str, allowed: set[str]) -> str:
    name = _label(suffix)
    value = (_raw(suffix, default) or "").strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {sorted(allowed)}")
    return value


def _https_url(suffix: str, raw: str | None, *, required: bool) -> str | None:
    name = _label(suffix)
    value = (raw or "").strip()
    if not value:
        if required:
            raise ConfigError(f"{name} is required")
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        raise ConfigError(f"{name} must be an absolute https URL without a fragment")
    return value


def _list_env(suffix: str, default: str) -> tuple[str, ...]:
    name = _label(suffix)
    raw = (_raw(suffix, default) or "").replace(",", " ")
    values = tuple(dict.fromkeys(item for item in raw.split() if item))
    if not values:
        raise ConfigError(f"{name} cannot be empty")
    return values


def _roots(raw: str | None) -> tuple[Path, ...]:
    if not raw:
        raw = f"{Path.home()}:/tmp"
    roots: list[Path] = []
    for item in raw.split(":"):
        item = item.strip()
        if not item:
            continue
        path = Path(item).expanduser().resolve(strict=False)
        if not path.is_absolute():
            raise ConfigError("RHMCP_ALLOWED_ROOTS entries must be absolute")
        if path not in roots:
            roots.append(path)
    if not roots:
        raise ConfigError("RHMCP_ALLOWED_ROOTS cannot be empty")
    return tuple(roots)


def _absolute_path(suffix: str, default: Path) -> Path:
    name = _label(suffix)
    raw = (_raw(suffix, str(default)) or "").strip()
    path = Path(raw).expanduser().resolve(strict=False)
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path


@dataclass(frozen=True, slots=True)
class Settings:
    path_key: str
    public_host: str
    bind_host: str
    port: int
    default_timeout_ms: int
    max_timeout_ms: int
    max_command_bytes: int
    max_output_bytes: int
    kill_grace_ms: int
    allowed_roots: tuple[Path, ...]
    state_dir: Path
    max_file_chunk_bytes: int
    max_transfer_bytes: int
    upload_ttl_seconds: int
    max_jobs: int
    job_max_timeout_ms: int
    job_log_max_bytes: int
    job_read_max_bytes: int
    job_retention_seconds: int
    job_heartbeat_seconds: int
    terminal_max_sessions: int
    terminal_buffer_bytes: int
    terminal_read_max_bytes: int
    terminal_idle_seconds: int
    terminal_max_lifetime_seconds: int
    terminal_wait_max_ms: int
    terminal_osc133_enabled: bool
    tasks_extension_enabled: bool
    auth_mode: str
    oauth_issuer: str | None
    oauth_jwks_url: str | None
    oauth_audience: str | None
    oauth_scopes: tuple[str, ...]
    oauth_algorithms: tuple[str, ...]
    json_response: bool
    stateless_http: bool
    max_request_body_bytes: int

    @property
    def mcp_path(self) -> str:
        if self.auth_mode == "oauth":
            return "/mcp"
        return f"/mcp/{self.path_key}"

    @property
    def public_url(self) -> str:
        return f"https://{self.public_host}{self.mcp_path}"

    @classmethod
    def from_env(cls) -> "Settings":
        auth_mode = _choice("AUTH_MODE", "capability", {"capability", "oauth"})
        path_key = (_raw("PATH_KEY", "") or "").strip()
        if auth_mode == "capability":
            if not _KEY_RE.fullmatch(path_key) or path_key.startswith("REPLACE_"):
                raise ConfigError("RHMCP_PATH_KEY must be a random 32-128 character URL-safe secret")
        elif path_key and not _KEY_RE.fullmatch(path_key):
            raise ConfigError("RHMCP_PATH_KEY, when retained in OAuth mode, must still be URL-safe")

        public_host = (_raw("PUBLIC_HOST", "") or "").strip().lower().rstrip(".")
        if not _HOST_RE.fullmatch(public_host):
            raise ConfigError("RHMCP_PUBLIC_HOST must be a DNS hostname without scheme or path")

        bind_host = (_raw("BIND_HOST", "127.0.0.1") or "").strip()
        if bind_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigError("RHMCP_BIND_HOST must stay on loopback; use a secure tunnel/reverse proxy for exposure")

        # Accept older deployments that still carry 120000, but clamp effective
        # synchronous execution to the established 90-second request-safe maximum.
        configured_max_timeout_ms = _int("MAX_TIMEOUT_MS", _EXEC_CFT_SAFE_HARD_MAX_MS, 1_000, 300_000)
        max_timeout_ms = min(configured_max_timeout_ms, _EXEC_CFT_SAFE_HARD_MAX_MS)
        default_timeout_ms = _int("DEFAULT_TIMEOUT_MS", 30_000, 1_000, max_timeout_ms)
        allowed_roots = _roots(_raw("ALLOWED_ROOTS"))
        state_dir = _absolute_path("STATE_DIR", Path.home() / ".local" / "state" / "remote-host-mcp")

        if auth_mode == "oauth":
            oauth_issuer = _https_url("OAUTH_ISSUER", _raw("OAUTH_ISSUER"), required=True)
            oauth_jwks_url = _https_url("OAUTH_JWKS_URL", _raw("OAUTH_JWKS_URL"), required=True)
            oauth_audience = _https_url(
                "OAUTH_AUDIENCE",
                _raw("OAUTH_AUDIENCE", f"https://{public_host}/mcp"),
                required=True,
            )
            oauth_scopes = _list_env("OAUTH_SCOPES", "remote-host")
            oauth_algorithms = _list_env("OAUTH_ALGORITHMS", "RS256")
            unsupported = set(oauth_algorithms) - _OAUTH_ALGORITHMS
            if unsupported:
                raise ConfigError(f"RHMCP_OAUTH_ALGORITHMS contains unsupported values: {sorted(unsupported)}")
        else:
            oauth_issuer = None
            oauth_jwks_url = None
            oauth_audience = None
            oauth_scopes = ("remote-host",)
            oauth_algorithms = ("RS256",)

        return cls(
            path_key=path_key,
            public_host=public_host,
            bind_host=bind_host,
            port=_int("PORT", 8765, 1024, 65535),
            default_timeout_ms=default_timeout_ms,
            max_timeout_ms=max_timeout_ms,
            max_command_bytes=_int("MAX_COMMAND_BYTES", 8192, 1, 32_768),
            max_output_bytes=_int("MAX_OUTPUT_BYTES", 262_144, 4096, 2_097_152),
            kill_grace_ms=_int("KILL_GRACE_MS", 1500, 100, 10_000),
            allowed_roots=allowed_roots,
            state_dir=state_dir,
            max_file_chunk_bytes=_int("MAX_FILE_CHUNK_BYTES", 262_144, 16_384, 524_288),
            max_transfer_bytes=_int("MAX_TRANSFER_BYTES", 2_147_483_648, 1_048_576, 109_951_162_777),
            upload_ttl_seconds=_int("UPLOAD_TTL_SECONDS", 3600, 60, 86_400),
            max_jobs=_int("MAX_JOBS", 256, 1, 4096),
            job_max_timeout_ms=_int("JOB_MAX_TIMEOUT_MS", 604_800_000, 60_000, 2_592_000_000),
            job_log_max_bytes=_int("JOB_LOG_MAX_BYTES", 67_108_864, 1_048_576, 1_073_741_824),
            job_read_max_bytes=_int("JOB_READ_MAX_BYTES", 131_072, 4096, 1_048_576),
            job_retention_seconds=_int("JOB_RETENTION_SECONDS", 604_800, 3600, 2_592_000),
            job_heartbeat_seconds=_int("JOB_HEARTBEAT_SECONDS", 5, 1, 60),
            terminal_max_sessions=_int("TERMINAL_MAX_SESSIONS", 16, 1, 128),
            terminal_buffer_bytes=_int("TERMINAL_BUFFER_BYTES", 1_048_576, 65_536, 16_777_216),
            terminal_read_max_bytes=_int("TERMINAL_READ_MAX_BYTES", 131_072, 4096, 1_048_576),
            terminal_idle_seconds=_int("TERMINAL_IDLE_SECONDS", 1800, 60, 86_400),
            terminal_max_lifetime_seconds=_int("TERMINAL_MAX_LIFETIME_SECONDS", 28_800, 300, 604_800),
            terminal_wait_max_ms=_int("TERMINAL_WAIT_MAX_MS", 20_000, 0, 30_000),
            terminal_osc133_enabled=_bool("TERMINAL_OSC133", True),
            tasks_extension_enabled=_bool("TASKS_EXTENSION", True),
            auth_mode=auth_mode,
            oauth_issuer=oauth_issuer,
            oauth_jwks_url=oauth_jwks_url,
            oauth_audience=oauth_audience,
            oauth_scopes=oauth_scopes,
            oauth_algorithms=oauth_algorithms,
            json_response=_bool("JSON_RESPONSE", True),
            stateless_http=_bool("STATELESS_HTTP", True),
            max_request_body_bytes=_int("MAX_REQUEST_BODY_BYTES", 786_432, 65_536, 8_388_608),
        )

    def _is_under_allowed_root(self, path: Path) -> bool:
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def resolve_allowed_path(self, raw: str | None, *, default: Path | None = None) -> Path:
        """Resolve a path including the final symlink and require the target to be allowed."""
        path = Path(raw).expanduser() if raw else (default or Path.home())
        resolved = path.resolve(strict=False)
        if not resolved.is_absolute():
            raise ValueError("Path must be absolute")
        if self._is_under_allowed_root(resolved):
            return resolved
        raise ValueError("Path is outside RHMCP_ALLOWED_ROOTS")

    def resolve_allowed_entry(self, raw: str) -> Path:
        """Validate an entry without following its final-component symlink."""
        path = Path(os.path.normpath(str(Path(raw).expanduser())))
        if not path.is_absolute():
            raise ValueError("Path must be absolute")

        for root in self.allowed_roots:
            if path == root:
                return root

        parent = path.parent.resolve(strict=False)
        if not self._is_under_allowed_root(parent):
            raise ValueError("Path is outside RHMCP_ALLOWED_ROOTS")
        return parent / path.name
