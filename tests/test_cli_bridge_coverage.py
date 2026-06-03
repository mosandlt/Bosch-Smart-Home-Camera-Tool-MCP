"""Coverage tests for bosch_camera_mcp.adapters.cli_bridge (uncovered lines).

Targets:
- get_cli_path (env override) — lines 44-47
- ensure_cli_importable (path inject + ImportError) — lines 55-58
- get_session_and_cameras:
    - custom config_path branch — lines 92-97
    - silent renewal success — lines 112-147
    - renewal ImportError path — lines 131-132
    - no token → reauth_required — lines 136-143
    - near-expiry renewal fails → proceed — lines 145-148
    - cloud 401 → reauth_required — line 163
    - cloud unreachable with cached → fallback — lines 189-196
    - cloud unreachable no cache → api_unreachable — lines 197-200
- _resolve_cam ambiguous match → MCPError — line 226
- _raise_api_error branches — 401/403/5xx/other
- set_privacy_mode / set_light / set_pan / set_notifications / get_audio /
  set_audio / get_intrusion_config / set_intrusion_config / trigger_siren /
  get_wifi_info / get_motion_config / set_motion_config /
  get_recording_options / set_recording_options /
  get_autofollow / set_autofollow / get_privacy_sound / set_privacy_sound
  — all `return False  # unreachable` lines reached via _raise_api_error
- trigger_siren unsupported model — lines 480-484
- get_wifi_info rssi>=0 quality branch — line 562
- set_motion_config enabled=False re-apply — line 609
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
import responses as resp_lib

CLOUD_API = "https://residential.cbs.boschsecurity.com"
CAM_ID = "aaaa-1111-bbbb-2222"

# Fake JWT with expiry far in the future (exp = 4102444800 = year 2100)
_VALID_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.placeholder"
# Fake JWT that is "near expiry" — exp = 0 (already expired)
_EXPIRED_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjB9.placeholder"

_BASE_CFG: dict = {
    "account": {
        "username": "test@example.com",
        "bearer_token": _VALID_TOKEN,
        "refresh_token": "",
    },
    "cameras": {
        "TestCam": {
            "id": CAM_ID,
            "name": "TestCam",
            "model": "HOME_Eyes_Indoor",
            "firmware": "9.40.25",
            "mac": "aa:bb:cc:dd:ee:ff",
            "download_folder": "TestCam",
            "local_ip": "",
            "local_username": "",
            "local_password": "",
        }
    },
    "settings": {},
    "nvr": {},
}

_VIDEO_INPUTS_RESPONSE = [
    {
        "id": CAM_ID,
        "title": "TestCam",
        "hardwareVersion": "HOME_Eyes_Indoor",
        "firmwareVersion": "9.40.25",
        "macAddress": "aa:bb:cc:dd:ee:ff",
    }
]


def _make_fake_bc(
    cfg: dict = _BASE_CFG, near_expiry: bool = False, expired: bool = False
) -> MagicMock:
    m = MagicMock()
    m.CLOUD_API = CLOUD_API
    m.DEFAULT_CONFIG = {"account": {}, "cameras": {}, "settings": {}, "nvr": {}}
    m.load_config.return_value = dict(cfg)
    m._merge_defaults = MagicMock()
    m._is_token_near_expiry.return_value = near_expiry
    m._is_token_expired.return_value = expired
    m.make_session.return_value = requests.Session()
    m.save_config.return_value = None
    return m


# ---------------------------------------------------------------------------
# Helpers — get_cli_path env override
# ---------------------------------------------------------------------------


class TestGetCliPath:
    def test_env_override_returns_custom_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BOSCH_CAMERA_CLI_PATH env var → get_cli_path returns that path."""
        monkeypatch.setenv("BOSCH_CAMERA_CLI_PATH", "/tmp/custom_cli")
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        result = bridge.get_cli_path()
        assert str(result) == "/tmp/custom_cli"

    def test_default_path_returned_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without env var get_cli_path returns DEFAULT_CLI_PATH."""
        monkeypatch.delenv("BOSCH_CAMERA_CLI_PATH", raising=False)
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        result = bridge.get_cli_path()
        assert result == bridge.DEFAULT_CLI_PATH


# ---------------------------------------------------------------------------
# ensure_cli_importable — path injection
# ---------------------------------------------------------------------------


class TestEnsureCliImportable:
    def test_path_injected_into_sys_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ensure_cli_importable inserts CLI path and succeeds when bosch_camera importable."""
        fake_bc = _make_fake_bc()
        monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)

        import bosch_camera_mcp.adapters.cli_bridge as bridge

        monkeypatch.setenv("BOSCH_CAMERA_CLI_PATH", "/tmp/fake_cli")
        # remove from sys.path to trigger insertion
        cli_str = "/tmp/fake_cli"
        orig_path = sys.path.copy()
        if cli_str in sys.path:
            sys.path.remove(cli_str)
        try:
            bridge.ensure_cli_importable()
            assert cli_str in sys.path
        finally:
            sys.path[:] = orig_path


