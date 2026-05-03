"""Tests for the ecobee Auth0 ROPG client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.ecobee.auth import (
    EcobeeAuth,
    InvalidCredentialsError,
    InvalidGrantError,
    MFAExpiredError,
    MFAInvalidCodeError,
    MFANotSupportedError,
    MFARateLimitedError,
    MFARequiredError,
)


def _acm(resp):
    """Wrap a response object as an async context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _token_resp(status: int, body: dict):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=json.dumps(body))
    resp.json = AsyncMock(return_value=body)
    return resp


def _make_auth(refresh_token: str = "rt-OLD") -> EcobeeAuth:
    session = MagicMock()
    return EcobeeAuth(session, refresh_token, email="user@example.com")


def _seq_session(*responses):
    """Build a MagicMock session whose .post / .get return responses in order.

    The MFA dance issues multiple HTTP calls per logical operation
    (e.g. password POST -> /mfa/authenticators GET when login() detects
    MFA, or /mfa/challenge POST -> /oauth/token POST). To keep the
    per-test wiring readable we feed responses in chronological order
    and route them based on URL prefix.
    """
    # Split responses into post / get queues by URL prefix later. We
    # keep one global queue and a router that pulls from it FIFO; this
    # mirrors how ``await session.post(...)`` and ``await session.get(...)``
    # would interleave at runtime.
    queue = list(responses)

    def _ctx(*_args, **_kwargs):
        if not queue:
            raise AssertionError("session HTTP call exceeded queued responses")
        return _acm(queue.pop(0))

    session = MagicMock()
    session.post = MagicMock(side_effect=_ctx)
    session.get = MagicMock(side_effect=_ctx)
    return session


# ---------------------------------------------------------------------------
# P0: refresh-token rotation persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_persist_callback_fires_on_rotation():
    """When Auth0 rotates the RT, the persist callback runs with the new RT."""
    auth = _make_auth(refresh_token="rt-OLD")
    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {
                    "access_token": "AT-1",
                    "expires_in": 3600,
                    "refresh_token": "rt-NEW",
                    "scope": "openid offline_access",
                    "token_type": "Bearer",
                },
            )
        )
    )

    persist_cb = AsyncMock()
    auth.set_refresh_token_persist_callback(persist_cb)

    await auth._refresh()

    assert auth._access_token == "AT-1"
    assert auth._refresh_token == "rt-NEW"
    persist_cb.assert_awaited_once_with("rt-NEW")


@pytest.mark.asyncio
async def test_refresh_persist_callback_not_called_when_rt_unchanged():
    """Server returns the same RT (no rotation) -> persist cb not called."""
    auth = _make_auth(refresh_token="rt-OLD")
    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {
                    "access_token": "AT-1",
                    "expires_in": 3600,
                    "refresh_token": "rt-OLD",
                },
            )
        )
    )

    persist_cb = AsyncMock()
    auth.set_refresh_token_persist_callback(persist_cb)

    await auth._refresh()

    assert auth._refresh_token == "rt-OLD"
    persist_cb.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_persist_callback_not_called_when_omitted():
    """Server omits refresh_token entirely -> persist cb not called."""
    auth = _make_auth(refresh_token="rt-OLD")
    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {"access_token": "AT-1", "expires_in": 3600},
            )
        )
    )

    persist_cb = AsyncMock()
    auth.set_refresh_token_persist_callback(persist_cb)

    await auth._refresh()

    assert auth._refresh_token == "rt-OLD"
    persist_cb.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_invalid_grant_raises():
    """A 400 invalid_grant from /oauth/token must raise InvalidGrantError."""
    auth = _make_auth(refresh_token="rt-REVOKED")
    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                400,
                {"error": "invalid_grant", "error_description": "rt revoked"},
            )
        )
    )

    with pytest.raises(InvalidGrantError):
        await auth._refresh()


@pytest.mark.asyncio
async def test_refresh_unknown_4xx_raises_runtime_error():
    """Unknown error shape comes through as RuntimeError, not InvalidGrant."""
    auth = _make_auth(refresh_token="rt-X")
    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(400, {"error": "transient_503"}),
        )
    )
    with pytest.raises(RuntimeError) as exc_info:
        await auth._refresh()
    # Must not be an InvalidGrantError or it'd trigger an unwanted reauth.
    assert not isinstance(exc_info.value, InvalidGrantError)


