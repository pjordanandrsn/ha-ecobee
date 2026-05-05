"""Thin async client for the ecobee REST API.

The endpoints themselves are unchanged from the core HA integration; we
just bypass ``pyecobee`` because that library is sync-``requests``
based and its auth layer is the very thing we're replacing.

ecobee's API has one quirk worth pointing out: GET requests pass their
JSON-shaped ``selection`` object as a *query string* under
``?json=<urlencoded JSON>``. POST requests send a JSON body with a
``selection`` object plus either a ``thermostat`` patch (for direct
settings updates) or a ``functions`` array (for hold-style operations
like setHold / resumeProgram).

v0.4: write-side endpoints — set_hvac_mode, set_hold (setpoint +
preset), resume_program, set_fan_mode. The selection used for writes
is keyed by the thermostat ``identifier`` so each call targets exactly
one device, which matters when an account has multiple thermostats.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Optional

import aiohttp

from .auth import EcobeeAuth, InvalidGrantError
from .const import API_THERMOSTAT

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class EcobeeApiError(Exception):
    """Generic API failure (non-200, malformed JSON, network)."""


class EcobeeAuthError(Exception):
    """The supplied credentials/token were rejected by the API.

    Distinct from EcobeeApiError so the coordinator can map it to
    ``ConfigEntryAuthFailed`` for the reauth UI.
    """


# Selection payload requested on every poll. We ask for everything the
# entity classes might need so we don't have to make multiple round
# trips. ecobee returns the same JSON regardless of how many flags are
# set, so this is essentially free.
_SELECTION = {
    "selection": {
        "selectionType": "registered",
        "selectionMatch": "",
        "includeRuntime": True,
        "includeSensors": True,
        "includeProgram": True,
        "includeEquipmentStatus": True,
        "includeEvents": True,
        "includeWeather": True,
        "includeSettings": True,
        "includeLocation": True,
    }
}


class EcobeeApiClient:
    """Async REST client for the ecobee thermostat API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: EcobeeAuth,
    ) -> None:
        self._session = session
        self._auth = auth

    async def async_get_thermostats(self) -> list[dict[str, Any]]:
        """Return the raw thermostatList from /1/thermostat.

        Each entry in the list is the full thermostat dict as returned
        by ecobee — we don't post-process it here so the entity classes
        can pick out exactly what they need.
        """
        try:
            access_token = await self._auth.ensure_access_token()
        except InvalidGrantError as ex:
            raise EcobeeAuthError(str(ex)) from ex

        # The ``json`` query-param convention is ecobee-specific:
        # the entire selection object is sent as a URL-encoded JSON
        # string in the ``json`` query param. Mirrors what pyecobee
        # does via ``params={"json": json.dumps(_SELECTION)}``.
        url = f"{API_THERMOSTAT}?json=" + urllib.parse.quote(
            json.dumps(_SELECTION, separators=(",", ":"))
        )

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        try:
            async with self._session.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                status = resp.status
                if status == 401:
                    # Bearer rejected. The auth handle will lazily
                    # refresh on the next call; surface as auth error
                    # so the coordinator can decide whether to retry or
                    # trigger reauth.
                    raise EcobeeAuthError(f"GET {API_THERMOSTAT} -> 401")
                text = await resp.text()
                if status != 200:
                    raise EcobeeApiError(
                        f"GET {API_THERMOSTAT} -> {status}: {text[:200]}"
                    )
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as ex:
                    raise EcobeeApiError(
                        f"malformed JSON from {API_THERMOSTAT}: {ex}; body={text[:200]!r}"
                    ) from ex
        except aiohttp.ClientError as ex:
            raise EcobeeApiError(f"network error: {type(ex).__name__}: {ex}") from ex

        # ecobee wraps every response in a ``status`` envelope. code=0
        # is success; non-zero codes carry an error message we want to
        # surface verbatim.
        status_blob = payload.get("status") or {}
        if status_blob.get("code", 0) != 0:
            msg = status_blob.get("message", "(no message)")
            code = status_blob.get("code")
            # Auth-related codes per ecobee docs: 14 = token expired,
            # 16 = revoked. Both should drive reauth not a transient
            # poll failure.
            if code in (14, 16):
                raise EcobeeAuthError(f"ecobee API auth code={code}: {msg}")
            raise EcobeeApiError(f"ecobee API code={code}: {msg}")

        thermostats = payload.get("thermostatList") or []
        if not isinstance(thermostats, list):
            raise EcobeeApiError(
                f"thermostatList was {type(thermostats).__name__}, expected list"
            )
        _LOGGER.debug("ecobee poll OK: %d thermostat(s)", len(thermostats))
        return thermostats

    # ─── write side ──────────────────────────────────────────────────

    async def _post_thermostat(self, body: dict[str, Any]) -> None:
        """POST /1/thermostat?format=json with the given JSON body.

        The body shape for any write is::

            {
              "selection": {"selectionType": "thermostats",
                            "selectionMatch": "<identifier>"},
              "thermostat": {"settings": {...}},   # one of these,
              "functions": [{"type": ..., "params": {...}}],  # not both
            }

        Successful writes return a status envelope with code=0; any
        non-zero code is surfaced as an error so the climate entity's
        action can fail loudly to HA's UI rather than silently no-op.
        """
        try:
            access_token = await self._auth.ensure_access_token()
        except InvalidGrantError as ex:
            raise EcobeeAuthError(str(ex)) from ex

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
        }
        url = f"{API_THERMOSTAT}?format=json"
        try:
            async with self._session.post(
                url, headers=headers, json=body, timeout=REQUEST_TIMEOUT
            ) as resp:
                status = resp.status
                if status == 401:
                    raise EcobeeAuthError(f"POST {API_THERMOSTAT} -> 401")
                text = await resp.text()
                if status != 200:
                    raise EcobeeApiError(
                        f"POST {API_THERMOSTAT} -> {status}: {text[:200]}"
                    )
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as ex:
                    raise EcobeeApiError(
                        f"malformed JSON from POST {API_THERMOSTAT}: {ex};"
                        f" body={text[:200]!r}"
                    ) from ex
        except aiohttp.ClientError as ex:
            raise EcobeeApiError(f"network error: {type(ex).__name__}: {ex}") from ex

        status_blob = payload.get("status") or {}
        code = status_blob.get("code", 0)
        if code != 0:
            msg = status_blob.get("message", "(no message)")
            if code in (14, 16):
                raise EcobeeAuthError(f"ecobee API auth code={code}: {msg}")
            raise EcobeeApiError(f"ecobee API code={code}: {msg}")

    @staticmethod
    def _selection(identifier: str) -> dict[str, Any]:
        return {
            "selectionType": "thermostats",
            "selectionMatch": identifier,
        }

    async def async_update_settings(
        self, identifier: str, settings: dict[str, Any]
    ) -> None:
        """Patch the thermostat's ``settings`` object — used for hvacMode,
        fan ``vent`` mode, hold-/away-circulate flags, etc."""
        await self._post_thermostat(
            {
                "selection": self._selection(identifier),
                "thermostat": {"settings": settings},
            }
        )

    async def async_set_hold(
        self,
        identifier: str,
        *,
        heat_hold_temp_f10: int | None = None,
        cool_hold_temp_f10: int | None = None,
        hold_climate_ref: str | None = None,
        hold_type: str = "nextTransition",
        fan: str | None = None,
    ) -> None:
        """Apply a temperature- or program-based hold via setHold function.

        Either supply explicit setpoints (``heat_hold_temp_f10`` /
        ``cool_hold_temp_f10`` — F * 10 integers, ecobee's storage unit)
        OR a ``hold_climate_ref`` ('home', 'away', 'sleep', custom). Mixing
        both is fine; ecobee will use the climateRef's defaults for any
        setpoint not provided.

        ``hold_type`` choices: ``nextTransition`` (until next scheduled
        program change — the ecobee app's default), ``indefinite``
        (sticky until manually resumed), ``holdHours`` (paired with
        ``params.holdHours`` — not exposed here yet).
        """
        params: dict[str, Any] = {"holdType": hold_type}
        if heat_hold_temp_f10 is not None:
            params["heatHoldTemp"] = int(heat_hold_temp_f10)
        if cool_hold_temp_f10 is not None:
            params["coolHoldTemp"] = int(cool_hold_temp_f10)
        if hold_climate_ref is not None:
            params["holdClimateRef"] = hold_climate_ref
        if fan is not None:
            # Per ecobee docs: passing fan='on' on a setHold forces the
            # fan to run for the duration of the hold; default 'auto'
            # follows the system's call.
            params["fan"] = fan
        await self._post_thermostat(
            {
                "selection": self._selection(identifier),
                "functions": [{"type": "setHold", "params": params}],
            }
        )

    async def async_resume_program(
        self, identifier: str, *, resume_all: bool = True
    ) -> None:
        """Clear active holds. ``resume_all=True`` clears every stacked
        hold in one call (recommended); ``False`` pops only the topmost,
        which can leave the user wondering why one tap didn't return to
        schedule."""
        await self._post_thermostat(
            {
                "selection": self._selection(identifier),
                "functions": [
                    {
                        "type": "resumeProgram",
                        "params": {"resumeAll": bool(resume_all)},
                    }
                ],
            }
        )

    async def async_create_vacation(
        self,
        identifier: str,
        *,
        name: str,
        start_date: str,
        start_time: str,
        end_date: str,
        end_time: str,
        heat_hold_temp_f10: int,
        cool_hold_temp_f10: int,
        fan: str = "auto",
        fan_min_on_time: int = 0,
    ) -> None:
        """Create a vacation event with explicit start + end timestamps.

        ecobee separates date and time into two fields each. Format:
            start_date / end_date: 'YYYY-MM-DD'
            start_time / end_time: 'HH:MM:SS' (24-hour, thermostat-local time)

        Vacation ``name`` doubles as the unique identifier — passing the
        same name as an existing vacation returns a 'duplicate name'
        error rather than overwriting. Caller is responsible for picking
        a unique name (the HA service handler suffixes with the start
        timestamp to make collisions unlikely).
        """
        params = {
            "name": name,
            "coolHoldTemp": int(cool_hold_temp_f10),
            "heatHoldTemp": int(heat_hold_temp_f10),
            "startDate": start_date,
            "startTime": start_time,
            "endDate": end_date,
            "endTime": end_time,
            "fan": fan,
            "fanMinOnTime": int(fan_min_on_time),
        }
        await self._post_thermostat(
            {
                "selection": self._selection(identifier),
                "functions": [{"type": "createVacation", "params": params}],
            }
        )

    async def async_delete_vacation(self, identifier: str, *, name: str) -> None:
        """Cancel a vacation by name. Silently no-ops if no match."""
        await self._post_thermostat(
            {
                "selection": self._selection(identifier),
                "functions": [
                    {"type": "deleteVacation", "params": {"name": name}}
                ],
            }
        )
