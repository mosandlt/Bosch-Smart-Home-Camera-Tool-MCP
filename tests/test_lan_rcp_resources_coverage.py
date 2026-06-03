"""Coverage-gap tests for lan_rcp.py, resources.py, and maintenance.py.

Targets previously uncovered lines:
  lan_rcp.py  : 155/177-180 (<err> response), 305-307 (bad JSON in refresh),
                334-335 (disk-persist exception), 354-391 (lan_tcp_ping)
  resources.py: 33-34/50-53/89-92/129-132 (MCPError re-raise on auth_expired)
  maintenance.py: 160-162 (ValueError in _parse_window), 229 (empty-title skip)
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared constants & helpers (keep identical to other test files)
# ---------------------------------------------------------------------------

CAM_ID_1 = "aaaa-1111-aaaa-1111"

_VALID_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.placeholder"

_CFG: dict[str, Any] = {
    "account": {
        "username": "test@example.com",
        "bearer_token": _VALID_TOKEN,
        "refresh_token": "",
    },
    "cameras": {
        "Terrasse": {
            "id": CAM_ID_1,
            "name": "Terrasse",
            "model": "HOME_Eyes_Outdoor",
            "firmware": "9.40.25",
            "mac": "aa:bb:cc:dd:ee:01",
            "download_folder": "Terrasse",
            "local_ip": "192.0.2.149",
            "local_username": "admin",
            "local_password": "secret",
            "has_light": True,
            "pan_limit": 0,
        },
    },
    "settings": {},
    "nvr": {},
}


def _make_fake_bc() -> MagicMock:
    import requests as req_lib

    m = MagicMock()
    m.CLOUD_API = "https://residential.cbs.boschsecurity.com"
    m.DEFAULT_CONFIG = {"account": {}, "cameras": {}, "settings": {}, "nvr": {}}
    m.load_config.return_value = _CFG
    m._merge_defaults = lambda c, d: None
    m._is_token_near_expiry.return_value = False
    m.make_session.return_value = req_lib.Session()
    m.save_config.return_value = None
    m.api_ping.return_value = "ONLINE"
    m.api_get_events.return_value = []
    m.snap_from_local.return_value = b"\xff\xd8\xff" + b"\x00" * 80
    return m


@pytest.fixture(autouse=True)
def _patch_bc_and_bridge(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake_bc = _make_fake_bc()
    monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
    monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())

    import bosch_camera_mcp.adapters.cli_bridge as bridge
    import bosch_camera_mcp.server as srv
    import requests as req_lib

    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)
    fake_session = req_lib.Session()

    def _fake_get_session(config_path: Any = None) -> Any:
        return _CFG, fake_session, _CFG["cameras"]

    monkeypatch.setattr(bridge, "get_session_and_cameras", _fake_get_session)
    monkeypatch.setattr(srv, "_get_session", _fake_get_session)

    import bosch_camera_mcp.resources as res_mod

    monkeypatch.setattr(res_mod, "_get_session", _fake_get_session)

    return fake_bc


# ---------------------------------------------------------------------------
# lan_rcp.py — lines 155 / 177-180: <err> tag in 200 response returns False
# ---------------------------------------------------------------------------


def _make_async_client(
    responses: list[MagicMock],
) -> tuple[MagicMock, MagicMock]:
    """Helper: mock httpx.AsyncClient yielding responses in order."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=responses)
    mock_client_cls = MagicMock(return_value=mock_client)
    return mock_client_cls, mock_client


