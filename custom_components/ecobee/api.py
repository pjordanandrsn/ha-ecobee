"""Thin async client for the ecobee REST API.

The endpoints themselves are unchanged from the core HA integration; we
just bypass ``pyecobee`` because that library is sync-``requests``
based and its auth layer is the very thing we're replacing.

ecobee's API has one quirk worth pointing out: GET requests pass their
JSON-shaped ``selection`` object as a *query string* under
``?json=<urlencoded JSON>``. We use ``urllib.parse.quote`` exactly once
and let aiohttp pass the result through verbatim.

We do not implement write-side endpoints (set_hvac_mode, set_hold, etc.)
in this v0.1 — the integration's purpose is exposing per-room sensors
that the SmartThings workaround hides. Read-only is enough for that.
The climate entity is read-only as a result; users who need to change
HVAC mode from HA can still do it via the SmartThings entry while it's
running, or via the ecobee app.
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
