"""Canonical Remote Host MCP product branding for the pinned MCP SDK 2.2.x.

The standalone product intentionally keeps the proven DSWD 2.x execution core intact while
presenting a generic host-facing MCP contract. This adapter is deliberately
small, version-pinned and covered by wire tests because MCPServer currently does
not expose public setters for registered tool metadata.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .provenance import get_build_provenance

PRODUCT_NAME = "Remote Host MCP"
PRODUCT_TITLE = "Remote Host MCP"
PRODUCT_DESCRIPTION = (
    "General-purpose MCP control plane for remote Linux hosts and containers, "
    "with shell, PTY, filesystem, transfer, durable jobs, process and service tools."
)

# Ordered from specific legacy product phrases to the final generic fallback.
_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("DSW Direct Control", PRODUCT_NAME),
    ("ModelScope DSW", "remote Linux host"),
    ("DSW_MCP_", "RHMCP_"),
    ("DSWD-owned", "Remote Host MCP-owned"),
    ("DSWD server", "Remote Host MCP server"),
    ("DSWD Job engine", "durable Job engine"),
    ("explicit DSWD job control", "explicit durable-job control"),
    ("DSW/MCP", "host/MCP"),
    ("DSW directory", "host directory"),
    ("DSW text file", "host text file"),
    ("DSW file", "host file"),
    ("DSW path", "host path"),
    ("on DSW", "on the remote host"),
    ("locally on DSW", "locally on the remote host"),
    ("from a DSW file", "from a host file"),
    ("inside DSW", "inside the remote host"),
    # Canonical model-visible metadata must not expose the historical product
    # token at all. Specific replacements above preserve natural phrasing first.
    ("DSWD", "Remote Host MCP"),
    ("DSW", "host"),
)


def _generic_text(value: str | None) -> str | None:
    if value is None:
        return None
    result = value
    for old, new in _REPLACEMENTS:
        result = result.replace(old, new)
    return result


def _rewrite_schema(value: Any) -> Any:
    if isinstance(value, str):
        return _generic_text(value)
    if isinstance(value, list):
        return [_rewrite_schema(item) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_schema(item) for key, item in value.items()}
    return value


async def _health(_request: Request) -> Response:
    build_commit, build_ref = get_build_provenance()
    return JSONResponse(
        {
            "ok": True,
            "service": PRODUCT_NAME,
            "version": __version__,
            "build_commit": build_commit,
            "build_ref": build_ref,
            "transport": "streamable-http",
        },
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def apply_generic_branding(mcp: MCPServer) -> MCPServer:
    """Apply the generic product identity without changing tool names or behavior.

    MCP SDK 2.2.x exposes registration through MCPServer but not a public metadata
    update API. The project pins 2.2.0 and therefore uses the SDK's internal
    managers here behind dedicated tests. If the SDK pin changes, this adapter is
    a mandatory compatibility checkpoint.
    """
    if getattr(mcp, "_rhmcp_branding_applied", False):
        return mcp

    lowlevel = mcp._lowlevel_server  # type: ignore[attr-defined]
    lowlevel.name = PRODUCT_NAME
    lowlevel.title = PRODUCT_TITLE
    lowlevel.description = PRODUCT_DESCRIPTION

    manager = mcp._tool_manager  # type: ignore[attr-defined]
    for tool in manager.list_tools():
        tool.title = _generic_text(tool.title)
        tool.description = _generic_text(tool.description) or ""
        parameters = _rewrite_schema(tool.parameters)
        if isinstance(parameters, dict) and parameters.get("type") == "object":
            parameters["additionalProperties"] = False
        tool.parameters = parameters

    # status() returns a typed StatusResult whose service field was historically
    # DSW-specific. Reuse the proven implementation and only replace the label.
    status_tool = manager.get_tool("status")
    if status_tool is not None:
        original_status = status_tool.fn

        async def generic_status() -> Any:
            result = await original_status()
            if hasattr(result, "model_copy"):
                return result.model_copy(update={"service": PRODUCT_NAME})
            return result

        status_tool.fn = generic_status
        status_tool.is_async = True

    # The legacy execution server registered /health first. Replace it so the
    # canonical health route never reports a historical product name.
    routes = [
        route
        for route in mcp._custom_starlette_routes  # type: ignore[attr-defined]
        if getattr(route, "path", None) != "/health"
    ]
    mcp._custom_starlette_routes[:] = [  # type: ignore[attr-defined]
        Route("/health", endpoint=_health, methods=["GET"], name="rhmcp-health"),
        *routes,
    ]

    setattr(mcp, "_rhmcp_branding_applied", True)
    return mcp
