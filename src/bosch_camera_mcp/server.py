"""MCP server entrypoint — v1.6.0.

All 8 tool bodies are now wired to the sister CLI's bosch_camera.py via
bosch_camera_mcp.adapters.cli_bridge (Option C: sys.path injection).
Resources and prompts are registered by importing resources.py / prompts.py
at the bottom of this module (the @mcp.resource / @mcp.prompt decorators
self-register against the shared `mcp` FastMCP instance).

Transport modes:
- stdio (default): for Claude Code / Claude Desktop local use
- streamable-http: for remote / multi-client deployments (binds 127.0.0.1 by default)
- sse: legacy SSE transport (also binds 127.0.0.1 by default)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from . import __version__
from .errors import MCPError

logger = logging.getLogger("bosch_camera_mcp")

mcp = FastMCP("bosch-smart-home-camera")

# Cache config path set at startup (--config flag / env var).
_CONFIG_PATH: Optional[str] = None


# ── Return models ─────────────────────────────────────────────────────────────


class CameraSummary(BaseModel):
    """Compact camera record returned by bosch_camera_list."""

    id: str = Field(description="Bosch cloud camera identifier (UUID)")
    name: str = Field(description="User-facing camera name from config")
    model: str = Field(description="Hardware model, e.g. HOME_Eyes_Outdoor")
    hw_version: str = Field(description="Hardware generation, e.g. Gen1, Gen2")
    status: str = Field(description="Online status: ONLINE | OFFLINE | UNKNOWN")


class CameraStatus(BaseModel):
    """Per-camera status snapshot."""

    name: str
    status: str
    privacy_mode: bool
    light_on: Optional[bool] = None
    last_event_at: Optional[str] = Field(
        default=None, description="ISO 8601 timestamp of the latest motion event"
    )


class SnapshotResult(BaseModel):
    """Path-based snapshot result."""

    path: str = Field(description="Filesystem path to the saved JPEG")
    method: str = Field(description="Source: local_lan (LAN-only; cloud fallback removed in v1.1.0)")
    timestamp: str = Field(description="ISO 8601 capture time")


class StreamUrlResult(BaseModel):
    """LAN RTSPS stream URL result."""

    camera: str = Field(description="Canonical camera name from config")
    rtsps_url: str = Field(description="LAN RTSPS URL via TLS proxy (rtsps://<user>:<pass>@<ip>:443/...)")
    note: str = "LAN-only — MCP host must be on the same network as the camera."


class LanPingResult(BaseModel):
    """LAN TCP reachability probe result."""

    reachable: bool = Field(description="True if the camera responded within the timeout")
    ip: str = Field(description="IP address that was probed")
    latency_ms: float = Field(
        description="Round-trip latency in milliseconds, or -1.0 if unreachable"
    )


class AudioSettings(BaseModel):
    """Microphone + speaker levels returned by bosch_camera_audio_get."""

    microphone_level: Optional[int] = Field(
        default=None, description="Microphone recording level (0-100)"
    )
    speaker_level: Optional[int] = Field(
        default=None, description="Speaker / intercom playback volume (0-100)"
    )
    intercom_enabled: Optional[bool] = Field(
        default=None,
        description="Two-way intercom enabled flag (Gen2 Indoor II only; None for cameras without intercom)",
    )


class IntrusionConfig(BaseModel):
    """Intrusion detection configuration returned by bosch_camera_intrusion_get."""

    mode: Optional[str] = Field(
        default=None,
        description=(
            "Detection mode (Bosch API field 'detectionMode'). "
            "Valid: PERSON | STANDARD | HIGH_SENSITIVITY | ZONES | ALL_MOTIONS | ONLY_HUMANS. "
            "Corrected 2026-05-28 — previously documented as OFF|ACTIVE|SCHEDULED which Bosch "
            "silently ignored because both the field name and the value set were wrong."
        ),
    )
    sensitivity: Optional[int] = Field(
        default=None, description="Detection sensitivity (0-7; 0=low, 7=high)"
    )
    distance: Optional[int] = Field(
        default=None, description="Detection distance in meters (1-8)"
    )


class WifiInfo(BaseModel):
    """WiFi signal information returned by bosch_camera_wifi."""

    rssi: Optional[int] = Field(
        default=None, description="Raw RSSI in dBm (negative; e.g. -67)"
    )
    ssid: Optional[str] = Field(default=None, description="Connected WiFi SSID")
    signal_strength: Optional[int] = Field(
        default=None,
        description="Signal quality 0-100 % derived from RSSI (-50 dBm = 100 %, -100 dBm = 0 %)",
    )


class MotionConfig(BaseModel):
    """Motion detection configuration returned by bosch_camera_motion_get."""

    enabled: bool = Field(description="Whether motion detection is enabled")
    sensitivity: Optional[str] = Field(
        default=None,
        description=(
            "Motion alarm sensitivity: OFF | LOW | MEDIUM_LOW | MEDIUM_HIGH | HIGH | SUPER_HIGH. "
            "Stored in 'motionAlarmConfiguration' field in the API."
        ),
    )


class RecordingOptions(BaseModel):
    """Cloud recording options returned by bosch_camera_recording_get."""

    sound_on: bool = Field(description="Whether audio is included in cloud recordings")


class AutofollowConfig(BaseModel):
    """Auto-follow (360° auto-tracking) config returned by bosch_camera_autofollow_get."""

    enabled: bool = Field(description="Whether auto-follow tracking is enabled")


class PrivacySoundConfig(BaseModel):
    """Privacy-sound override config returned by bosch_camera_privacy_sound_get."""

    enabled: bool = Field(
        description="Whether the camera plays an audible indicator when privacy mode changes"
    )


class UnreadCount(BaseModel):
    """Unread event count returned by bosch_camera_unread_get."""

    camera: str = Field(description="Camera name")
    count: int = Field(description="Number of unread events (from video_inputs listing)")


class CameraHealthEntry(BaseModel):
    """Per-camera health summary entry for bosch_camera_health_check_all."""

    name: str
    status: str
    privacy_mode: bool
    wifi_rssi: Optional[int] = None
    wifi_signal_strength: Optional[int] = None
    last_event_at: Optional[str] = None
    unread_count: int = 0
    error: Optional[str] = Field(
        default=None, description="Set when this camera's check failed"
    )


class TokenStatus(BaseModel):
    """Token validity returned by bosch_camera_token_status."""

    valid: bool = Field(description="True if the token is not expired")
    expires_in_min: Optional[int] = Field(
        default=None,
        description="Minutes until expiry (negative = already expired); None if undecodable",
    )
    email: Optional[str] = Field(
        default=None, description="Email/preferred_username from JWT claims"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bridge():
    """Lazy import of the bridge module (keeps import errors at call-time)."""
    from .adapters import cli_bridge  # noqa: PLC0415

    return cli_bridge


def _get_session(config_path: Optional[str] = None):
    """Return (cfg, session, cameras_dict); wraps reauth_required into MCPError."""
    br = _bridge()
    return br.get_session_and_cameras(config_path or _CONFIG_PATH)


def _build_status(
    name: str,
    cam_info: dict,
    session,
    cfg: dict,
) -> CameraStatus:
    """Build a CameraStatus model for one camera."""
    import bosch_camera as bc  # type: ignore[import-not-found]

    cam_id = cam_info["id"]

    # Ping
    status = bc.api_ping(session, cam_id)

    # Detail record (includes privacyMode, featureStatus)
    detail = bc.api_get_camera(session, cam_id) or {}
    privacy_raw = detail.get("privacyMode", "OFF")
    privacy_mode = privacy_raw.upper() == "ON"

    feat_status = detail.get("featureStatus", {})
    # light_on: True if front light override is on
    light_on: Optional[bool] = None
    if detail.get("featureSupport", {}).get("light", False):
        light_on = feat_status.get("frontIlluminatorInGeneralLightOn", False)

    # Latest event timestamp
    last_event_at: Optional[str] = None
    try:
        events = bc.api_get_events(session, cam_id, limit=1)
        if events:
            ts_raw = events[0].get("timestamp", "")
            last_event_at = ts_raw[:19] if ts_raw else None
    except Exception:
        pass

    return CameraStatus(
        name=name,
        status=status,
        privacy_mode=privacy_mode,
        light_on=light_on,
        last_event_at=last_event_at,
    )


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def bosch_camera_list() -> list[CameraSummary]:
    """List all configured Bosch cameras with their online status."""
    br = _bridge()
    cfg, session, cameras = _get_session()

    import bosch_camera as bc  # type: ignore[import-not-found]

    result: list[CameraSummary] = []
    for name, cam_info in cameras.items():
        cam_id = cam_info.get("id", "")
        status = bc.api_ping(session, cam_id) if cam_id else "UNKNOWN"
        model = cam_info.get("model", "CAMERA")
        result.append(
            CameraSummary(
                id=cam_id,
                name=name,
                model=model,
                hw_version=_camera_generation(model),
                status=status,
            )
        )
    return result


def _wrap_privacy_blocked(exc: Exception, name: str) -> Optional[MCPError]:
    """Convert raw Bosch HTTP 443 'camera.in.privacy.mode' into a clean MCPError.

    Returns the wrapping MCPError if the exception matches the privacy-blocked
    signature, otherwise None (caller should re-raise the original).
    """
    msg = str(exc)
    if "camera.in.privacy.mode" in msg or "HTTP 443" in msg:
        return MCPError(
            code="privacy_blocked",
            detail=(
                f"Camera '{name}' is in privacy mode — operation refused by Bosch cloud "
                "(HTTP 443 sh:camera.in.privacy.mode). Disable privacy first with "
                "bosch_camera_privacy_set(camera, enabled=False)."
            ),
            camera=name,
        )
    return None


def _camera_generation(model: str) -> str:
    """Map hardwareVersion → generation label.

    Gen2 models have the `HOME_*` prefix (HOME_Eyes_Outdoor, HOME_Eyes_Indoor).
    Gen1 are the legacy `OUTDOOR` / `INDOOR` (CAMERA_EYES / CAMERA_360).
    """
    return "Gen2" if model.startswith("HOME_") else "Gen1"


def _require_gen2(cam_info: dict, name: str, feature: str) -> None:
    """Raise hardware_unsupported for Gen1 cams attempting a Gen2-only feature.

    Source of truth is hardwareVersion (stored as ``model`` in cam_info) — the
    legacy ``has_sound`` flag was unreliable because the CLI scan never wrote
    it, so every Gen2 cam was blocked (incident 2026-05-24).
    """
    model = cam_info.get("model", "")
    if not model.startswith("HOME_"):
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model}') does not support {feature}. "
                "This feature is only available on Gen2 cameras (HOME_Eyes_*)."
            ),
            camera=name,
        )


@mcp.tool()
def bosch_camera_status(camera: str) -> CameraStatus:
    """Get the current status of one camera by name (case-insensitive)."""
    br = _bridge()
    cfg, session, cameras = _get_session()

    _bridge().ensure_cli_importable()
    name, cam_info = br._resolve_cam(cameras, camera)
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
def bosch_camera_snapshot(camera: str) -> SnapshotResult:
    """Capture a fresh snapshot via LAN only (HTTP Digest to camera IP). No Bosch cloud roundtrip.

    Requires the MCP host to be on the same network as the camera. Saves to
    ~/.cache/bosch-camera-mcp/snapshots/<camera>/<iso-ts>.jpg.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    import bosch_camera as bc  # type: ignore[import-not-found]

    name, cam_info = br._resolve_cam(cameras, camera)

    # Build cache directory
    safe_name = name.replace(" ", "_")
    cache_dir = (
        Path.home()
        / ".cache"
        / "bosch-camera-mcp"
        / "snapshots"
        / safe_name
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Filename uses `-` for the time part because `:` is illegal on FAT/NTFS;
    # the returned `.timestamp` is canonical ISO-8601 for callers to parse.
    now = datetime.datetime.now()
    ts_fs = now.strftime("%Y-%m-%dT%H-%M-%S")
    ts_iso = now.isoformat(timespec="seconds")

    data: Optional[bytes] = bc.snap_from_local(cam_info)

    if data is None:
        raise MCPError(
            code="local_unavailable",
            detail=(
                f"LAN snapshot failed for {name!r}. Possible causes: camera offline, "
                "Mac not on same network, local credentials missing."
            ),
            camera=name,
        )

    out_path = cache_dir / f"{ts_fs}.jpg"
    out_path.write_bytes(data)

    return SnapshotResult(
        path=str(out_path),
        method="local_lan",
        timestamp=ts_iso,
    )


@mcp.tool()
def bosch_camera_stream_url(camera: str) -> StreamUrlResult:
    """Get the LAN RTSPS stream URL for one camera. No Bosch cloud relay.

    The returned URL is consumable by ffmpeg/VLC/go2rtc. Requires that the MCP
    host runs on the same network as the camera and has local credentials
    configured for the camera (bosch_config.json → cameras[name].local_*).
    """
    from urllib.parse import quote as _q

    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    local_ip = cam_info.get("local_ip", "").strip()
    local_user = cam_info.get("local_username", "").strip()
    local_pass = cam_info.get("local_password", "").strip()

    if not local_ip or not local_user or not local_pass:
        raise MCPError(
            code="local_unavailable",
            detail=(
                f"No local credentials for camera {name!r}. "
                "Add local_ip + local_username + local_password in bosch_config.json."
            ),
            camera=name,
        )

    auth_prefix = f"{_q(local_user, safe='')}:{_q(local_pass, safe='')}@"
    rtsps_url = (
        f"rtsps://{auth_prefix}{local_ip}:443"
        "/rtsp_tunnel?inst=2&enableaudio=1&fmtp=1&maxSessionDuration=3600"
    )

    return StreamUrlResult(camera=name, rtsps_url=rtsps_url)


@mcp.tool()
def bosch_camera_events(camera: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent motion / person / audio events for one camera.

    Each item contains: event_id, type, timestamp_iso, has_clip.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    import bosch_camera as bc  # type: ignore[import-not-found]

    name, cam_info = br._resolve_cam(cameras, camera)
    cam_id = cam_info["id"]

    raw_events = bc.api_get_events(session, cam_id, limit=max(limit, 1))
    normalized: list[dict[str, Any]] = []
    for ev in raw_events[:limit]:
        ts_raw = ev.get("timestamp", "")
        # Bosch cloud field names: `eventType` + `videoClipUrl` /
        # `videoClipUploadStatus` (Done|Local|Unavailable). The legacy guesses
        # `type` / `clipUrl` / `videoUrl` never matched a real payload, so
        # every event was reported as type=UNKNOWN / has_clip=False.
        upload_status = ev.get("videoClipUploadStatus", "")
        has_clip = bool(ev.get("videoClipUrl")) or upload_status == "Done"
        normalized.append(
            {
                "event_id": ev.get("id", ""),
                "type": ev.get("eventType") or ev.get("type") or "UNKNOWN",
                "tags": ev.get("eventTags") or [],
                "timestamp_iso": ts_raw[:19] if ts_raw else "",
                "has_clip": has_clip,
                "clip_status": upload_status or None,
            }
        )
    return normalized


@mcp.tool()
async def bosch_camera_lan_ping(
    camera: Optional[str] = None,
    lan_ip: Optional[str] = None,
) -> LanPingResult:
    """Probe whether a camera is reachable on the LAN (TCP port 443, 1.5 s timeout).

    Pass either ``camera`` (resolved against bosch_config.json) or a raw
    ``lan_ip``.  Useful when diagnosing cloud-down situations: if this returns
    ``reachable=true`` while the cloud API is returning 5xx, privacy/light writes
    via ``prefer_local=True`` will work without waiting for Bosch infrastructure.

    Returns ``{reachable, ip, latency_ms}``.
    """
    from .lan_rcp import lan_tcp_ping  # noqa: PLC0415

    if lan_ip is not None:
        ip = lan_ip.strip()
    elif camera is not None:
        br = _bridge()
        _cfg, _session, cameras = _get_session()
        _name, cam_info = br._resolve_cam(cameras, camera)
        ip = cam_info.get("local_ip", "").strip()
        if not ip:
            raise MCPError(
                code="local_unavailable",
                detail=(
                    f"No local_ip configured for camera {camera!r}. "
                    "Add local_ip in bosch_config.json."
                ),
                camera=camera,
            )
    else:
        raise MCPError(
            code="invalid_argument",
            detail="Provide either camera (name) or lan_ip.",
        )

    reachable, latency_ms = await lan_tcp_ping(ip)
    return LanPingResult(reachable=reachable, ip=ip, latency_ms=latency_ms)


@mcp.tool()
async def bosch_camera_privacy_set(
    camera: str,
    enabled: bool,
    prefer_local: bool = False,
) -> CameraStatus:
    """Turn privacy mode on or off. enabled=True hides the camera.

    When ``prefer_local=True``, attempt the RCP-LAN write path FIRST (skipping
    the Bosch cloud entirely). Useful when the agent already knows the cloud is
    down but the camera is LAN-reachable (confirmed via ``bosch_camera_lan_ping``).
    Falls back to the cloud API automatically if the LAN write fails or if no
    ``local_ip`` is configured.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    if prefer_local:
        local_ip = cam_info.get("local_ip", "").strip()
        if local_ip:
            from .lan_rcp import refresh_local_creds, rcp_local_write_privacy  # noqa: PLC0415

            local_user = cam_info.get("local_username", "").strip() or None
            local_pass = cam_info.get("local_password", "").strip() or None
            cam_id = cam_info["id"]

            async def _on_401_privacy() -> Optional[tuple[str, str]]:
                return await refresh_local_creds(
                    cam_id=cam_id,
                    session=session,
                    cfg=cfg,
                    cam_name=name,
                    config_path=_CONFIG_PATH,
                )

            ok = await rcp_local_write_privacy(
                local_ip, enabled,
                user=local_user, password=local_pass,
                on_401=_on_401_privacy,
            )
            if ok:
                logger.info(
                    "privacy_set(%s, %s): succeeded via LOCAL RCP (%s)", name, enabled, local_ip
                )
                return _build_status(name, cam_info, session, cfg)
            logger.warning(
                "privacy_set(%s, %s): LOCAL RCP failed (%s), falling back to cloud",
                name,
                enabled,
                local_ip,
            )

    br.set_privacy_mode(session, cam_info["id"], enabled)
    # Cloud occasionally lags behind the PUT — poll briefly until the camera detail
    # reflects the requested state, so we don't return stale `privacy_mode` to the agent.
    import time as _time  # local import to keep top of module clean
    import bosch_camera as bc  # type: ignore[import-not-found]
    expected = "ON" if enabled else "OFF"
    for _ in range(10):  # 10 × 0.5 s = 5 s budget
        detail = bc.api_get_camera(session, cam_info["id"]) or {}
        if str(detail.get("privacyMode", "")).upper() == expected:
            break
        _time.sleep(0.5)
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
async def bosch_camera_light_set(
    camera: str,
    enabled: bool,
    prefer_local: bool = False,
) -> CameraStatus:
    """Turn the camera's spotlight on or off.

    Only for cameras with ``featureSupport.light=true`` (currently: Eyes Outdoor II /
    HOME_Eyes_Outdoor).  Raises ``hardware_unsupported`` immediately for cameras without
    a controllable light — no Bosch cloud call is made.

    When ``prefer_local=True``, attempt the RCP-LAN write path FIRST (skipping
    the Bosch cloud entirely). Maps ``enabled=True`` to brightness 100, ``False``
    to 0.  Falls back to the cloud API automatically if the LAN write fails or if
    no ``local_ip`` is configured.  Wallwasher RGB is always cloud-only.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    # Gate: reject cameras without controllable light hardware before any API call.
    # The config `has_light` flag is sometimes stale, so also accept Eyes Außenkamera II
    # (model == "HOME_Eyes_Outdoor") by hardware identity — that model always has lights.
    model = cam_info.get("model", "")
    has_light = cam_info.get("has_light", False) or model == "HOME_Eyes_Outdoor"
    if not has_light:
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model or 'unknown'}') has no controllable light hardware. "
                "Light is only available on Eyes Außenkamera II (HOME_Eyes_Outdoor)."
            ),
            camera=name,
        )

    if prefer_local:
        local_ip = cam_info.get("local_ip", "").strip()
        if local_ip:
            from .lan_rcp import refresh_local_creds, rcp_local_write_front_light  # noqa: PLC0415

            local_user = cam_info.get("local_username", "").strip() or None
            local_pass = cam_info.get("local_password", "").strip() or None
            cam_id = cam_info["id"]
            brightness = 100 if enabled else 0

            async def _on_401_light() -> Optional[tuple[str, str]]:
                return await refresh_local_creds(
                    cam_id=cam_id,
                    session=session,
                    cfg=cfg,
                    cam_name=name,
                    config_path=_CONFIG_PATH,
                )

            ok = await rcp_local_write_front_light(
                local_ip, brightness,
                user=local_user, password=local_pass,
                on_401=_on_401_light,
            )
            if ok:
                logger.info(
                    "light_set(%s, %s): succeeded via LOCAL RCP (%s)", name, enabled, local_ip
                )
                return _build_status(name, cam_info, session, cfg)
            logger.warning(
                "light_set(%s, %s): LOCAL RCP failed (%s), falling back to cloud",
                name,
                enabled,
                local_ip,
            )

    br.set_light(session, cam_info["id"], enabled)
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
def bosch_camera_pan(
    camera: str,
    direction: str = "home",
    preset: str | None = None,
) -> CameraStatus:
    """Pan the 360° indoor camera (Gen1 CAMERA_360 only, panLimit > 0).

    Named presets (preferred, via ``preset`` parameter):
      home       →   0° (center)
      left       → -60°
      right      → +60°
      back-left  → -120° (full left limit)
      back-right → +120° (full right limit)

    ``direction`` accepts the same preset names, legacy aliases (center), or
    an integer string in the range -panLimit to +panLimit.

    When ``preset`` is set it takes priority over ``direction``.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    # preset param overrides direction
    effective = preset if preset is not None else direction
    try:
        br.set_pan(session, cam_info["id"], effective)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
def bosch_camera_siren_trigger(camera: str, stop: bool = False) -> CameraStatus:
    """Trigger (or stop) the indoor siren on a camera.

    Endpoint depends on the camera model:

    - **Gen1 360° indoor** (``model == "INDOOR"``) → ``PUT /v11/video_inputs/{id}/acoustic_alarm``
      Body: ``{"enabled": <bool>}``.  Outdoor cameras return HTTP 442 (not supported).
    - **Gen2 Indoor II** (``model == "HOME_Eyes_Indoor"``) → ``PUT /v11/video_inputs/{id}/panic_alarm``
      Body: ``{"status": "ON"|"OFF"}``. 75 dB integrated siren.

    The siren plays for the camera-side configured duration (typically 30–60 s
    on Gen2). To change the duration on Gen2, use the HA integration's
    ``number.bosch_<cam>_sirenen_dauer`` entity (range 10–300 s).

    Raises ``hardware_unsupported`` for outdoor cameras and Gen1 outdoor models.
    Raises ``privacy_blocked`` when the camera is in privacy mode (Gen2 panic
    alarm is gated by privacyMode at the Bosch cloud level).

    Args:
        camera: Camera name (case-insensitive).
        stop:   If True, send the stop variant ({"enabled": False} or {"status": "OFF"}).
                Useful for cancelling an active Gen2 panic alarm before its duration expires.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    model = cam_info.get("model", "")
    # NOTE: only HOME_Eyes_Indoor (Gen2 Indoor II) has a working siren endpoint
    # (/panic_alarm).  Gen1 indoor's documented /acoustic_alarm returns HTTP 404
    # in production (verified 2026-05-28) — not supported here until the correct
    # Gen1 endpoint is found.
    if model != "HOME_Eyes_Indoor":
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model or 'unknown'}') has no siren tool support. "
                "Only Gen2 Indoor II (HOME_Eyes_Indoor) has the working /panic_alarm endpoint. "
                "Gen1 360° indoor's /acoustic_alarm returns HTTP 404 — endpoint unknown."
            ),
            camera=name,
        )

    try:
        br.trigger_siren(session, cam_info["id"], model=model, stop=stop)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise

    logger.info("siren_trigger(%s, stop=%s, model=%s): OK", name, stop, model)
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
def bosch_camera_notifications_set(camera: str, enabled: bool) -> CameraStatus:
    """Toggle push notifications for one camera."""
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    br.set_notifications(session, cam_info["id"], enabled)
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
async def bosch_camera_maintenance_status() -> dict[str, Any]:
    """Fetch the current Bosch Smart Home cloud maintenance announcement from the official community RSS feed.

    Returns title, time window, link, state (active/scheduled/past/recent/unknown/idle),
    and a ``recommended_action`` hint for the calling agent:

    - ``"check_lan"`` — state is ``"active"`` (outage/maintenance in progress):
      run ``bosch_camera_lan_ping`` to check per-camera LAN reachability, then use
      ``prefer_local=True`` on privacy/light writes while the cloud is down.
    - ``"wait"`` — state is ``"scheduled"``: outage is upcoming; no action needed yet.
    - ``null`` — state is past/recent/unknown/idle: normal operation expected.

    Use this tool when users ask why cameras are unavailable or when the cloud
    returns 5xx errors.
    """
    from .maintenance import async_fetch_maintenance  # noqa: PLC0415

    mw = await async_fetch_maintenance()
    if mw is None:
        return {
            "state": "idle",
            "summary": "No maintenance announcement found",
            "recommended_action": None,
        }
    result: dict[str, Any] = mw.as_dict()
    state = mw.state()
    result["state"] = state
    if state == "active":
        result["recommended_action"] = "check_lan"
    elif state == "scheduled":
        result["recommended_action"] = "wait"
    else:
        result["recommended_action"] = None
    return result


