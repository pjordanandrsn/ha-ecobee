"""Tests for the v0.3 ecobee config flow (single-step universal-login + reauth)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.ecobee.auth import InvalidCredentialsError
from custom_components.ecobee.const import (
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
)
from homeassistant import config_entries, setup
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _mock_auth(refresh_token: str = "rt-abc", email: str = "user@example.com"):
    auth = MagicMock()
    auth.refresh_token = refresh_token
    auth.email = email
    return auth


# ---------------------------------------------------------------------------
# Initial-setup happy path + error mappings
# ---------------------------------------------------------------------------


async def test_form_user_happy_path(hass: HomeAssistant) -> None:
    """Valid email+password creates the entry without persisting the password."""
    await setup.async_setup_component(hass, "persistent_notification", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["errors"] == {}

    fake_auth = _mock_auth()
    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(return_value=fake_auth),
    ), patch(
        "custom_components.ecobee.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "hunter2"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == "create_entry"
    assert result2["title"] == "user@example.com"
    assert result2["data"][CONF_USERNAME] == "user@example.com"
    assert result2["data"][CONF_REFRESH_TOKEN] == "rt-abc"
    assert CONF_PASSWORD not in result2["data"]
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_invalid_credentials(hass: HomeAssistant) -> None:
    """Bad creds show 'auth' error; no entry created."""
    await setup.async_setup_component(hass, "persistent_notification", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(side_effect=InvalidCredentialsError("bad creds")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "wrong"},
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "auth"}


async def test_form_internal_error(hass: HomeAssistant) -> None:
    """An unexpected exception surfaces as 'internal'."""
    await setup.async_setup_component(hass, "persistent_notification", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(side_effect=Exception("boom")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "u@e.com", CONF_PASSWORD: "any"},
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "internal"}


@pytest.mark.parametrize(
    "exception_msg, expected_error",
    [
        ("step=authorize: no state in redirect", "auth0_step_authorize"),
        ("step=login_form url=... status=500 no_code", "auth0_step_login_form"),
        ("step=resume: unexpected scheme", "auth0_step_resume"),
        ("step=custom-prompt: POST returned 200", "auth0_step_custom_prompt"),
        ("code exchange failed: 400 ...", "code_exchange"),
        ("login: no state in redirect", "auth0_redirect"),
    ],
)
async def test_form_auth0_step_errors_classified(
    hass: HomeAssistant, exception_msg: str, expected_error: str
) -> None:
    """RuntimeErrors from auth.py get classified by step= tag."""
    await setup.async_setup_component(hass, "persistent_notification", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(side_effect=RuntimeError(exception_msg)),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "u@e.com", CONF_PASSWORD: "any"},
        )
    assert result2["type"] == "form"
    assert result2["errors"] == {"base": expected_error}


async def test_form_auth0_unreachable(hass: HomeAssistant) -> None:
    """ClientConnectorError surfaces as auth0_unreachable."""
    import aiohttp
    await setup.async_setup_component(hass, "persistent_notification", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    err = aiohttp.ClientConnectorError(
        connection_key=MagicMock(), os_error=OSError("dns fail")
    )
    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(side_effect=err),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "u@e.com", CONF_PASSWORD: "any"},
        )
    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "auth0_unreachable"}


async def test_duplicate_entry_aborts(hass: HomeAssistant) -> None:
    """Same email twice -> abort already_configured."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={CONF_USERNAME: "user@example.com"},
    )
    existing.add_to_hass(hass)

    await setup.async_setup_component(hass, "persistent_notification", {})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(return_value=_mock_auth()),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "any"},
        )

    assert result2["type"] == "abort"
    assert result2["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reauth flow (single step — same as user step)
# ---------------------------------------------------------------------------


async def test_reauth_flow_swaps_refresh_token(hass: HomeAssistant) -> None:
    """Reauth re-prompts password (email locked) and persists new RT."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_REFRESH_TOKEN: "stale-rt",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"

    new_auth = _mock_auth(refresh_token="fresh-rt")
    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(return_value=new_auth),
    ), patch("custom_components.ecobee.async_setup_entry", return_value=True):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "new-pw"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == "abort"
    assert result2["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "fresh-rt"


async def test_reauth_locks_email_to_entry_unique_id(hass: HomeAssistant) -> None:
    """Reauth uses entry-stored email even if user-supplied data changes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="locked@example.com",
        data={
            CONF_USERNAME: "locked@example.com",
            CONF_REFRESH_TOKEN: "stale-rt",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    new_auth = _mock_auth(refresh_token="fresh-rt", email="locked@example.com")
    login_mock = AsyncMock(return_value=new_auth)
    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        login_mock,
    ), patch("custom_components.ecobee.async_setup_entry", return_value=True):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "new-pw"},
        )
        await hass.async_block_till_done()

    assert login_mock.await_count == 1
    args = login_mock.await_args.args
    kwargs = login_mock.await_args.kwargs
    used_email = args[1] if len(args) >= 2 else kwargs.get("email")
    assert used_email == "locked@example.com"


async def test_reauth_invalid_credentials_shows_form_error(hass: HomeAssistant) -> None:
    """Bad password during reauth must keep the entry intact."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_REFRESH_TOKEN: "stale-rt",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )

    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(side_effect=InvalidCredentialsError("bad")),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "wrong"},
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "auth"}
    assert entry.data[CONF_REFRESH_TOKEN] == "stale-rt"


async def test_reauth_with_legacy_entry_using_title_for_email(hass: HomeAssistant) -> None:
    """Older entries may have email only in title; reauth must still work."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="legacy@example.com",
        title="legacy@example.com",
        data={
            # CONF_USERNAME deliberately missing (legacy entry).
            CONF_REFRESH_TOKEN: "stale-rt",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["step_id"] == "reauth_confirm"

    new_auth = _mock_auth(refresh_token="fresh-rt")
    with patch(
        "custom_components.ecobee.config_flow.EcobeeAuth.login",
        AsyncMock(return_value=new_auth),
    ), patch("custom_components.ecobee.async_setup_entry", return_value=True):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "pw"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == "abort"
    assert result2["reason"] == "reauth_successful"