class TestRcpLocalWriteErrTag:
    """Lines 155/177-180: HTTP 200 with <err> in body returns False."""

    @pytest.mark.asyncio
    async def test_200_with_err_tag_returns_false(self) -> None:
        """HTTP 200 but body contains <err> tag → returns False."""
        err_resp = MagicMock()
        err_resp.status_code = 200
        err_resp.content = b"<rcp><err>RCP_ERR_INVALID_COMMAND</err></rcp>"
        mock_cls, _ = _make_async_client([err_resp])

        with (
            patch("httpx.AsyncClient", mock_cls),
            patch("bosch_camera_mcp.lan_rcp.pin_or_verify_cam"),
        ):
            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write(
                "192.0.2.149",
                "0x0d00",
                "00010000",
                user="admin",
                password="secret",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_200_with_err_tag_uppercase_returns_false(self) -> None:
        """<ERR> tag (uppercase) must also be caught via .lower()."""
        err_resp = MagicMock()
        err_resp.status_code = 200
        err_resp.content = b"<RCP><ERR>FORBIDDEN</ERR></RCP>"
        mock_cls, _ = _make_async_client([err_resp])

        with (
            patch("httpx.AsyncClient", mock_cls),
            patch("bosch_camera_mcp.lan_rcp.pin_or_verify_cam"),
        ):
            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write("192.0.2.149", "0x0d00", "00010000")

        assert result is False

    @pytest.mark.asyncio
    async def test_200_without_err_tag_returns_true(self) -> None:
        """HTTP 200 with no <err> tag → returns True (sanity check for above tests)."""
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.content = b"<rcp><payload>ok</payload></rcp>"
        mock_cls, _ = _make_async_client([ok_resp])

        with (
            patch("httpx.AsyncClient", mock_cls),
            patch("bosch_camera_mcp.lan_rcp.pin_or_verify_cam"),
        ):
            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write("192.0.2.149", "0x0d00", "00010000")

        assert result is True


# ---------------------------------------------------------------------------
# lan_rcp.py — lines 305-307: refresh_local_creds bad JSON branch
# ---------------------------------------------------------------------------


class TestRefreshLocalCredsBadJson:
    """Line 305-307: resp.json() raises → returns None."""

    @pytest.mark.asyncio
    async def test_bad_json_in_response_returns_none(self) -> None:
        """If resp.json() raises ValueError, refresh_local_creds returns None."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("invalid JSON")

        fake_session = req_lib.Session()
        with patch.object(fake_session, "put", return_value=mock_resp):
            from bosch_camera_mcp.lan_rcp import refresh_local_creds

            result = await refresh_local_creds(
                cam_id=CAM_ID_1,
                session=fake_session,
                cfg={"cameras": {}},
                cam_name="Terrasse",
                config_path=None,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_json_decode_error_returns_none(self) -> None:
        """If resp.json() raises json.JSONDecodeError, refresh_local_creds returns None."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("msg", "doc", 0)

        fake_session = req_lib.Session()
        with patch.object(fake_session, "put", return_value=mock_resp):
            from bosch_camera_mcp.lan_rcp import refresh_local_creds

            result = await refresh_local_creds(
                cam_id=CAM_ID_1,
                session=fake_session,
                cfg={"cameras": {}},
                cam_name="Terrasse",
                config_path=None,
            )

        assert result is None


# ---------------------------------------------------------------------------
# lan_rcp.py — lines 334-335: disk-persist exception in refresh_local_creds
# ---------------------------------------------------------------------------


class TestRefreshLocalCredsDiskPersistError:
    """Lines 334-335: file I/O error during disk persist is swallowed (logged only)."""

    @pytest.mark.asyncio
    async def test_ioerror_writing_config_still_returns_creds(self) -> None:
        """Even if writing to config_path raises, the function returns the new creds."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"user": "new-u", "password": "new-p"}

        cfg: dict[str, Any] = {
            "cameras": {
                "Terrasse": {
                    "id": CAM_ID_1,
                    "local_username": "old-u",
                    "local_password": "old-p",
                }
            }
        }

        fake_session = req_lib.Session()
        with patch.object(fake_session, "put", return_value=mock_resp):
            # Provide a path that will fail to open (a directory acts as a bad file)
            with tempfile.TemporaryDirectory() as tmp_dir:
                bad_path = tmp_dir  # opening a directory as a file raises IsADirectoryError

                from bosch_camera_mcp.lan_rcp import refresh_local_creds

                result = await refresh_local_creds(
                    cam_id=CAM_ID_1,
                    session=fake_session,
                    cfg=cfg,
                    cam_name="Terrasse",
                    config_path=bad_path,
                )

        # Despite I/O error, creds are returned
        assert result == ("new-u", "new-p")
        # In-memory cfg was still updated before the disk-write attempt
        assert cfg["cameras"]["Terrasse"]["local_username"] == "new-u"
        assert cfg["cameras"]["Terrasse"]["local_password"] == "new-p"

    @pytest.mark.asyncio
    async def test_nonexistent_json_in_config_path_still_returns_creds(self) -> None:
        """Config path that does not contain valid JSON still returns creds (exception swallowed)."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"user": "rotated", "password": "rotated-pw"}

        cfg: dict[str, Any] = {
            "cameras": {
                "Terrasse": {
                    "id": CAM_ID_1,
                    "local_username": "old-u",
                    "local_password": "old-p",
                }
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            bad_json_path = f.name

        try:
            fake_session = req_lib.Session()
            with patch.object(fake_session, "put", return_value=mock_resp):
                from bosch_camera_mcp.lan_rcp import refresh_local_creds

                result = await refresh_local_creds(
                    cam_id=CAM_ID_1,
                    session=fake_session,
                    cfg=cfg,
                    cam_name="Terrasse",
                    config_path=bad_json_path,
                )
        finally:
            Path(bad_json_path).unlink(missing_ok=True)

        assert result == ("rotated", "rotated-pw")


# ---------------------------------------------------------------------------
# lan_rcp.py — lines 354-391: lan_tcp_ping function
# ---------------------------------------------------------------------------


class TestLanTcpPing:
    """Lines 354-391: lan_tcp_ping httpx HEAD + asyncio TCP fallback."""

    @pytest.mark.asyncio
    async def test_head_200_returns_reachable_and_positive_latency(self) -> None:
        """Successful HTTPS HEAD → (True, latency_ms > 0)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            from bosch_camera_mcp.lan_rcp import lan_tcp_ping

            reachable, latency = await lan_tcp_ping("192.0.2.149")

        assert reachable is True
        assert latency >= 0.0

    @pytest.mark.asyncio
    async def test_connect_error_returns_reachable_true(self) -> None:
        """httpx.ConnectError (TLS/TCP reached host) → (True, latency_ms)."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=httpx.ConnectError("TLS handshake incomplete"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            from bosch_camera_mcp.lan_rcp import lan_tcp_ping

            reachable, latency = await lan_tcp_ping("192.0.2.149")

        assert reachable is True
        assert latency >= 0.0

    @pytest.mark.asyncio
    async def test_timeout_exception_returns_false_minus_one(self) -> None:
        """httpx.TimeoutException → (False, -1.0)."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            from bosch_camera_mcp.lan_rcp import lan_tcp_ping

            reachable, latency = await lan_tcp_ping("192.0.2.149")

        assert reachable is False
        assert latency == pytest.approx(-1.0)

    @pytest.mark.asyncio
    async def test_generic_exception_falls_through_to_asyncio_fallback_success(
        self,
    ) -> None:
        """Other httpx error → asyncio fallback; fallback success → (True, latency)."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=RuntimeError("unexpected error"))

        # Fake asyncio.open_connection returning a writer that closes cleanly
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock(return_value=None)

        async def _fake_open_connection(ip: str, port: int) -> tuple[MagicMock, MagicMock]:
            return MagicMock(), mock_writer

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.open_connection", side_effect=_fake_open_connection),
        ):
            from bosch_camera_mcp.lan_rcp import lan_tcp_ping

            reachable, latency = await lan_tcp_ping("192.0.2.149")

        assert reachable is True
        assert latency >= 0.0

    @pytest.mark.asyncio
    async def test_generic_exception_falls_through_to_asyncio_fallback_oserror(
        self,
    ) -> None:
        """asyncio fallback OSError → (False, -1.0)."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=RuntimeError("dns failure"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch(
                "asyncio.open_connection",
                side_effect=OSError("connection refused"),
            ),
        ):
            from bosch_camera_mcp.lan_rcp import lan_tcp_ping

            reachable, latency = await lan_tcp_ping("192.0.2.149")

        assert reachable is False
        assert latency == pytest.approx(-1.0)

    @pytest.mark.asyncio
    async def test_generic_exception_falls_through_to_asyncio_fallback_timeout(
        self,
    ) -> None:
        """asyncio fallback TimeoutError → (False, -1.0)."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=RuntimeError("dns failure"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch(
                "asyncio.open_connection",
                side_effect=asyncio.TimeoutError(),
            ),
        ):
            from bosch_camera_mcp.lan_rcp import lan_tcp_ping

            reachable, latency = await lan_tcp_ping("192.0.2.149")

        assert reachable is False
        assert latency == pytest.approx(-1.0)

    @pytest.mark.asyncio
    async def test_asyncio_fallback_writer_wait_closed_raises(self) -> None:
        """writer.wait_closed() exception is swallowed; result is still (True, latency)."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=RuntimeError("unexpected"))

        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock(side_effect=OSError("already closed"))

        async def _fake_open_connection(ip: str, port: int) -> tuple[MagicMock, MagicMock]:
            return MagicMock(), mock_writer

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.open_connection", side_effect=_fake_open_connection),
        ):
            from bosch_camera_mcp.lan_rcp import lan_tcp_ping

            reachable, latency = await lan_tcp_ping("192.0.2.149")

        assert reachable is True
        assert latency >= 0.0


# ---------------------------------------------------------------------------
# resources.py — lines 33-34/50-53/89-92/129-132: MCPError re-raise as auth_expired
# ---------------------------------------------------------------------------


class TestResourcesAuthExpiredReRaise:
    """Each resource re-raises MCPError(auth_expired) when _get_session raises it."""

    def test_cameras_list_reauth_required_reraises_as_auth_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cameras_list: reauth_required → re-raised as auth_expired."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_reauth(config_path: Any = None) -> Any:
            raise MCPError(code="reauth_required", detail="token expired")

        monkeypatch.setattr(res_mod, "_get_session", _raise_reauth)

        from bosch_camera_mcp.resources import cameras_list

        with pytest.raises(MCPError) as exc_info:
            cameras_list()

        assert exc_info.value.code == "auth_expired"

    def test_cameras_list_auth_expired_reraises_as_auth_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cameras_list: auth_expired → re-raised with same code."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_auth_expired(config_path: Any = None) -> Any:
            raise MCPError(code="auth_expired", detail="already expired")

        monkeypatch.setattr(res_mod, "_get_session", _raise_auth_expired)

        from bosch_camera_mcp.resources import cameras_list

        with pytest.raises(MCPError) as exc_info:
            cameras_list()

        assert exc_info.value.code == "auth_expired"

    def test_cameras_list_other_mcp_error_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cameras_list: other MCPError codes propagate without modification."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_api_unreachable(config_path: Any = None) -> Any:
            raise MCPError(code="api_unreachable", detail="network down")

        monkeypatch.setattr(res_mod, "_get_session", _raise_api_unreachable)

        from bosch_camera_mcp.resources import cameras_list

        with pytest.raises(MCPError) as exc_info:
            cameras_list()

        assert exc_info.value.code == "api_unreachable"

    def test_camera_snapshot_reauth_required_reraises_as_auth_expired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """camera_snapshot: reauth_required → re-raised as auth_expired."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_reauth(config_path: Any = None) -> Any:
            raise MCPError(code="reauth_required", detail="token expired")

        monkeypatch.setattr(res_mod, "_get_session", _raise_reauth)

        from bosch_camera_mcp.resources import camera_snapshot

        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(MCPError) as exc_info:
                camera_snapshot("Terrasse")

        assert exc_info.value.code == "auth_expired"

    def test_camera_snapshot_auth_expired_reraises_as_auth_expired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """camera_snapshot: auth_expired → re-raised with same code."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_auth_expired(config_path: Any = None) -> Any:
            raise MCPError(code="auth_expired", detail="expired")

        monkeypatch.setattr(res_mod, "_get_session", _raise_auth_expired)

        from bosch_camera_mcp.resources import camera_snapshot

        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(MCPError) as exc_info:
                camera_snapshot("Terrasse")

        assert exc_info.value.code == "auth_expired"

    def test_camera_events_reauth_required_reraises_as_auth_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """camera_events: reauth_required → re-raised as auth_expired."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_reauth(config_path: Any = None) -> Any:
            raise MCPError(code="reauth_required", detail="token expired")

        monkeypatch.setattr(res_mod, "_get_session", _raise_reauth)

        from bosch_camera_mcp.resources import camera_events

        with pytest.raises(MCPError) as exc_info:
            camera_events("Terrasse")

        assert exc_info.value.code == "auth_expired"

    def test_camera_events_auth_expired_reraises_as_auth_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """camera_events: auth_expired → re-raised with same code."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_auth_expired(config_path: Any = None) -> Any:
            raise MCPError(code="auth_expired", detail="expired")

        monkeypatch.setattr(res_mod, "_get_session", _raise_auth_expired)

        from bosch_camera_mcp.resources import camera_events

        with pytest.raises(MCPError) as exc_info:
            camera_events("Terrasse")

        assert exc_info.value.code == "auth_expired"

    def test_camera_snapshot_other_mcp_error_propagates_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """camera_snapshot: other MCPError codes propagate without modification (line 92)."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_api_unreachable(config_path: Any = None) -> Any:
            raise MCPError(code="api_unreachable", detail="network down")

        monkeypatch.setattr(res_mod, "_get_session", _raise_api_unreachable)

        from bosch_camera_mcp.resources import camera_snapshot

        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(MCPError) as exc_info:
                camera_snapshot("Terrasse")

        assert exc_info.value.code == "api_unreachable"

    def test_camera_events_other_mcp_error_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """camera_events: other MCPError codes propagate without modification (line 132)."""
        from bosch_camera_mcp.errors import MCPError
        import bosch_camera_mcp.resources as res_mod

        def _raise_api_unreachable(config_path: Any = None) -> Any:
            raise MCPError(code="api_unreachable", detail="network down")

        monkeypatch.setattr(res_mod, "_get_session", _raise_api_unreachable)

        from bosch_camera_mcp.resources import camera_events

        with pytest.raises(MCPError) as exc_info:
            camera_events("Terrasse")

        assert exc_info.value.code == "api_unreachable"


