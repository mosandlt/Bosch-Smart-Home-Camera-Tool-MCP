# Changelog — Bosch Smart Home Camera MCP Server

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

- Pre-existing test failure `test_intrusion_set_distance_boundary_max` (asserts `distance=10` is accepted) is now an intentional reject — needs test update next release.
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
