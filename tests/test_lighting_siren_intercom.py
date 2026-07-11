"""Tests for the v1.7.0 family-parity batch:

- bosch_camera_lighting_schedule_get/set (outdoor Eyes LED schedule)
- bosch_camera_siren_duration_set (Gen2 Indoor II alarm_settings)
- bosch_camera_intercom_open (listen-audio cloud-proxy tunnel)

Ported from Python CLI's cmd_lighting_schedule / cmd_siren --set-duration /
cmd_intercom. Mirrors the test pattern in test_audio_detection.py.
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

_CFG = {
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
            "has_light": True,
            "has_sound": False,
            "pan_limit": 0,
        },
    },
    "settings": {},
    "nvr": {},
}

_LIGHTING_RESPONSE = {
    "scheduleStatus": "FOLLOW_SCHEDULE",
    "generalLightOnTime": "18:00:00",
    "generalLightOffTime": "06:00:00",
    "darknessThreshold": 0.3,
    "lightOnMotion": True,
    "lightOnMotionFollowUpTimeSeconds": 30,
    "frontIlluminatorInGeneralLightOn": True,
    "frontIlluminatorGeneralLightIntensity": 80,
    "wallwasherInGeneralLightOn": False,
}


def _make_fake_bc(cfg: dict = _CFG) -> MagicMock:
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
# bosch_camera_lighting_schedule_get
# ---------------------------------------------------------------------------


class TestLightingScheduleGet:
    @resp_lib.activate
    def test_get_returns_full_schedule(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json=_LIGHTING_RESPONSE,
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_get

        result = bosch_camera_lighting_schedule_get(camera="Outdoor")
        assert result.schedule_status == "FOLLOW_SCHEDULE"
        assert result.on_time == "18:00:00"
        assert result.off_time == "06:00:00"
        assert result.darkness_threshold == 0.3
        assert result.light_on_motion is True
        assert result.light_on_motion_followup_secs == 30
        assert result.front_illuminator_on is True
        assert result.front_illuminator_intensity == 80
        assert result.wallwasher_on is False

    @resp_lib.activate
    def test_get_hardware_unsupported_442(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json={"errorCode": "sh:not.supported"},
            status=442,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lighting_schedule_get(camera="Outdoor")
        assert exc_info.value.code == "hardware_unsupported"

    @resp_lib.activate
    def test_get_camera_offline_444(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json={"errorCode": "sh:offline"},
            status=444,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lighting_schedule_get(camera="Outdoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_get_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lighting_schedule_get(camera="Outdoor")
        assert exc_info.value.code == "privacy_blocked"


# ---------------------------------------------------------------------------
# bosch_camera_lighting_schedule_set
# ---------------------------------------------------------------------------


class TestLightingScheduleSet:
    @resp_lib.activate
    def test_set_on_off_time_hhmm_appends_seconds(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json=_LIGHTING_RESPONSE,
            status=200,
        )
        put_calls: list[dict[str, Any]] = []

        def _put_callback(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            callback=_put_callback,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json={**_LIGHTING_RESPONSE, "generalLightOnTime": "19:00:00"},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_set

        result = bosch_camera_lighting_schedule_set(camera="Outdoor", on_time="19:00")
        assert result.on_time == "19:00:00"
        assert put_calls[0]["generalLightOnTime"] == "19:00:00"
        assert put_calls[0]["scheduleStatus"] == "FOLLOW_SCHEDULE"
        # Unmodified fields preserved from GET
        assert put_calls[0]["generalLightOffTime"] == "06:00:00"

    @resp_lib.activate
    def test_set_no_params_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lighting_schedule_set(camera="Outdoor")
        assert exc_info.value.code == "invalid_argument"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_set_darkness_threshold_out_of_range_raises(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lighting_schedule_set(camera="Outdoor", darkness_threshold=1.5)
        assert exc_info.value.code == "invalid_argument"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_set_malformed_time_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lighting_schedule_set(camera="Outdoor", on_time="25:99")
        assert exc_info.value.code == "invalid_argument"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_set_light_on_motion_and_threshold(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json=_LIGHTING_RESPONSE,
            status=200,
        )
        put_calls: list[dict[str, Any]] = []

        def _put_callback(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            callback=_put_callback,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/lighting_options",
            json={**_LIGHTING_RESPONSE, "lightOnMotion": False, "darknessThreshold": 0.7},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_lighting_schedule_set

        result = bosch_camera_lighting_schedule_set(
            camera="Outdoor", light_on_motion=False, darkness_threshold=0.7
        )
        assert result.light_on_motion is False
        assert result.darkness_threshold == 0.7
        assert put_calls[0]["lightOnMotion"] is False
        assert put_calls[0]["darknessThreshold"] == 0.7


# ---------------------------------------------------------------------------
# bosch_camera_siren_duration_set
# ---------------------------------------------------------------------------


class TestSirenDurationSet:
    @resp_lib.activate
    def test_set_duration_gen2_ok(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            json={"alarmDelayInSeconds": 30, "other": "x"},
            status=200,
        )
        put_calls: list[dict[str, Any]] = []

        def _put_callback(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            callback=_put_callback,
        )

        from bosch_camera_mcp.server import bosch_camera_siren_duration_set

        result = bosch_camera_siren_duration_set(camera="Indoor", seconds=120)
        assert result.alarm_delay_seconds == 120
        assert put_calls[0]["alarmDelayInSeconds"] == 120
        assert put_calls[0]["other"] == "x"  # preserved

    @resp_lib.activate
    def test_set_duration_gen1_raises_hardware_unsupported(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_siren_duration_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_siren_duration_set(camera="Outdoor", seconds=60)
        assert exc_info.value.code == "hardware_unsupported"
        assert len(resp_lib.calls) == 0

    @pytest.mark.parametrize("seconds", [0, 9, 301, -5, 1000])
    @resp_lib.activate
    def test_set_duration_out_of_range_raises(self, seconds: int) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_siren_duration_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_siren_duration_set(camera="Indoor", seconds=seconds)
        assert exc_info.value.code == "invalid_argument"
        assert len(resp_lib.calls) == 0

    @pytest.mark.parametrize("seconds", [10, 300])
    @resp_lib.activate
    def test_set_duration_boundary_values_ok(self, seconds: int) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            json={"alarmDelayInSeconds": 30},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            body="",
            status=204,
        )

        from bosch_camera_mcp.server import bosch_camera_siren_duration_set

        result = bosch_camera_siren_duration_set(camera="Indoor", seconds=seconds)
        assert result.alarm_delay_seconds == seconds

    @resp_lib.activate
    def test_set_duration_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            json={"alarmDelayInSeconds": 30},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_siren_duration_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_siren_duration_set(camera="Indoor", seconds=60)
        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_set_duration_get_fetch_fails_still_proceeds(self) -> None:
        """GET alarm_settings failing (e.g. 500) shouldn't block the PUT — CLI
        behavior is best-effort fetch, proceed with just the new field."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            json={"error": "boom"},
            status=500,
        )
        put_calls: list[dict[str, Any]] = []

        def _put_callback(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/alarm_settings",
            callback=_put_callback,
        )

        from bosch_camera_mcp.server import bosch_camera_siren_duration_set

        result = bosch_camera_siren_duration_set(camera="Indoor", seconds=60)
        assert result.alarm_delay_seconds == 60
        assert put_calls[0] == {"alarmDelayInSeconds": 60}


