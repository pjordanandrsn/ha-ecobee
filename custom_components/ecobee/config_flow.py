"""Config flow for the Ecobee Anderson fork integration.

UX:
  - User opens Settings -> Devices & Services -> Add Integration ->
    "Ecobee (Anderson fork)".
  - First step takes ``email`` + ``password`` and runs ROPG.
  - On a non-MFA account: persist (email, refresh_token) immediately.
  - On a 2FA-enabled account: branch through one (or two) extra steps
    to gather the second factor, then persist (email, refresh_token).
  - entry.unique_id = email so duplicate adds get aborted.
  - entry.title = email (until first successful poll, when we could
    enrich it with the household name; for v0.1 we keep it simple).

Reauth:
  - Triggered when the coordinator raises ConfigEntryAuthFailed.
  - We prompt for password only; email is locked to the entry's stored
    unique_id. If the account has MFA on, the same MFA branch fires.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import aiohttp_client

from .auth import (
    EcobeeAuth,
    InvalidCredentialsError,
    MFAExpiredError,
    MFAInvalidCodeError,
    MFANotSupportedError,
    MFARateLimitedError,
    MFARequiredError,
)
from .const import (
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
    MFA_TYPE_OOB,
    MFA_TYPE_OTP,
    MFA_TYPE_PUSH,
)

_LOGGER = logging.getLogger(__name__)


# Step IDs (kept as constants so tests can reference them without
# string-literal drift if we rename steps later).
STEP_USER = "user"
STEP_REAUTH_CONFIRM = "reauth_confirm"
STEP_MFA_SELECT_FACTOR = "mfa_select_factor"
STEP_MFA_CODE = "mfa_code"


def _factor_label(authenticator: dict) -> str:
    """Human-readable label for a single authenticator entry.

    Auth0's name field for OTP is usually "Authenticator app"; for SMS
    it's "Phone Number" with the masked number in ``name`` itself; push
    factors include the device name. Fall back to the type if name is
    missing.
    """
    name = authenticator.get("name") or ""
    a_type = authenticator.get("authenticator_type") or ""
    channel = authenticator.get("oob_channel") or ""

    if a_type == MFA_TYPE_OTP:
        return name or "Authenticator app"
    if a_type == MFA_TYPE_OOB and channel == "sms":
        return f"Text message: {name}" if name else "Text message"
    if a_type == MFA_TYPE_OOB:
        return f"Out-of-band: {name}" if name else f"Out-of-band ({channel})"
    if a_type == MFA_TYPE_PUSH:
        return f"Push: {name}" if name else "Push notification"
    return name or a_type or "MFA factor"


class EcobeeFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the Ecobee Anderson fork."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: config_entries.ConfigEntry | None = None
        # MFA branch state. Populated when async_step_user / reauth
        # catches MFARequiredError, cleared when the entry is created
        # (or when an expired mfa_token bounces us back to step user).
        self._mfa_email: Optional[str] = None
        self._mfa_token: Optional[str] = None
        self._mfa_authenticators: list[dict] = []
        self._mfa_chosen: Optional[dict] = None
        self._mfa_oob_code: Optional[str] = None
        # Whether the active MFA flow is part of reauth (drives whether
        # success creates a new entry or updates the existing one).
        self._mfa_is_reauth: bool = False

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    async def _try_login(
        self, email: str, password: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Run ROPG once. Returns (entry_data, error_key).

        On the happy (non-MFA) path entry_data is the dict to merge
        into entry.data; on a normal failure error_key is one of the
        keys in ``translations/en.json``.

        MFA-required is NOT signalled through this helper — it raises
        out of EcobeeAuth.login and the caller catches MFARequiredError
        directly so it can stash state and transition to the MFA step.
        """
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            auth = await EcobeeAuth.login(session, email, password)
        except MFARequiredError:
            # Re-raise so the step handler can branch into the MFA UI;
            # we deliberately don't squash this into an error string.
            raise
        except InvalidCredentialsError as ex:
            _LOGGER.warning("ecobee login rejected: %s", ex)
            return None, "auth"
        except MFANotSupportedError as ex:
            # Either an unsupported factor type (push) or a tenant that
            # said MFA-required without giving us an mfa_token.
            _LOGGER.warning("ecobee MFA not supported: %s", ex)
            return None, "mfa_unsupported"
        except aiohttp.ClientConnectorError as ex:
            _LOGGER.error("Cannot reach auth.ecobee.com: %s", ex)
            return None, "auth0_unreachable"
        except asyncio.TimeoutError as ex:
            _LOGGER.error("Timeout reaching auth.ecobee.com: %s", ex)
            return None, "auth0_unreachable"
        except RuntimeError as ex:
            _LOGGER.error("ecobee login failed: %s", ex, exc_info=True)
            return None, "internal"
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

    def _stash_mfa_state(
        self, email: str, exc: MFARequiredError, *, is_reauth: bool
    ) -> None:
        """Persist MFARequiredError details on self for the next step."""
        self._mfa_email = email
        self._mfa_token = exc.mfa_token
        self._mfa_authenticators = list(exc.authenticators)
        self._mfa_chosen = None
        self._mfa_oob_code = None
        self._mfa_is_reauth = is_reauth

    def _clear_mfa_state(self) -> None:
        self._mfa_email = None
        self._mfa_token = None
        self._mfa_authenticators = []
        self._mfa_chosen = None
        self._mfa_oob_code = None
        self._mfa_is_reauth = False

    async def _challenge_chosen_factor(self) -> Optional[str]:
        """Fire the SMS / push challenge if needed; returns error_key or None.

        OTP factors are a no-op. OOB factors POST /mfa/challenge and
        store the resulting oob_code on self for the eventual submit.
        """
        assert self._mfa_chosen is not None
        assert self._mfa_token is not None
        a_type = self._mfa_chosen.get("authenticator_type", "")
        if a_type == MFA_TYPE_PUSH:
            # We don't yet wire the push polling dance; surface a
            # dedicated message instead of letting the user dead-end.
            return "mfa_push_unsupported"
        if a_type == MFA_TYPE_OTP:
            return None
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            resp = await EcobeeAuth.challenge_mfa(
                session,
                mfa_token=self._mfa_token,
                authenticator_id=self._mfa_chosen["id"],
                authenticator_type=a_type,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("ecobee MFA challenge failed")
            return "internal"
        self._mfa_oob_code = (resp or {}).get("oob_code")
        return None

    async def _complete_mfa_entry(self, code: str) -> tuple[Optional[dict], Optional[str]]:
        """Submit the MFA code and return (entry_data, error_key).

        ``error_key`` may be a "soft" error (wrong code -> stay on the
        MFA step) or a "hard" error like ``mfa_expired`` that the
        caller should handle by resetting state and bouncing back to
        the password step.
        """
        assert self._mfa_chosen is not None
        assert self._mfa_token is not None
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            auth = await EcobeeAuth.submit_mfa(
                session,
                mfa_token=self._mfa_token,
                authenticator_type=self._mfa_chosen["authenticator_type"],
                code=code,
                oob_code=self._mfa_oob_code,
                email=self._mfa_email,
            )
        except MFAInvalidCodeError as ex:
            _LOGGER.info("ecobee MFA wrong code: %s", ex)
            return None, "mfa_invalid_code"
        except MFAExpiredError as ex:
            _LOGGER.info("ecobee MFA token expired: %s", ex)
            return None, "mfa_expired"
        except MFARateLimitedError as ex:
            _LOGGER.warning("ecobee MFA rate-limited: %s", ex)
            return None, "mfa_rate_limited"
        except MFANotSupportedError as ex:
            _LOGGER.warning("ecobee MFA not supported: %s", ex)
            return None, "mfa_unsupported"
        except aiohttp.ClientConnectorError as ex:
            _LOGGER.error("Cannot reach auth.ecobee.com: %s", ex)
            return None, "auth0_unreachable"
        except asyncio.TimeoutError as ex:
            _LOGGER.error("Timeout reaching auth.ecobee.com: %s", ex)
            return None, "auth0_unreachable"
        except RuntimeError as ex:
            _LOGGER.error("ecobee MFA submit failed: %s", ex, exc_info=True)
            return None, "internal"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during ecobee MFA submit")
            return None, "internal"

        return (
            {
                CONF_USERNAME: self._mfa_email,
                CONF_REFRESH_TOKEN: auth.refresh_token,
            },
            None,
        )

    async def _finish_with_entry_data(self, entry_data: dict) -> Any:
        """Either create a new entry or update the existing reauth entry."""
        if self._mfa_is_reauth and self._reauth_entry is not None:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data={**self._reauth_entry.data, **entry_data}
            )
            self._clear_mfa_state()
            return self.async_abort(reason="reauth_successful")

        email = entry_data[CONF_USERNAME]
        await self.async_set_unique_id(email)
        self._abort_if_unique_id_configured()
        self._clear_mfa_state()
        return self.async_create_entry(title=email, data=entry_data)

    # -------------------------------------------------------------------
    # Initial-setup steps
    # -------------------------------------------------------------------

    async def async_step_user(self, user_input=None):
        """Handle the user-initiated config flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            try:
                entry_data, error = await self._try_login(email, password)
            except MFARequiredError as exc:
                self._stash_mfa_state(email, exc, is_reauth=False)
                if not self._mfa_authenticators:
                    # Couldn't enumerate factors; surface and let user
                    # retry the password step.
                    self._clear_mfa_state()
                    errors["base"] = "mfa_no_factors"
                else:
                    return await self._dispatch_after_factors_known()

            if not errors:
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
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _dispatch_after_factors_known(self) -> Any:
        """Single factor -> jump to code step; multi -> show selector."""
        if len(self._mfa_authenticators) == 1:
            self._mfa_chosen = self._mfa_authenticators[0]
            challenge_err = await self._challenge_chosen_factor()
            if challenge_err is not None:
                # Surface on the password step since we have nothing
                # better to fall back to (selector hasn't rendered).
                self._clear_mfa_state()
                return self.async_show_form(
                    step_id=STEP_USER,
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_USERNAME): str,
                            vol.Required(CONF_PASSWORD): str,
                        }
                    ),
                    errors={"base": challenge_err},
                )
            return await self.async_step_mfa_code()
        return await self.async_step_mfa_select_factor()

    async def async_step_mfa_select_factor(self, user_input=None):
        """Render a radio list when 2+ factors exist; pick one and proceed."""
        errors: dict[str, str] = {}
        # Build a label-keyed map so the form's selector can show the
        # human label and we can recover the dict from the chosen id.
        factor_choices = {a["id"]: _factor_label(a) for a in self._mfa_authenticators}

        if user_input is not None:
            chosen_id = user_input.get("authenticator_id")
            chosen = next(
                (a for a in self._mfa_authenticators if a.get("id") == chosen_id),
                None,
            )
            if chosen is None:
                errors["base"] = "internal"
            else:
                self._mfa_chosen = chosen
                challenge_err = await self._challenge_chosen_factor()
                if challenge_err is not None:
                    errors["base"] = challenge_err
                else:
                    return await self.async_step_mfa_code()

        return self.async_show_form(
            step_id=STEP_MFA_SELECT_FACTOR,
            data_schema=vol.Schema(
                {
                    vol.Required("authenticator_id"): vol.In(factor_choices),
                }
            ),
            description_placeholders={"username": self._mfa_email or ""},
            errors=errors,
        )

    async def async_step_mfa_code(self, user_input=None):
        """Collect the second-factor code and complete the grant."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = (user_input.get("code") or "").strip()
            entry_data, error = await self._complete_mfa_entry(code)
            if error is None:
                assert entry_data is not None
                return await self._finish_with_entry_data(entry_data)

            if error == "mfa_expired":
                # Hard reset back to the password step — the mfa_token
                # cannot be re-used after expiry; user must re-login.
                self._clear_mfa_state()
                return self.async_show_form(
                    step_id=STEP_USER,
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_USERNAME): str,
                            vol.Required(CONF_PASSWORD): str,
                        }
                    ),
                    errors={"base": "mfa_expired"},
                )

            errors["base"] = error

        # Build the form. If we know the chosen factor type, customize
        # the description placeholder with a hint at where the code
        # comes from.
        chosen_label = (
            _factor_label(self._mfa_chosen) if self._mfa_chosen else "your second factor"
        )
        return self.async_show_form(
            step_id=STEP_MFA_CODE,
            data_schema=vol.Schema(
                {
                    vol.Required("code"): str,
                }
            ),
            description_placeholders={
                "username": self._mfa_email or "",
                "factor": chosen_label,
            },
            errors=errors,
        )

    # -------------------------------------------------------------------
    # Reauth steps
    # -------------------------------------------------------------------

    async def async_step_reauth(self, entry_data):
        """Triggered by ConfigEntryAuthFailed; ask the user for a fresh password."""
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
            try:
                # Reauth is bound to the entry's email — users who need
                # a different account must remove and re-add. This
                # prevents silently rebinding all entities to a
                # different account.
                entry_data, error = await self._try_login(default_email, password)
            except MFARequiredError as exc:
                self._stash_mfa_state(default_email, exc, is_reauth=True)
                if not self._mfa_authenticators:
                    self._clear_mfa_state()
                    errors["base"] = "mfa_no_factors"
                else:
                    return await self._dispatch_after_factors_known()

            if not errors:
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
