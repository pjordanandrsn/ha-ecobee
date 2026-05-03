"""Tests for the v0.3 ecobee Auth0 universal-login client."""

from __future__ import annotations

import json
import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.ecobee.auth import (
    EcobeeAuth,
    InvalidCredentialsError,
    InvalidGrantError,
    MFACodeExpiredError,
    MFACodeInvalidError,
    MFACodeRequiredError,
    _authorize,
    _exchange_code,
    _handle_custom_prompt,
    _identifier_step,
    _password_step,
    _resume_to_code,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _acm(resp):
    """Wrap a response object as an async context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _redir(location: str, status: int = 302):
    """Build a 302/303-shaped response object."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {"Location": location}
    resp.text = AsyncMock(return_value="")
    return resp


def _html(status: int, body: str = "<html></html>"):
    """Build an HTML response (typically 200 + page body)."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {}
    resp.text = AsyncMock(return_value=body)
    return resp


def _token_resp(status: int, body: dict):
    """Build a /oauth/token response."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=json.dumps(body))
    resp.json = AsyncMock(return_value=body)
    return resp


def _make_auth(refresh_token: str = "rt-OLD") -> EcobeeAuth:
    session = MagicMock()
    return EcobeeAuth(session, refresh_token, email="user@example.com")


def _routed_session(get_responses=None, post_responses=None):
    """Build a MagicMock session with separate FIFO queues for get / post.

    Universal-login interleaves GETs (/, /authorize/resume, /u/* page
    fetches) with POSTs (/u/login/identifier, /u/login/password,
    /u/custom-prompt POSTs, /oauth/token). Splitting the queues keeps
    the per-test wiring readable without having to interleave by URL
    inside a router.
    """
    get_q = list(get_responses or [])
    post_q = list(post_responses or [])

    def _get_ctx(*_args, **_kwargs):
        if not get_q:
            raise AssertionError("session.get exceeded queued responses")
        return _acm(get_q.pop(0))

    def _post_ctx(*_args, **_kwargs):
        if not post_q:
            raise AssertionError("session.post exceeded queued responses")
        return _acm(post_q.pop(0))

    session = MagicMock()
    session.get = MagicMock(side_effect=_get_ctx)
    session.post = MagicMock(side_effect=_post_ctx)
    return session


# ---------------------------------------------------------------------------
# P0: refresh-token rotation persistence (kept from v0.2 — unchanged behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_persist_callback_fires_on_rotation():
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
async def test_refresh_invalid_grant_triggers_reauth():
    """A 400 invalid_grant raises InvalidGrantError so __init__.py can map it."""
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
        return_value=_acm(_token_resp(400, {"error": "transient_503"}))
    )
    with pytest.raises(RuntimeError) as exc_info:
        await auth._refresh()
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

    await auth._refresh()
    assert auth._access_token == "AT-2"


@pytest.mark.asyncio
async def test_refresh_success_updates_refresh_token():
    """Happy-path refresh updates AT, expiry, and rotated RT."""
    auth = _make_auth(refresh_token="rt-OLD")
    auth._session.post = MagicMock(
        return_value=_acm(
            _token_resp(
                200,
                {
                    "access_token": "AT-NEW",
                    "expires_in": 3600,
                    "refresh_token": "rt-ROTATED",
                },
            )
        )
    )
    persist_cb = AsyncMock()
    auth.set_refresh_token_persist_callback(persist_cb)

    await auth._refresh()

    assert auth._access_token == "AT-NEW"
    assert auth._refresh_token == "rt-ROTATED"
    persist_cb.assert_awaited_once_with("rt-ROTATED")


# ---------------------------------------------------------------------------
# P0: ensure_access_token caching + lock (kept from v0.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_access_token_returns_cached_when_fresh():
    auth = _make_auth()
    auth._access_token = "cached-AT"
    import time as _t
    auth._access_token_exp = _t.time() + 3600
    auth._session.post = MagicMock(
        side_effect=AssertionError("should not be called when AT is fresh")
    )
    assert await auth.ensure_access_token() == "cached-AT"


@pytest.mark.asyncio
async def test_ensure_access_token_refreshes_when_expired():
    auth = _make_auth(refresh_token="rt-OLD")
    auth._access_token = "stale-AT"
    auth._access_token_exp = 0

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


def test_from_storage_constructs_without_network():
    """from_storage is a pure constructor — no I/O."""
    session = MagicMock()
    auth = EcobeeAuth.from_storage(session, "rt-stored", email="user@example.com")
    assert auth.refresh_token == "rt-stored"
    assert auth.email == "user@example.com"


