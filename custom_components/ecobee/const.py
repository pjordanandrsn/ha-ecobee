"""Constants for the Ecobee Anderson fork integration.

This integration shadows the Home Assistant core ``ecobee`` integration
because HA loads ``custom_components/`` first. The bypass exists for one
reason: ecobee shut down public dev-portal API-key registration in 2024,
so the core integration's PIN/API-key flow can no longer be set up by new
users. We use Resource Owner Password Grant (ROPG) against ecobee's
Auth0 tenant with the public web client_id instead.
"""

from homeassistant.const import Platform

DOMAIN = "ecobee"
NAME = "Ecobee (Anderson fork)"
MANUFACTURER = "ecobee"

# Auth0 endpoints + the well-known public web-app client_id. This is the
# same client_id the official ecobee web UI uses; pyecobee's
# request_tokens_web() also targets it. Audience and scopes mirror what
# the web UI requests; ``offline_access`` is added so we get a
# refresh_token for long-running operation.
AUTH_URL = "https://auth.ecobee.com/oauth/token"
WEB_CLIENT_ID = "183eORFPlXyz9BbDZwqexHPBQoVjgadh"
AUTH_AUDIENCE = "https://prod.ecobee.com/api/v1"
AUTH_SCOPE = "openid smartRead smartWrite piiRead piiWrite offline_access"

# Auth0 MFA endpoints (used when ROPG returns 403 mfa_required). The
# /mfa/authenticators endpoint enumerates configured second factors;
# /mfa/challenge fires an SMS / push prompt for OOB factors. OTP factors
# (TOTP / authenticator app) need no challenge call — the user just
# reads the current code from their app.
MFA_AUTHENTICATORS_URL = "https://auth.ecobee.com/mfa/authenticators"
MFA_CHALLENGE_URL = "https://auth.ecobee.com/mfa/challenge"

# Auth0 MFA grant-type strings (Auth0-namespaced URLs, not opaque
# identifiers). These go in the grant_type form field on the token
# endpoint after the user supplies their second-factor code.
GRANT_TYPE_MFA_OTP = "http://auth0.com/oauth/grant-type/mfa-otp"
GRANT_TYPE_MFA_OOB = "http://auth0.com/oauth/grant-type/mfa-oob"

# Authenticator type strings as returned by /mfa/authenticators. Used
# to branch between OTP (no challenge) and OOB (needs challenge call).
MFA_TYPE_OTP = "otp"
MFA_TYPE_OOB = "oob"
MFA_TYPE_PUSH = "push-notification"

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
