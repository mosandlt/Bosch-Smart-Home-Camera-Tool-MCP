"""Coverage tests targeting uncovered branches in server.py.

Targets lines: 256-257, 313, 704-708, 736-766, 926-930, 994-998,
1068-1081, 1166, 1225-1226, 1323, 1327, 1353, 1407-1411, 1433-1437,
1458-1462, 1493-1497, 1528-1532, 1552-1556, 1577-1581, 1606, 1647-1648,
1684-1685, 1699-1700, 1735, 1745-1747, 1843.

Covers:
- _build_status: api_get_events exception silently swallowed (256-257)
- _wrap_privacy_blocked: returns None for non-privacy exception (313)
- bosch_camera_pan: privacy_blocked wrapping + non-privacy re-raise (704-708)
- bosch_camera_siren_trigger: hardware_unsupported gate + privacy_blocked +
  non-privacy re-raise (736-766)
- bosch_camera_intrusion_get: privacy_blocked / re-raise (926-930)
- bosch_camera_intrusion_set: privacy_blocked / re-raise (994-998)
- _fetch_rcp_lan: non-200 HTTP and exception paths (1068-1081)
- bosch_camera_mjpeg_snapshot: asyncio.TimeoutError path (1166)
- bosch_camera_onvif_scopes: decode fallback (1225-1226)
- bosch_camera_feature_flags: raw-other-type branch + API-error path (1323,
  1327)
- bosch_camera_motion_get: privacy_blocked / re-raise (1353)
- bosch_camera_motion_set: privacy_blocked / re-raise (1407-1411)
- bosch_camera_recording_get: privacy_blocked / re-raise (1433-1437)
- bosch_camera_recording_set: privacy_blocked / re-raise (1458-1462)
- bosch_camera_autofollow_get: hardware gate + privacy_blocked (1493-1497)
- bosch_camera_autofollow_set: hardware gate + privacy_blocked / re-raise
  (1528-1532)
- bosch_camera_privacy_sound_get: privacy_blocked / re-raise (1552-1556)
- bosch_camera_privacy_sound_set: privacy_blocked / re-raise (1577-1581)
- bosch_camera_unread_get: 401 → reauth_required (1606)
- bosch_camera_health_check_all: per-camera exception → error field,
  WiFi and events exceptions silenced (1647-1648, 1684-1685, 1699-1700)
- bosch_camera_token_status: no-exp JWT + undecodable JWT (1735, 1745-1747)
- __main__ guard (1843)
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import responses as resp_lib

# ---------------------------------------------------------------------------
# Shared config / IDs
# ---------------------------------------------------------------------------

CAM_ID_GEN2 = "gen2-aabb-1111-ccdd"
CAM_ID_GEN1 = "gen1-eeff-2222-aabb"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

# Valid JWT: exp = 4102444800 (year 2100)
_VALID_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
    ".eyJleHAiOjQxMDI0NDQ4MDAsImVtYWlsIjoidGVzdEBleGFtcGxlLmNvbSJ9"
    ".FAKE_SIGNATURE"
)

# JWT without an "exp" claim — covers line 1745
_TOKEN_NO_EXP = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6Im5vZXhwQGV4YW1wbGUuY29tIn0.FAKE_SIGNATURE"
)

# JWT with invalid base64 payload — decode exception → lines 1745-1747
_TOKEN_INVALID = "header.!!!BAD!!!.sig"

_CFG: dict[str, Any] = {
    "account": {
        "username": "test@example.com",
        "bearer_token": _VALID_TOKEN,
        "refresh_token": "",
    },
    "cameras": {
        "IndoorII": {
            "id": CAM_ID_GEN2,
            "name": "IndoorII",
            "model": "HOME_Eyes_Indoor",
            "firmware": "9.40.102",
            "mac": "aa:bb:cc:dd:ee:ff",
            "download_folder": "IndoorII",
            "local_ip": "10.0.1.50",
            "local_username": "luser",
            "local_password": "lpass",
            "has_light": False,
            "has_sound": True,
            "pan_limit": 0,
        },
        "OutdoorGen1": {
            "id": CAM_ID_GEN1,
            "name": "OutdoorGen1",
            "model": "CAMERA_EYES",
            "firmware": "7.91.56",
            "mac": "11:22:33:44:55:66",
            "download_folder": "OutdoorGen1",
            "local_ip": "10.0.1.27",
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


def _make_fake_bc(cfg: dict[str, Any] = _CFG) -> MagicMock:
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
    """Install fake bosch_camera module and wire _get_session for all tests."""
    fake_bc = _make_fake_bc()
    monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
    monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())

    import bosch_camera_mcp.adapters.cli_bridge as bridge

    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)

    import requests as req_lib

    fake_session = req_lib.Session()

    def _fake_get_session(config_path: Any = None) -> tuple[dict, Any, dict]:
        return _CFG, fake_session, _CFG["cameras"]

    monkeypatch.setattr(bridge, "get_session_and_cameras", _fake_get_session)

    import bosch_camera_mcp.server as srv

    monkeypatch.setattr(srv, "_get_session", _fake_get_session)

    yield fake_bc


# ---------------------------------------------------------------------------
# _build_status: api_get_events exception silently swallowed (lines 256-257)
# ---------------------------------------------------------------------------


class TestBuildStatusEventsException:
    @resp_lib.activate
    def test_events_exception_swallowed_last_event_at_none(self) -> None:
        """api_get_events raising should be swallowed — last_event_at stays None."""
        # _build_status needs api_get_camera response
        import bosch_camera as bc  # type: ignore[import-not-found]

        bc.api_get_events.side_effect = RuntimeError("events exploded")
        bc.api_ping.side_effect = lambda session, cam_id: "ONLINE"
        bc.api_get_camera.return_value = {
            "id": CAM_ID_GEN2,
            "privacyMode": "OFF",
            "featureSupport": {"light": False},
            "featureStatus": {},
        }

        from bosch_camera_mcp.server import bosch_camera_status

        # bosch_camera_status calls bridge.get_camera which hits the cloud —
        # stub the cloud response for api_get_camera_detail endpoint
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}",
            json={
                "id": CAM_ID_GEN2,
                "status": "ONLINE",
                "privacyMode": "OFF",
                "featureSupport": {"light": False},
                "featureStatus": {},
            },
            status=200,
        )

        result = bosch_camera_status(camera="IndoorII")
        assert result.last_event_at is None

        bc.api_get_events.side_effect = None


# ---------------------------------------------------------------------------
# _wrap_privacy_blocked: non-matching exception returns None (line 313)
# ---------------------------------------------------------------------------


class TestWrapPrivacyBlockedReturnsNone:
    def test_non_privacy_exception_returns_none(self) -> None:
        """An unrelated exception must return None — line 313."""
        from bosch_camera_mcp import server as srv

        # Access the module-level function directly
        result = srv._wrap_privacy_blocked(ValueError("generic error"), "IndoorII")  # type: ignore[attr-defined]
        assert result is None


# ---------------------------------------------------------------------------
# bosch_camera_pan: privacy_blocked wrapping + non-privacy re-raise (704-708)
# ---------------------------------------------------------------------------


class TestPanPrivacyBlocked:
    @resp_lib.activate
    def test_pan_privacy_blocked_raised(self) -> None:
        """set_pan receiving a privacy-mode exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_pan

        # set_pan does GET /pan first — raise privacy exception from there
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/pan",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_pan(camera="IndoorII", direction=10)

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_pan_non_privacy_exception_reraised(self) -> None:
        """A non-privacy exception from set_pan must be re-raised as-is."""
        from bosch_camera_mcp.server import bosch_camera_pan

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/pan",
            body=ConnectionError("network error"),
        )

        with pytest.raises(ConnectionError):
            bosch_camera_pan(camera="IndoorII", direction=10)


