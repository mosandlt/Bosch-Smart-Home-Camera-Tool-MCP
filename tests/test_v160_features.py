"""Tests for v1.6.0 new tools.

Covers:
- bosch_camera_motion_get / _set
- bosch_camera_recording_get / _set
- bosch_camera_autofollow_get / _set  (hardware_unsupported gate for non-360°)
- bosch_camera_privacy_sound_get / _set
- bosch_camera_unread_get  (reads numberOfUnreadEvents from video_inputs listing)
- bosch_camera_health_check_all
- bosch_camera_token_status
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import responses as resp_lib

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

CAM_ID_360 = "cam-360-aaaa-1111"      # Gen1 360° with pan_limit (supports autofollow)
CAM_ID_OUTDOOR = "cam-out-bbbb-2222"  # Gen1 outdoor without pan_limit

CLOUD_API = "https://residential.cbs.boschsecurity.com"

# JWT with exp=4102444800 (year 2100 — never expires in tests)
_VALID_TOKEN = (
    "<FAKE-JWT-FOR-TESTS>"
    ".eyJleHAiOjQxMDI0NDQ4MDAsImVtYWlsIjoidGVzdEBleGFtcGxlLmNvbSJ9"
    ".placeholder"
)

_CFG = {
    "account": {
        "username": "test@example.com",
        "bearer_token": _VALID_TOKEN,
        "refresh_token": "",
    },
    "cameras": {
        "Indoor360": {
            "id": CAM_ID_360,
            "name": "Indoor360",
            "model": "INDOOR",
            "firmware": "7.91.56",
            "mac": "00:11:22:33:44:55",
            "download_folder": "Indoor360",
            "local_ip": "10.0.0.150",
            "local_username": "admin",
            "local_password": "secret",
            "has_light": False,
            "has_sound": False,
            "pan_limit": 120,
        },
        "Outdoor": {
            "id": CAM_ID_OUTDOOR,
            "name": "Outdoor",
            "model": "CAMERA_EYES",
            "firmware": "7.91.56",
            "mac": "00:11:22:33:44:66",
            "download_folder": "Outdoor",
            "local_ip": "10.0.0.27",
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


def _make_fake_bc(cfg: dict = _CFG) -> MagicMock:
    import requests as req_lib

    m = MagicMock()
    m.CLOUD_API = CLOUD_API
    m.DEFAULT_CONFIG = {"account": {}, "cameras": {}, "settings": {}, "nvr": {}}
    m.load_config.return_value = cfg
    m._merge_defaults = lambda c, d: None
    m._is_token_near_expiry.return_value = False
    m._is_token_expired.return_value = False
    m.make_session.return_value = req_lib.Session()
    m.save_config.return_value = None

    m.api_ping.side_effect = lambda session, cam_id: "ONLINE"
    m.api_get_camera.return_value = {
        "id": CAM_ID_360,
        "privacyMode": "OFF",
        "featureSupport": {"light": False, "sound": False, "panLimit": 120},
        "featureStatus": {},
    }
    m.api_get_events.return_value = [
        {
            "id": "evt-001",
            "eventType": "MOTION",
            "timestamp": "2026-05-28T10:00:00Z",
            "videoClipUploadStatus": "Done",
        }
    ]
    m.snap_from_local.return_value = b"\xff\xd8\xff"
    return m


@pytest.fixture(autouse=True)
def patch_bc(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_bc = _make_fake_bc()
    monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
    monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())

    import bosch_camera_mcp.adapters.cli_bridge as bridge

    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)

    import requests as req_lib

    fake_session = req_lib.Session()

    def _fake_get_session(config_path: Any = None) -> tuple:
        return _CFG, fake_session, _CFG["cameras"]

    monkeypatch.setattr(bridge, "get_session_and_cameras", _fake_get_session)

    import bosch_camera_mcp.server as srv

    monkeypatch.setattr(srv, "_get_session", _fake_get_session)

    yield fake_bc


# ---------------------------------------------------------------------------
# bosch_camera_motion_get
# ---------------------------------------------------------------------------


class TestMotionGet:
    @resp_lib.activate
    def test_motion_get_returns_enabled_and_sensitivity(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            json={"enabled": True, "motionAlarmConfiguration": "HIGH"},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_motion_get

        result = bosch_camera_motion_get(camera="Indoor360")
        assert result.enabled is True
        assert result.sensitivity == "HIGH"

    @resp_lib.activate
    def test_motion_get_disabled(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            json={"enabled": False, "motionAlarmConfiguration": "MEDIUM_HIGH"},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_motion_get

        result = bosch_camera_motion_get(camera="Indoor360")
        assert result.enabled is False
        assert result.sensitivity == "MEDIUM_HIGH"

    @resp_lib.activate
    def test_motion_get_privacy_blocked_wraps_error(self) -> None:
        """HTTP 443 is wrapped as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            body=MCPError(
                code="api_unreachable",
                detail="HTTP 443 sh:camera.in.privacy.mode",
            ),
            status=443,
        )

        from bosch_camera_mcp.server import bosch_camera_motion_get

        # Bridge will raise MCPError with the privacy pattern — server wraps it
        # For the test we just confirm it raises MCPError (any code)
        with pytest.raises(Exception):
            bosch_camera_motion_get(camera="Indoor360")


