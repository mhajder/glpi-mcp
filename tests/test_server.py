"""In-process tests for server.py's module-level setup and main().

server.py configures everything at import time, so each test imports a fresh
copy under a controlled environment. The subprocess tests in
test_healthcheck.py run the same module end to end, but coverage does not
see them.
"""

import importlib
import importlib.metadata
import logging
import sys
from unittest.mock import MagicMock

import dotenv
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware.rate_limiting import SlidingWindowRateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search import RegexSearchTransform

from glpi_mcp.auth import GlpiTokenVerifier
from glpi_mcp.auth import rate_limit_client_id
from tests.test_config import CONFIG_VARS

MODULE = "glpi_mcp.server"

SERVER_ENV_VARS = (*CONFIG_VARS, "LOG_LEVEL", "SENTRY_DSN")

OIDC_ENV = {
    "OIDC_CONFIG_URL": "https://id.example.com/.well-known/openid-configuration",
    "OIDC_CLIENT_ID": "client-id",
    "OIDC_CLIENT_SECRET": "client-secret",
    "OIDC_BASE_URL": "https://mcp.example.com",
}


@pytest.fixture
def load_server(monkeypatch, tmp_path):
    """Import a fresh server module with only the given environment."""
    for name in SERVER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # A developer's .env must not leak into the tests.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    # The GLPI login's OAuthProxy keeps its client store under the home dir.
    monkeypatch.setattr("fastmcp.settings.home", tmp_path)

    def factory(**env: str):
        env.setdefault("GLPI_URL", "https://glpi.test")
        env.setdefault("GLPI_CLIENT_ID", "client")
        env.setdefault("GLPI_CLIENT_SECRET", "secret")
        env.setdefault("GLPI_USERNAME", "glpi")
        env.setdefault("GLPI_PASSWORD", "glpi")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        sys.modules.pop(MODULE, None)
        module = importlib.import_module(MODULE)
        monkeypatch.setattr(module.mcp, "run", MagicMock())
        return module

    yield factory
    sys.modules.pop(MODULE, None)


@pytest.fixture
def fake_oidc(monkeypatch):
    """Stand in for OIDCProxy, which fetches the discovery document when built."""
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return StaticTokenVerifier(tokens={})

    monkeypatch.setattr("fastmcp.server.auth.oidc_proxy.OIDCProxy", factory)
    return calls


async def tool_names(module) -> set[str]:
    return {tool.name for tool in await module.mcp.list_tools()}


# ------------------------------------------------------------ module setup


async def test_health_route_answers_ok(load_server):
    server = load_server()

    response = await server.health(MagicMock())

    assert response.status_code == 200
    assert response.body == b"ok"


async def test_all_tools_are_exposed_by_default(load_server):
    names = await tool_names(load_server())

    assert {"glpi_search_itil", "glpi_create_itil", "glpi_delete_item"} <= names
    assert "search_tools" not in names


async def test_read_only_mode_hides_write_tools(load_server, caplog):
    caplog.set_level(logging.INFO)
    names = await tool_names(load_server(READ_ONLY_MODE="true"))

    assert {"glpi_search_itil", "glpi_get_item"} <= names
    assert "glpi_create_itil" not in names
    assert "glpi_delete_item" not in names
    assert "Read-only mode is enabled" in caplog.text


async def test_disabled_tags_hide_matching_tools(load_server, caplog):
    caplog.set_level(logging.INFO)
    names = await tool_names(load_server(DISABLED_TAGS="knowledgebase, documents"))

    assert "glpi_search_kb_articles" not in names
    assert "glpi_download_document" not in names
    assert "glpi_search_itil" in names
    assert "Disabled tags configured" in caplog.text


@pytest.mark.parametrize(
    ("strategy", "transform"),
    [("bm25", BM25SearchTransform), ("regex", RegexSearchTransform)],
)
async def test_tool_search_replaces_the_tool_list(
    load_server, caplog, strategy, transform
):
    caplog.set_level(logging.INFO)
    server = load_server(
        TOOL_SEARCH_ENABLED="true",
        TOOL_SEARCH_STRATEGY=strategy,
        TOOL_SEARCH_MAX_RESULTS="3",
    )

    [search] = [t for t in server.mcp.transforms if isinstance(t, transform)]
    assert search._max_results == 3
    assert await tool_names(server) == {"search_tools", "call_tool"}
    assert f"strategy={strategy}, max_results=3" in caplog.text


def test_rate_limiting_adds_middleware(load_server):
    server = load_server(
        RATE_LIMIT_ENABLED="true",
        RATE_LIMIT_MAX_REQUESTS="10",
        RATE_LIMIT_WINDOW_MINUTES="5",
    )

    [limiter] = [
        m
        for m in server.mcp.middleware
        if isinstance(m, SlidingWindowRateLimitingMiddleware)
    ]
    assert limiter.max_requests == 10
    assert limiter.window_seconds == 300
    assert limiter.get_client_id is rate_limit_client_id