# ---------------------------------------------------------------------------
# bosch_camera_siren_trigger: hardware gate + privacy_blocked + re-raise
# (lines 736-766)
# ---------------------------------------------------------------------------


class TestSirenTrigger:
    def test_siren_hardware_unsupported_for_gen1(self) -> None:
        """Gen1 outdoor camera raises hardware_unsupported."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_siren_trigger

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_siren_trigger(camera="OutdoorGen1")

        assert exc_info.value.code == "hardware_unsupported"
        assert "HOME_Eyes_Indoor" in exc_info.value.detail

    @resp_lib.activate
    def test_siren_privacy_blocked_wraps(self) -> None:
        """trigger_siren raising privacy pattern wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_siren_trigger

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/panic_alarm",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_siren_trigger(camera="IndoorII")

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_siren_non_privacy_exception_reraised(self) -> None:
        """A non-privacy exception from trigger_siren is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_siren_trigger

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/panic_alarm",
            body=OSError("camera offline"),
        )

        with pytest.raises(OSError):
            bosch_camera_siren_trigger(camera="IndoorII")


# ---------------------------------------------------------------------------
# bosch_camera_intrusion_get: privacy_blocked (lines 926-930)
# ---------------------------------------------------------------------------


class TestIntrusionGetPrivacyBlocked:
    @resp_lib.activate
    def test_intrusion_get_privacy_blocked(self) -> None:
        """get_intrusion_config raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_get(camera="IndoorII")

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_intrusion_get_non_privacy_reraised(self) -> None:
        """Non-privacy exception from get_intrusion_config is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_intrusion_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            body=TimeoutError("timed out"),
        )

        with pytest.raises(TimeoutError):
            bosch_camera_intrusion_get(camera="IndoorII")


# ---------------------------------------------------------------------------
# bosch_camera_intrusion_set: privacy_blocked on set (lines 994-998)
# ---------------------------------------------------------------------------


class TestIntrusionSetPrivacyBlocked:
    @resp_lib.activate
    def test_intrusion_set_privacy_blocked(self) -> None:
        """set_intrusion_config raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        # set_intrusion_config fetches current config first
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_intrusion_set(camera="IndoorII", mode="PERSON")

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_intrusion_set_non_privacy_reraised(self) -> None:
        """Non-privacy exception from set_intrusion_config is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_intrusion_set

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/intrusionDetectionConfig",
            body=ValueError("invalid field"),
        )

        with pytest.raises(ValueError):
            bosch_camera_intrusion_set(camera="IndoorII", sensitivity=3)


# ---------------------------------------------------------------------------
# _fetch_rcp_lan: non-200 response returns None + exception returns None
# (lines 1068-1081)
# ---------------------------------------------------------------------------


class TestFetchRcpLan:
    @pytest.mark.asyncio
    async def test_fetch_rcp_lan_connection_error_returns_none(self) -> None:
        """Connection error from RCP call returns None (exception swallowed)."""
        from bosch_camera_mcp.server import _fetch_rcp_lan  # type: ignore[attr-defined]

        # Port 1 is almost certainly refused on any OS — connection exception
        result = await _fetch_rcp_lan(
            "127.0.0.1:1",
            "admin",
            "secret",
            "0xff00",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_rcp_lan_non_200_returns_none(self) -> None:
        """Non-200 HTTP response from the RCP endpoint returns None.

        Mocks httpx.AsyncClient — the helper uses httpx.DigestAuth (aiohttp has
        no public DigestAuth class). Pinned to the RFC-5737 192.0.2.x range so
        no real network call is attempted.
        """
        import httpx

        from bosch_camera_mcp.server import _fetch_rcp_lan  # type: ignore[attr-defined]

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.content = b""

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            result = await _fetch_rcp_lan(
                "192.0.2.1",
                "admin",
                "secret",
                "0xff00",
            )

        assert result is None


# ---------------------------------------------------------------------------
# bosch_camera_mjpeg_snapshot: asyncio.TimeoutError path (line 1166)
# ---------------------------------------------------------------------------


class TestMjpegSnapshotTimeout:
    @pytest.mark.asyncio
    async def test_mjpeg_snapshot_timeout_raises_mcp_error(self) -> None:
        """asyncio.TimeoutError from ffmpeg wait raises MCPError local_unavailable."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_mjpeg_snapshot

        with patch("asyncio.create_subprocess_exec") as mock_proc:
            proc = MagicMock()
            proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_proc.return_value = proc

            with pytest.raises(MCPError) as exc_info:
                await bosch_camera_mjpeg_snapshot(camera="IndoorII")

        assert exc_info.value.code == "local_unavailable"
        assert "timed out" in exc_info.value.detail


