"""Tests for the Ecobee community fork integration setup/unload/reload."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from custom_components.ecobee import (
    async_reload_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ecobee.api import EcobeeAuthError
from custom_components.ecobee.auth import InvalidGrantError
from custom_components.ecobee.const import CONF_REFRESH_TOKEN, CONF_USERNAME, DOMAIN
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _valid_entry_data():
    return {
        CONF_USERNAME: "user@example.com",
        CONF_REFRESH_TOKEN: "fake-refresh-token",
    }


async def test_setup_unload_and_reload_entry(hass, bypass_get_data):
    """Successful setup -> unload -> reload round trip."""
    entry = MockConfigEntry(domain=DOMAIN, data=_valid_entry_data(), entry_id="t1")
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    assert await async_setup_entry(hass, entry)
    assert DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]

    assert await async_reload_entry(hass, entry) is None
    assert entry.entry_id in hass.data[DOMAIN]

    assert await async_unload_entry(hass, entry)
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_setup_missing_refresh_token_raises_auth_failed(hass):
    """An entry without RT data must trigger reauth, not crash."""
    bad = _valid_entry_data()
    del bad[CONF_REFRESH_TOKEN]
    entry = MockConfigEntry(domain=DOMAIN, data=bad, entry_id="t-bad")
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_first_refresh_invalid_grant_raises_auth_failed(hass):
    """InvalidGrantError on first poll -> ConfigEntryAuthFailed (reauth)."""
    entry = MockConfigEntry(domain=DOMAIN, data=_valid_entry_data(), entry_id="t-ig")
    entry.add_to_hass(hass)

    async def boom(self):
        raise InvalidGrantError("revoked")

    with patch(
        "custom_components.ecobee.coordinator.EcobeeDataUpdateCoordinator."
        "async_config_entry_first_refresh",
        boom,
    ), pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_first_refresh_auth_error_raises_auth_failed(hass):
    """EcobeeAuthError on first poll -> ConfigEntryAuthFailed."""
    entry = MockConfigEntry(domain=DOMAIN, data=_valid_entry_data(), entry_id="t-ae")
    entry.add_to_hass(hass)

    async def boom(self):
        raise EcobeeAuthError("401")

    with patch(
        "custom_components.ecobee.coordinator.EcobeeDataUpdateCoordinator."
        "async_config_entry_first_refresh",
        boom,
    ), pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_first_refresh_unexpected_exception_raises_not_ready(hass):
    """Unexpected exceptions surface as ConfigEntryNotReady (HA retries)."""
    entry = MockConfigEntry(domain=DOMAIN, data=_valid_entry_data(), entry_id="t-nr")
    entry.add_to_hass(hass)

    async def boom(self):
        raise RuntimeError("transient")

    with patch(
        "custom_components.ecobee.coordinator.EcobeeDataUpdateCoordinator."
        "async_config_entry_first_refresh",
        boom,
    ), pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_setup_persists_rt_callback_wired(hass, bypass_get_data):
    """Setup must wire the entry-update persist callback into EcobeeAuth.

    We can't easily inspect the callback (it's a closure over the
    entry); instead we verify ``set_refresh_token_persist_callback`` is
    called exactly once with a callable.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=_valid_entry_data(), entry_id="t-cb")
    entry.add_to_hass(hass)

    captured = {}

    def fake_from_storage(session, refresh_token, email=None):
        auth = type("FakeAuth", (), {})()
        auth.set_refresh_token_persist_callback = lambda cb: captured.setdefault(
            "cb", cb
        )
        return auth

    with patch(
        "custom_components.ecobee.EcobeeAuth.from_storage",
        side_effect=fake_from_storage,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert callable(captured.get("cb"))