def test_optional_middleware_is_off_by_default(load_server):
    server = load_server()

    assert not any(
        isinstance(m, SlidingWindowRateLimitingMiddleware | ResponseLimitingMiddleware)
        for m in server.mcp.middleware
    )


def test_response_size_limit_adds_middleware(load_server, caplog):
    caplog.set_level(logging.INFO)
    server = load_server(RESPONSE_MAX_SIZE="2048")

    [limiter] = [
        m for m in server.mcp.middleware if isinstance(m, ResponseLimitingMiddleware)
    ]
    assert limiter.max_size == 2048
    assert "max 2048 bytes" in caplog.text


def test_unknown_log_level_falls_back_to_info(load_server, caplog):
    load_server(LOG_LEVEL="chatty")

    assert "Unknown LOG_LEVEL 'CHATTY'" in caplog.text


def test_lowercase_log_level_is_accepted(load_server, caplog):
    load_server(LOG_LEVEL=" debug ")

    assert "Unknown LOG_LEVEL" not in caplog.text


def test_version_falls_back_when_package_is_not_installed(load_server, monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing)

    assert load_server().__version__ == "0.0.1"


def test_version_is_the_installed_package_version(load_server):
    assert load_server().__version__ == importlib.metadata.version("glpi-mcp-server")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {"TOOL_SEARCH_ENABLED": "true", "TOOL_SEARCH_STRATEGY": "regx"},
            "TOOL_SEARCH_STRATEGY",
        ),
        ({"READ_ONLY_MODE": "maybe"}, "READ_ONLY_MODE must be one of"),
        # Per-user tokens need an HTTP transport.
        ({"GLPI_TOKEN_PASSTHROUGH": "true"}, "MCP_TRANSPORT=http or sse"),
    ],
)
def test_invalid_configuration_is_logged_and_raised(load_server, caplog, env, message):
    with pytest.raises(ValueError, match=message):
        load_server(**env)

    assert "Invalid configuration" in caplog.text


@pytest.mark.parametrize(
    ("env", "expected", "absent"),
    [
        ({}, None, ("READ-ONLY", "DRY-RUN")),
        ({"READ_ONLY_MODE": "true"}, "READ-ONLY mode", ("DRY-RUN",)),
        ({"DRY_RUN_MODE": "true"}, "DRY-RUN mode", ("READ-ONLY",)),
        # Read-only wins: no write tool exists to simulate.
        (
            {"READ_ONLY_MODE": "true", "DRY_RUN_MODE": "true"},
            "READ-ONLY mode",
            ("DRY-RUN",),
        ),
    ],
)
def test_instructions_describe_the_safety_mode(load_server, env, expected, absent):
    instructions = load_server(**env).mcp.instructions

    assert instructions.startswith("This MCP server exposes tools for the GLPI")
    if expected:
        assert expected in instructions
    for word in absent:
        assert word not in instructions


# ------------------------------------------------------------ authentication


def test_no_authentication_by_default(load_server):
    server = load_server()

    assert server.mcp.auth is None
    assert server.auth_label() is None


def test_bearer_token_enables_auth(load_server):
    server = load_server(MCP_HTTP_BEARER_TOKEN="secret")

    assert isinstance(server.mcp.auth, StaticTokenVerifier)
    assert server.auth_label() == "bearer token"


def test_token_passthrough_verifies_tokens_with_glpi(load_server):
    server = load_server(MCP_TRANSPORT="http", GLPI_TOKEN_PASSTHROUGH="true")

    assert isinstance(server.mcp.auth, GlpiTokenVerifier)
    assert server.auth_label() == "per-user GLPI token passthrough"


def test_glpi_login_brokers_to_glpi(load_server):
    server = load_server(
        MCP_TRANSPORT="http",
        GLPI_OAUTH_LOGIN="true",
        GLPI_OAUTH_BASE_URL="https://mcp.example.com",
    )

    assert type(server.mcp.auth).__name__ == "GlpiOAuthProxy"
    assert server.auth_label() == "GLPI OAuth login (each user signs in with GLPI)"


def test_oidc_brokers_to_the_identity_provider(load_server, fake_oidc):
    server = load_server(MCP_TRANSPORT="http", **OIDC_ENV)

    assert [call["config_url"] for call in fake_oidc] == [OIDC_ENV["OIDC_CONFIG_URL"]]
    assert server.auth_label() == "OIDC"


# ------------------------------------------------------------ main()