# ---------------------------------------------------------------------------
# v0.3: per-step universal-login coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_returns_state():
    """GET /authorize -> 302 to /u/login/identifier?state=..."""
    session = _routed_session(
        get_responses=[
            _redir("https://auth.ecobee.com/u/login/identifier?state=AUTH-STATE"),
        ]
    )
    state = await _authorize(session, state="initial", challenge="CHAL")
    assert state == "AUTH-STATE"


@pytest.mark.asyncio
async def test_authorize_non_redirect_raises():
    session = _routed_session(get_responses=[_html(200, "<html>error</html>")])
    with pytest.raises(RuntimeError) as exc_info:
        await _authorize(session, state="initial", challenge="CHAL")
    assert "step=authorize" in str(exc_info.value)


@pytest.mark.asyncio
async def test_identifier_step_advances_to_password():
    """POST /u/login/identifier -> 302 to /u/login/password?state=..."""
    session = _routed_session(
        post_responses=[
            _redir("https://auth.ecobee.com/u/login/password?state=PW-STATE"),
        ]
    )
    state = await _identifier_step(session, "AUTH-STATE", "user@example.com")
    assert state == "PW-STATE"


@pytest.mark.asyncio
async def test_identifier_step_unknown_email_raises_invalid_credentials():
    """Auth0 sends us back to /u/login/identifier when the email is bad."""
    session = _routed_session(
        post_responses=[
            _redir("https://auth.ecobee.com/u/login/identifier?state=BACK"),
        ]
    )
    with pytest.raises(InvalidCredentialsError):
        await _identifier_step(session, "AUTH-STATE", "noone@nowhere.com")


@pytest.mark.asyncio
async def test_password_step_advances_to_resume():
    """POST /u/login/password -> 302 to /authorize/resume?state=..."""
    session = _routed_session(
        post_responses=[
            _redir("https://auth.ecobee.com/authorize/resume?state=RESUME-STATE"),
        ]
    )
    state = await _password_step(
        session, "PW-STATE", "user@example.com", "secret"
    )
    assert state == "RESUME-STATE"


@pytest.mark.asyncio
async def test_password_rejected_raises_invalid_credentials():
    """Auth0 returns a 400 with data-error-code='invalid_password'."""
    body = (
        '<html><span class="ulp-input-error-message" '
        'data-error-code="invalid_password">Wrong password</span></html>'
    )
    bad_resp = MagicMock()
    bad_resp.status = 400
    bad_resp.headers = {}
    bad_resp.text = AsyncMock(return_value=body)
    session = _routed_session(post_responses=[bad_resp])
    with pytest.raises(InvalidCredentialsError):
        await _password_step(session, "PW-STATE", "user@example.com", "wrong")


@pytest.mark.asyncio
async def test_resume_returns_code_on_app_callback():
    """GET /authorize/resume -> 302 to web callback ?code=&state=."""
    session = _routed_session(
        get_responses=[
            _redir(
                "https://www.ecobee.com/home/authCallback?code=AUTHCODE-1&state=ST"
            ),
        ]
    )
    code = await _resume_to_code(session, "RESUME-STATE")
    assert code == "AUTHCODE-1"


@pytest.mark.asyncio
async def test_resume_handles_custom_prompt_chain():
    """One /u/custom-prompt page in the middle is handled and chain completes."""
    # GET responses: resume (-> custom-prompt), GET on custom-prompt page,
    # GET resume again (-> callback).
    custom_prompt_loc = (
        "https://auth.ecobee.com/u/custom-prompt/abc?state=CUSTOM-STATE"
    )
    callback_loc = (
        "https://www.ecobee.com/home/authCallback?code=AUTHCODE-2&state=ST"
    )
    # POST responses: prompt-POST -> redirect to /authorize/resume.
    next_resume_loc = "https://auth.ecobee.com/authorize/resume?state=RESUME-2"
    session = _routed_session(
        get_responses=[
            _redir(custom_prompt_loc),
            _html(200, "<script id=\"__NEXT_DATA__\">{\"props\":{}}</script>"),
            _redir(callback_loc),
        ],
        post_responses=[
            _redir(next_resume_loc),
        ],
    )
    code = await _resume_to_code(session, "RESUME-1")
    assert code == "AUTHCODE-2"


