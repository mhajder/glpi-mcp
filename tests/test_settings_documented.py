"""Every setting read from the environment must be documented."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# glpi_client.py reads the settings; utils.py holds the transport helpers
# shared with the container healthcheck; server.py and sentry_init.py read the
# server and Sentry settings.
SOURCES = [
    (ROOT / "src" / "glpi_mcp" / name).read_text()
    for name in ("glpi_client.py", "utils.py", "server.py", "sentry_init.py")
]
# Settings are read with os.getenv, the env_* helpers in utils.py, or the
# _csv and _optional_int helpers in glpi_client.py.
ENV_VAR_READ = re.compile(
    r'(?:os\.getenv|env_[a-z]+|_csv|_optional_int)\(\s*"([A-Z_]+)"'
)
ENV_VARS = sorted({name for src in SOURCES for name in ENV_VAR_READ.findall(src)})


def test_env_vars_found():
    assert "GLPI_URL" in ENV_VARS
    assert "GLPI_VERIFY_SSL" in ENV_VARS
    assert "OIDC_CONFIG_URL" in ENV_VARS
    assert "RATE_LIMIT_MAX_REQUESTS" in ENV_VARS
    assert "DRY_RUN_MODE" in ENV_VARS
    assert "MCP_HTTP_PORT" in ENV_VARS


@pytest.mark.parametrize("name", ENV_VARS)
def test_env_var_in_env_example(name):
    assert name in (ROOT / ".env.example").read_text()


@pytest.mark.parametrize("name", ENV_VARS)
def test_env_var_in_readme(name):
    assert name in (ROOT / "README.md").read_text()


# The registry manifest configures a local install; binding, HTTP auth and the
# optional Sentry extra are left to the operator, as in the MCP registry examples.
REGISTRY_EXCLUDED = {
    "MCP_HTTP_HOST",
    "MCP_HTTP_BEARER_TOKEN",
    "GLPI_TOKEN_PASSTHROUGH",
    "GLPI_OAUTH_LOGIN",
    "GLPI_OAUTH_BASE_URL",
    "GLPI_OAUTH_REDIRECT_PATH",
    "GLPI_OAUTH_ALLOWED_REDIRECT_URIS",
} | {name for name in ENV_VARS if name.startswith(("OIDC_", "SENTRY_"))}


@pytest.mark.parametrize("name", sorted(set(ENV_VARS) - REGISTRY_EXCLUDED))
def test_env_var_in_server_json(name):
    assert name in (ROOT / "server.json").read_text()
