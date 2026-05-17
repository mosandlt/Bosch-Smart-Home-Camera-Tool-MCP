"""MCP Resources — v0.4.0-alpha.

Three resources registered on the shared FastMCP app:

  bosch://cameras                        — JSON list of all configured cameras
  bosch://cameras/{name}/snapshot.jpg    — last cached JPEG or fresh capture
  bosch://cameras/{name}/events          — last 50 events as JSON list

All resources use the same get_session_and_cameras bridge as the tools.
Auth errors raise MCPError("auth_expired").
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .errors import MCPError
from .server import mcp

logger = logging.getLogger("bosch_camera_mcp.resources")


def _bridge():
    from .adapters import cli_bridge  # noqa: PLC0415

    return cli_bridge


def _get_session(config_path: str | None = None):
    br = _bridge()
    return br.get_session_and_cameras(config_path)


# ── bosch://cameras ───────────────────────────────────────────────────────────


@mcp.resource(
    "bosch://cameras",
    name="cameras-list",
    description="JSON list of all configured Bosch cameras with status, firmware, and MAC.",
    mime_type="application/json",
)
def cameras_list() -> str:
    """Return JSON list of all cameras (extended CameraSummary + description/firmware/mac)."""
    try:
        cfg, session, cameras = _get_session()
    except MCPError as exc:
        if exc.code in ("reauth_required", "auth_expired"):
            raise MCPError(code="auth_expired", detail=exc.detail)
        raise

    import bosch_camera as bc  # type: ignore[import-not-found]

    result: list[dict[str, Any]] = []
    for name, cam_info in cameras.items():
        cam_id = cam_info.get("id", "")
        status = bc.api_ping(session, cam_id) if cam_id else "UNKNOWN"
        result.append(
            {
                "id": cam_id,
                "name": name,
                "model": cam_info.get("model", "CAMERA"),
                "hw_version": cam_info.get("model", "CAMERA"),
                "status": status,
                "description": cam_info.get("description", ""),
                "firmware_version": cam_info.get("firmware", ""),
                "mac": cam_info.get("mac", ""),
            }
        )
    return json.dumps(result, indent=2)


# ── bosch://cameras/{name}/snapshot.jpg ──────────────────────────────────────


@mcp.resource(
    "bosch://cameras/{name}/snapshot.jpg",
    name="camera-snapshot",
    description="Last cached snapshot JPEG, or a fresh capture when the cache is empty.",
    mime_type="image/jpeg",
)
def camera_snapshot(name: str) -> bytes:
    """Return the latest cached JPEG for `name`, capturing fresh if none exists."""
    try:
        cfg, session, cameras = _get_session()
    except MCPError as exc:
        if exc.code in ("reauth_required", "auth_expired"):
            raise MCPError(code="auth_expired", detail=exc.detail)
        raise

    br = _bridge()
    canonical_name, _cam_info = br._resolve_cam(cameras, name)
    safe_name = canonical_name.replace(" ", "_")
    cache_dir = (
        Path.home() / ".cache" / "bosch-camera-mcp" / "snapshots" / safe_name
    )

    # Try cache hit first
    if cache_dir.is_dir():
        jpegs = sorted(cache_dir.glob("*.jpg"))
        if jpegs:
            logger.debug("resource snapshot cache hit: %s", jpegs[-1])
            return jpegs[-1].read_bytes()

    # Cache miss — delegate to the snapshot tool so caching logic is centralised
    logger.debug("resource snapshot cache miss for %r — triggering fresh capture", name)
    from .server import bosch_camera_snapshot  # noqa: PLC0415

    result = bosch_camera_snapshot(camera=canonical_name)
    return Path(result.path).read_bytes()


# ── bosch://cameras/{name}/events ────────────────────────────────────────────


@mcp.resource(
    "bosch://cameras/{name}/events",
    name="camera-events",
    description="Last 50 events (motion, person, audio) as JSON list.",
    mime_type="application/json",
)
def camera_events(name: str) -> str:
    """Return the last 50 events for camera `name` as a JSON string."""
    try:
        cfg, session, cameras = _get_session()
    except MCPError as exc:
        if exc.code in ("reauth_required", "auth_expired"):
            raise MCPError(code="auth_expired", detail=exc.detail)
        raise

    br = _bridge()
    br.ensure_cli_importable()
    canonical_name, cam_info = br._resolve_cam(cameras, name)
    cam_id = cam_info["id"]

    import bosch_camera as bc  # type: ignore[import-not-found]

    raw_events = bc.api_get_events(session, cam_id, limit=50)
    normalized: list[dict[str, Any]] = []
    for ev in raw_events[:50]:
        ts_raw = ev.get("timestamp", "")
        normalized.append(
            {
                "event_id": ev.get("id", ""),
                "type": ev.get("type", "UNKNOWN"),
                "timestamp_iso": ts_raw[:19] if ts_raw else "",
                "has_clip": bool(ev.get("clipUrl") or ev.get("videoUrl")),
            }
        )
    return json.dumps(normalized, indent=2)
