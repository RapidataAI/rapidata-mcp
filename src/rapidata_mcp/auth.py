"""Client-resolution seam for the Rapidata MCP server.

Tools never construct a :class:`RapidataClient` themselves; they ask a
``ClientProvider`` for one. This is the boundary that lets the same tool code
run under different auth models:

* ``EnvClientProvider`` — ambient credentials from the SDK's own resolution
  chain (``RAPIDATA_CLIENT_ID`` / ``RAPIDATA_CLIENT_SECRET`` env vars, then the
  on-disk credential file). One process serves exactly one customer, so the
  client is built once and reused. Used for local development.
* ``TokenClientProvider`` — a pre-obtained OAuth token. This is the entry point
  the hosted, multi-tenant transport plugs into: validate the caller's bearer
  token, then build a per-request client scoped to that customer. The SDK
  already accepts a ``token`` object, so no SDK change is needed to get here.
* ``ExchangedTokenProvider`` — the hosted default once a client secret is set:
  the caller's mcp token is exchanged (RFC 8693) for an api token, and only the
  api token reaches the Rapidata API.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from rapidata import RapidataClient


class ClientProvider(Protocol):
    """Resolves an authenticated :class:`RapidataClient` for a tool call."""

    def get_client(self) -> RapidataClient: ...


class EnvClientProvider:
    """Builds one :class:`RapidataClient` from ambient credentials and reuses it."""

    def __init__(self, environment: str | None = None) -> None:
        self._environment = environment
        self._client: RapidataClient | None = None
        self._lock = threading.Lock()

    def get_client(self) -> RapidataClient:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from rapidata import RapidataClient

                    self._client = RapidataClient(environment=self._environment)
        return self._client


class TokenClientProvider:
    """Builds a :class:`RapidataClient` from a caller-supplied OAuth token.

    The token must be the complete object the SDK expects (access token, token
    type, expiry) — the same shape returned by the OAuth token endpoint.
    """

    def __init__(self, token: dict, environment: str | None = None) -> None:
        self._token = token
        self._environment = environment

    def get_client(self) -> RapidataClient:
        from rapidata import RapidataClient

        return RapidataClient(token=self._token, environment=self._environment)


_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class TokenExchangeError(RuntimeError):
    """The authorization server refused to exchange the caller's token."""


class TokenExchanger:
    """Exchanges mcp access tokens for api access tokens, caching each result.

    Shared across requests; the cache is keyed by ``sha256(subject token)`` and an
    entry lives until the exchanged token expires, minus ``skew`` seconds.
    """

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        api_resource: str,
        *,
        scope: str = "api",
        skew: int = 60,
        http: httpx.Client | None = None,
    ) -> None:
        self._token_endpoint = f"{issuer_url.rstrip('/')}/connect/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_resource = api_resource
        self._scope = scope
        self._skew = skew
        self._http = http or httpx.Client(timeout=10.0)
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def exchange(self, subject_token: str) -> dict:
        """Return an SDK token object for the API, exchanging on a cache miss."""
        key = hashlib.sha256(subject_token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached["expires_at"] - self._skew > now:
                return cached

        token = self._request(subject_token, now)
        with self._lock:
            self._cache = {
                k: v for k, v in self._cache.items() if v["expires_at"] > now
            }
            self._cache[key] = token
        return token

    def _request(self, subject_token: str, now: float) -> dict:
        try:
            resp = self._http.post(
                self._token_endpoint,
                data={
                    "grant_type": _TOKEN_EXCHANGE_GRANT,
                    "subject_token": subject_token,
                    "subject_token_type": _ACCESS_TOKEN_TYPE,
                    "resource": self._api_resource,
                    "scope": self._scope,
                },
                auth=(self._client_id, self._client_secret),
            )
        except httpx.HTTPError as e:
            raise TokenExchangeError(f"Token exchange failed: {e}") from e

        if resp.status_code in (400, 401):
            if _error_code(resp) == "invalid_grant":
                raise TokenExchangeError(
                    "Not authorized: your Rapidata sign-in is no longer valid "
                    f"({_error_detail(resp)}). Re-authenticate with the Rapidata MCP "
                    "server and retry."
                )
            raise TokenExchangeError(
                "Not authorized: the Rapidata authorization server refused to issue "
                f"an API token for this MCP server ({_error_detail(resp)}). This is "
                "a server configuration problem; signing in again will not help."
            )
        if resp.status_code != 200:
            raise TokenExchangeError(f"Token exchange failed ({_error_detail(resp)})")

        body = resp.json()
        return {
            "access_token": body["access_token"],
            "token_type": body.get("token_type", "Bearer"),
            "expires_at": int(now + int(body.get("expires_in", 3600))),
        }


def _error_body(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(resp: httpx.Response) -> str | None:
    return _error_body(resp).get("error")


def _error_detail(resp: httpx.Response) -> str:
    body = _error_body(resp)
    error, description = body.get("error"), body.get("error_description")
    parts = [f"HTTP {resp.status_code}", error, description]
    return ": ".join(p for p in parts if p)


class ExchangedTokenProvider:
    """Builds a :class:`RapidataClient` from an api token exchanged for the caller's
    mcp token; the mcp token itself never reaches the Rapidata API."""

    def __init__(
        self,
        exchanger: TokenExchanger,
        subject_token: str,
        environment: str | None = None,
    ) -> None:
        self._exchanger = exchanger
        self._subject_token = subject_token
        self._environment = environment

    def get_client(self) -> RapidataClient:
        from rapidata import RapidataClient

        token = self._exchanger.exchange(self._subject_token)
        return RapidataClient(token=dict(token), environment=self._environment)
