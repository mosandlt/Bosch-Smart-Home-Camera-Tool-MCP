"""MCP server entrypoint.

v0.1.0-alpha: tool surface is declared with full schemas, but every body
raises NotImplementedError. Wiring to the sister Python CLI's bosch_camera.py
happens in v0.2.0.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from . import __version__

logger = logging.getLogger("bosch_camera_mcp")

mcp = FastMCP("bosch-smart-home-camera")


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
    light_on: bool | None = None
    last_event_at: str | None = Field(
        default=None, description="ISO 8601 timestamp of the latest motion event"
    )


class SnapshotResult(BaseModel):
    """Path-based snapshot result."""

    path: str = Field(description="Filesystem path to the saved JPEG")
    method: str = Field(description="Source: cloud_proxy | local_lan | last_event")
    timestamp: str = Field(description="ISO 8601 capture time")


@mcp.tool()
def bosch_camera_list() -> list[CameraSummary]:
    """List all configured Bosch cameras."""
    raise NotImplementedError("v0.2.0 will wire this to bosch_camera.discover_cameras")


@mcp.tool()
def bosch_camera_status(camera: str) -> CameraStatus:
    """Get the current status of one camera by name (case-insensitive)."""
    raise NotImplementedError("v0.2.0 will wire this to bosch_camera.api_ping + api_get_camera")


@mcp.tool()
def bosch_camera_snapshot(camera: str, prefer_local: bool = False) -> SnapshotResult:
    """Capture a fresh snapshot. Tries cloud proxy first unless prefer_local=True."""
    raise NotImplementedError("v0.2.0 will wire this to bosch_camera.snap_from_proxy/local")


@mcp.tool()
def bosch_camera_events(camera: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent motion / person / audio events for one camera."""
    raise NotImplementedError("v0.2.0 will wire this to bosch_camera.api_get_events")


@mcp.tool()
def bosch_camera_privacy_set(camera: str, enabled: bool) -> CameraStatus:
    """Turn privacy mode on or off. enabled=True hides the camera."""
    raise NotImplementedError("v0.3.0 will wire this to bosch_camera.cmd_privacy logic")


@mcp.tool()
def bosch_camera_light_set(camera: str, enabled: bool) -> CameraStatus:
    """Turn the camera's spotlight on or off. Gen1 + Gen2."""
    raise NotImplementedError("v0.3.0 will wire this to bosch_camera.cmd_light logic")


@mcp.tool()
def bosch_camera_pan(camera: str, direction: str) -> CameraStatus:
    """Pan the 360° camera. direction: left | center | right | <-120..120>."""
    raise NotImplementedError("v0.3.0 will wire this to bosch_camera.cmd_pan logic")


@mcp.tool()
def bosch_camera_notifications_set(camera: str, enabled: bool) -> CameraStatus:
    """Toggle push notifications for one camera."""
    raise NotImplementedError("v0.3.0 will wire this to bosch_camera.cmd_notifications logic")


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint declared in pyproject.toml [project.scripts]."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("starting bosch-smart-home-camera-mcp v%s (config=%s)", __version__, args.config)
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
