from starlette.testclient import TestClient

from rapidata_mcp.server import Settings, build_app


def test_openai_apps_challenge_is_the_bare_token_without_auth():
    resp = TestClient(build_app(Settings())).get("/.well-known/openai-apps-challenge")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "lyipPhM1qYtibkjt83SF6f-5Am3_fYLSH-ZGRpAorUw"