@pytest.mark.asyncio
async def test_resume_with_mfa_prompt_chain_raises_mfa_code_required():
    """Auth0 /u/mfa-otp-challenge raises MFACodeRequiredError so config_flow can prompt."""
    # v0.3.1: code-entry MFA prompts (/u/mfa-otp-challenge,
    # /u/mfa-sms-challenge, /u/mfa-recovery-code-challenge) are
    # detected in _handle_custom_prompt BEFORE the POST and surfaced
    # as MFACodeRequiredError. The config_flow catches it, prompts
    # the user for the code, and resumes via continue_with_mfa_code.
    mfa_loc = (
        "https://auth.ecobee.com/u/mfa-otp-challenge?state=MFA-STATE"
    )
    session = _routed_session(
        get_responses=[
            _redir(mfa_loc),
            _html(200, "<html><body>mfa prompt</body></html>"),
        ],
        # No POSTs queued: handler must raise BEFORE attempting POST.
        post_responses=[],
    )
    with pytest.raises(MFACodeRequiredError) as exc_info:
        await _resume_to_code(session, "RESUME-1")
    assert exc_info.value.challenge_type == "otp"
    assert "mfa-otp-challenge" in exc_info.value.prompt_url
    assert exc_info.value.state == "MFA-STATE"


@pytest.mark.asyncio
async def test_resume_loop_bound_raises_after_too_many_prompts():
    """6 chained /u/* prompts in a row exceeds the 5-attempt loop bound."""
    prompt_loc = (
        "https://auth.ecobee.com/u/custom-prompt/x?state=PROMPT-{}"
    )
    next_resume_loc = (
        "https://auth.ecobee.com/authorize/resume?state=RESUME-{}"
    )
    # 5 iterations: each iteration consumes resume-GET + prompt-page-GET
    # + prompt-POST. So queue 5x of each.
    session = _routed_session(
        get_responses=[
            _redir(prompt_loc.format(i)) for i in range(5)
        ] + [_html(200, "<html></html>") for _ in range(5)],
        post_responses=[
            _redir(next_resume_loc.format(i)) for i in range(5)
        ],
    )
    # Re-interleave: each loop iteration is GET resume -> GET page ->
    # POST page. Re-queue accordingly.
    session = _routed_session(
        get_responses=[
            _redir(prompt_loc.format(0)),
            _html(200, "<html></html>"),
            _redir(prompt_loc.format(1)),
            _html(200, "<html></html>"),
            _redir(prompt_loc.format(2)),
            _html(200, "<html></html>"),
            _redir(prompt_loc.format(3)),
            _html(200, "<html></html>"),
            _redir(prompt_loc.format(4)),
            _html(200, "<html></html>"),
        ],
        post_responses=[
            _redir(next_resume_loc.format(i)) for i in range(5)
        ],
    )
    with pytest.raises(RuntimeError) as exc_info:
        await _resume_to_code(session, "INITIAL-STATE")
    assert "5 consecutive Auth0 prompts" in str(exc_info.value)


@pytest.mark.asyncio
async def test_handle_custom_prompt_returns_resume_state():
    """Generic /u/* prompt POST -> redirect to /authorize/resume?state=NEW."""
    session = _routed_session(
        get_responses=[_html(200, "<html></html>")],
        post_responses=[
            _redir(
                "https://auth.ecobee.com/authorize/resume?state=NEW-RESUME"
            ),
        ],
    )
    new_state = await _handle_custom_prompt(
        session,
        "https://auth.ecobee.com/u/custom-prompt/abc?state=PROMPT-STATE",
    )
    assert new_state == "NEW-RESUME"


@pytest.mark.asyncio
async def test_handle_custom_prompt_200_raises_actionable_error():
    """If POST returns 200 (not 302), prompt requires user interaction."""
    bad = MagicMock()
    bad.status = 200
    bad.headers = {}
    bad.text = AsyncMock(return_value="<html>still here</html>")
    session = _routed_session(
        get_responses=[_html(200, "<html></html>")],
        post_responses=[bad],
    )
    with pytest.raises(RuntimeError) as exc_info:
        await _handle_custom_prompt(
            session,
            "https://auth.ecobee.com/u/custom-prompt/x?state=ST",
        )
    msg = str(exc_info.value)
    assert "step=custom-prompt" in msg
    assert "ecobee.com" in msg


# ---------------------------------------------------------------------------
# v0.3: token exchange
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_code_success():
    """POST /oauth/token grant_type=authorization_code -> tokens dict."""
    session = _routed_session(
        post_responses=[
            _token_resp(
                200,
                {
                    "access_token": "AT-1",
                    "refresh_token": "RT-1",
                    "expires_in": 3600,
                    "scope": "openid offline_access",
                    "token_type": "Bearer",
                },
            )
        ]
    )
    tokens = await _exchange_code(session, code="AUTHCODE", verifier="VERIF")
    assert tokens["access_token"] == "AT-1"
    assert tokens["refresh_token"] == "RT-1"


