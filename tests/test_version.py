from importlib import metadata

import pytest
from starlette.testclient import TestClient

from rapidata_mcp.server import Settings, build_app, server_version

SDK = metadata.version("rapidata")
VERSION = f"2026.11.12.1640-0a5dfdf · rapidata {SDK}"


@pytest.fixture
def image_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAPIDATA_MCP_IMAGE_TAG", "2026.11.12.1640-0a5dfdf")


def test_version_is_the_image_tag_and_the_sdk(image_tag):
    assert server_version() == VERSION


@pytest.mark.parametrize("tag", [None, "", "  "])
def test_version_outside_the_image_is_dev(monkeypatch, tag):
    if tag is None:
        monkeypatch.delenv("RAPIDATA_MCP_IMAGE_TAG", raising=False)
    else:
        monkeypatch.setenv("RAPIDATA_MCP_IMAGE_TAG", tag)

    assert server_version() == f"dev · rapidata {SDK}"


def test_health_reports_the_version(image_tag):
    resp = TestClient(build_app(Settings(auth_disabled=True))).get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": VERSION}


def test_initialize_reports_the_version(image_tag):
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    with TestClient(build_app(Settings(auth_disabled=True))) as client:
        resp = client.post(
            "/mcp",
            json=initialize,
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["serverInfo"] == {
        "name": "rapidata",
        "version": VERSION,
        "websiteUrl": "https://docs.rapidata.ai/",
        "icons": [
            {
                "src": "https://docs.rapidata.ai/media/rapidata.svg",
                "mimeType": "image/svg+xml",
                "sizes": ["any"],
            }
        ],
    }
    assert result["instructions"].endswith(f"\n\nServer version: {VERSION}.")


def test_tools_list_logs_the_user_agent(caplog):
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    with TestClient(build_app(Settings(auth_disabled=True))) as client:
        with caplog.at_level("INFO", logger="rapidata_mcp.server"):
            resp = client.post(
                "/mcp",
                json=tools_list,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "User-Agent": "openai-mcp/1.0.0",
                    "Mcp-Protocol-Version": "2025-11-25",
                },
            )

    assert resp.status_code == 200
    assert len(resp.json()["result"]["tools"]) == 11
    assert "tools/list from openai-mcp/1.0.0" in caplog.messages