@mcp.tool()
def bosch_camera_audio_get(camera: str) -> AudioSettings:
    """Get the microphone and speaker level settings for one Gen2 camera.

    Only for cameras with ``featureSupport.sound=true`` (Gen2 Indoor II and
    Gen2 Outdoor II).  Raises ``hardware_unsupported`` immediately for Gen1
    cameras or Gen2 cameras without audio hardware.

    Returns ``{microphone_level, speaker_level, intercom_enabled}``.
    ``intercom_enabled`` is ``None`` for cameras without a two-way intercom
    (e.g. Outdoor II).
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "audio settings")

    raw = br.get_audio(session, cam_info["id"])
    mic: Optional[int] = raw.get("microphoneLevel")
    spk_raw = raw.get("SpeakerLevel") or raw.get("speakerLevel")
    spk: Optional[int] = int(spk_raw) if spk_raw is not None else None
    intercom_raw = raw.get("intercomEnabled")
    intercom: Optional[bool] = bool(intercom_raw) if intercom_raw is not None else None
    return AudioSettings(
        microphone_level=mic,
        speaker_level=spk,
        intercom_enabled=intercom,
    )


@mcp.tool()
def bosch_camera_audio_set(
    camera: str,
    mic_level: Optional[int] = None,
    speaker_level: Optional[int] = None,
) -> AudioSettings:
    """Set the microphone level and/or speaker level for one Gen2 camera.

    Only for cameras with ``featureSupport.sound=true`` (Gen2 Indoor II and
    Gen2 Outdoor II).  Raises ``hardware_unsupported`` for cameras without audio
    hardware.  At least one of ``mic_level`` or ``speaker_level`` must be provided.

    Both values must be in the range 0-100.  The API persists the full audio
    payload — unspecified fields are preserved from the current camera state.

    Returns updated ``{microphone_level, speaker_level, intercom_enabled}`` after write.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "audio settings")

    if mic_level is None and speaker_level is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of mic_level or speaker_level must be provided.",
            camera=name,
        )

    for label, val in (("mic_level", mic_level), ("speaker_level", speaker_level)):
        if val is not None and not (0 <= val <= 100):
            raise MCPError(
                code="invalid_argument",
                detail=f"{label}={val} is out of range. Must be 0-100.",
                camera=name,
            )

    br.set_audio(session, cam_info["id"], mic_level=mic_level, speaker_level=speaker_level)
    # Re-fetch to return updated values
    raw = br.get_audio(session, cam_info["id"])
    mic: Optional[int] = raw.get("microphoneLevel")
    spk_raw = raw.get("SpeakerLevel") or raw.get("speakerLevel")
    spk: Optional[int] = int(spk_raw) if spk_raw is not None else None
    intercom_raw = raw.get("intercomEnabled")
    intercom: Optional[bool] = bool(intercom_raw) if intercom_raw is not None else None
    return AudioSettings(
        microphone_level=mic,
        speaker_level=spk,
        intercom_enabled=intercom,
    )


