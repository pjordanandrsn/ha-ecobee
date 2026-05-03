"""Tests for the EcobeeApiClient."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from custom_components.ecobee.api import (
    EcobeeApiClient,
    EcobeeApiError,
    EcobeeAuthError,
)
from custom_components.ecobee.auth import InvalidGrantError


def _acm(resp):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _resp(status: int, body: str | dict):
    resp = MagicMock()
    resp.status = status
    if isinstance(body, dict):
        body = json.dumps(body)
    resp.text = AsyncMock(return_value=body)
    return resp


def _client(get_resp, *, ensure_token_returns="AT-x"):
    session = MagicMock()
    session.get = MagicMock(return_value=_acm(get_resp))
    auth = MagicMock()
    auth.ensure_access_token = AsyncMock(return_value=ensure_token_returns)
    return EcobeeApiClient(session, auth), session, auth


@pytest.mark.asyncio
async def test_async_get_thermostats_happy_path():
    client, session, auth = _client(
        _resp(
            200,
            {
                "status": {"code": 0, "message": ""},
                "thermostatList": [{"identifier": "12345"}],
            },
        )
    )
    result = await client.async_get_thermostats()
    assert result == [{"identifier": "12345"}]
    auth.ensure_access_token.assert_awaited_once()
    # Bearer token must end up in the request header.
    args, kwargs = session.get.call_args
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer AT-x"
    # And the URL must contain the URL-encoded "json=" selection param
    # (ecobee's idiosyncratic GET-with-JSON convention).
    url = args[0]
    assert "/1/thermostat?json=" in url
    assert "selectionType" in __import__("urllib.parse", fromlist=["unquote"]).unquote(url)


@pytest.mark.asyncio
async def test_async_get_thermostats_401_raises_auth_error():
    client, _, _ = _client(_resp(401, "Unauthorized"))
    with pytest.raises(EcobeeAuthError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_5xx_raises_api_error():
    client, _, _ = _client(_resp(503, "service unavailable"))
    with pytest.raises(EcobeeApiError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_envelope_auth_code_raises_auth_error():
    """ecobee status code 14 (token expired) must drive reauth, not retry."""
    client, _, _ = _client(
        _resp(
            200,
            {
                "status": {"code": 14, "message": "token expired"},
            },
        )
    )
    with pytest.raises(EcobeeAuthError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_envelope_other_code_raises_api_error():
    client, _, _ = _client(
        _resp(
            200,
            {"status": {"code": 99, "message": "rate limit"}},
        )
    )
    with pytest.raises(EcobeeApiError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_invalid_grant_from_auth_propagates_as_auth_error():
    """If ensure_access_token raises InvalidGrantError, surface as EcobeeAuthError."""
    session = MagicMock()
    auth = MagicMock()
    auth.ensure_access_token = AsyncMock(side_effect=InvalidGrantError("revoked"))
    client = EcobeeApiClient(session, auth)
    with pytest.raises(EcobeeAuthError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_malformed_json_raises_api_error():
    client, _, _ = _client(_resp(200, "<html>oops</html>"))
    with pytest.raises(EcobeeApiError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_network_error_raises_api_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("DNS"))
    auth = MagicMock()
    auth.ensure_access_token = AsyncMock(return_value="AT")
    client = EcobeeApiClient(session, auth)
    with pytest.raises(EcobeeApiError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_non_list_raises_api_error():
    """If thermostatList is something other than a list, fail loudly."""
    client, _, _ = _client(
        _resp(
            200,
            {
                "status": {"code": 0},
                "thermostatList": {"not": "a list"},
            },
        )
    )
    with pytest.raises(EcobeeApiError):
        await client.async_get_thermostats()


@pytest.mark.asyncio
async def test_async_get_thermostats_missing_list_returns_empty():
    """Missing thermostatList is valid for an account with zero devices."""
    client, _, _ = _client(
        _resp(
            200,
            {"status": {"code": 0, "message": ""}},
        )
    )
    out = await client.async_get_thermostats()
    assert out == []
