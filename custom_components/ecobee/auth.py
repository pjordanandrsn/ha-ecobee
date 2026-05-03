"""Auth0 Authorization Code + PKCE + universal-login against ecobee's tenant.

v0.3 replaces v0.2's Resource Owner Password Grant. Why the change:

* ecobee's web client config rejects ROPG MFA grants outright. Verified
  live 2026-05-03: Auth0 returns ``unauthorized_client`` for both
  ``mfa-otp`` and ``mfa-oob`` grant types regardless of the configured
  factor, so any 2FA-enabled account is fundamentally unreachable via
  ROPG. The same client config DOES allow the standard authorization-
  code flow with PKCE, and Auth0's universal-login pages handle MFA
  natively (a 2FA-enabled login simply chains through ``/u/mfa-*``
  pages between ``/u/login/password`` and ``/authorize/resume``).
* By using universal-login, MFA becomes Auth0's problem — we don't
  enumerate factors, fire challenges, or submit codes; the user
  interacts with Auth0's hosted pages via redirects we follow. The
  ``_handle_custom_prompt`` helper handles all the in-between
  ``/u/mfa-*`` and ``/u/custom-prompt/<id>`` pages generically.

Notes on this implementation:

* No DPoP. ecobee's web client doesn't enforce DPoP — Bearer tokens
  are accepted on every API call.
* The redirect URI is the web callback (``https://www.ecobee.com/home
  /authCallback``) rather than a deep-link URI scheme. We never
  navigate to it; we parse ``?code=...&state=...`` from the
  ``Location`` header on the eventual 302.
* Persisted credential is ``{email, refresh_token}``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from typing import Awaitable, Callable, Optional

import aiohttp

from .const import (
    AUDIENCE,
    AUTHORIZE_URL,
    IDENTIFIER_URL,
    PASSWORD_URL,
    REDIRECT_URI,
    RESUME_URL,
    SCOPES,
    TOKEN_URL,
    WEB_CLIENT_ID,
)

_LOGGER = logging.getLogger(__name__)

AUTH0_DOMAIN = "auth.ecobee.com"

# Standard browser UA — ecobee's web flow expects a browser-shaped
# request. Auth0's universal-login pages care about Accept + UA enough
# that an obvious bot UA can land you on a different render path.
USER_AGENT_WEB = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15"
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 S256.

    PKCE is mandatory for Auth0 authorization-code flow with public
    clients. The verifier is opaque random bytes; the challenge is
    SHA-256 of the verifier, base64url-encoded.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class InvalidGrantError(Exception):
    """Raised when the refresh token has been invalidated server-side.

    The caller should map this to ``ConfigEntryAuthFailed`` so HA
    prompts the user to re-authenticate.
    """


class InvalidCredentialsError(Exception):
    """Raised when the user-supplied email/password is rejected at login."""


class MFACodeRequiredError(Exception):
    """Raised by ``EcobeeAuth.login`` when Auth0 surfaces a code-entry MFA prompt.

    Carries everything the config_flow needs to bounce out, prompt the
    user for the code, and resume via ``EcobeeAuth.continue_with_mfa_code``.
    The aiohttp session's cookie jar already holds the Auth0 session
    cookies — those persist across calls because we use HA's shared
    client session.

    Attributes:
        prompt_url: Absolute URL of the Auth0 prompt page (we POST the
            code back to this same URL).
        state: ``state`` query param Auth0 issued for this prompt; must
            be echoed back in the form body.
        challenge_type: Which factor type Auth0 chose ("otp" / "sms" /
            "recovery"). Used by the config_flow to pick the right form
            label.
        verifier: The PKCE ``code_verifier`` from the original
            ``/authorize`` call. Required by the final
            ``/oauth/token`` exchange after the resume completes.
        email: Email the user logged in with — needed to set the
            entry's ``unique_id`` after resume.
    """

    def __init__(
        self,
        *,
        prompt_url: str,
        state: str,
        challenge_type: str,
        verifier: str = "",
        email: str = "",
    ) -> None:
        super().__init__(
            f"MFA code required: type={challenge_type} url={prompt_url[:80]}"
        )
        self.prompt_url = prompt_url
        self.state = state
        self.challenge_type = challenge_type
        self.verifier = verifier
        self.email = email


class MFACodeInvalidError(Exception):
    """Raised when the user's submitted MFA code was wrong.

    Config_flow re-renders the same code form with an error so the
    user can retype without restarting from email/password.
    """


class MFACodeExpiredError(Exception):
    """Raised when the MFA prompt's state has expired (typically ~10 min).

    Config_flow bounces back to the email/password step.
    """


# ---------------------------------------------------------------------------
# Login flow (one-shot, runs from the config flow when user submits creds)
# ---------------------------------------------------------------------------


async def _authorize(
    session: aiohttp.ClientSession, state: str, challenge: str
) -> str:
    """GET /authorize and follow the first 302 to /u/login/identifier.

    Returns the ``state`` parameter from the redirect — Auth0 binds
    each /u/login/* round-trip to a per-session state token, which we
    thread through every subsequent request.
    """
    params = {
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "audience": AUDIENCE,
        "state": state,
        "client_id": WEB_CLIENT_ID,
        "prompt": "login",
    }
    headers = {"User-Agent": USER_AGENT_WEB, "Accept": "text/html,*/*"}
    async with session.get(
        AUTHORIZE_URL, params=params, headers=headers, allow_redirects=False
    ) as resp:
        if resp.status not in (302, 303):
            body = (await resp.text())[:200]
            raise RuntimeError(
                f"step=authorize: expected 302/303, got {resp.status}; body={body!r}"
            )
        loc = resp.headers["Location"]
    _LOGGER.warning("Ecobee auth: step=authorize -> 302 loc=%s", loc[:200])
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    if "state" not in qs:
        raise RuntimeError(f"step=authorize: no state in redirect loc={loc!r}")
    return qs["state"][0]


