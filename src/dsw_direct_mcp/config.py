"""DSWD 2.x configuration compatibility shim.

Canonical Remote Host MCP standalone configuration lives in ``remote_host_mcp.config``
and uses ``RHMCP_*`` names.  Existing callers importing the historical
``dsw_direct_mcp.config`` namespace keep the old error-label spelling so legacy
validation/tests and operational diagnostics do not change underneath a 2.x
migration.
"""

from __future__ import annotations

from remote_host_mcp.config import ConfigError as ConfigError
from remote_host_mcp.config import Settings as _CanonicalSettings


class Settings(_CanonicalSettings):
    @classmethod
    def from_env(cls) -> _CanonicalSettings:
        try:
            return _CanonicalSettings.from_env()
        except ConfigError as exc:
            message = str(exc).replace("RHMCP_", "DSW_MCP_")
            raise ConfigError(message) from exc


__all__ = ["ConfigError", "Settings"]
