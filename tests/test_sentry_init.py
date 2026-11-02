import sys
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from glpi_mcp.sentry_init import init_sentry


@pytest.fixture
def fake_sentry(monkeypatch):
    """A stand-in sentry_sdk, so the test records init() without sending anything."""
    sdk = MagicMock()
    integrations = MagicMock()
    mcp = MagicMock()
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    monkeypatch.delenv("SENTRY_SEND_DEFAULT_PII", raising=False)
    with patch.dict(
        sys.modules,
        {
            "sentry_sdk": sdk,
            "sentry_sdk.integrations": integrations,
            "sentry_sdk.integrations.mcp": mcp,
        },
    ):
        yield sdk.init


def test_sentry_sends_no_pii_unless_asked(fake_sentry):
    """Setting only SENTRY_DSN shipped tool arguments and results - devices,
    addresses, contacts - to a third party without anyone opting in."""
    assert init_sentry() is True
    assert fake_sentry.call_args.kwargs["send_default_pii"] is False


def test_sentry_pii_can_still_be_opted_into(fake_sentry, monkeypatch):
    monkeypatch.setenv("SENTRY_SEND_DEFAULT_PII", "true")
    init_sentry()
    assert fake_sentry.call_args.kwargs["send_default_pii"] is True


def test_sentry_is_off_without_a_dsn(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "   ")
    with patch.dict(sys.modules, {"sentry_sdk": MagicMock()}) as modules:
        assert init_sentry() is False
        modules["sentry_sdk"].init.assert_not_called()


def test_missing_sentry_sdk_warns_and_stays_off(monkeypatch, caplog):
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    # A None entry makes the import raise ImportError.
    with patch.dict(sys.modules, {"sentry_sdk": None}):
        assert init_sentry() is False
    assert "Sentry SDK not installed" in caplog.text


def test_sentry_settings_are_passed_through(fake_sentry, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.25")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setenv("SENTRY_RELEASE", "1.2.3")
    monkeypatch.setenv("SENTRY_PROFILE_SESSION_SAMPLE_RATE", "0.5")
    monkeypatch.setenv("SENTRY_PROFILE_LIFECYCLE", "MANUAL")
    monkeypatch.setenv("SENTRY_ENABLE_LOGS", "false")

    assert init_sentry() is True

    kwargs = fake_sentry.call_args.kwargs
    assert kwargs["dsn"] == "https://key@example.invalid/1"
    assert kwargs["traces_sample_rate"] == 0.25
    assert kwargs["environment"] == "staging"
    assert kwargs["release"] == "1.2.3"
    assert kwargs["profile_session_sample_rate"] == 0.5
    assert kwargs["profile_lifecycle"] == "manual"
    assert kwargs["enable_logs"] is False
    assert "traces_sample_rate=0.25, environment=staging" in caplog.text


def test_unknown_profile_lifecycle_means_trace(fake_sentry, monkeypatch):
    monkeypatch.setenv("SENTRY_PROFILE_LIFECYCLE", "continuous")
    init_sentry()
    assert fake_sentry.call_args.kwargs["profile_lifecycle"] == "trace"


def test_release_is_none_when_the_package_is_not_installed(fake_sentry, monkeypatch):
    import importlib.metadata

    def missing(_name):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    monkeypatch.setattr(importlib.metadata, "version", missing)
    init_sentry()
    assert fake_sentry.call_args.kwargs["release"] is None


def test_release_defaults_to_the_installed_package_version(fake_sentry, monkeypatch):
    import importlib.metadata

    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    init_sentry()
    assert fake_sentry.call_args.kwargs["release"] == importlib.metadata.version(
        "glpi-mcp-server"
    )


def test_invalid_setting_is_logged_not_raised(fake_sentry, monkeypatch, caplog):
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "lots")
    assert init_sentry() is False
    fake_sentry.assert_not_called()
    assert "Failed to initialize Sentry" in caplog.text


def test_init_failure_is_logged_not_raised(fake_sentry, caplog):
    fake_sentry.side_effect = RuntimeError("bad dsn")
    assert init_sentry() is False
    assert "Failed to initialize Sentry: bad dsn" in caplog.text
