"""Hosted MCP server for the Rapidata labeling platform.

Exposes Rapidata labeling tasks to MCP-capable agents over the Streamable-HTTP
transport, as an OAuth 2.0 protected resource. Run it with the ``rapidata-mcp``
console script or ``python -m rapidata_mcp``.
"""

from __future__ import annotations

from rapidata_mcp.auth import ClientProvider, EnvClientProvider, TokenClientProvider
from rapidata_mcp.server import Settings, build_app, main
from rapidata_mcp.token_verifier import JWTVerifier
from rapidata_mcp.tools import register_tools

__all__ = [
    "build_app",
    "main",
    "Settings",
    "register_tools",
    "JWTVerifier",
    "ClientProvider",
    "EnvClientProvider",
    "TokenClientProvider",
]
