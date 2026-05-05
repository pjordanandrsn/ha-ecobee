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
  4. Register the ecobee.create_vacation / ecobee.delete_vacation
     services on first setup (no-op on subsequent setups).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import aiohttp_client, config_validation as cv, entity_platform
from homeassistant.helpers.entity_component import EntityComponent

from .api import EcobeeApiClient, EcobeeApiError, EcobeeAuthError
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

    # Register the vacation services exactly once across all entries.
    if not hass.services.has_service(DOMAIN, "create_vacation"):
        _register_vacation_services(hass)

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


# ─── vacation services ────────────────────────────────────────────────
#
# These are domain-level services (not entity-bound) for two reasons:
#   1. The `target` block in services.yaml maps the supplied entity_id
#      → its parent thermostat identifier without the entity needing to
#      define the service itself.
#   2. A vacation modifies an ecobee schedule object that's tied to the
#      whole thermostat, not specifically to the climate entity — same
#      device, but the climate entity is one of several surfaces.

_CREATE_VACATION_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=100)),
        vol.Optional("start_datetime"): cv.datetime,
        vol.Required("end_datetime"): cv.datetime,
        vol.Optional("heat_temperature", default=60): vol.All(
            vol.Coerce(float), vol.Range(min=45, max=95)
        ),
        vol.Optional("cool_temperature", default=85): vol.All(
            vol.Coerce(float), vol.Range(min=60, max=99)
        ),
        vol.Optional("fan_mode", default="auto"): vol.In(["auto", "on"]),
    },
    extra=vol.ALLOW_EXTRA,  # HA injects target.* keys we don't care about
)

_DELETE_VACATION_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=100)),
    },
    extra=vol.ALLOW_EXTRA,
)


def _register_vacation_services(hass: HomeAssistant) -> None:
    """Register the two vacation services. Called exactly once."""

    async def _resolve_thermostats(
        entity_ids: list[str],
    ) -> list[tuple[EcobeeApiClient, str]]:
        """Map climate entity_ids → (api_client, thermostat_identifier).

        We walk hass.data[DOMAIN] (a dict of coordinators keyed by config
        entry id) and check each coordinator's data for the matching
        identifier. Climate entities have unique_id of the form
        ``<identifier>-thermostat``; we don't rely on that suffix
        though, just iterate and find by entity_id ↔ device.
        """
        ent_reg = hass.helpers.entity_registry.async_get(hass)  # type: ignore[attr-defined]
        # Newer HA: `entity_registry.async_get(hass)` is the canonical call
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        out: list[tuple[EcobeeApiClient, str]] = []
        for entity_id in entity_ids:
            ent = registry.async_get(entity_id)
            if ent is None:
                raise HomeAssistantError(f"Entity not found: {entity_id}")
            if ent.platform != DOMAIN:
                raise HomeAssistantError(
                    f"{entity_id} is not an ecobee entity (platform={ent.platform})"
                )
            # unique_id format: '<identifier>-thermostat' (per
            # EcobeeThermostat.unique_id in climate.py).
            if not ent.unique_id or "-" not in ent.unique_id:
                raise HomeAssistantError(
                    f"{entity_id} has no thermostat identifier in unique_id"
                )
            identifier = ent.unique_id.rsplit("-", 1)[0]
            # Find which coordinator owns this thermostat.
            for coord in hass.data[DOMAIN].values():
                if not isinstance(coord, EcobeeDataUpdateCoordinator):
                    continue
                for t in coord.data or []:
                    if t.get("identifier") == identifier:
                        out.append((coord.api, identifier))
                        break
        return out

    async def _handle_create_vacation(call: ServiceCall) -> None:
        data = _CREATE_VACATION_SCHEMA(dict(call.data))
        targets = await _resolve_thermostats(data["entity_id"])
        if not targets:
            raise HomeAssistantError("No matching ecobee thermostat for service call")

        # Default start = now if omitted; end is required by the schema.
        start_dt: datetime = data.get("start_datetime") or datetime.now()
        end_dt: datetime = data["end_datetime"]
        if end_dt <= start_dt:
            raise HomeAssistantError(
                f"end_datetime ({end_dt.isoformat()}) must be after start_datetime "
                f"({start_dt.isoformat()})"
            )

        params = dict(
            name=data["name"],
            start_date=start_dt.strftime("%Y-%m-%d"),
            start_time=start_dt.strftime("%H:%M:%S"),
            end_date=end_dt.strftime("%Y-%m-%d"),
            end_time=end_dt.strftime("%H:%M:%S"),
            heat_hold_temp_f10=int(round(float(data["heat_temperature"]) * 10)),
            cool_hold_temp_f10=int(round(float(data["cool_temperature"]) * 10)),
            fan=data["fan_mode"],
        )
        for api, identifier in targets:
            try:
                await api.async_create_vacation(identifier, **params)
            except EcobeeApiError as ex:
                raise HomeAssistantError(
                    f"Create vacation failed for thermostat {identifier}: {ex}"
                ) from ex

        # Force a refresh so the climate entity's vacation attribute updates.
        for coord in hass.data[DOMAIN].values():
            if isinstance(coord, EcobeeDataUpdateCoordinator):
                await coord.async_request_refresh()

    async def _handle_delete_vacation(call: ServiceCall) -> None:
        data = _DELETE_VACATION_SCHEMA(dict(call.data))
        targets = await _resolve_thermostats(data["entity_id"])
        if not targets:
            raise HomeAssistantError("No matching ecobee thermostat for service call")
        name = data["name"]
        for api, identifier in targets:
            try:
                await api.async_delete_vacation(identifier, name=name)
            except EcobeeApiError as ex:
                raise HomeAssistantError(
                    f"Delete vacation failed for thermostat {identifier}: {ex}"
                ) from ex
        for coord in hass.data[DOMAIN].values():
            if isinstance(coord, EcobeeDataUpdateCoordinator):
                await coord.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "create_vacation",
        _handle_create_vacation,
        schema=_CREATE_VACATION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        "delete_vacation",
        _handle_delete_vacation,
        schema=_DELETE_VACATION_SCHEMA,
    )