# ---------------------------------------------------------------------------
# maintenance.py — line 160-162: ValueError in _parse_window (invalid date)
# ---------------------------------------------------------------------------


class TestParseWindowValueError:
    """Lines 160-162: datetime constructor raises ValueError → returns (None, None)."""

    def test_invalid_month_returns_none_none(self) -> None:
        """Date with month=13 triggers ValueError in datetime() → (None, None)."""
        from datetime import datetime, timezone

        from bosch_camera_mcp.maintenance import _parse_window

        pub = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
        # Month 13 is invalid; _DATE_RE will match it; datetime() will raise ValueError
        text = "Wartung am 19.13.2026 von 07:00 bis 10:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start is None
        assert end is None

    def test_invalid_day_returns_none_none(self) -> None:
        """Date with day=32 triggers ValueError → (None, None)."""
        from datetime import datetime, timezone

        from bosch_camera_mcp.maintenance import _parse_window

        pub = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
        text = "Wartung am 32.05.2026 von 09:00 bis 11:00 Uhr"
        start, end = _parse_window(text, pub)
        assert start is None
        assert end is None


# ---------------------------------------------------------------------------
# maintenance.py — line 229: empty-title item is skipped in _parse_feed_body
# ---------------------------------------------------------------------------


class TestParseFeedBodyEmptyTitle:
    """Line 229: item with no/empty title is skipped (continue branch)."""

    def test_item_with_empty_title_is_skipped(self) -> None:
        """RSS item that has no title text is ignored; parse returns None."""
        from bosch_camera_mcp.maintenance import _parse_feed_body

        rss_no_title = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Wartungsarbeiten</title>
    <item>
      <title></title>
      <link>https://example.com/x</link>
      <pubDate>Mon, 18 May 2026 10:06:13 GMT</pubDate>
      <description>Some description without a title</description>
    </item>
  </channel>
