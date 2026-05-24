"""Tests for bosch_camera_audio_get/set, bosch_camera_intrusion_get/set, bosch_camera_wifi.

Covers:
- hardware_unsupported gate (Gen1 cameras without has_sound)
- valid get/set round-trips for Gen2 cameras
- range-boundary validation (mic/speaker 0-100, sensitivity 0-7, distance 1-10)
- no-op guard (at least one param required for _set tools)
- WifiInfo RSSI → signal_strength mapping
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import responses as resp_lib

# ---------------------------------------------------------------------------
# Test fixtures and helpers (mirrored pattern from test_tools_integration.py)
# ---------------------------------------------------------------------------

CAM_ID_GEN2 = "gen2-aaaa-1111-aaaa"
CAM_ID_GEN1 = "gen1-bbbb-2222-bbbb"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

_VALID_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJleHAiOjQxMDI0NDQ4MDB9"
    ".placeholder"
)

_CFG_AUDIO = {
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
            "mac": "64:da:a0:30:68:28",
            "download_folder": "Indoor",
            "local_ip": "192.168.20.150",
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
            "mac": "64:da:a0:09:eb:6e",
            "download_folder": "Outdoor",
            "local_ip": "192.168.20.27",
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

_AUDIO_RESPONSE = {
    "microphoneLevel": 60,
    "SpeakerLevel": 50,
    "intercomEnabled": True,
}

_INTRUSION_RESPONSE = {
    "mode": "ACTIVE",
    "sensitivity": 3,
    "distance": 5,
}

_WIFI_RESPONSE = {
    "rssi": -67,
    "ssid": "HomeNetwork",
    "signalStrength": -67,
}


def _make_fake_bc(cfg: dict = _CFG_AUDIO) -> MagicMock:
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
def patch_bc_audio(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_bc = _make_fake_bc()
    monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
    monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())

    import bosch_camera_mcp.adapters.cli_bridge as bridge

    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)

    import requests as req_lib

    fake_session = req_lib.Session()

    def _fake_get_session(config_path: Any = None) -> tuple:
        return _CFG_AUDIO, fake_session, _CFG_AUDIO["cameras"]

    monkeypatch.setattr(bridge, "get_session_and_cameras", _fake_get_session)

    import bosch_camera_mcp.server as srv

    monkeypatch.setattr(srv, "_get_session", _fake_get_session)

    yield fake_bc


# ---------------------------------------------------------------------------
# bosch_camera_audio_get
# ---------------------------------------------------------------------------


class TestAudioGet:
    @resp_lib.activate
    def test_audio_get_gen2_returns_levels(self) -> None:
        """Gen2 camera with has_sound=True returns mic + speaker + intercom."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json=_AUDIO_RESPONSE,
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_get

        result = bosch_camera_audio_get(camera="Indoor")
        assert result.microphone_level == 60
        assert result.speaker_level == 50
        assert result.intercom_enabled is True

    @resp_lib.activate
    def test_audio_get_gen1_raises_hardware_unsupported(self) -> None:
        """Gen1 camera (has_sound=False) raises hardware_unsupported immediately."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_get(camera="Outdoor")

        assert exc_info.value.code == "hardware_unsupported"
        assert "Outdoor" in exc_info.value.detail
        # No HTTP call should have been made
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_audio_get_no_intercom_field_returns_none(self) -> None:
        """When intercomEnabled is absent, intercom_enabled is None."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={"microphoneLevel": 70},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_get

        result = bosch_camera_audio_get(camera="Indoor")
        assert result.microphone_level == 70
        assert result.intercom_enabled is None


# ---------------------------------------------------------------------------
# bosch_camera_audio_set
# ---------------------------------------------------------------------------


