"""MCP server entrypoint — v1.3.4.

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
import datetime
import logging
import os
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
        description="Detection mode, e.g. OFF | ACTIVE | SCHEDULED",
    )
    sensitivity: Optional[int] = Field(
        default=None, description="Detection sensitivity (0-7; 0=low, 7=high)"
    )
    distance: Optional[int] = Field(
        default=None, description="Detection distance in meters (1-10)"
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
        result.append(
            CameraSummary(
                id=cam_id,
                name=name,
                model=cam_info.get("model", "CAMERA"),
                hw_version=cam_info.get("model", "CAMERA"),
                status=status,
            )
        )
    return result


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

    ts_now = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

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

    out_path = cache_dir / f"{ts_now}.jpg"
    out_path.write_bytes(data)

    return SnapshotResult(
        path=str(out_path),
        method="local_lan",
        timestamp=ts_now.replace("_", "T").replace("-", ":"),
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
        normalized.append(
            {
                "event_id": ev.get("id", ""),
                "type": ev.get("type", "UNKNOWN"),
                "timestamp_iso": ts_raw[:19] if ts_raw else "",
                "has_clip": bool(ev.get("clipUrl") or ev.get("videoUrl")),
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
            code="api_unreachable",
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

    # Gate: reject cameras without a controllable light before any API call.
    if not cam_info.get("has_light", False):
        model = cam_info.get("model", "unknown")
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model}') has no controllable light hardware. "
                "Light is only available on Eyes Outdoor II."
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
    br.set_pan(session, cam_info["id"], effective)
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

    if not cam_info.get("has_sound", False):
        model = cam_info.get("model", "unknown")
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model}') has no audio hardware. "
                "Audio settings are only available on Gen2 cameras (has_sound=true in config)."
            ),
            camera=name,
        )

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

    if not cam_info.get("has_sound", False):
        model = cam_info.get("model", "unknown")
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model}') has no audio hardware. "
                "Audio settings are only available on Gen2 cameras (has_sound=true in config)."
            ),
            camera=name,
        )

    if mic_level is None and speaker_level is None:
        raise MCPError(
            code="permission_denied",
            detail="At least one of mic_level or speaker_level must be provided.",
            camera=name,
        )

    for label, val in (("mic_level", mic_level), ("speaker_level", speaker_level)):
        if val is not None and not (0 <= val <= 100):
            raise MCPError(
                code="permission_denied",
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

    ``mode``: detection activation mode (e.g. ``OFF`` | ``ACTIVE`` | ``SCHEDULED``).
    ``sensitivity``: 0 (low) to 7 (high) — confirmed range FW 9.40+.
    ``distance``: detection range in meters, 1-10.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    if not cam_info.get("has_sound", False):
        model = cam_info.get("model", "unknown")
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model}') does not support intrusion detection. "
                "This feature is only available on Gen2 cameras."
            ),
            camera=name,
        )

    raw = br.get_intrusion_config(session, cam_info["id"])
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

    ``mode``: ``OFF`` | ``ACTIVE`` | ``SCHEDULED``.
    ``sensitivity``: 0-7 (0 = low, 7 = high; FW 9.40+ confirmed range).
    ``distance``: detection range in meters, 1-10.

    Unspecified fields are preserved from the current camera configuration.
    Returns the updated ``{mode, sensitivity, distance}`` after write.
    """
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)

    if not cam_info.get("has_sound", False):
        model = cam_info.get("model", "unknown")
        raise MCPError(
            code="hardware_unsupported",
            detail=(
                f"Camera '{name}' (model '{model}') does not support intrusion detection. "
                "This feature is only available on Gen2 cameras."
            ),
            camera=name,
        )

    if mode is None and sensitivity is None and distance is None:
        raise MCPError(
            code="permission_denied",
            detail="At least one of mode, sensitivity, or distance must be provided.",
            camera=name,
        )

    if sensitivity is not None and not (0 <= sensitivity <= 7):
        raise MCPError(
            code="permission_denied",
            detail=f"sensitivity={sensitivity} is out of range. Must be 0-7.",
            camera=name,
        )
    if distance is not None and not (1 <= distance <= 10):
        raise MCPError(
            code="permission_denied",
            detail=f"distance={distance} is out of range. Must be 1-10.",
            camera=name,
        )

    br.set_intrusion_config(
        session, cam_info["id"], mode=mode, sensitivity=sensitivity, distance=distance
    )
    raw = br.get_intrusion_config(session, cam_info["id"])
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
