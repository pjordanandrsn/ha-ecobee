"""Tests for the read-only ecobee climate entity."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ecobee.climate import EcobeeThermostat
from homeassistant.components.climate import HVACAction, HVACMode


def _coordinator_with(thermostats):
    coord = MagicMock()
    coord.data = thermostats
    coord.last_update_success = True
    return coord


def _thermostat(*, mode="cool", equipment="compCool,fan", desired_heat=680, desired_cool=720):
    return {
        "identifier": "411111111111",
        "name": "Main Floor",
        "modelNumber": "aresSmart",
        "settings": {"hvacMode": mode},
        "equipmentStatus": equipment,
        "runtime": {
            "connected": True,
            "actualTemperature": 715,
            "actualHumidity": 50,
            "desiredHeat": desired_heat,
            "desiredCool": desired_cool,
        },
        "events": [],
        "program": {"currentClimateRef": "home"},
    }


def test_climate_current_temperature_converts_tenths():
    coord = _coordinator_with([_thermostat()])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.current_temperature == 71.5


def test_climate_current_humidity_passes_through():
    coord = _coordinator_with([_thermostat()])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.current_humidity == 50


def test_climate_target_temperature_in_cool_mode_uses_desired_cool():
    coord = _coordinator_with([_thermostat(mode="cool", desired_cool=720)])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.target_temperature == 72.0


def test_climate_target_temperature_in_heat_mode_uses_desired_heat():
    coord = _coordinator_with([_thermostat(mode="heat", desired_heat=685)])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.target_temperature == 68.5


def test_climate_target_temp_high_low_in_auto():
    """In HEAT_COOL the high/low are populated; single target is None."""
    coord = _coordinator_with([_thermostat(mode="auto", desired_heat=680, desired_cool=720)])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.target_temperature is None
    assert c.target_temperature_low == 68.0
    assert c.target_temperature_high == 72.0


def test_climate_hvac_mode_maps_off():
    coord = _coordinator_with([_thermostat(mode="off")])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.hvac_mode == HVACMode.OFF


def test_climate_hvac_mode_maps_aux_heat_only_to_heat():
    """auxHeatOnly is ecobee-specific; map to HEAT (matches core)."""
    coord = _coordinator_with([_thermostat(mode="auxHeatOnly")])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.hvac_mode == HVACMode.HEAT


def test_climate_hvac_action_cooling_when_compressor_running():
    coord = _coordinator_with([_thermostat(equipment="compCool,fan")])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.hvac_action == HVACAction.COOLING


def test_climate_hvac_action_idle_when_nothing_running_in_active_mode():
    coord = _coordinator_with([_thermostat(mode="cool", equipment="")])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.hvac_action == HVACAction.IDLE


def test_climate_hvac_action_off_when_mode_off_and_no_equipment():
    coord = _coordinator_with([_thermostat(mode="off", equipment="")])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.hvac_action == HVACAction.OFF


def test_climate_extra_state_attributes_includes_program_climate():
    coord = _coordinator_with([_thermostat()])
    c = EcobeeThermostat(coord, "411111111111")
    attrs = c.extra_state_attributes
    assert attrs["current_climate"] == "home"
    assert "actualHumidity" in attrs


def test_climate_unavailable_when_thermostat_disconnected():
    """runtime.connected=False -> entity reports unavailable."""
    bad = _thermostat()
    bad["runtime"]["connected"] = False
    coord = _coordinator_with([bad])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.available is False


def test_climate_no_thermostat_returns_none_safely():
    """If the identifier isn't in coordinator.data anymore, all reads are None."""
    coord = _coordinator_with([])
    c = EcobeeThermostat(coord, "411111111111")
    assert c.current_temperature is None
    assert c.current_humidity is None
    assert c.target_temperature is None
    assert c.hvac_mode == HVACMode.OFF
    assert c.hvac_action is None
