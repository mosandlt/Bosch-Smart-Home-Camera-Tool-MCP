"""Local data interface: status lookup, stream URL selection, password hygiene."""

from __future__ import annotations

import logging
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests as req_lib
import responses as resp_lib

import bosch_camera_mcp.local_data as ld
from bosch_camera_mcp.errors import MCPError

CLOUD_API = "https://residential.cbs.boschsecurity.com"
GEN2_ID = "11111111-1111-1111-1111-111111111111"
GEN1_ID = "22222222-2222-2222-2222-222222222222"
OLD_ID = "33333333-3333-3333-3333-333333333333"
PW = "test-pw"


def _cam(cam_id: str, model: str, fw: Any, ip: str = "10.0.0.50", **extra: Any) -> dict[str, Any]:
    return {
        "id": cam_id,
        "name": "x",
        "model": model,
        "firmware": fw,
        "local_ip": ip,
        "local_username": "admin",
        "local_password": "temp-secret",
        **extra,
    }


def _cameras() -> dict[str, dict[str, Any]]:
    return {
        "Indoor": _cam(GEN2_ID, "HOME_Eyes_Indoor", "9.40.105", local_data_password=PW),
        "NoPw": _cam(GEN2_ID, "HOME_Eyes_Indoor", "9.40.105"),
        "Old": _cam(OLD_ID, "HOME_Eyes_Indoor", "9.40.104", local_data_password=PW),
        "Outdoor": _cam(GEN1_ID, "CAMERA_EYES", "9.40.105", local_data_password=PW),
    }


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> Any:
    ld._last_state.clear()
    for k in list(__import__("os").environ):
        if k.startswith("BOSCH_CAMERA_LDI_PASSWORD_"):
            monkeypatch.delenv(k)
    monkeypatch.setitem(sys.modules, "bosch_camera", MagicMock())
    import bosch_camera_mcp.adapters.cli_bridge as bridge
    import bosch_camera_mcp.server as srv

    cams = _cameras()
    session = req_lib.Session()
    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)
    monkeypatch.setattr(srv, "_get_session", lambda config_path=None: ({}, session, cams))
    yield cams


def _status_url(cam_id: str = GEN2_ID) -> str:
    return f"{CLOUD_API}/v11/video_inputs/{cam_id}/onvif_user"


class TestFirmwareGate:
    @pytest.mark.parametrize(
        ("fw", "ok"),
        [
            ("9.40.105", True),
            ("9.40.202", True),
            ("10.0.0", True),
            ("9.40.104", False),
            ("9.40", False),
            (None, False),
            ("", False),
            ("garbage", False),
            ("9.40.x", False),
            ("9." + "9" * 5000 + ".1", False),
            (12345, False),
        ],
    )
    def test_gen2_firmware(self, fw: object, ok: bool) -> None:
        assert ld.is_queryable("HOME_Eyes_Indoor", fw) is ok

    def test_gen1_never_queried(self) -> None:
        assert ld.is_queryable("CAMERA_EYES", "99.0.0") is False
        assert ld.is_queryable(None, "99.0.0") is False


