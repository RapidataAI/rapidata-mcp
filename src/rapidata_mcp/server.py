"""The hosted Rapidata MCP server.

A remote, multi-tenant MCP server over the Streamable-HTTP transport. It is an
OAuth 2.0 protected resource (RFC 9728): unauthenticated requests get a 401
pointing at the protected-resource metadata, clients discover the Rapidata
authorization server from there, and each request carries a bearer JWT that is
verified and turned into a customer-scoped Rapidata client for the tool call.

The tool layer (:mod:`rapidata_mcp.tools`) is transport-agnostic; everything
here is the resource-server shell around it.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field

from mcp.server.auth.middleware.auth_context import (
    AuthContextMiddleware,
    get_access_token,
)
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from rapidata_mcp.auth import ClientProvider, EnvClientProvider, TokenClientProvider
from rapidata_mcp.token_verifier import JWTVerifier
from rapidata_mcp.tools import register_tools

logger = logging.getLogger(__name__)

_METADATA_PATH = "/.well-known/oauth-protected-resource"
_MCP_PATH = "/mcp"
_DEFAULT_SCOPES = ["openid", "profile", "email", "roles", "offline_access"]


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Server configuration, resolved from the environment."""

    resource_url: str = "https://mcp.rapidata.ai"
    issuer_url: str = "https://auth.rapidata.ai"
    scopes_supported: list[str] = field(default_factory=lambda: list(_DEFAULT_SCOPES))
    host: str = "0.0.0.0"
    port: int = 8000
    environment: str | None = None
    auth_disabled: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        scopes = os.environ.get("RAPIDATA_MCP_SCOPES")
        return cls(
            resource_url=os.environ.get(
                "RAPIDATA_MCP_RESOURCE_URL", cls.resource_url
            ).rstrip("/"),
            issuer_url=os.environ.get(
                "RAPIDATA_MCP_ISSUER_URL", cls.issuer_url
            ).rstrip("/"),
            scopes_supported=scopes.split() if scopes else list(_DEFAULT_SCOPES),
            host=os.environ.get("HOST", cls.host),
            port=int(os.environ.get("PORT", cls.port)),
            environment=os.environ.get("RAPIDATA_ENVIRONMENT") or None,
            auth_disabled=_env_flag("RAPIDATA_MCP_AUTH_DISABLED"),
        )


def _silence_sdk_stdout() -> None:
    """Keep the Rapidata SDK from printing to stdout and from emitting OTLP."""
    from rapidata.rapidata_client.config import rapidata_config

    rapidata_config.logging.silent_mode = True
    rapidata_config.logging.enable_otlp = False


def _token_provider_factory(environment: str | None):
    """Per-request factory: build a customer-scoped client from the bearer token.

    The token has already been verified by :class:`JWTVerifier`; here we read it
    back off the request's auth context and hand the raw JWT to the SDK, which
    forwards it to the gateway. The gateway scopes data access by the token's
    customer, so no customer id needs threading through.
    """

    def factory() -> ClientProvider:
        access = get_access_token()
        if access is None:
            # RequireAuthMiddleware guarantees a token reaches the tools, so this
            # only fires if the wiring is wrong.
            raise RuntimeError("No authenticated access token in request context")
        token = {
            "access_token": access.token,
            "token_type": "Bearer",
            "expires_at": access.expires_at,
        }
        return TokenClientProvider(token, environment=environment)

    return factory


def build_app(settings: Settings | None = None) -> Starlette:
    """Build the Streamable-HTTP ASGI app with the OAuth resource-server shell."""
    settings = settings or Settings.from_env()
    _silence_sdk_stdout()

    if settings.auth_disabled:
        logger.warning(
            "RAPIDATA_MCP_AUTH_DISABLED is set — serving WITHOUT authentication "
            "using ambient SDK credentials. Local development only."
        )
        env = settings.environment

        def provider_factory() -> ClientProvider:
            return EnvClientProvider(environment=env)

    else:
        provider_factory = _token_provider_factory(settings.environment)

    mcp = FastMCP("rapidata", stateless_http=True, json_response=False)
    register_tools(mcp, provider_factory)

    # Build the Streamable-HTTP transport directly so the OAuth shell below is
    # fully under our control (FastMCP couples advertised scopes to enforced
    # scopes, which we don't want).
    session_manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server, json_response=False, stateless=True
    )
    streamable_asgi = StreamableHTTPASGIApp(session_manager)

    metadata = {
        "resource": settings.resource_url,
        "authorization_servers": [settings.issuer_url],
        "scopes_supported": settings.scopes_supported,
        "bearer_methods_supported": ["header"],
    }

    async def protected_resource_metadata(request: Request) -> JSONResponse:
        return JSONResponse(metadata)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [
        Route("/health", health, methods=["GET"]),
        Route(_METADATA_PATH, protected_resource_metadata, methods=["GET"]),
    ]
    middleware: list[Middleware] = []

    if settings.auth_disabled:
        routes.append(Route(_MCP_PATH, endpoint=streamable_asgi))
    else:
        verifier = JWTVerifier(settings.issuer_url)
        middleware = [
            Middleware(
                AuthenticationMiddleware, backend=BearerAuthBackend(verifier)
            ),
            Middleware(AuthContextMiddleware),
        ]
        # required_scopes=[] enforces only a valid token; the metadata above
        # advertises the supported scopes a client should request.
        guarded = RequireAuthMiddleware(
            streamable_asgi,
            required_scopes=[],
            resource_metadata_url=AnyHttpUrl(
                f"{settings.resource_url}{_METADATA_PATH}"
            ),
        )
        routes.append(Route(_MCP_PATH, endpoint=guarded))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def main() -> None:
    """Console-script entry point: serve the app with uvicorn."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    app = build_app(settings)

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
