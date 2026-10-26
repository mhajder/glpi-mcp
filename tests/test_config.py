"""Settings are read from the environment strictly: a typo stops the server."""

import pytest

from glpi_mcp.glpi_client import get_glpi_config_from_env
from glpi_mcp.glpi_client import get_transport_config_from_env
from glpi_mcp.models import GlpiConfig

# Every variable the config readers use, cleared so the developer's own
# environment cannot leak into these tests.
CONFIG_VARS = (
    "GLPI_URL",
    "GLPI_CLIENT_ID",
    "GLPI_CLIENT_SECRET",
    "GLPI_USERNAME",
    "GLPI_PASSWORD",
    "GLPI_SCOPES",
    "GLPI_API_VERSION",
    "GLPI_TOKEN_PASSTHROUGH",
    "GLPI_OAUTH_LOGIN",
    "GLPI_OAUTH_BASE_URL",
    "GLPI_OAUTH_REDIRECT_PATH",
    "GLPI_OAUTH_ALLOWED_REDIRECT_URIS",
    "GLPI_ENTITY",
    "GLPI_ENTITY_RECURSIVE",
    "GLPI_PROFILE",
    "GLPI_VERIFY_SSL",
    "GLPI_TIMEOUT",
    "READ_ONLY_MODE",
    "DRY_RUN_MODE",
    "DISABLED_TAGS",
    "RESPONSE_MAX_SIZE",
    "RATE_LIMIT_ENABLED",
    "RATE_LIMIT_MAX_REQUESTS",
    "RATE_LIMIT_WINDOW_MINUTES",
    "TOOL_SEARCH_ENABLED",
    "TOOL_SEARCH_STRATEGY",
    "TOOL_SEARCH_MAX_RESULTS",
    "MCP_TRANSPORT",
    "MCP_HTTP_HOST",
    "MCP_HTTP_PORT",
    "MCP_HTTP_BEARER_TOKEN",
    "OIDC_CONFIG_URL",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_BASE_URL",
    "OIDC_REDIRECT_PATH",
    "OIDC_REQUIRED_SCOPES",
    "OIDC_ALLOWED_REDIRECT_URIS",
    "OIDC_VERIFY_ID_TOKEN",
    "OIDC_FORWARD_RESOURCE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLPI_URL", "https://glpi.invalid")
    monkeypatch.setenv("GLPI_CLIENT_ID", "client")
    monkeypatch.setenv("GLPI_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GLPI_USERNAME", "glpi")
    monkeypatch.setenv("GLPI_PASSWORD", "glpi")


def test_defaults():
    config = get_glpi_config_from_env()
    assert config.api_version == "auto"
    assert config.scopes == ["api", "user"]
    assert config.verify_ssl is True
    assert config.timeout == 30
    assert config.missing_credentials() == []
    assert not config.per_user_tokens


@pytest.mark.parametrize(
    "name",
    [
        "GLPI_VERIFY_SSL",
        "GLPI_TOKEN_PASSTHROUGH",
        "GLPI_OAUTH_LOGIN",
        "GLPI_ENTITY_RECURSIVE",
        "READ_ONLY_MODE",
        "DRY_RUN_MODE",
        "RATE_LIMIT_ENABLED",
        "TOOL_SEARCH_ENABLED",
    ],
)
def test_unknown_bool_setting_stops_startup(monkeypatch, name):
    """A typo must not turn read-only mode or TLS verification off."""
    monkeypatch.setenv(name, "enabled")
    with pytest.raises(ValueError, match=f"{name} must be one of"):
        get_glpi_config_from_env()


def test_blank_settings_fall_back_to_defaults(monkeypatch):
    """A variable declared without a value means "unset", not a crash."""
    for name in ("GLPI_TIMEOUT", "GLPI_VERIFY_SSL", "READ_ONLY_MODE", "GLPI_ENTITY"):
        monkeypatch.setenv(name, "  ")
    config = get_glpi_config_from_env()
    assert config.timeout == 30
    assert config.verify_ssl is True
    assert config.read_only_mode is False
    assert config.entity_id is None


def test_bad_number_setting_names_the_variable(monkeypatch):
    monkeypatch.setenv("GLPI_TIMEOUT", "30s")
    with pytest.raises(ValueError, match="GLPI_TIMEOUT must be a number"):
        get_glpi_config_from_env()


