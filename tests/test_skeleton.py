"""Smoke tests for the v0.1.0-alpha skeleton.

These tests prove the MCP app is constructible and all declared tools raise
NotImplementedError (the contract of this alpha — tools are *declared* but
not yet wired). When tool bodies land in v0.2.0+, the NotImplementedError
expectations will flip to real assertions.
"""

from __future__ import annotations

import pytest

from bosch_camera_mcp import __version__
from bosch_camera_mcp.server import (
    bosch_camera_events,
    bosch_camera_light_set,
    bosch_camera_list,
    bosch_camera_notifications_set,
    bosch_camera_pan,
    bosch_camera_privacy_set,
    bosch_camera_snapshot,
    bosch_camera_status,
    mcp,
)


class TestVersion:
    def test_version_is_alpha(self) -> None:
        """Alpha tag stays on the version string until tools are wired."""
        assert __version__ == "0.1.0a0"


class TestServerApp:
    def test_server_name(self) -> None:
        """FastMCP server identifies as bosch-smart-home-camera."""
        assert mcp.name == "bosch-smart-home-camera"


class TestToolSurface:
    """Every tool currently raises NotImplementedError — locked-in by tests so
    that wiring work in v0.2.0+ explicitly flips each one."""

    def test_list_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.2.0"):
            bosch_camera_list.fn()

    def test_status_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.2.0"):
            bosch_camera_status.fn(camera="garten")

    def test_snapshot_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.2.0"):
            bosch_camera_snapshot.fn(camera="garten")

    def test_events_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.2.0"):
            bosch_camera_events.fn(camera="garten")

    def test_privacy_set_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.3.0"):
            bosch_camera_privacy_set.fn(camera="garten", enabled=True)

    def test_light_set_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.3.0"):
            bosch_camera_light_set.fn(camera="garten", enabled=True)

    def test_pan_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.3.0"):
            bosch_camera_pan.fn(camera="garten", direction="left")

    def test_notifications_set_not_yet_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="v0.3.0"):
            bosch_camera_notifications_set.fn(camera="garten", enabled=True)
