"""Tests for v1.7.0 family-parity tools: motion zones, privacy masks, rules,
friends/camera-sharing, firmware install.

Cross-ported from the sister CLI's cmd_zones/cmd_privacy_masks/cmd_rules/
cmd_friends/cmd_firmware_update (docs/family-parity-plan.md §2b, 2026-07-11).
Mirrors the fixture/mocking pattern in test_audio_detection.py.
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
# motion zones
# ---------------------------------------------------------------------------


class TestMotionZones:
    @resp_lib.activate
    def test_get_returns_zones(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            json=[{"x": 0.0, "y": 0.3, "w": 0.67, "h": 0.7}],
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_motion_zones_get

        result = bosch_camera_motion_zones_get(camera="Indoor")
        assert len(result) == 1
        assert result[0].x == 0.0
        assert result[0].h == 0.7

    @resp_lib.activate
    def test_get_empty_list(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            json=[],
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_motion_zones_get

        assert bosch_camera_motion_zones_get(camera="Indoor") == []

    @resp_lib.activate
    def test_get_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_zones_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_zones_get(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_get_skips_malformed_entry(self) -> None:
        """A zone missing a required key is skipped, not a hard failure."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            json=[{"x": 0.1, "y": 0.1, "w": 0.1}, {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}],
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_motion_zones_get

        result = bosch_camera_motion_zones_get(camera="Indoor")
        assert len(result) == 1
        assert result[0].x == 0.2

    @resp_lib.activate
    def test_set_replaces_zones(self) -> None:
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (200, {}, "")

        resp_lib.add_callback(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_motion_zones_set

        zones = [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}]
        result = bosch_camera_motion_zones_set(camera="Indoor", zones=zones)
        assert len(result) == 1
        assert put_calls[0] == zones

    def test_set_missing_key_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_zones_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_zones_set(camera="Indoor", zones=[{"x": 0.1, "y": 0.1}])
        assert exc_info.value.code == "invalid_argument"
        assert "w" in exc_info.value.detail or "h" in exc_info.value.detail

    @pytest.mark.parametrize(
        "bad_zone",
        [
            {"x": -0.1, "y": 0.1, "w": 0.2, "h": 0.2},  # x below 0.0
            {"x": 0.1, "y": 1.5, "w": 0.2, "h": 0.2},  # y above 1.0
            {"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.2},  # w not > 0
            {"x": 0.1, "y": 0.1, "w": 0.2, "h": -0.2},  # h negative
            {"x": "bad", "y": 0.1, "w": 0.2, "h": 0.2},  # wrong type
        ],
    )
    def test_set_out_of_range_value_raises_invalid_argument(
        self, bad_zone: dict[str, Any]
    ) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_zones_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_zones_set(camera="Indoor", zones=[bad_zone])
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_clear_sends_empty_list(self) -> None:
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_motion_zones_clear

        result = bosch_camera_motion_zones_clear(camera="Indoor")
        assert result == []
        assert put_calls[0] == []

    @resp_lib.activate
    def test_set_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_zones_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_zones_set(
                camera="Indoor", zones=[{"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}]
            )
        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_clear_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion_sensitive_areas",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_zones_clear

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_zones_clear(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"


# ---------------------------------------------------------------------------
# privacy masks
# ---------------------------------------------------------------------------


class TestPrivacyMasks:
    @resp_lib.activate
    def test_get_returns_masks(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_masks",
            json=[{"x": 0.0, "y": 0.0, "w": 0.3, "h": 0.3}],
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_get

        result = bosch_camera_privacy_masks_get(camera="Indoor")
        assert len(result) == 1
        assert result[0].w == 0.3

    @resp_lib.activate
    def test_get_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_masks",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_privacy_masks_get(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_get_skips_malformed_entry(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_masks",
            json=[{"x": 0.1, "y": 0.1, "w": 0.1}, {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}],
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_get

        result = bosch_camera_privacy_masks_get(camera="Indoor")
        assert len(result) == 1
        assert result[0].x == 0.2

    @resp_lib.activate
    def test_set_replaces_masks(self) -> None:
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (200, {}, "")

        resp_lib.add_callback(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_masks",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_set

        masks = [{"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}]
        result = bosch_camera_privacy_masks_set(camera="Indoor", masks=masks)
        assert len(result) == 1
        assert put_calls[0] == masks

    def test_set_missing_key_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_privacy_masks_set(camera="Indoor", masks=[{"x": 0.1}])
        assert exc_info.value.code == "invalid_argument"

    def test_set_out_of_range_value_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_privacy_masks_set(
                camera="Indoor", masks=[{"x": 0.1, "y": 0.1, "w": 1.5, "h": 0.2}]
            )
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_clear_sends_empty_list(self) -> None:
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_masks",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_clear

        result = bosch_camera_privacy_masks_clear(camera="Indoor")
        assert result == []
        assert put_calls[0] == []

    @resp_lib.activate
    def test_set_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_masks",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_privacy_masks_set(
                camera="Indoor", masks=[{"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}]
            )
        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_clear_privacy_blocked(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_masks",
            json={"errorCode": "sh:camera.in.privacy.mode"},
            status=443,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_privacy_masks_clear

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_privacy_masks_clear(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

_RAW_RULE = {
    "id": "rule-1",
    "name": "Night Mode",
    "isActive": True,
    "startTime": "22:00:00",
    "endTime": "06:00:00",
    "weekdays": [0, 1, 2, 3, 4, 5, 6],
}


class TestRules:
    @resp_lib.activate
    def test_list_rules(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules",
            json=[_RAW_RULE],
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_rules_list

        result = bosch_camera_rules_list(camera="Indoor")
        assert len(result) == 1
        assert result[0].id == "rule-1"
        assert result[0].active is True
        assert result[0].days == [0, 1, 2, 3, 4, 5, 6]

    @resp_lib.activate
    def test_add_rule(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules",
            json=_RAW_RULE,
            status=201,
        )
        from bosch_camera_mcp.server import bosch_camera_rules_add

        result = bosch_camera_rules_add(
            camera="Indoor", name="Night Mode", start="22:00", end="06:00", days=[0, 1, 2]
        )
        assert result.id == "rule-1"
        assert result.name == "Night Mode"

    def test_add_rule_invalid_day_raises(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_add

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_add(
                camera="Indoor", name="X", start="00:00", end="01:00", days=[7]
            )
        assert exc_info.value.code == "invalid_argument"

    @pytest.mark.parametrize("bad_time", ["25:99", "foo", "9:00", "12:60"])
    def test_add_rule_invalid_time_format_raises(self, bad_time: str) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_add

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_add(
                camera="Indoor", name="X", start=bad_time, end="01:00", days=[0]
            )
        assert exc_info.value.code == "invalid_argument"

    def test_edit_rule_invalid_time_format_raises(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_edit

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_edit(camera="Indoor", rule_id="rule-1", start="nope")
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_edit_rule(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules",
            json=[_RAW_RULE],
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules/rule-1",
            body="",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_rules_edit

        result = bosch_camera_rules_edit(camera="Indoor", rule_id="rule-1", active=False)
        assert result.active is False

    @resp_lib.activate
    def test_edit_rule_all_fields(self) -> None:
        """Every optional field (name/start/end/days) applied in one call."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules",
            json=[_RAW_RULE],
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules/rule-1",
            body="",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_rules_edit

        result = bosch_camera_rules_edit(
            camera="Indoor",
            rule_id="rule-1",
            name="Renamed",
            start="10:00",
            end="12:00:30",
            days=[5, 6],
        )
        assert result.name == "Renamed"
        assert result.start == "10:00:00"
        assert result.end == "12:00:30"
        assert result.days == [5, 6]

    @resp_lib.activate
    def test_add_rule_api_error_raises(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_add

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_add(
                camera="Indoor", name="X", start="00:00", end="01:00", days=[0]
            )
        assert exc_info.value.code == "api_unreachable"

    def test_edit_rule_no_fields_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_edit

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_edit(camera="Indoor", rule_id="rule-1")
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_edit_rule_not_found_raises_invalid_argument(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules",
            json=[],
            status=200,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_edit

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_edit(camera="Indoor", rule_id="missing", active=True)
        assert exc_info.value.code == "invalid_argument"
        assert "missing" in exc_info.value.detail

    @resp_lib.activate
    def test_delete_rule(self) -> None:
        resp_lib.add(
            resp_lib.DELETE,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules/rule-1",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_rules_delete

        result = bosch_camera_rules_delete(camera="Indoor", rule_id="rule-1")
        assert result == {"deleted": True, "rule_id": "rule-1"}

    @resp_lib.activate
    def test_list_rules_api_error_raises(self) -> None:
        resp_lib.add(
            resp_lib.GET, f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules", status=500
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_list

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_list(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_delete_rule_api_error_raises(self) -> None:
        resp_lib.add(
            resp_lib.DELETE,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/rules/rule-1",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rules_delete

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rules_delete(camera="Indoor", rule_id="rule-1")
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# friends / camera sharing
# ---------------------------------------------------------------------------

_RAW_FRIEND = {
    "id": "friend-1",
    "email": "friend@example.com",
    "nickName": "Friend",
    "status": "ACCEPTED",
    "sharedVideoInputs": [{"videoInputId": CAM_ID_GEN2}],
}


class TestFriends:
    @resp_lib.activate
    def test_list_friends(self) -> None:
        resp_lib.add(
            resp_lib.GET, f"{CLOUD_API}/v11/friends", json=[_RAW_FRIEND], status=200
        )
        from bosch_camera_mcp.server import bosch_camera_friends_list

        result = bosch_camera_friends_list()
        assert len(result) == 1
        assert result[0].id == "friend-1"
        assert result[0].shared_cameras == [CAM_ID_GEN2]

    @resp_lib.activate
    def test_invite_friend(self) -> None:
        resp_lib.add(
            resp_lib.POST, f"{CLOUD_API}/v11/friends", json=_RAW_FRIEND, status=201
        )
        from bosch_camera_mcp.server import bosch_camera_friends_invite

        result = bosch_camera_friends_invite(email="friend@example.com")
        assert result.email == "friend@example.com"

    @resp_lib.activate
    def test_share_camera_no_prior_shares(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/friends",
            json=[{"id": "friend-1", "sharedVideoInputs": []}],
            status=200,
        )
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/friends/friend-1/share",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_friends_share

        result = bosch_camera_friends_share(friend_id="friend-1", camera="Indoor")
        assert result == {"shared": True, "friend_id": "friend-1", "camera": "Indoor"}
        assert put_calls[0] == [{"videoInputId": CAM_ID_GEN2}]

    @resp_lib.activate
    def test_share_camera_preserves_existing_shares(self) -> None:
        """Bug fix (2026-07-11): sharing a 2nd camera must NOT drop the 1st.

        PUT /share is full-replace on the Bosch side — share_camera() must
        read-modify-write by fetching the friend's current shares first.
        """
        other_cam_id = "other-cam-id"
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/friends",
            json=[
                {
                    "id": "friend-1",
                    "sharedVideoInputs": [{"videoInputId": other_cam_id}],
                }
            ],
            status=200,
        )
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/friends/friend-1/share",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_friends_share

        bosch_camera_friends_share(friend_id="friend-1", camera="Indoor")
        cam_ids = {entry["videoInputId"] for entry in put_calls[0]}
        assert cam_ids == {other_cam_id, CAM_ID_GEN2}

    @resp_lib.activate
    def test_share_camera_replaces_own_prior_entry(self) -> None:
        """Re-sharing the SAME camera (e.g. to change the days window) replaces
        only that camera's entry, not append a duplicate."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/friends",
            json=[
                {
                    "id": "friend-1",
                    "sharedVideoInputs": [{"videoInputId": CAM_ID_GEN2}],
                }
            ],
            status=200,
        )
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/friends/friend-1/share",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_friends_share

        bosch_camera_friends_share(friend_id="friend-1", camera="Indoor", days=3)
        assert len(put_calls[0]) == 1
        assert put_calls[0][0]["videoInputId"] == CAM_ID_GEN2
        assert "shareTime" in put_calls[0][0]

    @resp_lib.activate
    def test_share_camera_with_days_window(self) -> None:
        resp_lib.add(
            resp_lib.GET, f"{CLOUD_API}/v11/friends", json=[], status=200
        )
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/friends/friend-1/share",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_friends_share

        bosch_camera_friends_share(friend_id="friend-1", camera="Indoor", days=7)
        assert "shareTime" in put_calls[0][0]

    @resp_lib.activate
    def test_unshare_camera(self) -> None:
        put_calls: list[Any] = []

        def _cb(request: Any) -> tuple:
            put_calls.append(json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/friends/friend-1/share",
            callback=_cb,
        )
        from bosch_camera_mcp.server import bosch_camera_friends_unshare

        result = bosch_camera_friends_unshare(friend_id="friend-1")
        assert result == {"unshared": True, "friend_id": "friend-1"}
        assert put_calls[0] == []

    @resp_lib.activate
    def test_remove_friend(self) -> None:
        resp_lib.add(
            resp_lib.DELETE, f"{CLOUD_API}/v11/friends/friend-1", status=204
        )
        from bosch_camera_mcp.server import bosch_camera_friends_remove

        result = bosch_camera_friends_remove(friend_id="friend-1")
        assert result == {"removed": True, "friend_id": "friend-1"}

    @resp_lib.activate
    def test_list_friends_api_error_raises(self) -> None:
        resp_lib.add(resp_lib.GET, f"{CLOUD_API}/v11/friends", status=500)
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_friends_list

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_friends_list()
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_invite_friend_api_error_raises(self) -> None:
        resp_lib.add(resp_lib.POST, f"{CLOUD_API}/v11/friends", status=500)
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_friends_invite

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_friends_invite(email="friend@example.com")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_share_camera_api_error_raises(self) -> None:
        resp_lib.add(resp_lib.GET, f"{CLOUD_API}/v11/friends", json=[], status=200)
        resp_lib.add(resp_lib.PUT, f"{CLOUD_API}/v11/friends/friend-1/share", status=500)
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_friends_share

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_friends_share(friend_id="friend-1", camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_unshare_camera_api_error_raises(self) -> None:
        resp_lib.add(resp_lib.PUT, f"{CLOUD_API}/v11/friends/friend-1/share", status=500)
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_friends_unshare

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_friends_unshare(friend_id="friend-1")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_remove_friend_api_error_raises(self) -> None:
        resp_lib.add(resp_lib.DELETE, f"{CLOUD_API}/v11/friends/friend-1", status=500)
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_friends_remove

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_friends_remove(friend_id="friend-1")
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# firmware install
# ---------------------------------------------------------------------------


class TestFirmware:
    @resp_lib.activate
    def test_status_up_to_date(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            json={"current": "9.40.25", "upToDate": True, "updating": False, "update": None},
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_firmware_status

        result = bosch_camera_firmware_status(camera="Indoor")
        assert result.current == "9.40.25"
        assert result.up_to_date is True
        assert result.installing is False

    @resp_lib.activate
    def test_install_starts_update(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            json={
                "current": "9.40.25",
                "upToDate": False,
                "updating": False,
                "update": "9.40.104",
            },
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            body="",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_firmware_install

        result = bosch_camera_firmware_install(camera="Indoor")
        assert result.installing is True
        assert result.update_available == "9.40.104"

    @resp_lib.activate
    def test_install_already_in_progress_raises_invalid_argument(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            json={
                "current": "9.40.25",
                "upToDate": False,
                "updating": True,
                "update": "9.40.104",
            },
            status=200,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_firmware_install

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_firmware_install(camera="Indoor")
        assert exc_info.value.code == "invalid_argument"
        assert "in progress" in exc_info.value.detail

    @resp_lib.activate
    def test_install_no_update_available_raises_invalid_argument(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            json={"current": "9.40.25", "upToDate": True, "updating": False, "update": None},
            status=200,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_firmware_install

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_firmware_install(camera="Indoor")
        assert exc_info.value.code == "invalid_argument"
        assert "No firmware update" in exc_info.value.detail

    @resp_lib.activate
    def test_status_api_error_raises(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_firmware_status

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_firmware_status(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_install_put_failure_raises_api_unreachable(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            json={
                "current": "9.40.25",
                "upToDate": False,
                "updating": False,
                "update": "9.40.104",
            },
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/firmware",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_firmware_install

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_firmware_install(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"
