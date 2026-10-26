import asyncio

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.utilities.tests import asgi_client
from fastmcp.utilities.tests import asgi_server

from glpi_mcp import glpi_client
from glpi_mcp.auth import GlpiTokenVerifier
from glpi_mcp.auth import GlpiUnavailableError
from glpi_mcp.auth import auth_error_response
from glpi_mcp.auth import build_auth_provider
from glpi_mcp.auth import glpi_oauth_proxy_class
from glpi_mcp.models import GlpiConfig
from glpi_mcp.models import TransportConfig
from glpi_mcp.tools import register_tools

USER_TOKEN = "alice-glpi-token"  # noqa: S105
SESSION = {"user_id": 7, "name": "alice"}
OIDC_SETTINGS = {
    "oidc_config_url": "https://id.example.com/.well-known/openid-configuration",
    "oidc_client_id": "client-id",
    "oidc_client_secret": "client-secret",
    "oidc_base_url": "https://mcp.example.com",
}


@pytest.fixture
def passthrough_config(config) -> GlpiConfig:
    return config.model_copy(update={"token_passthrough": True})


@pytest.fixture
def login_config(config) -> GlpiConfig:
    return config.model_copy(
        update={
            "oauth_login": True,
            "oauth_base_url": "https://mcp.example.com",
            "username": None,
            "password": None,
        }
    )


def transport(kind: str, bearer: str | None = None, **oidc) -> TransportConfig:
    return TransportConfig(transport_type=kind, http_bearer_token=bearer, **oidc)


def auth_headers(glpi) -> list[str]:
    return [r.headers.get("Authorization", "") for r in glpi.api_requests()]


# ------------------------------------------------------------ verifier


async def test_verifier_accepts_token_glpi_accepts(passthrough_config, glpi):
    glpi.get("session", payload=SESSION)
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)

    token = await verifier.verify_token(USER_TOKEN)

    assert token is not None
    assert token.token == USER_TOKEN
    assert token.client_id == "user:7"
    assert auth_headers(glpi) == [f"Bearer {USER_TOKEN}"]
    # The caller's token is checked as is: no password grant.
    assert glpi.sent("POST", "/api.php/token") == []


async def test_verifier_caches_accepted_tokens(passthrough_config, glpi):
    glpi.get("session", payload=SESSION)
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    for _ in range(3):
        await verifier.verify_token(USER_TOKEN)
    assert len(glpi.sent("GET", "session")) == 1


INVALID_TOKEN = {
    "status": "ERROR_INVALID_PARAMETER",
    "title": "Invalid OAuth token",
    "detail": "Access token could not be verified",
}


@pytest.mark.parametrize(
    ("status", "payload"),
    [(401, {"status": "ERROR"}), (403, {"status": "ERROR"}), (400, INVALID_TOKEN)],
)
async def test_verifier_rejects_and_remembers(
    passthrough_config, glpi, status, payload
):
    glpi.get("session", status=status, payload=payload)
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    assert await verifier.verify_token(USER_TOKEN) is None
    assert await verifier.verify_token(USER_TOKEN) is None
    assert len(glpi.sent("GET", "session")) == 1


@pytest.mark.parametrize("token", ["", "  ", "tokén", "a\nb"])
async def test_verifier_rejects_malformed_token_locally(
    passthrough_config, glpi, token
):
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    assert await verifier.verify_token(token) is None
    assert glpi.requests == []


async def test_verifier_does_not_reject_on_other_400s(passthrough_config, glpi):
    glpi.get("session", status=400, payload={"status": "ERROR", "title": "Bad"})
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    with pytest.raises(GlpiUnavailableError):
        await verifier.verify_token(USER_TOKEN)


async def test_verifier_reports_glpi_errors_as_unavailable(passthrough_config, glpi):
    glpi.get("session", status=502, payload=None, headers={"Retry-After": "7"})
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    with pytest.raises(GlpiUnavailableError) as info:
        await verifier.verify_token(USER_TOKEN)
    assert info.value.retry_after == 7


async def test_verifier_does_not_accept_a_non_object_answer(passthrough_config, glpi):
    glpi.get("session", payload=["not", "a", "session"])
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    with pytest.raises(GlpiUnavailableError, match="GLPI_URL"):
        await verifier.verify_token(USER_TOKEN)


async def test_verifier_reports_a_glpi_without_the_api(passthrough_config, glpi):
    glpi.routes[("GET", "/api.php/")] = [(404, None, {})]
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    with pytest.raises(GlpiUnavailableError, match=r"11\.0\.6"):
        await verifier.verify_token(USER_TOKEN)


async def test_verifier_checks_concurrent_requests_once(passthrough_config, glpi):
    glpi.get("session", payload=SESSION)
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    tokens = await asyncio.gather(
        *(verifier.verify_token(USER_TOKEN) for _ in range(5))
    )
    assert all(t is not None for t in tokens)
    assert len(glpi.sent("GET", "session")) == 1


