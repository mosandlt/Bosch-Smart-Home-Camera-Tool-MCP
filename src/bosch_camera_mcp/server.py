"""MCP server entrypoint — v0.5.0-alpha.

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
def bosch_camera_privacy_set(camera: str, enabled: bool) -> CameraStatus:
    """Turn privacy mode on or off. enabled=True hides the camera."""
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    br.set_privacy_mode(session, cam_info["id"], enabled)
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
def bosch_camera_light_set(camera: str, enabled: bool) -> CameraStatus:
    """Turn the camera's spotlight on or off. Applies to Gen1 + Gen2 outdoor cameras."""
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    br.set_light(session, cam_info["id"], enabled)
    return _build_status(name, cam_info, session, cfg)


@mcp.tool()
def bosch_camera_pan(camera: str, direction: str) -> CameraStatus:
    """Pan the 360° camera. direction: left | center | right | <-120..120>."""
    br = _bridge()
    cfg, session, cameras = _get_session()
    br.ensure_cli_importable()

    name, cam_info = br._resolve_cam(cameras, camera)
    br.set_pan(session, cam_info["id"], direction)
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
    """Fetch the current Bosch Smart Home cloud maintenance announcement from the official community RSS feed. Returns title, time window, link, and state (active/scheduled/past/recent/unknown/idle). Use this when users ask why cameras are unavailable or when the cloud returns 5xx errors."""
    from .maintenance import async_fetch_maintenance  # noqa: PLC0415

    mw = await async_fetch_maintenance()
    if mw is None:
        return {"state": "idle", "summary": "No maintenance announcement found"}
    result: dict[str, Any] = mw.as_dict()
    result["state"] = mw.state()
    return result


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