def test_secrets_keep_their_spaces(monkeypatch):
    monkeypatch.setenv("GLPI_PASSWORD", " pass word ")
    monkeypatch.setenv("GLPI_CLIENT_SECRET", " s ")
    config = get_glpi_config_from_env()
    assert config.password == " pass word "  # noqa: S105
    assert config.client_secret == " s "  # noqa: S105


def test_whitespace_only_credentials_count_as_missing(monkeypatch):
    monkeypatch.setenv("GLPI_USERNAME", " ")
    monkeypatch.setenv("GLPI_PASSWORD", "   ")
    config = get_glpi_config_from_env()
    assert config.missing_credentials() == ["GLPI_USERNAME", "GLPI_PASSWORD"]


def test_passthrough_needs_no_credentials(monkeypatch):
    for name in ("GLPI_CLIENT_ID", "GLPI_CLIENT_SECRET", "GLPI_USERNAME"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("GLPI_TOKEN_PASSTHROUGH", "true")
    config = get_glpi_config_from_env()
    assert config.missing_credentials() == []
    assert config.per_user_tokens


def test_oauth_login_needs_client_and_base_url(monkeypatch):
    monkeypatch.delenv("GLPI_USERNAME")
    monkeypatch.delenv("GLPI_PASSWORD")
    monkeypatch.setenv("GLPI_OAUTH_LOGIN", "true")
    config = get_glpi_config_from_env()
    assert config.missing_credentials() == ["GLPI_OAUTH_BASE_URL"]
    monkeypatch.setenv("GLPI_OAUTH_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("GLPI_OAUTH_ALLOWED_REDIRECT_URIS", "https://a/*, https://b/*")
    config = get_glpi_config_from_env()
    assert config.missing_credentials() == []
    assert config.oauth_allowed_redirect_uris == ["https://a/*", "https://b/*"]


def test_passthrough_and_login_are_exclusive(monkeypatch):
    monkeypatch.setenv("GLPI_TOKEN_PASSTHROUGH", "true")
    monkeypatch.setenv("GLPI_OAUTH_LOGIN", "true")
    with pytest.raises(ValueError, match="mutually exclusive"):
        get_glpi_config_from_env()


@pytest.mark.parametrize(("raw", "expected"), [("3.0", "3.0"), (" 2.3 ", "2.3")])
def test_api_version_is_read(monkeypatch, raw, expected):
    monkeypatch.setenv("GLPI_API_VERSION", raw)
    assert get_glpi_config_from_env().api_version == expected


@pytest.mark.parametrize("raw", ["2.2", "v2", "latest"])
def test_unsupported_api_version_stops_startup(monkeypatch, raw):
    """GLPI routes an unknown version to its newest one, so it must be refused."""
    monkeypatch.setenv("GLPI_API_VERSION", raw)
    with pytest.raises(ValueError, match="GLPI_API_VERSION must be 'auto'"):
        get_glpi_config_from_env()


def test_scopes_accept_spaces_and_commas(monkeypatch):
    monkeypatch.setenv("GLPI_SCOPES", "api, user graphql")
    assert get_glpi_config_from_env().scopes == ["api", "user", "graphql"]


def test_entity_and_profile(monkeypatch):
    monkeypatch.setenv("GLPI_ENTITY", "0")
    monkeypatch.setenv("GLPI_ENTITY_RECURSIVE", "true")
    monkeypatch.setenv("GLPI_PROFILE", "4")
    config = get_glpi_config_from_env()
    assert (config.entity_id, config.entity_recursive, config.profile_id) == (
        0,
        True,
        4,
    )


def test_recursive_without_entity_stops_startup(monkeypatch):
    """GLPI ignores GLPI-Entity-Recursive alone, so the setting would do nothing."""
    monkeypatch.setenv("GLPI_ENTITY_RECURSIVE", "true")
    with pytest.raises(ValueError, match="GLPI_ENTITY is not"):
        get_glpi_config_from_env()


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_response_max_size_must_be_positive(monkeypatch, raw):
    monkeypatch.setenv("RESPONSE_MAX_SIZE", raw)
    with pytest.raises(ValueError, match="response_max_size"):
        get_glpi_config_from_env()


def test_disabled_tags_are_split(monkeypatch):
    monkeypatch.setenv("DISABLED_TAGS", " assets, ,knowledgebase ")
    assert get_glpi_config_from_env().disabled_tags == {"assets", "knowledgebase"}


@pytest.mark.parametrize("raw", ["", "   "])
def test_whitespace_bearer_token_is_no_token(monkeypatch, raw):
    """A blank token must not become the one accepted credential."""
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", raw)
    assert get_transport_config_from_env().http_bearer_token is None


def test_bearer_token_is_stripped(monkeypatch):
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", " secret ")
    assert get_transport_config_from_env().http_bearer_token == "secret"  # noqa: S105


def test_oidc_settings_are_read(monkeypatch):
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://id.example.com/.well-known/x")
    monkeypatch.setenv("OIDC_CLIENT_ID", "id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("OIDC_REQUIRED_SCOPES", "openid,profile")
    config = get_transport_config_from_env()
    assert config.oidc_enabled
    assert config.oidc_required_scopes == ["openid", "profile"]
    assert config.oidc_redirect_path == "/auth/callback"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RATE_LIMIT_MAX_REQUESTS", "0"),
        ("RATE_LIMIT_WINDOW_MINUTES", "abc"),
        ("TOOL_SEARCH_MAX_RESULTS", "-1"),
        ("TOOL_SEARCH_STRATEGY", "regx"),
    ],
)
def test_disabled_feature_ignores_its_settings(monkeypatch, name, value):
    """A leftover value for a feature that is off must not stop the server."""
    monkeypatch.setenv(name, value)
    config = get_glpi_config_from_env()
    assert config.rate_limit_max_requests == 60
    assert config.rate_limit_window_minutes == 1
    assert config.tool_search_max_results == 5
    assert config.tool_search_strategy == "bm25"


@pytest.mark.parametrize(
    ("enable", "name"),
    [
        ("RATE_LIMIT_ENABLED", "RATE_LIMIT_MAX_REQUESTS"),
        ("RATE_LIMIT_ENABLED", "RATE_LIMIT_WINDOW_MINUTES"),
        ("TOOL_SEARCH_ENABLED", "TOOL_SEARCH_MAX_RESULTS"),
    ],
)
def test_enabled_feature_rejects_zero(monkeypatch, enable, name):
    monkeypatch.setenv(enable, "true")
    monkeypatch.setenv(name, "0")
    with pytest.raises(ValueError, match=f"must be at least 1: {name}=0"):
        get_glpi_config_from_env()


def test_strategy_typo_is_rejected_when_tool_search_is_on(monkeypatch):
    monkeypatch.setenv("TOOL_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TOOL_SEARCH_STRATEGY", "regx")
    with pytest.raises(ValueError, match="TOOL_SEARCH_STRATEGY must be 'bm25' or"):
        get_glpi_config_from_env()


@pytest.mark.parametrize("timeout", ["0", "-5"])
def test_timeout_must_be_positive(monkeypatch, timeout):
    monkeypatch.setenv("GLPI_TIMEOUT", timeout)
    with pytest.raises(ValueError, match="timeout"):
        get_glpi_config_from_env()


@pytest.mark.parametrize("transport", [None, "stdio"])
def test_port_is_ignored_under_stdio(monkeypatch, transport):
    """`fastmcp run` imports the server; a leftover bad port must not stop it."""
    if transport is not None:
        monkeypatch.setenv("MCP_TRANSPORT", transport)
    monkeypatch.setenv("MCP_HTTP_PORT", "not-a-port")
    assert get_transport_config_from_env().http_port == 8000


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_port_is_checked_for_http_transports(monkeypatch, transport):
    monkeypatch.setenv("MCP_TRANSPORT", transport)
    monkeypatch.setenv("MCP_HTTP_PORT", "not-a-port")
    with pytest.raises(ValueError, match="MCP_HTTP_PORT must be an integer"):
        get_transport_config_from_env()


def test_model_defaults_match_env_defaults():
    """The env reader and the model agree on what an unset setting means."""
    assert GlpiConfig(glpi_url="https://x").scopes == ["api", "user"]