@pytest.mark.parametrize("user_id", [None, 0, -1, True, "7"])
async def test_client_id_needs_a_real_user_id(passthrough_config, glpi, user_id):
    glpi.get("session", payload={"user_id": user_id})
    verifier = GlpiTokenVerifier(passthrough_config, transport=glpi.transport)
    token = await verifier.verify_token(USER_TOKEN)
    assert token is not None
    assert token.client_id.startswith("token:")


async def test_verifier_grants_the_required_scopes(passthrough_config, glpi):
    glpi.get("session", payload=SESSION)
    verifier = GlpiTokenVerifier(
        passthrough_config, transport=glpi.transport, required_scopes=["api", "user"]
    )
    token = await verifier.verify_token(USER_TOKEN)
    assert token is not None
    assert token.scopes == ["api", "user"]


# ------------------------------------------------------------ provider


def test_provider_passthrough(passthrough_config):
    provider = build_auth_provider(passthrough_config, transport("http"))
    assert isinstance(provider, GlpiTokenVerifier)


@pytest.mark.parametrize("kind", ["stdio", "bogus"])
def test_provider_passthrough_needs_http(passthrough_config, kind):
    with pytest.raises(ValueError, match="MCP_TRANSPORT=http or sse"):
        build_auth_provider(passthrough_config, transport(kind))


def test_provider_passthrough_conflicts_with_bearer(passthrough_config):
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_auth_provider(passthrough_config, transport("http", bearer="shared"))


def test_provider_passthrough_conflicts_with_oidc(passthrough_config):
    with pytest.raises(ValueError, match="OIDC"):
        build_auth_provider(passthrough_config, transport("http", **OIDC_SETTINGS))


def test_provider_static_bearer(config):
    provider = build_auth_provider(config, transport("http", bearer="shared"))
    assert isinstance(provider, StaticTokenVerifier)


def test_provider_none(config):
    assert build_auth_provider(config, transport("http")) is None