def test_main_runs_stdio_by_default(load_server, caplog):
    caplog.set_level(logging.INFO)
    server = load_server()

    server.main()

    server.mcp.run.assert_called_once_with()
    assert "Using STDIO transport" in caplog.text
    assert (
        "connecting to https://glpi.test (API version: auto, modes: read-write)"
        in caplog.text
    )
    assert "WITHOUT authentication" not in caplog.text


@pytest.mark.parametrize(
    ("env", "modes"),
    [
        ({"READ_ONLY_MODE": "true"}, "read-only"),
        ({"DRY_RUN_MODE": "true"}, "dry-run"),
        ({"READ_ONLY_MODE": "true", "DRY_RUN_MODE": "true"}, "read-only, dry-run"),
    ],
)
def test_main_logs_the_active_modes(load_server, caplog, env, modes):
    caplog.set_level(logging.INFO)
    server = load_server(GLPI_API_VERSION="2.3", **env)

    server.main()

    assert f"(API version: 2.3, modes: {modes})" in caplog.text


@pytest.mark.parametrize(
    ("transport", "expected", "kind"),
    [
        ("http", "http", "Streamable"),
        ("streamable-http", "http", "Streamable"),
        ("sse", "sse", "SSE"),
    ],
)
def test_main_runs_http_transports_on_the_configured_address(
    load_server, caplog, transport, expected, kind
):
    caplog.set_level(logging.INFO)
    server = load_server(
        MCP_TRANSPORT=transport,
        MCP_HTTP_HOST="192.0.2.10",
        MCP_HTTP_PORT="9001",
        MCP_HTTP_BEARER_TOKEN="secret",
    )

    server.main()

    server.mcp.run.assert_called_once_with(
        transport=expected, host="192.0.2.10", port=9001
    )
    assert f"Using HTTP {kind} transport on 192.0.2.10:9001" in caplog.text
    assert f"Authentication for {kind} transport: bearer token" in caplog.text
    assert "WITHOUT authentication" not in caplog.text


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_main_warns_when_http_runs_without_auth(load_server, caplog, transport):
    caplog.set_level(logging.INFO)
    server = load_server(MCP_TRANSPORT=transport)

    server.main()

    server.mcp.run.assert_called_once()
    assert "WITHOUT authentication" in caplog.text
    assert "Authentication for" not in caplog.text


def test_main_rejects_an_unknown_transport(load_server, caplog):
    server = load_server(MCP_TRANSPORT="websocket")

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 1
    assert "Unknown MCP_TRANSPORT 'websocket'" in caplog.text
    assert "sse, stdio, streamable-http" in caplog.text
    server.mcp.run.assert_not_called()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"GLPI_URL": ""}, "Missing required GLPI URL (GLPI_URL)"),
        (
            {"GLPI_URL": "glpi.example.com"},
            "GLPI_URL must start with https:// or http:// (got 'glpi.example.com')",
        ),
        ({"GLPI_URL": "ftp://glpi.example.com"}, "must start with https://"),
        (
            {"GLPI_PASSWORD": "", "GLPI_CLIENT_SECRET": ""},
            "Missing GLPI OAuth settings: GLPI_CLIENT_SECRET, GLPI_PASSWORD",
        ),
    ],
)
def test_main_stops_on_an_unusable_glpi_setup(load_server, caplog, env, message):
    server = load_server(**env)

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 1
    assert message in caplog.text
    server.mcp.run.assert_not_called()


def test_main_warns_about_plaintext_http(load_server, caplog):
    server = load_server(GLPI_URL="HTTP://glpi.example.com")

    server.main()

    assert "GLPI_URL uses plaintext http://" in caplog.text
    server.mcp.run.assert_called_once_with()


def test_main_accepts_https_without_a_plaintext_warning(load_server, caplog):
    server = load_server()

    server.main()

    assert "plaintext" not in caplog.text


def test_main_warns_that_server_credentials_are_unused_with_per_user_tokens(
    load_server, caplog
):
    caplog.set_level(logging.INFO)
    server = load_server(MCP_TRANSPORT="http", GLPI_TOKEN_PASSTHROUGH="true")

    server.main()

    assert "GLPI_USERNAME/GLPI_PASSWORD are ignored" in caplog.text
    assert (
        "Authentication for Streamable transport: per-user GLPI token passthrough"
        in caplog.text
    )
    server.mcp.run.assert_called_once()


def test_main_per_user_tokens_need_no_server_credentials(load_server, caplog):
    server = load_server(
        MCP_TRANSPORT="http",
        GLPI_TOKEN_PASSTHROUGH="true",
        GLPI_CLIENT_ID="",
        GLPI_CLIENT_SECRET="",
        GLPI_USERNAME="",
        GLPI_PASSWORD="",
    )

    server.main()

    assert "are ignored" not in caplog.text
    assert "Missing GLPI OAuth settings" not in caplog.text
    server.mcp.run.assert_called_once()
