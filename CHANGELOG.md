# Changelog — Bosch Smart Home Camera MCP Server

## [v1.7.1] - 2026-07-14

Docs-only release: refreshed the sibling-repo version table in README's
Integration Comparison section (no functional changes).

## [v1.7.0] - 2026-07-11

### Added

Family-parity closeout (`docs/family-parity-plan.md` §2b) — 21 new tools closing the gap between the MCP server and the HA integration / Python CLI. 55 tools total.

- **Motion zones**: `bosch_camera_motion_zones_get` / `_set` / `_clear` — `GET/POST /v11/video_inputs/{id}/motion_sensitive_areas`, full-replace semantics. `_set` validates each zone via a Pydantic model (`0.0 ≤ x,y ≤ 1.0`, `0.0 < w,h ≤ 1.0`) before sending.
- **Privacy masks**: `bosch_camera_privacy_masks_get` / `_set` / `_clear` — `GET/POST /v11/video_inputs/{id}/privacy_masks`, same shape/validation as motion zones.
- **Automation rules**: `bosch_camera_rules_list` / `_add` / `_edit` / `_delete` — `GET/POST/PUT/DELETE /v11/video_inputs/{id}/rules`. `_edit` is read-modify-write (Bosch's PUT requires the full rule body). `start`/`end` are validated as 24h `HH:MM[:SS]` before being sent.
- **Camera sharing / friends**: `bosch_camera_friends_list` / `_invite` / `_share` / `_unshare` / `_remove` — `GET/POST/PUT/DELETE /v11/friends/*`. `_share` reads the friend's current shares first and merges the new camera in, rather than replacing the whole share list (Bosch's `PUT /friends/{id}/share` is full-replace on its side — a naive one-entry PUT would silently drop any camera the friend already had).
- **Firmware install**: `bosch_camera_firmware_status` / `_install` — `GET/PUT /v11/video_inputs/{id}/firmware`, same endpoint the official Bosch app's "Update now" button uses. Guards mirror the HA integration's `async_install_firmware`: refuses if an install is already in progress, or if there is no pending update.
- **Siren duration**: `bosch_camera_siren_duration_set` — `GET/PUT /v11/video_inputs/{id}/alarm_settings` (`alarmDelayInSeconds`, 10-300 s, Gen2 Indoor II only).
- **LED lighting schedule**: `bosch_camera_lighting_schedule_get` / `_set` — `GET/PUT /v11/video_inputs/{id}/lighting_options` (outdoor Eyes cameras).
- **Listen-audio intercom**: `bosch_camera_intercom_open` — opens a live LOCAL/REMOTE connection and returns an RTSPS URL streaming the camera's microphone audio to the caller. Listen-only: two-way talk (caller mic → camera speaker) is not exposed by Bosch's cloud API at all, matching the sister CLI's own limitation.

### CI

