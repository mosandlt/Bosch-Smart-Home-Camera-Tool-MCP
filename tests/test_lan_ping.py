"""Tests for LAN-fallback feature set (v1.3.0).

Covers:
  - bosch_camera_lan_ping — reachable + timeout cases
  - bosch_camera_privacy_set / bosch_camera_light_set prefer_local routing
  - bosch_camera_maintenance_status recommended_action field
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared test fixtures
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
        "Garten": {
            "id": CAM_ID_1,
            "name": "Garten",
            "model": "HOME_Eyes_Outdoor",
            "firmware": "9.40.25",
            "mac": "aa:bb:cc:dd:ee:01",
            "download_folder": "Garten",
            "local_ip": "192.0.2.10",
            "local_username": "admin",
            "local_password": "secret123",
            "has_light": True,
            "pan_limit": 0,
        },
        "Innen": {
            "id": "bbbb-2222-bbbb-2222",
            "name": "Innen",
            "model": "HOME_Eyes_Indoor",
            "firmware": "9.40.25",
            "mac": "aa:bb:cc:dd:ee:02",
            "download_folder": "Innen",
            "local_ip": "",
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
    "title": "Garten",
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
    m.snap_from_local.return_value = b"\xff\xd8\xff" + b"\x00" * 80
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
# bosch_camera_lan_ping — reachable cases
# ---------------------------------------------------------------------------


class TestLanPingReachable:
    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_reachable_by_camera_name(self) -> None:
        """Resolving camera by name, mocking lan_tcp_ping to return reachable."""
        with patch(
            "bosch_camera_mcp.lan_rcp.lan_tcp_ping",
            new=AsyncMock(return_value=(True, 4.2)),
        ):
            from bosch_camera_mcp.server import bosch_camera_lan_ping

            result = await bosch_camera_lan_ping(camera="Garten")

        assert result.reachable is True
        assert result.ip == "192.0.2.10"
        assert result.latency_ms == pytest.approx(4.2, abs=0.1)

    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_reachable_by_explicit_ip(self) -> None:
        """Explicit lan_ip bypasses camera-lookup; calls lan_tcp_ping with that IP."""
        with patch(
            "bosch_camera_mcp.lan_rcp.lan_tcp_ping",
            new=AsyncMock(return_value=(True, 2.9)),
        ) as mock_ping:
            from bosch_camera_mcp.server import bosch_camera_lan_ping

            result = await bosch_camera_lan_ping(lan_ip="192.0.2.99")

        assert result.reachable is True
        assert result.ip == "192.0.2.99"
        mock_ping.assert_awaited_once_with("192.0.2.99")

    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_reachable_returns_lan_ping_result_type(self) -> None:
        """Return type is LanPingResult."""
        with patch(
            "bosch_camera_mcp.lan_rcp.lan_tcp_ping",
            new=AsyncMock(return_value=(True, 3.0)),
        ):
            from bosch_camera_mcp.server import LanPingResult, bosch_camera_lan_ping

            result = await bosch_camera_lan_ping(lan_ip="192.0.2.10")

        assert isinstance(result, LanPingResult)

    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_reachable_low_latency(self) -> None:
        """Latency value is preserved exactly from lan_tcp_ping."""
        with patch(
            "bosch_camera_mcp.lan_rcp.lan_tcp_ping",
            new=AsyncMock(return_value=(True, 0.8)),
        ):
            from bosch_camera_mcp.server import bosch_camera_lan_ping

            result = await bosch_camera_lan_ping(lan_ip="192.0.2.10")

        assert result.latency_ms == pytest.approx(0.8, abs=0.01)


# ---------------------------------------------------------------------------
# bosch_camera_lan_ping — timeout / unreachable cases
# ---------------------------------------------------------------------------


class TestLanPingTimeout:
    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_timeout_returns_false(self) -> None:
        """When lan_tcp_ping returns False, result.reachable is False."""
        with patch(
            "bosch_camera_mcp.lan_rcp.lan_tcp_ping",
            new=AsyncMock(return_value=(False, -1.0)),
        ):
            from bosch_camera_mcp.server import bosch_camera_lan_ping

            result = await bosch_camera_lan_ping(lan_ip="192.0.2.10")

        assert result.reachable is False
        assert result.latency_ms == pytest.approx(-1.0)

    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_timeout_by_camera_name(self) -> None:
        """Timeout path via camera name resolves correctly."""
        with patch(
            "bosch_camera_mcp.lan_rcp.lan_tcp_ping",
            new=AsyncMock(return_value=(False, -1.0)),
        ):
            from bosch_camera_mcp.server import bosch_camera_lan_ping

            result = await bosch_camera_lan_ping(camera="Garten")

        assert result.reachable is False
        assert result.ip == "192.0.2.10"

    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_timeout_missing_local_ip_raises(self) -> None:
        """Camera with no local_ip configured raises MCPError(local_unavailable)."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lan_ping

        with pytest.raises(MCPError) as exc_info:
            await bosch_camera_lan_ping(camera="Innen")

        assert exc_info.value.code == "local_unavailable"
        assert "local_ip" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_timeout_no_args_raises(self) -> None:
        """Calling with neither camera nor lan_ip raises MCPError."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lan_ping

        with pytest.raises(MCPError) as exc_info:
            await bosch_camera_lan_ping()

        assert exc_info.value.code == "api_unreachable"

    @pytest.mark.asyncio
    async def test_bosch_camera_lan_ping_timeout_unknown_camera_raises(self) -> None:
        """Unknown camera name raises MCPError(unknown_camera)."""
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.server import bosch_camera_lan_ping

        with pytest.raises(MCPError) as exc_info:
            await bosch_camera_lan_ping(camera="Dachboden")

        assert exc_info.value.code == "unknown_camera"


# ---------------------------------------------------------------------------
# prefer_local routing — bosch_camera_privacy_set
# ---------------------------------------------------------------------------


class TestPreferLocalPrivacySet:
    @pytest.mark.asyncio
    async def test_prefer_local_routes_to_rcp_privacy_on_success(self) -> None:
        """prefer_local=True + LAN write succeeds → cloud API NOT called."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode",
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            result = await bosch_camera_privacy_set(
                camera="Garten", enabled=True, prefer_local=True
            )

        from unittest.mock import ANY
        mock_rcp.assert_awaited_once_with("192.0.2.10", True, user="admin", password="secret123", on_401=ANY)
        mock_cloud.assert_not_called()
        assert result is not None

    @pytest.mark.asyncio
    async def test_prefer_local_routes_to_rcp_privacy_fallback_on_failure(self) -> None:
        """prefer_local=True + LAN write fails → falls back to cloud API."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode",
                return_value=True,
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            result = await bosch_camera_privacy_set(
                camera="Garten", enabled=False, prefer_local=True
            )

        mock_cloud.assert_called_once()
        assert result is not None

    @pytest.mark.asyncio
    async def test_prefer_local_false_skips_rcp_uses_cloud(self) -> None:
        """prefer_local=False (default) → only cloud API called, no RCP."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode",
                return_value=True,
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            result = await bosch_camera_privacy_set(
                camera="Garten", enabled=True, prefer_local=False
            )

        mock_rcp.assert_not_awaited()
        mock_cloud.assert_called_once()

    @pytest.mark.asyncio
    async def test_prefer_local_no_local_ip_skips_rcp_goes_to_cloud(self) -> None:
        """prefer_local=True but no local_ip → skips RCP, goes straight to cloud."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode",
                return_value=True,
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            result = await bosch_camera_privacy_set(
                camera="Innen", enabled=True, prefer_local=True
            )

        mock_rcp.assert_not_awaited()
        mock_cloud.assert_called_once()


# ---------------------------------------------------------------------------
# prefer_local routing — bosch_camera_light_set
# ---------------------------------------------------------------------------


class TestPreferLocalLightSet:
    @pytest.mark.asyncio
    async def test_prefer_local_routes_to_rcp_light_on_success(self) -> None:
        """prefer_local=True + LAN write succeeds → enabled=True maps to brightness 100."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_front_light",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_light",
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_light_set

            result = await bosch_camera_light_set(
                camera="Garten", enabled=True, prefer_local=True
            )

        from unittest.mock import ANY
        mock_rcp.assert_awaited_once_with("192.0.2.10", 100, user="admin", password="secret123", on_401=ANY)
        mock_cloud.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefer_local_routes_to_rcp_light_off_maps_to_zero(self) -> None:
        """prefer_local=True + enabled=False → brightness 0 sent to RCP."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_front_light",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_light",
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_light_set

            result = await bosch_camera_light_set(
                camera="Garten", enabled=False, prefer_local=True
            )

        from unittest.mock import ANY
        mock_rcp.assert_awaited_once_with("192.0.2.10", 0, user="admin", password="secret123", on_401=ANY)
        mock_cloud.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefer_local_routes_to_rcp_light_fallback_on_failure(self) -> None:
        """prefer_local=True + LAN write fails → falls back to cloud API."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_front_light",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_light",
                return_value=True,
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_light_set

            result = await bosch_camera_light_set(
                camera="Garten", enabled=True, prefer_local=True
            )

        mock_cloud.assert_called_once()

    @pytest.mark.asyncio
    async def test_prefer_local_false_light_uses_cloud_only(self) -> None:
        """Default prefer_local=False → cloud API only, no RCP."""
        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_front_light",
                new=AsyncMock(return_value=True),
            ) as mock_rcp,
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_light",
                return_value=True,
            ) as mock_cloud,
        ):
            from bosch_camera_mcp.server import bosch_camera_light_set

            result = await bosch_camera_light_set(
                camera="Garten", enabled=True, prefer_local=False
            )

        mock_rcp.assert_not_awaited()
        mock_cloud.assert_called_once()


