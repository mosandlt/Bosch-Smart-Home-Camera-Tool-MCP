"""Regression tests for LAN RCP HTTPS + Digest auth fix (v1.3.1).

Root cause: lan_rcp.py used plain HTTP on port 80 with no auth — Gen2 cameras
only listen on HTTPS port 443 and require HTTP Digest auth on rcp.xml.
Confirmed against live Gen2 hardware (FW 9.40.25) on 2026-05-20.

Test strategy:
- Unit-tests on rcp_local_write: verify URL scheme is https://, DigestAuth is
  constructed with provided creds, and no-creds path still uses https://.
- Integration-level tests: rcp_local_write_privacy / rcp_local_write_front_light
  forward user+password kwargs to rcp_local_write.
- Server-level tests: bosch_camera_privacy_set / bosch_camera_light_set with
  prefer_local=True pass the cam_info creds down to the RCP helpers.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures shared with test_lan_ping.py (duplicated here to keep files independent)
# ---------------------------------------------------------------------------

CAM_ID_1 = "aaaa-1111-aaaa-1111"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

_VALID_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJleHAiOjQxMDI0NDQ4MDB9"
    ".placeholder"
)

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
            "local_username": "cbs-testuser",
            "local_password": "testpass456",
            "has_light": True,
            "pan_limit": 0,
        },
        "NoCreds": {
            "id": "cccc-3333-cccc-3333",
            "name": "NoCreds",
            "model": "HOME_Eyes_Indoor",
            "firmware": "9.40.25",
            "mac": "aa:bb:cc:dd:ee:02",
            "download_folder": "NoCreds",
            "local_ip": "192.0.2.150",
            "local_username": "",
            "local_password": "",
            "has_light": False,
            "pan_limit": 120,
        },
    },
    "settings": {},
    "nvr": {},
}

_CAM1_DETAIL: dict[str, Any] = {
    "id": CAM_ID_1,
    "title": "Terrasse",
    "hardwareVersion": "HOME_Eyes_Outdoor",
    "firmwareVersion": "9.40.25",
    "macAddress": "aa:bb:cc:dd:ee:01",
    "privacyMode": "OFF",
    "featureSupport": {"light": True, "panLimit": 0},
    "featureStatus": {"frontIlluminatorInGeneralLightOn": False},
}


def _make_fake_bc() -> MagicMock:
    import requests as req_lib

    m = MagicMock()
    m.CLOUD_API = CLOUD_API
    m.DEFAULT_CONFIG = {"account": {}, "cameras": {}, "settings": {}, "nvr": {}}
    m.load_config.return_value = _CFG
    m._merge_defaults = lambda c, d: None
    m._is_token_near_expiry.return_value = False
    m.make_session.return_value = req_lib.Session()
    m.save_config.return_value = None
    m.api_ping.return_value = "ONLINE"
    m.api_get_camera.return_value = _CAM1_DETAIL
    m.api_get_events.return_value = []
    return m


@pytest.fixture(autouse=True)
def _patch_bc_and_bridge(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject fake bosch_camera module and stub out the session/CLI bridge."""
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
    return fake_bc


# ---------------------------------------------------------------------------
# Unit: rcp_local_write — URL scheme must be https://
# ---------------------------------------------------------------------------