# ---------------------------------------------------------------------------
# bosch_camera_intercom_open
# ---------------------------------------------------------------------------


class TestIntercomOpen:
    @resp_lib.activate
    def test_open_default_no_speaker_level(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/connection",
            json={"urls": ["proxy.example.com:42090/deadbeef1234"]},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intercom_open

        result = bosch_camera_intercom_open(camera="Indoor")
        assert result.camera == "Indoor"
        assert result.duration == 60
        assert result.speaker_level_set is None
        assert result.rtsps_url.startswith("rtsps://proxy.example.com:443/deadbeef1234")
        assert "enableaudio=1" in result.rtsps_url
        assert "maxSessionDuration=60" in result.rtsps_url
        # no /audio call made since speaker_level wasn't provided
        audio_calls = [c for c in resp_lib.calls if "/audio" in c.request.url]
        assert len(audio_calls) == 0

    @resp_lib.activate
    def test_open_with_speaker_level_sets_it_first(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={"audioEnabled": True, "microphoneLevel": 40, "speakerLevel": 20},
            status=200,
        )
        put_audio_calls: list[dict[str, Any]] = []

        def _put_audio_cb(request: Any) -> tuple:
            put_audio_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            callback=_put_audio_cb,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/connection",
            json={"urls": ["proxy.example.com:42090/deadbeef1234"]},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intercom_open

        result = bosch_camera_intercom_open(camera="Indoor", duration=120, speaker_level=75)
        assert result.speaker_level_set == 75
        assert result.duration == 120
        assert put_audio_calls[0] == {
            "audioEnabled": True,
            "microphoneLevel": 40,
            "speakerLevel": 75,
        }

    @resp_lib.activate
    def test_open_connection_fails_raises_api_unreachable(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/connection",
            json={"error": "boom"},
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intercom_open

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intercom_open(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @pytest.mark.parametrize("speaker_level", [-1, 101])
    @resp_lib.activate
    def test_open_invalid_speaker_level_raises(self, speaker_level: int) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intercom_open

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intercom_open(camera="Indoor", speaker_level=speaker_level)
        assert exc_info.value.code == "invalid_argument"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_open_invalid_duration_raises(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intercom_open

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intercom_open(camera="Indoor", duration=0)
        assert exc_info.value.code == "invalid_argument"
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_open_speaker_level_write_failure_still_returns_session(self) -> None:
        """If the /audio PUT fails, the intercom session should still open
        (best-effort speaker level, matches CLI's warn-and-continue)."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={"audioEnabled": True, "microphoneLevel": 40, "speakerLevel": 20},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/audio",
            json={"error": "boom"},
            status=500,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/connection",
            json={"urls": ["proxy.example.com:42090/deadbeef1234"]},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intercom_open

        result = bosch_camera_intercom_open(camera="Indoor", speaker_level=75)
        assert result.speaker_level_set is None
        assert result.rtsps_url.startswith("rtsps://proxy.example.com:443/deadbeef1234")

    @resp_lib.activate
    def test_open_local_fallback_when_remote_fails(self) -> None:
        """REMOTE connection type fails (non-2xx) -> falls back to LOCAL."""
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/connection",
            json={"error": "remote unavailable"},
            status=503,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/connection",
            json={"urls": ["local-proxy.example.com:42090/cafebabe"]},
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_intercom_open

        result = bosch_camera_intercom_open(camera="Indoor")
        assert "local-proxy.example.com" in result.rtsps_url
