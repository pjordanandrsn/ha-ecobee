# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.0] - 2026-05-05

### Added
- **Vacation events.** Two new domain-level services:
  `ecobee.create_vacation` (name + start_datetime + end_datetime +
  heat / cool setpoints + fan) and `ecobee.delete_vacation` (name).
  These map to ecobee's `createVacation` / `deleteVacation` API
  functions and are the right tool for "we're gone Mar 15–22, drop
  to 60°F heat / 85°F cool with fan auto, then resume the schedule"
  workflows that don't fit HA's preset_modes (which can't carry an
  end time). Vacations show up on the climate entity's
  `extra_state_attributes.vacations` list with name + running flag
  + start/end + setpoints, and `vacation_active` is a quick boolean
  for automation conditions.
- `EcobeeApiClient.async_create_vacation`,
  `EcobeeApiClient.async_delete_vacation`.

### Notes
- ecobee uses the vacation `name` as a primary key — duplicate names
  return an error rather than overwriting. The service caller is
  responsible for picking unique names; an HA convention to prefix
  with the start date (e.g. `2026-03-15 Spring Break`) avoids
  collisions when re-running the same template.
- Active vacations don't surface as an HA `preset_mode` because the
  preset selector can't carry the end time; the schedule's hold-style
  presets (home/away/sleep) remain on `preset_mode` and are mutually
  visible alongside the vacation in `extra_state_attributes`.

## [0.4.0] - 2026-05-05

### Added
- **Climate write surface.** The thermostat entity is no longer
  read-only — it now exposes the standard HA service surface:
  `climate.set_hvac_mode`, `climate.set_temperature`,
  `climate.set_fan_mode`, `climate.set_preset_mode`. Setpoints land
  via ecobee's `setHold` function with `holdType=nextTransition`
  (matches the ecobee app's default tap-to-adjust behaviour); preset
  modes map to the thermostat's own program climates (`home`,
  `away`, `sleep`, plus any custom climates the user defined in the
  ecobee app), with a synthesised `none` preset that calls
  `resumeProgram(resumeAll=true)` to return to the schedule.
- `EcobeeApiClient.async_update_settings`,
  `EcobeeApiClient.async_set_hold`,
  `EcobeeApiClient.async_resume_program` — the underlying API plumbing.

### Changed
- `_attr_supported_features` on the climate entity flipped from `0`
  to `TARGET_TEMPERATURE | TARGET_TEMPERATURE_RANGE | FAN_MODE |
  PRESET_MODE` so HA renders the controls in the more-info card.

### Why this took until 0.4
- v0.1 was deliberately read-only as a guard against double-control
  while the SmartThings entry was still installed. With that race
  no longer a concern, the gate's gone.

## [0.3.1] - 2026-05-01

### Added
- **OTP-challenge "Two-factor verification" step in the config flow.**
  When Auth0 surfaces an `/u/mfa-otp-challenge` (or
  `mfa-sms-challenge` / `mfa-recovery-code-challenge`) page during
  the universal-login redirect chain, the integration now intercepts
  it and turns it into a dedicated Home Assistant step that prompts
  the user for the 6-digit code (instead of trying to walk past the
  prompt programmatically, which v0.3.0 did and which doesn't work
  for code-entry factors).
- `MFACodeRequiredError`, `MFACodeInvalidError`, `MFACodeExpiredError`
  exceptions in `auth.py` plus an `EcobeeAuth.continue_with_mfa_code`
  classmethod that resumes the redirect chain after the user provides
  a code.
- New translations (`mfa_code` step UI strings) and `config_flow.py`
  branching to handle wrong-code re-render and expired-prompt fall-
  back without restarting the flow from email/password.
- 3 new unit tests covering the MFA-code-step happy path, wrong-code
  rejection, and expired-state fallback (100 tests total).

### Changed
- `EcobeeAuth.login` now reuses the caller's aiohttp session rather
  than building its own private cookie-jar session. The cookie jar
  carries through to a possible `continue_with_mfa_code` follow-up.

