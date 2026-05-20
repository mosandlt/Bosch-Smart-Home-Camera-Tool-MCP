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
from typing import Optional

import requests

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
    import bosch_camera  # noqa: F401 — fails fast if path wrong


# ── Session + camera bootstrap ────────────────────────────────────────────────


def get_session_and_cameras(
    config_path: Optional[str] = None,
) -> tuple[dict, requests.Session, dict[str, dict]]:
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

    import bosch_camera as bc  # type: ignore[import-not-found]

    # If a custom config path is requested we temporarily patch CONFIG_FILE.
    # The CLI stores it as a module-level constant derived from __file__ which
    # we cannot change after import, so we load the JSON manually when needed.
    if config_path:
        import json

        with open(config_path) as fh:
            cfg: dict = json.load(fh)
        # Merge defaults so forward-compat keys are present
        bc._merge_defaults(cfg, bc.DEFAULT_CONFIG)
    else:
        cfg = bc.load_config()

    # Check token freshness — buffer of 300 s matches the auth-lifecycle spec
    token = cfg["account"].get("bearer_token", "").strip()
    if not token or bc._is_token_near_expiry(token, buffer_secs=300):
        logger.warning(
            "Token missing or near-expiry (< 5 min). Attempting silent renewal."
        )
        # Try silent renewal via refresh_token
        refresh = cfg["account"].get("refresh_token", "").strip()
        renewed = False
        if refresh:
            try:
                from get_token import _do_refresh  # type: ignore[import-not-found]

                tokens = _do_refresh(refresh)
                if tokens:
                    cfg["account"]["bearer_token"] = tokens.get("access_token", "")
                    cfg["account"]["refresh_token"] = tokens.get(
                        "refresh_token", refresh
                    )
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
            logger.warning(
                "Token near-expiry and renewal failed — proceeding with existing token."
            )

    session = bc.make_session(token)
    cameras = cfg.get("cameras", {})
    if not cameras:
        # Try to load from API (no interactive prompt — just fetch)
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
            cam_list = r.json()
            for cam in cam_list:
                name = cam.get("title", cam.get("id", "unknown"))
                cameras[name] = {
                    "id": cam.get("id", ""),
                    "name": name,
                    "model": cam.get("hardwareVersion", "CAMERA"),
                    "firmware": cam.get("firmwareVersion", ""),
                    "mac": cam.get("macAddress", ""),
                    "download_folder": name,
                    "local_ip": "",
                    "local_username": "",
                    "local_password": "",
                }
            cfg["cameras"] = cameras
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError(
                code="api_unreachable",
                detail=f"Failed to discover cameras: {exc}",
            )

    return cfg, session, cameras


def _resolve_cam(cameras: dict, key: str) -> tuple[str, dict]:
    """Resolve a partial camera name → (canonical_name, cam_info).

    Raises MCPError("unknown_camera") when no unambiguous match is found.
    """
    from bosch_camera_mcp.errors import MCPError

    if key in cameras:
        return key, cameras[key]
    key_lower = key.lower()
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


def set_pan(session: requests.Session, cam_id: str, direction: str) -> dict:
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
        except ValueError:
            raise MCPError(
                code="permission_denied",
                detail=(
                    f"Invalid pan direction {direction!r}. "
                    "Use home|left|right|back-left|back-right or an integer in [-120, 120]."
                ),
                camera=cam_id,
            )
        if not (-limit <= target <= limit):
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
        return r.json()
    _raise_api_error(r, f"set_pan({cam_id}, {direction})")
    return {}  # unreachable


def set_notifications(
    session: requests.Session, cam_id: str, enabled: bool
) -> bool:
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


def get_audio(session: requests.Session, cam_id: str) -> dict:
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
        current["SpeakerLevel"] = speaker_level
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


def get_intrusion_config(session: requests.Session, cam_id: str) -> dict:
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


def set_intrusion_config(
    session: requests.Session,
    cam_id: str,
    mode: Optional[str] = None,
    sensitivity: Optional[int] = None,
    distance: Optional[int] = None,
) -> bool:
    """PUT /v11/video_inputs/{cam_id}/intrusionDetectionConfig — partial update.

    Fetches current config first, merges requested fields, then PUT full body.
    sensitivity: 0-7 (HA v12.6.0 FW 9.40+ confirmed range).
    distance: 1-10 (iOS app slider range, capture 2026-04-28).
    Extracted from HA v12.6.0 BoschIntrusionSensitivityNumber.
    """
    current = get_intrusion_config(session, cam_id)
    if mode is not None:
        current["mode"] = mode
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


def get_wifi_info(session: requests.Session, cam_id: str) -> dict:
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
            # Map RSSI dBm to 0-100% signal quality
            clamped = max(-100, min(-50, int(rssi)))
            data["signal_strength"] = int(round((clamped + 100) * 2))
        return data
    _raise_api_error(r, f"get_wifi_info({cam_id})")
    return {}  # unreachable


def _raise_api_error(resp: requests.Response, context: str) -> None:
    """Translate a non-success HTTP response to an MCPError."""
    from bosch_camera_mcp.errors import MCPError

    if resp.status_code == 401:
        raise MCPError(
            code="reauth_required",
            detail=f"API returned 401 during {context}. Run `python3 bosch_camera.py token browser`.",
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
