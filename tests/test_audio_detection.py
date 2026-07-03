"""Tests for bosch_camera_audio_detection_get/set (glass-break + fire-alarm).

Cross-ported from HA integration v14.2.0 (BoschGlassBreakDetectionSwitch /
BoschFireAlarmDetectionSwitch). Mirrors the intrusion-detection test pattern
in test_audio_intrusion_wifi.py.

Covers:
- hardware_unsupported gate (Gen1 cameras)
- valid get/set round-trips for Gen2 cameras
- PUT body always carries BOTH detectGlassBreak + detectFireAlarm
  (read-modify-write merge — only the provided field(s) change)
- no-op guard (at least one param required for _set)
- privacy-mode-blocked error propagation (HTTP 443 sh:camera.in.privacy.mode)
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import responses as resp_lib

CAM_ID_GEN2 = "gen2-aaaa-1111-aaaa"
CAM_ID_GEN1 = "gen1-bbbb-2222-bbbb"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

_VALID_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJleHAiOjQxMDI0NDQ4MDB9"
    ".placeholder"
)

_CFG_AUDIO_DETECTION = {
    "account": {
        "username": "test@example.com",
        "bearer_token": _VALID_TOKEN,
        "refresh_token": "",
    },
    "cameras": {
        "Indoor": {
            "id": CAM_ID_GEN2,
            "name": "Indoor",
            "model": "HOME_Eyes_Indoor",
            "firmware": "9.40.25",
            "mac": "aa:bb:cc:00:11:55",
            "download_folder": "Indoor",
            "local_ip": "192.0.2.150",
            "local_username": "admin",
            "local_password": "secret",
            "has_light": False,
            "has_sound": True,
            "pan_limit": 0,
        },
        "Outdoor": {
            "id": CAM_ID_GEN1,
            "name": "Outdoor",
            "model": "CAMERA_EYES",
            "firmware": "7.91.56",
            "mac": "aa:bb:cc:00:11:44",
            "download_folder": "Outdoor",
            "local_ip": "192.0.2.27",
            "local_username": "admin",
            "local_password": "secret",
            "has_light": False,
            "has_sound": False,
            "pan_limit": 0,
        },
    },
    "settings": {},
    "nvr": {},
}

_AUDIO_DETECTION_RESPONSE = {
    "detectGlassBreak": True,
    "detectFireAlarm": False,
}


def _make_fake_bc(cfg: dict = _CFG_AUDIO_DETECTION) -> MagicMock:
    import requests as req_lib

    m = MagicMock()
    m.CLOUD_API = CLOUD_API
    m.DEFAULT_CONFIG = {"account": {}, "cameras": {}, "settings": {}, "nvr": {}}
    m.load_config.return_value = cfg
    m._merge_defaults = lambda c, d: None
    m._is_token_near_expiry.return_value = False
    m.make_session.return_value = req_lib.Session()
    m.save_config.return_value = None

    m.api_ping.side_effect = lambda session, cam_id: "ONLINE"
    m.api_get_camera.return_value = {
        "id": CAM_ID_GEN2,
        "privacyMode": "OFF",
        "featureSupport": {"light": False, "sound": True},
        "featureStatus": {},
    }
    m.api_get_events.return_value = []
    m.snap_from_local.return_value = b"\xff\xd8\xff"
    return m


@pytest.fixture(autouse=True)
def patch_bc_audio_detection(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_bc = _make_fake_bc()
    monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
    monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())

    import bosch_camera_mcp.adapters.cli_bridge as bridge

    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)

    import requests as req_lib

    fake_session = req_lib.Session()

    def _fake_get_session(config_path: Any = None) -> tuple:
        return _CFG_AUDIO_DETECTION, fake_session, _CFG_AUDIO_DETECTION["cameras"]

    monkeypatch.setattr(bridge, "get_session_and_cameras", _fake_get_session)

    import bosch_camera_mcp.server as srv

    monkeypatch.setattr(srv, "_get_session", _fake_get_session)

    yield fake_bc


# ---------------------------------------------------------------------------
# bosch_camera_audio_detection_get
# ---------------------------------------------------------------------------


class TestAudioDetectionGet:
    @resp_lib.activate
    def test_get_gen2_returns_config(self) -> None:
        """Gen2 camera returns glass_break + fire_alarm mapped from the API fields."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json=_AUDIO_DETECTION_RESPONSE,
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_detection_get

        result = bosch_camera_audio_detection_get(camera="Indoor")
        assert result.glass_break is True
        assert result.fire_alarm is False

    @resp_lib.activate
    def test_get_gen1_raises_hardware_unsupported(self) -> None:
        """Gen1 camera raises hardware_unsupported — no API call made."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_detection_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_detection_get(camera="Outdoor")

        assert exc_info.value.code == "hardware_unsupported"
        assert "Outdoor" in exc_info.value.detail
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_get_both_true(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={"detectGlassBreak": True, "detectFireAlarm": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_detection_get

        result = bosch_camera_audio_detection_get(camera="Indoor")
        assert result.glass_break is True
        assert result.fire_alarm is True

    @resp_lib.activate
    def test_get_both_false(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={"detectGlassBreak": False, "detectFireAlarm": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_detection_get

        result = bosch_camera_audio_detection_get(camera="Indoor")
        assert result.glass_break is False
        assert result.fire_alarm is False

    @resp_lib.activate
    def test_get_fields_missing_returns_none(self) -> None:
        """When the API omits the fields entirely, both map to None."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_detection_get

        result = bosch_camera_audio_detection_get(camera="Indoor")
        assert result.glass_break is None
        assert result.fire_alarm is None

    @resp_lib.activate
    def test_get_privacy_blocked_raises_privacy_blocked(self) -> None:
        """HTTP 443 sh:camera.in.privacy.mode is wrapped into privacy_blocked."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )

        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_detection_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_detection_get(camera="Indoor")

        assert exc_info.value.code == "privacy_blocked"
        assert "Indoor" in exc_info.value.detail


# ---------------------------------------------------------------------------
# bosch_camera_audio_detection_set
# ---------------------------------------------------------------------------


class TestAudioDetectionSet:
    @resp_lib.activate
    def test_set_glass_break_only_preserves_fire_alarm(self) -> None:
        """Setting glass_break only still PUTs both fields (fire_alarm preserved)."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json=_AUDIO_DETECTION_RESPONSE,
            status=200,
        )
        put_calls: list[dict[str, Any]] = []

        def _put_callback(request: Any) -> tuple:
            body = json.loads(request.body)
            put_calls.append(body)
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            callback=_put_callback,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={**_AUDIO_DETECTION_RESPONSE, "detectGlassBreak": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_detection_set

        result = bosch_camera_audio_detection_set(camera="Indoor", glass_break=False)
        assert result.glass_break is False
        assert result.fire_alarm is False  # preserved from _AUDIO_DETECTION_RESPONSE

        assert len(put_calls) == 1
        # Both fields are ALWAYS present in the PUT body.
        assert put_calls[0] == {"detectGlassBreak": False, "detectFireAlarm": False}

    @resp_lib.activate
    def test_set_fire_alarm_only_preserves_glass_break(self) -> None:
        """Setting fire_alarm only still PUTs both fields (glass_break preserved)."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json=_AUDIO_DETECTION_RESPONSE,
            status=200,
        )
        put_calls: list[dict[str, Any]] = []

        def _put_callback(request: Any) -> tuple:
            body = json.loads(request.body)
            put_calls.append(body)
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            callback=_put_callback,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={**_AUDIO_DETECTION_RESPONSE, "detectFireAlarm": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_detection_set

        result = bosch_camera_audio_detection_set(camera="Indoor", fire_alarm=True)
        assert result.fire_alarm is True
        assert result.glass_break is True  # preserved from _AUDIO_DETECTION_RESPONSE

        assert len(put_calls) == 1
        assert put_calls[0] == {"detectGlassBreak": True, "detectFireAlarm": True}

    @resp_lib.activate
    def test_set_both_fields(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json=_AUDIO_DETECTION_RESPONSE,
            status=200,
        )
        put_calls: list[dict[str, Any]] = []

        def _put_callback(request: Any) -> tuple:
            body = json.loads(request.body)
            put_calls.append(body)
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            callback=_put_callback,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={"detectGlassBreak": True, "detectFireAlarm": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_detection_set

        result = bosch_camera_audio_detection_set(
            camera="Indoor", glass_break=True, fire_alarm=True
        )
        assert result.glass_break is True
        assert result.fire_alarm is True
        assert put_calls[0] == {"detectGlassBreak": True, "detectFireAlarm": True}

    @resp_lib.activate
    def test_set_gen1_raises_hardware_unsupported(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_detection_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_detection_set(camera="Outdoor", glass_break=True)

        assert exc_info.value.code == "hardware_unsupported"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_set_no_params_raises(self) -> None:
        """Calling set with no params raises invalid_argument — no API call."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_detection_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_detection_set(camera="Indoor")

        assert exc_info.value.code == "invalid_argument"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_set_privacy_blocked_raises_privacy_blocked(self) -> None:
        """PUT rejected with HTTP 443 while privacy mode is ON is wrapped."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json=_AUDIO_DETECTION_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audioDetectionConfig",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )

        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_detection_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_detection_set(camera="Indoor", glass_break=True)

        assert exc_info.value.code == "privacy_blocked"
        assert "Indoor" in exc_info.value.detail
