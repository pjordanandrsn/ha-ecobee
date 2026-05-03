# Ecobee (community fork) — Home Assistant integration

[![HACS Custom][hacs-badge]][hacs]
[![Validate][validate-badge]][validate-workflow]
[![Tests][tests-badge]][tests-workflow]

A Home Assistant custom integration that polls ecobee thermostats
**without** requiring an ecobee developer-portal API key. Authenticates
against ecobee's Auth0 tenant using the standard Authorization Code
flow with PKCE + universal-login — the same flow ecobee.com uses on
the web — and exposes per-room remote sensor data that the SmartThings
cloud bridge hides.

> **Status:** Maintained as a side-fork; community PRs welcome — no SLA.
> Read-only in v0.3; see [Read-only by design](#read-only-by-design).

## Why this fork exists

ecobee shut down public dev-portal API key registration in 2024. The
**Home Assistant core ecobee integration still expects a dev-portal
API key for new setups**, so a brand-new HA install can no longer add
ecobee via the core integration even if you have an ecobee account.

The common workaround — pulling ecobee data through SmartThings cloud
— works for thermostat-level temp/setpoint, but **SmartThings hides
per-room remote sensor data**. Hallway/bedroom temperature and
occupancy from the SmartSensors are not exposed.

The ecobee mobile app and ecobee.com both authenticate against
ecobee's Auth0 tenant using a public web `client_id`. The bash project
[`r00k/ecobee-cli`][r00k] proved that bypassing the dev-portal API
key is possible. v0.3 of this integration uses the Auth0
Authorization Code flow with PKCE + universal-login (the same flow
ecobee.com itself uses on the web), which means MFA is handled
natively by Auth0's hosted prompt pages — no per-step HA UI needed.

This integration ships ~450 LoC of Auth0 universal-login + ~100 LoC
of API client mapped onto HA entities, **including** per-room remote
sensors.

## Install via HACS

1. In Home Assistant, open **HACS**.
2. Open the top-right menu and pick **Custom repositories**.
3. Add this repository:
   - Repository: `https://github.com/pjordanandrsn/ha-ecobee`
   - Category: `Integration`
4. Click **Add**.
5. Find **Ecobee (community fork)** in the HACS integration list and
   click **Download**.
6. Restart Home Assistant.
7. **Settings → Devices & Services → Add Integration → Ecobee
   (community fork)** and follow [First-time setup](#first-time-setup).

## Manual install (no HACS)

1. Copy `custom_components/ecobee/` from this repo into your HA
   `config/custom_components/` directory. If you already have the HA
   core ecobee integration installed, this fork's directory will
   overlay it — they share the `ecobee` domain. Make sure you don't
   have any active config entries for the core integration first.
2. Restart Home Assistant.
3. Add via **Settings → Devices & Services → Add Integration → Ecobee
   (community fork)**.

## First-time setup

Adding the integration requires a Home Assistant **admin** session
(non-admin users cannot add integrations).

1. Open `http://<HA_HOST>:8123` and sign in as an admin.
2. **Settings → Devices & Services → Add Integration**.
3. Search **Ecobee**. You may see two options:
   - **Ecobee** — the HA core one. Will fail unless you already have a
     dev-portal API key.
   - **Ecobee (community fork)** — pick **this** one.
4. Enter your **ecobee.com** email and password (NOT your SmartThings
   login).
5. Submit. If your account has 2FA enabled, see
   [2FA / MFA support](#2fa--mfa-support).
6. Within ~30 s, an entry appears with one or more thermostats and
   their remote sensors.

After it's working, **don't disable the SmartThings entry yet** —
verify the new entities first
(`sensor.ecobee_<room>_temperature`,
`binary_sensor.ecobee_<room>_occupancy`). Once verified, manually
disable the SmartThings ecobee thermostats so the two integrations
don't race while you wire dashboard tiles to the new entity IDs.

### What entities you get

After the first poll, the entity registry contains:

- `climate.<thermostat_name>` — read-only; exposes
  `current_temperature`, `target_temperature` (or
  `target_temperature_high` / `_low` in HEAT_COOL), HVAC mode, and
  HVAC action.
- `sensor.ecobee_<room>_temperature` — one per remote sensor.
- `sensor.ecobee_<room>_humidity` — one per remote sensor that reports
  humidity (Smart Premium and Lite generally do; the older ecobee3
  SmartSensor doesn't).
- `binary_sensor.ecobee_<room>_occupancy` — one per remote sensor.
- `weather.<thermostat_name>` — forecast at the thermostat's location.
- `sensor.ecobee_<thermostat_name>_outdoor_temperature` — current
  outdoor temp from the same forecast block.

Slugification for `<room>` follows what you set as the room labels in
the ecobee app (e.g. `Living Room` → `living_room`).

## 2FA / MFA support

**Native in v0.3.1+.** The integration uses Auth0's universal-login
flow — the same web sign-in ecobee.com uses. The Home Assistant
config flow starts as a single email + password form; if your
account has 2FA enabled, Auth0 surfaces an OTP-challenge page
mid-flow that the integration intercepts and turns into a Home
Assistant **"Two-factor verification"** step.

### What you'll see at setup

1. **Email + password step** — submit your ecobee.com credentials.
2. **(Only if 2FA is on) Two-factor verification step** — a single
   field asking for the 6-digit code. Read it from your authenticator
   app:
   - **Authy / Google Authenticator / 1Password / Microsoft
     Authenticator / Duo / etc.** — open the app, find your ecobee
     entry, type the current code.
   - **SMS / recovery code** — use the code Auth0 sends or your
     stored recovery code.
   - **Push notifications are NOT supported** in this version. If
     your only factor is an Auth0 Guardian push prompt, set up a
     backup OTP factor on ecobee.com first.
3. **Done** — entry is created and thermostats + remote sensors
   appear within ~30 seconds.

### Error handling

- **Wrong code.** The form re-renders with a "code didn't match"
  message; just type the new code without restarting from the
  password step.
- **Expired session (~10 minutes between password and code).** Auth0
  ages out the prompt's state token; the flow bounces back to the
  email + password step so a fresh session can be minted.
- **No 2FA on the account.** The MFA step is skipped automatically;
  setup completes after the password step.

### Once-per-setup pattern

The MFA prompt fires exactly **once** at config-entry creation (and
once at reauth). After we have the post-MFA refresh token, all
subsequent Auth0 token refreshes go through the regular
`grant_type=refresh_token` path — no MFA prompt. **You should not
need to re-enter the MFA code on subsequent restarts** — only when
the refresh token gets revoked (password change or ecobee revoking
the web client RTs).

> **History.** v0.2.0 attempted MFA via Auth0 ROPG MFA grants, which
> ecobee's web client rejects with `unauthorized_client`. v0.3.0
> switched to authorization-code + universal-login but tried to walk
> through the OTP prompt page programmatically (which doesn't work —
> Auth0 expects a real code). v0.3.1 adds the dedicated
> "Two-factor verification" step so the user types the code directly
> into Home Assistant.

## Read-only by design

v0.3 ships **read-only**: the climate entity exposes state but does
NOT implement `set_hvac_mode` / `set_hold` / etc. Reasons:

1. The fork's stated goal is per-room sensor visibility, not control.
2. The full ecobee write surface (hold types, vacation events,
   fan-min-on-time, comfort settings) is large; reproducing it would
   balloon the test surface for no payoff toward the integration's
   goal.
3. If you migrate from the SmartThings ecobee path, leaving the
   SmartThings entry temporarily active for control while the new
   integration handles read avoids a race window.

If you want control, `api.py` already has the auth + Bearer plumbing —
adding a `POST /1/thermostat` helper plus the corresponding
`async_set_hvac_mode` etc. on the climate entity is the next iteration.

## Known gotchas

- **Web `client_id` and ecobee ToS.** This integration uses ecobee's
  public web `client_id` (`183eORFPlXyz9BbDZwqexHPBQoVjgadh`) as if it
  were ecobee.com. ecobee's ToS technically prohibits "imitating"
  their services, but this `client_id` is hard-coded in public
  ecobee.com JS and `pyecobee`'s `request_tokens_web()` does the same
  thing under HA core's hood for accounts set up before the dev portal
  closed — same posture as the upstream HA integration. If ecobee
  rotates the web `client_id`, update `WEB_CLIENT_ID` in `const.py`.
  The ecobee.com web UI is the canonical source for the current value.
- **3-minute poll floor.** ecobee documents a 3-minute minimum poll
  cadence per thermostat. The coordinator floors the configured
  scan_interval at `MIN_SCAN_INTERVAL = 180 s`; default is 300 s
  (5 min). Going lower invites rate-limit errors and offers no
  real-time benefit.
- **Refresh token rotation.** Auth0 may or may not rotate refresh
  tokens depending on tenant config. We handle both cases — if a new
  RT comes back, the persist callback writes it into the entry's data;
  if the same RT comes back, we skip the write. Frequent
  "next HA restart will need reauth" warnings indicate the persist
  callback path is failing — investigate.
- **`runtime.connected`.** Per-thermostat `runtime.connected` drives
  the `available` flag on every entity tied to that thermostat. Brief
  Wi-Fi disconnects flip the entire group to unavailable for one poll
  cycle until the thermostat checks back in. Same as the HA core
  integration's behavior.
- **`/api/v1` audience vs `/1` request URL.** The Auth0 access-token
  audience claim is `https://prod.ecobee.com/api/v1`. The actual REST
  base URL is `https://api.ecobee.com/1` (no `/v` prefix, no `/api`).
  Both correct — the audience is what Auth0 audits the token against;
  the request URL is what `api.ecobee.com` expects.
- **Refresh-token longevity.** Same RT works across HA restarts and
  through the rotation pattern above. The only known revocation
  vectors are: ecobee account password change, or ecobee revoking the
  web-client RTs server-side.

## Upstreaming progress

No upstream tracking — HA core's ecobee integration won't accept this
approach (proxying the user's credentials through Auth0's
universal-login isn't the recommended HA pattern), and ecobee hasn't
restored the public dev portal. If the HA core integration ever
adopts a similar fallback, this fork can shrink to just the per-room
sensor mapping logic.

If ecobee restores the dev portal, the cleanest migration is:

1. Remove the `ecobee` entry created by this integration.
2. Add the HA core ecobee integration with your freshly minted PIN.
3. Uninstall this custom integration.

## Tests

97 tests live in `tests/`. Run locally:

```sh
python3 -m venv /tmp/ecobee-test-venv
/tmp/ecobee-test-venv/bin/pip install \
    homeassistant==2026.2.3 \
    pytest==9.0.0 \
    pytest-homeassistant-custom-component==0.13.316 \
    pytest-asyncio==1.3.0 \
    aiohttp \
    voluptuous
/tmp/ecobee-test-venv/bin/python -m pytest tests/ -v
```

CI runs these on every push and PR — see
[`.github/workflows/tests.yaml`](.github/workflows/tests.yaml).

## Community + upstream tracking

- [Discussion of the dev-portal closure that motivated this fork][forum-thread]
- [HA core issues tracking the dev-portal closure][ha-core-issues]:
  [#131789][issue-131789], [#169328][issue-169328], [#146560][issue-146560]
- [Sibling fork using the same Auth0 universal-login pattern][ha-generac]
- Inspirations: [`r00k/ecobee-cli`][r00k] (auth pattern reference),
  [`JJTech0130/ha-ecobee`][jjtech-fork] (early dev-portal-bypass attempt),
  [`nkgilley/python-ecobee-api`][pyecobee] +
  [`mikeg0/ecobeectl`][ecobeectl] (mobile-app constants reference)

[forum-thread]: https://community.home-assistant.io/t/ecobee-dumped-developer-accounts/711817
[ha-core-issues]: https://github.com/home-assistant/core/issues
[issue-131789]: https://github.com/home-assistant/core/issues/131789
[issue-169328]: https://github.com/home-assistant/core/issues/169328
[issue-146560]: https://github.com/home-assistant/core/issues/146560
[ha-generac]: https://github.com/pjordanandrsn/ha-generac
[jjtech-fork]: https://github.com/JJTech0130/ha-ecobee
[ecobeectl]: https://github.com/mikeg0/ecobeectl

## License

[Apache 2.0][license]. This integration is original work modeled on
the patterns of the HA core ecobee integration (also Apache 2.0). No
upstream fork base.

Copyright (c) 2026 pjordanandrsn.

## Acknowledgments

- [`r00k/ecobee-cli`][r00k] — proved the ecobee Auth0 ROPG path works
  without a dev-portal API key.
- [`pyecobee`][pyecobee] — `request_tokens_web()` is the reference
  implementation for the web `client_id` flow that the HA core
  integration uses for legacy accounts.
- [HA core `ecobee` integration][ha-core-ecobee] — entity layout,
  service surface, and slugification conventions.

[r00k]: https://github.com/r00k/ecobee-cli
[pyecobee]: https://github.com/nkgilley/python-ecobee-api
[ha-core-ecobee]: https://github.com/home-assistant/core/tree/dev/homeassistant/components/ecobee
[license]: ./LICENSE
[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-orange.svg
[validate-workflow]: https://github.com/pjordanandrsn/ha-ecobee/actions/workflows/validate.yaml
[validate-badge]: https://github.com/pjordanandrsn/ha-ecobee/actions/workflows/validate.yaml/badge.svg
[tests-workflow]: https://github.com/pjordanandrsn/ha-ecobee/actions/workflows/tests.yaml
[tests-badge]: https://github.com/pjordanandrsn/ha-ecobee/actions/workflows/tests.yaml/badge.svg
