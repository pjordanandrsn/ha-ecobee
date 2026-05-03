"""Resource Owner Password Grant (ROPG) auth against ecobee's Auth0 tenant.

Why ROPG (and not the proper code-flow + PIN dance the core integration
uses): ecobee shut down public dev-portal API-key registration in 2024,
so a new user can no longer create an API key to drive the core HA
integration. ROPG against the public web-app client_id mirrors what
ecobee.com itself does when you sign in there; the bash project
``r00k/ecobee-cli`` proved this works without an API key.

This is much simpler than the parallel Generac fork's auth.py: no DPoP,
no universal-login HTML scraping, no PKCE. Just two POSTs to
``/oauth/token`` (one for password grant, one for refresh).

For MFA-enabled accounts (v0.2 and later), the password grant returns
403 mfa_required + an mfa_token JWT. We then enumerate the user's
second-factor authenticators (/mfa/authenticators), prompt for a code
through the config flow, and re-POST /oauth/token with one of the
Auth0 MFA grant types (mfa-otp or mfa-oob) plus the code. The resulting
refresh_token works for unattended refreshes forever after — MFA is
verified once at config-entry creation, never again.

Persistence pattern: ``set_refresh_token_persist_callback`` lets the
caller (``__init__.py``) hook into Auth0 token rotation and write the
new RT back into the ConfigEntry. The same async-Lock + double-check
pattern as the Generac fork prevents concurrent refresh storms.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import aiohttp

from .const import (
    AUTH_AUDIENCE,
    AUTH_SCOPE,
    AUTH_URL,
    GRANT_TYPE_MFA_OOB,
    GRANT_TYPE_MFA_OTP,
    MFA_AUTHENTICATORS_URL,
    MFA_CHALLENGE_URL,
    MFA_TYPE_OOB,
    MFA_TYPE_OTP,
    WEB_CLIENT_ID,
)

_LOGGER = logging.getLogger(__name__)


class InvalidGrantError(Exception):
    """Raised when the refresh token has been invalidated server-side.

    The caller should map this to ``ConfigEntryAuthFailed`` so HA prompts
    the user to re-authenticate.
    """


class InvalidCredentialsError(Exception):
    """Raised when the user-supplied email/password is rejected at login."""


class MFANotSupportedError(Exception):
    """Raised when the account's MFA factor type cannot be handled.

    v0.2 wires OTP (authenticator app) + OOB SMS. Push-notification
    factors are recognised but rejected with this error so the user
    knows it isn't a generic auth bug. They can fall back to
    authenticator-app or SMS in the meantime, or wait for push support
    to land.
    """


class MFARequiredError(Exception):
    """Raised by ``EcobeeAuth.login`` when the account has 2FA enabled.

    Carries the short-lived ``mfa_token`` JWT returned by Auth0 and
    (after enrichment by ``list_mfa_authenticators``) the list of
    configured authenticators. The config flow catches this, prompts
    the user for a second-factor code, and calls back into
    ``submit_mfa`` to complete the grant.
    """

    def __init__(
        self,
        mfa_token: str,
        authenticators: Optional[list[dict]] = None,
    ) -> None:
        super().__init__("MFA required")
        self.mfa_token = mfa_token
        # Populated by the caller after list_mfa_authenticators(); we
        # accept it here so the exception object can be a single
        # carrier through the config flow without needing a separate
        # pre-fetch step.
        self.authenticators: list[dict] = authenticators or []


class MFAInvalidCodeError(Exception):
    """Raised when the user-supplied OTP / SMS code is wrong.

    Distinct from the ``mfa_token`` having expired (see
    ``MFAExpiredError``) so the config flow can keep the user on the
    same MFA-code form for a retry instead of restarting from password.
    """


class MFAExpiredError(Exception):
    """Raised when the ``mfa_token`` is no longer valid.

    Auth0 mfa_tokens live ~10 minutes. When this fires the config flow
    must restart from the email/password step — the user has to log in
    again to get a fresh mfa_token before they can submit a code.
    """


class MFARateLimitedError(Exception):
    """Raised when Auth0 blocks further MFA submits (429 too_many_attempts).

    The user has burned their attempt allowance and must wait (Auth0's
    default lockout is several minutes; the precise duration isn't
    surfaced in the response).
    """


class EcobeeAuth:
    """Holds the long-lived refresh token and mints fresh access tokens.

    Instances are reused across the lifetime of a ConfigEntry. The same
    aiohttp session is reused for token + API calls.
    """

    # Refresh slightly before expiry so callers always see a fresh token.
    _ACCESS_TOKEN_LEEWAY = 60

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        *,
        email: Optional[str] = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._email = email
        self._access_token: Optional[str] = None
        self._access_token_exp: float = 0.0
        self._refresh_lock = asyncio.Lock()
        self._rt_persist_cb: Optional[Callable[[str], Awaitable[None]]] = None

    def set_refresh_token_persist_callback(
        self, cb: Optional[Callable[[str], Awaitable[None]]]
    ) -> None:
        """Register an async callback invoked when Auth0 rotates the RT.

        The callback receives the new refresh token and is responsible
        for persisting it (typically into the ConfigEntry's ``data``
        dict). Auth0 may or may not rotate on each refresh depending on
        tenant configuration; we handle both cases.
        """
        self._rt_persist_cb = cb

    @classmethod
    async def login(
        cls, session: aiohttp.ClientSession, email: str, password: str
    ) -> "EcobeeAuth":
        """Run ROPG and return a ready instance.

        Raises:
            InvalidCredentialsError: bad email/password.
            MFARequiredError: account has 2FA enabled. ``mfa_token`` and
                ``authenticators`` are populated; caller should branch
                to the MFA challenge / submit dance.
            RuntimeError: any other unexpected response.
        """
        body = {
            "grant_type": "password",
            "username": email,
            "password": password,
            "client_id": WEB_CLIENT_ID,
            "audience": AUTH_AUDIENCE,
            "scope": AUTH_SCOPE,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        async with session.post(AUTH_URL, data=body, headers=headers) as resp:
            status = resp.status
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                payload = {"raw": (await resp.text())[:200]}

        if status == 200:
            return cls._auth_from_token_payload(session, payload, email=email)

        # Map known Auth0 errors:
        #   403 mfa_required + mfa_token=<jwt> -> MFARequiredError
        #     (Auth0 standard shape per its "MFA in Resource Owner
        #     Password Grant" doc — what ecobee uses for 2FA accounts.)
        #   400 invalid_grant -> bad email/password
        #   400 invalid_request + error_description mentions MFA/2FA
        #     -> legacy phrasing kept for older Auth0 tenant configs;
        #     no mfa_token is supplied so we can't continue from here.
        if status in (400, 403):
            mfa_required = await cls._maybe_initiate_mfa(
                session, payload, email=email
            )
            if mfa_required is not None:
                raise mfa_required
            err = payload.get("error", "")
            if err == "invalid_grant":
                raise InvalidCredentialsError(
                    payload.get("error_description") or "invalid email or password"
                )
        raise RuntimeError(f"login failed: status={status} payload={payload}")

    @classmethod
    async def _maybe_initiate_mfa(
        cls,
        session: aiohttp.ClientSession,
        payload: dict,
        *,
        email: Optional[str] = None,
    ) -> Optional[MFARequiredError]:
        """Detect Auth0's MFA-required error and return an enriched typed error.

        Returns ``None`` if the payload isn't an MFA challenge. When it
        IS an MFA challenge with a real ``mfa_token``, we eagerly
        enumerate the authenticators so the caller has everything it
        needs to render the next form without a second round-trip.
        """
        err = (payload.get("error") or "").lower()
        desc = (payload.get("error_description") or "").lower()
        mfa_token = payload.get("mfa_token")

        looks_like_mfa = (
            err == "mfa_required"
            or "mfa" in err
            or "mfa" in desc
            or "multifactor" in desc
        )
        if not looks_like_mfa:
            return None

        if not mfa_token:
            # Older / misconfigured tenants surface MFA as 400
            # invalid_request without an mfa_token. Without a token we
            # can't continue, so surface the legacy "not supported"
            # shape to keep behaviour consistent for users who hit it.
            raise MFANotSupportedError(
                "ecobee account has 2FA but the auth tenant did not "
                "return an mfa_token; cannot complete MFA flow."
            )

        # Eagerly enumerate authenticators so the config flow has
        # everything it needs. If this call fails we still raise
        # MFARequiredError with an empty list — the flow will surface
        # an "internal" error rather than crashing.
        try:
            authenticators = await cls._fetch_authenticators(session, mfa_token)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("ecobee /mfa/authenticators enumeration failed")
            authenticators = []
        return MFARequiredError(mfa_token=mfa_token, authenticators=authenticators)

    @staticmethod
    async def _fetch_authenticators(
        session: aiohttp.ClientSession, mfa_token: str
    ) -> list[dict]:
        """GET /mfa/authenticators -> normalized list of dicts.

        Auth0 returns an array; we filter to ``active`` entries (an
        inactive authenticator can't be used to satisfy the grant).
        """
        headers = {
            "Authorization": f"Bearer {mfa_token}",
            "Accept": "application/json",
        }
        async with session.get(MFA_AUTHENTICATORS_URL, headers=headers) as resp:
            status = resp.status
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                data = []
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(
                f"/mfa/authenticators failed: status={status} body={data!r:.200}"
            )
        return [
            a
            for a in data
            if isinstance(a, dict) and a.get("active", True) is not False
        ]

    @classmethod
    async def list_mfa_authenticators(
        cls, session: aiohttp.ClientSession, mfa_token: str
    ) -> list[dict]:
        """Public wrapper around the authenticators enumeration.

        Useful when the config flow needs to re-fetch (e.g. user
        backed out of the factor selection step and came back).
        """
        return await cls._fetch_authenticators(session, mfa_token)

    @classmethod
    async def challenge_mfa(
        cls,
        session: aiohttp.ClientSession,
        *,
        mfa_token: str,
        authenticator_id: str,
        authenticator_type: str,
    ) -> Optional[dict]:
        """Trigger an OOB challenge for SMS / push factors.

        OTP (authenticator app) factors don't need a challenge call —
        the user just reads the current 6-digit code from their app —
        so this is a no-op for ``MFA_TYPE_OTP`` and returns ``None``.

        For OOB factors (SMS / push), POST /mfa/challenge fires the
        text message or push notification. We return the response dict
        which contains ``oob_code`` (passed back to /oauth/token along
        with the user's code) and ``binding_method``.
        """
        if authenticator_type == MFA_TYPE_OTP:
            return None
        body = {
            "client_id": WEB_CLIENT_ID,
            "mfa_token": mfa_token,
            "challenge_type": "oob",
            "authenticator_id": authenticator_id,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with session.post(MFA_CHALLENGE_URL, json=body, headers=headers) as resp:
            status = resp.status
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                payload = {"raw": (await resp.text())[:200]}
        if status != 200:
            raise RuntimeError(
                f"/mfa/challenge failed: status={status} payload={payload}"
            )
        return payload

    @classmethod
    async def submit_mfa(
        cls,
        session: aiohttp.ClientSession,
        *,
        mfa_token: str,
        authenticator_type: str,
        code: str,
        oob_code: Optional[str] = None,
        email: Optional[str] = None,
    ) -> "EcobeeAuth":
        """Complete the MFA grant and return a ready EcobeeAuth handle.

        For OTP, ``code`` is the 6-digit value from the user's
        authenticator app. For OOB, ``code`` is the binding code from
        the SMS / push prompt and ``oob_code`` is what
        ``challenge_mfa`` returned.

        Raises:
            MFAInvalidCodeError: user typed the wrong code; flow should
                re-prompt on the same form.
            MFAExpiredError: ``mfa_token`` aged out (~10 min); flow
                must restart at email/password.
            MFARateLimitedError: too many bad attempts; user is
                temporarily locked out by Auth0.
            MFANotSupportedError: caller passed a factor type we don't
                wire (currently ``push-notification``).
            RuntimeError: any other unexpected response.
        """
        if authenticator_type == MFA_TYPE_OTP:
            body = {
                "grant_type": GRANT_TYPE_MFA_OTP,
                "client_id": WEB_CLIENT_ID,
                "mfa_token": mfa_token,
                "otp": code,
            }
        elif authenticator_type == MFA_TYPE_OOB:
            if not oob_code:
                # Programmer error; OOB requires the oob_code from the
                # /mfa/challenge response or the grant will 400.
                raise RuntimeError("submit_mfa: OOB factor needs oob_code")
            body = {
                "grant_type": GRANT_TYPE_MFA_OOB,
                "client_id": WEB_CLIENT_ID,
                "mfa_token": mfa_token,
                "oob_code": oob_code,
                "binding_code": code,
            }
        else:
            raise MFANotSupportedError(
                f"MFA factor type '{authenticator_type}' is not yet "
                "supported by this integration. Use an authenticator "
                "app (OTP) or SMS instead."
            )

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        async with session.post(AUTH_URL, data=body, headers=headers) as resp:
            status = resp.status
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                payload = {"raw": (await resp.text())[:200]}

        if status == 200:
            return cls._auth_from_token_payload(session, payload, email=email)

        if status == 429 or (payload.get("error") or "") == "too_many_attempts":
            raise MFARateLimitedError(
                payload.get("error_description")
                or "Too many MFA attempts; wait a few minutes and try again."
            )

        if status == 403 and (payload.get("error") or "") == "invalid_grant":
            desc = (payload.get("error_description") or "").lower()
            if "expired" in desc or "expire" in desc:
                raise MFAExpiredError(
                    payload.get("error_description")
                    or "MFA session expired; re-enter password to start over."
                )
            # Auth0's wrong-code message is "Invalid otp_code" or
            # "Invalid binding_code" depending on grant type. Catch
            # both with a substring match so wording shifts don't
            # demote the typed error to RuntimeError.
            if "invalid" in desc and ("otp" in desc or "binding" in desc or "code" in desc):
                raise MFAInvalidCodeError(
                    payload.get("error_description") or "Invalid code."
                )
            # Anything else 403/invalid_grant gets the generic typed
            # error so the caller doesn't restart unnecessarily.
            raise MFAInvalidCodeError(
                payload.get("error_description") or "MFA code rejected."
            )

        raise RuntimeError(f"MFA submit failed: status={status} payload={payload}")

    @classmethod
    def _auth_from_token_payload(
        cls,
        session: aiohttp.ClientSession,
        payload: dict,
        *,
        email: Optional[str] = None,
    ) -> "EcobeeAuth":
        """Build an EcobeeAuth from a /oauth/token 200 response.

        Shared by the password grant (login) and the MFA grant
        (submit_mfa) since both return the identical token shape.
        """
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "no refresh_token in token response (scope did not include offline_access?)"
            )
        auth = cls(session, refresh_token, email=email)
        auth._access_token = payload["access_token"]
        auth._access_token_exp = time.time() + int(payload.get("expires_in", 0))
        _LOGGER.info(
            "ecobee token grant OK: expires_in=%s scope=%s token_type=%s",
            payload.get("expires_in"),
            payload.get("scope"),
            payload.get("token_type"),
        )
        return auth

    @classmethod
    def from_storage(
        cls,
        session: aiohttp.ClientSession,
        refresh_token: str,
        *,
        email: Optional[str] = None,
    ) -> "EcobeeAuth":
        """Construct an instance from a previously stored refresh token."""
        return cls(session, refresh_token, email=email)

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def email(self) -> Optional[str]:
        return self._email

    async def ensure_access_token(self) -> str:
        """Return a non-expired access token, refreshing if necessary."""
        if (
            self._access_token
            and time.time() < self._access_token_exp - self._ACCESS_TOKEN_LEEWAY
        ):
            return self._access_token

        async with self._refresh_lock:
            # Double-check inside the lock — concurrent callers may have
            # already refreshed by the time we acquired it.
            if (
                self._access_token
                and time.time() < self._access_token_exp - self._ACCESS_TOKEN_LEEWAY
            ):
                return self._access_token
            await self._refresh()
            assert self._access_token is not None
            return self._access_token

    async def _refresh(self) -> None:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": WEB_CLIENT_ID,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        async with self._session.post(AUTH_URL, data=body, headers=headers) as resp:
            status = resp.status
            try:
                payload = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                payload = {"raw": (await resp.text())[:200]}

        if status == 200:
            self._access_token = payload["access_token"]
            self._access_token_exp = time.time() + int(payload.get("expires_in", 0))
            _LOGGER.info(
                "ecobee refresh OK: expires_in=%s scope=%s",
                payload.get("expires_in"),
                payload.get("scope"),
            )
            new_rt = payload.get("refresh_token")
            if new_rt and new_rt != self._refresh_token:
                self._refresh_token = new_rt
                if self._rt_persist_cb is not None:
                    try:
                        await self._rt_persist_cb(new_rt)
                        _LOGGER.info("ecobee refresh token rotated and persisted")
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception(
                            "ecobee refresh token rotated but persist callback "
                            "failed; next HA restart may need reauth"
                        )
                else:
                    _LOGGER.warning(
                        "ecobee refresh token rotated but no persist callback "
                        "registered; next HA restart will need reauth"
                    )
            return

        if status == 400 and payload.get("error") == "invalid_grant":
            raise InvalidGrantError(
                payload.get("error_description") or "refresh_token invalid_grant"
            )

        raise RuntimeError(f"ecobee refresh failed: status={status} payload={payload}")
