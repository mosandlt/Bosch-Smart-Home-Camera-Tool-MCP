"""MCP server entrypoint.

Tool bodies are wired to the sister CLI's bosch_camera.py via
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
import functools
import logging
import os
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Optional, ParamSpec, TypeVar

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .errors import MCPError
from .time_utils import clean_bosch_timestamp

if TYPE_CHECKING:
    import requests

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
    method: str = Field(
        description="Source: local_lan (LAN-only; cloud fallback removed in v1.1.0)"
    )
    timestamp: str = Field(description="ISO 8601 capture time")


class StreamUrlResult(BaseModel):
    """LAN RTSPS stream URL result."""

    camera: str = Field(description="Canonical camera name from config")
    rtsps_url: str = Field(
        description="LAN RTSPS URL via TLS proxy (rtsps://<user>:<pass>@<ip>:443/...)"
    )
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
    distance: Optional[int] = Field(default=None, description="Detection distance in meters (1-8)")


class AudioDetectionConfig(BaseModel):
    """Glass-break + smoke/fire-alarm sound detection config returned by
    bosch_camera_audio_detection_get.

    Gen2 Audio-Plus feature (ported from HA integration v14.2.0).
    """

    glass_break: Optional[bool] = Field(
        default=None,
        description="Whether glass-break sound detection is enabled (Bosch API field 'detectGlassBreak')",
    )
    fire_alarm: Optional[bool] = Field(
        default=None,
        description="Whether smoke/fire-alarm sound detection is enabled (Bosch API field 'detectFireAlarm')",
    )


class WifiInfo(BaseModel):
    """WiFi signal information returned by bosch_camera_wifi."""

    rssi: Optional[int] = Field(default=None, description="Raw RSSI in dBm (negative; e.g. -67)")
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
    error: Optional[str] = Field(default=None, description="Set when this camera's check failed")


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


class MotionZone(BaseModel):
    """One motion-detection zone rectangle (bosch_camera_motion_zones_get/set).

    Range constraints (bug-hunt finding 2026-07-11: the _set tools originally
    only checked that x/y/w/h keys were present, never that the values were
    numeric-sane — unlike the sibling bosch_camera_intrusion_set, which
    range-checks sensitivity/distance before ever calling the bridge). Also
    used on the read path (via _zone_dicts_to_models) to skip a malformed
    entry the API returns rather than crash the whole read.
    """

    x: float = Field(ge=0.0, le=1.0, description="Left edge, normalized 0.0-1.0")
    y: float = Field(ge=0.0, le=1.0, description="Top edge, normalized 0.0-1.0")
    w: float = Field(gt=0.0, le=1.0, description="Width, normalized 0.0-1.0")
    h: float = Field(gt=0.0, le=1.0, description="Height, normalized 0.0-1.0")


class PrivacyMask(BaseModel):
    """One privacy-mask zone rectangle (bosch_camera_privacy_masks_get/set).

    Same range constraints as MotionZone — see its docstring.
    """

    x: float = Field(ge=0.0, le=1.0, description="Left edge, normalized 0.0-1.0")
    y: float = Field(ge=0.0, le=1.0, description="Top edge, normalized 0.0-1.0")
    w: float = Field(gt=0.0, le=1.0, description="Width, normalized 0.0-1.0")
    h: float = Field(gt=0.0, le=1.0, description="Height, normalized 0.0-1.0")


class Rule(BaseModel):
    """One camera automation (time-schedule) rule."""

    id: Optional[str] = Field(default=None, description="Rule id (Bosch API field 'id')")
    name: str = Field(description="Rule display name")
    active: bool = Field(description="Whether the rule is currently active")
    start: str = Field(description="Start time HH:MM:SS (Bosch API field 'startTime')")
    end: str = Field(description="End time HH:MM:SS (Bosch API field 'endTime')")
    days: list[int] = Field(description="Active weekdays, 0=Monday .. 6=Sunday")


class Friend(BaseModel):
    """One camera-sharing friend/invitation entry."""

    id: str = Field(description="Friend id (Bosch API field 'id')")
    email: Optional[str] = Field(default=None, description="Friend's email address")
    nickname: Optional[str] = Field(default=None, description="Display nickname")
    status: Optional[str] = Field(
        default=None, description="Invitation status, e.g. ACCEPTED/PENDING"
    )
    shared_cameras: list[str] = Field(
        default_factory=list, description="Camera ids currently shared with this friend"
    )


class FirmwareStatus(BaseModel):
    """Firmware status returned by bosch_camera_firmware_status/install."""

    camera: str = Field(description="Camera name")
    current: Optional[str] = Field(default=None, description="Currently installed version")
    up_to_date: Optional[bool] = Field(
        default=None, description="Whether the camera is already on the latest firmware"
    )
    update_available: Optional[str] = Field(
        default=None, description="Pending update's target version, if any"
    )
    installing: bool = Field(
        default=False, description="Whether an install is currently in progress"
    )


class TimestampOverlayConfig(BaseModel):
    """Timestamp overlay config returned by bosch_camera_timestamp_overlay_get/set."""

    enabled: bool = Field(description="Whether a date/time overlay is burned into the video")


class StatusLedConfig(BaseModel):
    """Status LED config returned by bosch_camera_status_led_get/set (Gen2 only)."""

    enabled: bool = Field(description="Whether the camera's status LED is lit")


class LensElevationConfig(BaseModel):
    """Lens mounting height returned by bosch_camera_lens_elevation_get/set (Gen2 only)."""

    meters: float = Field(
        description="Mounting height in meters, used for person-detection perspective correction"
    )


class DarknessThresholdConfig(BaseModel):
    """Day/night lighting config returned by bosch_camera_darkness_threshold_get/set (Gen2 only)."""

    threshold_percent: float = Field(
        description="Ambient-darkness trigger threshold, 0=always day .. 100=always night"
    )
    soft_light_fading: bool = Field(
        description="Whether lights fade smoothly (True) or snap on/off (False)"
    )


class WhiteBalanceConfig(BaseModel):
    """Front light white balance returned by bosch_camera_white_balance_get/set (Gen2 only)."""

    value: float = Field(description="-1.0 (cool/blue) .. 1.0 (warm/orange), 0.0 = neutral")


class LedBrightnessConfig(BaseModel):
    """Top/bottom LED brightness returned by bosch_camera_led_brightness_get/set (Gen2 only)."""

    position: str = Field(description="'top' or 'bottom'")
    brightness_percent: float = Field(description="0-100 %")


class LightingSchedule(BaseModel):
    """Lighting schedule returned by bosch_camera_lighting_schedule_get/set.

    Outdoor (Eyes) cameras with LED light only. API: GET/PUT
    /v11/video_inputs/{id}/lighting_options.
    """

    schedule_status: Optional[str] = Field(
        default=None, description="Bosch API field 'scheduleStatus', e.g. FOLLOW_SCHEDULE"
    )
    on_time: Optional[str] = Field(
        default=None, description="Light-on time HH:MM:SS (Bosch API field 'generalLightOnTime')"
    )
    off_time: Optional[str] = Field(
        default=None, description="Light-off time HH:MM:SS (Bosch API field 'generalLightOffTime')"
    )
    darkness_threshold: Optional[float] = Field(
        default=None, description="0.0-1.0 ambient-darkness trigger threshold"
    )
    light_on_motion: Optional[bool] = Field(
        default=None, description="Whether motion also triggers the light"
    )
    light_on_motion_followup_secs: Optional[int] = Field(
        default=None, description="Seconds the light stays on after motion (read-only)"
    )
    front_illuminator_on: Optional[bool] = Field(
        default=None, description="Front illuminator state during general-light-on (read-only)"
    )
    front_illuminator_intensity: Optional[int] = Field(
        default=None, description="Front illuminator intensity (read-only)"
    )
    wallwasher_on: Optional[bool] = Field(
        default=None, description="Wallwasher light state during general-light-on (read-only)"
    )


class SirenDuration(BaseModel):
    """Siren alarm duration returned by bosch_camera_siren_duration_set."""

    alarm_delay_seconds: int = Field(
        description="Configured siren duration in seconds (Bosch API field 'alarmDelayInSeconds')"
    )


class IntercomSession(BaseModel):
    """Listen-audio session tunnel returned by bosch_camera_intercom_open.

    This is a **listen-only** audio tunnel (camera microphone -> caller).
    True two-way talk is not exposed via the Bosch cloud API (same
    limitation as the Python CLI's own `intercom` command).
    """

    camera: str = Field(description="Canonical camera name")
    rtsps_url: str = Field(
        description="Cloud-proxy RTSPS URL with enableaudio=1, consumable by ffmpeg/ffplay/VLC"
    )
    duration: int = Field(description="Requested session duration in seconds")
    speaker_level_set: Optional[int] = Field(
        default=None,
        description="Speaker level actually applied (0-100), or None if not requested/failed",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

_P = ParamSpec("_P")
_R = TypeVar("_R")

# All cloud-backed tool bodies share the bridge's requests.Session, which is not
# safe under concurrent multi-threaded use (cookie jar / token refresh races), so
# the offloaded bodies are serialized behind one lock.
_BLOCKING_TOOL_LOCK = threading.Lock()


def _locked(fn: Callable[_P, _R], /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
    """Run *fn* while holding the shared blocking-tool lock."""
    with _BLOCKING_TOOL_LOCK:
        return fn(*args, **kwargs)


def _blocking_tool(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Register a blocking (requests-based) tool body without stalling the event loop.

    FastMCP executes sync tool functions directly on the event-loop thread
    (mcp 1.27.2 func_metadata.py: ``return fn(**arguments_parsed_dict)``), so a
    blocking HTTP call inside a sync tool freezes the whole server for every
    parallel client. This decorator registers an *async* wrapper that offloads
    the body via ``asyncio.to_thread``; ``functools.wraps`` preserves name,
    docstring and signature (FastMCP introspects through ``__wrapped__``), so
    the published tool schema is unchanged. The module-level name keeps
    pointing at the plain sync function for direct in-process calls.
    """

    @functools.wraps(fn)
    async def _async_tool(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await asyncio.to_thread(_locked, fn, *args, **kwargs)

    mcp.tool()(_async_tool)
    return fn


def _bridge() -> ModuleType:
    """Lazy import of the bridge module (keeps import errors at call-time)."""
    from .adapters import cli_bridge  # noqa: PLC0415

    return cli_bridge


def _get_session(
    config_path: Optional[str] = None,
) -> tuple[dict[str, Any], requests.Session, dict[str, dict[str, Any]]]:
    """Return (cfg, session, cameras_dict); wraps reauth_required into MCPError."""
    br = _bridge()
    return br.get_session_and_cameras(config_path or _CONFIG_PATH)  # type: ignore[no-any-return]


def _build_status(
    name: str,
    cam_info: dict[str, Any],
    session: requests.Session,
    _cfg: dict[str, Any],
) -> CameraStatus:
    """Build a CameraStatus model for one camera."""
    import bosch_camera as bc

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
            last_event_at = clean_bosch_timestamp(ts_raw) or None
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


@_blocking_tool
def bosch_camera_list() -> list[CameraSummary]:
    """List all configured Bosch cameras with their online status."""
    _cfg, session, cameras = _get_session()

    import bosch_camera as bc

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


def _require_gen2(cam_info: dict[str, Any], name: str, feature: str) -> None:
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


@_blocking_tool
def bosch_camera_status(camera: str) -> CameraStatus:
    """Get the current status of one camera by name (case-insensitive)."""
    br = _bridge()
    cfg, session, cameras = _get_session()

    _bridge().ensure_cli_importable()
    name, cam_info = br._resolve_cam(cameras, camera)
    return _build_status(name, cam_info, session, cfg)


@_blocking_tool
def bosch_camera_snapshot(camera: str) -> SnapshotResult:
    """Capture a fresh snapshot via LAN only (HTTP Digest to camera IP). No Bosch cloud roundtrip.

    Requires the MCP host to be on the same network as the camera. Saves to
    ~/.cache/bosch-camera-mcp/snapshots/<camera>/<iso-ts>.jpg.
    """
    br = _bridge()
    _cfg, _session, cameras = _get_session()
    br.ensure_cli_importable()

    import bosch_camera as bc

    name, cam_info = br._resolve_cam(cameras, camera)

    # Build cache directory
    safe_name = name.replace(" ", "_")
    cache_dir = Path.home() / ".cache" / "bosch-camera-mcp" / "snapshots" / safe_name
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


@_blocking_tool
def bosch_camera_stream_url(camera: str) -> StreamUrlResult:
    """Get the LAN RTSPS stream URL for one camera. No Bosch cloud relay.

    The returned URL is consumable by ffmpeg/VLC/go2rtc. Requires that the MCP
    host runs on the same network as the camera and has local credentials
    configured for the camera (bosch_config.json → cameras[name].local_*).
    """
    from urllib.parse import quote as _q

    br = _bridge()
    _cfg, _session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_events(camera: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent motion / person / audio events for one camera.

    Each item contains: event_id, type, timestamp_iso, has_clip.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    import bosch_camera as bc

    _name, cam_info = br._resolve_cam(cameras, camera)
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
                "timestamp_iso": clean_bosch_timestamp(ts_raw),
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
        _cfg, _session, cameras = await asyncio.to_thread(_locked, _get_session)
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


def _privacy_set_cloud(
    session: requests.Session,
    cfg: dict[str, Any],
    name: str,
    cam_info: dict[str, Any],
    enabled: bool,
) -> CameraStatus:
    """Blocking cloud write + readback-poll for privacy mode (runs in a worker thread)."""
    br = _bridge()
    br.set_privacy_mode(session, cam_info["id"], enabled)
    # Cloud occasionally lags behind the PUT — poll briefly until the camera detail
    # reflects the requested state, so we don't return stale `privacy_mode` to the agent.
    import time as _time  # noqa: PLC0415 — local import to keep top of module clean
    import bosch_camera as bc  # noqa: PLC0415

    expected = "ON" if enabled else "OFF"
    for _ in range(10):  # 10 × 0.5 s = 5 s budget
        detail = bc.api_get_camera(session, cam_info["id"]) or {}
        if str(detail.get("privacyMode", "")).upper() == expected:
            break
        _time.sleep(0.5)
    return _build_status(name, cam_info, session, cfg)


def _light_set_cloud(
    session: requests.Session,
    cfg: dict[str, Any],
    name: str,
    cam_info: dict[str, Any],
    enabled: bool,
) -> CameraStatus:
    """Blocking cloud write for the spotlight (runs in a worker thread)."""
    br = _bridge()
    br.set_light(session, cam_info["id"], enabled)
    return _build_status(name, cam_info, session, cfg)


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
    cfg, session, cameras = await asyncio.to_thread(_locked, _get_session)
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
                local_ip,
                enabled,
                user=local_user,
                password=local_pass,
                on_401=_on_401_privacy,
            )
            if ok:
                logger.info(
                    "privacy_set(%s, %s): succeeded via LOCAL RCP (%s)", name, enabled, local_ip
                )
                return await asyncio.to_thread(_locked, _build_status, name, cam_info, session, cfg)
            logger.warning(
                "privacy_set(%s, %s): LOCAL RCP failed (%s), falling back to cloud",
                name,
                enabled,
                local_ip,
            )

    return await asyncio.to_thread(
        _locked, _privacy_set_cloud, session, cfg, name, cam_info, enabled
    )


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
    cfg, session, cameras = await asyncio.to_thread(_locked, _get_session)
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
                local_ip,
                brightness,
                user=local_user,
                password=local_pass,
                on_401=_on_401_light,
            )
            if ok:
                logger.info(
                    "light_set(%s, %s): succeeded via LOCAL RCP (%s)", name, enabled, local_ip
                )
                return await asyncio.to_thread(_locked, _build_status, name, cam_info, session, cfg)
            logger.warning(
                "light_set(%s, %s): LOCAL RCP failed (%s), falling back to cloud",
                name,
                enabled,
                local_ip,
            )

    return await asyncio.to_thread(_locked, _light_set_cloud, session, cfg, name, cam_info, enabled)


@_blocking_tool
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


@_blocking_tool
def bosch_camera_siren_trigger(camera: str, stop: bool = False) -> CameraStatus:
    """Trigger (or stop) the indoor siren on a camera.

    Endpoint depends on the camera model:

    - **Gen1 360° indoor** (``model == "INDOOR"``) → ``PUT /v11/video_inputs/{id}/acoustic_alarm``
      Body: ``{"enabled": <bool>}``.  Outdoor cameras return HTTP 442 (not supported).
    - **Gen2 Indoor II** (``model == "HOME_Eyes_Indoor"``) → ``PUT /v11/video_inputs/{id}/panic_alarm``
      Body: ``{"status": "ON"|"OFF"}``. 75 dB integrated siren.

    The siren plays for the camera-side configured duration (typically 30–60 s
    on Gen2). To change the duration, use ``bosch_camera_siren_duration_set``
    (range 10–300 s) before triggering.

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


@_blocking_tool
def bosch_camera_siren_duration_set(camera: str, seconds: int) -> SirenDuration:
    """Set the siren alarm duration for one camera (Gen2 Indoor II only).

    Read-modify-write on ``GET/PUT /v11/video_inputs/{id}/alarm_settings``
    (field ``alarmDelayInSeconds``) — existing fields in that config are
    preserved. Does **not** trigger the siren itself; call
    ``bosch_camera_siren_trigger`` afterwards to fire it with the new duration.

    Raises ``hardware_unsupported`` for non-Gen2-Indoor-II cameras (matches
    ``bosch_camera_siren_trigger``'s own gating). Raises ``invalid_argument``
    if ``seconds`` is outside 10-300. Raises ``privacy_blocked`` when the
    camera is in privacy mode.

    Args:
        camera: Camera name (case-insensitive).
        seconds: New siren duration, 10-300 seconds inclusive.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    model = cam_info.get("model", "")
    if model != "HOME_Eyes_Indoor":
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model or 'unknown'}') has no siren duration support. "
                "Only Gen2 Indoor II (HOME_Eyes_Indoor) exposes /alarm_settings."
            ),
            camera=name,
        )
    if not 10 <= seconds <= 300:
        raise MCPError(
            code="invalid_argument",
            detail=f"seconds must be between 10 and 300 (got {seconds}).",
            camera=name,
        )

    try:
        result = br.set_siren_duration(session, cam_info["id"], seconds)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise

    logger.info("siren_duration_set(%s, seconds=%s): OK", name, seconds)
    return SirenDuration(alarm_delay_seconds=result.get("alarmDelayInSeconds", seconds))


