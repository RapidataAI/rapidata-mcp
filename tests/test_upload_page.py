from starlette.testclient import TestClient

from rapidata_mcp.server import Settings, build_app


def _get(path: str, **settings) -> object:
    return TestClient(build_app(Settings(**settings))).get(path)


def test_upload_page_is_served_without_auth():
    resp = _get("/upload/ups_1Zk-o0_H")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert '<meta name="rapidata-api" content="https://api.rapidata.ai">' in resp.text
    policy = resp.headers["content-security-policy"]
    assert "connect-src https://api.rapidata.ai;" in policy
    assert "script-src 'sha256-" in policy
    assert "frame-ancestors 'none'" in policy
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


def test_upload_page_talks_to_the_configured_environment():
    resp = _get("/upload/ups_1", environment="rabbitdata.ch")

    assert 'content="https://api.rabbitdata.ch"' in resp.text
    policy = resp.headers["content-security-policy"]
    assert "connect-src https://api.rabbitdata.ch;" in policy


def test_upload_page_rejects_malformed_session_ids():
    assert _get("/upload/ups%20x").status_code == 404
    assert _get("/upload/ups_1%3Cscript%3E").status_code == 404