- Coverage gate: `pytest --cov-fail-under=96` in the `test` job (measured baseline; ratchets up as coverage improves).
- `pip-audit -r requirements.txt` (runtime dependencies only) — blocking, 0 vulnerabilities.
- `pylint` added to the `lint` job (10.00/10, with narrowly-scoped documented disables for this codebase's own architectural patterns — sys.path-injected sister-CLI bridge, `_get_session()` fan-out, error-boundary broad-catches).
- `codespell` added to the `lint` job.
- New workflows: `codeql.yml` (weekly + push/PR, `security-extended`), `secret-scan.yml` (gitleaks, full history), `dependency-review.yml` (`fail-on-severity: high` on `pyproject.toml`/`requirements-test.txt` changes).

### Fixed

- `_zone_dicts_to_models`/`_mask_dicts_to_models` now also catch a pydantic `ValidationError` (a present-but-wrong-typed API value, e.g. `"x": "bad"`), not just a missing key — previously a malformed-but-present zone/mask value crashed the whole read instead of being skipped as documented.

## [v1.6.0] - 2026-07-08

### Added

- **2 new tools: `bosch_camera_audio_detection_get` / `bosch_camera_audio_detection_set`** — glass-break and smoke/fire-alarm sound detection for Gen2 Audio-Plus cameras (`GET`/`PUT /v11/video_inputs/{id}/audioDetectionConfig`). Gated on `featureSupport.sound`; raises `hardware_unsupported` on Gen1 cameras. Read-modify-write on the setter — both `detectGlassBreak` and `detectFireAlarm` are always sent together on write, since Bosch's API silently resets whichever field is omitted. Cross-ported from HA integration v14.2.0 (`BoschGlassBreakDetectionSwitch` / `BoschFireAlarmDetectionSwitch`). 34 tools total.

### Fixed

- **Release workflow (`--generate-notes`) hardening** — fixed a crash and an awk shell-injection risk in the tag-triggered release workflow, and replaced a silent changelog-lookup fallback with an explicit failure so a missing changelog entry is caught instead of shipping an empty release body.

### Internal

- Trimmed padding/duplication in `docs/architecture.md` and corrected stale release-process notes.

## [v1.5.5] - 2026-06-29

- **`camera_events` resource:** use `eventType + eventTags` for correct event classification (fixes #36 Fix D)

## [v1.5.4] - 2026-06-18

### Fixed

- **Event timestamps no longer drop the timezone offset.** Bosch `/v11/events` timestamps are offset-bearing (e.g. `2026-06-18T06:06:30.499+02:00[Europe/Berlin]`). The server truncated them to the first 19 characters (`timestamp_iso` in the event list/resource and `last_event_at` in camera status/health), which discarded the `+02:00` offset — a consumer reading the field (documented as "ISO 8601 timestamp") would assume UTC and be off by the local offset (e.g. +2h in CEST). The server now strips only the trailing RFC-9557 `[zone]` suffix, preserving the explicit offset so the value is faithful, unambiguous ISO 8601. (Cross-version of the Home Assistant issue #34 fix.)

## [v1.5.3] - 2026-06-11

### Security

- **Pin Bosch cloud CA for MCP cloud session (CWE-295, GHSA-6qh5-x5m5-vj6v)** — the shared `requests.Session` used for all cloud OAuth and API calls now verifies TLS against the Bosch private CA bundle (bundled in the package) plus system roots, instead of accepting any certificate. Closes an adjacent-network MITM attack surface on OAuth tokens and bearer-credential exchanges. Local camera endpoints are unchanged (TOFU-pinned at first connect as before).

## [v1.5.2] - 2026-06-03

### Dependencies

- **Dropped `aiohttp` as a runtime dependency** — the v1.5.1 httpx migration removed its last usage in package code. It is now declared only under the `test` extra (a single regression test pins the original `aiohttp.DigestAuth` root cause via `hasattr`).
- **Security floors for transitive deps** — `mcp` pulls in `pyjwt` (`>=2.10.1`) and `starlette` (`>=0.27`), whose older releases carry advisories. Added explicit floors `pyjwt>=2.13.0` (PYSEC-2026-175/177/178/179) and `starlette>=1.0.1` (PYSEC-2026-161) so the resolver always picks patched versions. `pip-audit` is clean. Mirrored in `requirements.txt` / `requirements-test.txt`.

### Tests

- Fixed `test_fetch_rcp_lan_non_200_returns_none` — it still mocked `aiohttp.ClientSession` (a stack the helper no longer uses after v1.5.1), so it neither exercised the non-200 branch nor avoided a real network attempt to a non-documentation IP (`10.0.0.1`). Rewritten to mock `httpx.AsyncClient` and pinned to RFC-5737 `192.0.2.x`.

## [v1.5.1] - 2026-06-03

### Bug fixes

- **`_fetch_rcp_lan` (LAN RCP READ) never worked** — the helper used `aiohttp.DigestAuth`, which does not exist (aiohttp exposes only `DigestAuthMiddleware`, never a public `DigestAuth` class). The `auth = aiohttp.DigestAuth(...)` line raised `AttributeError` on every call; the broad `except Exception` swallowed it and returned `None`. As a result the two tools that depend on this READ path — **`bosch_camera_onvif_scopes`** (RCP 0x0a98) and **`bosch_camera_rcp_version`** (0xff00 / 0xff04) — always failed with a misleading `local_unavailable` "camera may be offline or credentials invalid" error, even on a reachable camera with valid LAN credentials. Now uses `httpx.DigestAuth`, mirroring the working sibling module `lan_rcp.py`. The unused `aiohttp` / `ssl` imports were dropped. Bug was MCP-only (Python CLI uses `requests.HTTPDigestAuth`, HA uses `urllib HTTPDigestAuthHandler`, both correct).

### Internal

- **Test coverage 83% → 98%** — added targeted tests for `cli_bridge.py` (67→93%), `server.py` (88→99%), `lan_rcp.py` (77→99%), `resources.py` (80→97%), `maintenance.py` (97→100%). The remaining uncovered lines are `return …  # unreachable` sentinels after `_raise_api_error` (which always raises) and a `__main__` guard.
- Two existing `_fetch_rcp_lan` tests in `test_mcp_features_2026_05_25.py` were mocking `aiohttp.ClientSession` — a library the helper never reached (the `DigestAuth` AttributeError fired first), so they only ever passed *because* of the bug. After the fix they would have made real network calls to a live camera IP; rewritten to mock `httpx.AsyncClient` and pinned to the RFC-5737 `192.0.2.0/24` documentation range.
- Cleaned pre-existing ruff findings in touched files (unused `br = _bridge()` assignments, missing `Optional` import, dead locals).
- **Test fixtures sanitized** — replaced real device identifiers (cloud IDs, MAC addresses, LAN IPs) across the test suite with fake values (RFC-5737 `192.0.2.x`, `aa:bb:cc` MACs, `AABBCCDD-…` UUIDs). Project rule: fixtures use fake IDs only.
- **CI bumped to Node-24-native action majors** — `actions/checkout` v4→v6, `actions/setup-python` v5→v6, `actions/upload-artifact` v4→v7, `actions/download-artifact` v4→v8, ahead of GitHub's 2026-06-16 forced-Node-24 cutover.

## [v1.5.0] - 2026-05-28

**11 new tools + 8 bugfixes from a live-camera audit (4 hardware units, all 4 generations exercised) plus a same-day follow-up that resolved the intrusion mode field-name mystery.**

### New tools

- **`bosch_camera_siren_trigger(camera, stop=False)`** — fires the indoor siren via `PUT /v11/video_inputs/{id}/panic_alarm` body `{"status":"ON"|"OFF"}`. Gated to `HOME_Eyes_Indoor` (Gen2 Indoor II — 75 dB integrated hardware). Gen1 INDOOR's documented `/acoustic_alarm` endpoint returns HTTP 404 in production and was deliberately excluded after live verification 2026-05-28. Companion to the HA integration's `BoschPanicAlarmSwitch`.
- **`bosch_camera_motion_get / _set`** — motion detection enable + sensitivity (`OFF | LOW | MEDIUM_LOW | MEDIUM_HIGH | HIGH | SUPER_HIGH`). API field is `motionAlarmConfiguration`, not `sensitivity` — both spellings accepted on read for forward-compat.
- **`bosch_camera_recording_get / _set`** — cloud-recording sound enable (`recordSound` boolean).
- **`bosch_camera_autofollow_get / _set`** — 360° Indoor auto-tracking. Hardware-gated to cameras with `pan_limit > 0`; rejected immediately with `hardware_unsupported` for outdoor + Gen2 indoor models. Body shape is `{"result": bool}` (yes, that's the Bosch API field name).
- **`bosch_camera_privacy_sound_get / _set`** — audible "privacy on/off" indicator chime override.
- **`bosch_camera_unread_get`** — reads `numberOfUnreadEvents` from the main `/v11/video_inputs` listing. The documented `/v11/video_inputs/{id}/unread_events_count` returns HTTP 404 in production.
- **`bosch_camera_health_check_all()`** — single-call bulk health summary (status + WiFi + privacy + last-event + unread for every configured camera). Per-camera errors captured non-fatally; eliminates 4× tool calls for dashboards.
- **`bosch_camera_token_status()`** — local JWT parse (no network call). Returns `{valid, expires_in_min, email}` from the cached bearer's claims.

### Bug fixes

- **`bosch_camera_light_set`** — gate no longer rejects valid Eyes Außenkamera II (`HOME_Eyes_Outdoor`) when the stale config-cache `has_light` flag is false. The hardware identity (`model == "HOME_Eyes_Outdoor"`) is now an automatic override for the gate because this model ALWAYS has spotlights. Surfaced 2026-05-28 against the user's Terrasse unit which had `has_light: false` in config but `featureSupport.light: true` from the live API.
- **`bosch_camera_privacy_set`** — eliminated the stale-state race. The PUT to `/privacy` returns 204 immediately but the Bosch cloud takes a moment to propagate the change to its read replicas. Previous code read `_build_status` directly after the PUT and frequently returned `privacy_mode: <old_value>` to the agent. Now polls `GET /v11/video_inputs/{id}` every 500 ms up to 5 s until `privacyMode` matches the requested state before returning.
- **`bosch_camera_wifi`** — the `signal_strength` percentage was computed by clamping `rssi` to the dBm range `[-100, -50]`. For Gen1 cameras the API returns `rssi` as a 0–100 quality value (already a percentage), not dBm — which collapsed to 100% always under the dBm clamp. Now branches on sign: ≥ 0 is treated as quality-already, < 0 is converted from dBm. The `rssi` field docstring updated accordingly.
- **`bosch_camera_intrusion_set`** — `distance` validation tightened to `1–8` (was `1–10`). The Bosch cloud rejects `distance > 8` with HTTP 400 `"must be less than or equal to 8"` — verified 2026-05-28 against FW 9.40.102. The iOS app's slider visually goes to 10 but the server still enforces ≤ 8.
- **`bosch_camera_intrusion_get / _set` and `bosch_camera_pan`** — HTTP 443 `sh:camera.in.privacy.mode` is now wrapped as a clean `MCPError(code="privacy_blocked")` instead of bubbling the raw Bosch error string to the agent. Adds a new `ErrorCode` literal `privacy_blocked` to `errors.py`.
- **`intrusion_set mode` resolved (same-day follow-up)** — earlier on 2026-05-28 the `mode` parameter was confirmed as silently dropped server-side. Root cause traced via cross-reference with the HA integration's `number.py:840` comment + `captures/api-findings.md §6.2`: Bosch's API field name is `detectionMode` (camelCase), not `mode`, and the value set is **not** `OFF|ACTIVE|SCHEDULED` — it is `PERSON | STANDARD | HIGH_SENSITIVITY | ZONES | ALL_MOTIONS | ONLY_HUMANS`. Both wrong assumptions cancelled out: Bosch happily accepted a request body with neither the right field name nor a recognised enum value, and dropped both quietly. The bridge now sends `detectionMode` and strips any legacy `mode` key from the cache; the docstring in `server.py` lists the correct value set.

### Internal

- `test_intrusion_set_distance_boundary_max` updated to assert `distance=8` as the valid upper boundary and `distance=9` as an out-of-range reject (post-release fixup, commit `1cb87df`).
- 30 new tests in `tests/test_v160_features.py` exercising every new tool's happy path + hardware gates + privacy-blocked wrap.

## [v1.4.0] - 2026-05-25

- 9 user-visible bugs fixed: live camera list, field mapping, Gen2 gate, error codes.
- Added `aiohttp>=3.9` to runtime deps.

## v1.3.6 (2026-05-24)

**Fix (9 items from live audit):**
- `bosch_camera_list`: camera list now always fetched live from cloud; stale cache no longer returned after token expiry
- `bosch_camera_status`: `hw_version` field now correctly distinguishes Gen1 vs Gen2 via `hardwareVersion` field
- UUID resolution: camera IDs resolved from `deviceId` field; previous mapping used wrong key
- `bosch_camera_events`: `eventType` / `timestamp` fields now read from correct JSON paths
- `bosch_camera_audio_get` / `bosch_camera_audio_set`: field names corrected from snake_case to camelCase (`micLevel` → `microphoneLevel` etc.) matching Bosch API
- `bosch_camera_intrusion_get` / `bosch_camera_intrusion_set`: Gen2 gate now checks `hardwareVersion` (not `cameraType`); Gen1 cameras correctly return `hardware_unsupported`
- Error codes: HTTP 4xx/5xx responses now surface Bosch error code string instead of raw status integer
- `bosch_camera_snapshot`: `timestamp` field in response now ISO-8601 string instead of raw epoch integer
- `requirements_test.txt`: mirrored all runtime deps from `manifest.json` so pytest no longer silently skips test files

---

## v1.3.4 (2026-05-20)

**New:** `bosch_camera_pan` — `preset` argument: `"home"` (0°) / `"left"` (-60°) / `"right"` (+60°) / `"back_left"` (-120°) / `"back_right"` (+120°). When `preset` is provided it overrides `angle`. Cross-port from HA v12.6.0 pan preset select entity + Python CLI `pan --preset` flag.

**Fix:** Transparent credential rotation on 401 for LAN-RCP tools (`bosch_camera_privacy_set`, `bosch_camera_light_set`, `bosch_camera_pan` with `prefer_local=True`). On 401 response the tool silently re-fetches Digest credentials from `bosch_config.json` and retries once. No user-visible API change; eliminates cold-start failures when cached Digest nonce has expired.

---

## v1.3.3 (2026-05-20)

Cross-port of HA v12.6.0 audio/intrusion/WiFi features.

**New tools (5):**
- `bosch_camera_audio_get` — GET microphone level, speaker level, intercom enabled flag (Gen2 only; `hardware_unsupported` for Gen1)
- `bosch_camera_audio_set(mic_level, speaker_level)` — PUT audio settings 0-100; read-then-write to preserve unmodified fields
- `bosch_camera_intrusion_get` — GET intrusion detection config: mode, sensitivity 0-7, distance 1-10 m (Gen2 only)
- `bosch_camera_intrusion_set(mode, sensitivity, distance)` — PUT intrusion config; at least one param required; range-validated
- `bosch_camera_wifi` — GET WiFi RSSI, SSID, derived signal_strength 0-100 % (no hardware gate)

**Tests:** 34 new tests in `tests/test_audio_intrusion_wifi.py` — range boundaries, hardware gates, partial-update semantics, RSSI→strength mapping.

**Tool count:** 11 → 16.

---

## v1.3.2 (2026-05-14)

**Fix:** `bosch_camera_light_set` — add `hardware_unsupported` gate for cameras without a controllable light (`has_light=False`). Previously called the cloud API unconditionally; now rejects Gen1 cameras and indoor Gen2 cameras immediately.

**Tests:** 3 new regression tests in `test_tools_integration.py`.

---

## v1.3.1 (2026-05-10)

**Fix:** LAN-fallback writes (`prefer_local=True` on privacy/light) now use HTTPS port 443 + HTTP Digest auth. Previous v1.3.0 used plain HTTP port 80 which Bosch cameras reject.

**Cross-port from:** HA v12.5.0 `lan_rcp.py` rewrite.

---

## v1.3.0 (2026-05-06)

**New:** `bosch_camera_lan_ping` — TCP reachability probe (port 443, 1.5 s timeout). Returns `{reachable, ip, latency_ms}`. Useful to verify LAN path before `prefer_local=True` writes during cloud outages.

**New:** `recommended_action` field on `bosch_camera_maintenance_status` — `"check_lan"` when state is `active`, `"wait"` when `scheduled`, `null` otherwise.

**New:** `prefer_local=True` option on `bosch_camera_privacy_set` and `bosch_camera_light_set` — attempts RCP-LAN write first, falls back to cloud.

---

## v1.2.0 (2026-04-28)

**New:** `bosch_camera_maintenance_status` — fetches current Bosch Smart Home cloud maintenance announcement from the official community RSS feed.

---

## v1.1.0 (2026-04-15)

**Change:** `bosch_camera_snapshot` is now LAN-only (HTTP Digest, no cloud roundtrip). Cloud snapshot path removed for privacy hardening.

**New:** `bosch_camera_stream_url` — returns LAN RTSPS URL without cloud relay.

---

## v1.0.0 (2026-03-30)

Initial PyPI release. Tools: list, status, snapshot, events, privacy_set, light_set, pan, notifications_set, stream_url. stdio + SSE + streamable-HTTP transports.
