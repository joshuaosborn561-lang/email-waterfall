from __future__ import annotations

import pytest

from email_waterfall import config
from email_waterfall.vendors.getleads import reset_getleads_pace


@pytest.fixture(autouse=True)
def _reset_getleads_pace() -> None:
    reset_getleads_pace()


def _set_phone_lookups(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # Env var so mcp_server._reload_settings() keeps the value too.
    monkeypatch.setenv("PHONE_LOOKUPS_ENABLED", value)
    monkeypatch.setattr(config, "settings", config.load_settings())


@pytest.fixture(autouse=True)
def _phone_lookups_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts on the production default: PHONE_LOOKUPS_ENABLED=false."""
    _set_phone_lookups(monkeypatch, "false")


@pytest.fixture
def phone_lookups_on(
    monkeypatch: pytest.MonkeyPatch, _phone_lookups_default_off: None
) -> None:
    """Emergency-rollback mode: legacy phone behavior (pre-1.7.0)."""
    _set_phone_lookups(monkeypatch, "true")