class TestStatus:
    @resp_lib.activate
    def test_active(self) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": "localuser"}, status=200)
        from bosch_camera_mcp.server import bosch_camera_local_data_status

        r = bosch_camera_local_data_status(camera="Indoor")
        assert (r.state, r.queried, r.password_configured) == ("active", True, True)
        assert PW not in r.model_dump_json()

    @resp_lib.activate
    def test_inactive_404(self) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={"error": "x"}, status=404)
        from bosch_camera_mcp.server import bosch_camera_local_data_status

        assert bosch_camera_local_data_status(camera="Indoor").state == "inactive"

    @resp_lib.activate
    def test_unsupported_449(self) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={}, status=449)
        from bosch_camera_mcp.server import bosch_camera_local_data_status

        assert bosch_camera_local_data_status(camera="Indoor").state == "unsupported"

    @resp_lib.activate
    @pytest.mark.parametrize("status", [200, 500, 401])
    def test_garbage_unknown_without_history(self, status: int) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), body="not json", status=status)
        from bosch_camera_mcp.server import bosch_camera_local_data_status

        assert bosch_camera_local_data_status(camera="Indoor").state == "unknown"

    @resp_lib.activate
    def test_bad_username_type_keeps_last(self) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": 5}, status=200)
        assert (
            ld.fetch_state(req_lib.Session(), CLOUD_API, GEN2_ID, "HOME_X", "9.40.105") == "unknown"
        )

    @resp_lib.activate
    def test_failures_keep_last_value(self) -> None:
        s = req_lib.Session()
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": "u"}, status=200)
        resp_lib.add(resp_lib.GET, _status_url(), body="oops", status=200)
        resp_lib.add(resp_lib.GET, _status_url(), body=req_lib.ConnectionError("down"))
        resp_lib.add(resp_lib.GET, _status_url(), json={}, status=503)
        args = (s, CLOUD_API, GEN2_ID, "HOME_X", "9.40.105")
        assert ld.fetch_state(*args) == "active"
        assert ld.fetch_state(*args) == "active"
        assert ld.fetch_state(*args) == "active"
        assert ld.fetch_state(*args) == "active"

    @resp_lib.activate
    def test_old_firmware_and_gen1_not_queried(self) -> None:
        from bosch_camera_mcp.server import bosch_camera_local_data_status

        for cam in ("Old", "Outdoor"):
            r = bosch_camera_local_data_status(camera=cam)
            assert (r.state, r.queried) == ("unsupported", False)
        assert len(resp_lib.calls) == 0

    @resp_lib.activate
    def test_fw_none_not_queried(self, setup: dict[str, Any]) -> None:
        setup["Indoor"]["firmware"] = None
        from bosch_camera_mcp.server import bosch_camera_local_data_status

        assert bosch_camera_local_data_status(camera="Indoor").queried is False
        assert len(resp_lib.calls) == 0


class TestStreamUrl:
    @resp_lib.activate
    def test_active_with_password_uses_local_source_masked(self) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": "localuser"}, status=200)
        from bosch_camera_mcp.server import bosch_camera_stream_url

        r = bosch_camera_stream_url(camera="Indoor")
        assert r.source == "local_data_interface"
        assert (
            r.rtsps_url
            == "rtsps://localuser:***@10.0.0.50:9554/rtsp_tunnel?line=1&inst=1&enableaudio=1"
        )
        assert PW not in r.model_dump_json()
        assert "temp-secret" not in r.model_dump_json()

    @resp_lib.activate
    @pytest.mark.parametrize(("quality", "inst"), [("high", 1), ("low", 2)])
    @pytest.mark.parametrize(("audio", "flag"), [(True, 1), (False, 0)])
    def test_tool_quality_audio(self, quality: str, inst: int, audio: bool, flag: int) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": "localuser"}, status=200)
        from bosch_camera_mcp.server import bosch_camera_stream_url

        r = bosch_camera_stream_url(camera="Indoor", quality=quality, audio=audio)
        assert r.rtsps_url.endswith(f"/rtsp_tunnel?line=1&inst={inst}&enableaudio={flag}")
        assert PW not in r.model_dump_json()
        assert ("video only" in r.note) is (not audio)

    @resp_lib.activate
    def test_tool_garbage_quality_rejected(self) -> None:
        from bosch_camera_mcp.server import bosch_camera_stream_url

        with pytest.raises(MCPError) as ei:
            bosch_camera_stream_url(camera="Indoor", quality="ultra")
        assert ei.value.code == "invalid_argument"

    @resp_lib.activate
    def test_active_with_password_bad_ip_fails_closed(self, setup: dict[str, Any]) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": "u"}, status=200)
        setup["Indoor"]["local_ip"] = "127.0.0.1"
        from bosch_camera_mcp.server import bosch_camera_stream_url

        with pytest.raises(MCPError) as ei:
            bosch_camera_stream_url(camera="Indoor")
        assert ei.value.code == "local_unavailable"
        assert PW not in str(ei.value.detail)

    @resp_lib.activate
    def test_active_without_password_stays_on_legacy_path_with_hint(self) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": "u"}, status=200)
        from bosch_camera_mcp.server import bosch_camera_stream_url

        r = bosch_camera_stream_url(camera="NoPw")
        assert r.rtsps_url.startswith("rtsps://admin:temp-secret@10.0.0.50:443/rtsp_tunnel")
        assert r.source == "temporary_credentials"
        assert "local_data_password" in r.note

    @resp_lib.activate
    @pytest.mark.parametrize("status", [404, 449, 500])
    def test_inactive_is_byte_identical_to_legacy(self, status: int) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), json={}, status=status)
        from bosch_camera_mcp.server import StreamUrlResult, bosch_camera_stream_url

        r = bosch_camera_stream_url(camera="Indoor")
        assert r == StreamUrlResult(
            camera="Indoor",
            rtsps_url=(
                "rtsps://admin:temp-secret@10.0.0.50:443"
                "/rtsp_tunnel?inst=2&enableaudio=1&fmtp=1&maxSessionDuration=3600"
            ),
        )

    @resp_lib.activate
    def test_network_error_keeps_legacy(self) -> None:
        resp_lib.add(resp_lib.GET, _status_url(), body=req_lib.ConnectionError("x"))
        from bosch_camera_mcp.server import bosch_camera_stream_url

        assert bosch_camera_stream_url(camera="Indoor").source == "temporary_credentials"

    @resp_lib.activate
    def test_gen1_and_old_fw_make_no_status_call(self) -> None:
        from bosch_camera_mcp.server import bosch_camera_stream_url

        bosch_camera_stream_url(camera="Outdoor")
        bosch_camera_stream_url(camera="Old")
        assert len(resp_lib.calls) == 0


