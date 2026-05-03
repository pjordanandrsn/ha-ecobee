"""Common helpers / base classes for ecobee entities.

We keep the inheritance shallow on purpose: each platform module
defines its own concrete entity class that knows exactly how to map
from the raw ecobee thermostat dict to its native_value. The shared
helpers here are mostly device_info construction (so all entities for a
given thermostat group correctly in the device registry).
"""

from __future__ import annotations

from typing import Any, Optional

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ECOBEE_MODEL_TO_NAME, MANUFACTURER
from .coordinator import EcobeeDataUpdateCoordinator


def find_thermostat(
    coordinator: EcobeeDataUpdateCoordinator, identifier: str
) -> Optional[dict[str, Any]]:
    """Return the thermostat dict whose ``identifier`` matches, or None."""
    if not coordinator.data:
        return None
    for t in coordinator.data:
        if t.get("identifier") == identifier:
            return t
    return None


def thermostat_device_info(thermostat: dict[str, Any]) -> DeviceInfo:
    """Build the DeviceInfo for a thermostat (the parent device)."""
    model_key = thermostat.get("modelNumber") or ""
    model = ECOBEE_MODEL_TO_NAME.get(model_key, model_key or "ecobee Thermostat")
    return DeviceInfo(
        identifiers={(DOMAIN, thermostat["identifier"])},
        name=thermostat.get("name") or "ecobee thermostat",
        manufacturer=MANUFACTURER,
        model=model,
    )


def remote_sensor_device_info(
    thermostat: dict[str, Any], sensor: dict[str, Any]
) -> DeviceInfo:
    """Build the DeviceInfo for a remote sensor.

    Remote SmartSensors have their own ``code`` (factory ID printed on
    the back of the device). Built-in thermostat sensors don't have a
    code — we fall back to grouping them under the parent thermostat
    device so the UI shows them as that thermostat's "thermostat"
    sensor entry rather than a separate orphan device.
    """
    code = sensor.get("code")
    if code:
        return DeviceInfo(
            identifiers={(DOMAIN, code)},
            name=sensor.get("name") or "ecobee Sensor",
            manufacturer=MANUFACTURER,
            model="ecobee Room Sensor",
            via_device=(DOMAIN, thermostat["identifier"]),
        )
    return thermostat_device_info(thermostat)


class EcobeeBaseEntity(CoordinatorEntity[EcobeeDataUpdateCoordinator]):
    """Base entity tied to a specific thermostat by identifier."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcobeeDataUpdateCoordinator,
        thermostat_identifier: str,
    ) -> None:
        super().__init__(coordinator)
        self._thermostat_identifier = thermostat_identifier

    @property
    def thermostat(self) -> Optional[dict[str, Any]]:
        return find_thermostat(self.coordinator, self._thermostat_identifier)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        t = self.thermostat
        if t is None:
            return False
        runtime = t.get("runtime") or {}
        # ecobee reports a per-thermostat ``connected`` flag inside
        # runtime. False = the device hasn't checked in recently.
        return bool(runtime.get("connected", True))
