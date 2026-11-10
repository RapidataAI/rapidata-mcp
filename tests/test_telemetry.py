import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, format_span_id, format_trace_id

from rapidata_mcp.telemetry import TracedTransport, trace_api_calls, trace_tool_calls

_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans() -> list[ReadableSpan]:
    _EXPORTER.clear()
    return _EXPORTER.get_finished_spans  # type: ignore[return-value]


def _traceparent(span: ReadableSpan) -> str:
    context = span.get_span_context()
    assert context is not None
    trace_id = format_trace_id(context.trace_id)
    flags = f"{context.trace_flags:02x}"
    return f"00-{trace_id}-{format_span_id(context.span_id)}-{flags}"


def _server(sent: list[httpx.Request]) -> FastMCP:
    mcp = FastMCP("test")

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "job_9"})

    http = httpx.Client(transport=TracedTransport(httpx.MockTransport(handler)))

    @mcp.tool()
    def get_job_status(job_id: str) -> dict[str, Any]:
        http.get(f"https://api.example.test/job/{job_id}?x=1")
        return {"job_id": job_id, "status": "Running"}

    @mcp.tool()
    def start_job(job_definition_id: str) -> dict[str, Any]:
        raise ValueError("unknown audience")

    trace_tool_calls(mcp)
    return mcp


def test_a_tool_call_is_one_span_and_its_api_call_a_linked_child(spans):
    sent: list[httpx.Request] = []
    mcp = _server(sent)
    user = AuthenticatedUser(
        AccessToken(
            token="t",
            client_id="claude",
            scopes=[],
            subject="customer-1",
            claims={"email": "ada@example.test"},
        )
    )
    reset = auth_context_var.set(user)
    try:
        asyncio.run(mcp.call_tool("get_job_status", {"job_id": "job_9"}))
    finally:
        auth_context_var.reset(reset)

    api, tool = spans()
    assert tool.name == "tools/call get_job_status"
    assert tool.kind == SpanKind.SERVER
    expected = {
        "gen_ai.tool.name": "get_job_status",
        "enduser.id": "customer-1",
        "user.email": "ada@example.test",
        "rapidata.mcp.outcome": "ok",
        "rapidata.job_id": "job_9",
    }
    assert expected.items() <= dict(tool.attributes or {}).items()
    assert api.name == "GET /job/job_9"
    assert api.kind == SpanKind.CLIENT
    assert api.parent is not None and api.parent.span_id == tool.context.span_id
    assert api.attributes is not None
    assert api.attributes["url.full"] == "https://api.example.test/job/job_9"
    assert api.attributes["http.response.status_code"] == 200
    assert sent[0].headers["traceparent"] == _traceparent(api)
    assert sent[0].headers["X-Client"].startswith("rapidata-mcp")


def test_a_failed_tool_call_records_the_error(spans):
    mcp = _server([])

    with pytest.raises(ToolError, match="unknown audience"):
        asyncio.run(mcp.call_tool("start_job", {"job_definition_id": "jd_1"}))

    (tool,) = spans()
    assert tool.attributes is not None
    assert tool.attributes["rapidata.mcp.outcome"] == "error"
    assert tool.attributes["error.type"] == "ToolError"
    assert tool.attributes["rapidata.job_definition_id"] == "jd_1"


def test_sdk_api_calls_carry_the_trace_and_the_client(spans, monkeypatch):
    from rapidata.api_client.api_client import ApiClient
    from rapidata.rapidata_client.api.rapidata_api_client import RapidataApiClient

    sent: list[dict[str, str]] = []

    def fake_call_api(self, method, url, header_params, *rest):
        sent.append(header_params)
        return SimpleNamespace(status=404)

    monkeypatch.setattr(ApiClient, "call_api", fake_call_api)
    monkeypatch.setattr(RapidataApiClient, "call_api", RapidataApiClient.call_api)
    trace_api_calls("rapidata-mcp/sha-1234567")
    client = RapidataApiClient.__new__(RapidataApiClient)

    with trace.get_tracer("test").start_as_current_span("tool"):
        response = client.call_api("GET", "https://api.example.test/job/j?a=b", {})

    assert response.status == 404
    api, tool = spans()
    assert api.parent is not None and api.parent.span_id == tool.context.span_id
    assert api.status.is_ok is False
    assert sent == [
        {"X-Client": "rapidata-mcp/sha-1234567", "traceparent": _traceparent(api)}
    ]