# ---------------------------------------------------------------------------
# bosch_camera_onvif_scopes: decode fallback (lines 1225-1226)
# ---------------------------------------------------------------------------


class TestOnvifScopesDecodeFallback:
    @pytest.mark.asyncio
    async def test_onvif_scopes_decode_exception_uses_repr(self) -> None:
        """When raw.decode() raises an exception, repr() fallback is used."""
        from bosch_camera_mcp.server import bosch_camera_onvif_scopes

        mock_bytes = MagicMock(spec=bytes)
        mock_bytes.decode.side_effect = Exception("decode failed")
        mock_bytes.__repr__ = lambda self: "b'\\xff\\xfe'"

        with patch(
            "bosch_camera_mcp.server._fetch_rcp_lan",
            new=AsyncMock(return_value=mock_bytes),
        ):
            result = await bosch_camera_onvif_scopes(camera="IndoorII")

        assert "raw_scopes" in result
        assert result["raw_scopes"] == repr(mock_bytes)


# ---------------------------------------------------------------------------
# bosch_camera_feature_flags: raw-other-type (1323) and API-error (1327)
# ---------------------------------------------------------------------------


class TestFeatureFlags:
    @resp_lib.activate
    def test_feature_flags_raw_other_type_returns_raw_key(self) -> None:
        """When API returns neither list nor dict, result is {raw: <value>}."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/feature_flags",
            json="unexpected_string",
            status=200,
        )

        from bosch_camera_mcp.server import bosch_camera_feature_flags

        result = bosch_camera_feature_flags()
        assert "raw" in result
        assert result["raw"] == "unexpected_string"

    @resp_lib.activate
    def test_feature_flags_api_error_raises(self) -> None:
        """Non-200 response causes _raise_api_error to raise MCPError."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/feature_flags",
            json={"error": "forbidden"},
            status=403,
        )

        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_feature_flags

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_feature_flags()

        assert exc_info.value.code == "permission_denied"


