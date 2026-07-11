"""Bridge between MCP tools and the sister bosch_camera.py CLI.

Implementation strategy: Option C (interim sys.path injection).
The write-tool helpers (set_privacy_mode, set_light, set_pan,
set_notifications) replicate the cloud-API PUT calls extracted from the
corresponding cmd_* functions in bosch_camera.py.  We do NOT call the
cmd_* functions directly because they call print() and sys.exit() — they are
interactive CLI functions, not library calls.

bosch_camera.py is imported read-only; we never modify the sister repo.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import requests

from bosch_camera_mcp.cloud_ssl import bosch_cloud_ssl_context

logger = logging.getLogger("bosch_camera_mcp.bridge")

# ── Path injection ────────────────────────────────────────────────────────────

DEFAULT_CLI_PATH = (
    Path.home()
    / "Nextcloud"
    / "Priv"
    / "Claude"
    / "bosch kamera"
    / "Bosch-Smart-Home-Camera-Tool-Python"
)

CLOUD_API = "https://residential.cbs.boschsecurity.com"


def get_cli_path() -> Path:
    """Return path to the sister CLI checkout.

    Override by setting the BOSCH_CAMERA_CLI_PATH environment variable.
    """
    p = os.environ.get("BOSCH_CAMERA_CLI_PATH")
    if p:
        return Path(p).expanduser()
    return DEFAULT_CLI_PATH


def ensure_cli_importable() -> None:
    """Add the sister CLI directory to sys.path and verify the import works.

    Raises ImportError if the path does not contain bosch_camera.py.
    """
    cli = str(get_cli_path())
    if cli not in sys.path:
        sys.path.insert(0, cli)
    import bosch_camera  # noqa: F401 — fails fast if path wrong  # pylint: disable=unused-import,import-error


# ── Session + camera bootstrap ────────────────────────────────────────────────


def get_session_and_cameras(
    config_path: Optional[str] = None,
) -> tuple[dict[str, Any], requests.Session, dict[str, dict[str, Any]]]:
    """Load config, check/refresh token, build session, load camera registry.

    Args:
        config_path: Optional path to bosch_config.json.  When None the CLI's
                     default path (sibling to bosch_camera.py) is used.

    Returns:
        (cfg, session, cameras_dict) — cameras_dict keyed by camera name.

    Raises:
        MCPError("reauth_required") if the token is near-expiry AND silent
            renewal fails (no refresh_token or refresh fails).
        MCPError("auth_expired") if any request gets HTTP 401 even after one
            handle_401 retry — callers should wrap individual tool calls.
    """
    from bosch_camera_mcp.errors import MCPError

    ensure_cli_importable()

    import bosch_camera as bc

    # If a custom config path is requested we temporarily patch CONFIG_FILE.
    # The CLI stores it as a module-level constant derived from __file__ which
    # we cannot change after import, so we load the JSON manually when needed.
    if config_path:
        import json

        with open(config_path, encoding="utf-8") as fh:
            cfg: dict[str, Any] = json.load(fh)
        # Merge defaults so forward-compat keys are present
        bc._merge_defaults(cfg, bc.DEFAULT_CONFIG)
    else:
        cfg = bc.load_config()

    # Check token freshness. `_is_token_near_expiry` only fires within the
    # 300 s buffer BEFORE expiry — for a token already expired by >5 min the
    # call returns False, so we also need `_is_token_expired` to catch
    # long-expired tokens (incident 2026-05-24: 34-day-old token never refreshed).
    token = cfg["account"].get("bearer_token", "").strip()
    needs_refresh = (
        not token or bc._is_token_near_expiry(token, buffer_secs=300) or bc._is_token_expired(token)
    )
    if needs_refresh:
        logger.warning("Token missing or near-expiry (< 5 min). Attempting silent renewal.")
        # Try silent renewal via refresh_token
        refresh = cfg["account"].get("refresh_token", "").strip()
        renewed = False
        if refresh:
            try:
                from get_token import _do_refresh

                tokens = _do_refresh(refresh)
                if tokens:
                    cfg["account"]["bearer_token"] = tokens.get("access_token", "")
                    cfg["account"]["refresh_token"] = tokens.get("refresh_token", refresh)
                    bc.save_config(cfg)
                    token = cfg["account"]["bearer_token"]
                    renewed = True
            except ImportError:
                logger.debug("get_token.py not available; skipping silent renewal")
            except Exception as exc:
                logger.warning("Silent renewal failed: %s", exc)

        if not renewed:
            if not token:
                raise MCPError(
                    code="reauth_required",
                    detail=(
                        "No bearer token in config. "
                        "Run `python3 bosch_camera.py token browser` to authenticate."
                    ),
                )
            # Token exists but is near-expiry and renewal failed — log warning
            # and proceed; the API call may still succeed if expiry is marginal.
            logger.warning("Token near-expiry and renewal failed — proceeding with existing token.")

    session = bc.make_session(token)
    # CWE-295 / GHSA-6qh5-x5m5-vj6v: the CLI's make_session() sets
    # session.verify = False for cloud calls (private Bosch PKI not in any
    # public trust store).  Override defensively here so that MCP cloud TLS
    # verification is guaranteed regardless of the installed CLI version.
    # requests accepts an ssl.SSLContext as the `verify` argument.
    # LOCAL httpx/CERT_NONE sites (lan_rcp.py, server.py:1147) are intentionally
    # excluded — they are LAN self-signed cameras, TOFU-pinned.
    session.verify = bosch_cloud_ssl_context()
    cached = cfg.get("cameras", {})

    # Always refresh from /v11/video_inputs — the cloud is authoritative for
    # camera names, hardware version, MAC etc. Pre-2026-05-24 we only hit the
    # API when `cached` was empty, which caused stale local config to mask
    # camera renames and new additions. Local-only fields (local_ip / creds)
    # are preserved by ID across refresh.
    cameras: dict[str, dict[str, Any]] = {}
    try:
        r = session.get(f"{CLOUD_API}/v11/video_inputs", timeout=15)
        if r.status_code == 401:
            raise MCPError(
                code="reauth_required",
                detail=(
                    "API returned 401. Token expired. "
                    "Run `python3 bosch_camera.py token browser` to re-authenticate."
                ),
            )
        r.raise_for_status()
        # Preserve local-only fields across refresh, keyed by cam id.
        by_id = {info.get("id"): info for info in cached.values() if info.get("id")}
        for cam in r.json():
            cam_id = cam.get("id", "")
            name = cam.get("title", cam_id or "unknown")
            prev = by_id.get(cam_id, {})
            cameras[name] = {
                "id": cam_id,
                "name": name,
                "model": cam.get("hardwareVersion", "CAMERA"),
                "firmware": cam.get("firmwareVersion", ""),
                "mac": cam.get("macAddress", ""),
                "download_folder": prev.get("download_folder", name),
                "local_ip": prev.get("local_ip", ""),
                "local_username": prev.get("local_username", ""),
                "local_password": prev.get("local_password", ""),
            }
        cfg["cameras"] = cameras
    except MCPError:
        raise
    except Exception as exc:
        # Cloud unreachable — fall back to the cached config so that LAN-only
        # tools (lan_ping with explicit IP, snapshot via local creds) still work.
        if cached:
            logger.warning("Cloud refresh failed (%s); using cached cameras", exc)
            cameras = cached
        else:
            raise MCPError(
                code="api_unreachable",
                detail=f"Failed to discover cameras: {exc}",
            ) from exc

    return cfg, session, cameras


def _resolve_cam(cameras: dict[str, dict[str, Any]], key: str) -> tuple[str, dict[str, Any]]:
    """Resolve a partial camera name → (canonical_name, cam_info).

    Raises MCPError("unknown_camera") when no unambiguous match is found.
    """
    from bosch_camera_mcp.errors import MCPError

    if key in cameras:
        return key, cameras[key]
    # UUID lookup — Bosch ids look like AABBCCDD-1111-4111-8111-AABBCCDD1111.
    # Case-insensitive because Bosch sometimes hands them back lowercased.
    key_lower = key.lower()
    for cam_name, info in cameras.items():
        if (info.get("id") or "").lower() == key_lower:
            return cam_name, info
    matches = {k: v for k, v in cameras.items() if key_lower in k.lower()}
    if len(matches) == 1:
        name = next(iter(matches))
        return name, matches[name]
    if len(matches) > 1:
        raise MCPError(
            code="unknown_camera",
            detail=f"Ambiguous camera name {key!r}: matches {list(matches.keys())}",
            camera=key,
        )
    raise MCPError(
        code="unknown_camera",
        detail=f"Camera {key!r} not found. Known cameras: {list(cameras.keys())}",
        camera=key,
    )


# ── Write helpers — extracted PUT logic from cmd_privacy / cmd_light / cmd_pan / cmd_notifications
# These replicate the cloud API calls without the print()/sys.exit() CLI behaviour.
# API endpoints discovered from the docstrings in bosch_camera.py.


def set_privacy_mode(session: requests.Session, cam_id: str, enabled: bool) -> bool:
    """PUT /v11/video_inputs/{cam_id}/privacy → enable or disable privacy mode.

    Returns True on success (HTTP 200/201/204), raises on failure.
    Extracted from cmd_privacy in bosch_camera.py.
    API: PUT /v11/video_inputs/{id}/privacy
         Body: {"privacyMode": "ON"/"OFF", "durationInSeconds": null}
         Response: HTTP 204 on success.
    """
    new_state = "ON" if enabled else "OFF"
    body = {"privacyMode": new_state, "durationInSeconds": None}
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_privacy_mode({cam_id}, {enabled})")
    return False  # unreachable — _raise_api_error always raises


def set_light(session: requests.Session, cam_id: str, enabled: bool) -> bool:
    """PUT /v11/video_inputs/{cam_id}/lighting_override → all lights on/off.

    Returns True on success.
    Extracted from cmd_light in bosch_camera.py.
    API: PUT /v11/video_inputs/{id}/lighting_override
         ON:  {"frontLightOn": true, "wallwasherOn": true, "frontLightIntensity": 1.0}
         OFF: {"frontLightOn": false, "wallwasherOn": false, "frontLightIntensity": 0.0}
         Response: HTTP 204 on success.
    """
    body = (
        {"frontLightOn": True, "wallwasherOn": True, "frontLightIntensity": 1.0}
        if enabled
        else {"frontLightOn": False, "wallwasherOn": False, "frontLightIntensity": 0.0}
    )
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_override",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_light({cam_id}, {enabled})")
    return False


# Canonical pan preset angles — shared with Python CLI and ioBroker.
# home=0° / left=-60° / right=+60° / back-left=-120° / back-right=+120°
PAN_PRESET_MAP: dict[str, int] = {
    "home": 0,
    "left": -60,
    "right": 60,
    "back-left": -120,
    "back-right": 120,
}


def set_pan(session: requests.Session, cam_id: str, direction: str) -> dict[str, Any]:
    """PUT /v11/video_inputs/{cam_id}/pan → move camera to target position.

    Args:
        direction: named preset (home|left|right|back-left|back-right),
                   legacy alias (center), or str(<-120..120>)

    Returns the API response JSON dict.
    Extracted from cmd_pan in bosch_camera.py.
    API: PUT /v11/video_inputs/{id}/pan
         Body: {"absolutePosition": <int>}
         Response: {"currentAbsolutePosition": ..., "estimatedTimeToCompletion": ...}
    """
    from bosch_camera_mcp.errors import MCPError

    # Fetch pan limits first
    gr = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/pan", timeout=10)
    if gr.status_code != 200:
        raise MCPError(
            code="api_unreachable",
            detail=f"Could not fetch pan state: HTTP {gr.status_code}",
            camera=cam_id,
        )
    pan_data = gr.json()
    limit = pan_data.get("panLimit", 120)

    direction_lower = direction.strip().lower()
    # Named presets take priority; legacy "center" alias kept for back-compat
    legacy_map: dict[str, int] = {"center": 0}
    if direction_lower in PAN_PRESET_MAP:
        target = PAN_PRESET_MAP[direction_lower]
    elif direction_lower in legacy_map:
        target = legacy_map[direction_lower]
    else:
        try:
            target = int(direction)
        except ValueError as exc:
            raise MCPError(
                code="permission_denied",
                detail=(
                    f"Invalid pan direction {direction!r}. "
                    "Use home|left|right|back-left|back-right or an integer in [-120, 120]."
                ),
                camera=cam_id,
            ) from exc
        if not -limit <= target <= limit:
            raise MCPError(
                code="permission_denied",
                detail=f"Pan position {target} out of range (-{limit} to +{limit}).",
                camera=cam_id,
            )

    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/pan",
        json={"absolutePosition": target},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code == 200:
        return r.json()  # type: ignore[no-any-return]
    _raise_api_error(r, f"set_pan({cam_id}, {direction})")
    return {}  # unreachable


def set_notifications(session: requests.Session, cam_id: str, enabled: bool) -> bool:
    """PUT /v11/video_inputs/{cam_id}/enable_notifications → on or off.

    Returns True on success.
    Extracted from cmd_notifications in bosch_camera.py.
    API: PUT /v11/video_inputs/{id}/enable_notifications
         Body: {"enabledNotificationsStatus": "FOLLOW_CAMERA_SCHEDULE"/"ALWAYS_OFF"}
         Response: HTTP 204 on success.
    """
    new_status = "FOLLOW_CAMERA_SCHEDULE" if enabled else "ALWAYS_OFF"
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/enable_notifications",
        json={"enabledNotificationsStatus": new_status},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_notifications({cam_id}, {enabled})")
    return False


def get_audio(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/audio → {microphoneLevel, speakerLevel, ...}.

    Returns the raw JSON dict from the API.
    Endpoint documented from HA v12.6.0 coordinator._audio_cache.
    """
    r = session.get(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/audio",
        timeout=10,
    )
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_audio({cam_id})")
    return {}  # unreachable


