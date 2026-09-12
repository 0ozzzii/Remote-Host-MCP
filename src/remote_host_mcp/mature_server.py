"""Deprecated DSWD 2.x server-module compatibility shim.

Use remote_host_mcp.host_server in new code.
"""

from .host_server import _terminal_status, build_server, main

__all__ = ["_terminal_status", "build_server", "main"]

if __name__ == "__main__":
    main()
