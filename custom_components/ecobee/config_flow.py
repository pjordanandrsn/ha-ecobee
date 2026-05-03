"""Config flow for the Ecobee community fork integration.

UX (v0.3 — universal-login):
  - User opens Settings -> Devices & Services -> Add Integration ->
    "Ecobee (community fork)".
  - Single step takes ``email`` + ``password`` and runs the full Auth0
    universal-login flow against ecobee's tenant.
  - For 2FA-enabled accounts: Auth0 chains an /u/mfa-* prompt page
    between password submit and the callback redirect. Our auth
    backend's generic prompt handler POSTs ``state=<state>&action=
    default`` which works for OTP / push challenges that the user
    completed externally (authenticator app, push approval). Pages
    that require typing a code into Auth0's hosted form are NOT
    supported by this single-step flow — the user has to complete
    that part on ecobee.com first.
  - On success: persist (email, refresh_token); entry.unique_id =
    email so duplicate adds get aborted; entry.title = email.

Reauth:
  - Triggered when the coordinator raises ConfigEntryAuthFailed.
  - Single-step: prompts for password only; email is locked to the
    entry's stored unique_id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import aiohttp_client

from .auth import (
    EcobeeAuth,
    InvalidCredentialsError,
    MFACodeExpiredError,
    MFACodeInvalidError,
    MFACodeRequiredError,
)
from .const import (
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


STEP_USER = "user"
STEP_REAUTH_CONFIRM = "reauth_confirm"
STEP_MFA_CODE = "mfa_code"

# Schema for the MFA-code form. Single field — Auth0 only ever asks
# for a 6-digit OTP / SMS / recovery code on these prompts; the
# challenge type is shown via description_placeholders so the user
# knows where to look (authenticator app vs SMS vs backup codes).
_MFA_CODE_SCHEMA = vol.Schema(
    {
        vol.Required("code"): str,
    }
)

# Schema for the email + password form. Module-level so the
# user-step and the bounce-back-to-user paths render the same fields.
_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _classify_login_error(ex: RuntimeError) -> str:
    """Map a RuntimeError from EcobeeAuth.login to a translation key.

    auth.py tags every RuntimeError with ``step=<name>`` — peel it off
    so the user sees WHERE the chain broke instead of a generic
    "internal error".
    """
    msg = str(ex)
    if "step=authorize" in msg:
        return "auth0_step_authorize"
    if "step=login_form" in msg:
        return "auth0_step_login_form"
    if "step=resume" in msg:
        return "auth0_step_resume"
    if "step=custom-prompt" in msg:
        return "auth0_step_custom_prompt"
    if "code exchange" in msg.lower():
        return "code_exchange"
    if "redirect" in msg.lower() or "no state" in msg.lower():
        return "auth0_redirect"
    return "internal"


class EcobeeFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the Ecobee community fork."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: config_entries.ConfigEntry | None = None
        # MFA-code-step state. Populated by async_step_user (or the
        # reauth-confirm step) when EcobeeAuth.login raises
        # MFACodeRequiredError; cleared on successful entry creation
        # or when the MFA prompt expires and the user has to restart.
        self._mfa_prompt_url: Optional[str] = None
        self._mfa_state: Optional[str] = None
        self._mfa_challenge_type: Optional[str] = None
        self._mfa_verifier: Optional[str] = None
        self._mfa_email: Optional[str] = None
        self._mfa_is_reauth: bool = False

    async def _try_login(
        self, email: str, password: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Run the universal-login flow once. Returns (entry_data, error_key).

        On success, ``entry_data`` is the dict to merge into entry.data.
        On failure, ``error_key`` is one of the keys defined in
        translations/en.json.
        """
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            auth = await EcobeeAuth.login(session, email, password)
        except MFACodeRequiredError:
            # Don't squash into an error string — caller (the user step
            # handler) catches this and transitions to async_step_mfa_code
            # with the prompt state stashed on self.
            raise
        except InvalidCredentialsError as ex:
            _LOGGER.warning("ecobee login rejected: %s", ex)
            return None, "auth"
        except aiohttp.ClientConnectorError as ex:
            _LOGGER.error("Cannot reach auth.ecobee.com: %s", ex)
            return None, "auth0_unreachable"
        except asyncio.TimeoutError as ex:
            _LOGGER.error("Timeout reaching auth.ecobee.com: %s", ex)
            return None, "auth0_unreachable"
        except RuntimeError as ex:
            _LOGGER.error("ecobee login failed: %s", ex, exc_info=True)
            return None, _classify_login_error(ex)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during ecobee login")
            return None, "internal"

        return (
            {
                CONF_USERNAME: email,
                CONF_REFRESH_TOKEN: auth.refresh_token,
            },
            None,
        )

    async def async_step_user(self, user_input=None):
        """Handle the user-initiated config flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            try:
                entry_data, error = await self._try_login(email, password)
            except MFACodeRequiredError as ex:
                self._stash_mfa_state(ex, is_reauth=False)
                return await self.async_step_mfa_code()

            if error is None:
                assert entry_data is not None
                await self.async_set_unique_id(entry_data[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=entry_data[CONF_USERNAME], data=entry_data
                )
            errors["base"] = error

        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    def _stash_mfa_state(
        self, ex: MFACodeRequiredError, *, is_reauth: bool
    ) -> None:
        """Persist the MFACodeRequiredError state across config-flow steps."""
        self._mfa_prompt_url = ex.prompt_url
        self._mfa_state = ex.state
        self._mfa_challenge_type = ex.challenge_type
        self._mfa_verifier = ex.verifier
        self._mfa_email = ex.email
        self._mfa_is_reauth = is_reauth

    def _clear_mfa_state(self) -> None:
        self._mfa_prompt_url = None
        self._mfa_state = None
        self._mfa_challenge_type = None
        self._mfa_verifier = None
        self._mfa_email = None
        self._mfa_is_reauth = False

    async def async_step_mfa_code(self, user_input=None):
        """Collect the MFA code and resume the auth chain."""
        errors: dict[str, str] = {}
        # Friendly label for the prompt source — placed in
        # description_placeholders so the form copy can say "Enter the
        # 6-digit code from your authenticator app" or similar.
        challenge_label = {
            "otp": "authenticator app",
            "sms": "text message",
            "recovery": "recovery codes",
        }.get(self._mfa_challenge_type or "", "authenticator app")

        if user_input is not None:
            code = (user_input.get("code") or "").strip()
            assert self._mfa_prompt_url and self._mfa_state
            assert self._mfa_verifier is not None
            assert self._mfa_email is not None
            try:
                session = aiohttp_client.async_get_clientsession(self.hass)
                auth = await EcobeeAuth.continue_with_mfa_code(
                    session,
                    prompt_url=self._mfa_prompt_url,
                    state=self._mfa_state,
                    code=code,
                    verifier=self._mfa_verifier,
                    email=self._mfa_email,
                )
            except MFACodeInvalidError:
                errors["base"] = "mfa_invalid_code"
            except MFACodeExpiredError:
                self._clear_mfa_state()
                return self.async_show_form(
                    step_id=STEP_USER,
                    data_schema=_USER_SCHEMA,
                    errors={"base": "mfa_expired"},
                )
            except MFACodeRequiredError as ex:
                # Chained MFA prompt — re-stash + render again.
                self._stash_mfa_state(ex, is_reauth=self._mfa_is_reauth)
                return await self.async_step_mfa_code()
            except InvalidCredentialsError:
                errors["base"] = "auth"
            except RuntimeError as ex:
                _LOGGER.error("ecobee MFA resume failed: %s", ex, exc_info=True)
                errors["base"] = _classify_login_error(ex)
            else:
                entry_data = {
                    CONF_USERNAME: self._mfa_email,
                    CONF_REFRESH_TOKEN: auth.refresh_token,
                }
                is_reauth = self._mfa_is_reauth
                self._clear_mfa_state()
                if is_reauth:
                    entry = self._reauth_entry
                    assert entry is not None
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, **entry_data}
                    )
                    return self.async_abort(reason="reauth_successful")
                await self.async_set_unique_id(entry_data[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=entry_data[CONF_USERNAME], data=entry_data
                )

        return self.async_show_form(
            step_id=STEP_MFA_CODE,
            data_schema=_MFA_CODE_SCHEMA,
            description_placeholders={
                "challenge_label": challenge_label,
                "username": self._mfa_email or "",
            },
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        """Triggered by ConfigEntryAuthFailed; ask for a fresh password."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Collect a fresh password and re-mint the refresh_token."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None
        # Older entries may have stored email under a different key or
        # only in the title. Fall back through both.
        default_email = entry.data.get(CONF_USERNAME) or entry.title or ""

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            # Reauth is bound to the entry's email — users who need a
            # different account must remove and re-add. This prevents
            # silently rebinding all entities to a different account.
            try:
                entry_data, error = await self._try_login(default_email, password)
            except MFACodeRequiredError as ex:
                self._stash_mfa_state(ex, is_reauth=True)
                return await self.async_step_mfa_code()
            if error is None:
                assert entry_data is not None
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, **entry_data}
                )
                return self.async_abort(reason="reauth_successful")
            errors["base"] = error

        return self.async_show_form(
            step_id=STEP_REAUTH_CONFIRM,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"username": default_email},
            errors=errors,
        )
