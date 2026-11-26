"""Tests for outbound User-Agent configuration and header injection."""

import pytest

from glpi_mcp.glpi_client import DEFAULT_USER_AGENT
from glpi_mcp.glpi_client import GlpiClient
from glpi_mcp.glpi_client import get_glpi_config_from_env
from tests.conftest import API_VERSIONS_12
from tests.conftest import FakeGlpi


def test_env_user_agent_defaults_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLPI_URL", "https://glpi.test")
    monkeypatch.setenv("GLPI_CLIENT_ID", "client")
    monkeypatch.setenv("GLPI_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GLPI_USERNAME", "glpi")
    monkeypatch.setenv("GLPI_PASSWORD", "glpi")
    monkeypatch.delenv("GLPI_USER_AGENT", raising=False)

    assert get_glpi_config_from_env().user_agent is None

    monkeypatch.setenv("GLPI_USER_AGENT", "  custom-glpi/1.0  ")
    assert get_glpi_config_from_env().user_agent == "custom-glpi/1.0"

    monkeypatch.setenv("GLPI_USER_AGENT", "   ")
    assert get_glpi_config_from_env().user_agent is None


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, DEFAULT_USER_AGENT),
        ("custom-glpi/1.0", "custom-glpi/1.0"),
    ],
)
async def test_outbound_requests_send_user_agent(
    config, configured: str | None, expected: str
) -> None:
    config.user_agent = configured
    glpi = FakeGlpi(API_VERSIONS_12, version="3.0")
    glpi.get("Assistance/Ticket/1", payload={"id": 1})
    async with GlpiClient(config, transport=glpi.transport) as client:
        assert await client.get("Assistance/Ticket/1") == {"id": 1}

    assert glpi.requests
    assert all(req.headers.get("User-Agent") == expected for req in glpi.requests)
