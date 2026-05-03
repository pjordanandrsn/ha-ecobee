"""Tests for ecobee occupancy binary_sensor entities."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ecobee.binary_sensor import EcobeeOccupancyBinarySensor


def _coordinator_with(thermostats):
    coord = MagicMock()
    coord.data = thermostats
    coord.last_update_success = True
    return coord


def _build_thermostat(occupancy_value):
    return {
        "identifier": "411111111111",
        "name": "Main",
        "modelNumber": "athenaSmart",
        "runtime": {"connected": True},
        "remoteSensors": [
            {
                "id": "rs:200",
                "name": "Bedroom",
                "code": "WXYZ9876",
                "capability": [
                    {"type": "temperature", "value": "705"},
                    {"type": "occupancy", "value": occupancy_value},
                ],
            }
        ],
    }


def test_occupancy_is_on_when_value_is_string_true():
    coord = _coordinator_with([_build_thermostat("true")])
    sensor = EcobeeOccupancyBinarySensor(coord, "411111111111", "rs:200")
    assert sensor.is_on is True


def test_occupancy_is_off_when_value_is_string_false():
    coord = _coordinator_with([_build_thermostat("false")])
    sensor = EcobeeOccupancyBinarySensor(coord, "411111111111", "rs:200")
    assert sensor.is_on is False


def test_occupancy_is_none_when_value_is_unknown():
    """ecobee reports 'unknown' while a sensor is calibrating."""
    coord = _coordinator_with([_build_thermostat("unknown")])
    sensor = EcobeeOccupancyBinarySensor(coord, "411111111111", "rs:200")
    assert sensor.is_on is None


def test_occupancy_unique_id_uses_code():
    coord = _coordinator_with([_build_thermostat("true")])
    sensor = EcobeeOccupancyBinarySensor(coord, "411111111111", "rs:200")
    assert sensor.unique_id == "WXYZ9876-occupancy"


def test_occupancy_handles_missing_sensor_gracefully():
    coord = _coordinator_with([])
    sensor = EcobeeOccupancyBinarySensor(coord, "411111111111", "rs:200")
    assert sensor.is_on is None
    assert sensor.unique_id is None