@mcp.tool()
def bosch_camera_intrusion_get(camera: str) -> IntrusionConfig:
    """Get the intrusion detection configuration for one Gen2 camera.

    Returns ``{mode, sensitivity, distance}``.  Only available on Gen2 cameras
    (``has_sound=true`` in config is reused as the Gen2 gate; intrusion detection
    is a Gen2-only feature).  Raises ``hardware_unsupported`` for Gen1 cameras.

    ``mode``: ``PERSON`` | ``STANDARD`` | ``HIGH_SENSITIVITY`` | ``ZONES`` |
    ``ALL_MOTIONS`` | ``ONLY_HUMANS`` (maps to Bosch API field ``detectionMode``).
    ``sensitivity``: 0 (low) to 7 (high) — confirmed range FW 9.40+.
    ``distance``: detection range in meters, 1-8 (Bosch API limit).
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "intrusion detection")

    try:
        raw = br.get_intrusion_config(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return IntrusionConfig(
        mode=raw.get("mode"),
        sensitivity=raw.get("sensitivity"),
        distance=raw.get("distance"),
    )


@mcp.tool()
def bosch_camera_intrusion_set(
    camera: str,
    mode: Optional[str] = None,
    sensitivity: Optional[int] = None,
    distance: Optional[int] = None,
) -> IntrusionConfig:
    """Update the intrusion detection configuration for one Gen2 camera.

    Gen2-only feature — raises ``hardware_unsupported`` for Gen1 cameras.
    At least one parameter must be provided.

    ``mode``: ``PERSON`` | ``STANDARD`` | ``HIGH_SENSITIVITY`` | ``ZONES`` |
    ``ALL_MOTIONS`` | ``ONLY_HUMANS`` (maps to Bosch API field ``detectionMode``).
    v1.6.0 incorrectly documented ``OFF|ACTIVE|SCHEDULED`` and silently sent ``mode``
    instead of ``detectionMode`` — Bosch's API accepted the request but dropped the
    field. Fixed in v1.6.1.
    ``sensitivity``: 0-7 (0 = low, 7 = high; FW 9.40+ confirmed range).
    ``distance``: detection range in meters, 1-8 (Bosch API rejects 9+ with
    HTTP 400 ``"must be less than or equal to 8"`` — verified 2026-05-28 FW 9.40.102).

    Unspecified fields are preserved from the current camera configuration.
    Returns the updated ``{mode, sensitivity, distance}`` after write.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "intrusion detection")

    if mode is None and sensitivity is None and distance is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of mode, sensitivity, or distance must be provided.",
            camera=name,
        )

    if sensitivity is not None and not (0 <= sensitivity <= 7):
        raise MCPError(
            code="invalid_argument",
            detail=f"sensitivity={sensitivity} is out of range. Must be 0-7.",
            camera=name,
        )
    if distance is not None and not (1 <= distance <= 8):
        raise MCPError(
            code="invalid_argument",
            detail=f"distance={distance} is out of range. Must be 1-8 (Bosch API limit).",
            camera=name,
        )

    try:
        br.set_intrusion_config(
            session, cam_info["id"], mode=mode, sensitivity=sensitivity, distance=distance
        )
        raw = br.get_intrusion_config(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return IntrusionConfig(
        mode=raw.get("mode"),
        sensitivity=raw.get("sensitivity"),
        distance=raw.get("distance"),
    )


@mcp.tool()
def bosch_camera_wifi(camera: str) -> WifiInfo:
    """Get the WiFi signal quality for one camera.

    Queries ``GET /v11/video_inputs/{id}/wifiinfo`` and returns
    ``{rssi, ssid, signal_strength}``.

    ``rssi``: raw RSSI in dBm (negative integer, e.g. ``-67``).
    ``ssid``: the connected WiFi network name.
    ``signal_strength``: 0-100 % quality derived from RSSI
    (``-50 dBm = 100 %``, ``-100 dBm = 0 %``).

    Useful for diagnosing intermittent connectivity, stream drops, or deciding
    whether to force ``prefer_local=True`` on privacy/light writes.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    raw = br.get_wifi_info(session, cam_info["id"])
    rssi_raw = raw.get("rssi") or raw.get("signalStrength")
    rssi: Optional[int] = int(rssi_raw) if rssi_raw is not None else None
    ssid: Optional[str] = raw.get("ssid") or raw.get("networkName")
    strength: Optional[int] = raw.get("signal_strength")
    return WifiInfo(rssi=rssi, ssid=ssid, signal_strength=strength)


# ── LAN RCP READ helper ───────────────────────────────────────────────────────


async def _fetch_rcp_lan(
    cam_ip: str,
    user: str,
    password: str,
    opcode_hex: str,
) -> Optional[bytes]:
    """Send a READ RCP request to the camera's LAN HTTPS endpoint.

    Uses aiohttp with Digest auth and TLS verify disabled (cameras use
    self-signed certs). Returns the raw response body bytes on HTTP 200,
    or None on any error (network, auth, non-200).

    Used by bosch_camera_onvif_scopes (0x0a98) and bosch_camera_rcp_version
    (0xff00 / 0xff04). Pass opcode_hex with or without leading "0x".
    """
    import aiohttp

    if not opcode_hex.lower().startswith("0x"):
        opcode_hex = "0x" + opcode_hex

    url = f"https://{cam_ip}/rcp.xml"
    params = {
        "command": opcode_hex,
        "direction": "READ",
        "type": "P_OCTET",
    }
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        auth = aiohttp.DigestAuth(login=user, password=password)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(
                url,
                params=params,
                ssl=ssl_ctx,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.debug(
                    "_fetch_rcp_lan: %s@%s opcode=%s HTTP %d",
                    opcode_hex, cam_ip, opcode_hex, resp.status,
                )
                return None
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("_fetch_rcp_lan: %s@%s error: %s", opcode_hex, cam_ip, exc)
        return None


# ── New tools: MJPEG snapshot, ONVIF scopes, RCP version, feature flags ───────


@mcp.tool()
async def bosch_camera_mjpeg_snapshot(camera: str) -> dict[str, Any]:
    """Direct LAN MJPEG snapshot via RTSP inst=3 (Gen2 only). Faster than snap.jpg, no cloud roundtrip.

    Uses ffmpeg to pull a single frame from the camera's RTSPS stream (inst=3 = sub-stream,
    lower resolution but faster). Saves JPEG to ~/.cache/bosch-camera-mcp/snapshots/.
    Requires ffmpeg installed and MCP host on same LAN as camera.
    Gen2 only (HOME_Eyes_Outdoor / HOME_Eyes_Indoor).
    """
    from urllib.parse import quote as _q

    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "MJPEG snapshot")

    local_ip = cam_info.get("local_ip", "").strip()
    local_user = cam_info.get("local_username", "").strip()
    local_pass = cam_info.get("local_password", "").strip()

    if not local_ip or not local_user or not local_pass:
        raise MCPError(
            code="local_unavailable",
            detail=(
                f"No local credentials for camera {name!r}. "
                "Add local_ip + local_username + local_password in bosch_config.json."
            ),
            camera=name,
        )

    auth_prefix = f"{_q(local_user, safe='')}:{_q(local_pass, safe='')}@"
    rtsp_url = (
        f"rtsps://{auth_prefix}{local_ip}:443"
        "/rtsp_tunnel?inst=3&enableaudio=0&fmtp=1"
    )

    safe_name = name.replace(" ", "_")
    cache_dir = (
        Path.home()
        / ".cache"
        / "bosch-camera-mcp"
        / "snapshots"
        / safe_name
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    ts_fs = now.strftime("%Y-%m-%dT%H-%M-%S")
    ts_iso = now.isoformat(timespec="seconds")
    out_path = cache_dir / f"{ts_fs}_mjpeg.jpg"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-vframes", "1",
            "-f", "image2",
            "-y",
            str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        if proc.returncode != 0 or not out_path.exists():
            stderr_msg = (stderr_bytes or b"").decode(errors="replace")[-300:]
            raise MCPError(
                code="local_unavailable",
                detail=(
                    f"ffmpeg MJPEG snapshot failed for {name!r} "
                    f"(exit {proc.returncode}): {stderr_msg}"
                ),
                camera=name,
            )
    except asyncio.TimeoutError:
        raise MCPError(
            code="local_unavailable",
            detail=f"ffmpeg MJPEG snapshot timed out for {name!r} after 20 s.",
            camera=name,
        )

    return {
        "path": str(out_path),
        "method": "mjpeg_lan_rtsp",
        "timestamp": ts_iso,
        "camera": name,
    }


@mcp.tool()
async def bosch_camera_onvif_scopes(camera: str) -> dict[str, Any]:
    """Read ONVIF device scopes from camera via LAN RCP command 0x0a98 (Gen2 only).

    Returns parsed scope fields: name, hardware model, ONVIF profiles, and the
    raw scope string. Requires local_ip + local credentials in bosch_config.json.
    Gen2 only (HOME_Eyes_Outdoor / HOME_Eyes_Indoor).
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "ONVIF scopes")

    local_ip = cam_info.get("local_ip", "").strip()
    local_user = cam_info.get("local_username", "").strip()
    local_pass = cam_info.get("local_password", "").strip()

    if not local_ip or not local_user or not local_pass:
        raise MCPError(
            code="local_unavailable",
            detail=(
                f"No local credentials for camera {name!r}. "
                "Add local_ip + local_username + local_password in bosch_config.json."
            ),
            camera=name,
        )

    raw = await _fetch_rcp_lan(local_ip, local_user, local_pass, "0x0a98")
    if raw is None:
        raise MCPError(
            code="local_unavailable",
            detail=(
                f"RCP 0x0a98 (ONVIF scopes) failed for {name!r} at {local_ip}. "
                "Camera may be offline or credentials invalid."
            ),
            camera=name,
        )

    # Parse scope string — typical format:
    # onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/<n>
    # onvif://www.onvif.org/hardware/<model> onvif://www.onvif.org/Profile/S ...
    try:
        scope_str = raw.decode("utf-8", errors="replace").strip()
    except Exception:
        scope_str = repr(raw)

    parsed_name: Optional[str] = None
    parsed_hw: Optional[str] = None
    parsed_profiles: list[str] = []
    for token in scope_str.split():
        if "/name/" in token:
            parsed_name = token.split("/name/", 1)[-1]
        elif "/hardware/" in token:
            parsed_hw = token.split("/hardware/", 1)[-1]
        elif "/Profile/" in token:
            parsed_profiles.append(token.split("/Profile/", 1)[-1])

    return {
        "name": parsed_name,
        "hardware": parsed_hw,
        "profiles": parsed_profiles,
        "raw_scopes": scope_str,
    }


