from __future__ import annotations

import pytest

from remote_host_mcp.client_context import (
    ClientContext,
    bind_client_context,
    capture_client_context,
    current_client_context,
    extract_client_context,
)


def test_cloudflare_headers_win_over_loopback_peer() -> None:
    context = extract_client_context(
        {
            "CF-Connecting-IP": "203.0.113.7",
            "CF-IPCountry": "cn",
            "User-Agent": "Mozilla/5.0 (ChatGPT)",
        },
        peer_host="127.0.0.1",
    )
    assert context.ip == "203.0.113.7"
    assert context.country == "CN"
    assert context.ua == "Mozilla/5.0 (ChatGPT)"


def test_tcp_peer_is_never_used_when_cloudflare_header_present() -> None:
    context = extract_client_context(
        {"CF-Connecting-IP": "198.51.100.9"}, peer_host="127.0.0.1"
    )
    assert context.ip == "198.51.100.9"


def test_forwarded_for_and_peer_are_fallbacks_only() -> None:
    forwarded = extract_client_context(
        {"X-Forwarded-For": "198.51.100.4, 10.0.0.1"}, peer_host="127.0.0.1"
    )
    assert forwarded.ip == "198.51.100.4"

    peer_only = extract_client_context({}, peer_host="192.0.2.10")
    assert peer_only.ip == "192.0.2.10"

    empty = extract_client_context({})
    assert empty.ip is None


def test_geo_fields_outside_cloudflare_headers_stay_null() -> None:
    """City/region/org/ASN are unavailable at the origin and must not be invented."""
    fields = extract_client_context(
        {"CF-Connecting-IP": "203.0.113.7", "CF-IPCountry": "CN"}
    ).as_audit_fields()
    assert fields["clientCity"] is None
    assert fields["clientRegion"] is None
    assert fields["clientOrg"] is None
    assert fields["clientAsn"] is None
    assert fields["clientIp"] == "203.0.113.7"
    assert fields["clientCountry"] == "CN"


@pytest.mark.parametrize("sentinel", ["XX", "T1"])
def test_cloudflare_unknown_country_sentinels_are_dropped(sentinel: str) -> None:
    assert extract_client_context({"CF-IPCountry": sentinel}).country is None


@pytest.mark.parametrize("bad", ["", "CHN", "C1", "  ", "C"])
def test_malformed_country_is_dropped(bad: str) -> None:
    assert extract_client_context({"CF-IPCountry": bad}).country is None


def test_oversized_or_whitespace_ip_is_dropped() -> None:
    assert extract_client_context({"CF-Connecting-IP": "a" * 65}).ip is None
    assert extract_client_context({"CF-Connecting-IP": "1.2.3.4 5.6.7.8"}).ip is None


def test_user_agent_is_bounded() -> None:
    context = extract_client_context({"User-Agent": "u" * 4000})
    assert context.ua is not None
    assert len(context.ua) == 512


def test_context_is_bound_and_restored() -> None:
    assert current_client_context().is_empty
    with bind_client_context(ClientContext(ip="203.0.113.7", country="CN")):
        assert current_client_context().ip == "203.0.113.7"
    assert current_client_context().is_empty


class _FakeRequest:
    def __init__(self, headers: dict[str, str], host: str = "127.0.0.1") -> None:
        self.headers = headers
        self.client = type("Peer", (), {"host": host})()


def test_capture_from_request_object() -> None:
    request = _FakeRequest({"CF-Connecting-IP": "203.0.113.7", "CF-IPCountry": "DE"})
    context = capture_client_context(request)
    assert context.ip == "203.0.113.7"
    assert context.country == "DE"


def test_capture_tolerates_missing_attributes() -> None:
    assert capture_client_context(object()).is_empty


class _FakeMiddlewareContext:
    def __init__(self, request: object | None) -> None:
        self.request = request


async def test_middleware_binds_caller_for_the_inner_chain() -> None:
    from remote_host_mcp.client_context import ClientContextMiddleware

    seen: list[ClientContext] = []

    async def call_next(_ctx: object) -> str:
        seen.append(current_client_context())
        return "ok"

    request = _FakeRequest({"CF-Connecting-IP": "203.0.113.7", "CF-IPCountry": "JP"})
    result = await ClientContextMiddleware()(_FakeMiddlewareContext(request), call_next)

    assert result == "ok"
    assert seen[0].ip == "203.0.113.7"
    assert seen[0].country == "JP"
    assert current_client_context().is_empty


async def test_middleware_without_request_binds_nothing() -> None:
    from remote_host_mcp.client_context import ClientContextMiddleware

    async def call_next(_ctx: object) -> str:
        assert current_client_context().is_empty
        return "ok"

    assert await ClientContextMiddleware()(_FakeMiddlewareContext(None), call_next) == "ok"


def test_mcp_server_middleware_list_accepts_the_middleware() -> None:
    """Guard the SDK seam: host_server appends to MCPServer.middleware at build time."""
    from mcp.server import MCPServer

    from remote_host_mcp.client_context import ClientContextMiddleware

    server = MCPServer("test")
    middleware = ClientContextMiddleware()
    server.middleware.append(middleware)
    assert server.middleware[-1] is middleware