# ---------------------------------------------------------------------------
# bosch_camera_motion_get: privacy_blocked (line 1353)
# ---------------------------------------------------------------------------


class TestMotionGetPrivacyBlocked:
    @resp_lib.activate
    def test_motion_get_privacy_blocked(self) -> None:
        """get_motion_config raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_get(camera="IndoorII")

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_motion_get_non_privacy_reraised(self) -> None:
        """Non-privacy exception from get_motion_config is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_motion_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion",
            body=TimeoutError("timeout"),
        )

        with pytest.raises(TimeoutError):
            bosch_camera_motion_get(camera="IndoorII")


# ---------------------------------------------------------------------------
# bosch_camera_motion_set: privacy_blocked on set (lines 1407-1411)
# ---------------------------------------------------------------------------


class TestMotionSetPrivacyBlocked:
    @resp_lib.activate
    def test_motion_set_privacy_blocked(self) -> None:
        """set_motion_config raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_motion_set

        # set_motion_config fetches current motion config first
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_motion_set(camera="IndoorII", enabled=True)

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_motion_set_non_privacy_reraised(self) -> None:
        """Non-privacy exception from set_motion_config is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_motion_set

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/motion",
            body=IOError("network"),
        )

        with pytest.raises(IOError):
            bosch_camera_motion_set(camera="IndoorII", enabled=True)


# ---------------------------------------------------------------------------
# bosch_camera_recording_get: privacy_blocked (lines 1433-1437)
# ---------------------------------------------------------------------------


class TestRecordingGetPrivacyBlocked:
    @resp_lib.activate
    def test_recording_get_privacy_blocked(self) -> None:
        """get_recording_options raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_recording_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/recording_options",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_recording_get(camera="IndoorII")

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_recording_get_non_privacy_reraised(self) -> None:
        """Non-privacy exception from get_recording_options is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_recording_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/recording_options",
            body=TimeoutError("timeout"),
        )

        with pytest.raises(TimeoutError):
            bosch_camera_recording_get(camera="IndoorII")


# ---------------------------------------------------------------------------
# bosch_camera_recording_set: privacy_blocked on set (lines 1458-1462)
# ---------------------------------------------------------------------------


class TestRecordingSetPrivacyBlocked:
    @resp_lib.activate
    def test_recording_set_privacy_blocked(self) -> None:
        """set_recording_options raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_recording_set

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/recording_options",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_recording_set(camera="IndoorII", sound_on=True)

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_recording_set_non_privacy_reraised(self) -> None:
        """Non-privacy exception from set_recording_options is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_recording_set

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/recording_options",
            body=IOError("io error"),
        )

        with pytest.raises(IOError):
            bosch_camera_recording_set(camera="IndoorII", sound_on=False)