@pytest.mark.asyncio
async def test_refresh_persist_callback_failure_swallowed():
    """If the persist cb raises, _refresh must not propagate the exception."""
    auth = _make_auth(refresh_token="rt-OLD")
    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {"access_token": "AT-2", "expires_in": 60, "refresh_token": "rt-NEW"},
            )
        )
    )

    async def boom(_):
        raise RuntimeError("disk full")

    auth.set_refresh_token_persist_callback(boom)

    # Must NOT raise — we logged it but the new AT is still valid for
    # the rest of this process's lifetime.
    await auth._refresh()
    assert auth._access_token == "AT-2"


# ---------------------------------------------------------------------------
# P0: ensure_access_token caching + lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_access_token_returns_cached_when_fresh():
    """If the cached AT hasn't expired, no /oauth/token POST happens."""
    auth = _make_auth()
    auth._access_token = "cached-AT"
    # Long expiry into the future.
    import time as _t
    auth._access_token_exp = _t.time() + 3600
    auth._session.post = MagicMock(
        side_effect=AssertionError("should not be called when AT is fresh")
    )
    assert await auth.ensure_access_token() == "cached-AT"


@pytest.mark.asyncio
async def test_ensure_access_token_refreshes_when_expired():
    """A stale AT triggers a refresh; subsequent call returns the fresh one."""
    auth = _make_auth(refresh_token="rt-OLD")
    auth._access_token = "stale-AT"
    auth._access_token_exp = 0  # already expired

    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {"access_token": "fresh-AT", "expires_in": 3600},
            )
        )
    )
    new = await auth.ensure_access_token()
    assert new == "fresh-AT"


# ---------------------------------------------------------------------------
# P0: ROPG login (success path + MFA detection + invalid creds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_happy_path():
    session = MagicMock()
    session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {
                    "access_token": "AT-login",
                    "refresh_token": "RT-login",
                    "expires_in": 3600,
                    "scope": "openid offline_access",
                    "token_type": "Bearer",
                },
            )
        )
    )
    auth = await EcobeeAuth.login(session, "user@example.com", "secret")
    assert auth.refresh_token == "RT-login"
    assert auth._access_token == "AT-login"
    assert auth.email == "user@example.com"


@pytest.mark.asyncio
async def test_login_invalid_grant_maps_to_invalid_credentials():
    session = MagicMock()
    session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                400,
                {"error": "invalid_grant", "error_description": "Wrong email or password."},
            )
        )
    )
    with pytest.raises(InvalidCredentialsError):
        await EcobeeAuth.login(session, "user@example.com", "wrong")


@pytest.mark.asyncio
async def test_login_mfa_without_token_raises_unsupported():
    """If Auth0 says MFA but doesn't supply mfa_token, we can't continue."""
    session = MagicMock()
    session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                400,
                {
                    "error": "invalid_request",
                    "error_description": "Multifactor authentication required.",
                },
            )
        )
    )
    with pytest.raises(MFANotSupportedError):
        await EcobeeAuth.login(session, "user@example.com", "anything")


@pytest.mark.asyncio
async def test_login_mfa_alternative_phrasing_still_caught():
    """Substring match on 'mfa' catches alt phrasings too (no mfa_token -> Unsupported)."""
    session = MagicMock()
    session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                400,
                {
                    "error": "mfa_required",
                    "error_description": "Please provide MFA token.",
                },
            )
        )
    )
    with pytest.raises(MFANotSupportedError):
        await EcobeeAuth.login(session, "u@e.com", "x")


@pytest.mark.asyncio
async def test_login_no_refresh_token_in_response_raises_runtime():
    """A 200 without a refresh_token means we asked for the wrong scope."""
    session = MagicMock()
    session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {"access_token": "AT-only", "expires_in": 3600},
            )
        )
    )
    with pytest.raises(RuntimeError):
        await EcobeeAuth.login(session, "u@e.com", "x")