</rss>"""
        result = _parse_feed_body(rss_no_title, "https://x?board.id=Wartungsarbeiten")
        # Empty title causes the item to be skipped → no candidate → None
        assert result is None

    def test_item_with_whitespace_only_title_is_skipped(self) -> None:
        """RSS item whose title is whitespace-only is also skipped."""
        from bosch_camera_mcp.maintenance import _parse_feed_body

        rss_ws_title = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>   </title>
      <link>https://example.com/y</link>
      <pubDate>Mon, 18 May 2026 10:06:13 GMT</pubDate>
      <description>desc</description>
    </item>
  </channel>
</rss>"""
        result = _parse_feed_body(rss_ws_title, "https://x?board.id=Statusmeldungen")
        assert result is None

    def test_mixed_items_empty_and_valid_title_returns_valid(self) -> None:
        """First item has empty title (skipped); second has valid title (returned)."""
        from bosch_camera_mcp.maintenance import _parse_feed_body

        rss_mixed = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Wartungsarbeiten</title>
    <item>
      <title></title>
      <link>https://example.com/empty</link>
      <pubDate>Mon, 18 May 2026 10:06:13 GMT</pubDate>
      <description>skipped</description>
    </item>
    <item>
      <title>Wartung Kameras am 19.05.2026 von 07:00 bis 10:00 Uhr</title>
      <link>https://example.com/real</link>
      <pubDate>Mon, 18 May 2026 10:06:13 GMT</pubDate>
      <description>Camera maintenance</description>
    </item>
  </channel>
</rss>"""
        result = _parse_feed_body(rss_mixed, "https://x?board.id=Wartungsarbeiten")
        assert result is not None
        assert result.title.startswith("Wartung Kameras")