class FakeProxy:
    """Stand-in for OIDCProxy/OAuthProxy, which reach out when constructed."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def fake_proxies(monkeypatch):
    monkeypatch.setattr("fastmcp.server.auth.oidc_proxy.OIDCProxy", FakeProxy)
    monkeypatch.setattr("fastmcp.server.auth.oauth_proxy.OAuthProxy", FakeProxy)


@pytest.mark.usefixtures("fake_proxies")
def test_provider_oidc_wins_over_bearer(config):
    provider = build_auth_provider(
        config, transport("http", bearer="shared", **OIDC_SETTINGS)
    )
    assert isinstance(provider, FakeProxy)
    assert provider.kwargs["config_url"] == OIDC_SETTINGS["oidc_config_url"]
    assert provider.kwargs["forward_resource"] is False


def test_partial_oidc_warns_and_falls_back(config, caplog):
    partial = {"oidc_config_url": "https://id.example.com", "oidc_client_id": "x"}
    with caplog.at_level("WARNING"):
        provider = build_auth_provider(
            config, transport("http", bearer="shared", **partial)
        )
    assert isinstance(provider, StaticTokenVerifier)
    assert "OIDC_CLIENT_SECRET, OIDC_BASE_URL" in caplog.text


@pytest.mark.usefixtures("fake_proxies")
def test_provider_glpi_login(login_config):
    provider = build_auth_provider(login_config, transport("http"))
    assert isinstance(provider, FakeProxy)
    kwargs = provider.kwargs
    assert kwargs["upstream_authorization_endpoint"] == (
        "https://glpi.test/api.php/authorize"
    )
    assert kwargs["upstream_token_endpoint"] == "https://glpi.test/api.php/token"  # noqa: S105
    assert kwargs["upstream_client_id"] == "client"
    assert kwargs["base_url"] == "https://mcp.example.com"
    assert kwargs["valid_scopes"] == ["api", "user"]
    assert isinstance(kwargs["token_verifier"], GlpiTokenVerifier)
    assert kwargs["token_verifier"].required_scopes == ["api", "user"]


@pytest.mark.usefixtures("fake_proxies")
def test_provider_glpi_login_needs_base_url(login_config):
    config = login_config.model_copy(update={"oauth_base_url": None})
    with pytest.raises(ValueError, match="GLPI_OAUTH_BASE_URL"):
        build_auth_provider(config, transport("http"))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bearer": "shared"}, "MCP_HTTP_BEARER_TOKEN"),
        (OIDC_SETTINGS, "OIDC"),
    ],
)
@pytest.mark.usefixtures("fake_proxies")
def test_provider_glpi_login_is_exclusive(login_config, kwargs, message):
    with pytest.raises(ValueError, match=message):
        build_auth_provider(login_config, transport("http", **kwargs))


@pytest.mark.usefixtures("fake_proxies")
def test_provider_glpi_login_needs_http(login_config):
    with pytest.raises(ValueError, match="MCP_TRANSPORT=http or sse"):
        build_auth_provider(login_config, transport("stdio"))


# ------------------------------------------------------------ tool calls


def fake_access_token(monkeypatch, token: str | None) -> None:
    value = AccessToken(token=token, client_id="user:7", scopes=[]) if token else None
    monkeypatch.setattr(glpi_client, "get_access_token", lambda: value)


@pytest.mark.parametrize("mode", ["passthrough", "login"])
async def test_tool_uses_callers_token(
    passthrough_config, login_config, glpi, client_for, monkeypatch, mode
):
    fake_access_token(monkeypatch, USER_TOKEN)
    glpi.get("session", payload=SESSION)
    glpi.get("Administration/User/Me", payload={"id": 7})
    config = passthrough_config if mode == "passthrough" else login_config

    async with client_for(config) as client:
        result = await client.call_tool("glpi_whoami", {})

    assert result.data["session"] == SESSION
    assert set(auth_headers(glpi)) == {f"Bearer {USER_TOKEN}"}
    assert glpi.sent("POST", "/api.php/token") == []


@pytest.mark.parametrize(
    ("mode", "message"),
    [("passthrough", "No GLPI token"), ("login", "No GLPI login")],
)
async def test_tool_without_token_never_uses_server_credentials(
    passthrough_config, login_config, glpi, client_for, monkeypatch, mode, message
):
    fake_access_token(monkeypatch, None)
    config = passthrough_config if mode == "passthrough" else login_config

    async with client_for(config) as client:
        with pytest.raises(ToolError, match=message):
            await client.call_tool("glpi_whoami", {})

    assert glpi.requests == []


# ------------------------------------------------------------- over HTTP


def http_server(config: GlpiConfig, glpi) -> FastMCP:
    mcp = FastMCP("test", auth=GlpiTokenVerifier(config, transport=glpi.transport))
    register_tools(mcp, config, glpi.transport)
    return mcp


async def test_http_request_runs_as_caller(passthrough_config, glpi):
    glpi.get("session", payload=SESSION)
    glpi.get("Administration/User/Me", payload={"id": 7})
    mcp = http_server(passthrough_config, glpi)

    async with asgi_client(
        mcp, headers={"Authorization": f"Bearer {USER_TOKEN}"}
    ) as client:
        result = await client.call_tool("glpi_whoami", {})

    assert result.data["user"] == {"id": 7}
    assert set(auth_headers(glpi)) == {f"Bearer {USER_TOKEN}"}


async def post_tools_list(mcp: FastMCP, headers: dict[str, str]) -> httpx2.Response:
    """Send a raw MCP request, to see the HTTP status the auth layer returns."""
    async with (
        asgi_server(mcp) as server,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=server.app), base_url="http://127.0.0.1"
        ) as http,
    ):
        return await http.post(
            "/mcp",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer "}, {"Authorization": "Bearer bad-token"}],
)
async def test_http_request_without_valid_token_is_rejected(
    passthrough_config, glpi, headers
):
    glpi.get("session", status=401, payload={"status": "ERROR"})
    response = await post_tools_list(http_server(passthrough_config, glpi), headers)
    assert response.status_code == 401
    assert glpi.sent("POST", "/api.php/token") == []


async def test_http_request_while_glpi_is_down_gets_503(passthrough_config, glpi):
    glpi.get("session", status=502, payload={"status": "ERROR"})
    response = await post_tools_list(
        http_server(passthrough_config, glpi),
        {"Authorization": f"Bearer {USER_TOKEN}"},
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"] == "temporarily_unavailable"


# ------------------------------------------------- GLPI login during an outage


class SwallowingProxy:
    """Mimics OAuthProxy.load_access_token: any verifier error means None."""

    def __init__(self, verifier: GlpiTokenVerifier):
        self.verifier = verifier

    async def load_access_token(self, token: str):
        try:
            return await self.verifier.verify_token(token)
        except Exception:  # this is what OAuthProxy does
            return None


async def test_login_proxy_reports_glpi_outage_as_unavailable(passthrough_config, glpi):
    glpi.get("session", status=502, payload={"status": "ERROR"})
    proxy_class = glpi_oauth_proxy_class(SwallowingProxy)
    proxy = proxy_class(GlpiTokenVerifier(passthrough_config, transport=glpi.transport))
    with pytest.raises(GlpiUnavailableError):
        await proxy.load_access_token(USER_TOKEN)


async def test_login_proxy_still_rejects_a_bad_token(passthrough_config, glpi):
    glpi.get("session", status=401, payload={"status": "ERROR"})
    proxy_class = glpi_oauth_proxy_class(SwallowingProxy)
    proxy = proxy_class(GlpiTokenVerifier(passthrough_config, transport=glpi.transport))
    assert await proxy.load_access_token(USER_TOKEN) is None


def test_real_login_proxy_installs_the_503_handler(login_config, tmp_path, monkeypatch):
    """Fails if a FastMCP upgrade changes OAuthProxy's auth middleware."""
    monkeypatch.setattr("fastmcp.settings.home", tmp_path)
    provider = build_auth_provider(login_config, transport("http"))
    assert provider is not None
    middleware = provider.get_middleware()
    handlers = [m.kwargs.get("on_error") for m in middleware if "on_error" in m.kwargs]
    assert handlers == [auth_error_response]
    assert type(provider).__mro__[1].__name__ == "OAuthProxy"
