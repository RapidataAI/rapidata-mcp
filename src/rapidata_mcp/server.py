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
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from rapidata_mcp.auth import ClientProvider, EnvClientProvider, TokenClientProvider
from rapidata_mcp.token_verifier import JWTVerifier
from rapidata_mcp.tools import register_tools

logger = logging.getLogger(__name__)

_METADATA_PATH = "/.well-known/oauth-protected-resource"
_MCP_PATH = "/mcp"
# "mcp" is the scope that carries https://mcp.rapidata.ai as an OAuth resource on
# the auth server; clients must request it so the auth server accepts the RFC 8707
# resource indicator they send for this server (otherwise: invalid_target).
# "api" additionally carries the backend API (https://api.rapidata.ai) as a resource,
# so the token this server forwards to the Rapidata API stays valid once the API
# enforces token audiences. It's harmless until then (audience is not yet enforced).
_DEFAULT_SCOPES = [
    "openid",
    "profile",
    "email",
    "roles",
    "offline_access",
    "mcp",
    "api",
]

# Surfaced to the connecting agent via the MCP initialize response so it knows,
# up front, that these tools are only a slice of Rapidata and where to go for more.
_INSTRUCTIONS = (
    "Rapidata runs human-in-the-loop labeling tasks. These tools are a small, "
    "curated subset of the Rapidata platform: classification and comparison on the "
    "global audience, created as a draft and run only after you confirm the cost "
    "with the user (creating never spends; start_job does).\n\n"
    "Billing is pre-paid, pay-as-you-go for individual (non-organization) accounts, "
    "and new sign-ups get $20 in free credit — so trying these tools out is free.\n\n"
    "For anything beyond that — other task types (ranking, draw, locate, free text, "
    "select words), curated or custom audiences and targeting filters (country, "
    "language, age, device), benchmarks/leaderboards, or richer per-annotator "
    "results — use the full Rapidata Python SDK directly instead of these tools. "
    "The easiest way is to install the official Rapidata skill, which teaches the "
    "agent to write Rapidata SDK code: in Claude Code run "
    "`/install-plugin https://github.com/RapidataAI/skills`; for other agents see "
    "https://docs.rapidata.ai/latest/ai_agents/ . SDK docs: https://docs.rapidata.ai/"
)


# Shown to a human who opens the endpoint in a browser (the /mcp path is a
# machine endpoint, so a plain visit would otherwise just 401). MCP clients are
# unaffected: they never send Accept: text/html.
_OVERVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rapidata MCP Server</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font: 16px/1.6 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    max-width: 42rem; margin: 4rem auto; padding: 0 1.25rem;
  }
  h1 { font-size: 1.6rem; margin-bottom: .25rem; }
  .sub { color: #6b7280; margin-top: 0; }
  code {
    background: rgba(127,127,127,.15); padding: .15em .4em; border-radius: .3em;
  }
  .card {
    background: rgba(127,127,127,.08); border-radius: .6em;
    padding: 1rem 1.25rem; margin: 1.5rem 0;
  }
  a { color: #2563eb; }
  ul { padding-left: 1.2rem; }
</style>
</head>
<body>
  <h1>Rapidata MCP Server</h1>
  <p class="sub">Real human feedback and labeling for your AI agent.</p>
  <p>This is the
     <a href="https://modelcontextprotocol.io">Model Context Protocol</a>
     endpoint for <a href="https://www.rapidata.ai">Rapidata</a>. It's a machine
     endpoint meant to be added to an MCP-capable client (Claude, Cursor, and
     others) — not browsed directly. Point your client at it and it can create
     classification and comparison tasks, run them on a global crowd of humans,
     and read the results.</p>
  <div class="card">
    <strong>Connect</strong>
    <p style="margin:.5rem 0 0">
      Add this URL as a custom connector / remote MCP server:
    </p>
    <p style="margin:.5rem 0 0"><code>https://mcp.rapidata.ai/mcp</code></p>
    <p style="margin:.5rem 0 0">
      You'll sign in once with your Rapidata account. New accounts get
      <strong>$20 in free credit</strong>, so trying it out is free.
    </p>
  </div>
  <p>Learn more:</p>
  <ul>
    <li><a href="https://docs.rapidata.ai/">Rapidata documentation</a></li>
    <li>
      <a href="https://docs.rapidata.ai/latest/ai_agents/">Use the full
      Rapidata SDK from an agent</a>
    </li>
    <li><a href="https://www.rapidata.ai">rapidata.ai</a></li>
  </ul>
</body>
</html>"""


class _BrowserLandingApp:
    """Serves an HTML overview to browsers and delegates everything else.

    Wraps the /mcp endpoint so a human who opens the URL sees what this is,
    while MCP protocol traffic and the OAuth discovery 401 pass through — those
    requests never send Accept: text/html, so they're never intercepted.
    """

    def __init__(self, app, html: str) -> None:
        self._app = app
        self._html = html

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") == "GET":
            accept = b""
            for key, value in scope.get("headers", []):
                if key == b"accept":
                    accept = value
                    break
            if b"text/html" in accept.lower():
                await HTMLResponse(self._html)(scope, receive, send)
                return
        await self._app(scope, receive, send)


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

    mcp = FastMCP(
        "rapidata",
        instructions=_INSTRUCTIONS,
        stateless_http=True,
        json_response=False,
    )
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

    async def landing(request: Request) -> HTMLResponse:
        return HTMLResponse(_OVERVIEW_HTML)

    routes = [
        Route("/", landing, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route(_METADATA_PATH, protected_resource_metadata, methods=["GET"]),
        # RFC 9728 also lets clients build the metadata URL by inserting the
        # resource's path after /.well-known/, so serve that form too (otherwise
        # a spec-compliant client requesting /.well-known/...-resource/mcp 404s).
        Route(
            f"{_METADATA_PATH}{_MCP_PATH}",
            protected_resource_metadata,
            methods=["GET"],
        ),
    ]

    # CORS must be outermost so it answers browser preflights (OPTIONS) before the
    # auth layer can reject them, and so browser-based MCP clients can read the
    # metadata and the WWW-Authenticate discovery hint cross-origin. Auth is via
    # the Authorization header (not cookies), so wildcard origins are safe.
    middleware: list[Middleware] = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["WWW-Authenticate", "Mcp-Session-Id"],
            max_age=600,
        )
    ]

    if settings.auth_disabled:
        mcp_endpoint = streamable_asgi
    else:
        verifier = JWTVerifier(settings.issuer_url)
        middleware += [
            Middleware(
                AuthenticationMiddleware, backend=BearerAuthBackend(verifier)
            ),
            Middleware(AuthContextMiddleware),
        ]
        # required_scopes=[] enforces only a valid token; the metadata above
        # advertises the supported scopes a client should request.
        mcp_endpoint = RequireAuthMiddleware(
            streamable_asgi,
            required_scopes=[],
            resource_metadata_url=AnyHttpUrl(
                f"{settings.resource_url}{_METADATA_PATH}"
            ),
        )

    # Wrap so a browser opening the URL gets the overview; MCP traffic passes through.
    routes.append(
        Route(_MCP_PATH, endpoint=_BrowserLandingApp(mcp_endpoint, _OVERVIEW_HTML))
    )

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