### Fixed
- 5 stale e2e tests in `test_auth.py` that asserted the v0.3.0
  "MFA-prompt-walks-through-with-action=default" behavior have been
  updated to the v0.3.1 MFA-code-required behavior.

### Changed (cosmetic)
- Display name in HACS / the Add-Integration picker changed from
  "Ecobee (Anderson fork)" to "Ecobee (community fork)". The
  integration `domain` is still `ecobee`, so existing config entries
  keep working without any user action — only the label updates.
- Manifest `version` no longer carries the `-anderson-fork` suffix.

### Migration notes
- Existing v0.3.0 entries continue to work as-is. The refresh-token
  grant path is unchanged; only the initial credential mint changed
  for 2FA-enabled accounts.
- No schema or storage migration; the config entry's data shape is
  still `{username, refresh_token}`.

## [0.3.0] - 2026-05-03

### Added
- Auth0 Authorization Code flow with PKCE + universal-login,
  replacing v0.2's Resource Owner Password Grant + ROPG-MFA grants.
- Generic `/u/<prompt>` handler that walks Auth0's hosted prompt
  pages between `/u/login/password` and `/authorize/resume`.
- Per-step warning logs (`Ecobee auth: step=<name>`) to make stuck
  redirect chains traceable without flipping `logger.ecobee` to
  debug.

### Removed
- All v0.2 MFA-OOB grant scaffolding (`MFARequiredError`,
  `MFAInvalidCodeError`, `MFAExpiredError`, `MFARateLimitedError`,
  `MFANotSupportedError`, `submit_mfa`, `list_mfa_authenticators`,
  `challenge_mfa`, `mfa_select_factor` / `mfa_code` config-flow
  steps as originally designed for ROPG-MFA). v0.3.0 attempted
  to handle MFA inline via the universal-login chain; v0.3.1 added
  the dedicated user-facing code-entry step that v0.2 had as
  scaffolding but couldn't use against ecobee's tenant.

### Why v0.3 supersedes v0.2
- Verified live 2026-05-03: Auth0 returns `unauthorized_client` for
  both `grant_type=mfa-otp` AND `grant_type=mfa-oob` against ecobee's
  web client_id. The web client config simply does not have those
  grants enabled. There is no per-user workaround — every 2FA-enabled
  account is unreachable via ROPG.
- The same client config DOES allow the standard authorization-code +
  PKCE flow against `/authorize`, and Auth0's hosted `/u/login/*` +
  `/u/mfa-*` pages handle MFA natively.

## [0.2.0] - 2026-05-01

### Added
- Auth0 standard MFA-OOB grant flow for ROPG (OTP + SMS factor types)
  per [Auth0 docs](https://auth0.com/docs/secure/multi-factor-authentication/authenticate-using-ropg-with-mfa).
- Recognition (and rejection with a clear error) of push-notification
  factors; users with only push configured must add an
  authenticator-app or SMS backup factor on ecobee.com.

### Notes
- This release was effectively non-functional in production because
  ecobee's web client_id rejects both `mfa-otp` and `mfa-oob` ROPG
  grants. v0.3.0 supersedes it with the universal-login flow.

## [0.1.0] - 2026-05-03

### Added
- Initial release. Read-only ecobee integration that bypasses the
  shut-down ecobee dev portal by using Resource Owner Password Grant
  (ROPG) against ecobee's Auth0 tenant with the public web
  `client_id` (`183eORFPlXyz9BbDZwqexHPBQoVjgadh`).
- Per-thermostat `climate.<name>` (read-only).
- Per-remote-sensor `sensor.ecobee_<room>_temperature`,
  `sensor.ecobee_<room>_humidity`, and
  `binary_sensor.ecobee_<room>_occupancy`.
- Per-thermostat `weather.<name>` and
  `sensor.ecobee_<name>_outdoor_temperature`.
- 88 tests covering ROPG happy path, refresh-token rotation, status-
  envelope mapping, coordinator scan-interval clamping, sensor + climate
  + binary-sensor entity behaviour.

### Known limitation
- 2FA-enabled accounts hard-stop with "MFA not supported". v0.2.0
  attempted to fix this; v0.3.x finally does.
