"""OpenTelemetry traces for the MCP server.

Every tool call is one span carrying the tool, the caller and its outcome; every
Rapidata API call made during it is a child span whose ``traceparent`` the backend
continues, so one trace runs from the chat down to the backend's own spans.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from opentelemetry.sdk.trace import TracerProvider

SERVICE_NAME = "rapidata-mcp-service"

_tracer = trace.get_tracer("rapidata_mcp")
_client_header = "rapidata-mcp"

_JOB_ID_KEYS = ("job_id", "job_definition_id")


def configure_tracing(version: str) -> TracerProvider | None:
    """Export spans to ``OTEL_EXPORTER_OTLP_ENDPOINT`` (OTLP/gRPC); without it the
    spans stay no-ops."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {"service.name": SERVICE_NAME, "service.version": version}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def trace_api_calls(client_header: str) -> None:
    """Send ``X-Client: client_header`` and a ``traceparent`` on every SDK API call,
    each inside its own client span.

    Replaces the SDK's own ``call_api``: its tracing assumes the SDK's exporter, and
    with that off it fails on any call made under a recording span.
    """
    global _client_header
    _client_header = client_header

    from rapidata.api_client.api_client import ApiClient
    from rapidata.rapidata_client.api.rapidata_api_client import RapidataApiClient

    def call_api(
        self,
        method,
        url,
        header_params=None,
        body=None,
        post_params=None,
        _request_timeout=None,
    ):
        headers = dict(header_params or {})
        with _api_span(method, url, headers) as span:
            response = ApiClient.call_api(
                self, method, url, headers, body, post_params, _request_timeout
            )
            _record_status(span, response.status)
            return response

    RapidataApiClient.call_api = call_api


class TracedTransport(httpx.BaseTransport):
    """An httpx transport that traces each request like an SDK API call."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        with _api_span(request.method, url, request.headers) as span:
            response = self._inner.handle_request(request)
            _record_status(span, response.status_code)
            return response

    def close(self) -> None:
        self._inner.close()


def traced_http_client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=TracedTransport())


@contextmanager
def _api_span(
    method: str, url: str, headers: MutableMapping[str, str]
) -> Iterator[Span]:
    parts = urlsplit(url)
    method = str(method).upper()
    with _tracer.start_as_current_span(
        f"{method} {parts.path or '/'}",
        kind=SpanKind.CLIENT,
        attributes={
            "http.request.method": method,
            "url.full": f"{parts.scheme}://{parts.netloc}{parts.path}",
            "server.address": parts.hostname or "",
        },
    ) as span:
        headers["X-Client"] = _client_header
        propagate.inject(headers)
        yield span


def _record_status(span: Span, status: int) -> None:
    span.set_attribute("http.response.status_code", status)
    if status >= 400:
        span.set_status(Status(StatusCode.ERROR))


def trace_tool_calls(mcp: FastMCP) -> None:
    """Run every tool call inside a span; call after all tools are registered."""
    manager = mcp._tool_manager
    call_tool = manager.call_tool

    async def traced_call(name: str, arguments: dict[str, Any], *args, **kwargs):
        with _tracer.start_as_current_span(
            f"tools/call {name}",
            kind=SpanKind.SERVER,
            attributes={
                "mcp.method.name": "tools/call",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
            },
        ) as span:
            _set_caller(span, mcp)
            _set_job_ids(span, arguments)
            try:
                result = await call_tool(name, arguments, *args, **kwargs)
            except Exception as e:
                span.set_attribute("rapidata.mcp.outcome", "error")
                span.set_attribute("error.type", type(e).__name__)
                raise
            span.set_attribute("rapidata.mcp.outcome", "ok")
            if isinstance(result, tuple) and isinstance(result[-1], Mapping):
                _set_job_ids(span, result[-1])
            return result

    manager.call_tool = traced_call


def _set_caller(span: Span, mcp: FastMCP) -> None:
    from mcp.server.auth.middleware.auth_context import get_access_token

    access = get_access_token()
    if access is not None:
        span.set_attribute("enduser.id", access.subject or "")
        email = (access.claims or {}).get("email")
        if email:
            span.set_attribute("user.email", str(email))
    try:
        request = mcp._mcp_server.request_context.request
    except LookupError:
        return
    user_agent = request.headers.get("user-agent") if request is not None else None
    if user_agent:
        span.set_attribute("user_agent.original", user_agent)
        span.set_attribute("rapidata.mcp.client", user_agent.split("/", 1)[0])


def _set_job_ids(span: Span, values: Mapping[str, Any]) -> None:
    for key in _JOB_ID_KEYS:
        value = values.get(key)
        if isinstance(value, str) and value:
            span.set_attribute(f"rapidata.{key}", value)