@pytest.mark.asyncio
async def test_exchange_code_invalid_grant_raises():
    """Auth0 rejects code -> RuntimeError; not InvalidGrant (init-time only)."""
    session = _routed_session(
        post_responses=[
            _token_resp(400, {"error": "invalid_grant", "error_description": "bad code"})
        ]
    )
    with pytest.raises(RuntimeError) as exc_info:
        await _exchange_code(session, code="BAD", verifier="V")
    assert "code exchange failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# v0.3: end-to-end EcobeeAuth.login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_login_e2e_no_mfa():
    """End-to-end login on a non-MFA account: 5 steps, returns ready EcobeeAuth.

    v0.3.1: auth.login() reuses the passed-in session so the cookie
    jar carries through to a possible continue_with_mfa_code follow-
    up. We pass the routed-session mock directly.
    """
    chain = _routed_session(
        get_responses=[
            # /authorize -> /u/login/identifier
            _redir("https://auth.ecobee.com/u/login/identifier?state=A"),
            # /authorize/resume -> callback
            _redir(
                "https://www.ecobee.com/home/authCallback?code=CODE&state=ST"
            ),
        ],
        post_responses=[
            # /u/login/identifier -> /u/login/password
            _redir("https://auth.ecobee.com/u/login/password?state=B"),
            # /u/login/password -> /authorize/resume
            _redir("https://auth.ecobee.com/authorize/resume?state=C"),
            # /oauth/token (code exchange)
            _token_resp(
                200,
                {
                    "access_token": "AT-final",
                    "refresh_token": "RT-final",
                    "expires_in": 3600,
                },
            ),
        ],
    )

    auth = await EcobeeAuth.login(chain, "user@example.com", "secret")

    assert auth.refresh_token == "RT-final"
    assert auth._access_token == "AT-final"
    assert auth.email == "user@example.com"


@pytest.mark.asyncio
async def test_full_login_e2e_with_custom_prompt():
    """End-to-end login that hits one /u/custom-prompt before the callback."""
    chain = _routed_session(
        get_responses=[
            # /authorize -> /u/login/identifier
            _redir("https://auth.ecobee.com/u/login/identifier?state=A"),
            # /authorize/resume -> /u/custom-prompt/<id>
            _redir(
                "https://auth.ecobee.com/u/custom-prompt/tnc?state=PROMPT"
            ),
            # GET prompt page (for diagnostics — handler then POSTs)
            _html(200, "<html><body>T&C</body></html>"),
            # second /authorize/resume -> callback
            _redir(
                "https://www.ecobee.com/home/authCallback?code=PC&state=ST"
            ),
        ],
        post_responses=[
            # /u/login/identifier -> /u/login/password
            _redir("https://auth.ecobee.com/u/login/password?state=B"),
            # /u/login/password -> /authorize/resume
            _redir("https://auth.ecobee.com/authorize/resume?state=C"),
            # POST /u/custom-prompt -> /authorize/resume
            _redir("https://auth.ecobee.com/authorize/resume?state=D"),
            # /oauth/token (code exchange)
            _token_resp(
                200,
                {
                    "access_token": "AT-prompt",
                    "refresh_token": "RT-prompt",
                    "expires_in": 3600,
                },
            ),
        ],
    )

    auth = await EcobeeAuth.login(chain, "user@example.com", "secret")

    assert auth.refresh_token == "RT-prompt"
    assert auth._access_token == "AT-prompt"


@pytest.mark.asyncio
async def test_full_login_e2e_with_mfa_prompt_chain_raises_mfa_required():
    """E2E login on a 2FA account: Auth0 redirects to /u/mfa-otp-challenge.

    v0.3.1: login() pumps through identifier + password and then runs
    into the MFA prompt during _resume_to_code, which raises
    MFACodeRequiredError. login() catches it, enriches with verifier
    + email, and re-raises so the config_flow can prompt the user.
    """
    chain = _routed_session(
        get_responses=[
            # /authorize -> /u/login/identifier
            _redir("https://auth.ecobee.com/u/login/identifier?state=A"),
            # /authorize/resume -> /u/mfa-otp-challenge
            _redir("https://auth.ecobee.com/u/mfa-otp-challenge?state=MFA"),
            # GET MFA prompt page
            _html(200, "<html>mfa</html>"),
        ],
        post_responses=[
            # /u/login/identifier -> /u/login/password
            _redir("https://auth.ecobee.com/u/login/password?state=B"),
            # /u/login/password -> /authorize/resume
            _redir("https://auth.ecobee.com/authorize/resume?state=C"),
        ],
    )

    with pytest.raises(MFACodeRequiredError) as exc_info:
        await EcobeeAuth.login(chain, "user@example.com", "secret")

    assert exc_info.value.challenge_type == "otp"
    assert exc_info.value.email == "user@example.com"
    # verifier is enriched by login() so continue_with_mfa_code can
    # exchange the eventual auth code for tokens.
    assert exc_info.value.verifier  # non-empty string


