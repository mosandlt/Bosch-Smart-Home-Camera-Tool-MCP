"""Tests for the 2026-08-19 family-parity tools: image/video tuning surface,
soft/hard reset, and rename_camera.

Ported from the reference HA integration's switch.py/number.py/services.py
(docs/family-parity-plan.md, ground-truth gap audit 2026-08-19). Mirrors the
fixture/mocking pattern in test_v170_features.py.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import responses as resp_lib

CAM_ID_GEN2 = "gen2-aaaa-1111-aaaa"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

# Built from separate segments (not one literal) so static secret-scanners
# (gitleaks' "jwt" rule matches a contiguous header.payload.signature
# literal) don't flag this fake, far-future-expiry fixture as a real token —
# same reasoning as test_resources.py's header-only fixtures. Header decodes
# to {"alg":"HS256","typ":"JWT"}, payload to {"exp":4102444800} (2100-01-01);
# this token needs the real 3-segment structure (unlike most fixtures in this
# repo) because it exercises cli_bridge's actual `_is_token_near_expiry`/
# `_is_token_expired` decode path.
_JWT_HEADER = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
_JWT_PAYLOAD = "eyJleHAiOjQxMDI0NDQ4MDB9"
_VALID_TOKEN = _JWT_HEADER + "." + _JWT_PAYLOAD + ".placeholder"

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
    },
    "settings": {"time_zone": "Europe/Berlin"},
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
# timestamp overlay
# ---------------------------------------------------------------------------


class TestTimestampOverlay:
    @resp_lib.activate
    def test_get(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/timestamp",
            json={"result": True},
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_timestamp_overlay_get

        result = bosch_camera_timestamp_overlay_get(camera="Indoor")
        assert result.enabled is True

    @resp_lib.activate
    def test_set(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/timestamp",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_timestamp_overlay_set

        result = bosch_camera_timestamp_overlay_set(camera="Indoor", enabled=False)
        assert result.enabled is False

    @resp_lib.activate
    def test_set_api_error(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/timestamp",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_timestamp_overlay_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_timestamp_overlay_set(camera="Indoor", enabled=True)
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_get_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/timestamp",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_timestamp_overlay_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_timestamp_overlay_get(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# status LED
# ---------------------------------------------------------------------------


class TestStatusLed:
    @resp_lib.activate
    def test_get(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/ledlights",
            json={"state": "ON"},
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_status_led_get

        result = bosch_camera_status_led_get(camera="Indoor")
        assert result.enabled is True

    @resp_lib.activate
    def test_set(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/ledlights",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_status_led_set

        result = bosch_camera_status_led_set(camera="Indoor", enabled=False)
        assert result.enabled is False

    @resp_lib.activate
    def test_get_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/ledlights",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_status_led_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_status_led_get(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_api_error(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/ledlights",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_status_led_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_status_led_set(camera="Indoor", enabled=True)
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# lens elevation
# ---------------------------------------------------------------------------


class TestLensElevation:
    @resp_lib.activate
    def test_get(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lens_elevation",
            json={"elevation": 2.0},
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_lens_elevation_get

        result = bosch_camera_lens_elevation_get(camera="Indoor")
        assert result.meters == 2.0

    @resp_lib.activate
    def test_set(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lens_elevation",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_lens_elevation_set

        result = bosch_camera_lens_elevation_set(camera="Indoor", meters=3.5)
        assert result.meters == 3.5

    def test_set_out_of_range_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lens_elevation_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lens_elevation_set(camera="Indoor", meters=10.0)
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_get_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lens_elevation",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lens_elevation_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lens_elevation_get(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_api_error(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lens_elevation",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lens_elevation_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lens_elevation_set(camera="Indoor", meters=2.0)
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# darkness threshold / soft light fading
# ---------------------------------------------------------------------------


class TestDarknessThreshold:
    @resp_lib.activate
    def test_get(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting",
            json={"darknessThreshold": 0.47, "softLightFading": True},
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_get

        result = bosch_camera_darkness_threshold_get(camera="Indoor")
        assert result.threshold_percent == 47.0
        assert result.soft_light_fading is True

    @resp_lib.activate
    def test_set_threshold_only_preserves_fading(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting",
            json={"darknessThreshold": 0.5, "softLightFading": False},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_set

        result = bosch_camera_darkness_threshold_set(camera="Indoor", threshold_percent=80)
        assert result.threshold_percent == 80.0
        assert result.soft_light_fading is False

    def test_set_no_args_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_darkness_threshold_set(camera="Indoor")
        assert exc_info.value.code == "invalid_argument"

    def test_set_out_of_range_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_darkness_threshold_set(camera="Indoor", threshold_percent=150)
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_get_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_darkness_threshold_get(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting",
            json={"darknessThreshold": 0.5, "softLightFading": True},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_darkness_threshold_set(camera="Indoor", threshold_percent=10)
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# white balance
# ---------------------------------------------------------------------------

_LIGHTING_SWITCH_JSON = {
    "frontLightSettings": {"brightness": 50, "color": None, "whiteBalance": 0.2},
    "topLedLightSettings": {"brightness": 30, "color": None, "whiteBalance": 0.0},
    "bottomLedLightSettings": {"brightness": 10, "color": None, "whiteBalance": 0.0},
}


class TestWhiteBalance:
    @resp_lib.activate
    def test_get(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            json=_LIGHTING_SWITCH_JSON,
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_white_balance_get

        result = bosch_camera_white_balance_get(camera="Indoor")
        assert result.value == 0.2

    @resp_lib.activate
    def test_set_full_body_merge(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            json=_LIGHTING_SWITCH_JSON,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_white_balance_set

        result = bosch_camera_white_balance_set(camera="Indoor", value=-0.5)
        assert result.value == -0.5
        put_call = resp_lib.calls[-1]
        import json as _json

        body = _json.loads(put_call.request.body)
        # Full 3-group body required by the API, sibling groups untouched.
        assert body["topLedLightSettings"]["brightness"] == 30
        assert body["bottomLedLightSettings"]["brightness"] == 10
        assert body["frontLightSettings"]["whiteBalance"] == -0.5
        assert body["frontLightSettings"]["color"] is None

    def test_set_out_of_range_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_white_balance_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_white_balance_set(camera="Indoor", value=2.0)
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_get_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_white_balance_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_white_balance_get(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            json=_LIGHTING_SWITCH_JSON,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_white_balance_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_white_balance_set(camera="Indoor", value=0.1)
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# top/bottom LED brightness
# ---------------------------------------------------------------------------


class TestLedBrightness:
    @resp_lib.activate
    def test_get_top(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            json=_LIGHTING_SWITCH_JSON,
            status=200,
        )
        from bosch_camera_mcp.server import bosch_camera_led_brightness_get

        result = bosch_camera_led_brightness_get(camera="Indoor", position="top")
        assert result.brightness_percent == 30
        assert result.position == "top"

    @resp_lib.activate
    def test_set_bottom(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            json=_LIGHTING_SWITCH_JSON,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_led_brightness_set

        result = bosch_camera_led_brightness_set(
            camera="Indoor", position="bottom", brightness_percent=75
        )
        assert result.brightness_percent == 75
        assert result.position == "bottom"

    def test_invalid_position_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_led_brightness_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_led_brightness_get(camera="Indoor", position="middle")
        assert exc_info.value.code == "invalid_argument"

    def test_set_out_of_range_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_led_brightness_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_led_brightness_set(camera="Indoor", position="top", brightness_percent=-5)
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_get_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_led_brightness_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_led_brightness_get(camera="Indoor", position="top")
        assert exc_info.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_api_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            json=_LIGHTING_SWITCH_JSON,
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/lighting/switch",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_led_brightness_set

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_led_brightness_set(camera="Indoor", position="top", brightness_percent=50)
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# soft / hard reset
# ---------------------------------------------------------------------------


class TestSoftHardReset:
    @resp_lib.activate
    def test_soft_reset(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/soft_reset",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_soft_reset

        result = bosch_camera_soft_reset(camera="Indoor")
        assert result["rebooting"] is True

    @resp_lib.activate
    def test_soft_reset_404_raised_as_api_unreachable(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/soft_reset",
            json={"errorCode": "sh:entity.notfound"},
            status=404,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_soft_reset

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_soft_reset(camera="Indoor")
        assert exc_info.value.code == "api_unreachable"

    def test_hard_reset_requires_confirm(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_hard_reset

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_hard_reset(camera="Indoor")
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_hard_reset_with_confirm(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/hard_reset",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_hard_reset

        result = bosch_camera_hard_reset(camera="Indoor", confirm=True)
        assert result["factory_reset"] is True

    @resp_lib.activate
    def test_hard_reset_api_error(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/hard_reset",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_hard_reset

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_hard_reset(camera="Indoor", confirm=True)
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# rename_camera
# ---------------------------------------------------------------------------


class TestRenameCamera:
    @resp_lib.activate
    def test_rename(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs",
            status=204,
        )
        from bosch_camera_mcp.server import bosch_camera_rename

        result = bosch_camera_rename(camera="Indoor", new_name="Wohnzimmer")
        assert result["new_name"] == "Wohnzimmer"
        assert result["camera"] == "Indoor"
        put_call = resp_lib.calls[-1]
        import json as _json

        body = _json.loads(put_call.request.body)
        assert body["videoInputId"] == CAM_ID_GEN2
        assert body["title"] == "Wohnzimmer"
        assert body["timeZone"] == "Europe/Berlin"

    def test_rename_empty_name_raises_invalid_argument(self) -> None:
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rename

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rename(camera="Indoor", new_name="   ")
        assert exc_info.value.code == "invalid_argument"

    @resp_lib.activate
    def test_rename_api_error(self) -> None:
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs",
            status=500,
        )
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rename

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rename(camera="Indoor", new_name="Wohnzimmer")
        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# privacy-mode-blocked passthrough — every new tool must translate a raw
# "camera.in.privacy.mode" bridge exception into MCPError(code="privacy_blocked"),
# same as every pre-existing tool (see _wrap_privacy_blocked in server.py).
# ---------------------------------------------------------------------------

_PRIVACY_EXC = Exception("HTTP 443 camera.in.privacy.mode")


class TestPrivacyBlockedPassthrough:
    def test_timestamp_overlay_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_timestamp_overlay_get

        monkeypatch.setattr(
            bridge, "get_timestamp_overlay", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_timestamp_overlay_get(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    def test_status_led_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_status_led_get

        monkeypatch.setattr(
            bridge, "get_status_led", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_status_led_get(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    def test_lens_elevation_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lens_elevation_get

        monkeypatch.setattr(
            bridge, "get_lens_elevation", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_lens_elevation_get(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    def test_darkness_threshold_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_get

        monkeypatch.setattr(
            bridge, "get_global_lighting", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_darkness_threshold_get(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    def test_darkness_threshold_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_darkness_threshold_set

        monkeypatch.setattr(
            bridge, "set_global_lighting", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_darkness_threshold_set(camera="Indoor", threshold_percent=10)
        assert exc_info.value.code == "privacy_blocked"

    def test_white_balance_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_white_balance_get

        monkeypatch.setattr(
            bridge, "get_lighting_switch", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_white_balance_get(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    def test_white_balance_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_white_balance_set

        monkeypatch.setattr(
            bridge, "set_white_balance", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_white_balance_set(camera="Indoor", value=0.1)
        assert exc_info.value.code == "privacy_blocked"

    def test_led_brightness_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_led_brightness_get

        monkeypatch.setattr(
            bridge, "get_lighting_switch", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_led_brightness_get(camera="Indoor", position="top")
        assert exc_info.value.code == "privacy_blocked"

    def test_led_brightness_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_led_brightness_set

        monkeypatch.setattr(
            bridge, "set_led_brightness", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_led_brightness_set(camera="Indoor", position="top", brightness_percent=50)
        assert exc_info.value.code == "privacy_blocked"

    def test_soft_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_soft_reset

        monkeypatch.setattr(
            bridge, "soft_reset_camera", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_soft_reset(camera="Indoor")
        assert exc_info.value.code == "privacy_blocked"

    def test_hard_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_hard_reset

        monkeypatch.setattr(
            bridge, "hard_reset_camera", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_hard_reset(camera="Indoor", confirm=True)
        assert exc_info.value.code == "privacy_blocked"

    def test_rename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_rename

        monkeypatch.setattr(
            bridge, "rename_camera", lambda *a, **kw: (_ for _ in ()).throw(_PRIVACY_EXC)
        )
        with pytest.raises(MCPError) as exc_info:
            bosch_camera_rename(camera="Indoor", new_name="Wohnzimmer")
        assert exc_info.value.code == "privacy_blocked"
