"""DataUpdateCoordinator for the Ecobee community fork integration.

We poll once per ``CONF_SCAN_INTERVAL`` (default 5 min) and stash the
raw thermostatList. Entity classes pull from ``coordinator.data`` and
do their own per-thermostat / per-sensor lookups.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EcobeeApiClient, EcobeeApiError, EcobeeAuthError
from .auth import InvalidGrantError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class EcobeeDataUpdateCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Class to manage fetching ecobee data via REST."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EcobeeApiClient,
        config_entry: ConfigEntry,
    ) -> None:
        self.hass = hass
        self.api = client
        self._config_entry = config_entry

        configured = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        # Floor at MIN_SCAN_INTERVAL — ecobee documents 3 min as the
        # minimum poll cadence for a single thermostat. Going below this
        # invites rate-limit errors and offers no real-time benefit
        # (their cloud doesn't push faster than that anyway).
        scan_seconds = max(int(configured), MIN_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_seconds),
        )

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Fetch the latest thermostatList."""
        try:
            thermostats = await self.api.async_get_thermostats()
            _LOGGER.debug("ecobee poll succeeded: %d thermostat(s)", len(thermostats))
            return thermostats
        except (EcobeeAuthError, InvalidGrantError) as ex:
            _LOGGER.warning("ecobee auth rejected, requesting reauth: %s", ex)
            raise ConfigEntryAuthFailed(str(ex)) from ex
        except EcobeeApiError as ex:
            _LOGGER.warning("ecobee API error: %s", ex)
            raise UpdateFailed(f"ecobee API error: {ex}") from ex
        except Exception as ex:  # noqa: BLE001
            _LOGGER.exception("Unexpected error refreshing ecobee data")
            raise UpdateFailed(str(ex)) from ex