def set_audio(
    session: requests.Session,
    cam_id: str,
    mic_level: Optional[int] = None,
    speaker_level: Optional[int] = None,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/audio — update mic/speaker levels.

    Fetches current audio state first, merges the requested fields, then
    sends the full body (API requires complete payload).
    mic_level and speaker_level must be in range 0-100.
    Extracted from HA v12.6.0 BoschMicrophoneLevelNumber + BoschSpeakerLevelNumber.
    """
    # Fetch current state so we can preserve unmodified fields
    current = get_audio(session, cam_id)
    if mic_level is not None:
        current["microphoneLevel"] = mic_level
    if speaker_level is not None:
        # Bosch API uses camelCase. PascalCase "SpeakerLevel" is silently
        # dropped server-side (incident 2026-05-24).
        current["speakerLevel"] = speaker_level
        current.pop("SpeakerLevel", None)
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/audio",
        json=current,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_audio({cam_id})")
    return False  # unreachable


def get_intrusion_config(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/intrusionDetectionConfig.

    Returns raw JSON dict with at least {mode, sensitivity, distance}.
    Endpoint documented from HA v12.6.0 coordinator._intrusion_config_cache.
    """
    r = session.get(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/intrusionDetectionConfig",
        timeout=10,
    )
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_intrusion_config({cam_id})")
    return {}  # unreachable


def get_audio_detection_config(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/audioDetectionConfig.

    Returns raw JSON dict with at least {detectGlassBreak, detectFireAlarm}.
    Gen2 Audio-Plus-only feature — cross-ported from HA integration v14.2.0
    (switch.py BoschGlassBreakDetectionSwitch / BoschFireAlarmDetectionSwitch).
    """
    r = session.get(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/audioDetectionConfig",
        timeout=10,
    )
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_audio_detection_config({cam_id})")
    return {}  # unreachable


def set_audio_detection_config(
    session: requests.Session,
    cam_id: str,
    glass_break: Optional[bool] = None,
    fire_alarm: Optional[bool] = None,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/audioDetectionConfig — partial update.

    Fetches current config first, merges requested fields, then PUT the full
    body. Both ``detectGlassBreak`` and ``detectFireAlarm`` must always be
    sent together — sending only one silently resets the other one server-side
    (same read-modify-write requirement as intrusionDetectionConfig).
    """
    current = get_audio_detection_config(session, cam_id)
    if glass_break is not None:
        current["detectGlassBreak"] = glass_break
    if fire_alarm is not None:
        current["detectFireAlarm"] = fire_alarm
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/audioDetectionConfig",
        json=current,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_audio_detection_config({cam_id})")
    return False  # unreachable


def trigger_siren(
    session: requests.Session,
    cam_id: str,
    model: str,
    stop: bool = False,
) -> bool:
    """Trigger or stop the indoor siren — endpoint depends on the camera model.

    - Gen1 360° (model "INDOOR"): PUT /v11/video_inputs/{id}/acoustic_alarm
      Body: {"enabled": <bool>}.  Returns HTTP 442 on outdoor models.
    - Gen2 Indoor II (model "HOME_Eyes_Indoor"): PUT /v11/video_inputs/{id}/panic_alarm
      Body: {"status": "ON"|"OFF"}.  Used by HA integration's BoschPanicAlarmSwitch.

    Returns True on HTTP 2xx, raises on failure.
    """
    # Gen1's /acoustic_alarm endpoint returns HTTP 404 in production (verified
    # 2026-05-28 — both Gen1 INDOOR and OUTDOOR). The correct Gen1 siren endpoint
    # has not been identified yet; for now only Gen2 Indoor II is supported.
    if model == "HOME_Eyes_Indoor":
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/panic_alarm"
        body: dict[str, Any] = {"status": "OFF" if stop else "ON"}
    else:
        raise ValueError(
            f"trigger_siren: unsupported camera model '{model}'. "
            "Only HOME_Eyes_Indoor (Gen2 Indoor II) has a working siren endpoint via /panic_alarm. "
            "Gen1 INDOOR's documented /acoustic_alarm endpoint returns HTTP 404."
        )
    r = session.put(url, json=body, headers={"Content-Type": "application/json"}, timeout=10)
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"trigger_siren({cam_id}, model={model}, stop={stop})")
    return False  # unreachable


def set_intrusion_config(
    session: requests.Session,
    cam_id: str,
    mode: Optional[str] = None,
    sensitivity: Optional[int] = None,
    distance: Optional[int] = None,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/intrusionDetectionConfig — partial update.

    Fetches current config first, merges requested fields, then PUT full body.

    **detectionMode** (`mode` parameter for backwards-compat — both accepted):
    Bosch's actual API field is ``detectionMode`` (camelCase), not ``mode``.
    Previous versions sent ``"mode"`` and Bosch silently dropped the field
    (verified 2026-05-28 against FW 9.40.102 — sensitivity + distance writes
    succeeded, mode field was ignored). Source: HA-integration ``api-findings.md``
    §6.2 + the captured iOS app PUT body.

    Valid values: ``PERSON | STANDARD | HIGH_SENSITIVITY | ZONES |
    ALL_MOTIONS | ONLY_HUMANS`` (NOT ``OFF|ACTIVE|SCHEDULED`` as previously
    misdocumented). To "turn off" intrusion detection set ``enabled`` to
    ``false`` instead — but our partial-update API doesn't expose that yet.

    sensitivity: 0-7 (HA v12.6.0 FW 9.40+ confirmed range).
    distance: 1-8 (Bosch API rejects 9+ with HTTP 400 "must be less than or equal
    to 8" — verified 2026-05-28 against FW 9.40.102. The iOS app slider goes to
    10 visually but the server still enforces ≤ 8).
    Extracted from HA v12.6.0 BoschIntrusionSensitivityNumber.
    """
    current = get_intrusion_config(session, cam_id)
    if mode is not None:
        # Bosch's field is detectionMode (camelCase). Strip any legacy "mode"
        # key the cache might have so the PUT body only carries the correct one.
        current["detectionMode"] = mode
        current.pop("mode", None)
    if sensitivity is not None:
        current["sensitivity"] = sensitivity
    if distance is not None:
        current["distance"] = distance
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/intrusionDetectionConfig",
        json=current,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_intrusion_config({cam_id})")
    return False  # unreachable


def get_wifi_info(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/wifiinfo.

    Returns {rssi, ssid, ...} with a synthesised signal_strength (0-100).
    RSSI → signal_strength mapping: rssi >= -50 → 100, <= -100 → 0, linear in between.
    Endpoint documented from HA v12.6.0 coordinator._wifiinfo_cache.
    """
    r = session.get(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/wifiinfo",
        timeout=10,
    )
    if r.status_code == 200:
        data = dict(r.json())
        rssi: Optional[int] = data.get("rssi") or data.get("signalStrength")
        if rssi is not None:
            rssi_int = int(rssi)
            if rssi_int >= 0:
                # Bosch firmware on some cams returns a 0–100 quality value here,
                # not real dBm. Pass through as quality; the dBm field stays raw.
                data["signal_strength"] = max(0, min(100, rssi_int))
            else:
                # Real RSSI dBm — map to 0–100% (>= -50 dBm = 100, <= -100 dBm = 0).
                clamped = max(-100, min(-50, rssi_int))
                data["signal_strength"] = int(round((clamped + 100) * 2))
        return data
    _raise_api_error(r, f"get_wifi_info({cam_id})")
    return {}  # unreachable


def get_motion_config(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/motion.

    Returns {enabled, motionAlarmConfiguration (= sensitivity string), ...}.
    Sensitivity values: OFF | LOW | MEDIUM_LOW | MEDIUM_HIGH | HIGH | SUPER_HIGH.
    API reference: cmd_motion in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion", timeout=10)
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_motion_config({cam_id})")
    return {}  # unreachable


def set_motion_config(
    session: requests.Session,
    cam_id: str,
    enabled: Optional[bool] = None,
    sensitivity: Optional[str] = None,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/motion — partial update.

    Fetches current config first, merges requested fields, then PUT full body.
    Body: {"enabled": bool, "motionAlarmConfiguration": sensitivity_str}
    Sensitivity values: OFF | LOW | MEDIUM_LOW | MEDIUM_HIGH | HIGH | SUPER_HIGH.
    Note: setting sensitivity implicitly enables motion (mirrors CLI behavior).
    API reference: cmd_motion in bosch_camera.py.
    """
    current = get_motion_config(session, cam_id)
    if enabled is not None:
        current["enabled"] = enabled
    if sensitivity is not None:
        current["motionAlarmConfiguration"] = sensitivity
        # Setting sensitivity implicitly enables motion (mirrors CLI behavior)
        current["enabled"] = True
    # Re-apply explicit enabled=False even after sensitivity was set
    if enabled is False:
        current["enabled"] = False
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion",
        json=current,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_motion_config({cam_id})")
    return False  # unreachable


def get_recording_options(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/recording_options.

    Returns {recordSound: bool, ...}.
    API reference: cmd_recording in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/recording_options", timeout=10)
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_recording_options({cam_id})")
    return {}  # unreachable


def set_recording_options(
    session: requests.Session,
    cam_id: str,
    sound_on: bool,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/recording_options.

    Body: {"recordSound": bool}.
    API reference: cmd_recording in bosch_camera.py.
    """
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/recording_options",
        json={"recordSound": sound_on},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_recording_options({cam_id})")
    return False  # unreachable


def get_autofollow(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/autofollow.

    Returns {result: bool}.
    Only available for cameras with featureSupport.panLimit > 0 (CAMERA_360).
    API reference: cmd_autofollow in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/autofollow", timeout=10)
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_autofollow({cam_id})")
    return {}  # unreachable


def set_autofollow(
    session: requests.Session,
    cam_id: str,
    enabled: bool,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/autofollow.

    Body: {"result": bool}.
    Response: HTTP 204 on success.
    API reference: cmd_autofollow in bosch_camera.py.
    """
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/autofollow",
        json={"result": enabled},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_autofollow({cam_id})")
    return False  # unreachable


def get_lighting_schedule(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/lighting_options.

    Returns {scheduleStatus, generalLightOnTime, generalLightOffTime,
    darknessThreshold, lightOnMotion, lightOnMotionFollowUpTimeSeconds,
    frontIlluminatorInGeneralLightOn, frontIlluminatorGeneralLightIntensity,
    wallwasherInGeneralLightOn}.

    Only available on outdoor (Eyes) cameras with LED light. HTTP 442 = not
    supported on this camera model. HTTP 444 = camera offline.
    API reference: cmd_lighting_schedule in bosch_camera.py.
    """
    from bosch_camera_mcp.errors import MCPError

    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_options", timeout=10)
    if r.status_code == 200:
        return dict(r.json())
    if r.status_code == 442:
        raise MCPError(
            code="hardware_unsupported",
            detail=f"Lighting schedule not supported on camera model (HTTP 442, cam {cam_id}).",
        )
    if r.status_code == 444:
        raise MCPError(
            code="api_unreachable",
            detail=f"Camera is offline (HTTP 444, cam {cam_id}). Lighting schedule unavailable.",
        )
    _raise_api_error(r, f"get_lighting_schedule({cam_id})")
    return {}  # unreachable


def set_lighting_schedule(
    session: requests.Session,
    cam_id: str,
    on_time: Optional[str] = None,
    off_time: Optional[str] = None,
    light_on_motion: Optional[bool] = None,
    darkness_threshold: Optional[float] = None,
) -> dict[str, Any]:
    """PUT /v11/video_inputs/{cam_id}/lighting_options — partial update.

    Fetches current schedule first, merges requested fields, forces
    scheduleStatus="FOLLOW_SCHEDULE" (matches CLI behavior — writing a
    schedule implies following it), then sends the full body.

    on_time/off_time: "HH:MM" or "HH:MM:SS" (":00" appended if seconds missing).
    darkness_threshold: 0.0-1.0.
    HTTP 442 = not supported on this camera model. HTTP 444 = camera offline.
    API reference: cmd_lighting_schedule in bosch_camera.py.
    """
    current = get_lighting_schedule(session, cam_id)
    if on_time is not None:
        current["generalLightOnTime"] = on_time if len(on_time.split(":")) == 3 else f"{on_time}:00"
    if off_time is not None:
        current["generalLightOffTime"] = (
            off_time if len(off_time.split(":")) == 3 else f"{off_time}:00"
        )
    if light_on_motion is not None:
        current["lightOnMotion"] = light_on_motion
    if darkness_threshold is not None:
        current["darknessThreshold"] = float(darkness_threshold)
    current["scheduleStatus"] = "FOLLOW_SCHEDULE"

    from bosch_camera_mcp.errors import MCPError

    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_options",
        json=current,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 204):
        return get_lighting_schedule(session, cam_id)
    if r.status_code == 442:
        raise MCPError(
            code="hardware_unsupported",
            detail=f"Lighting schedule not supported on camera model (HTTP 442, cam {cam_id}).",
        )
    if r.status_code == 444:
        raise MCPError(
            code="api_unreachable",
            detail=f"Camera is offline (HTTP 444, cam {cam_id}). Lighting schedule unavailable.",
        )
    _raise_api_error(r, f"set_lighting_schedule({cam_id})")
    return {}  # unreachable


def get_alarm_settings(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/alarm_settings.

    Returns {alarmDelayInSeconds, ...}. Gen2 Indoor II (HOME_Eyes_Indoor) only.
    API reference: cmd_siren --set-duration in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/alarm_settings", timeout=10)
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_alarm_settings({cam_id})")
    return {}  # unreachable


def set_siren_duration(session: requests.Session, cam_id: str, duration_secs: int) -> dict[str, Any]:
    """PUT /v11/video_inputs/{cam_id}/alarm_settings — set siren duration.

    Fetches current alarm_settings first (best-effort — a fetch failure still
    proceeds with just the new field, mirroring CLI behavior), merges
    alarmDelayInSeconds, then PUTs the full body. Range 10-300 seconds
    (validated by the caller before this is invoked).
    Gen2 Indoor II (HOME_Eyes_Indoor) only. HTTP 443 = camera in privacy mode.
    API reference: cmd_siren --set-duration in bosch_camera.py.
    """
    try:
        current = get_alarm_settings(session, cam_id)
    except Exception:
        current = {}
    current["alarmDelayInSeconds"] = duration_secs
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/alarm_settings",
        json=current,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return {"alarmDelayInSeconds": duration_secs}
    _raise_api_error(r, f"set_siren_duration({cam_id})")
    return {}  # unreachable


LIVE_TYPE_CANDIDATES = ["REMOTE", "LOCAL"]


def open_intercom_session(
    session: requests.Session,
    cam_id: str,
    duration: int = 60,
    speaker_level: Optional[int] = None,
) -> dict[str, Any]:
    """Open a listen-audio session tunnel to a camera (cloud proxy).

    Mirrors cmd_intercom in bosch_camera.py:
      1. (optional) full-body PUT /v11/video_inputs/{id}/audio to set speakerLevel,
         preserving audioEnabled + microphoneLevel from the current state.
      2. PUT /v11/video_inputs/{id}/connection with type in LIVE_TYPE_CANDIDATES
         (REMOTE then LOCAL) to obtain a proxy URL.
      3. Build an rtsps:// URL with enableaudio=1 for the audio-only stream.

    NOTE: this is a **listen-only** audio tunnel (camera → caller). True
    two-way talk (microphone → camera speaker) requires a direct media tunnel
    that is not yet exposed via the cloud API (same limitation as the CLI's
    own `intercom` command, verified 2026-05-xx).

    Returns {rtsps_url, duration, speaker_level_set}. Raises MCPError
    (api_unreachable) if no connection type succeeds.
    """
    from bosch_camera_mcp.errors import MCPError

    speaker_level_set: Optional[int] = None
    if speaker_level is not None:
        try:
            cur_audio = get_audio(session, cam_id)
            cur_enabled = cur_audio.get("audioEnabled", cur_audio.get("enabled", True))
            cur_mic = cur_audio.get("microphoneLevel", cur_audio.get("MicrophoneLevel", 50))
            r = session.put(
                f"{CLOUD_API}/v11/video_inputs/{cam_id}/audio",
                json={
                    "audioEnabled": cur_enabled,
                    "microphoneLevel": cur_mic,
                    "speakerLevel": speaker_level,
                },
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if r.status_code in (200, 201, 204):
                speaker_level_set = speaker_level
        except Exception:
            # Best-effort — a failed speaker-level write shouldn't block the
            # audio session itself (matches CLI's own warn-and-continue).
            speaker_level_set = None

    conn_data: Optional[dict[str, Any]] = None
    for conn_type in LIVE_TYPE_CANDIDATES:
        try:
            r = session.put(
                f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection",
                json={"type": conn_type, "highQualityVideo": False},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if r.status_code in (200, 201):
                conn_data = r.json()
                break
        except Exception:
            continue

    if not conn_data or not conn_data.get("urls"):
        raise MCPError(
            code="api_unreachable",
            detail=f"Could not open live connection for intercom (cam {cam_id}).",
        )

    proxy_url = conn_data["urls"][0]
    proxy_host = proxy_url.split("/")[0].replace(":42090", "")
    proxy_hash = proxy_url.split("/", 1)[1] if "/" in proxy_url else ""
    rtsps_url = (
        f"rtsps://{proxy_host}:443/{proxy_hash}"
        f"/rtsp_tunnel?inst=2&enableaudio=1&fmtp=1&maxSessionDuration={duration}"
    )

    return {
        "rtsps_url": rtsps_url,
        "duration": duration,
        "speaker_level_set": speaker_level_set,
    }


def get_privacy_sound(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/privacy_sound_override.

    Returns {result: bool} — True means audible indicator is on.
    API reference: cmd_privacy_sound in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy_sound_override", timeout=10)
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_privacy_sound({cam_id})")
    return {}  # unreachable


def set_privacy_sound(
    session: requests.Session,
    cam_id: str,
    enabled: bool,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/privacy_sound_override.

    Body: {"result": bool}.
    API reference: cmd_privacy_sound in bosch_camera.py.
    """
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy_sound_override",
        json={"result": enabled},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"set_privacy_sound({cam_id})")
    return False  # unreachable


def get_motion_zones(session: requests.Session, cam_id: str) -> list[dict[str, Any]]:
    """GET /v11/video_inputs/{cam_id}/motion_sensitive_areas.

    Returns a list of {x, y, w, h} normalized (0.0-1.0) zone rectangles.
    Extracted from cmd_zones in bosch_camera.py.
    """
    r = session.get(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion_sensitive_areas", timeout=10
    )
    if r.status_code == 200:
        result = r.json()
        return list(result) if isinstance(result, list) else []
    _raise_api_error(r, f"get_motion_zones({cam_id})")
    return []  # unreachable


def set_motion_zones(
    session: requests.Session, cam_id: str, zones: list[dict[str, float]]
) -> list[dict[str, Any]]:
    """POST /v11/video_inputs/{cam_id}/motion_sensitive_areas — replace all zones.

    Body: the full list of {x, y, w, h} rectangles (full replace, not a merge —
    same semantics as the CLI's ``zones set``). Pass an empty list to clear all
    zones (same as the CLI's ``zones clear``). Returns the zones just set.
    Extracted from cmd_zones in bosch_camera.py.
    """
    r = session.post(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion_sensitive_areas",
        json=zones,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 204):
        return list(zones)
    _raise_api_error(r, f"set_motion_zones({cam_id})")
    return []  # unreachable


def get_privacy_masks(session: requests.Session, cam_id: str) -> list[dict[str, Any]]:
    """GET /v11/video_inputs/{cam_id}/privacy_masks.

    Returns a list of {x, y, w, h} normalized (0.0-1.0) mask rectangles.
    Extracted from cmd_privacy_masks in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy_masks", timeout=10)
    if r.status_code == 200:
        result = r.json()
        return list(result) if isinstance(result, list) else []
    _raise_api_error(r, f"get_privacy_masks({cam_id})")
    return []  # unreachable


def set_privacy_masks(
    session: requests.Session, cam_id: str, masks: list[dict[str, float]]
) -> list[dict[str, Any]]:
    """POST /v11/video_inputs/{cam_id}/privacy_masks — replace all masks.

    Body: the full list of {x, y, w, h} rectangles (full replace). Pass an
    empty list to clear all masks. Returns the masks just set.
    Extracted from cmd_privacy_masks in bosch_camera.py.
    """
    r = session.post(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy_masks",
        json=masks,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 204):
        return list(masks)
    _raise_api_error(r, f"set_privacy_masks({cam_id})")
    return []  # unreachable


def list_rules(session: requests.Session, cam_id: str) -> list[dict[str, Any]]:
    """GET /v11/video_inputs/{cam_id}/rules — list automation (schedule) rules.

    Extracted from cmd_rules in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules", timeout=10)
    if r.status_code == 200:
        result = r.json()
        return list(result) if isinstance(result, list) else []
    _raise_api_error(r, f"list_rules({cam_id})")
    return []  # unreachable


def add_rule(
    session: requests.Session,
    cam_id: str,
    name: str,
    start: str,
    end: str,
    weekdays: list[int],
) -> dict[str, Any]:
    """POST /v11/video_inputs/{cam_id}/rules — create a new schedule rule.

    ``start``/``end`` are ``HH:MM`` (seconds appended automatically).
    ``weekdays`` are 0=Mon..6=Sun. Returns the created rule (includes its id).
    Extracted from cmd_rules(add) in bosch_camera.py.
    """
    body = {
        "id": None,
        "name": name,
        "isActive": True,
        "startTime": f"{start}:00",
        "endTime": f"{end}:00",
        "weekdays": weekdays,
    }
    r = session.post(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201):
        return dict(r.json())
    _raise_api_error(r, f"add_rule({cam_id})")
    return {}  # unreachable


def edit_rule(
    session: requests.Session,
    cam_id: str,
    rule_id: str,
    active: Optional[bool] = None,
    name: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    weekdays: Optional[list[int]] = None,
) -> dict[str, Any]:
    """PUT /v11/video_inputs/{cam_id}/rules/{rule_id} — partial update.

    Fetches the current rule first (the endpoint requires the full rule body,
    not a partial patch — same read-modify-write requirement as the other
    config PUTs in this module), merges requested fields, then PUTs it back.
    Raises MCPError(code="unknown_camera"... no — raises a plain ValueError if
    ``rule_id`` is not found among this camera's rules (caller wraps as needed).
    Extracted from cmd_rules(edit) in bosch_camera.py.
    """
    rules = list_rules(session, cam_id)
    target = next((rl for rl in rules if rl.get("id") == rule_id), None)
    if target is None:
        raise ValueError(f"Rule id {rule_id!r} not found for camera {cam_id!r}")

    if active is not None:
        target["isActive"] = active
    if name is not None:
        target["name"] = name
    if start is not None:
        target["startTime"] = start if len(start.split(":")) == 3 else f"{start}:00"
    if end is not None:
        target["endTime"] = end if len(end.split(":")) == 3 else f"{end}:00"
    if weekdays is not None:
        target["weekdays"] = weekdays

    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules/{rule_id}",
        json=target,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return dict(target)
    _raise_api_error(r, f"edit_rule({cam_id}, {rule_id})")
    return {}  # unreachable


def delete_rule(session: requests.Session, cam_id: str, rule_id: str) -> bool:
    """DELETE /v11/video_inputs/{cam_id}/rules/{rule_id}.

    Extracted from cmd_rules(delete) in bosch_camera.py.
    """
    r = session.delete(f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules/{rule_id}", timeout=10)
    if r.status_code in (200, 204):
        return True
    _raise_api_error(r, f"delete_rule({cam_id}, {rule_id})")
    return False  # unreachable


def list_friends(session: requests.Session) -> list[dict[str, Any]]:
    """GET /v11/friends — list camera-sharing friends/invitations.

    Extracted from cmd_friends in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/friends", timeout=10)
    if r.status_code == 200:
        result = r.json()
        return list(result) if isinstance(result, list) else []
    _raise_api_error(r, "list_friends()")
    return []  # unreachable


def invite_friend(session: requests.Session, email: str) -> dict[str, Any]:
    """POST /v11/friends — invite a new friend to share cameras with.

    Extracted from cmd_friends(invite) in bosch_camera.py.
    """
    r = session.post(
        f"{CLOUD_API}/v11/friends",
        json={"invitationEmail": email, "nickName": email},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201):
        return dict(r.json())
    _raise_api_error(r, "invite_friend()")
    return {}  # unreachable


def share_camera(
    session: requests.Session,
    friend_id: str,
    cam_id: str,
    days: Optional[int] = None,
) -> bool:
    """PUT /v11/friends/{friend_id}/share — share one camera with a friend.

    PUT /share is full-replace on the Bosch side (confirmed against the CLI's
    cmd_friends(share), which has the same semantics). Because the MCP tool's
    signature is one-camera-per-call (unlike the CLI, which can share several
    cameras in one command), sending a single-entry list here would silently
    DROP any camera the friend already had shared — bug-hunt finding
    2026-07-11. Fixed by first fetching this friend's current shares via
    list_friends() and merging: any existing entry for a DIFFERENT camera is
    kept as-is, an existing entry for the SAME camera is replaced (so calling
    this again with a new ``days`` value updates that camera's window), and
    the new camera is appended if not already present. ``days`` adds a
    time-limited share window for the target camera; omit for an unlimited
    share. Extracted from cmd_friends(share).
    """
    import datetime as _dt

    entry: dict[str, Any] = {"videoInputId": cam_id}
    if days is not None:
        now = _dt.datetime.now(_dt.timezone.utc)
        end = now + _dt.timedelta(days=days)
        entry["shareTime"] = {
            "start": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }

    existing_shares: list[dict[str, Any]] = []
    for friend in list_friends(session):
        if str(friend.get("id", "")) == str(friend_id):
            raw_shares = friend.get("sharedVideoInputs", friend.get("shares", []))
            existing_shares = [s for s in (raw_shares or []) if isinstance(s, dict)]
            break
    merged = [s for s in existing_shares if s.get("videoInputId") != cam_id]
    merged.append(entry)

    r = session.put(
        f"{CLOUD_API}/v11/friends/{friend_id}/share",
        json=merged,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"share_camera({friend_id}, {cam_id})")
    return False  # unreachable


def unshare_camera(session: requests.Session, friend_id: str) -> bool:
    """PUT /v11/friends/{friend_id}/share with an empty list — revoke all shares.

    Extracted from cmd_friends(unshare) in bosch_camera.py.
    """
    r = session.put(
        f"{CLOUD_API}/v11/friends/{friend_id}/share",
        json=[],
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        return True
    _raise_api_error(r, f"unshare_camera({friend_id})")
    return False  # unreachable


def remove_friend(session: requests.Session, friend_id: str) -> bool:
    """DELETE /v11/friends/{friend_id} — remove a friend entirely.

    Extracted from cmd_friends(remove) in bosch_camera.py.
    """
    r = session.delete(f"{CLOUD_API}/v11/friends/{friend_id}", timeout=10)
    if r.status_code in (200, 204):
        return True
    _raise_api_error(r, f"remove_friend({friend_id})")
    return False  # unreachable


def get_firmware_status(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """GET /v11/video_inputs/{cam_id}/firmware.

    Returns raw JSON dict {current, upToDate, updating, update (target
    version or None)}. Extracted from cmd_firmware_update in bosch_camera.py.
    """
    r = session.get(f"{CLOUD_API}/v11/video_inputs/{cam_id}/firmware", timeout=10)
    if r.status_code == 200:
        return dict(r.json())
    _raise_api_error(r, f"get_firmware_status({cam_id})")
    return {}  # unreachable


def install_firmware(session: requests.Session, cam_id: str) -> dict[str, Any]:
    """PUT /v11/video_inputs/{cam_id}/firmware — install the pending update.

    Same endpoint the official Bosch app's "Update now" button and the HA
    integration's ``async_install_firmware`` use. Guards mirror both of
    those: raises ValueError if an install is already ``updating``, or if
    there is no pending ``update`` target (caller maps ValueError to
    MCPError). Installing reboots the camera for 3-7 minutes.
    Extracted from cmd_firmware_update(install) in bosch_camera.py.
    """
    fw = get_firmware_status(session, cam_id)
    if fw.get("updating"):
        raise ValueError(f"Firmware install already in progress for camera {cam_id!r}")
    target = fw.get("update")
    if not target:
        raise ValueError(f"No firmware update available to install for camera {cam_id!r}")
    r = session.put(
        f"{CLOUD_API}/v11/video_inputs/{cam_id}/firmware",
        json={"id": target},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code in (200, 201, 204):
        fw["updating"] = True
        return fw
    _raise_api_error(r, f"install_firmware({cam_id})")
    return {}  # unreachable


def _raise_api_error(resp: requests.Response, context: str) -> None:
    """Translate a non-success HTTP response to an MCPError."""
    from bosch_camera_mcp.errors import MCPError

    if resp.status_code == 401:
        raise MCPError(
            code="reauth_required",
            detail=(
                f"API returned 401 during {context}. "
                "Run `python3 bosch_camera.py token browser`."
            ),
        )
    if resp.status_code == 403:
        raise MCPError(
            code="permission_denied",
            detail=f"API returned 403 during {context}.",
        )
    if resp.status_code >= 500:
        raise MCPError(
            code="api_unreachable",
            detail=f"API server error {resp.status_code} during {context}: {resp.text[:200]}",
        )
    raise MCPError(
        code="api_unreachable",
        detail=f"Unexpected HTTP {resp.status_code} during {context}: {resp.text[:200]}",
    )