class TestAudioSet:
    @resp_lib.activate
    def test_audio_set_mic_only(self) -> None:
        """Set mic_level only — speaker preserved via read-then-write."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json=_AUDIO_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            status=204,
        )
        # re-fetch after write
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={**_AUDIO_RESPONSE, "microphoneLevel": 80},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_set

        result = bosch_camera_audio_set(camera="Indoor", mic_level=80)
        assert result.microphone_level == 80
        assert result.speaker_level == 50

    @resp_lib.activate
    def test_audio_set_speaker_only(self) -> None:
        """Set speaker_level only — mic preserved."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json=_AUDIO_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={**_AUDIO_RESPONSE, "SpeakerLevel": 30},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_set

        result = bosch_camera_audio_set(camera="Indoor", speaker_level=30)
        assert result.speaker_level == 30
        assert result.microphone_level == 60

    @resp_lib.activate
    def test_audio_set_gen1_raises_hardware_unsupported(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_set(camera="Outdoor", mic_level=50)

        assert exc_info.value.code == "hardware_unsupported"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_audio_set_no_params_raises(self) -> None:
        """Calling set with no params raises permission_denied."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_set(camera="Indoor")

        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_audio_set_mic_level_boundary_min(self) -> None:
        """mic_level=0 is valid boundary."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json=_AUDIO_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={**_AUDIO_RESPONSE, "microphoneLevel": 0},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_set

        result = bosch_camera_audio_set(camera="Indoor", mic_level=0)
        assert result.microphone_level == 0

    @resp_lib.activate
    def test_audio_set_mic_level_boundary_max(self) -> None:
        """mic_level=100 is valid boundary."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json=_AUDIO_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={**_AUDIO_RESPONSE, "microphoneLevel": 100},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_audio_set

        result = bosch_camera_audio_set(camera="Indoor", mic_level=100)
        assert result.microphone_level == 100

    @resp_lib.activate
    def test_audio_set_mic_level_out_of_range_raises(self) -> None:
        """mic_level=101 is out of range — raises permission_denied."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_set(camera="Indoor", mic_level=101)

        assert exc_info.value.code == "invalid_argument"
        assert "101" in exc_info.value.detail

    @resp_lib.activate
    def test_audio_set_mic_level_negative_raises(self) -> None:
        """mic_level=-1 is out of range."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_set(camera="Indoor", mic_level=-1)

        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_audio_set_speaker_level_out_of_range_raises(self) -> None:
        """speaker_level=101 is out of range."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_audio_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_audio_set(camera="Indoor", speaker_level=101)

        assert exc_info.value.code == "invalid_argument"


# ---------------------------------------------------------------------------
# bosch_camera_intrusion_get
# ---------------------------------------------------------------------------


class TestIntrusionGet:
    @resp_lib.activate
    def test_intrusion_get_gen2_returns_config(self) -> None:
        """Gen2 camera returns mode, sensitivity, distance."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json=_INTRUSION_RESPONSE,
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_get

        result = bosch_camera_intrusion_get(camera="Indoor")
        assert result.mode == "ACTIVE"
        assert result.sensitivity == 3
        assert result.distance == 5

    @resp_lib.activate
    def test_intrusion_get_gen1_raises_hardware_unsupported(self) -> None:
        """Gen1 camera raises hardware_unsupported — no API call."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_get(camera="Outdoor")

        assert exc_info.value.code == "hardware_unsupported"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_intrusion_get_off_mode(self) -> None:
        """Mode can be OFF (disabled)."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json={"mode": "OFF", "sensitivity": 0, "distance": 1},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_get

        result = bosch_camera_intrusion_get(camera="Indoor")
        assert result.mode == "OFF"
        assert result.sensitivity == 0
        assert result.distance == 1


# ---------------------------------------------------------------------------
# bosch_camera_intrusion_set
# ---------------------------------------------------------------------------


class TestIntrusionSet:
    @resp_lib.activate
    def test_intrusion_set_sensitivity_only(self) -> None:
        """Set sensitivity only — mode + distance preserved via read-then-write."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json=_INTRUSION_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json={**_INTRUSION_RESPONSE, "sensitivity": 7},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        result = bosch_camera_intrusion_set(camera="Indoor", sensitivity=7)
        assert result.sensitivity == 7
        assert result.mode == "ACTIVE"  # preserved

    @resp_lib.activate
    def test_intrusion_set_mode_off(self) -> None:
        """Set mode to OFF."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json=_INTRUSION_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json={**_INTRUSION_RESPONSE, "mode": "OFF"},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        result = bosch_camera_intrusion_set(camera="Indoor", mode="OFF")
        assert result.mode == "OFF"

    @resp_lib.activate
    def test_intrusion_set_gen1_raises_hardware_unsupported(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_set(camera="Outdoor", sensitivity=3)

        assert exc_info.value.code == "hardware_unsupported"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_intrusion_set_no_params_raises(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_set(camera="Indoor")

        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_intrusion_set_sensitivity_boundary_min(self) -> None:
        """sensitivity=0 is valid lower boundary."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json=_INTRUSION_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json={**_INTRUSION_RESPONSE, "sensitivity": 0},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        result = bosch_camera_intrusion_set(camera="Indoor", sensitivity=0)
        assert result.sensitivity == 0

    @resp_lib.activate
    def test_intrusion_set_sensitivity_boundary_max(self) -> None:
        """sensitivity=7 is valid upper boundary."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json=_INTRUSION_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json={**_INTRUSION_RESPONSE, "sensitivity": 7},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        result = bosch_camera_intrusion_set(camera="Indoor", sensitivity=7)
        assert result.sensitivity == 7

    @resp_lib.activate
    def test_intrusion_set_sensitivity_out_of_range_raises(self) -> None:
        """sensitivity=8 is out of range."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_set(camera="Indoor", sensitivity=8)

        assert exc_info.value.code == "invalid_argument"
        assert "8" in exc_info.value.detail

    @resp_lib.activate
    def test_intrusion_set_sensitivity_negative_raises(self) -> None:
        """sensitivity=-1 is out of range."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_set(camera="Indoor", sensitivity=-1)

        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_intrusion_set_distance_boundary_min(self) -> None:
        """distance=1 is valid lower boundary."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json=_INTRUSION_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json={**_INTRUSION_RESPONSE, "distance": 1},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        result = bosch_camera_intrusion_set(camera="Indoor", distance=1)
        assert result.distance == 1

    @resp_lib.activate
    def test_intrusion_set_distance_boundary_max(self) -> None:
        """distance=10 is valid upper boundary."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json=_INTRUSION_RESPONSE,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            status=204,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            json={**_INTRUSION_RESPONSE, "distance": 10},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        result = bosch_camera_intrusion_set(camera="Indoor", distance=10)
        assert result.distance == 10

    @resp_lib.activate
    def test_intrusion_set_distance_zero_raises(self) -> None:
        """distance=0 is out of range (min is 1)."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_set(camera="Indoor", distance=0)

        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_intrusion_set_distance_out_of_range_raises(self) -> None:
        """distance=11 is out of range."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_set(camera="Indoor", distance=11)

        assert exc_info.value.code == "invalid_argument"
        assert "11" in exc_info.value.detail


# ---------------------------------------------------------------------------
# bosch_camera_wifi
# ---------------------------------------------------------------------------


class TestWifi:
    @resp_lib.activate
    def test_wifi_returns_rssi_ssid_strength(self) -> None:
        """WiFi info returns rssi, ssid, and derived signal_strength."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            json=_WIFI_RESPONSE,
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_wifi

        result = bosch_camera_wifi(camera="Indoor")
        assert result.rssi == -67
        assert result.ssid == "HomeNetwork"
        # -67 dBm → signal_strength = ((-67 + 100) * 2) = 66
        assert result.signal_strength == 66

    @resp_lib.activate
    def test_wifi_rssi_minus50_is_100_percent(self) -> None:
        """RSSI = -50 dBm → signal_strength = 100 %."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            json={"rssi": -50, "ssid": "Strong", "signalStrength": -50},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_wifi

        result = bosch_camera_wifi(camera="Indoor")
        assert result.signal_strength == 100

    @resp_lib.activate
    def test_wifi_rssi_minus100_is_0_percent(self) -> None:
        """RSSI = -100 dBm → signal_strength = 0 %."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            json={"rssi": -100, "ssid": "Weak", "signalStrength": -100},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_wifi

        result = bosch_camera_wifi(camera="Indoor")
        assert result.signal_strength == 0

    @resp_lib.activate
    def test_wifi_rssi_above_minus50_clamped_to_100(self) -> None:
        """RSSI better than -50 dBm is clamped at 100 %."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            json={"rssi": -30, "ssid": "VeryStrong", "signalStrength": -30},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_wifi

        result = bosch_camera_wifi(camera="Indoor")
        assert result.signal_strength == 100

    @resp_lib.activate
    def test_wifi_below_minus100_clamped_to_0(self) -> None:
        """RSSI worse than -100 dBm is clamped at 0 %."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            json={"rssi": -110, "ssid": "VeryWeak", "signalStrength": -110},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_wifi

        result = bosch_camera_wifi(camera="Indoor")
        assert result.signal_strength == 0

    @resp_lib.activate
    def test_wifi_outdoor_gen1_no_gate(self) -> None:
        """WiFi tool has no hardware gate — works for all cameras."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/wifiinfo",
            json={"rssi": -80, "ssid": "Net", "signalStrength": -80},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_wifi

        result = bosch_camera_wifi(camera="Outdoor")
        assert result.rssi == -80
        assert result.signal_strength == 40  # (-80 + 100) * 2

    @resp_lib.activate
    def test_wifi_missing_rssi_returns_none(self) -> None:
        """When rssi key is absent, rssi and signal_strength are None."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            json={"ssid": "NoRSSI"},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_wifi

        result = bosch_camera_wifi(camera="Indoor")
        assert result.rssi is None
        assert result.ssid == "NoRSSI"
        # signal_strength is None because rssi is absent
        assert result.signal_strength is None