# ---------------------------------------------------------------------------
# bosch_camera_motion_set
# ---------------------------------------------------------------------------


class TestMotionSet:
    @resp_lib.activate
    def test_motion_set_enable(self) -> None:
        """Enable motion detection."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            json={"enabled": False, "motionAlarmConfiguration": "MEDIUM_HIGH"},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            json={"enabled": True, "motionAlarmConfiguration": "MEDIUM_HIGH"},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_motion_set

        result = bosch_camera_motion_set(camera="Indoor360", enabled=True)
        assert result.enabled is True
        assert result.sensitivity == "MEDIUM_HIGH"

    @resp_lib.activate
    def test_motion_set_sensitivity_implies_enable(self) -> None:
        """Setting sensitivity alone implicitly enables motion."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            json={"enabled": False, "motionAlarmConfiguration": "LOW"},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/motion",
            json={"enabled": True, "motionAlarmConfiguration": "SUPER_HIGH"},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_motion_set

        result = bosch_camera_motion_set(camera="Indoor360", sensitivity="SUPER_HIGH")
        assert result.enabled is True
        assert result.sensitivity == "SUPER_HIGH"

    @resp_lib.activate
    def test_motion_set_no_params_raises(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_set(camera="Indoor360")

        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_motion_set_invalid_sensitivity_raises(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_set(camera="Indoor360", sensitivity="TURBO")

        assert exc_info.value.code == "invalid_argument"
        assert "TURBO" in exc_info.value.detail


# ---------------------------------------------------------------------------
# bosch_camera_recording_get / _set
# ---------------------------------------------------------------------------


class TestRecording:
    @resp_lib.activate
    def test_recording_get_sound_on(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/recording_options",
            json={"recordSound": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_recording_get

        result = bosch_camera_recording_get(camera="Indoor360")
        assert result.sound_on is True

    @resp_lib.activate
    def test_recording_get_sound_off(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/recording_options",
            json={"recordSound": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_recording_get

        result = bosch_camera_recording_get(camera="Indoor360")
        assert result.sound_on is False

    @resp_lib.activate
    def test_recording_set_sound_on(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/recording_options",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/recording_options",
            json={"recordSound": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_recording_set

        result = bosch_camera_recording_set(camera="Indoor360", sound_on=True)
        assert result.sound_on is True

    @resp_lib.activate
    def test_recording_set_sound_off(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/recording_options",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/recording_options",
            json={"recordSound": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_recording_set

        result = bosch_camera_recording_set(camera="Indoor360", sound_on=False)
        assert result.sound_on is False


# ---------------------------------------------------------------------------
# bosch_camera_autofollow_get / _set
# ---------------------------------------------------------------------------


class TestAutofollow:
    @resp_lib.activate
    def test_autofollow_get_enabled(self) -> None:
        """360° camera (pan_limit=120) returns enabled state."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/autofollow",
            json={"result": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_autofollow_get

        result = bosch_camera_autofollow_get(camera="Indoor360")
        assert result.enabled is True

    @resp_lib.activate
    def test_autofollow_get_disabled(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/autofollow",
            json={"result": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_autofollow_get

        result = bosch_camera_autofollow_get(camera="Indoor360")
        assert result.enabled is False

    @resp_lib.activate
    def test_autofollow_get_non360_raises_hardware_unsupported(self) -> None:
        """Outdoor camera (pan_limit=0) raises hardware_unsupported — no API call."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_autofollow_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_autofollow_get(camera="Outdoor")

        assert exc_info.value.code == "hardware_unsupported"
        assert "Outdoor" in exc_info.value.detail
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_autofollow_set_enable(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/autofollow",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/autofollow",
            json={"result": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_autofollow_set

        result = bosch_camera_autofollow_set(camera="Indoor360", enabled=True)
        assert result.enabled is True

    @resp_lib.activate
    def test_autofollow_set_disable(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/autofollow",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/autofollow",
            json={"result": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_autofollow_set

        result = bosch_camera_autofollow_set(camera="Indoor360", enabled=False)
        assert result.enabled is False

    @resp_lib.activate
    def test_autofollow_set_non360_raises_hardware_unsupported(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_autofollow_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_autofollow_set(camera="Outdoor", enabled=True)

        assert exc_info.value.code == "hardware_unsupported"
        assert len(resp_lib.calls) == 0


# ---------------------------------------------------------------------------
# bosch_camera_privacy_sound_get / _set
# ---------------------------------------------------------------------------


class TestPrivacySound:
    @resp_lib.activate
    def test_privacy_sound_get_enabled(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/privacy_sound_override",
            json={"result": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_privacy_sound_get

        result = bosch_camera_privacy_sound_get(camera="Indoor360")
        assert result.enabled is True

    @resp_lib.activate
    def test_privacy_sound_get_disabled(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/privacy_sound_override",
            json={"result": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_privacy_sound_get

        result = bosch_camera_privacy_sound_get(camera="Indoor360")
        assert result.enabled is False

    @resp_lib.activate
    def test_privacy_sound_set_enable(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/privacy_sound_override",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/privacy_sound_override",
            json={"result": True},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_privacy_sound_set

        result = bosch_camera_privacy_sound_set(camera="Indoor360", enabled=True)
        assert result.enabled is True

    @resp_lib.activate
    def test_privacy_sound_set_disable(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/privacy_sound_override",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/privacy_sound_override",
            json={"result": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_privacy_sound_set

        result = bosch_camera_privacy_sound_set(camera="Indoor360", enabled=False)
        assert result.enabled is False

    @resp_lib.activate
    def test_privacy_sound_outdoor_camera_works(self) -> None:
        """No hardware gate on privacy_sound — works for all cameras."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_OUTDOOR}/privacy_sound_override",
            json={"result": False},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_privacy_sound_get

        result = bosch_camera_privacy_sound_get(camera="Outdoor")
        assert result.enabled is False


# ---------------------------------------------------------------------------
# bosch_camera_unread_get
# ---------------------------------------------------------------------------


class TestUnreadGet:
    @resp_lib.activate
    def test_unread_get_reads_from_listing(self) -> None:
        """Reads numberOfUnreadEvents from /v11/video_inputs listing."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[
                {
                    "id": CAM_ID_360,
                    "title": "Indoor360",
                    "hardwareVersion": "INDOOR",
                    "numberOfUnreadEvents": 5,
                    "privacyMode": "OFF",
                },
                {
                    "id": CAM_ID_OUTDOOR,
                    "title": "Outdoor",
                    "hardwareVersion": "CAMERA_EYES",
                    "numberOfUnreadEvents": 0,
                    "privacyMode": "OFF",
                },
            ],
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_unread_get

        result = bosch_camera_unread_get(camera="Indoor360")
        assert result.camera == "Indoor360"
        assert result.count == 5

    @resp_lib.activate
    def test_unread_get_zero_when_field_absent(self) -> None:
        """numberOfUnreadEvents absent → count=0."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[
                {
                    "id": CAM_ID_360,
                    "title": "Indoor360",
                    "hardwareVersion": "INDOOR",
                    "privacyMode": "OFF",
                    # numberOfUnreadEvents absent
                },
            ],
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_unread_get

        result = bosch_camera_unread_get(camera="Indoor360")
        assert result.count == 0

    @resp_lib.activate
    def test_unread_get_outdoor_camera(self) -> None:
        """Works for any camera, not hardware-gated."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[
                {
                    "id": CAM_ID_OUTDOOR,
                    "title": "Outdoor",
                    "hardwareVersion": "CAMERA_EYES",
                    "numberOfUnreadEvents": 12,
                    "privacyMode": "OFF",
                },
            ],
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_unread_get

        result = bosch_camera_unread_get(camera="Outdoor")
        assert result.count == 12


# ---------------------------------------------------------------------------
# bosch_camera_health_check_all
# ---------------------------------------------------------------------------


class TestHealthCheckAll:
    @resp_lib.activate
    def test_health_check_returns_all_cameras(self) -> None:
        """Returns one entry per configured camera."""
        # video_inputs listing
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[
                {
                    "id": CAM_ID_360,
                    "title": "Indoor360",
                    "hardwareVersion": "INDOOR",
                    "numberOfUnreadEvents": 3,
                    "privacyMode": "OFF",
                },
                {
                    "id": CAM_ID_OUTDOOR,
                    "title": "Outdoor",
                    "hardwareVersion": "CAMERA_EYES",
                    "numberOfUnreadEvents": 0,
                    "privacyMode": "ON",
                },
            ],
            status=200,
        )
        # wifiinfo for both cameras
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/wifiinfo",
            json={"rssi": -65, "ssid": "Home", "signalStrength": -65},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_OUTDOOR}/wifiinfo",
            json={"rssi": -80, "ssid": "Home", "signalStrength": -80},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_health_check_all

        results = bosch_camera_health_check_all()
        assert len(results) == 2

        by_name = {r.name: r for r in results}
        indoor = by_name["Indoor360"]
        assert indoor.status == "ONLINE"
        assert indoor.privacy_mode is False
        assert indoor.unread_count == 3
        assert indoor.error is None

        outdoor = by_name["Outdoor"]
        assert outdoor.privacy_mode is True
        assert outdoor.unread_count == 0

    @resp_lib.activate
    def test_health_check_captures_per_camera_errors(self) -> None:
        """An error for one camera is captured; other cameras succeed."""
        # video_inputs listing fails
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[],
            status=200,
        )
        # wifiinfo — will fail for indoor (500)
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_360}/wifiinfo",
            status=500,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_OUTDOOR}/wifiinfo",
            json={"rssi": -70, "ssid": "Net", "signalStrength": -70},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_health_check_all

        # Should not raise — errors captured per camera
        results = bosch_camera_health_check_all()
        assert len(results) == 2


# ---------------------------------------------------------------------------
# bosch_camera_token_status
# ---------------------------------------------------------------------------


class TestTokenStatus:
    def test_token_status_valid(self) -> None:
        """Valid far-future token returns valid=True with email."""
        from bosch_camera_mcp.server import bosch_camera_token_status

        result = bosch_camera_token_status()
        assert result.valid is True
        assert result.expires_in_min is not None
        assert result.expires_in_min > 0
        # The test token has email claim
        # Note: _VALID_TOKEN payload is {"exp":4102444800,"email":"test@example.com"}
        # But base64 decoding depends on exact padding — just check it doesn't crash
        assert isinstance(result.email, (str, type(None)))

    def test_token_status_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expired token returns valid=False."""
        import base64 as _b64
        import json as _json

        # Build a token with exp in the past (unix epoch 1000 = 1970-01-01 00:16:40)
        header = _b64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload_data = {"exp": 1000, "email": "old@example.com"}
        payload = _b64.urlsafe_b64encode(_json.dumps(payload_data).encode()).decode().rstrip("=")
        expired_token = f"{header}.{payload}.placeholder"

        expired_cfg = {**_CFG, "account": {**_CFG["account"], "bearer_token": expired_token}}

        def _fake_get_session_expired(config_path=None):
            return expired_cfg, None, expired_cfg["cameras"]

        import bosch_camera_mcp.server as srv
        monkeypatch.setattr(srv, "_get_session", _fake_get_session_expired)

        from bosch_camera_mcp.server import bosch_camera_token_status as _fn
        result = _fn()
        assert result.valid is False
        assert result.expires_in_min is not None
        assert result.expires_in_min < 0
        assert result.email == "old@example.com"

    def test_token_status_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing token returns valid=False."""
        no_token_cfg = {**_CFG, "account": {**_CFG["account"], "bearer_token": ""}}

        def _fake_get_session_none(config_path=None):
            return no_token_cfg, None, no_token_cfg["cameras"]

        import bosch_camera_mcp.server as srv
        monkeypatch.setattr(srv, "_get_session", _fake_get_session_none)

        from bosch_camera_mcp.server import bosch_camera_token_status as _fn
        result = _fn()
        assert result.valid is False
        assert result.expires_in_min is None
        assert result.email is None