async def _post_login_form(
    session: aiohttp.ClientSession, url: str, state: str, form: dict
) -> str:
    """POST a /u/login/* form. Return the redirect ``Location`` on success."""
    headers = {
        "User-Agent": USER_AGENT_WEB,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,*/*",
        "Origin": f"https://{AUTH0_DOMAIN}",
        "Referer": f"{url}?state={state}",
    }
    body = urllib.parse.urlencode(form)
    async with session.post(
        url,
        params={"state": state},
        data=body,
        headers=headers,
        allow_redirects=False,
    ) as resp:
        if resp.status not in (302, 303):
            text = await resp.text()
            # Auth0 ULP renders field-level errors as
            #   class="ulp-input-error-message" data-error-code="<code>"
            # Surface the first code so the user sees a meaningful
            # reason instead of a bare HTTP 400.
            m = re.search(r'data-error-code="([^"]+)"', text)
            code = m.group(1) if m else None
            _LOGGER.warning(
                "POST %s -> %s; auth0 error code=%s", url, resp.status, code
            )
            if code:
                if any(
                    s in code.lower()
                    for s in ("password", "credential", "user", "lock", "blocked")
                ):
                    raise InvalidCredentialsError(f"login rejected ({code})")
                raise RuntimeError(
                    f"step=login_form url={url} status={resp.status} auth0_code={code}"
                )
            raise RuntimeError(
                f"step=login_form url={url} status={resp.status} no_code body={text[:200]!r}"
            )
        return resp.headers["Location"]


async def _identifier_step(
    session: aiohttp.ClientSession, state: str, email: str
) -> str:
    """POST /u/login/identifier with the email; advance to /u/login/password."""
    form = {
        "state": state,
        "username": email,
        "js-available": "true",
        "webauthn-available": "true",
        "is-brave": "false",
        "webauthn-platform-available": "true",
        "action": "default",
    }
    loc = await _post_login_form(session, IDENTIFIER_URL, state, form)
    _LOGGER.warning("Ecobee auth: step=identifier -> loc=%s", loc[:200])
    parsed = urllib.parse.urlparse(loc)
    if not parsed.path.endswith("/u/login/password"):
        # Auth0 sends us back to /u/login/identifier when the email is
        # not recognized; surface that as bad credentials.
        raise InvalidCredentialsError("email not recognized")
    return urllib.parse.parse_qs(parsed.query)["state"][0]


async def _password_step(
    session: aiohttp.ClientSession, state: str, email: str, password: str
) -> str:
    """POST /u/login/password; advance to /authorize/resume."""
    form = {
        "state": state,
        "username": email,
        "password": password,
        "action": "default",
    }
    loc = await _post_login_form(session, PASSWORD_URL, state, form)
    _LOGGER.warning("Ecobee auth: step=password -> loc=%s", loc[:200])
    parsed = urllib.parse.urlparse(loc)
    if not parsed.path.endswith("/authorize/resume"):
        raise InvalidCredentialsError(f"step=password: rejected loc={loc!r}")
    return urllib.parse.parse_qs(parsed.query)["state"][0]


async def _resume_to_code(session: aiohttp.ClientSession, resume_state: str) -> str:
    """GET /authorize/resume?state=… and turn the eventual web callback into a code.

    Loops up to 5 times to handle Auth0 prompt redirects (T&C updates,
    cookie consent, MFA challenges, profile completion, etc.) that
    chain between password submit and the final code redirect. Each
    such prompt presents as a /u/* redirect after password — we fetch
    the page, post back the form, and recurse on the new resume state.
    Loop bound prevents infinite redirect storms if a prompt can't be
    auto-handled.

    The bound is set to 5 because MFA flows can chain identifier ->
    password -> mfa-detect -> mfa-otp-challenge -> custom-prompt before
    reaching the callback; tighter bounds were occasionally too tight
    on 2FA-enabled accounts.
    """
    headers = {"User-Agent": USER_AGENT_WEB, "Accept": "text/html,*/*"}
    for attempt in range(5):
        async with session.get(
            RESUME_URL,
            params={"state": resume_state},
            headers=headers,
            allow_redirects=False,
        ) as resp:
            if resp.status not in (302, 303):
                body = (await resp.text())[:200]
                raise RuntimeError(
                    f"step=resume: expected 302/303, got {resp.status}; body={body!r}"
                )
            loc = resp.headers["Location"]
        _LOGGER.warning("Ecobee auth: step=resume -> loc=%s", loc[:200])

        # Final destination: the web callback URL with ?code=&state=.
        # We never actually navigate to the callback — we just lift
        # the code out of the Location header.
        if loc.startswith(REDIRECT_URI) or loc.startswith(
            "https://www.ecobee.com/home/authCallback"
        ):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
            if "code" not in qs:
                raise RuntimeError(f"step=resume: no code in redirect loc={loc!r}")
            return qs["code"][0]

        # Any /u/* path (custom-prompt, mfa-detect, mfa-otp-challenge,
        # mfa-push-challenge-push, consent, etc.) goes through the same
        # generic prompt handler — it POSTs ``state=<state>&action=default``
        # which works for primary-button "Continue / Confirm / Submit"
        # actions across Auth0's universal-login surface.
        if "/u/" in loc:
            resume_state = await _handle_custom_prompt(session, loc)
            continue

        raise RuntimeError(f"step=resume: unexpected scheme loc={loc!r}")

    raise RuntimeError(
        "step=resume: 5 consecutive Auth0 prompts without reaching the "
        "callback redirect. Sign in to https://www.ecobee.com from a "
        "browser, complete any pending prompts (T&C, MFA setup, "
        "profile completion), then retry the integration setup."
    )


async def _handle_custom_prompt(session: aiohttp.ClientSession, loc: str) -> str:
    """POST an Auth0 /u/* prompt page back to itself; return next resume state.

    Auth0 universal-login pages are React-rendered — the visible form
    is hydrated client-side from JSON in a ``<script>`` tag, so static
    HTML parsing can't find a ``<form>`` tag. We bypass parsing
    entirely: the POST endpoint is always the same path the GET
    landed on, and the body is always ``state=<state>&action=default``
    for the primary button (Auth0's universal convention — confirmed
    via the auth0 universal-login source).

    The same handler covers most Auth0 prompts: ``/u/custom-prompt/<id>``
    (T&C, profile completion), ``/u/mfa-detect``, ``/u/mfa-push-*`` (push
    auto-approves once the user taps the notification on their phone).

    For prompts that REQUIRE typing a value into the form (the 6-digit
    code on ``/u/mfa-otp-challenge`` etc.), ``action=default`` returns
    400 because the form has no `code` field. We short-circuit those
    prompts BEFORE the POST and raise ``MFACodeRequiredError`` with the
    state needed to resume after the user provides the code via our
    config flow's mfa-code step.
    """
    abs_url = (
        loc if loc.startswith("http") else f"https://{AUTH0_DOMAIN}{loc}"
    )
    parsed = urllib.parse.urlparse(abs_url)
    qs = urllib.parse.parse_qs(parsed.query)
    state = qs.get("state", [""])[0]
    if not state:
        raise RuntimeError(f"step=custom-prompt: no state in url={abs_url!r}")

    # Recognise code-entry MFA prompts up-front. Path-pattern match
    # because Auth0 names these consistently across tenants. If new
    # variants surface (mfa-recovery-code-challenge etc.), the regex
    # below catches them — keep it broad on purpose.
    if re.search(r"/u/mfa-(otp|sms|recovery-code)(-challenge)?(/|\?|$)", parsed.path):
        challenge_type = "otp" if "otp" in parsed.path else (
            "sms" if "sms" in parsed.path else "recovery"
        )
        _LOGGER.warning(
            "Ecobee auth: step=custom-prompt code-required prompt=%s url=%s",
            challenge_type, abs_url[:160],
        )
        raise MFACodeRequiredError(
            prompt_url=abs_url,
            state=state,
            challenge_type=challenge_type,
        )

    # Fetch the page to inspect the embedded prompt config. Auth0
    # universal-login pages ship the React props as JSON inside a
    # <script id="__NEXT_DATA__"> tag — the prompt name + required
    # form fields are in there. We log the relevant bits so a failing
    # POST below has actionable diagnostics in the trace.
    headers_get = {"User-Agent": USER_AGENT_WEB, "Accept": "text/html,*/*"}
    async with session.get(abs_url, headers=headers_get, allow_redirects=False) as resp:
        page = await resp.text() if resp.status == 200 else ""
    nd = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        page, re.DOTALL,
    )
    if nd:
        try:
            nd_json = json.loads(nd.group(1))
            prompt_blob = (
                nd_json.get("props", {}).get("pageProps", {}).get("prompt")
                or nd_json.get("prompt")
            )
            _LOGGER.warning(
                "Ecobee auth: step=custom-prompt config=%s",
                json.dumps(prompt_blob)[:1500] if prompt_blob else "(no prompt key)",
            )
        except (json.JSONDecodeError, KeyError, AttributeError) as e:
            _LOGGER.warning(
                "Ecobee auth: step=custom-prompt __NEXT_DATA__ parse failed: %s; "
                "raw[:500]=%r", e, nd.group(1)[:500],
            )
    else:
        # Auth0 Forms (the post-2024 form-builder feature, distinguished
        # by .af-custom-form-container CSS classes) embeds its JSON in
        # `window.universal_login_context = {...};` rather than
        # __NEXT_DATA__. Pull that out if present.
        ulc = re.search(
            r'window\.universal_login_context\s*=\s*(\{.*?\});\s*<',
            page, re.DOTALL,
        )
        if ulc:
            try:
                ulc_json = json.loads(ulc.group(1))
                _LOGGER.warning(
                    "Ecobee auth: step=custom-prompt ulc=%s",
                    json.dumps(ulc_json)[:3000],
                )
            except json.JSONDecodeError as e:
                _LOGGER.warning(
                    "Ecobee auth: step=custom-prompt ulc parse failed: %s; "
                    "raw[:1000]=%r", e, ulc.group(1)[:1000],
                )
        else:
            _LOGGER.warning(
                "Ecobee auth: step=custom-prompt no embedded JSON; "
                "page[:3000]=%r", page[:3000],
            )

    headers_post = {
        "User-Agent": USER_AGENT_WEB,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,*/*",
        "Origin": f"https://{AUTH0_DOMAIN}",
        "Referer": abs_url,
    }
    body = {"state": state, "action": "default"}
    async with session.post(
        abs_url, data=body, headers=headers_post, allow_redirects=False,
    ) as resp:
        status = resp.status
        if status not in (302, 303):
            page = (await resp.text())[:300]
            raise RuntimeError(
                f"step=custom-prompt: POST {abs_url[:120]} -> {status} "
                f"(expected 302/303). The prompt requires interactive "
                f"action (most likely email verification, MFA setup, or "
                f"profile completion). Sign in to https://www.ecobee.com "
                f"from a browser, complete any pending step shown there, "
                f"then retry the HA integration setup. Page snippet: {page!r}"
            )
        new_loc = resp.headers["Location"]
    _LOGGER.warning(
        "Ecobee auth: step=custom-prompt POST -> %d loc=%s",
        status, new_loc[:200],
    )

    # Most prompts redirect to /authorize/resume?state=<new>; some
    # chain to another /u/* page — the caller's loop handles that case
    # (we just return whatever state we found here).
    parsed_new = urllib.parse.urlparse(new_loc)
    if parsed_new.path.endswith("/authorize/resume"):
        new_qs = urllib.parse.parse_qs(parsed_new.query)
        if "state" not in new_qs:
            raise RuntimeError(f"step=custom-prompt: no state in loc={new_loc!r}")
        return new_qs["state"][0]
    if "/u/" in new_loc:
        chained_state = urllib.parse.parse_qs(parsed_new.query).get("state", [""])[0]
        if not chained_state:
            raise RuntimeError(
                f"step=custom-prompt: chained prompt has no state: {new_loc!r}"
            )
        return chained_state
    raise RuntimeError(
        f"step=custom-prompt: unexpected redirect target loc={new_loc!r}"
    )


async def _exchange_code(
    session: aiohttp.ClientSession, code: str, verifier: str
) -> dict:
    """POST /oauth/token grant_type=authorization_code; return token payload."""
    body = {
        "grant_type": "authorization_code",
        "client_id": WEB_CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    async with session.post(TOKEN_URL, data=body, headers=headers) as resp:
        text = await resp.text()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw": text}
        if resp.status == 200:
            return payload
        raise RuntimeError(
            f"code exchange failed: {resp.status} {payload}"
        )


# ---------------------------------------------------------------------------
# EcobeeAuth — the main reusable handle
# ---------------------------------------------------------------------------


class EcobeeAuth:
    """Holds the long-lived refresh token and mints fresh access tokens.

    Instances are reused across the lifetime of a ConfigEntry. The
    same aiohttp session is reused for token + API calls.
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
        dict). Auth0 may or may not rotate on each refresh depending
        on tenant configuration; we handle both cases.
        """
        self._rt_persist_cb = cb

    @classmethod
    async def login(
        cls, session: aiohttp.ClientSession, email: str, password: str
    ) -> "EcobeeAuth":
        """Run the full Auth0 universal-login flow and return a ready instance.

        The Auth0 universal-login flow is stateful: /authorize sets a
        session cookie that /u/login/identifier and /u/login/password
        require. We use a dedicated cookie-jar-backed session for the
        login flow only; the caller's long-lived ``session`` is reused
        afterward for refresh-token rotation, which doesn't depend on
        cookies.

        Raises:
            InvalidCredentialsError: bad email or password rejected at
                /u/login/identifier or /u/login/password.
            RuntimeError: any other unexpected step failure (network,
                Auth0 redirect chain breakage, etc.).
        """
        verifier, challenge = _make_pkce()
        state = _b64url(secrets.token_bytes(32))

        # Use the shared session so cookies persist across the call.
        # If Auth0 surfaces a code-entry MFA prompt mid-flow,
        # _handle_custom_prompt raises MFACodeRequiredError which
        # bubbles out of _resume_to_code; the config_flow catches it,
        # prompts the user for the code, then calls
        # continue_with_mfa_code on the same shared session so the
        # cookie jar still holds Auth0's session cookies.
        login_state = await _authorize(session, state, challenge)
        pw_state = await _identifier_step(session, login_state, email)
        resume_state = await _password_step(
            session, pw_state, email, password
        )
        try:
            code = await _resume_to_code(session, resume_state)
        except MFACodeRequiredError as ex:
            # Enrich with the verifier + email so continue_with_mfa_code
            # has everything it needs once the user provides the code.
            ex.verifier = verifier
            ex.email = email
            raise
        tokens = await _exchange_code(session, code, verifier)

        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "login: no refresh_token returned (scope did not include "
                "offline_access?)"
            )

        auth = cls(session, refresh_token, email=email)
        auth._access_token = tokens["access_token"]
        auth._access_token_exp = time.time() + int(tokens.get("expires_in", 0))
        _LOGGER.info(
            "ecobee universal-login OK: expires_in=%s scope=%s token_type=%s",
            tokens.get("expires_in"),
            tokens.get("scope"),
            tokens.get("token_type"),
        )
        return auth

    @classmethod
    async def continue_with_mfa_code(
        cls,
        session: aiohttp.ClientSession,
        *,
        prompt_url: str,
        state: str,
        code: str,
        verifier: str,
        email: str,
    ) -> "EcobeeAuth":
        """Resume login after the user provides an MFA code.

        Caller is the config_flow's mfa-code step. ``prompt_url`` and
        ``state`` come from the MFACodeRequiredError raised by
        ``login``; ``verifier`` and ``email`` were enriched there too.
        ``session`` MUST be the same shared session used by ``login``
        — its cookie jar holds the Auth0 session cookies.

        Raises:
            MFACodeInvalidError: Auth0 rejected the code (typo or
                expired one-time-password); caller re-renders the
                code form.
            MFACodeExpiredError: the prompt's state has expired
                (~10 min); caller bounces back to the password step.
            RuntimeError: unexpected redirect / network failure.
        """
        body = {"state": state, "code": code}
        headers = {
            "User-Agent": USER_AGENT_WEB,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,*/*",
            "Origin": f"https://{AUTH0_DOMAIN}",
            "Referer": prompt_url,
        }
        async with session.post(
            prompt_url, data=body, headers=headers, allow_redirects=False,
        ) as resp:
            status = resp.status
            if status == 200:
                # Auth0 re-renders the prompt form when the code is wrong
                # or the prompt has expired. Inspect the page to choose
                # which typed error to raise.
                page = (await resp.text()).lower()
                if "expired" in page or "session has expired" in page:
                    raise MFACodeExpiredError(
                        "MFA prompt expired; restart from email/password."
                    )
                raise MFACodeInvalidError(
                    "MFA code rejected by Auth0; check the code and retry."
                )
            if status not in (302, 303):
                page = (await resp.text())[:200]
                raise RuntimeError(
                    f"continue_with_mfa_code: POST -> {status}; body={page!r}"
                )
            new_loc = resp.headers["Location"]
        _LOGGER.warning(
            "Ecobee auth: step=mfa-code POST -> %d loc=%s",
            status, new_loc[:200],
        )

        # Auth0 typically redirects back to /authorize/resume after a
        # successful prompt POST; from there _resume_to_code chases the
        # rest of the chain (which may include more prompts).
        parsed = urllib.parse.urlparse(new_loc)
        if parsed.path.endswith("/authorize/resume"):
            new_state = urllib.parse.parse_qs(parsed.query).get("state", [""])[0]
            if not new_state:
                raise RuntimeError(
                    f"continue_with_mfa_code: no state in {new_loc!r}"
                )
            try:
                auth_code = await _resume_to_code(session, new_state)
            except MFACodeRequiredError as ex:
                # Chained MFA prompt (rare). Preserve carry-through state
                # so the config_flow can prompt again.
                ex.verifier = verifier
                ex.email = email
                raise
        elif "/u/" in parsed.path:
            # Auth0 chained another in-line prompt without going through
            # /authorize/resume first. Hand off to _handle_custom_prompt
            # via the standard loop.
            try:
                auth_code = await _resume_to_code(session, state)
            except MFACodeRequiredError as ex:
                ex.verifier = verifier
                ex.email = email
                raise
        elif new_loc.startswith("https://www.ecobee.com/home/authCallback"):
            # Direct callback (no further resume needed).
            auth_code = urllib.parse.parse_qs(parsed.query).get("code", [""])[0]
            if not auth_code:
                raise RuntimeError(
                    f"continue_with_mfa_code: callback had no code: {new_loc!r}"
                )
        else:
            raise RuntimeError(
                f"continue_with_mfa_code: unexpected redirect loc={new_loc!r}"
            )

        tokens = await _exchange_code(session, auth_code, verifier)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "continue_with_mfa_code: no refresh_token returned"
            )
        auth = cls(session, refresh_token, email=email)
        auth._access_token = tokens["access_token"]
        auth._access_token_exp = time.time() + int(tokens.get("expires_in", 0))
        _LOGGER.info(
            "ecobee universal-login + MFA OK: expires_in=%s scope=%s",
            tokens.get("expires_in"), tokens.get("scope"),
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
        async with self._session.post(TOKEN_URL, data=body, headers=headers) as resp:
            text = await resp.text()
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"raw": text}
            status = resp.status

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
