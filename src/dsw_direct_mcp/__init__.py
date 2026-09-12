"""Compatibility namespace for DSWD 2.x imports.

New code should import ``remote_host_mcp``.  Existing ``dsw_direct_mcp.*``
references remain supported during the pre-1.0 migration.  Explicit shims in this
package take precedence; modules without a shim transparently fall through to
the canonical Remote Host MCP package.
"""

from pathlib import Path

from remote_host_mcp import __version__
from remote_host_mcp import __path__ as _remote_host_path

_compat_path = str(Path(__file__).resolve().parent)
__path__ = [_compat_path, *[path for path in _remote_host_path if path != _compat_path]]

__all__ = ["__version__"]