# ---------------------------------------------------------------------------
# _raise_api_error — 401 / 403 / 5xx / other
# ---------------------------------------------------------------------------


class TestRaiseApiError:
    def _make_response(self, status: int, text: str = "") -> requests.Response:
        r = requests.Response()
        r.status_code = status
        r._content = text.encode()
        return r

    def test_401_raises_reauth_required(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge._raise_api_error(self._make_response(401), "test_ctx")
        assert exc.value.code == "reauth_required"
        assert "401" in exc.value.detail

    def test_403_raises_permission_denied(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge._raise_api_error(self._make_response(403), "test_ctx")
        assert exc.value.code == "permission_denied"
        assert "403" in exc.value.detail

    def test_500_raises_api_unreachable(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge._raise_api_error(self._make_response(500, "server error"), "test_ctx")
        assert exc.value.code == "api_unreachable"
        assert "500" in exc.value.detail

    def test_503_raises_api_unreachable(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge._raise_api_error(self._make_response(503), "test_ctx")
        assert exc.value.code == "api_unreachable"

    def test_other_status_raises_api_unreachable(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge._raise_api_error(self._make_response(422), "test_ctx")
        assert exc.value.code == "api_unreachable"
        assert "422" in exc.value.detail


# ---------------------------------------------------------------------------
# _resolve_cam — ambiguous match
# ---------------------------------------------------------------------------


class TestResolveCam:
    def test_ambiguous_match_raises_unknown_camera(self) -> None:
        """Two cameras matching the key → MCPError(unknown_camera)."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        cameras = {
            "Garten Vorne": {"id": "aaaa-1111", "name": "Garten Vorne"},
            "Garten Hinten": {"id": "bbbb-2222", "name": "Garten Hinten"},
        }
        with pytest.raises(MCPError) as exc:
            bridge._resolve_cam(cameras, "Garten")
        assert exc.value.code == "unknown_camera"
        assert "Ambiguous" in exc.value.detail

    def test_exact_match_returns_entry(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        cameras = {"MyCamera": {"id": "aaaa-1111", "name": "MyCamera"}}
        name, info = bridge._resolve_cam(cameras, "MyCamera")
        assert name == "MyCamera"
        assert info["id"] == "aaaa-1111"

    def test_partial_match_single_returns_entry(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        cameras = {"Garden Camera": {"id": "cccc-3333", "name": "Garden Camera"}}
        name, info = bridge._resolve_cam(cameras, "garden")
        assert name == "Garden Camera"

    def test_no_match_raises_unknown_camera(self) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        cameras = {"MyCamera": {"id": "aaaa-1111", "name": "MyCamera"}}
        with pytest.raises(MCPError) as exc:
            bridge._resolve_cam(cameras, "NonExistent")
        assert exc.value.code == "unknown_camera"
        assert "not found" in exc.value.detail


# ---------------------------------------------------------------------------
# get_session_and_cameras — token logic + cloud scenarios
# ---------------------------------------------------------------------------


class TestGetSessionAndCameras:
    def _setup_bridge(self, monkeypatch: pytest.MonkeyPatch, fake_bc: MagicMock) -> Any:
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
        monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())
        monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)
        return bridge

    @resp_lib.activate
    def test_no_token_raises_reauth_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing bearer_token raises MCPError(reauth_required)."""
        cfg_no_token = {
            "account": {"username": "t@t.com", "bearer_token": "", "refresh_token": ""},
            "cameras": {},
            "settings": {},
            "nvr": {},
        }
        fake_bc = _make_fake_bc(cfg=cfg_no_token, near_expiry=False, expired=False)
        fake_bc.load_config.return_value = dict(cfg_no_token)
        # needs_refresh fires because token is empty
        fake_bc._is_token_near_expiry.return_value = False
        fake_bc._is_token_expired.return_value = False
        bridge = self._setup_bridge(monkeypatch, fake_bc)

        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge.get_session_and_cameras()
        assert exc.value.code == "reauth_required"
        assert "No bearer token" in exc.value.detail

    @resp_lib.activate
    def test_custom_config_path_loads_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_path argument loads JSON from file and calls _merge_defaults."""
        fake_bc = _make_fake_bc()
        bridge = self._setup_bridge(monkeypatch, fake_bc)

        # Write temp config file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            json.dump(_BASE_CFG, fh)
            path = fh.name

        try:
            resp_lib.add(
                resp_lib.GET,
                f"{CLOUD_API}/v11/video_inputs",
                json=_VIDEO_INPUTS_RESPONSE,
                status=200,
            )
            cfg, session, cameras = bridge.get_session_and_cameras(config_path=path)
            fake_bc._merge_defaults.assert_called_once()
            assert "TestCam" in cameras
        finally:
            os.unlink(path)

    @resp_lib.activate
    def test_silent_renewal_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Token near-expiry + refresh_token → silent renewal succeeds."""
        cfg_near_expiry = {
            "account": {
                "username": "t@t.com",
                "bearer_token": _EXPIRED_TOKEN,
                "refresh_token": "fake-refresh-token",
            },
            "cameras": {},
            "settings": {},
            "nvr": {},
        }
        fake_bc = _make_fake_bc(cfg=cfg_near_expiry, near_expiry=True, expired=False)
        fake_bc.load_config.return_value = dict(cfg_near_expiry)

        bridge = self._setup_bridge(monkeypatch, fake_bc)

        # Mock get_token._do_refresh
        fake_get_token = MagicMock()
        fake_get_token._do_refresh.return_value = {
            "access_token": _VALID_TOKEN,
            "refresh_token": "new-refresh-token",
        }
        monkeypatch.setitem(sys.modules, "get_token", fake_get_token)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=_VIDEO_INPUTS_RESPONSE,
            status=200,
        )

        cfg, session, cameras = bridge.get_session_and_cameras()
        fake_bc.save_config.assert_called_once()
        assert "TestCam" in cameras

    @resp_lib.activate
    def test_renewal_import_error_proceeds_with_existing_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_token not available → ImportError path → proceed with existing token."""
        cfg_near = {
            "account": {
                "username": "t@t.com",
                "bearer_token": _VALID_TOKEN,
                "refresh_token": "some-refresh",
            },
            "cameras": {},
            "settings": {},
            "nvr": {},
        }
        fake_bc = _make_fake_bc(cfg=cfg_near, near_expiry=True, expired=False)
        fake_bc.load_config.return_value = dict(cfg_near)

        bridge = self._setup_bridge(monkeypatch, fake_bc)

        # Remove get_token from sys.modules to force ImportError
        monkeypatch.delitem(sys.modules, "get_token", raising=False)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=_VIDEO_INPUTS_RESPONSE,
            status=200,
        )

        cfg, session, cameras = bridge.get_session_and_cameras()
        assert "TestCam" in cameras

    @resp_lib.activate
    def test_cloud_401_raises_reauth_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cloud /v11/video_inputs returning 401 → MCPError(reauth_required)."""
        fake_bc = _make_fake_bc()
        bridge = self._setup_bridge(monkeypatch, fake_bc)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            status=401,
        )

        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge.get_session_and_cameras()
        assert exc.value.code == "reauth_required"
        assert "401" in exc.value.detail

    @resp_lib.activate
    def test_cloud_unreachable_with_cached_returns_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud request throws ConnectionError → fallback to cached cameras."""
        fake_bc = _make_fake_bc()
        bridge = self._setup_bridge(monkeypatch, fake_bc)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            body=ConnectionError("network down"),
        )

        cfg, session, cameras = bridge.get_session_and_cameras()
        assert "TestCam" in cameras

    @resp_lib.activate
    def test_cloud_unreachable_no_cache_raises_api_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud unreachable + no cached cameras → MCPError(api_unreachable)."""
        cfg_no_cache = {
            "account": {
                "username": "t@t.com",
                "bearer_token": _VALID_TOKEN,
                "refresh_token": "",
            },
            "cameras": {},  # empty cache
            "settings": {},
            "nvr": {},
        }
        fake_bc = _make_fake_bc(cfg=cfg_no_cache)
        fake_bc.load_config.return_value = dict(cfg_no_cache)
        bridge = self._setup_bridge(monkeypatch, fake_bc)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            body=ConnectionError("network down"),
        )

        from bosch_camera_mcp.errors import MCPError

        with pytest.raises(MCPError) as exc:
            bridge.get_session_and_cameras()
        assert exc.value.code == "api_unreachable"

    @resp_lib.activate
    def test_near_expiry_renewal_exception_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Silent renewal raises generic exception → log warning + proceed with token."""
        cfg_near = {
            "account": {
                "username": "t@t.com",
                "bearer_token": _VALID_TOKEN,
                "refresh_token": "some-refresh",
            },
            "cameras": {},
            "settings": {},
            "nvr": {},
        }
        fake_bc = _make_fake_bc(cfg=cfg_near, near_expiry=True, expired=False)
        fake_bc.load_config.return_value = dict(cfg_near)

        bridge = self._setup_bridge(monkeypatch, fake_bc)

        fake_get_token = MagicMock()
        fake_get_token._do_refresh.side_effect = RuntimeError("token server down")
        monkeypatch.setitem(sys.modules, "get_token", fake_get_token)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=_VIDEO_INPUTS_RESPONSE,
            status=200,
        )

        cfg, session, cameras = bridge.get_session_and_cameras()
        assert "TestCam" in cameras


# ---------------------------------------------------------------------------
# Error branches (return False / return {}) via _raise_api_error
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_session() -> requests.Session:
    return requests.Session()


class TestWriteHelperErrorBranches:
    """Each helper has a `return False # unreachable` after _raise_api_error.
    These lines are hit by having the HTTP endpoint return 4xx/5xx."""

    @resp_lib.activate
    def test_set_privacy_mode_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(resp_lib.PUT, f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/privacy", status=403)
        with pytest.raises(MCPError) as exc:
            bridge.set_privacy_mode(fake_session, CAM_ID, True)
        assert exc.value.code == "permission_denied"

    @resp_lib.activate
    def test_set_light_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/lighting_override",
            status=500,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_light(fake_session, CAM_ID, True)
        assert exc.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_pan_error_branch(self, fake_session: requests.Session) -> None:
        """set_pan PUT fails with 401 → reauth_required."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/pan",
            json={"panLimit": 120, "currentAbsolutePosition": 0},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/pan",
            status=401,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_pan(fake_session, CAM_ID, "home")
        assert exc.value.code == "reauth_required"

    @resp_lib.activate
    def test_set_pan_get_fails(self, fake_session: requests.Session) -> None:
        """set_pan GET fails → api_unreachable (line 322)."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/pan",
            status=503,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_pan(fake_session, CAM_ID, "left")
        assert exc.value.code == "api_unreachable"
        assert "pan state" in exc.value.detail

    @resp_lib.activate
    def test_set_notifications_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/enable_notifications",
            status=403,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_notifications(fake_session, CAM_ID, True)
        assert exc.value.code == "permission_denied"

    @resp_lib.activate
    def test_get_audio_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/audio",
            status=401,
        )
        with pytest.raises(MCPError) as exc:
            bridge.get_audio(fake_session, CAM_ID)
        assert exc.value.code == "reauth_required"

    @resp_lib.activate
    def test_set_audio_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/audio",
            json={"microphoneLevel": 50},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/audio",
            status=500,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_audio(fake_session, CAM_ID, mic_level=70)
        assert exc.value.code == "api_unreachable"

    @resp_lib.activate
    def test_get_intrusion_config_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/intrusionDetectionConfig",
            status=403,
        )
        with pytest.raises(MCPError) as exc:
            bridge.get_intrusion_config(fake_session, CAM_ID)
        assert exc.value.code == "permission_denied"

    @resp_lib.activate
    def test_set_intrusion_config_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/intrusionDetectionConfig",
            json={"detectionMode": "PERSON", "sensitivity": 3, "distance": 5},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/intrusionDetectionConfig",
            status=500,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_intrusion_config(fake_session, CAM_ID, sensitivity=5)
        assert exc.value.code == "api_unreachable"

    @resp_lib.activate
    def test_get_wifi_info_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/wifiinfo",
            status=401,
        )
        with pytest.raises(MCPError) as exc:
            bridge.get_wifi_info(fake_session, CAM_ID)
        assert exc.value.code == "reauth_required"

    @resp_lib.activate
    def test_get_motion_config_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/motion",
            status=500,
        )
        with pytest.raises(MCPError) as exc:
            bridge.get_motion_config(fake_session, CAM_ID)
        assert exc.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_motion_config_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/motion",
            json={"enabled": True, "motionAlarmConfiguration": "HIGH"},
            status=200,
        )
        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/motion",
            status=403,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_motion_config(fake_session, CAM_ID, enabled=False)
        assert exc.value.code == "permission_denied"

    @resp_lib.activate
    def test_get_recording_options_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/recording_options",
            status=500,
        )
        with pytest.raises(MCPError) as exc:
            bridge.get_recording_options(fake_session, CAM_ID)
        assert exc.value.code == "api_unreachable"

    @resp_lib.activate
    def test_set_recording_options_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/recording_options",
            status=401,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_recording_options(fake_session, CAM_ID, sound_on=True)
        assert exc.value.code == "reauth_required"

    @resp_lib.activate
    def test_get_autofollow_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/autofollow",
            status=403,
        )
        with pytest.raises(MCPError) as exc:
            bridge.get_autofollow(fake_session, CAM_ID)
        assert exc.value.code == "permission_denied"

    @resp_lib.activate
    def test_set_autofollow_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/autofollow",
            status=500,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_autofollow(fake_session, CAM_ID, enabled=True)
        assert exc.value.code == "api_unreachable"

    @resp_lib.activate
    def test_get_privacy_sound_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/privacy_sound_override",
            status=401,
        )
        with pytest.raises(MCPError) as exc:
            bridge.get_privacy_sound(fake_session, CAM_ID)
        assert exc.value.code == "reauth_required"

    @resp_lib.activate
    def test_set_privacy_sound_error_branch(self, fake_session: requests.Session) -> None:
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/privacy_sound_override",
            status=403,
        )
        with pytest.raises(MCPError) as exc:
            bridge.set_privacy_sound(fake_session, CAM_ID, enabled=True)
        assert exc.value.code == "permission_denied"


# ---------------------------------------------------------------------------
# trigger_siren — unsupported model
# ---------------------------------------------------------------------------


class TestTriggerSiren:
    def test_unsupported_model_raises_value_error(self, fake_session: requests.Session) -> None:
        """Non-Indoor model raises ValueError (lines 480-484)."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        with pytest.raises(ValueError, match="unsupported camera model"):
            bridge.trigger_siren(fake_session, CAM_ID, model="CAMERA_EYES")

    def test_unsupported_model_indoor_gen1_raises(self, fake_session: requests.Session) -> None:
        """INDOOR Gen1 model also raises ValueError."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        with pytest.raises(ValueError, match="unsupported camera model"):
            bridge.trigger_siren(fake_session, CAM_ID, model="INDOOR")

    @resp_lib.activate
    def test_trigger_siren_home_eyes_indoor_success(self, fake_session: requests.Session) -> None:
        """HOME_Eyes_Indoor model → PUT /panic_alarm → returns True."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/panic_alarm",
            status=204,
        )
        result = bridge.trigger_siren(fake_session, CAM_ID, model="HOME_Eyes_Indoor")
        assert result is True

    @resp_lib.activate
    def test_trigger_siren_stop(self, fake_session: requests.Session) -> None:
        """stop=True sends status=OFF."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/panic_alarm",
            status=200,
        )
        result = bridge.trigger_siren(fake_session, CAM_ID, model="HOME_Eyes_Indoor", stop=True)
        assert result is True

    @resp_lib.activate
    def test_trigger_siren_error_branch(self, fake_session: requests.Session) -> None:
        """trigger_siren HTTP error → _raise_api_error (line 488)."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge
        from bosch_camera_mcp.errors import MCPError

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/panic_alarm",
            status=403,
        )
        with pytest.raises(MCPError) as exc:
            bridge.trigger_siren(fake_session, CAM_ID, model="HOME_Eyes_Indoor")
        assert exc.value.code == "permission_denied"


