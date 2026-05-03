"""Binary sensor platform for the Ecobee Anderson fork integration.

We expose one occupancy binary_sensor per remote sensor — the other
half of the SmartThings-bypass payoff (SmartThings hides per-room
occupancy as well as temperature).
"""

from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EcobeeDataUpdateCoordinator
from .entity import EcobeeBaseEntity, remote_sensor_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Build occupancy entities for every remote sensor that reports it."""
    coordinator: EcobeeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []

    for thermostat in coordinator.data or []:
        identifier = thermostat.get("identifier")
        if not identifier:
            continue
        for sensor in thermostat.get("remoteSensors") or []:
            sensor_id = sensor.get("id")
            if not sensor_id:
                continue
            for capability in sensor.get("capability") or []:
                if capability.get("type") == "occupancy":
                    entities.append(
                        EcobeeOccupancyBinarySensor(coordinator, identifier, sensor_id)
                    )
                    break  # one occupancy per sensor

    async_add_entities(entities)


def _find_remote_sensor(
    thermostat: dict[str, Any], sensor_id: str
) -> Optional[dict[str, Any]]:
    for s in thermostat.get("remoteSensors") or []:
        if s.get("id") == sensor_id:
            return s
    return None


class EcobeeOccupancyBinarySensor(EcobeeBaseEntity, BinarySensorEntity):
    """Occupancy reading from a single ecobee remote sensor."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(
        self,
        coordinator: EcobeeDataUpdateCoordinator,
        thermostat_identifier: str,
        sensor_id: str,
    ) -> None:
        super().__init__(coordinator, thermostat_identifier)
        self._sensor_id = sensor_id

    @property
    def _sensor(self) -> Optional[dict[str, Any]]:
        t = self.thermostat
        if t is None:
            return None
        return _find_remote_sensor(t, self._sensor_id)

    @property
    def name(self) -> str:
        return "Occupancy"

    @property
    def unique_id(self) -> Optional[str]:
        s = self._sensor
        if s is None:
            return None
        if s.get("code"):
            return f"{s['code']}-occupancy"
        return f"{self._thermostat_identifier}-{self._sensor_id}-occupancy"

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        t = self.thermostat
        s = self._sensor
        if t is None or s is None:
            return None
        return remote_sensor_device_info(t, s)

    @property
    def is_on(self) -> Optional[bool]:
        s = self._sensor
        if s is None:
            return None
        for cap in s.get("capability") or []:
            if cap.get("type") == "occupancy":
                # ecobee returns the literal strings "true"/"false" for
                # binary capabilities. The string "unknown" means the
                # sensor is calibrating and we should report unavailable.
                value = cap.get("value")
                if value in (True, "true"):
                    return True
                if value in (False, "false"):
                    return False
                return None
        return None
