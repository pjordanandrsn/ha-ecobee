"""Climate platform for the Ecobee community fork integration.

v0.4 adds the write surface — set_hvac_mode, set_temperature,
set_fan_mode, set_preset_mode + an explicit "Resume program" preset
that clears holds. Read side unchanged from v0.1.

Setpoints land via setHold (function-based POST) rather than direct
runtime patches because runtime values are continuously overwritten by
the active program; only a hold sticks. Preset mode maps to
holdClimateRef (home / away / sleep / custom) through the same setHold.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import EcobeeApiError
from .const import DOMAIN
from .coordinator import EcobeeDataUpdateCoordinator
from .entity import EcobeeBaseEntity, thermostat_device_info

_LOGGER = logging.getLogger(__name__)

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
# Inverse for write side. We never write 'auxHeatOnly' from HA — the
# user can't distinguish it from HEAT in the UI; ecobee will pick aux
# automatically when the heat pump can't keep up.
HASS_HVAC_MODE_TO_ECOBEE = {
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.HEAT_COOL: "auto",
    HVACMode.OFF: "off",
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

# ecobee's fan modes are per-thermostat and per-program. The two
# universally-supported values are 'auto' (run only when heating/cooling)
# and 'on' (run continuously). Some models also support 'circulate' via
# ``fanMinOnTime`` minutes; we don't expose that knob in v0.4 to keep
# the UI simple.
FAN_MODE_AUTO = "auto"
FAN_MODE_ON = "on"

# A synthesised preset that means "clear all holds and follow the
# schedule again". Not an ecobee climateRef — handled specially in
# async_set_preset_mode.
PRESET_NONE = "none"


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
    """Read+write climate entity backed by an ecobee thermostat."""

    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
    _attr_fan_modes = [FAN_MODE_AUTO, FAN_MODE_ON]

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

        # Vacation surfacing — list every event with type == 'vacation',
        # active or scheduled. ecobee returns them in the same events
        # array as holds, distinguished by ``type``. The HA frontend
        # can't model an end-time on preset_modes, so we publish
        # vacations as a list attribute and let the user act on it via
        # the ecobee.delete_vacation service.
        vacations: list[dict[str, Any]] = []
        for e in events:
            if e.get("type") != "vacation":
                continue
            vacations.append(
                {
                    "name": e.get("name"),
                    "running": bool(e.get("running")),
                    # ecobee returns these as 'YYYY-MM-DD' / 'HH:MM:SS'
                    # strings — pass through verbatim so the user sees
                    # the same values they'd see in the ecobee app.
                    "start": f"{e.get('startDate', '?')} {e.get('startTime', '?')}",
                    "end": f"{e.get('endDate', '?')} {e.get('endTime', '?')}",
                    "heat_temp_f": (e.get("heatHoldTemp", 0) / 10.0)
                    if e.get("heatHoldTemp") is not None
                    else None,
                    "cool_temp_f": (e.get("coolHoldTemp", 0) / 10.0)
                    if e.get("coolHoldTemp") is not None
                    else None,
                    "fan": e.get("fan"),
                }
            )
        attrs["vacations"] = vacations
        attrs["vacation_active"] = any(v["running"] for v in vacations)

        return attrs

    # ─── preset + fan mode reads ──────────────────────────────────────

    @property
    def preset_modes(self) -> Optional[list[str]]:
        """Build the preset list from the thermostat's program climates.

        ecobee ships three default climates (home / away / sleep) plus
        any user-defined ones. We always prepend PRESET_NONE so the UI
        has a path back to the schedule from a stuck hold.
        """
        t = self.thermostat
        if t is None:
            return [PRESET_NONE]
        program = t.get("program") or {}
        climates = program.get("climates") or []
        names: list[str] = []
        for c in climates:
            ref = c.get("climateRef") or c.get("name")
            if ref and ref not in names:
                names.append(ref)
        return [PRESET_NONE, *names]

    @property
    def preset_mode(self) -> Optional[str]:
        """The active preset is the topmost hold's climateRef, if any.

        With no holds, we report PRESET_NONE rather than the schedule's
        current climate — leaving "none" visible makes it obvious that
        the thermostat is on the schedule rather than on a hold.
        """
        t = self.thermostat
        if t is None:
            return None
        events = t.get("events") or []
        for e in events:
            if e.get("running") and e.get("type") == "hold":
                ref = e.get("holdClimateRef")
                if ref:
                    return ref
                # Hold without a climateRef = direct setpoint hold; map
                # back to "none" so the UI doesn't pick a misleading label.
                return PRESET_NONE
        return PRESET_NONE

    @property
    def fan_mode(self) -> Optional[str]:
        """Return the current fan mode.

        While a hold is active, the running event carries the live fan
        setting; otherwise fall back to the active program climate.
        """
        t = self.thermostat
        if t is None:
            return None
        events = t.get("events") or []
        for e in events:
            if e.get("running"):
                fan = e.get("fan")
                if fan in (FAN_MODE_AUTO, FAN_MODE_ON):
                    return fan
        program = t.get("program") or {}
        climates = program.get("climates") or []
        current_ref = program.get("currentClimateRef")
        for c in climates:
            if c.get("climateRef") == current_ref:
                fan = c.get("fan")
                if fan in (FAN_MODE_AUTO, FAN_MODE_ON):
                    return fan
        return None

    # ─── write side ──────────────────────────────────────────────────

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        ecobee_mode = HASS_HVAC_MODE_TO_ECOBEE.get(hvac_mode)
        if ecobee_mode is None:
            raise HomeAssistantError(f"Unsupported hvac_mode: {hvac_mode}")
        try:
            await self.coordinator.api.async_update_settings(
                self._thermostat_identifier, {"hvacMode": ecobee_mode}
            )
        except EcobeeApiError as ex:
            raise HomeAssistantError(f"ecobee set_hvac_mode failed: {ex}") from ex
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Apply a temperature hold.

        Single-setpoint mode (HEAT or COOL) sends ``temperature``; auto
        mode sends ``target_temp_low`` + ``target_temp_high``. ecobee
        wants both setpoints on every setHold even in single-mode (the
        unused side is ignored), so we read whichever is missing back
        from the current state to keep the call balanced.
        """
        mode = self.hvac_mode
        # Read current setpoints once; defaults below cover the case
        # where the thermostat hasn't reported them yet.
        cur_low = self.target_temperature_low
        cur_high = self.target_temperature_high
        cur_single = self.target_temperature

        target = kwargs.get(ATTR_TEMPERATURE)
        target_low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        target_high = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        if mode == HVACMode.HEAT_COOL:
            if target_low is None and target_high is None:
                raise HomeAssistantError(
                    "auto mode requires both target_temp_low and target_temp_high"
                )
            heat_f = float(target_low if target_low is not None else cur_low or 68)
            cool_f = float(target_high if target_high is not None else cur_high or 76)
        elif mode == HVACMode.HEAT:
            if target is None:
                raise HomeAssistantError("heat mode requires temperature")
            heat_f = float(target)
            cool_f = float(cur_high if cur_high is not None else (cur_single or 76) + 4)
        elif mode == HVACMode.COOL:
            if target is None:
                raise HomeAssistantError("cool mode requires temperature")
            cool_f = float(target)
            heat_f = float(cur_low if cur_low is not None else (cur_single or 68) - 4)
        else:
            raise HomeAssistantError(
                f"Cannot set temperature while hvac_mode is {mode}"
            )

        try:
            await self.coordinator.api.async_set_hold(
                self._thermostat_identifier,
                heat_hold_temp_f10=int(round(heat_f * 10)),
                cool_hold_temp_f10=int(round(cool_f * 10)),
                hold_type="nextTransition",
            )
        except EcobeeApiError as ex:
            raise HomeAssistantError(f"ecobee set_temperature failed: {ex}") from ex
        await self.coordinator.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in (FAN_MODE_AUTO, FAN_MODE_ON):
            raise HomeAssistantError(f"Unsupported fan_mode: {fan_mode}")
        # Setting fan mode without a setpoint change means re-issuing
        # the existing setpoints under a hold with the new fan value.
        cur_low = self.target_temperature_low
        cur_high = self.target_temperature_high
        cur_single = self.target_temperature
        if self.hvac_mode == HVACMode.HEAT_COOL:
            heat_f = float(cur_low or 68)
            cool_f = float(cur_high or 76)
        elif self.hvac_mode == HVACMode.HEAT:
            heat_f = float(cur_single or 68)
            cool_f = float((cur_single or 68) + 8)
        elif self.hvac_mode == HVACMode.COOL:
            cool_f = float(cur_single or 76)
            heat_f = float((cur_single or 76) - 8)
        else:
            # Off mode — issue a fan-only hold by passing through climateRef
            # 'home' so we don't accidentally write nonsense setpoints.
            try:
                await self.coordinator.api.async_set_hold(
                    self._thermostat_identifier,
                    hold_climate_ref="home",
                    fan=fan_mode,
                )
            except EcobeeApiError as ex:
                raise HomeAssistantError(f"ecobee set_fan_mode failed: {ex}") from ex
            await self.coordinator.async_request_refresh()
            return

        try:
            await self.coordinator.api.async_set_hold(
                self._thermostat_identifier,
                heat_hold_temp_f10=int(round(heat_f * 10)),
                cool_hold_temp_f10=int(round(cool_f * 10)),
                fan=fan_mode,
            )
        except EcobeeApiError as ex:
            raise HomeAssistantError(f"ecobee set_fan_mode failed: {ex}") from ex
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        try:
            if preset_mode == PRESET_NONE:
                await self.coordinator.api.async_resume_program(
                    self._thermostat_identifier, resume_all=True
                )
            else:
                await self.coordinator.api.async_set_hold(
                    self._thermostat_identifier,
                    hold_climate_ref=preset_mode,
                    hold_type="nextTransition",
                )
        except EcobeeApiError as ex:
            raise HomeAssistantError(f"ecobee set_preset_mode failed: {ex}") from ex
        await self.coordinator.async_request_refresh()
