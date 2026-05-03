"""Sensor platform for the Ecobee community fork integration.

We expose two flavors of sensor:

1.  Per-remote-sensor temperature / humidity. ecobee returns one
    ``capability`` array per remote sensor; ``type`` is ``temperature``
    or ``humidity`` (occupancy lives in the binary_sensor platform).
    These are the SmartThings-bypass payoff: the SmartThings cloud
    only forwards the thermostat-level reading, hiding the room
    sensors. We surface them as
    ``sensor.ecobee_<room>_temperature`` / ``sensor.ecobee_<room>_humidity``.

2.  Thermostat-level extras (current outdoor temperature from the
    weather block, and HVAC equipment running state).

The unique_id pattern matches HA core's choice (sensor['code']-<class>
for SmartSensors, identifier-id-<class> for built-in thermostat
sensors) so a future migration off this fork onto the core integration
keeps existing entity_ids stable.
"""

from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EcobeeDataUpdateCoordinator
from .entity import (
    EcobeeBaseEntity,
    remote_sensor_device_info,
    thermostat_device_info,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Build sensor entities for every remote sensor + thermostat-level series."""
    coordinator: EcobeeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    for thermostat in coordinator.data or []:
        identifier = thermostat.get("identifier")
        if not identifier:
            continue

        for sensor in thermostat.get("remoteSensors") or []:
            sensor_id = sensor.get("id")
            if not sensor_id:
                continue
            for capability in sensor.get("capability") or []:
                cap_type = capability.get("type")
                if cap_type == "temperature":
                    entities.append(
                        EcobeeRemoteSensorTemperature(
                            coordinator, identifier, sensor_id
                        )
                    )
                elif cap_type == "humidity":
                    entities.append(
                        EcobeeRemoteSensorHumidity(
                            coordinator, identifier, sensor_id
                        )
                    )

        # Thermostat-level series.
        weather = thermostat.get("weather") or {}
        forecasts = weather.get("forecasts") or []
        if forecasts:
            entities.append(EcobeeOutdoorTemperature(coordinator, identifier))

    async_add_entities(entities)


def _ecobee_temp_to_native(raw: Any) -> Optional[float]:
    """Convert ecobee's tenths-of-degree integer to a float in F.

    ecobee returns temperatures as int in tenths of a Fahrenheit degree
    (e.g. 705 = 70.5 F). The literal string ``"unknown"`` is also a
    valid value when a sensor is calibrating or offline.
    """
    if raw is None or raw == "unknown":
        return None
    try:
        return float(raw) / 10.0
    except (TypeError, ValueError):
        return None


def _find_remote_sensor(
    thermostat: dict[str, Any], sensor_id: str
) -> Optional[dict[str, Any]]:
    for s in thermostat.get("remoteSensors") or []:
        if s.get("id") == sensor_id:
            return s
    return None


def _find_capability(sensor: dict[str, Any], cap_type: str) -> Optional[dict[str, Any]]:
    for c in sensor.get("capability") or []:
        if c.get("type") == cap_type:
            return c
    return None


class _RemoteSensorMixin(EcobeeBaseEntity):
    """Shared behaviour for any per-remote-sensor entity."""

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
    def device_info(self) -> Optional[DeviceInfo]:
        t = self.thermostat
        s = self._sensor
        if t is None or s is None:
            return None
        return remote_sensor_device_info(t, s)


class EcobeeRemoteSensorTemperature(_RemoteSensorMixin, SensorEntity):
    """Temperature reading from a single ecobee remote (or thermostat) sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    @property
    def name(self) -> str:
        return "Temperature"

    @property
    def unique_id(self) -> Optional[str]:
        s = self._sensor
        if s is None:
            return None
        # Match HA core's pattern: SmartSensors get the printed code,
        # built-in thermostat sensors get id+identifier so they're
        # stable across rename.
        if s.get("code"):
            return f"{s['code']}-temperature"
        return f"{self._thermostat_identifier}-{self._sensor_id}-temperature"

    @property
    def native_value(self) -> Optional[float]:
        s = self._sensor
        if s is None:
            return None
        cap = _find_capability(s, "temperature")
        if cap is None:
            return None
        return _ecobee_temp_to_native(cap.get("value"))


class EcobeeRemoteSensorHumidity(_RemoteSensorMixin, SensorEntity):
    """Humidity reading from a remote sensor (only some support this)."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def name(self) -> str:
        return "Humidity"

    @property
    def unique_id(self) -> Optional[str]:
        s = self._sensor
        if s is None:
            return None
        if s.get("code"):
            return f"{s['code']}-humidity"
        return f"{self._thermostat_identifier}-{self._sensor_id}-humidity"

    @property
    def native_value(self) -> Optional[float]:
        s = self._sensor
        if s is None:
            return None
        cap = _find_capability(s, "humidity")
        if cap is None:
            return None
        try:
            return float(cap.get("value"))
        except (TypeError, ValueError):
            return None


class EcobeeOutdoorTemperature(EcobeeBaseEntity, SensorEntity):
    """Outdoor temperature from the thermostat's weather block.

    ecobee's weather forecast comes from a third-party feed; the value
    in ``weather.forecasts[0].temperature`` is in tenths of a degree
    Fahrenheit (same convention as the runtime block).
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    @property
    def name(self) -> str:
        return "Outdoor temperature"

    @property
    def unique_id(self) -> Optional[str]:
        return f"{self._thermostat_identifier}-outdoor-temperature"

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        t = self.thermostat
        if t is None:
            return None
        return thermostat_device_info(t)

    @property
    def native_value(self) -> Optional[float]:
        t = self.thermostat
        if t is None:
            return None
        forecasts = (t.get("weather") or {}).get("forecasts") or []
        if not forecasts:
            return None
        return _ecobee_temp_to_native(forecasts[0].get("temperature"))
