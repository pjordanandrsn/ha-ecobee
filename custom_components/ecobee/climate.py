"""Climate platform for the Ecobee Anderson fork integration.

Read-only v0.1: we expose current/target temperatures, current humidity,
HVAC mode, and HVAC action so the dashboard can show the same data the
SmartThings entry currently shows. We deliberately *don't* implement
the write-side service handlers (set_hvac_mode, set_temperature, etc.)
because:

  - The user's stated need is per-room sensor visibility, not control.
  - Implementing the full ecobee write surface (with hold types,
    vacation events, fan-min-on-time, etc.) is a large amount of code
    that would substantially expand the test surface for no payoff
    relative to the integration's actual goal.
  - Until SmartThings is removed, both integrations would race on
    write — best to keep this one read-only as a guard against
    accidental double-control during the verification window.

If the user later wants control, this is the file to extend; the auth
+ API plumbing is already wired for POST.
"""

from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.climate import (
    ClimateEntity,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EcobeeDataUpdateCoordinator
from .entity import EcobeeBaseEntity, thermostat_device_info

# ecobee's ``hvacMode`` setting -> HA HVACMode. ``auxHeatOnly`` is
# ecobee-specific (run aux/strip heat without compressor); we map it to
# HEAT — same compromise the core integration makes.
ECOBEE_HVAC_MODE_TO_HASS = {
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "auto": HVACMode.HEAT_COOL,
    "off": HVACMode.OFF,
    "auxHeatOnly": HVACMode.HEAT,
}

# Thermostat ``equipmentStatus`` is a comma-separated list of running
# components. We pick the most informative one for HVACAction.
EQUIPMENT_TO_HVAC_ACTION = [
    ("heatPump", HVACAction.HEATING),
    ("compHeat", HVACAction.HEATING),
    ("auxHeat", HVACAction.HEATING),
    ("compCool", HVACAction.COOLING),
    ("dehumidifier", HVACAction.DRYING),
    ("humidifier", HVACAction.IDLE),
    ("fan", HVACAction.FAN),
    ("ventilator", HVACAction.FAN),
]

ATTR_FAN_MODE = "fan_mode"  # noqa: F841 (kept for future write-side use)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Build one climate entity per thermostat."""
    coordinator: EcobeeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ClimateEntity] = []
    for thermostat in coordinator.data or []:
        identifier = thermostat.get("identifier")
        if not identifier:
            continue
        entities.append(EcobeeThermostat(coordinator, identifier))
    async_add_entities(entities)


class EcobeeThermostat(EcobeeBaseEntity, ClimateEntity):
    """Read-only climate entity backed by an ecobee thermostat."""

    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = 0  # read-only — see module docstring
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]

    @property
    def name(self) -> Optional[str]:
        # has_entity_name=True + name=None means the entity inherits the
        # device name (the thermostat's name as set in the ecobee app).
        return None

    @property
    def unique_id(self) -> str:
        return f"{self._thermostat_identifier}-thermostat"

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        t = self.thermostat
        if t is None:
            return None
        return thermostat_device_info(t)

    @property
    def current_temperature(self) -> Optional[float]:
        t = self.thermostat
        if t is None:
            return None
        runtime = t.get("runtime") or {}
        raw = runtime.get("actualTemperature")
        if raw is None:
            return None
        try:
            return float(raw) / 10.0
        except (TypeError, ValueError):
            return None

    @property
    def current_humidity(self) -> Optional[int]:
        t = self.thermostat
        if t is None:
            return None
        runtime = t.get("runtime") or {}
        raw = runtime.get("actualHumidity")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @property
    def target_temperature(self) -> Optional[float]:
        """Return single setpoint when in HEAT or COOL mode."""
        t = self.thermostat
        if t is None:
            return None
        mode = self.hvac_mode
        runtime = t.get("runtime") or {}
        if mode == HVACMode.HEAT:
            raw = runtime.get("desiredHeat")
        elif mode == HVACMode.COOL:
            raw = runtime.get("desiredCool")
        else:
            return None
        if raw is None:
            return None
        try:
            return float(raw) / 10.0
        except (TypeError, ValueError):
            return None

    @property
    def target_temperature_high(self) -> Optional[float]:
        t = self.thermostat
        if t is None or self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        raw = (t.get("runtime") or {}).get("desiredCool")
        if raw is None:
            return None
        try:
            return float(raw) / 10.0
        except (TypeError, ValueError):
            return None

    @property
    def target_temperature_low(self) -> Optional[float]:
        t = self.thermostat
        if t is None or self.hvac_mode != HVACMode.HEAT_COOL:
            return None
        raw = (t.get("runtime") or {}).get("desiredHeat")
        if raw is None:
            return None
        try:
            return float(raw) / 10.0
        except (TypeError, ValueError):
            return None

    @property
    def hvac_mode(self) -> HVACMode:
        t = self.thermostat
        if t is None:
            return HVACMode.OFF
        settings = t.get("settings") or {}
        ecobee_mode = settings.get("hvacMode") or "off"
        return ECOBEE_HVAC_MODE_TO_HASS.get(ecobee_mode, HVACMode.OFF)

    @property
    def hvac_action(self) -> Optional[HVACAction]:
        t = self.thermostat
        if t is None:
            return None
        # ecobee gives a comma-separated list of running components.
        # If empty -> idle. Otherwise pick the first match in priority order.
        equipment = (t.get("equipmentStatus") or "").strip()
        if not equipment:
            # idle if HVAC mode isn't off; off otherwise
            return HVACAction.OFF if self.hvac_mode == HVACMode.OFF else HVACAction.IDLE
        running = {item.strip() for item in equipment.split(",")}
        for keyword, action in EQUIPMENT_TO_HVAC_ACTION:
            if keyword in running:
                return action
        # Some unknown equipment string but something is running.
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self.thermostat
        if t is None:
            return {}
        runtime = t.get("runtime") or {}
        attrs: dict[str, Any] = {
            "equipment_running": t.get("equipmentStatus") or "",
        }
        # Surface the active program / event so the SmartThings entry's
        # data isn't lost when the user migrates dashboard wiring.
        events = t.get("events") or []
        if events:
            running = events[0]
            attrs["event_type"] = running.get("type")
            attrs["event_holdClimateRef"] = running.get("holdClimateRef")
        program = t.get("program") or {}
        attrs["current_climate"] = program.get("currentClimateRef")
        attrs["actualHumidity"] = runtime.get("actualHumidity")
        return attrs