# ---------------------------------------------------------------------------
# get_wifi_info — rssi >= 0 quality branch (line 562)
# ---------------------------------------------------------------------------


class TestWifiRssiQualityBranch:
    @resp_lib.activate
    def test_rssi_positive_small_passes_through_as_quality(
        self, fake_session: requests.Session
    ) -> None:
        """rssi=1 (positive, non-dBm quality value) → quality branch: signal_strength=1."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/wifiinfo",
            json={"rssi": 1, "ssid": "CamNet"},
            status=200,
        )
        result = bridge.get_wifi_info(fake_session, CAM_ID)
        assert result["signal_strength"] == 1

    @resp_lib.activate
    def test_rssi_positive_clamped_to_100(self, fake_session: requests.Session) -> None:
        """rssi=80 (quality 0-100 scale) → signal_strength = 80."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/wifiinfo",
            json={"rssi": 80, "ssid": "CamNet"},
            status=200,
        )
        result = bridge.get_wifi_info(fake_session, CAM_ID)
        assert result["signal_strength"] == 80

    @resp_lib.activate
    def test_rssi_over_100_clamped_to_100(self, fake_session: requests.Session) -> None:
        """rssi=120 → clamped at 100."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/wifiinfo",
            json={"rssi": 120, "ssid": "CamNet"},
            status=200,
        )
        result = bridge.get_wifi_info(fake_session, CAM_ID)
        assert result["signal_strength"] == 100


# ---------------------------------------------------------------------------
# set_motion_config — enabled=False re-apply (line 609)
# ---------------------------------------------------------------------------


class TestSetMotionConfigEnabledFalse:
    @resp_lib.activate
    def test_enabled_false_overrides_sensitivity_set(self, fake_session: requests.Session) -> None:
        """sensitivity + enabled=False → final body has enabled=False."""
        import bosch_camera_mcp.adapters.cli_bridge as bridge

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/motion",
            json={"enabled": True, "motionAlarmConfiguration": "HIGH"},
            status=200,
        )

        captured: list[Any] = []

        def _put_handler(request: Any) -> tuple[int, dict, str]:  # type: ignore[type-arg]
            import json as _json

            captured.append(_json.loads(request.body))
            return (204, {}, "")

        resp_lib.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID}/motion",
            callback=_put_handler,
        )

        result = bridge.set_motion_config(fake_session, CAM_ID, enabled=False, sensitivity="LOW")
        assert result is True
        assert len(captured) == 1
        assert captured[0]["enabled"] is False
        assert captured[0]["motionAlarmConfiguration"] == "LOW"