@mcp.tool()
async def bosch_camera_rcp_version(camera: str) -> dict[str, Any]:
    """Read RCP firmware version from camera via LAN (opcodes 0xff00 + 0xff04).

    Returns primary and secondary RCP library versions in dotted decimal and raw hex.
    Useful for diagnosing protocol compatibility. Requires local_ip + local credentials.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    local_ip = cam_info.get("local_ip", "").strip()
    local_user = cam_info.get("local_username", "").strip()
    local_pass = cam_info.get("local_password", "").strip()

    if not local_ip or not local_user or not local_pass:
        raise MCPError(
            code="local_unavailable",
            detail=(
                f"No local credentials for camera {name!r}. "
                "Add local_ip + local_username + local_password in bosch_config.json."
            ),
            camera=name,
        )

    primary_raw, secondary_raw = await asyncio.gather(
        _fetch_rcp_lan(local_ip, local_user, local_pass, "0xff00"),
        _fetch_rcp_lan(local_ip, local_user, local_pass, "0xff04"),
    )

    def _parse_version(data: Optional[bytes]) -> tuple[Optional[str], Optional[str]]:
        """Return (dotted_str, hex_str) from 4-byte RCP version payload."""
        if data is None or len(data) < 4:
            return None, None
        b = data[:4]
        hex_str = b.hex()
        dotted = f"{b[0]}.{b[1]}.{b[2]}.{b[3]}"
        return dotted, hex_str

    pv, ph = _parse_version(primary_raw)
    sv, sh = _parse_version(secondary_raw)

    return {
        "primary": pv,
        "secondary": sv,
        "raw_primary_hex": ph,
        "raw_secondary_hex": sh,
    }


@mcp.tool()
def bosch_camera_feature_flags() -> dict[str, Any]:
    """Fetch account-level Bosch cloud feature flags from GET /v11/feature_flags.

    Returns the raw dict of feature flag names to boolean values, e.g.
    {"APP_RATING": true, "IOT_THINGS_INTEGRATION": true, ...}.
    No camera parameter — flags are account-level.
    Useful for discovering which Bosch platform features are active for this account.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    from .adapters.cli_bridge import CLOUD_API  # noqa: PLC0415

    r = session.get(f"{CLOUD_API}/v11/feature_flags", timeout=10)
    if r.status_code == 200:
        data = r.json()
        # API may return a list of {name, enabled} or a flat dict — normalise both
        if isinstance(data, list):
            return {item.get("name", str(i)): bool(item.get("enabled", False))
                    for i, item in enumerate(data)}
        if isinstance(data, dict):
            return {k: bool(v) for k, v in data.items()}
        return {"raw": data}

    from .adapters.cli_bridge import _raise_api_error  # noqa: PLC0415
    _raise_api_error(r, "bosch_camera_feature_flags")
    return {}  # unreachable


