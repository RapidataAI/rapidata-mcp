import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from rapidata_mcp import auth
from rapidata_mcp.auth import ExchangedTokenProvider, TokenExchangeError, TokenExchanger

ISSUER = "https://auth.example.test"
TOKEN_ENDPOINT = f"{ISSUER}/connect/token"
API_RESOURCE = "https://api.example.test"
MCP_TOKEN = "mcp-token"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(auth.time, "time", fake.time)
    return fake


@pytest.fixture
def api_clients(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import rapidata

    built: list[dict] = []

    def fake_client(**kwargs):
        built.append(kwargs)
        return kwargs

    monkeypatch.setattr(rapidata, "RapidataClient", fake_client)
    return built


@pytest.fixture
def exchanger() -> TokenExchanger:
    return TokenExchanger(
        ISSUER, "rapidata-mcp-server", API_RESOURCE, client_secret="s3cret"
    )


def _issue(tokens: list[str], expires_in: int = 3600):
    issued = iter(tokens)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": next(issued),
                "token_type": "Bearer",
                "expires_in": expires_in,
            },
        )

    return handler


@respx.mock
def test_exchange_request_shape(exchanger, clock, api_clients):
    route = respx.post(TOKEN_ENDPOINT).mock(side_effect=_issue(["api-1"]))

    ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()

    request = route.calls.last.request
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": MCP_TOKEN,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "resource": API_RESOURCE,
        "scope": "api",
    }
    assert (
        request.headers["authorization"]
        == httpx.BasicAuth("rapidata-mcp-server", "s3cret")._auth_header
    )


@respx.mock
def test_exchanges_once_per_token_and_api_never_sees_mcp_token(
    exchanger, clock, api_clients
):
    route = respx.post(TOKEN_ENDPOINT).mock(side_effect=_issue(["api-1", "api-2"]))

    ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()
    ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()

    assert route.call_count == 1
    assert [c["token"]["access_token"] for c in api_clients] == ["api-1", "api-1"]
    assert MCP_TOKEN not in json.dumps(api_clients)


@respx.mock
def test_distinct_subject_tokens_are_cached_separately(exchanger, clock, api_clients):
    route = respx.post(TOKEN_ENDPOINT).mock(side_effect=_issue(["api-a", "api-b"]))

    ExchangedTokenProvider(exchanger, "mcp-a").get_client()
    ExchangedTokenProvider(exchanger, "mcp-b").get_client()

    assert route.call_count == 2
    assert [c["token"]["access_token"] for c in api_clients] == ["api-a", "api-b"]


@respx.mock
def test_expiry_refreshes(exchanger, clock, api_clients):
    route = respx.post(TOKEN_ENDPOINT).mock(side_effect=_issue(["api-1", "api-2"]))

    ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()
    clock.now += 3600 - 61
    ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()
    assert route.call_count == 1

    clock.now += 2
    ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()

    assert route.call_count == 2
    assert api_clients[-1]["token"]["access_token"] == "api-2"
    assert api_clients[-1]["token"]["expires_at"] == int(clock.now + 3600)


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (400, "invalid_grant", "Re-authenticate"),
        (400, "unauthorized_client", "server configuration problem"),
        (400, "invalid_scope", "server configuration problem"),
        (400, "invalid_target", "server configuration problem"),
        (401, "invalid_client", "server configuration problem"),
    ],
)
@respx.mock
def test_refused_exchange_is_an_auth_error(
    exchanger, clock, api_clients, status, error, message
):
    respx.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(
            status, json={"error": error, "error_description": "nope"}
        )
    )

    with pytest.raises(TokenExchangeError, match=f"Not authorized.*{error}: nope") as e:
        ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()
    assert message in str(e.value)
    assert api_clients == []


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@respx.mock
def test_assertion_file_replaces_the_secret(tmp_path, clock, api_clients):
    assertion = tmp_path / "token"
    assertion.write_text("sa-jwt-1\n")
    route = respx.post(TOKEN_ENDPOINT).mock(side_effect=_issue(["api-1"]))
    exchanger = TokenExchanger(
        ISSUER,
        "rapidata-mcp-server",
        API_RESOURCE,
        client_assertion_file=str(assertion),
        client_secret="s3cret",
    )

    ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()

    request = route.calls.last.request
    form = _form(request)
    assert form["client_id"] == "rapidata-mcp-server"
    assert form["client_assertion_type"] == (
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )
    assert form["client_assertion"] == "sa-jwt-1"
    assert "client_secret" not in form
    assert "authorization" not in request.headers
    assert "s3cret" not in request.content.decode()


@respx.mock
def test_assertion_file_is_reread_after_rotation(tmp_path, clock, api_clients):
    assertion = tmp_path / "token"
    assertion.write_text("sa-jwt-1")
    route = respx.post(TOKEN_ENDPOINT).mock(side_effect=_issue(["api-1", "api-2"]))
    exchanger = TokenExchanger(
        ISSUER,
        "rapidata-mcp-server",
        API_RESOURCE,
        client_assertion_file=str(assertion),
    )

    ExchangedTokenProvider(exchanger, "mcp-a").get_client()
    assertion.write_text("sa-jwt-2")
    ExchangedTokenProvider(exchanger, "mcp-b").get_client()

    assert [_form(c.request)["client_assertion"] for c in route.calls] == [
        "sa-jwt-1",
        "sa-jwt-2",
    ]


def test_unreadable_assertion_file_is_an_exchange_error(tmp_path, clock, api_clients):
    exchanger = TokenExchanger(
        ISSUER,
        "rapidata-mcp-server",
        API_RESOURCE,
        client_assertion_file=str(tmp_path / "missing"),
    )

    with pytest.raises(TokenExchangeError, match="cannot read client assertion"):
        ExchangedTokenProvider(exchanger, MCP_TOKEN).get_client()
    assert api_clients == []


def test_factory_without_secret_forwards(monkeypatch, caplog):
    from rapidata_mcp import server

    monkeypatch.setattr(
        server,
        "get_access_token",
        lambda: type("A", (), {"token": "t", "expires_at": 1})(),
    )
    factory = server._token_provider_factory(server.Settings(client_secret=None))
    assert isinstance(factory(), auth.TokenClientProvider)
    assert "RAPIDATA_MCP_CLIENT_ASSERTION_FILE" in caplog.text


def test_factory_with_secret_exchanges(monkeypatch):
    from rapidata_mcp import server

    monkeypatch.setattr(
        server,
        "get_access_token",
        lambda: type("A", (), {"token": "t", "expires_at": 1})(),
    )
    factory = server._token_provider_factory(server.Settings(client_secret="s"))
    assert isinstance(factory(), ExchangedTokenProvider)


def test_settings_read_the_assertion_file(monkeypatch):
    from rapidata_mcp import server

    monkeypatch.setenv(
        "RAPIDATA_MCP_CLIENT_ASSERTION_FILE", "/var/run/secrets/rapidata/identity/token"
    )
    monkeypatch.delenv("RAPIDATA_MCP_CLIENT_SECRET", raising=False)
    settings = server.Settings.from_env()
    assert settings.client_assertion_file == "/var/run/secrets/rapidata/identity/token"
    assert settings.client_secret is None
