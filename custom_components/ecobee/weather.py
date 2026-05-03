"""Weather platform for the Ecobee Anderson fork integration.

ecobee includes a weather forecast block per thermostat (location-based,
sourced from a third-party feed). We surface it as a weather entity
mostly for parity with the HA core integration — the AmbientWeather
station is the canonical local-weather source on this hub, but having
ecobee weather available means dashboard cards that want forecast can
still render even if AW is offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.components.weather import (
    ATTR_CONDITION_CLOUDY,
    ATTR_CONDITION_FOG,
    ATTR_CONDITION_HAIL,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_PARTLYCLOUDY,
    ATTR_CONDITION_POURING,
    ATTR_CONDITION_RAINY,
    ATTR_CONDITION_SNOWY,
    ATTR_CONDITION_SNOWY_RAINY,
    ATTR_CONDITION_SUNNY,
    ATTR_CONDITION_WINDY,
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EcobeeDataUpdateCoordinator
from .entity import EcobeeBaseEntity, thermostat_device_info

# ecobee weatherSymbol -> HA condition. Mirrors the table in the core
# integration's const.py so we behave identically for downstream
# automations keyed off the state value.
ECOBEE_WEATHER_SYMBOL_TO_HASS = {
    0: ATTR_CONDITION_SUNNY,
    1: ATTR_CONDITION_PARTLYCLOUDY,
    2: ATTR_CONDITION_PARTLYCLOUDY,
    3: ATTR_CONDITION_CLOUDY,
    4: ATTR_CONDITION_CLOUDY,
    5: ATTR_CONDITION_CLOUDY,
    6: ATTR_CONDITION_RAINY,
    7: ATTR_CONDITION_SNOWY_RAINY,
    8: ATTR_CONDITION_POURING,
    9: ATTR_CONDITION_HAIL,
    10: ATTR_CONDITION_SNOWY,
    11: ATTR_CONDITION_SNOWY,
    12: ATTR_CONDITION_SNOWY_RAINY,
    13: ATTR_CONDITION_SNOWY,
    14: ATTR_CONDITION_HAIL,
    15: ATTR_CONDITION_LIGHTNING_RAINY,
    16: ATTR_CONDITION_WINDY,
    17: "tornado",
    18: ATTR_CONDITION_FOG,
    19: "hazy",
    20: "hazy",
    21: "hazy",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Build one weather entity per thermostat."""
    coordinator: EcobeeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[WeatherEntity] = []
    for thermostat in coordinator.data or []:
        identifier = thermostat.get("identifier")
        if not identifier:
            continue
        weather = thermostat.get("weather") or {}
        if (weather.get("forecasts") or []):
            entities.append(EcobeeWeather(coordinator, identifier))
    async_add_entities(entities)


def _to_temp(raw: Any) -> Optional[float]:
    if raw is None or raw == "unknown":
        return None
    try:
        return float(raw) / 10.0
    except (TypeError, ValueError):
        return None


def _parse_forecast_dt(raw: Any) -> Optional[datetime]:
    """Parse ecobee's "YYYY-MM-DD HH:MM:SS" forecast timestamp.

    ecobee timestamps are UTC but lack a TZ designator. We tag them
    explicitly so HA doesn't reinterpret them in local time.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class EcobeeWeather(EcobeeBaseEntity, WeatherEntity):
    """Weather entity backed by a thermostat's weather block."""

    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_native_pressure_unit = UnitOfPressure.MBAR
    _attr_native_wind_speed_unit = UnitOfSpeed.MILES_PER_HOUR
    _attr_supported_features = WeatherEntityFeature.FORECAST_DAILY

    @property
    def name(self) -> str:
        return "Weather"

    @property
    def unique_id(self) -> str:
        return f"{self._thermostat_identifier}-weather"

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        t = self.thermostat
        if t is None:
            return None
        return thermostat_device_info(t)

    def _current_forecast(self) -> Optional[dict[str, Any]]:
        t = self.thermostat
        if t is None:
            return None
        forecasts = (t.get("weather") or {}).get("forecasts") or []
        return forecasts[0] if forecasts else None

    @property
    def condition(self) -> Optional[str]:
        f = self._current_forecast()
        if f is None:
            return None
        symbol = f.get("weatherSymbol")
        if symbol is None:
            return None
        return ECOBEE_WEATHER_SYMBOL_TO_HASS.get(int(symbol))

    @property
    def native_temperature(self) -> Optional[float]:
        f = self._current_forecast()
        if f is None:
            return None
        return _to_temp(f.get("temperature"))

    @property
    def native_pressure(self) -> Optional[float]:
        f = self._current_forecast()
        if f is None:
            return None
        try:
            return float(f.get("pressure"))
        except (TypeError, ValueError):
            return None

    @property
    def humidity(self) -> Optional[float]:
        f = self._current_forecast()
        if f is None:
            return None
        try:
            return float(f.get("relativeHumidity"))
        except (TypeError, ValueError):
            return None

    @property
    def native_wind_speed(self) -> Optional[float]:
        f = self._current_forecast()
        if f is None:
            return None
        try:
            return float(f.get("windSpeed"))
        except (TypeError, ValueError):
            return None

    @property
    def wind_bearing(self) -> Optional[float]:
        f = self._current_forecast()
        if f is None:
            return None
        try:
            return float(f.get("windBearing"))
        except (TypeError, ValueError):
            return None

    async def async_forecast_daily(self) -> Optional[list[Forecast]]:
        t = self.thermostat
        if t is None:
            return None
        forecasts = (t.get("weather") or {}).get("forecasts") or []
        out: list[Forecast] = []
        for f in forecasts:
            dt = _parse_forecast_dt(f.get("dateTime"))
            symbol = f.get("weatherSymbol")
            condition = (
                ECOBEE_WEATHER_SYMBOL_TO_HASS.get(int(symbol))
                if symbol is not None
                else None
            )
            out.append(
                Forecast(
                    datetime=dt.isoformat() if dt else "",
                    native_temperature=_to_temp(f.get("tempHigh")),
                    native_templow=_to_temp(f.get("tempLow")),
                    condition=condition,
                )
            )
        return out
