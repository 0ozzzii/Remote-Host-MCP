from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from .config import Settings


class OAuthJwksTokenVerifier(TokenVerifier):
    """Validate JWT bearer access tokens against an external OAuth/OIDC issuer.

    Remote Host MCP intentionally acts only as an MCP Resource Server. The authorization
    server remains external and publishes its normal OAuth metadata/JWKS. This
    keeps authorization-code, PKCE, refresh-token, CIMD/DCR, user login, and key
    rotation out of the host process while still giving ChatGPT/MCP standards-
    compliant RFC 9728 protected-resource discovery and bearer enforcement.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.auth_mode != "oauth":
            raise ValueError("OAuth verifier requires RHMCP_AUTH_MODE=oauth")
        assert settings.oauth_issuer is not None
        assert settings.oauth_jwks_url is not None
        assert settings.oauth_audience is not None
        self._issuer = settings.oauth_issuer
        self._audience = settings.oauth_audience
        self._algorithms = list(settings.oauth_algorithms)
        self._resource = settings.public_url
        self._jwks = PyJWKClient(settings.oauth_jwks_url, cache_keys=True, lifespan=300)

    @staticmethod
    def _scopes(claims: dict[str, Any]) -> list[str]:
        raw = claims.get("scope")
        if isinstance(raw, str):
            return [item for item in raw.split() if item]
        scp = claims.get("scp")
        if isinstance(scp, str):
            return [item for item in scp.split() if item]
        if isinstance(scp, list):
            return [str(item) for item in scp if str(item)]
        return []

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception:
            # Authentication failure is intentionally opaque. Never include token
            # bytes, JWT headers, or provider error bodies in application logs/results.
            return None

        client_id_raw = claims.get("client_id") or claims.get("azp") or claims.get("sub")
        if client_id_raw is None:
            return None
        subject_raw = claims.get("sub")
        exp_raw = claims.get("exp")
        try:
            expires_at = int(exp_raw) if exp_raw is not None else None
        except (TypeError, ValueError):
            return None
        if expires_at is not None and expires_at <= int(time.time()):
            return None

        return AccessToken(
            token=token,
            client_id=str(client_id_raw),
            scopes=self._scopes(claims),
            expires_at=expires_at,
            resource=self._resource,
            subject=(str(subject_raw) if subject_raw is not None else None),
            claims={
                "iss": str(claims.get("iss")),
                "aud": claims.get("aud"),
            },
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await asyncio.to_thread(self._verify_sync, token)


def build_oauth_components(settings: Settings) -> tuple[TokenVerifier | None, AuthSettings | None]:
    """Build MCP SDK bearer/PRM components; capability mode remains unchanged."""

    if settings.auth_mode != "oauth":
        return None, None
    assert settings.oauth_issuer is not None
    assert settings.oauth_audience is not None
    verifier = OAuthJwksTokenVerifier(settings)
    auth = AuthSettings(
        issuer_url=settings.oauth_issuer,
        resource_server_url=settings.public_url,
        required_scopes=list(settings.oauth_scopes),
        # The verifier validates aud explicitly, and we also set AccessToken.resource
        # so the SDK performs its own RFC 8707 resource check as defense in depth.
        validate_token_resource=True,
    )
    return verifier, auth
