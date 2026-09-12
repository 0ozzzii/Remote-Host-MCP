"""Canonical Remote Host MCP application surface."""

from __future__ import annotations

import logging
import os
import sys

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .branding import PRODUCT_NAME, apply_generic_branding
from .compat_env import apply_env_compat
from .config import ConfigError, Settings
from .host_server import build_server as build_execution_server


def load_settings() -> Settings:
    """Load canonical RHMCP_* settings while accepting legacy DSW_MCP_* files."""
    apply_env_compat()
    return Settings.from_env()


def build_server(settings: Settings) -> MCPServer:
    """Build the generic product server on top of the proven DSWD 2.x core."""
    return apply_generic_branding(build_execution_server(settings))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(settings.state_dir, 0o700)
    except OSError:
        pass

    server = build_server(settings)
    security = TransportSecuritySettings(
        allowed_hosts=[
            settings.public_host,
            f"{settings.public_host}:*",
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ],
        allowed_origins=[
            "https://chatgpt.com",
            "https://chat.openai.com",
        ],
    )

    print(f"{PRODUCT_NAME} listening on {settings.bind_host}:{settings.port}", file=sys.stderr)
    if settings.auth_mode == "oauth":
        print(f"Public MCP URL: {settings.public_url} (OAuth bearer required)", file=sys.stderr)
    else:
        print(f"Public MCP URL: https://{settings.public_host}/mcp/[REDACTED]", file=sys.stderr)

    server.run(
        transport="streamable-http",
        host=settings.bind_host,
        port=settings.port,
        streamable_http_path=settings.mcp_path,
        json_response=settings.json_response,
        stateless_http=settings.stateless_http,
        max_request_body_size=settings.max_request_body_bytes,
        max_sessions=64,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
