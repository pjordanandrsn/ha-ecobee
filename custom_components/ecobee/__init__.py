"""Ecobee community fork — top-level setup.

This integration shadows the HA core ``ecobee`` integration so we can
use Resource Owner Password Grant against ecobee's Auth0 tenant, which
the core integration doesn't support (it still expects a dev-portal
API key — registration of which ecobee shut down in 2024).

Setup pattern:
  1. Pull email + refresh_token out of entry.data.
  2. Build an EcobeeAuth from storage and wire the persist callback so
     Auth0 RT rotation gets written back into the ConfigEntry.
  3. Build the API client + coordinator, do the first refresh, and
     forward to the platform setups.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client

from .api import EcobeeApiClient, EcobeeAuthError
from .auth import EcobeeAuth, InvalidGrantError
from .const import CONF_REFRESH_TOKEN, CONF_USERNAME, DOMAIN, PLATFORMS
from .coordinator import EcobeeDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a ConfigEntry."""
    hass.data.setdefault(DOMAIN, {})

    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    email = entry.data.get(CONF_USERNAME)
    if not refresh_token:
        # Either a v1->v2 migration with stripped data, or somehow the
        # credentials were lost. Either way, prompt for reauth.
        raise ConfigEntryAuthFailed("Missing refresh token")

    session = aiohttp_client.async_get_clientsession(hass)
    auth = EcobeeAuth.from_storage(session, refresh_token, email=email)

    async def _persist_rt(new_rt: str) -> None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: new_rt}
        )

    auth.set_refresh_token_persist_callback(_persist_rt)

    client = EcobeeApiClient(session, auth)
    coordinator = EcobeeDataUpdateCoordinator(
        hass, client=client, config_entry=entry
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except (EcobeeAuthError, InvalidGrantError) as ex:
        raise ConfigEntryAuthFailed(str(ex)) from ex
    except (ConfigEntryAuthFailed, ConfigEntryNotReady):
        # Coordinator raises the right one — don't double-wrap.
        raise
    except Exception as ex:  # noqa: BLE001
        raise ConfigEntryNotReady from ex

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down the integration on entry removal/reload."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Defensive default: if a previous reload already popped the
        # coordinator (e.g. mid-reconfigure race), don't KeyError.
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after the entry's data/options changed."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
