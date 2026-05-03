"""Tests for EcobeeDataUpdateCoordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.ecobee.api import EcobeeApiError, EcobeeAuthError
from custom_components.ecobee.auth import InvalidGrantError
from custom_components.ecobee.const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from custom_components.ecobee.coordinator import EcobeeDataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed


def _entry_with_options(options: dict | None = None):
    entry = MagicMock()
    entry.options = options or {}
    return entry


async def test_coordinator_init(hass):
    """Default scan interval is DEFAULT_SCAN_INTERVAL."""
    client = MagicMock()
    coordinator = EcobeeDataUpdateCoordinator(hass, client, _entry_with_options())
    assert coordinator.hass is hass
    assert coordinator.api is client
    assert coordinator.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def test_coordinator_honors_scan_interval_option(hass):
    """A configured scan_interval >= MIN overrides the default."""
    coordinator = EcobeeDataUpdateCoordinator(
        hass, MagicMock(), _entry_with_options({CONF_SCAN_INTERVAL: 600})
    )
    assert coordinator.update_interval == timedelta(seconds=600)


async def test_coordinator_floors_at_min_scan_interval(hass):
    """Below-minimum requests get clamped to MIN_SCAN_INTERVAL.

    ecobee documents 3-min as the minimum poll cadence; we silently
    floor rather than hard-error so misconfiguration doesn't keep
    HA from starting up.
    """
    coordinator = EcobeeDataUpdateCoordinator(
        hass, MagicMock(), _entry_with_options({CONF_SCAN_INTERVAL: 30})
    )
    assert coordinator.update_interval == timedelta(seconds=MIN_SCAN_INTERVAL)


async def test_coordinator_update_data_success(hass):
    """Happy path returns the thermostat list."""
    client = MagicMock()
    client.async_get_thermostats = AsyncMock(
        return_value=[{"identifier": "1234567890", "name": "Main"}]
    )
    coordinator = EcobeeDataUpdateCoordinator(hass, client, _entry_with_options())
    data = await coordinator._async_update_data()
    assert data == [{"identifier": "1234567890", "name": "Main"}]


async def test_coordinator_invalid_grant_raises_auth_failed(hass):
    """An InvalidGrantError from the auth handle triggers reauth."""
    client = MagicMock()
    client.async_get_thermostats = AsyncMock(side_effect=InvalidGrantError("revoked"))
    coordinator = EcobeeDataUpdateCoordinator(hass, client, _entry_with_options())
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_auth_error_raises_auth_failed(hass):
    """An EcobeeAuthError from the API client also triggers reauth."""
    client = MagicMock()
    client.async_get_thermostats = AsyncMock(side_effect=EcobeeAuthError("401"))
    coordinator = EcobeeDataUpdateCoordinator(hass, client, _entry_with_options())
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_api_error_surfaces_update_failed(hass):
    """A 5xx or malformed response surfaces as UpdateFailed (entities go unavail)."""
    client = MagicMock()
    client.async_get_thermostats = AsyncMock(side_effect=EcobeeApiError("503"))
    coordinator = EcobeeDataUpdateCoordinator(hass, client, _entry_with_options())
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_unexpected_exception_surfaces_update_failed(hass):
    """Bare Exception still becomes UpdateFailed (no crash, no AuthFailed)."""
    client = MagicMock()
    client.async_get_thermostats = AsyncMock(side_effect=Exception("boom"))
    coordinator = EcobeeDataUpdateCoordinator(hass, client, _entry_with_options())
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