@pytest.mark.asyncio
async def test_login_unknown_5xx_raises_runtime_error():
    session = MagicMock()
    session.post = MagicMock(
        return_value=_acm(_token_resp(503, {"error": "service_unavailable"}))
    )
    with pytest.raises(RuntimeError):
        await EcobeeAuth.login(session, "u@e.com", "x")


def test_from_storage_constructs_without_network():
    """from_storage is a pure constructor — no I/O."""
    session = MagicMock()
    auth = EcobeeAuth.from_storage(session, "rt-stored", email="user@example.com")
    assert auth.refresh_token == "rt-stored"
    assert auth.email == "user@example.com"


# ---------------------------------------------------------------------------
# v0.2: MFA flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_mfa_required_raises_with_token_and_factors():
    """Password POST returns 403 mfa_required + mfa_token; login enriches with factors."""
    factors = [
        {
            "id": "totp|abc",
            "authenticator_type": "otp",
            "name": "Authenticator app",
            "active": True,
        },
        {
            "id": "sms|xyz",
            "authenticator_type": "oob",
            "oob_channel": "sms",
            "name": "XXX-XXX-1234",
            "active": True,
        },
    ]
    # Sequence: password POST (403 mfa_required) -> /mfa/authenticators GET.
    session = _seq_session(
        _token_resp(
            403,
            {
                "error": "mfa_required",
                "error_description": "Multifactor authentication required",
                "mfa_token": "MFA-JWT-1",
            },
        ),
        _token_resp(200, factors),
    )
    with pytest.raises(MFARequiredError) as exc_info:
        await EcobeeAuth.login(session, "u@e.com", "secret")
    assert exc_info.value.mfa_token == "MFA-JWT-1"
    assert len(exc_info.value.authenticators) == 2
    types = {a["authenticator_type"] for a in exc_info.value.authenticators}
    assert types == {"otp", "oob"}


@pytest.mark.asyncio
async def test_list_mfa_authenticators_returns_active_only():
    """Inactive authenticators are filtered out so the form doesn't offer them."""
    session = _seq_session(
        _token_resp(
            200,
            [
                {"id": "a", "authenticator_type": "otp", "active": True},
                {"id": "b", "authenticator_type": "oob", "active": False},
                {"id": "c", "authenticator_type": "otp"},  # missing -> default True
            ],
        )
    )
    out = await EcobeeAuth.list_mfa_authenticators(session, "MFA-JWT")
    ids = {a["id"] for a in out}
    assert ids == {"a", "c"}


@pytest.mark.asyncio
async def test_challenge_mfa_otp_is_noop():
    """OTP factors don't need a challenge call — challenge_mfa returns None."""
    session = MagicMock()
    session.post = MagicMock(side_effect=AssertionError("OTP must not POST /mfa/challenge"))
    out = await EcobeeAuth.challenge_mfa(
        session,
        mfa_token="MFA-JWT",
        authenticator_id="totp|abc",
        authenticator_type="otp",
    )
    assert out is None


@pytest.mark.asyncio
async def test_challenge_mfa_oob_returns_oob_code():
    """OOB factors POST /mfa/challenge and the oob_code threads to submit."""
    session = _seq_session(
        _token_resp(
            200,
            {
                "challenge_type": "oob",
                "oob_code": "OOB-CODE-1",
                "binding_method": "prompt",
            },
        )
    )
    out = await EcobeeAuth.challenge_mfa(
        session,
        mfa_token="MFA-JWT",
        authenticator_id="sms|xyz",
        authenticator_type="oob",
    )
    assert out is not None
    assert out["oob_code"] == "OOB-CODE-1"


@pytest.mark.asyncio
async def test_submit_mfa_otp_success_returns_auth_handle():
    """OTP grant returns the same shape as password grant -> auth handle."""
    session = _seq_session(
        _token_resp(
            200,
            {
                "access_token": "AT-MFA",
                "refresh_token": "RT-MFA",
                "expires_in": 3600,
                "scope": "openid offline_access",
                "token_type": "Bearer",
            },
        )
    )
    auth = await EcobeeAuth.submit_mfa(
        session,
        mfa_token="MFA-JWT",
        authenticator_type="otp",
        code="123456",
        email="u@e.com",
    )
    assert auth.refresh_token == "RT-MFA"
    assert auth._access_token == "AT-MFA"
    assert auth.email == "u@e.com"


