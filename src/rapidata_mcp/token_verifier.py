"""Bearer-token verification for the hosted MCP server.

Implements the MCP SDK's :class:`TokenVerifier` protocol by validating the
incoming JWT against the authorization server: the signature is checked with a
key from the issuer's published JWKS, and the issuer and expiry are enforced.
The token's ``sub`` claim is the Rapidata CustomerId; the raw JWT is carried
through on the resulting :class:`AccessToken` so a per-request client can
forward it to the API gateway, which scopes data access by that customer.
"""

from __future__ import annotations

import logging

import httpx
import jwt
from anyio import to_thread
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

# Asymmetric algorithms only — a JWKS carries public keys, so symmetric
# algorithms (HS*) must never be accepted here (would allow key confusion).
_ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]


class JWTVerifier(TokenVerifier):
    """Verifies issuer-signed JWTs against the authorization server's JWKS."""

    def __init__(
        self,
        issuer_url: str,
        *,
        jwks_uri: str | None = None,
        audience: str | None = None,
        leeway: int = 60,
    ) -> None:
        """Args:
        issuer_url: Expected ``iss`` claim, and the base for OIDC discovery.
        jwks_uri: Explicit JWKS URI; skips discovery when provided.
        audience: Expected ``aud``. ``None`` disables audience checking — the
            Rapidata gateway accepts a token regardless of audience and scopes
            by the token's customer, so this is normally left unset.
        leeway: Clock-skew allowance in seconds for the expiry check.
        """
        self._issuer = issuer_url
        base = issuer_url.rstrip("/")
        self._discovery_url = f"{base}/.well-known/openid-configuration"
        self._explicit_jwks_uri = jwks_uri
        self._audience = audience
        self._leeway = leeway
        self._jwks_client: PyJWKClient | None = None

    def _resolve_jwks_uri(self) -> str:
        if self._explicit_jwks_uri:
            return self._explicit_jwks_uri
        resp = httpx.get(self._discovery_url, timeout=10.0)
        resp.raise_for_status()
        jwks_uri = resp.json()["jwks_uri"]
        logger.info("Resolved JWKS URI from discovery: %s", jwks_uri)
        return jwks_uri

    def _get_jwks_client(self) -> PyJWKClient:
        # Built lazily and cached; PyJWKClient caches signing keys internally
        # and refetches on rotation (unknown kid).
        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(self._resolve_jwks_uri())
        return self._jwks_client

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._get_jwks_client().get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=_ALLOWED_ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={
                    "require": ["exp", "sub"],
                    "verify_aud": self._audience is not None,
                },
            )
        except Exception as e:
            # Any failure (bad signature, wrong issuer, expired, malformed)
            # is an auth failure; log at info and let the middleware 401.
            logger.info("Rejected bearer token: %s", e)
            return None

        scope = payload.get("scope") or payload.get("scp") or ""
        scopes = scope.split() if isinstance(scope, str) else list(scope)
        client_id = (
            payload.get("azp")
            or payload.get("client_id")
            or payload.get("sub")
            or ""
        )
        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=scopes,
            expires_at=int(payload["exp"]),
            subject=str(payload["sub"]),
            claims=payload,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # JWKS resolution and key fetch can touch the network; keep them off
        # the event loop.
        return await to_thread.run_sync(self._verify_sync, token)