@_blocking_tool
def bosch_camera_lighting_schedule_get(camera: str) -> LightingSchedule:
    """Get the LED lighting schedule for one outdoor (Eyes) camera.

    API: ``GET /v11/video_inputs/{id}/lighting_options``. Only available on
    outdoor cameras with LED light — raises ``hardware_unsupported`` (HTTP 442)
    on cameras without this feature, and ``api_unreachable`` (HTTP 444) if the
    camera is offline.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        raw = br.get_lighting_schedule(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _lighting_dict_to_model(raw)


@_blocking_tool
def bosch_camera_lighting_schedule_set(
    camera: str,
    on_time: Optional[str] = None,
    off_time: Optional[str] = None,
    light_on_motion: Optional[bool] = None,
    darkness_threshold: Optional[float] = None,
) -> LightingSchedule:
    """Update the LED lighting schedule for one outdoor (Eyes) camera.

    Read-modify-write on ``GET/PUT /v11/video_inputs/{id}/lighting_options``.
    At least one of ``on_time``/``off_time``/``light_on_motion``/
    ``darkness_threshold`` must be provided. Writing any field forces
    ``scheduleStatus`` to ``FOLLOW_SCHEDULE`` (matches CLI behavior).

    Only available on outdoor cameras with LED light — raises
    ``hardware_unsupported`` (HTTP 442) otherwise, ``api_unreachable``
    (HTTP 444) if the camera is offline.

    Args:
        camera: Camera name (case-insensitive).
        on_time: Light-on time, "HH:MM" or "HH:MM:SS".
        off_time: Light-off time, "HH:MM" or "HH:MM:SS".
        light_on_motion: Whether motion also triggers the light.
        darkness_threshold: Ambient-darkness trigger threshold, 0.0-1.0.
    """
    if (
        on_time is None
        and off_time is None
        and light_on_motion is None
        and darkness_threshold is None
    ):
        raise MCPError(
            code="invalid_argument",
            detail="At least one of on_time, off_time, light_on_motion, darkness_threshold must be provided.",
        )
    if darkness_threshold is not None and not 0.0 <= darkness_threshold <= 1.0:
        raise MCPError(
            code="invalid_argument",
            detail=f"darkness_threshold must be between 0.0 and 1.0 (got {darkness_threshold}).",
        )

    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    if on_time is not None:
        _validate_hhmm(on_time, "on_time", name)
    if off_time is not None:
        _validate_hhmm(off_time, "off_time", name)
    try:
        raw = br.set_lighting_schedule(
            session,
            cam_info["id"],
            on_time=on_time,
            off_time=off_time,
            light_on_motion=light_on_motion,
            darkness_threshold=darkness_threshold,
        )
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    logger.info("lighting_schedule_set(%s): OK", name)
    return _lighting_dict_to_model(raw)


@_blocking_tool
def bosch_camera_intercom_open(
    camera: str,
    duration: int = 60,
    speaker_level: Optional[int] = None,
) -> IntercomSession:
    """Open a listen-audio session tunnel to a camera (cloud proxy).

    **Listen-only** — returns an RTSPS URL streaming the camera's microphone
    audio to the caller. True two-way talk (caller mic -> camera speaker) is
    not exposed via the Bosch cloud API (same limitation as the Python CLI's
    own `intercom` command — it falls back to `ffplay` for listen-only
    playback). The returned URL is consumable by ffmpeg/ffplay/VLC; the MCP
    server does not itself play audio.

    Flow: optionally sets the camera's speaker level (full-body PUT to
    ``/audio``, preserving other fields), then opens a live connection via
    ``PUT /v11/video_inputs/{id}/connection`` (tries REMOTE then LOCAL) and
    builds an ``rtsps://`` URL with ``enableaudio=1``.

    Args:
        camera: Camera name (case-insensitive).
        duration: Requested session duration in seconds (embedded in the URL
            as ``maxSessionDuration``), default 60.
        speaker_level: Optional 0-100 speaker level to set before opening
            the session.
    """
    if speaker_level is not None and not 0 <= speaker_level <= 100:
        raise MCPError(
            code="invalid_argument",
            detail=f"speaker_level must be between 0 and 100 (got {speaker_level}).",
        )
    if duration <= 0:
        raise MCPError(
            code="invalid_argument",
            detail=f"duration must be positive (got {duration}).",
        )

    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        result = br.open_intercom_session(
            session, cam_info["id"], duration=duration, speaker_level=speaker_level
        )
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise

    logger.info("intercom_open(%s, duration=%s): OK", name, duration)
    return IntercomSession(
        camera=name,
        rtsps_url=result["rtsps_url"],
        duration=result["duration"],
        speaker_level_set=result.get("speaker_level_set"),
    )


@_blocking_tool
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


@_blocking_tool
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
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
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
    _cfg, session, cameras = _get_session()
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
        if val is not None and not 0 <= val <= 100:
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


@_blocking_tool
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
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
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
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "intrusion detection")

    if mode is None and sensitivity is None and distance is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of mode, sensitivity, or distance must be provided.",
            camera=name,
        )

    if sensitivity is not None and not 0 <= sensitivity <= 7:
        raise MCPError(
            code="invalid_argument",
            detail=f"sensitivity={sensitivity} is out of range. Must be 0-7.",
            camera=name,
        )
    if distance is not None and not 1 <= distance <= 8:
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


@_blocking_tool
def bosch_camera_audio_detection_get(camera: str) -> AudioDetectionConfig:
    """Get the glass-break + smoke/fire-alarm sound detection config for one Gen2 camera.

    Only available on Gen2 Audio-Plus cameras (``featureSupport.sound=true``).
    Raises ``hardware_unsupported`` for Gen1 cameras. Ported from HA
    integration v14.2.0 (BoschGlassBreakDetectionSwitch / BoschFireAlarmDetectionSwitch).

    Returns ``{glass_break, fire_alarm}`` (Bosch API fields ``detectGlassBreak`` /
    ``detectFireAlarm``).
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "audio detection (glass-break / fire-alarm)")

    try:
        raw = br.get_audio_detection_config(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return AudioDetectionConfig(
        glass_break=raw.get("detectGlassBreak"),
        fire_alarm=raw.get("detectFireAlarm"),
    )


@_blocking_tool
def bosch_camera_audio_detection_set(
    camera: str,
    glass_break: Optional[bool] = None,
    fire_alarm: Optional[bool] = None,
) -> AudioDetectionConfig:
    """Update glass-break and/or fire-alarm sound detection for one Gen2 camera.

    Gen2 Audio-Plus-only feature — raises ``hardware_unsupported`` for Gen1
    cameras. At least one of ``glass_break`` or ``fire_alarm`` must be provided.

    Read-modify-write: the current config is fetched first, only the provided
    field(s) are merged in, and both ``detectGlassBreak`` and ``detectFireAlarm``
    are always sent together on the PUT — Bosch's API silently resets whichever
    field is omitted (same requirement as intrusionDetectionConfig).

    Returns the updated ``{glass_break, fire_alarm}`` after write.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    _require_gen2(cam_info, name, "audio detection (glass-break / fire-alarm)")

    if glass_break is None and fire_alarm is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of glass_break or fire_alarm must be provided.",
            camera=name,
        )

    try:
        br.set_audio_detection_config(
            session, cam_info["id"], glass_break=glass_break, fire_alarm=fire_alarm
        )
        raw = br.get_audio_detection_config(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return AudioDetectionConfig(
        glass_break=raw.get("detectGlassBreak"),
        fire_alarm=raw.get("detectFireAlarm"),
    )


@_blocking_tool
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
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    _name, cam_info = br._resolve_cam(cameras, camera)
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

    Uses httpx with Digest auth and TLS verify disabled (cameras use
    self-signed certs). Returns the raw response body bytes on HTTP 200,
    or None on any error (network, auth, non-200).

    httpx (not aiohttp) is the digest stack here: aiohttp exposes no public
    ``DigestAuth`` class (only ``DigestAuthMiddleware``), so the previous
    ``aiohttp.DigestAuth`` always raised AttributeError and the broad
    ``except`` silently returned None — this READ path never worked in
    production. The sibling lan_rcp.py module already uses ``httpx.DigestAuth``.

    Used by bosch_camera_onvif_scopes (0x0a98) and bosch_camera_rcp_version
    (0xff00 / 0xff04). Pass opcode_hex with or without leading "0x".
    """
    import httpx

    if not opcode_hex.lower().startswith("0x"):
        opcode_hex = "0x" + opcode_hex

    url = f"https://{cam_ip}/rcp.xml"
    params = {
        "command": opcode_hex,
        "direction": "READ",
        "type": "P_OCTET",
    }
    try:
        auth = httpx.DigestAuth(user, password)
        async with httpx.AsyncClient(  # noqa: S501 — self-signed cam cert
            verify=False,
            timeout=httpx.Timeout(5.0),
            auth=auth,
        ) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return bytes(resp.content)
            logger.debug(
                "_fetch_rcp_lan: %s@%s HTTP %d",
                opcode_hex,
                cam_ip,
                resp.status_code,
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
    _cfg, _session, cameras = await asyncio.to_thread(_locked, _get_session)
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
    rtsp_url = f"rtsps://{auth_prefix}{local_ip}:443/rtsp_tunnel?inst=3&enableaudio=0&fmtp=1"

    safe_name = name.replace(" ", "_")
    cache_dir = Path.home() / ".cache" / "bosch-camera-mcp" / "snapshots" / safe_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    ts_fs = now.strftime("%Y-%m-%dT%H-%M-%S")
    ts_iso = now.isoformat(timespec="seconds")
    out_path = cache_dir / f"{ts_fs}_mjpeg.jpg"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-vframes",
            "1",
            "-f",
            "image2",
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
    except asyncio.TimeoutError as exc:
        raise MCPError(
            code="local_unavailable",
            detail=f"ffmpeg MJPEG snapshot timed out for {name!r} after 20 s.",
            camera=name,
        ) from exc

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
    _cfg, _session, cameras = await asyncio.to_thread(_locked, _get_session)
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
    _cfg, _session, cameras = await asyncio.to_thread(_locked, _get_session)
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


@_blocking_tool
def bosch_camera_feature_flags() -> dict[str, Any]:
    """Fetch account-level Bosch cloud feature flags from GET /v11/feature_flags.

    Returns the raw dict of feature flag names to boolean values, e.g.
    {"APP_RATING": true, "IOT_THINGS_INTEGRATION": true, ...}.
    No camera parameter — flags are account-level.
    Useful for discovering which Bosch platform features are active for this account.
    """
    br = _bridge()
    _cfg, session, _cameras = _get_session()
    br.ensure_cli_importable()

    from .adapters.cli_bridge import CLOUD_API  # noqa: PLC0415

    r = session.get(f"{CLOUD_API}/v11/feature_flags", timeout=10)
    if r.status_code == 200:
        data = r.json()
        # API may return a list of {name, enabled} or a flat dict — normalise both
        if isinstance(data, list):
            return {
                item.get("name", str(i)): bool(item.get("enabled", False))
                for i, item in enumerate(data)
            }
        if isinstance(data, dict):
            return {k: bool(v) for k, v in data.items()}
        return {"raw": data}

    from .adapters.cli_bridge import _raise_api_error  # noqa: PLC0415

    _raise_api_error(r, "bosch_camera_feature_flags")
    return {}  # unreachable


# ── v1.6.0 tools: motion, recording, autofollow, privacy_sound, unread, health_check_all, token_status ──


@_blocking_tool
def bosch_camera_motion_get(camera: str) -> MotionConfig:
    """Get motion detection settings for one camera.

    Returns ``{enabled, sensitivity}`` where sensitivity is one of:
    ``OFF | LOW | MEDIUM_LOW | MEDIUM_HIGH | HIGH | SUPER_HIGH``.

    API: GET /v11/video_inputs/{id}/motion.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
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
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    if enabled is None and sensitivity is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of enabled or sensitivity must be provided.",
            camera=name,
        )

    valid_sensitivities = {"OFF", "LOW", "MEDIUM_LOW", "MEDIUM_HIGH", "HIGH", "SUPER_HIGH"}
    if sensitivity is not None and sensitivity.upper() not in valid_sensitivities:
        raise MCPError(
            code="invalid_argument",
            detail=(
                f"sensitivity={sensitivity!r} is not valid. "
                f"Must be one of: {', '.join(sorted(valid_sensitivities))}."
            ),
            camera=name,
        )

    try:
        br.set_motion_config(
            session,
            cam_info["id"],
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


@_blocking_tool
def bosch_camera_recording_get(camera: str) -> RecordingOptions:
    """Get cloud recording options for one camera.

    Returns ``{sound_on}`` — whether audio is recorded in cloud clips.

    API: GET /v11/video_inputs/{id}/recording_options.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_recording_set(camera: str, sound_on: bool) -> RecordingOptions:
    """Enable or disable audio in cloud recordings for one camera.

    ``sound_on=True``  → record audio in cloud clips.
    ``sound_on=False`` → no audio in cloud clips.

    API: PUT /v11/video_inputs/{id}/recording_options  Body: ``{"recordSound": bool}``.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_autofollow_get(camera: str) -> AutofollowConfig:
    """Get auto-follow (360° auto-tracking) state for one camera.

    Returns ``{enabled}``.  Only meaningful for 360° cameras with ``panLimit > 0``
    (Gen1 CAMERA_360 indoor). Raises ``hardware_unsupported`` for non-360° cameras.

    API: GET /v11/video_inputs/{id}/autofollow.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_autofollow_set(camera: str, enabled: bool) -> AutofollowConfig:
    """Enable or disable 360° auto-tracking for one camera.

    Only available on 360° indoor cameras (``panLimit > 0``).
    Raises ``hardware_unsupported`` for non-360° cameras.

    API: PUT /v11/video_inputs/{id}/autofollow  Body: ``{"result": bool}``.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_privacy_sound_get(camera: str) -> PrivacySoundConfig:
    """Get the privacy-sound indicator setting for one camera.

    When enabled, the camera plays an audible indicator when privacy mode changes.
    Returns ``{enabled}``.

    API: GET /v11/video_inputs/{id}/privacy_sound_override.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_privacy_sound_set(camera: str, enabled: bool) -> PrivacySoundConfig:
    """Enable or disable the audible indicator that plays when privacy mode changes.

    ``enabled=True``  → camera beeps/chimes when privacy is toggled.
    ``enabled=False`` → silent privacy mode switching.

    API: PUT /v11/video_inputs/{id}/privacy_sound_override  Body: ``{"result": bool}``.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_unread_get(camera: str) -> UnreadCount:
    """Get the unread event count for one camera.

    Reads ``numberOfUnreadEvents`` from the ``/v11/video_inputs`` listing
    (the ``/unread_events_count`` endpoint returns HTTP 404 — verified in testing).

    Returns ``{camera, count}``.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
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


@_blocking_tool
def bosch_camera_health_check_all() -> list[CameraHealthEntry]:
    """Bulk health check for ALL configured cameras in one call.

    Returns status + WiFi signal + privacy mode + last event + unread count
    for each camera. Replaces 4+ separate MCP calls for dashboard use.

    Errors per camera are captured in the ``error`` field rather than raising,
    so a single failing camera does not abort the entire check.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    import bosch_camera as bc
    from .adapters.cli_bridge import CLOUD_API  # noqa: PLC0415

    # Fetch the full /v11/video_inputs listing once for unread counts + status
    listing_by_id: dict[str, dict[str, Any]] = {}
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
                    last_event_at = clean_bosch_timestamp(ts_raw) or None
            except Exception:
                pass

            # Unread count from listing
            unread_count = int(listing_entry.get("numberOfUnreadEvents", 0))

            results.append(
                CameraHealthEntry(
                    name=name,
                    status=status,
                    privacy_mode=privacy_mode,
                    wifi_rssi=wifi_rssi,
                    wifi_signal_strength=wifi_strength,
                    last_event_at=last_event_at,
                    unread_count=unread_count,
                )
            )
        except Exception as exc:
            results.append(
                CameraHealthEntry(
                    name=name,
                    status="UNKNOWN",
                    privacy_mode=False,
                    error=str(exc),
                )
            )

    return results


@_blocking_tool
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

    cfg, _session, _cameras = _get_session()

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
            exp_dt = datetime.datetime.fromtimestamp(exp)
            diff = exp_dt - datetime.datetime.now()
            mins = int(diff.total_seconds() / 60)
            return TokenStatus(valid=mins > 0, expires_in_min=mins, email=email)
        return TokenStatus(valid=True, expires_in_min=None, email=email)
    except Exception:
        return TokenStatus(valid=False, expires_in_min=None, email=None)


# ── v1.7.0 tools: motion zones, privacy masks, rules, friends, firmware install ─
# Family-parity closeout (docs/family-parity-plan.md §2b, 2026-07-11): these
# mirror the sister CLI's cmd_zones/cmd_privacy_masks/cmd_rules/cmd_friends/
# cmd_firmware_update read+write commands, hitting the same /v11 cloud
# endpoints (session-authenticated, same as every other write tool above).


def _zone_dicts_to_models(zones: list[dict[str, Any]]) -> list[MotionZone]:
    """Coerce raw {x,y,w,h} dicts from the API into MotionZone models, skipping
    any malformed entry rather than failing the whole read.

    Catches both a missing key (KeyError/TypeError on the dict subscript) AND
    a present-but-wrong-typed value (pydantic ValidationError on model
    construction, e.g. ``"x": "not a number"`` or ``"x": None``) — bug-hunt
    finding 2026-07-11: only the missing-key case was originally handled,
    so a malformed-but-present value crashed the whole read instead of being
    skipped as documented.
    """
    out: list[MotionZone] = []
    for z in zones:
        try:
            out.append(MotionZone(x=z["x"], y=z["y"], w=z["w"], h=z["h"]))
        except (KeyError, TypeError, ValidationError):
            continue
    return out


def _mask_dicts_to_models(masks: list[dict[str, Any]]) -> list[PrivacyMask]:
    """Coerce raw {x,y,w,h} dicts from the API into PrivacyMask models, skipping
    any malformed entry rather than failing the whole read. See
    _zone_dicts_to_models for why both KeyError/TypeError and pydantic
    ValidationError must be caught."""
    out: list[PrivacyMask] = []
    for m in masks:
        try:
            out.append(PrivacyMask(x=m["x"], y=m["y"], w=m["w"], h=m["h"]))
        except (KeyError, TypeError, ValidationError):
            continue
    return out


def _lighting_dict_to_model(raw: dict[str, Any]) -> LightingSchedule:
    return LightingSchedule(
        schedule_status=raw.get("scheduleStatus"),
        on_time=raw.get("generalLightOnTime"),
        off_time=raw.get("generalLightOffTime"),
        darkness_threshold=raw.get("darknessThreshold"),
        light_on_motion=raw.get("lightOnMotion"),
        light_on_motion_followup_secs=raw.get("lightOnMotionFollowUpTimeSeconds"),
        front_illuminator_on=raw.get("frontIlluminatorInGeneralLightOn"),
        front_illuminator_intensity=raw.get("frontIlluminatorGeneralLightIntensity"),
        wallwasher_on=raw.get("wallwasherInGeneralLightOn"),
    )


def _rule_dict_to_model(raw: dict[str, Any]) -> Rule:
    return Rule(
        id=raw.get("id"),
        name=raw.get("name", ""),
        active=bool(raw.get("isActive", False)),
        start=raw.get("startTime", ""),
        end=raw.get("endTime", ""),
        days=list(raw.get("weekdays", [])),
    )


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")


def _validate_hhmm(value: str, field: str, cam_name: str) -> None:
    """Raise invalid_argument unless *value* is a well-formed 24h HH:MM[:SS] time.

    Bug-hunt finding 2026-07-11: rules_add/rules_edit previously forwarded
    ``start``/``end`` straight to Bosch with zero format checking — a garbage
    string like "25:99" or "foo" would only surface as an opaque
    api_unreachable from Bosch's own rejection instead of a clear
    invalid_argument up front. HH:MM:SS is also accepted because
    cli_bridge.edit_rule() passes an already-``:SS``-qualified value through
    unchanged (see its docstring).
    """
    if not _HHMM_RE.match(value):
        raise MCPError(
            code="invalid_argument",
            detail=f"{field}={value!r} is not a valid 24h HH:MM time (e.g. '22:00').",
            camera=cam_name,
        )


def _friend_dict_to_model(raw: dict[str, Any]) -> Friend:
    shared = raw.get("sharedVideoInputs", raw.get("shares", []))
    shared_ids = [s.get("videoInputId", s) if isinstance(s, dict) else s for s in (shared or [])]
    return Friend(
        id=str(raw.get("id", "")),
        email=raw.get("email") or raw.get("invitationEmail"),
        nickname=raw.get("nickName"),
        status=raw.get("status") or raw.get("invitationStatus"),
        shared_cameras=[str(s) for s in shared_ids],
    )


@_blocking_tool
def bosch_camera_motion_zones_get(camera: str) -> list[MotionZone]:
    """List the motion-detection zone rectangles configured for one camera.

    Coordinates are normalized 0.0-1.0 (x/y = top-left corner, w/h = size).
    Returns an empty list if no zones are configured. Raises ``privacy_blocked``
    if the camera is currently in privacy mode (Bosch returns HTTP 443 for
    this endpoint while privacy is on).
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        zones = br.get_motion_zones(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _zone_dicts_to_models(zones)


@_blocking_tool
def bosch_camera_motion_zones_set(camera: str, zones: list[dict[str, float]]) -> list[MotionZone]:
    """Replace ALL motion-detection zones for one camera with the given list.

    This is a full replace, not a merge — pass every zone you want to keep.
    Each zone is ``{"x": ..., "y": ..., "w": ..., "h": ...}``, normalized
    0.0-1.0. Pass an empty list to clear all zones (equivalent to calling
    ``bosch_camera_motion_zones_clear``).
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    for i, z in enumerate(zones):
        try:
            MotionZone(**z)
        except (KeyError, TypeError, ValidationError) as e:
            raise MCPError(
                code="invalid_argument",
                detail=f"Zone {i} is invalid: {e}",
                camera=name,
            ) from e
    try:
        result = br.set_motion_zones(session, cam_info["id"], zones)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _zone_dicts_to_models(result)


@_blocking_tool
def bosch_camera_motion_zones_clear(camera: str) -> list[MotionZone]:
    """Remove all motion-detection zones for one camera. Returns the (empty) list."""
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        result = br.set_motion_zones(session, cam_info["id"], [])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _zone_dicts_to_models(result)


@_blocking_tool
def bosch_camera_privacy_masks_get(camera: str) -> list[PrivacyMask]:
    """List the privacy-mask zone rectangles configured for one camera.

    Coordinates are normalized 0.0-1.0 (x/y = top-left corner, w/h = size).
    Masked areas are permanently blacked out in both live view and recordings.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        masks = br.get_privacy_masks(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _mask_dicts_to_models(masks)


@_blocking_tool
def bosch_camera_privacy_masks_set(camera: str, masks: list[dict[str, float]]) -> list[PrivacyMask]:
    """Replace ALL privacy-mask zones for one camera with the given list.

    Full replace, not a merge. Each mask is ``{"x": ..., "y": ..., "w": ...,
    "h": ...}``, normalized 0.0-1.0. Pass an empty list to clear all masks.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    for i, m in enumerate(masks):
        try:
            PrivacyMask(**m)
        except (KeyError, TypeError, ValidationError) as e:
            raise MCPError(
                code="invalid_argument",
                detail=f"Mask {i} is invalid: {e}",
                camera=name,
            ) from e
    try:
        result = br.set_privacy_masks(session, cam_info["id"], masks)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _mask_dicts_to_models(result)


@_blocking_tool
def bosch_camera_privacy_masks_clear(camera: str) -> list[PrivacyMask]:
    """Remove all privacy-mask zones for one camera. Returns the (empty) list."""
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        result = br.set_privacy_masks(session, cam_info["id"], [])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return _mask_dicts_to_models(result)


@_blocking_tool
def bosch_camera_rules_list(camera: str) -> list[Rule]:
    """List the automation (time-schedule) rules configured for one camera."""
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    _name, cam_info = br._resolve_cam(cameras, camera)
    raw_rules = br.list_rules(session, cam_info["id"])
    return [_rule_dict_to_model(r) for r in raw_rules]


@_blocking_tool
def bosch_camera_rules_add(
    camera: str,
    name: str,
    start: str,
    end: str,
    days: list[int],
) -> Rule:
    """Create a new automation rule for one camera.

    ``start``/``end`` are ``HH:MM`` 24h time. ``days`` are 0=Monday..6=Sunday.
    The new rule is created active. Returns the created rule (with its id).
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    cam_name, cam_info = br._resolve_cam(cameras, camera)
    if not all(0 <= d <= 6 for d in days):
        raise MCPError(
            code="invalid_argument",
            detail=f"days must all be 0-6 (Monday-Sunday), got {days!r}",
            camera=cam_name,
        )
    _validate_hhmm(start, "start", cam_name)
    _validate_hhmm(end, "end", cam_name)
    raw = br.add_rule(session, cam_info["id"], name=name, start=start, end=end, weekdays=days)
    return _rule_dict_to_model(raw)


@_blocking_tool
def bosch_camera_rules_edit(
    camera: str,
    rule_id: str,
    active: Optional[bool] = None,
    name: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    days: Optional[list[int]] = None,
) -> Rule:
    """Update an existing automation rule for one camera. Only provided fields change."""
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    cam_name, cam_info = br._resolve_cam(cameras, camera)
    if active is None and name is None and start is None and end is None and days is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of active, name, start, end, or days must be provided.",
            camera=cam_name,
        )
    if start is not None:
        _validate_hhmm(start, "start", cam_name)
    if end is not None:
        _validate_hhmm(end, "end", cam_name)
    try:
        raw = br.edit_rule(
            session,
            cam_info["id"],
            rule_id,
            active=active,
            name=name,
            start=start,
            end=end,
            weekdays=days,
        )
    except ValueError as e:
        raise MCPError(code="invalid_argument", detail=str(e), camera=cam_name) from e
    return _rule_dict_to_model(raw)


@_blocking_tool
def bosch_camera_rules_delete(camera: str, rule_id: str) -> dict[str, Any]:
    """Delete an automation rule from one camera. Returns {deleted, rule_id}."""
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    _cam_name, cam_info = br._resolve_cam(cameras, camera)
    br.delete_rule(session, cam_info["id"], rule_id)
    return {"deleted": True, "rule_id": rule_id}


@_blocking_tool
def bosch_camera_friends_list() -> list[Friend]:
    """List all friends/invitations this account has shared cameras with.

    No camera parameter — friends are account-level, not per-camera.
    """
    br = _bridge()
    _cfg, session, _cameras = _get_session()
    br.ensure_cli_importable()

    raw_friends = br.list_friends(session)
    return [_friend_dict_to_model(f) for f in raw_friends]


@_blocking_tool
def bosch_camera_friends_invite(email: str) -> Friend:
    """Invite a new friend (by email) to share cameras with. Account-level."""
    br = _bridge()
    _cfg, session, _cameras = _get_session()
    br.ensure_cli_importable()

    raw = br.invite_friend(session, email)
    return _friend_dict_to_model(raw)


@_blocking_tool
def bosch_camera_friends_share(
    friend_id: str, camera: str, days: Optional[int] = None
) -> dict[str, Any]:
    """Share one camera with an existing friend.

    ``days`` limits the share to a time window starting now; omit for an
    unlimited share. Returns {shared, friend_id, camera}.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    br.share_camera(session, friend_id, cam_info["id"], days=days)
    return {"shared": True, "friend_id": friend_id, "camera": name}


@_blocking_tool
def bosch_camera_friends_unshare(friend_id: str) -> dict[str, Any]:
    """Revoke ALL camera shares from a friend (the friend entry itself remains).

    Returns {unshared, friend_id}.
    """
    br = _bridge()
    _cfg, session, _cameras = _get_session()
    br.ensure_cli_importable()

    br.unshare_camera(session, friend_id)
    return {"unshared": True, "friend_id": friend_id}


@_blocking_tool
def bosch_camera_friends_remove(friend_id: str) -> dict[str, Any]:
    """Remove a friend entirely (revokes shares and deletes the invitation/friendship).

    Returns {removed, friend_id}.
    """
    br = _bridge()
    _cfg, session, _cameras = _get_session()
    br.ensure_cli_importable()

    br.remove_friend(session, friend_id)
    return {"removed": True, "friend_id": friend_id}


@_blocking_tool
def bosch_camera_firmware_status(camera: str) -> FirmwareStatus:
    """Get the current/latest firmware version and update-availability for one camera."""
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    fw = br.get_firmware_status(session, cam_info["id"])
    return FirmwareStatus(
        camera=name,
        current=fw.get("current"),
        up_to_date=fw.get("upToDate"),
        update_available=fw.get("update"),
        installing=bool(fw.get("updating", False)),
    )


@_blocking_tool
def bosch_camera_firmware_install(camera: str) -> FirmwareStatus:
    """Install the pending firmware update for one camera right now.

    Mirrors the HA integration's ``async_install_firmware`` guard: raises
    ``invalid_argument`` if an install is already in progress, or if there is
    no pending update to install (already up to date). The camera reboots for
    roughly 3-7 minutes after the install starts; this call returns as soon
    as Bosch accepts the request, it does not wait for the reboot to finish.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        fw = br.install_firmware(session, cam_info["id"])
    except ValueError as e:
        raise MCPError(code="invalid_argument", detail=str(e), camera=name) from e
    return FirmwareStatus(
        camera=name,
        current=fw.get("current"),
        up_to_date=fw.get("upToDate"),
        update_available=fw.get("update"),
        installing=bool(fw.get("updating", False)),
    )


@_blocking_tool
def bosch_camera_timestamp_overlay_get(camera: str) -> TimestampOverlayConfig:
    """Get whether a date/time overlay is burned into the video for one camera.

    API: GET /v11/video_inputs/{id}/timestamp.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        enabled = br.get_timestamp_overlay(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return TimestampOverlayConfig(enabled=enabled)


@_blocking_tool
def bosch_camera_timestamp_overlay_set(camera: str, enabled: bool) -> TimestampOverlayConfig:
    """Turn the date/time video overlay on or off for one camera.

    API: PUT /v11/video_inputs/{id}/timestamp  Body: {"result": bool}.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        br.set_timestamp_overlay(session, cam_info["id"], enabled)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return TimestampOverlayConfig(enabled=enabled)


@_blocking_tool
def bosch_camera_status_led_get(camera: str) -> StatusLedConfig:
    """Get the camera's status LED on/off state. Gen2 cameras only.

    API: GET /v11/video_inputs/{id}/ledlights.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        enabled = br.get_status_led(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return StatusLedConfig(enabled=enabled)


@_blocking_tool
def bosch_camera_status_led_set(camera: str, enabled: bool) -> StatusLedConfig:
    """Turn the camera's status LED on or off. Gen2 cameras only.

    API: PUT /v11/video_inputs/{id}/ledlights  Body: {"state": "ON"/"OFF"}.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        br.set_status_led(session, cam_info["id"], enabled)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return StatusLedConfig(enabled=enabled)


@_blocking_tool
def bosch_camera_lens_elevation_get(camera: str) -> LensElevationConfig:
    """Get the configured lens mounting height (meters). Gen2 cameras only.

    Used by the camera for perspective correction in person detection.
    API: GET /v11/video_inputs/{id}/lens_elevation.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        meters = br.get_lens_elevation(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return LensElevationConfig(meters=meters)


@_blocking_tool
def bosch_camera_lens_elevation_set(camera: str, meters: float) -> LensElevationConfig:
    """Set the lens mounting height in meters (0.5-5.0). Gen2 cameras only.

    API: PUT /v11/video_inputs/{id}/lens_elevation  Body: {"elevation": float}.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    if not 0.5 <= meters <= 5.0:
        raise MCPError(
            code="invalid_argument",
            detail=f"meters={meters!r} out of range. Must be 0.5-5.0.",
            camera=name,
        )
    try:
        br.set_lens_elevation(session, cam_info["id"], meters)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return LensElevationConfig(meters=meters)


@_blocking_tool
def bosch_camera_darkness_threshold_get(camera: str) -> DarknessThresholdConfig:
    """Get the day/night lighting threshold and fading mode. Gen2 cameras only.

    0% = always day, 100% = always night.
    API: GET /v11/video_inputs/{id}/lighting.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        raw = br.get_global_lighting(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return DarknessThresholdConfig(
        threshold_percent=round(float(raw.get("darknessThreshold", 0.0)) * 100, 1),
        soft_light_fading=bool(raw.get("softLightFading", True)),
    )


@_blocking_tool
def bosch_camera_darkness_threshold_set(
    camera: str,
    threshold_percent: Optional[float] = None,
    soft_light_fading: Optional[bool] = None,
) -> DarknessThresholdConfig:
    """Set the day/night lighting threshold and/or fading mode. Gen2 cameras only.

    At least one of ``threshold_percent`` (0-100) or ``soft_light_fading`` must
    be provided; the other field is preserved from the camera's current setting.

    API: PUT /v11/video_inputs/{id}/lighting  Body: {"darknessThreshold", "softLightFading"}.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    if threshold_percent is None and soft_light_fading is None:
        raise MCPError(
            code="invalid_argument",
            detail="At least one of threshold_percent or soft_light_fading must be provided.",
            camera=name,
        )
    if threshold_percent is not None and not 0 <= threshold_percent <= 100:
        raise MCPError(
            code="invalid_argument",
            detail=f"threshold_percent={threshold_percent!r} out of range. Must be 0-100.",
            camera=name,
        )
    fraction = threshold_percent / 100 if threshold_percent is not None else None
    try:
        body = br.set_global_lighting(
            session,
            cam_info["id"],
            darkness_threshold=fraction,
            soft_light_fading=soft_light_fading,
        )
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return DarknessThresholdConfig(
        threshold_percent=round(float(body.get("darknessThreshold", 0.0)) * 100, 1),
        soft_light_fading=bool(body.get("softLightFading", True)),
    )


@_blocking_tool
def bosch_camera_white_balance_get(camera: str) -> WhiteBalanceConfig:
    """Get the front light's white balance (-1.0 cool .. 1.0 warm). Gen2 cameras only.

    API: GET /v11/video_inputs/{id}/lighting/switch.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        raw = br.get_lighting_switch(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    front = raw.get("frontLightSettings") or {}
    return WhiteBalanceConfig(value=float(front.get("whiteBalance", 0.0)))


@_blocking_tool
def bosch_camera_white_balance_set(camera: str, value: float) -> WhiteBalanceConfig:
    """Set the front light's white balance (-1.0 cool/blue .. 1.0 warm/orange). Gen2 cameras only.

    API: PUT /v11/video_inputs/{id}/lighting/switch (full-body write, GET-merged).
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    if not -1.0 <= value <= 1.0:
        raise MCPError(
            code="invalid_argument",
            detail=f"value={value!r} out of range. Must be -1.0 to 1.0.",
            camera=name,
        )
    try:
        body = br.set_white_balance(session, cam_info["id"], value)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    front = body.get("frontLightSettings") or {}
    return WhiteBalanceConfig(value=float(front.get("whiteBalance", value)))


@_blocking_tool
def bosch_camera_led_brightness_get(camera: str, position: str) -> LedBrightnessConfig:
    """Get top or bottom LED brightness (0-100%). Gen2 cameras only.

    ``position`` must be "top" or "bottom".
    API: GET /v11/video_inputs/{id}/lighting/switch.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    pos = position.lower().strip()
    if pos not in ("top", "bottom"):
        raise MCPError(
            code="invalid_argument",
            detail=f"position={position!r} is not valid. Must be 'top' or 'bottom'.",
            camera=name,
        )
    try:
        raw = br.get_lighting_switch(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    led_key = "topLedLightSettings" if pos == "top" else "bottomLedLightSettings"
    led = raw.get(led_key) or {}
    return LedBrightnessConfig(position=pos, brightness_percent=float(led.get("brightness", 0)))


@_blocking_tool
def bosch_camera_led_brightness_set(
    camera: str, position: str, brightness_percent: float
) -> LedBrightnessConfig:
    """Set top or bottom LED brightness (0-100%). Gen2 cameras only.

    ``position`` must be "top" or "bottom".
    API: PUT /v11/video_inputs/{id}/lighting/switch (full-body write, GET-merged).
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    pos = position.lower().strip()
    if pos not in ("top", "bottom"):
        raise MCPError(
            code="invalid_argument",
            detail=f"position={position!r} is not valid. Must be 'top' or 'bottom'.",
            camera=name,
        )
    if not 0 <= brightness_percent <= 100:
        raise MCPError(
            code="invalid_argument",
            detail=f"brightness_percent={brightness_percent!r} out of range. Must be 0-100.",
            camera=name,
        )
    try:
        body = br.set_led_brightness(session, cam_info["id"], pos, brightness_percent)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    led_key = "topLedLightSettings" if pos == "top" else "bottomLedLightSettings"
    led = body.get(led_key) or {}
    return LedBrightnessConfig(
        position=pos, brightness_percent=float(led.get("brightness", brightness_percent))
    )


@_blocking_tool
def bosch_camera_soft_reset(camera: str) -> dict[str, Any]:
    """Reboot one camera (soft reset). The camera briefly drops offline.

    API: PUT /v11/video_inputs/{id}/soft_reset (empty body).
    NOTE: on real hardware this has been observed to return HTTP 404
    ``sh:entity.notfound`` even when the request matches the official Bosch
    app byte-for-byte (same finding that made HA disable its equivalent
    button by default) — a 404 here likely means "not supported for this
    camera/account", not a client bug.
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    try:
        ok = br.soft_reset_camera(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return {"camera": name, "rebooting": ok}


@_blocking_tool
def bosch_camera_hard_reset(camera: str, confirm: bool = False) -> dict[str, Any]:
    """Factory-reset one camera (hard reset). DESTRUCTIVE.

    Unpairs the camera from the Bosch account — it must be re-commissioned
    from scratch via the Bosch app before it works again. Requires
    ``confirm=True`` or raises ``invalid_argument`` without calling the API.

    API: PUT /v11/video_inputs/{id}/hard_reset (empty body).
    """
    br = _bridge()
    _cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    if not confirm:
        raise MCPError(
            code="invalid_argument",
            detail=(
                "hard_reset is destructive (factory-resets and unpairs the camera). "
                "Pass confirm=True to proceed."
            ),
            camera=name,
        )
    try:
        ok = br.hard_reset_camera(session, cam_info["id"])
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return {"camera": name, "factory_reset": ok}


@_blocking_tool
def bosch_camera_rename(camera: str, new_name: str) -> dict[str, Any]:
    """Rename a camera via the Bosch cloud API. The new name appears in the Bosch app.

    API: PUT /v11/video_inputs  Body: {"videoInputId", "title", "timeZone"}.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    new_name = new_name.strip()
    if not new_name:
        raise MCPError(
            code="invalid_argument",
            detail="new_name must not be empty.",
            camera=name,
        )
    time_zone = str(cfg.get("settings", {}).get("time_zone") or "UTC")
    try:
        br.rename_camera(session, cam_info["id"], new_name, time_zone)
    except Exception as e:
        wrapped = _wrap_privacy_blocked(e, name)
        if wrapped:
            raise wrapped from e
        raise
    return {"camera": name, "new_name": new_name}


# ── Register resources + prompts ──────────────────────────────────────────────
# Importing these modules causes their @mcp.resource / @mcp.prompt decorators
# to execute, self-registering against the shared `mcp` FastMCP instance above.
# Must be imported AFTER `mcp` is defined and AFTER all tool definitions so that
# resources.py can import bosch_camera_snapshot from this module without a
# circular import.
from . import resources, prompts  # noqa: E402,F401  — side-effect imports  # pylint: disable=unused-import,wrong-import-position

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
    # module-level config-path override, set once at CLI startup
    global _CONFIG_PATH  # pylint: disable=global-statement
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