@pytest.mark.asyncio
async def test_continue_with_mfa_code_completes_login():
    """After MFACodeRequiredError, config_flow calls continue_with_mfa_code."""
    chain = _routed_session(
        get_responses=[
            # /authorize/resume -> callback
            _redir(
                "https://www.ecobee.com/home/authCallback?code=MFA-C&state=ST"
            ),
        ],
        post_responses=[
            # POST mfa-otp-challenge code -> /authorize/resume
            _redir("https://auth.ecobee.com/authorize/resume?state=POSTMFA"),
            # /oauth/token (code exchange)
            _token_resp(
                200,
                {
                    "access_token": "AT-mfa",
                    "refresh_token": "RT-mfa",
                    "expires_in": 3600,
                },
            ),
        ],
    )

    auth = await EcobeeAuth.continue_with_mfa_code(
        chain,
        prompt_url="https://auth.ecobee.com/u/mfa-otp-challenge?state=MFA",
        state="MFA",
        code="123456",
        verifier="VERIFIER-X",
        email="user@example.com",
    )

    assert auth.refresh_token == "RT-mfa"
    assert auth.email == "user@example.com"


@pytest.mark.asyncio
async def test_continue_with_mfa_code_wrong_code_raises_invalid():
    """Auth0 rejects wrong code by re-rendering the prompt with status 200."""
    bad = MagicMock()
    bad.status = 200
    bad.headers = {}
    bad.text = AsyncMock(
        return_value="<html><body>Wrong code, try again.</body></html>"
    )
    chain = _routed_session(
        post_responses=[bad],
    )

    with pytest.raises(MFACodeInvalidError):
        await EcobeeAuth.continue_with_mfa_code(
            chain,
            prompt_url="https://auth.ecobee.com/u/mfa-otp-challenge?state=MFA",
            state="MFA",
            code="000000",
            verifier="V",
            email="user@example.com",
        )


@pytest.mark.asyncio
async def test_continue_with_mfa_code_expired_state_raises_expired():
    """When the prompt's state has aged out, Auth0's re-render mentions 'expired'."""
    expired = MagicMock()
    expired.status = 200
    expired.headers = {}
    expired.text = AsyncMock(
        return_value="<html><body>Session has expired. Please sign in again.</body></html>"
    )
    chain = _routed_session(
        post_responses=[expired],
    )

    with pytest.raises(MFACodeExpiredError):
        await EcobeeAuth.continue_with_mfa_code(
            chain,
            prompt_url="https://auth.ecobee.com/u/mfa-otp-challenge?state=OLD",
            state="OLD",
            code="123456",
            verifier="V",
            email="user@example.com",
        )


@pytest.mark.asyncio
async def test_login_no_refresh_token_in_response_raises_runtime():
    """A 200 token response without refresh_token = wrong scope; surface clearly."""
    chain = _routed_session(
        get_responses=[
            _redir("https://auth.ecobee.com/u/login/identifier?state=A"),
            _redir(
                "https://www.ecobee.com/home/authCallback?code=CODE&state=ST"
            ),
        ],
        post_responses=[
            _redir("https://auth.ecobee.com/u/login/password?state=B"),
            _redir("https://auth.ecobee.com/authorize/resume?state=C"),
            _token_resp(200, {"access_token": "AT-only", "expires_in": 3600}),
        ],
    )

    with pytest.raises(RuntimeError, match="no refresh_token"):
        await EcobeeAuth.login(chain, "u@e.com", "x")


@pytest.mark.asyncio
async def test_login_invalid_credentials_raises_at_password_step():
    """Bad password = Auth0 returns 400 with data-error-code at /u/login/password."""
    bad_pw = MagicMock()
    bad_pw.status = 400
    bad_pw.headers = {}
    bad_pw.text = AsyncMock(
        return_value=(
            '<html><span data-error-code="invalid_password">Wrong</span></html>'
        )
    )
    chain = _routed_session(
        get_responses=[
            _redir("https://auth.ecobee.com/u/login/identifier?state=A"),
        ],
        post_responses=[
            _redir("https://auth.ecobee.com/u/login/password?state=B"),
            bad_pw,
        ],
    )

    with pytest.raises(InvalidCredentialsError):
        await EcobeeAuth.login(chain, "u@e.com", "wrong")
