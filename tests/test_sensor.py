"""Tests for ecobee sensor entities (per-room temp, humidity, outdoor temp)."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ecobee.sensor import (
    EcobeeOutdoorTemperature,
    EcobeeRemoteSensorHumidity,
    EcobeeRemoteSensorTemperature,
)


def _coordinator_with(thermostats):
    """Build a fake coordinator that returns a fixed thermostat list."""
    coord = MagicMock()
    coord.data = thermostats
    coord.last_update_success = True
    return coord


_THERMOSTAT = {
    "identifier": "411111111111",
    "name": "Main Floor",
    "modelNumber": "athenaSmart",
    "runtime": {
        "connected": True,
        "actualTemperature": 715,
        "actualHumidity": 45,
    },
    "remoteSensors": [
        {
            "id": "rs:100",
            "name": "Living Room",
            "code": "ABCD1234",
            "type": "ecobee3_remote_sensor",
            "capability": [
                {"id": "1", "type": "temperature", "value": "725"},
                {"id": "2", "type": "occupancy", "value": "true"},
            ],
        },
        {
            "id": "ei:0",
            "name": "Main Floor",
            # built-in thermostat sensor — no `code`
            "type": "thermostat",
            "capability": [
                {"id": "1", "type": "temperature", "value": "715"},
                {"id": "2", "type": "humidity", "value": "45"},
                {"id": "3", "type": "occupancy", "value": "false"},
            ],
        },
    ],
    "weather": {
        "forecasts": [
            {"temperature": 685, "weatherSymbol": 1},
        ]
    },
}


def test_remote_sensor_temperature_native_value_converts_tenths():
    """ecobee returns 725 -> we report 72.5 F."""
    coord = _coordinator_with([_THERMOSTAT])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "rs:100")
    assert sensor.native_value == 72.5


def test_remote_sensor_temperature_unique_id_uses_code_when_present():
    """SmartSensors with a printed code use the code as their stable id."""
    coord = _coordinator_with([_THERMOSTAT])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "rs:100")
    assert sensor.unique_id == "ABCD1234-temperature"


def test_remote_sensor_temperature_unique_id_falls_back_to_id_for_builtin():
    """Built-in thermostat sensors have no code; unique_id uses (identifier, id)."""
    coord = _coordinator_with([_THERMOSTAT])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "ei:0")
    assert sensor.unique_id == "411111111111-ei:0-temperature"


def test_remote_sensor_temperature_handles_unknown_value():
    """When ecobee reports 'unknown' the entity reports None (not 0)."""
    bad = {
        **_THERMOSTAT,
        "remoteSensors": [
            {
                "id": "rs:100",
                "name": "Nursery",
                "code": "ZZZZ0000",
                "capability": [
                    {"type": "temperature", "value": "unknown"},
                ],
            }
        ],
    }
    coord = _coordinator_with([bad])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "rs:100")
    assert sensor.native_value is None


def test_remote_sensor_temperature_handles_missing_thermostat():
    """If the thermostat dropped out of the poll, native_value is None."""
    coord = _coordinator_with([])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "rs:100")
    assert sensor.native_value is None


def test_remote_sensor_humidity_native_value_is_int():
    """Humidity is returned as a percentage already, no /10 conversion."""
    coord = _coordinator_with([_THERMOSTAT])
    sensor = EcobeeRemoteSensorHumidity(coord, "411111111111", "ei:0")
    assert sensor.native_value == 45.0


def test_outdoor_temperature_uses_first_forecast():
    """Outdoor temp comes from weather.forecasts[0]."""
    coord = _coordinator_with([_THERMOSTAT])
    sensor = EcobeeOutdoorTemperature(coord, "411111111111")
    assert sensor.native_value == 68.5


def test_outdoor_temperature_returns_none_when_no_forecast():
    no_weather = {**_THERMOSTAT, "weather": {"forecasts": []}}
    coord = _coordinator_with([no_weather])
    sensor = EcobeeOutdoorTemperature(coord, "411111111111")
    assert sensor.native_value is None


def test_remote_sensor_temperature_device_info_smartsensor():
    """SmartSensors register as their own device, linked via via_device."""
    coord = _coordinator_with([_THERMOSTAT])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "rs:100")
    info = sensor.device_info
    assert info is not None
    # SmartSensor is identified by its printed code.
    assert ("ecobee", "ABCD1234") in info["identifiers"]
    # And linked back to the parent thermostat via via_device.
    assert info.get("via_device") == ("ecobee", "411111111111")


def test_remote_sensor_temperature_device_info_builtin_groups_under_thermostat():
    """Built-in thermostat sensor appears under the thermostat device, not standalone."""
    coord = _coordinator_with([_THERMOSTAT])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "ei:0")
    info = sensor.device_info
    assert info is not None
    assert ("ecobee", "411111111111") in info["identifiers"]
    assert info.get("via_device") is None


def test_remote_sensor_available_reflects_thermostat_connected():
    """When the thermostat reports runtime.connected=False, sensor is unavailable."""
    disconnected = {**_THERMOSTAT, "runtime": {"connected": False}}
    coord = _coordinator_with([disconnected])
    sensor = EcobeeRemoteSensorTemperature(coord, "411111111111", "rs:100")
    assert sensor.available is False