class TestRcpLocalWriteHttpsScheme:
    @pytest.mark.asyncio
    async def test_url_uses_https_not_http(self) -> None:
        """rcp_local_write must use https:// — not http:// (port 80 refused on Gen2)."""
        captured_urls: list[str] = []

        async def _fake_get(url: Any, **kwargs: Any) -> MagicMock:
            # httpx client.get first arg is the URL string
            captured_urls.append(str(url))
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<rcp><payload>ok</payload></rcp>"
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = _fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from bosch_camera_mcp.lan_rcp import rcp_local_write

            await rcp_local_write("192.0.2.149", "0x0d00", "00010000")

        assert len(captured_urls) == 1
        assert captured_urls[0].startswith("https://"), (
            f"Expected https:// URL but got: {captured_urls[0]!r}"
        )
        assert "http://" not in captured_urls[0], (
            f"URL must not contain plain http://: {captured_urls[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_url_contains_correct_cam_ip(self) -> None:
        """rcp_local_write URL must embed the camera IP address."""
        captured_urls: list[str] = []

        async def _fake_get(url: Any, **kwargs: Any) -> MagicMock:
            captured_urls.append(str(url))
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<ok/>"
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = _fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from bosch_camera_mcp.lan_rcp import rcp_local_write

            await rcp_local_write("10.0.1.55", "0x0d00", "00010000")

        assert "10.0.1.55" in captured_urls[0]

    @pytest.mark.asyncio
    async def test_digest_auth_constructed_with_user_and_password(self) -> None:
        """When user+password provided, httpx.DigestAuth is created with those creds."""
        with (
            patch("httpx.DigestAuth") as mock_digest_cls,
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_digest_cls.return_value = MagicMock()
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<ok/>"
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from bosch_camera_mcp.lan_rcp import rcp_local_write

            await rcp_local_write(
                "192.0.2.149", "0x0d00", "00010000",
                user="cbs-testuser", password="testpass456",
            )

        mock_digest_cls.assert_called_once_with("cbs-testuser", "testpass456")

    @pytest.mark.asyncio
    async def test_no_creds_digest_auth_not_constructed(self) -> None:
        """When no user/password, httpx.DigestAuth is NOT constructed (auth=None)."""
        with (
            patch("httpx.DigestAuth") as mock_digest_cls,
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<ok/>"
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from bosch_camera_mcp.lan_rcp import rcp_local_write

            await rcp_local_write("192.0.2.149", "0x0d00", "00010000")

        mock_digest_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_http_401_returns_false(self) -> None:
        """HTTP 401 (auth rejected) → function returns False, no exception."""
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.content = b""
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write(
                "192.0.2.149", "0x0d00", "00010000",
                user="cbs-testuser", password="testpass456",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_connection_refused_returns_false(self) -> None:
        """ConnectError (port 80 refused, port 443 down) → function returns False."""
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx_connect_error())
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write("192.0.2.149", "0x0d00", "00010000")

        assert result is False


def httpx_connect_error() -> Exception:
    """Build an httpx.ConnectError without needing a real network call."""
    import httpx

    return httpx.ConnectError("Connection refused")


# ---------------------------------------------------------------------------
# Unit: rcp_local_write_privacy — forwards creds to rcp_local_write
# ---------------------------------------------------------------------------


class TestRcpLocalWritePrivacyCreds:
    @pytest.mark.asyncio
    async def test_privacy_on_passes_creds_to_rcp_local_write(self) -> None:
        """rcp_local_write_privacy forwards user+password to rcp_local_write."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_privacy

            result = await rcp_local_write_privacy(
                "192.0.2.149", True, user="cbs-testuser", password="testpass456"
            )

        assert result is True
        mock_write.assert_awaited_once_with(
            "192.0.2.149", "0x0d00", "00010000", "P_OCTET",
            user="cbs-testuser", password="testpass456",
        )

    @pytest.mark.asyncio
    async def test_privacy_off_sends_correct_payload(self) -> None:
        """rcp_local_write_privacy(enabled=False) sends OFF payload 00000000."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_privacy

            await rcp_local_write_privacy(
                "192.0.2.149", False, user="u", password="p"
            )

        args, kwargs = mock_write.call_args
        assert args[2] == "00000000", f"Expected OFF payload 00000000, got {args[2]!r}"

    @pytest.mark.asyncio
    async def test_privacy_on_sends_correct_payload(self) -> None:
        """rcp_local_write_privacy(enabled=True) sends ON payload 00010000."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_privacy

            await rcp_local_write_privacy(
                "192.0.2.149", True, user="u", password="p"
            )

        args, kwargs = mock_write.call_args
        assert args[2] == "00010000", f"Expected ON payload 00010000, got {args[2]!r}"

    @pytest.mark.asyncio
    async def test_privacy_no_creds_passes_none_to_rcp_local_write(self) -> None:
        """Without creds, user=None / password=None is forwarded (graceful degradation)."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=False),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_privacy

            await rcp_local_write_privacy("192.0.2.149", True)

        _, kwargs = mock_write.call_args
        assert kwargs.get("user") is None
        assert kwargs.get("password") is None


# ---------------------------------------------------------------------------
# Unit: rcp_local_write_front_light — forwards creds
# ---------------------------------------------------------------------------


class TestRcpLocalWriteFrontLightCreds:
    @pytest.mark.asyncio
    async def test_light_on_passes_brightness_100_with_creds(self) -> None:
        """rcp_local_write_front_light(100) passes correct payload + creds."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_front_light

            result = await rcp_local_write_front_light(
                "192.0.2.149", 100, user="cbs-testuser", password="testpass456"
            )

        assert result is True
        mock_write.assert_awaited_once_with(
            "192.0.2.149", "0x0c22", "0064", "T_WORD", num=1,
            user="cbs-testuser", password="testpass456",
        )

    @pytest.mark.asyncio
    async def test_light_off_passes_brightness_0_with_creds(self) -> None:
        """rcp_local_write_front_light(0) → payload '0000', creds forwarded."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_front_light

            await rcp_local_write_front_light(
                "192.0.2.149", 0, user="u", password="p"
            )

        args, kwargs = mock_write.call_args
        assert args[2] == "0000"

    @pytest.mark.asyncio
    async def test_light_clamps_over_100(self) -> None:
        """Brightness > 100 is clamped to 100 before encoding."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_front_light

            await rcp_local_write_front_light("192.0.2.149", 150)

        args, _ = mock_write.call_args
        assert args[2] == "0064"  # 100 decimal = 0x0064

    @pytest.mark.asyncio
    async def test_light_clamps_below_0(self) -> None:
        """Brightness < 0 is clamped to 0."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_front_light

            await rcp_local_write_front_light("192.0.2.149", -5)

        args, _ = mock_write.call_args
        assert args[2] == "0000"


# ---------------------------------------------------------------------------
# Server integration: creds from cam_info forwarded via prefer_local path
# ---------------------------------------------------------------------------


class TestServerPassesCredsThroughPreferLocal:
    @pytest.mark.asyncio
    async def test_privacy_set_prefer_local_passes_cam_creds(self) -> None:
        """bosch_camera_privacy_set prefer_local=True passes local_username+password to RCP."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch("bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode") as _mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            await bosch_camera_privacy_set(camera="Terrasse", enabled=True, prefer_local=True)

        mock_rcp.assert_awaited_once_with(
            "192.0.2.149", True,
            user="cbs-testuser", password="testpass456",
        )

    @pytest.mark.asyncio
    async def test_privacy_set_prefer_local_privacy_off_passes_creds(self) -> None:
        """privacy_set(enabled=False) with prefer_local also forwards creds correctly."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch("bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode") as _mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            await bosch_camera_privacy_set(camera="Terrasse", enabled=False, prefer_local=True)

        mock_rcp.assert_awaited_once_with(
            "192.0.2.149", False,
            user="cbs-testuser", password="testpass456",
        )

    @pytest.mark.asyncio
    async def test_light_set_prefer_local_passes_cam_creds(self) -> None:
        """bosch_camera_light_set prefer_local=True passes local_username+password to RCP."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_front_light",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch("bosch_camera_mcp.adapters.cli_bridge.set_light") as _mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_light_set

            await bosch_camera_light_set(camera="Terrasse", enabled=True, prefer_local=True)

        mock_rcp.assert_awaited_once_with(
            "192.0.2.149", 100,
            user="cbs-testuser", password="testpass456",
        )

    @pytest.mark.asyncio
    async def test_light_set_prefer_local_off_passes_creds(self) -> None:
        """light_set(enabled=False) with prefer_local passes creds + brightness=0."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_front_light",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch("bosch_camera_mcp.adapters.cli_bridge.set_light") as _mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_light_set

            await bosch_camera_light_set(camera="Terrasse", enabled=False, prefer_local=True)

        mock_rcp.assert_awaited_once_with(
            "192.0.2.149", 0,
            user="cbs-testuser", password="testpass456",
        )

    @pytest.mark.asyncio
    async def test_prefer_local_empty_creds_passes_none_not_empty_string(self) -> None:
        """Camera with empty local_username / local_password → None passed, not empty str."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=False),
            ) as mock_rcp,
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode",
                return_value=True,
            ) as _mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            await bosch_camera_privacy_set(camera="NoCreds", enabled=True, prefer_local=True)

        # NoCreds has local_ip but empty creds → user=None, password=None
        mock_rcp.assert_awaited_once_with(
            "192.0.2.150", True,
            user=None, password=None,
        )