# ---------------------------------------------------------------------------
# bosch_camera_maintenance_status — recommended_action field
# ---------------------------------------------------------------------------


class TestMaintenanceRecommendedAction:
    @pytest.mark.asyncio
    async def test_maintenance_active_recommended_action_check_lan(self) -> None:
        """State=active → recommended_action='check_lan'."""
        from datetime import datetime, timedelta, timezone

        from bosch_camera_mcp.maintenance import MaintenanceWindow

        now = datetime.now(tz=timezone.utc)
        mw = MaintenanceWindow(
            title="Kamera Wartung",
            link="https://example.com/maint",
            pub_date=now - timedelta(hours=1),
            summary="Wartung aktiv",
            scheduled_start=now - timedelta(minutes=30),
            scheduled_end=now + timedelta(hours=2),
            source="rss:Wartungsarbeiten",
            camera_relevant=True,
        )
        assert mw.state() == "active"

        with patch(
            "bosch_camera_mcp.maintenance.async_fetch_maintenance",
            new=AsyncMock(return_value=mw),
        ):
            from bosch_camera_mcp.server import bosch_camera_maintenance_status

            result = await bosch_camera_maintenance_status()

        assert result["state"] == "active"
        assert result["recommended_action"] == "check_lan"

    @pytest.mark.asyncio
    async def test_maintenance_scheduled_recommended_action_wait(self) -> None:
        """State=scheduled → recommended_action='wait'."""
        from datetime import datetime, timedelta, timezone

        from bosch_camera_mcp.maintenance import MaintenanceWindow

        now = datetime.now(tz=timezone.utc)
        mw = MaintenanceWindow(
            title="Geplante Wartung",
            link="https://example.com/maint2",
            pub_date=now,
            summary="Geplant",
            scheduled_start=now + timedelta(hours=2),
            scheduled_end=now + timedelta(hours=5),
            source="rss:Wartungsarbeiten",
            camera_relevant=True,
        )
        assert mw.state() == "scheduled"

        with patch(
            "bosch_camera_mcp.maintenance.async_fetch_maintenance",
            new=AsyncMock(return_value=mw),
        ):
            from bosch_camera_mcp.server import bosch_camera_maintenance_status

            result = await bosch_camera_maintenance_status()

        assert result["state"] == "scheduled"
        assert result["recommended_action"] == "wait"

    @pytest.mark.asyncio
    async def test_maintenance_idle_recommended_action_null(self) -> None:
        """No announcement → recommended_action=None."""
        with patch(
            "bosch_camera_mcp.maintenance.async_fetch_maintenance",
            new=AsyncMock(return_value=None),
        ):
            from bosch_camera_mcp.server import bosch_camera_maintenance_status

            result = await bosch_camera_maintenance_status()

        assert result["state"] == "idle"
        assert result["recommended_action"] is None

    @pytest.mark.asyncio
    async def test_maintenance_past_recommended_action_null(self) -> None:
        """State=past → recommended_action=None (outage over)."""
        from datetime import datetime, timedelta, timezone

        from bosch_camera_mcp.maintenance import MaintenanceWindow

        now = datetime.now(tz=timezone.utc)
        mw = MaintenanceWindow(
            title="Vergangene Wartung",
            link="https://example.com/maint3",
            pub_date=now - timedelta(hours=5),
            summary="Abgeschlossen",
            scheduled_start=now - timedelta(hours=4),
            scheduled_end=now - timedelta(hours=1),
            source="rss:Wartungsarbeiten",
            camera_relevant=True,
        )
        assert mw.state() == "past"

        with patch(
            "bosch_camera_mcp.maintenance.async_fetch_maintenance",
            new=AsyncMock(return_value=mw),
        ):
            from bosch_camera_mcp.server import bosch_camera_maintenance_status

            result = await bosch_camera_maintenance_status()

        assert result["state"] == "past"
        assert result["recommended_action"] is None

    @pytest.mark.asyncio
    async def test_maintenance_result_contains_recommended_action_key(self) -> None:
        """Result dict always contains recommended_action key regardless of state."""
        with patch(
            "bosch_camera_mcp.maintenance.async_fetch_maintenance",
            new=AsyncMock(return_value=None),
        ):
            from bosch_camera_mcp.server import bosch_camera_maintenance_status

            result = await bosch_camera_maintenance_status()

        assert "recommended_action" in result