class TestPassword:
    def test_config_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ld.get_password("Indoor", {"local_data_password": PW}) == PW
        monkeypatch.setenv("BOSCH_CAMERA_LDI_PASSWORD_MY_CAM", "env-pw")
        assert ld.get_password("my cam", {"local_data_password": PW}) == "env-pw"

    @pytest.mark.parametrize(
        "bad", ["", "   ", "has space", "tab\tx", "new\nline", "ü", "a" * 129, 5, None]
    )
    def test_invalid_format_is_unset(self, bad: object) -> None:
        assert ld.get_password("Indoor", {"local_data_password": bad}) is None

    def test_url_quoting(self) -> None:
        assert ld.build_url("10.0.0.5", "p@ss/w:rd") == (
            "rtsps://localuser:p%40ss%2Fw%3Ard@10.0.0.5:9554/rtsp_tunnel?line=1&inst=1&enableaudio=1"
        )
        assert ld.build_url("10.0.0.5", None) == (
            "rtsps://localuser:***@10.0.0.5:9554/rtsp_tunnel?line=1&inst=1&enableaudio=1"
        )

    @pytest.mark.parametrize(("quality", "inst"), [("high", 1), ("low", 2)])
    @pytest.mark.parametrize(("audio", "flag"), [(True, 1), (False, 0)])
    def test_quality_audio_modes(self, quality: str, inst: int, audio: bool, flag: int) -> None:
        assert ld.build_url("10.0.0.5", None, quality, audio).endswith(
            f":9554/rtsp_tunnel?line=1&inst={inst}&enableaudio={flag}"
        )

    def test_garbage_quality_rejected(self) -> None:
        with pytest.raises(ValueError):
            ld.build_url("10.0.0.5", None, "ultra")

    @pytest.mark.parametrize(
        ("ip", "ok"),
        [
            ("10.0.0.5", True),
            ("192.168.1.2", True),
            ("127.0.0.1", False),
            ("169.254.1.1", False),
            ("0.0.0.0", False),  # noqa: S104
            ("8.8.8.8", False),
            ("::1", False),
            ("fd00::1", False),
            ("host.local", False),
            ("", False),
            (None, False),
        ],
    )
    def test_safe_lan_ip(self, ip: object, ok: bool) -> None:
        assert (ld.safe_lan_ip(ip) is not None) is ok

    @resp_lib.activate
    def test_password_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        resp_lib.add(resp_lib.GET, _status_url(), body=req_lib.ConnectionError("x"))
        resp_lib.add(resp_lib.GET, _status_url(), json={"username": "u"}, status=200)
        from bosch_camera_mcp.server import bosch_camera_stream_url

        bosch_camera_stream_url(camera="Indoor")
        bosch_camera_stream_url(camera="Indoor")
        assert PW not in caplog.text


class TestBridgePreservesPassword:
    def test_refresh_keeps_local_data_password(self) -> None:
        src = open("src/bosch_camera_mcp/adapters/cli_bridge.py", encoding="utf-8").read()
        assert '"local_data_password": prev.get("local_data_password", "")' in src