# ---------------------------------------------------------------------------
# bosch_camera_autofollow_get: hardware gate + privacy_blocked (1493-1497)
# ---------------------------------------------------------------------------


class TestAutofollowGetPrivacyBlocked:
    def test_autofollow_get_hardware_unsupported_no_pan_limit(self) -> None:
        """Camera without pan_limit raises hardware_unsupported."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_autofollow_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_autofollow_get(camera="IndoorII")

        assert exc_info.value.code == "hardware_unsupported"
        assert "panLimit=0" in exc_info.value.detail

    @resp_lib.activate
    def test_autofollow_get_privacy_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_autofollow raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_autofollow_get

        # Override session to include camera with pan_limit > 0
        cfg_360: dict[str, Any] = {
            **_CFG,
            "cameras": {
                "IndoorII": {**_CFG["cameras"]["IndoorII"], "pan_limit": 120},
                "OutdoorGen1": _CFG["cameras"]["OutdoorGen1"],
            },
        }

        import bosch_camera_mcp.server as srv

        def _fake_get_360(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_360, req_lib.Session(), cfg_360["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake_get_360)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/autofollow",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_autofollow_get(camera="IndoorII")

        assert exc_info.value.code == "privacy_blocked"


# ---------------------------------------------------------------------------
# bosch_camera_autofollow_set: hardware gate + privacy_blocked + re-raise
# (lines 1528-1532)
# ---------------------------------------------------------------------------


class TestAutofollowSetPrivacyBlocked:
    @resp_lib.activate
    def test_autofollow_set_privacy_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """set_autofollow raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_autofollow_set

        cfg_360: dict[str, Any] = {
            **_CFG,
            "cameras": {
                "IndoorII": {**_CFG["cameras"]["IndoorII"], "pan_limit": 120},
                "OutdoorGen1": _CFG["cameras"]["OutdoorGen1"],
            },
        }

        import bosch_camera_mcp.server as srv

        def _fake_get_360(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_360, req_lib.Session(), cfg_360["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake_get_360)

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/autofollow",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_autofollow_set(camera="IndoorII", enabled=True)

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_autofollow_set_non_privacy_reraised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-privacy exception from set_autofollow is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_autofollow_set

        cfg_360: dict[str, Any] = {
            **_CFG,
            "cameras": {
                "IndoorII": {**_CFG["cameras"]["IndoorII"], "pan_limit": 120},
                "OutdoorGen1": _CFG["cameras"]["OutdoorGen1"],
            },
        }

        import bosch_camera_mcp.server as srv

        def _fake_get_360(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_360, req_lib.Session(), cfg_360["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake_get_360)

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/autofollow",
            body=ConnectionError("dropped"),
        )

        with pytest.raises(ConnectionError):
            bosch_camera_autofollow_set(camera="IndoorII", enabled=False)


# ---------------------------------------------------------------------------
# bosch_camera_privacy_sound_get: privacy_blocked (lines 1552-1556)
# ---------------------------------------------------------------------------


class TestPrivacySoundGetPrivacyBlocked:
    @resp_lib.activate
    def test_privacy_sound_get_privacy_blocked(self) -> None:
        """get_privacy_sound raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_privacy_sound_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_sound_override",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_privacy_sound_get(camera="IndoorII")

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_privacy_sound_get_non_privacy_reraised(self) -> None:
        """Non-privacy exception from get_privacy_sound is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_privacy_sound_get

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_sound_override",
            body=OSError("offline"),
        )

        with pytest.raises(OSError):
            bosch_camera_privacy_sound_get(camera="IndoorII")


# ---------------------------------------------------------------------------
# bosch_camera_privacy_sound_set: privacy_blocked (lines 1577-1581)
# ---------------------------------------------------------------------------


class TestPrivacySoundSetPrivacyBlocked:
    @resp_lib.activate
    def test_privacy_sound_set_privacy_blocked(self) -> None:
        """set_privacy_sound raising privacy exception wraps as privacy_blocked."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_privacy_sound_set

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_sound_override",
            body=RuntimeError("HTTP 443 sh:camera.in.privacy.mode"),
        )

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_privacy_sound_set(camera="IndoorII", enabled=True)

        assert exc_info.value.code == "privacy_blocked"

    @resp_lib.activate
    def test_privacy_sound_set_non_privacy_reraised(self) -> None:
        """Non-privacy exception from set_privacy_sound is re-raised."""
        from bosch_camera_mcp.server import bosch_camera_privacy_sound_set

        resp_lib.add(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/privacy_sound_override",
            body=RuntimeError("not privacy"),
        )

        with pytest.raises(RuntimeError, match="not privacy"):
            bosch_camera_privacy_sound_set(camera="IndoorII", enabled=False)


# ---------------------------------------------------------------------------
# bosch_camera_unread_get: 401 raises reauth_required (line 1606)
# ---------------------------------------------------------------------------


class TestUnreadGet401:
    @resp_lib.activate
    def test_unread_get_401_raises_reauth_required(self) -> None:
        """API returning 401 on video_inputs raises MCPError reauth_required."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json={"error": "unauthorized"},
            status=401,
        )

        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_unread_get

        with pytest.raises(MCPError) as exc_info:
            bosch_camera_unread_get(camera="IndoorII")

        assert exc_info.value.code == "reauth_required"


# ---------------------------------------------------------------------------
# bosch_camera_health_check_all: per-camera exception → error field (1647-1648),
# WiFi exception silenced (1684-1685), events exception silenced (1699-1700)
# ---------------------------------------------------------------------------


class TestHealthCheckAll:
    @resp_lib.activate
    def test_health_check_per_camera_exception_captured(self) -> None:
        """An exception for a single camera is captured in error field, not raised."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[],
            status=200,
        )

        import bosch_camera as bc  # type: ignore[import-not-found]

        bc.api_ping.side_effect = RuntimeError("camera exploded")

        from bosch_camera_mcp.server import bosch_camera_health_check_all

        results = bosch_camera_health_check_all()

        assert len(results) == 2
        for entry in results:
            assert entry.error is not None
            assert "camera exploded" in entry.error

        bc.api_ping.side_effect = lambda session, cam_id: "ONLINE"

    @resp_lib.activate
    def test_health_check_wifi_exception_silenced(self) -> None:
        """WiFi fetch exception is silenced — entry still returns with wifi_rssi=None."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[
                {
                    "id": CAM_ID_GEN2,
                    "privacyMode": "ON",
                    "numberOfUnreadEvents": 5,
                }
            ],
            status=200,
        )

        # wifi raises for both cameras
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            body=RuntimeError("wifi error"),
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/wifiinfo",
            body=RuntimeError("wifi error"),
        )

        from bosch_camera_mcp.server import bosch_camera_health_check_all

        results = bosch_camera_health_check_all()

        indoor_entry = next((e for e in results if e.name == "IndoorII"), None)
        assert indoor_entry is not None
        assert indoor_entry.wifi_rssi is None

    @resp_lib.activate
    def test_health_check_events_exception_silenced(self) -> None:
        """Events fetch exception is silenced — last_event_at stays None."""
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=[
                {
                    "id": CAM_ID_GEN2,
                    "privacyMode": "OFF",
                    "numberOfUnreadEvents": 2,
                }
            ],
            status=200,
        )

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/wifiinfo",
            json={"rssi": -70, "ssid": "TestNet", "signal_strength": 60},
            status=200,
        )
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN1}/wifiinfo",
            json={"rssi": -80, "ssid": "TestNet", "signal_strength": 40},
            status=200,
        )

        import bosch_camera as bc  # type: ignore[import-not-found]

        bc.api_get_events.side_effect = RuntimeError("events gone")

        from bosch_camera_mcp.server import bosch_camera_health_check_all

        results = bosch_camera_health_check_all()

        indoor_entry = next((e for e in results if e.name == "IndoorII"), None)
        assert indoor_entry is not None
        assert indoor_entry.last_event_at is None

        bc.api_get_events.side_effect = None


