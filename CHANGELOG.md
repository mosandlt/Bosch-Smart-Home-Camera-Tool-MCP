# Changelog — Bosch Smart Home Camera MCP Server

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