# ── v1.6.0 tools: motion, recording, autofollow, privacy_sound, unread, health_check_all, token_status ──


@mcp.tool()
def bosch_camera_motion_get(camera: str) -> MotionConfig:
    """Get motion detection settings for one camera.

    Returns ``{enabled, sensitivity}`` where sensitivity is one of:
    ``OFF | LOW | MEDIUM_LOW | MEDIUM_HIGH | HIGH | SUPER_HIGH``.

    API: GET /v11/video_inputs/{id}/motion.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        raw = br.get_motion_config(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return MotionConfig(
        enabled=bool(raw.get("enabled", False)),
        sensitivity=raw.get("motionAlarmConfiguration") or raw.get("sensitivity"),
    )


@mcp.tool()
def bosch_camera_motion_set(
    camera: str,
    enabled: Optional[bool] = None,
    sensitivity: Optional[str] = None,
) -> MotionConfig:
    """Set motion detection enabled state and/or sensitivity for one camera.

    At least one of ``enabled`` or ``sensitivity`` must be provided.

    ``sensitivity``: ``OFF | LOW | MEDIUM_LOW | MEDIUM_HIGH | HIGH | SUPER_HIGH``.
    Note: providing only ``sensitivity`` also implicitly enables motion detection
    (mirrors the CLI behavior — setting sensitivity implies you want it active).

    API: PUT /v11/video_inputs/{id}/motion.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    if enabled is None and sensitivity is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of enabled or sensitivity must be provided.",
            camera=name,
        )

    VALID_SENSITIVITIES = {"OFF", "LOW", "MEDIUM_LOW", "MEDIUM_HIGH", "HIGH", "SUPER_HIGH"}
    if sensitivity is not None and sensitivity.upper() not in VALID_SENSITIVITIES:
        raise MCPError(
            code="invalid_argument",
            detail=(
                f"sensitivity={sensitivity!r} is not valid. "
                f"Must be one of: {', '.join(sorted(VALID_SENSITIVITIES))}."
            ),
            camera=name,
        )

    try:
        br.set_motion_config(
            session, cam_info["id"],
            enabled=enabled,
            sensitivity=sensitivity.upper() if sensitivity else None,
        )
        raw = br.get_motion_config(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return MotionConfig(
        enabled=bool(raw.get("enabled", False)),
        sensitivity=raw.get("motionAlarmConfiguration") or raw.get("sensitivity"),
    )


@mcp.tool()
def bosch_camera_recording_get(camera: str) -> RecordingOptions:
    """Get cloud recording options for one camera.

    Returns ``{sound_on}`` — whether audio is recorded in cloud clips.

    API: GET /v11/video_inputs/{id}/recording_options.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        raw = br.get_recording_options(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return RecordingOptions(sound_on=bool(raw.get("recordSound", False)))


@mcp.tool()
def bosch_camera_recording_set(camera: str, sound_on: bool) -> RecordingOptions:
    """Enable or disable audio in cloud recordings for one camera.

    ``sound_on=True``  → record audio in cloud clips.
    ``sound_on=False`` → no audio in cloud clips.

    API: PUT /v11/video_inputs/{id}/recording_options  Body: ``{"recordSound": bool}``.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        br.set_recording_options(session, cam_info["id"], sound_on=sound_on)
        raw = br.get_recording_options(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return RecordingOptions(sound_on=bool(raw.get("recordSound", False)))


@mcp.tool()
def bosch_camera_autofollow_get(camera: str) -> AutofollowConfig:
    """Get auto-follow (360° auto-tracking) state for one camera.

    Returns ``{enabled}``.  Only meaningful for 360° cameras with ``panLimit > 0``
    (Gen1 CAMERA_360 indoor). Raises ``hardware_unsupported`` for non-360° cameras.

    API: GET /v11/video_inputs/{id}/autofollow.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    # Gate: panLimit must be > 0 for auto-follow to make sense
    pan_limit = cam_info.get("pan_limit", 0)
    if not pan_limit:
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' does not support auto-follow (panLimit=0). "
                "Auto-follow is only available on 360° indoor cameras (CAMERA_360)."
            ),
            camera=name,
        )
    try:
        raw = br.get_autofollow(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return AutofollowConfig(enabled=bool(raw.get("result", False)))


@mcp.tool()
def bosch_camera_autofollow_set(camera: str, enabled: bool) -> AutofollowConfig:
    """Enable or disable 360° auto-tracking for one camera.

    Only available on 360° indoor cameras (``panLimit > 0``).
    Raises ``hardware_unsupported`` for non-360° cameras.

    API: PUT /v11/video_inputs/{id}/autofollow  Body: ``{"result": bool}``.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    pan_limit = cam_info.get("pan_limit", 0)
    if not pan_limit:
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' does not support auto-follow (panLimit=0). "
                "Auto-follow is only available on 360° indoor cameras (CAMERA_360)."
            ),
            camera=name,
        )
    try:
        br.set_autofollow(session, cam_info["id"], enabled=enabled)
        raw = br.get_autofollow(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return AutofollowConfig(enabled=bool(raw.get("result", False)))


@mcp.tool()
def bosch_camera_privacy_sound_get(camera: str) -> PrivacySoundConfig:
    """Get the privacy-sound indicator setting for one camera.

    When enabled, the camera plays an audible indicator when privacy mode changes.
    Returns ``{enabled}``.

    API: GET /v11/video_inputs/{id}/privacy_sound_override.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        raw = br.get_privacy_sound(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return PrivacySoundConfig(enabled=bool(raw.get("result", False)))


@mcp.tool()
def bosch_camera_privacy_sound_set(camera: str, enabled: bool) -> PrivacySoundConfig:
    """Enable or disable the audible indicator that plays when privacy mode changes.

    ``enabled=True``  → camera beeps/chimes when privacy is toggled.
    ``enabled=False`` → silent privacy mode switching.

    API: PUT /v11/video_inputs/{id}/privacy_sound_override  Body: ``{"result": bool}``.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        br.set_privacy_sound(session, cam_info["id"], enabled=enabled)
        raw = br.get_privacy_sound(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return PrivacySoundConfig(enabled=bool(raw.get("result", False)))


@mcp.tool()
def bosch_camera_unread_get(camera: str) -> UnreadCount:
    """Get the unread event count for one camera.

    Reads ``numberOfUnreadEvents`` from the ``/v11/video_inputs`` listing
    (the ``/unread_events_count`` endpoint returns HTTP 404 — verified in testing).

    Returns ``{camera, count}``.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    from .adapters.cli_bridge import CLOUD_API  # noqa: PLC0415

    name, cam_info = br._resolve_cam(cameras, camera)
    cam_id = cam_info["id"]

    # Fetch from the video_inputs listing which contains numberOfUnreadEvents per cam
    r = session.get(f"{CLOUD_API}/v11/video_inputs", timeout=15)
    if r.status_code == 401:
        raise MCPError(
            code="reauth_required",
            detail="API returned 401 fetching video_inputs for unread count.",
        )
    r.raise_for_status()

    count = 0
    for cam in r.json():
        if cam.get("id") == cam_id:
            count = int(cam.get("numberOfUnreadEvents", 0))
            break

    return UnreadCount(camera=name, count=count)


@mcp.tool()
def bosch_camera_health_check_all() -> list[CameraHealthEntry]:
    """Bulk health check for ALL configured cameras in one call.

    Returns status + WiFi signal + privacy mode + last event + unread count
    for each camera. Replaces 4+ separate MCP calls for dashboard use.

    Errors per camera are captured in the ``error`` field rather than raising,
    so a single failing camera does not abort the entire check.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    import bosch_camera as bc  # type: ignore[import-not-found]
    from .adapters.cli_bridge import CLOUD_API  # noqa: PLC0415

    # Fetch the full /v11/video_inputs listing once for unread counts + status
    listing_by_id: dict[str, dict] = {}
    try:
        r = session.get(f"{CLOUD_API}/v11/video_inputs", timeout=15)
        if r.status_code == 200:
            for cam in r.json():
                cid = cam.get("id", "")
                if cid:
                    listing_by_id[cid] = cam
    except Exception:
        pass  # Best-effort; individual status calls will still work

    results: list[CameraHealthEntry] = []
    for name, cam_info in cameras.items():
        cam_id = cam_info.get("id", "")
        try:
            # Status (online/offline)
            status = bc.api_ping(session, cam_id) if cam_id else "UNKNOWN"

            # Detail for privacy mode (may come from listing or dedicated call)
            listing_entry = listing_by_id.get(cam_id, {})
            privacy_raw = listing_entry.get("privacyMode", "")
            if not privacy_raw:
                detail = bc.api_get_camera(session, cam_id) or {}
                privacy_raw = detail.get("privacyMode", "OFF")
            privacy_mode = privacy_raw.upper() == "ON"

            # WiFi (best-effort — some cameras may not support it)
            wifi_rssi: Optional[int] = None
            wifi_strength: Optional[int] = None
            try:
                wifi_raw = br.get_wifi_info(session, cam_id)
                rssi_raw = wifi_raw.get("rssi") or wifi_raw.get("signalStrength")
                if rssi_raw is not None:
                    wifi_rssi = int(rssi_raw)
                    wifi_strength = wifi_raw.get("signal_strength")
            except Exception:
                pass

            # Last event (best-effort)
            last_event_at: Optional[str] = None
            try:
                events = bc.api_get_events(session, cam_id, limit=1)
                if events:
                    ts_raw = events[0].get("timestamp", "")
                    last_event_at = ts_raw[:19] if ts_raw else None
            except Exception:
                pass

            # Unread count from listing
            unread_count = int(listing_entry.get("numberOfUnreadEvents", 0))

            results.append(CameraHealthEntry(
                name=name,
                status=status,
                privacy_mode=privacy_mode,
                wifi_rssi=wifi_rssi,
                wifi_signal_strength=wifi_strength,
                last_event_at=last_event_at,
                unread_count=unread_count,
            ))
        except Exception as exc:
            results.append(CameraHealthEntry(
                name=name,
                status="UNKNOWN",
                privacy_mode=False,
                error=str(exc),
            ))

    return results


@mcp.tool()
def bosch_camera_token_status() -> TokenStatus:
    """Return the current bearer token validity, expiry, and account email.

    Parses the JWT ``exp`` and ``email``/``preferred_username`` claims from the
    stored bearer token without making a network call.

    Returns ``{valid, expires_in_min, email}``.
    ``valid=False`` when the token is expired or missing.
    ``expires_in_min`` is negative when already expired.
    """
    import base64 as _b64
    import json as _json_mod
    import datetime as _dt

    br = _bridge()
    cfg, session, cameras = _get_session()

    token = cfg.get("account", {}).get("bearer_token", "").strip()
    if not token:
        return TokenStatus(valid=False, expires_in_min=None, email=None)

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return TokenStatus(valid=False, expires_in_min=None, email=None)
        pad = len(parts[1]) % 4
        info = _json_mod.loads(_b64.urlsafe_b64decode(parts[1] + "=" * pad))
        exp = info.get("exp", 0)
        email = info.get("email") or info.get("preferred_username")
        if exp:
            exp_dt = _dt.datetime.fromtimestamp(exp)
            diff = exp_dt - _dt.datetime.now()
            mins = int(diff.total_seconds() / 60)
            return TokenStatus(valid=mins > 0, expires_in_min=mins, email=email)
        return TokenStatus(valid=True, expires_in_min=None, email=email)
    except Exception:
        return TokenStatus(valid=False, expires_in_min=None, email=None)


# ── Register resources + prompts ──────────────────────────────────────────────
# Importing these modules causes their @mcp.resource / @mcp.prompt decorators
# to execute, self-registering against the shared `mcp` FastMCP instance above.
# Must be imported AFTER `mcp` is defined and AFTER all tool definitions so that
# resources.py can import bosch_camera_snapshot from this module without a
# circular import.
from . import resources, prompts  # noqa: E402,F401  — side-effect imports

# ── CLI entrypoint ────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bosch-smart-home-camera-mcp",
        description=f"Bosch Smart Home Camera MCP server (v{__version__})",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("BOSCH_CAMERA_CONFIG"),
        help="Path to bosch_config.json (env BOSCH_CAMERA_CONFIG also accepted)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging to stderr",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport protocol: stdio (default, Claude Code/Desktop), "
             "http (streamable-HTTP, remote/multi-client), "
             "sse (legacy SSE)",
    )
    parser.add_argument(
        "--http-host",
        default="127.0.0.1",
        dest="http_host",
        help="Bind host for HTTP/SSE transports (default: 127.0.0.1). "
             "WARNING: set to 0.0.0.0 only in trusted network environments.",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8765,
        dest="http_port",
        help="Listen port for HTTP/SSE transports (default: 8765)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint declared in pyproject.toml [project.scripts]."""
    global _CONFIG_PATH
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _CONFIG_PATH = args.config

    if args.transport in ("http", "sse"):
        # Reconfigure the shared FastMCP instance with the requested host/port.
        # FastMCP stores host+port in mcp.settings (pydantic model); the cleanest
        # way to override them without re-creating the instance (which would lose
        # all registered tools/resources/prompts) is to mutate settings directly.
        mcp.settings.host = args.http_host
        mcp.settings.port = args.http_port
        if args.http_host != "127.0.0.1":
            logger.warning(
                "HTTP transport bound to %s — ensure this host is firewalled.",
                args.http_host,
            )

    logger.info(
        "starting bosch-smart-home-camera-mcp v%s transport=%s config=%s",
        __version__,
        args.transport,
        args.config,
    )

    if args.transport == "http":
        mcp.run(transport="streamable-http")
    elif args.transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")

    return 0


if __name__ == "__main__":
    sys.exit(main())