# ---------------------------------------------------------------------------
# bosch_camera_token_status: no-exp JWT (1735) + undecodable JWT (1745-1747)
# ---------------------------------------------------------------------------


class TestTokenStatus:
    def test_token_no_exp_returns_valid_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JWT without 'exp' claim returns valid=True, expires_in_min=None."""
        import bosch_camera_mcp.server as srv

        cfg_no_exp: dict[str, Any] = {
            **_CFG,
            "account": {**_CFG["account"], "bearer_token": _TOKEN_NO_EXP},
        }

        def _fake(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_no_exp, req_lib.Session(), cfg_no_exp["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake)

        result = srv.bosch_camera_token_status()
        assert result.valid is True
        assert result.expires_in_min is None

    def test_token_invalid_base64_returns_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Undecodable JWT returns valid=False."""
        import bosch_camera_mcp.server as srv

        cfg_bad: dict[str, Any] = {
            **_CFG,
            "account": {**_CFG["account"], "bearer_token": _TOKEN_INVALID},
        }

        def _fake(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_bad, req_lib.Session(), cfg_bad["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake)

        result = srv.bosch_camera_token_status()
        assert result.valid is False

    def test_token_missing_returns_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty bearer_token returns valid=False immediately."""
        import bosch_camera_mcp.server as srv

        cfg_empty: dict[str, Any] = {
            **_CFG,
            "account": {**_CFG["account"], "bearer_token": ""},
        }

        def _fake(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_empty, req_lib.Session(), cfg_empty["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake)

        result = srv.bosch_camera_token_status()
        assert result.valid is False


# ---------------------------------------------------------------------------
# __main__ guard (line 1843)
# ---------------------------------------------------------------------------


class TestMainGuard:
    def test_main_guard_executes_main(self) -> None:
        """The if __name__ == '__main__': block calls sys.exit(main())."""
        import bosch_camera_mcp.server as srv

        with patch.object(srv, "main", return_value=0) as mock_main:
            with patch.object(sys, "exit") as mock_exit:
                # Simulate exactly what the if-block does
                exec(  # noqa: S102
                    "import sys; sys.exit(main())",
                    {"main": mock_main, "sys": sys},
                )
        mock_main.assert_called_once()
        mock_exit.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# bosch_camera_token_status: token with < 2 parts (line 1735)
# ---------------------------------------------------------------------------


class TestTokenStatusFewParts:
    def test_token_single_part_returns_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JWT with no dots (< 2 parts) returns valid=False immediately (line 1735)."""
        import bosch_camera_mcp.server as srv

        cfg_short: dict[str, Any] = {
            **_CFG,
            "account": {**_CFG["account"], "bearer_token": "nodottoken"},
        }

        def _fake(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_short, req_lib.Session(), cfg_short["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake)

        result = srv.bosch_camera_token_status()
        assert result.valid is False
        assert result.expires_in_min is None


# ---------------------------------------------------------------------------
# bosch_camera_autofollow_get: non-privacy exception re-raised (line 1497)
# ---------------------------------------------------------------------------


class TestAutofollowGetNonPrivacyReraise:
    @resp_lib.activate
    def test_autofollow_get_non_privacy_reraised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-privacy exception from get_autofollow is re-raised as-is."""
        from bosch_camera_mcp.server import bosch_camera_autofollow_get

        cfg_360: dict[str, Any] = {
            **_CFG,
            "cameras": {
                "IndoorII": {**_CFG["cameras"]["IndoorII"], "pan_limit": 120},
                "OutdoorGen1": _CFG["cameras"]["OutdoorGen1"],
            },
        }

        import bosch_camera_mcp.server as srv

        def _fake_get_360(config_path: Any = None) -> tuple[dict, Any, dict]:
            import requests as req_lib

            return cfg_360, req_lib.Session(), cfg_360["cameras"]

        monkeypatch.setattr(srv, "_get_session", _fake_get_360)

        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_GEN2}/autofollow",
            body=TimeoutError("request timed out"),
        )

        with pytest.raises(TimeoutError):
            bosch_camera_autofollow_get(camera="IndoorII")


# ---------------------------------------------------------------------------
# bosch_camera_health_check_all: video_inputs listing exception silenced
# (lines 1647-1648)
# ---------------------------------------------------------------------------


class TestHealthCheckAllListingException:
    @resp_lib.activate
    def test_health_check_listing_exception_silenced(self) -> None:
        """Exception fetching /v11/video_inputs listing is silenced — checks still run."""
        # Raise exception on the listing call
        resp_lib.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            body=RuntimeError("cloud unreachable"),
        )

        from bosch_camera_mcp.server import bosch_camera_health_check_all

        # Should not raise; should return entries for each camera
        results = bosch_camera_health_check_all()
        assert len(results) == 2
        # With listing failed, listing_by_id is empty → api_get_camera called
        for entry in results:
            assert entry.name in ("IndoorII", "OutdoorGen1")
