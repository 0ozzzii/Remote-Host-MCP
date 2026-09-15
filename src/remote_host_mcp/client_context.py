"""Caller identity captured from the inbound HTTP request.

RHMCP normally sits behind a Cloudflare Tunnel, so the TCP peer of every
request is the local tunnel daemon (``127.0.0.1``) and carries no information
about the real caller. Cloudflare publishes the caller identity as request
headers instead, and those headers survive the tunnel hop.

Deliberately NOT captured (see the接入方案 §4.4): ``clientCity``,
``clientRegion``, ``clientOrg`` and ``clientAsn``. Those values only exist on
Cloudflare's runtime object ``request.cf`` inside a Worker; a Tunnel does not
forward them as headers and the origin cannot observe them. Filling them in
would require a GeoIP database dependency, so they stay ``None`` rather than
being guessed.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

_CF_CONNECTING_IP = "cf-connecting-ip"
_CF_IP_COUNTRY = "cf-ipcountry"
_X_FORWARDED_FOR = "x-forwarded-for"
_USER_AGENT = "user-agent"

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_MAX_IP_CHARS = 64
_MAX_UA_CHARS = 512

# Cloudflare uses these sentinels when it cannot resolve a location.
_UNKNOWN_COUNTRIES = frozenset({"XX", "T1"})


@dataclass(frozen=True, slots=True)
class ClientContext:
    """Best-effort caller identity for one inbound request."""

    ip: str | None = None
    country: str | None = None
    ua: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.ip is None and self.country is None and self.ua is None

    def as_audit_fields(self) -> dict[str, str | None]:
        """Project into the Hub report schema, including the always-null geo fields."""
        return {
            "clientIp": self.ip,
            "clientCountry": self.country,
            "clientCity": None,
            "clientRegion": None,
            "clientOrg": None,
            "clientAsn": None,
            "clientUa": self.ua,
        }


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    """Case-insensitive header read.

    Starlette's ``Headers`` is already case-insensitive, but this helper keeps
    plain mappings (tests, hand-built request shims) behaving the same way.
    """
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == lowered:
                value = candidate
                break
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_hop(value: str | None) -> str | None:
    if value is None:
        return None
    hop = value.split(",", 1)[0].strip()
    return hop or None


def _clean_ip(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > _MAX_IP_CHARS or any(char.isspace() for char in value):
        return None
    return value


def _clean_country(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not _COUNTRY_RE.fullmatch(code) or code in _UNKNOWN_COUNTRIES:
        return None
    return code


def _clean_ua(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:_MAX_UA_CHARS]


def extract_client_context(
    headers: Mapping[str, Any],
    *,
    peer_host: str | None = None,
) -> ClientContext:
    """Build a :class:`ClientContext` from request headers.

    ``CF-Connecting-IP`` is authoritative: behind a Tunnel it is the only field
    that identifies the real caller. ``X-Forwarded-For`` and finally the TCP
    peer are used only as fallbacks for deployments that are not behind
    Cloudflare, so a self-hosted reverse proxy still yields a usable IP.
    """
    ip = _clean_ip(_header(headers, _CF_CONNECTING_IP))
    if ip is None:
        ip = _clean_ip(_first_hop(_header(headers, _X_FORWARDED_FOR)))
    if ip is None:
        ip = _clean_ip(peer_host)
    return ClientContext(
        ip=ip,
        country=_clean_country(_header(headers, _CF_IP_COUNTRY)),
        ua=_clean_ua(_header(headers, _USER_AGENT)),
    )


def capture_client_context(request: Any) -> ClientContext:
    """Extract caller identity from a Starlette request-like object."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return ClientContext()
    peer_host: str | None = None
    client = getattr(request, "client", None)
    if client is not None:
        peer_host = getattr(client, "host", None)
    return extract_client_context(headers, peer_host=peer_host)


_CURRENT: contextvars.ContextVar[ClientContext] = contextvars.ContextVar(
    "rhmcp_client_context", default=ClientContext()
)


def current_client_context() -> ClientContext:
    """Return the caller identity bound to the in-flight request."""
    return _CURRENT.get()


@contextmanager
def bind_client_context(context: ClientContext) -> Iterator[ClientContext]:
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


class ClientContextMiddleware:
    """Context-tier middleware that binds caller identity for the request.

    Runs outermost so the value is already bound by the time the tool dispatch
    layer (and anything auditing it) executes. The ContextVar is task-scoped, so
    concurrent stateless-HTTP requests never observe each other's caller.
    """

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        request = getattr(ctx, "request", None)
        context = capture_client_context(request) if request is not None else ClientContext()
        with bind_client_context(context):
            return await call_next(ctx)
