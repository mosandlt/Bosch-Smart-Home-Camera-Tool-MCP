"""
Tests for pan preset feature in MCP server (v1.3.4).

PIN_EVERY_MODE: one test per preset name + integration test for preset-overrides-direction.
Source: HA integration v12.6.1 PTZ preset port.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bosch_camera_mcp.adapters.cli_bridge import PAN_PRESET_MAP, set_pan
from bosch_camera_mcp.errors import MCPError


# ── PAN_PRESET_MAP constant tests ─────────────────────────────────────────────

class TestPanPresetMapMCP:
    """Verify canonical preset→angle mapping exported from cli_bridge."""

    def test_home_is_zero(self) -> None:
        assert PAN_PRESET_MAP["home"] == 0

    def test_left_is_minus_60(self) -> None:
        assert PAN_PRESET_MAP["left"] == -60

    def test_right_is_plus_60(self) -> None:
        assert PAN_PRESET_MAP["right"] == 60

    def test_back_left_is_minus_120(self) -> None:
        assert PAN_PRESET_MAP["back-left"] == -120

    def test_back_right_is_plus_120(self) -> None:
        assert PAN_PRESET_MAP["back-right"] == 120


# ── set_pan dispatch tests ────────────────────────────────────────────────────

def _mock_session_for_pan(limit: int = 120) -> MagicMock:
    """Return a session mock that accepts any put and returns a 200 JSON."""
    session = MagicMock()

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {"currentAbsolutePosition": 0, "panLimit": limit}
    session.get.return_value = get_resp

    put_resp = MagicMock()
    put_resp.status_code = 200
    put_resp.json.return_value = {
        "currentAbsolutePosition": 0,
        "estimatedTimeToCompletion": 300,
        "cameraStoppedAtLimit": False,
    }
    session.put.return_value = put_resp
    return session


CAM_ID = "TEST-CAM-001"


class TestSetPanPresetResolution:
    """set_pan resolves named presets to correct absolutePosition."""

    def _put_body(self, session: MagicMock) -> dict:
        return session.put.call_args[1]["json"]

    def test_set_pan_home(self) -> None:
        s = _mock_session_for_pan()
        set_pan(s, CAM_ID, "home")
        assert self._put_body(s)["absolutePosition"] == 0

    def test_set_pan_left(self) -> None:
        s = _mock_session_for_pan()
        set_pan(s, CAM_ID, "left")
        assert self._put_body(s)["absolutePosition"] == -60

    def test_set_pan_right(self) -> None:
        s = _mock_session_for_pan()
        set_pan(s, CAM_ID, "right")
        assert self._put_body(s)["absolutePosition"] == 60

    def test_set_pan_back_left(self) -> None:
        s = _mock_session_for_pan()
        set_pan(s, CAM_ID, "back-left")
        assert self._put_body(s)["absolutePosition"] == -120

    def test_set_pan_back_right(self) -> None:
        s = _mock_session_for_pan()
        set_pan(s, CAM_ID, "back-right")
        assert self._put_body(s)["absolutePosition"] == 120

    def test_set_pan_legacy_center(self) -> None:
        """Legacy 'center' alias still maps to 0°."""
        s = _mock_session_for_pan()
        set_pan(s, CAM_ID, "center")
        assert self._put_body(s)["absolutePosition"] == 0

    def test_set_pan_invalid_raises_mcp_error(self) -> None:
        s = _mock_session_for_pan()
        with pytest.raises(MCPError):
            set_pan(s, CAM_ID, "diagonal")

    def test_set_pan_out_of_range_raises_mcp_error(self) -> None:
        s = _mock_session_for_pan(limit=60)
        with pytest.raises(MCPError):
            set_pan(s, CAM_ID, "200")