@pytest.mark.asyncio
async def test_submit_mfa_oob_success_returns_auth_handle():
    """OOB grant supplies oob_code + binding_code; same return shape."""
    session = _seq_session(
        _token_resp(
            200,
            {
                "access_token": "AT-OOB",
                "refresh_token": "RT-OOB",
                "expires_in": 3600,
            },
        )
    )
    auth = await EcobeeAuth.submit_mfa(
        session,
        mfa_token="MFA-JWT",
        authenticator_type="oob",
        code="654321",
        oob_code="OOB-CODE-1",
        email="u@e.com",
    )
    assert auth.refresh_token == "RT-OOB"
    assert auth._access_token == "AT-OOB"


@pytest.mark.asyncio
async def test_submit_mfa_wrong_code_raises_invalid_code():
    """Auth0's 'Invalid otp_code' surfaces as MFAInvalidCodeError so flow can retry."""
    session = _seq_session(
        _token_resp(
            403,
            {
                "error": "invalid_grant",
                "error_description": "Invalid otp_code",
            },
        )
    )
    with pytest.raises(MFAInvalidCodeError):
        await EcobeeAuth.submit_mfa(
            session,
            mfa_token="MFA-JWT",
            authenticator_type="otp",
            code="000000",
        )


@pytest.mark.asyncio
async def test_submit_mfa_expired_token_raises_expired():
    """An aged-out mfa_token gets a dedicated typed error so flow can restart."""
    session = _seq_session(
        _token_resp(
            403,
            {
                "error": "invalid_grant",
                "error_description": "mfa_token expired",
            },
        )
    )
    with pytest.raises(MFAExpiredError):
        await EcobeeAuth.submit_mfa(
            session,
            mfa_token="MFA-JWT",
            authenticator_type="otp",
            code="000000",
        )


@pytest.mark.asyncio
async def test_submit_mfa_rate_limited_raises_typed_error():
    """429 too_many_attempts -> dedicated rate-limit error."""
    session = _seq_session(
        _token_resp(
            429,
            {
                "error": "too_many_attempts",
                "error_description": "Too many MFA attempts",
            },
        )
    )
    with pytest.raises(MFARateLimitedError):
        await EcobeeAuth.submit_mfa(
            session,
            mfa_token="MFA-JWT",
            authenticator_type="otp",
            code="111111",
        )


@pytest.mark.asyncio
async def test_submit_mfa_push_factor_not_supported():
    """Push factors are recognised but rejected with a clear error."""
    session = MagicMock()
    session.post = MagicMock(side_effect=AssertionError("must reject before any HTTP call"))
    with pytest.raises(MFANotSupportedError):
        await EcobeeAuth.submit_mfa(
            session,
            mfa_token="MFA-JWT",
            authenticator_type="push-notification",
            code="anything",
        )


@pytest.mark.asyncio
async def test_refresh_after_mfa_does_not_require_mfa_again():
    """Once we've got the post-MFA refresh_token, subsequent /oauth/token refresh
    has no MFA prompt — verify the flow by exchanging RT-MFA for a fresh AT
    using the regular refresh_token grant.
    """
    # Seed a fresh auth handle as if it had just come out of submit_mfa.
    session = _seq_session(
        _token_resp(
            200,
            {
                "access_token": "AT-MFA",
                "refresh_token": "RT-MFA",
                "expires_in": 3600,
            },
        ),
        # Then a refresh_token POST returns a new AT (and possibly the
        # same RT — Auth0 rotation depends on tenant config).
        _token_resp(
            200,
            {
                "access_token": "AT-REFRESHED",
                "refresh_token": "RT-MFA",
                "expires_in": 3600,
            },
        ),
    )
    auth = await EcobeeAuth.submit_mfa(
        session,
        mfa_token="MFA-JWT",
        authenticator_type="otp",
        code="123456",
        email="u@e.com",
    )
    # Force the refresh path; no MFA error should be raised.
    auth._access_token = None
    auth._access_token_exp = 0
    new_at = await auth.ensure_access_token()
    assert new_at == "AT-REFRESHED"
    assert auth.refresh_token == "RT-MFA"
