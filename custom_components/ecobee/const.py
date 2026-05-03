"""Constants for the Ecobee Anderson fork integration.

This integration shadows the Home Assistant core ``ecobee`` integration
because HA loads ``custom_components/`` first. The bypass exists for one
reason: ecobee shut down public dev-portal API-key registration in 2024,
so the core integration's PIN/API-key flow can no longer be set up by
new users. v0.3 uses Auth0 Authorization Code flow with PKCE +
universal-login against ecobee's Auth0 tenant — the same flow ecobee.com
itself uses, which means MFA is handled natively by Auth0's hosted
prompt pages (we just follow the redirect chain).
"""

from homeassistant.const import Platform

DOMAIN = "ecobee"
NAME = "Ecobee (Anderson fork)"
MANUFACTURER = "ecobee"

# Auth0 tenant + endpoints. Same tenant as the parallel Generac fork.
AUTH0_DOMAIN = "auth.ecobee.com"

# /authorize is the universal-login entry point. We GET it with the
# PKCE challenge + state and follow the redirect to /u/login/identifier.
AUTHORIZE_URL = f"https://{AUTH0_DOMAIN}/authorize"

# /authorize/resume is what Auth0 redirects to after every /u/* prompt
# step (login, MFA, T&C). It either chains to another /u/* page or
# redirects to our REDIRECT_URI with the auth code.
RESUME_URL = f"https://{AUTH0_DOMAIN}/authorize/resume"

# /u/login/identifier accepts the email; /u/login/password accepts the
# password. Both are POSTed with form-encoded bodies.
IDENTIFIER_URL = f"https://{AUTH0_DOMAIN}/u/login/identifier"
PASSWORD_URL = f"https://{AUTH0_DOMAIN}/u/login/password"

# /oauth/token: code -> tokens, refresh_token -> tokens.
TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"

# The web-app callback URL. Auth0 redirects here with ?code=&state=
# after a successful login. We never actually navigate to it — we just
# parse the params from the Location header on the final 302.
REDIRECT_URI = "https://www.ecobee.com/home/authCallback"

# The public web-app client_id. Same one pyecobee's
# request_tokens_web() targets and that ecobee.com itself ships in
# plain JS. No client_secret is needed for an Auth0 public client doing
# authorization-code + PKCE.
WEB_CLIENT_ID = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"

# Audience claim: what Auth0 audits the access_token against. The REST
# API base URL (api.ecobee.com/1) is different from the audience —
# don't confuse them.
AUDIENCE = "https://prod.ecobee.com/api/v1"

# Scopes requested at /authorize. ``offline_access`` is what makes
# Auth0 issue a refresh_token; without it we'd need to interactively
# re-login on every access-token expiry.
SCOPES = "openid smartRead smartWrite piiRead piiWrite offline_access"

# REST API endpoints. ecobee's API is HTTPS + Bearer auth.
API_BASE = "https://api.ecobee.com/1"
API_THERMOSTAT = f"{API_BASE}/thermostat"

# Configuration / entry data keys.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_SCAN_INTERVAL = "scan_interval"

# Polling cadence. ecobee's docs say "do not poll a thermostat more
# often than once every three minutes" (https://www.ecobee.com/home/
# developer/api/documentation/v1/objects/Selection.shtml). Default 5 min
# is a polite margin and matches what the core integration does.
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 180

# Platforms we expose. We deliberately ship a small set vs. core's huge
# surface (core does humidifier/notify/number/switch too) — those aren't
# needed for the per-room-sensor SmartThings-bypass goal and would
# multiply the code+test surface for no payoff.
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.WEATHER,
]

# Map ecobee modelNumber -> human-readable name for the device registry.
# Mirrors HA core's table so device cards look the same after migration.
ECOBEE_MODEL_TO_NAME = {
    "idtSmart": "ecobee Smart",
    "idtEms": "ecobee Smart EMS",
    "siSmart": "ecobee Si Smart",
    "siEms": "ecobee Si EMS",
    "athenaSmart": "ecobee3 Smart",
    "athenaEms": "ecobee3 EMS",
    "corSmart": "Carrier/Bryant Cor",
    "nikeSmart": "ecobee3 lite Smart",
    "nikeEms": "ecobee3 lite EMS",
    "apolloSmart": "ecobee4 Smart",
    "vulcanSmart": "ecobee4 Smart",
    "aresSmart": "ecobee Smart Premium",
    "artemisSmart": "ecobee Smart Enhanced",
    "attisRetail": "ecobee Smart Thermostat with Voice Control",
}
