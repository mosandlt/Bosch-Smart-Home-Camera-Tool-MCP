"""Tests for MCP resources (bosch://cameras, snapshot, events).

All HTTP and filesystem calls are mocked; no real Bosch API or disk access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared test fixtures (mirrors test_tools_integration.py setup)
# ---------------------------------------------------------------------------

CAM_ID_1 = "aaaa-1111-aaaa-1111"
CAM_ID_2 = "bbbb-2222-bbbb-2222"

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
        "Garten": {
            "id": CAM_ID_1,
            "name": "Garten",
            "model": "HOME_Eyes_Outdoor",
            "firmware": "3.1.0",
            "mac": "aa:bb:cc:dd:ee:01",
            "description": "Rear garden camera",
            "download_folder": "Garten",
            "local_ip": "",
            "local_username": "",
            "local_password": "",
        },
        "Innen": {
            "id": CAM_ID_2,
            "name": "Innen",
            "model": "HOME_Eyes_Indoor",
            "firmware": "3.1.0",
            "mac": "aa:bb:cc:dd:ee:02",
            "description": "Indoor hallway",
            "download_folder": "Innen",
            "local_ip": "",
            "local_username": "",
            "local_password": "",
        },
    },
    "settings": {},
    "nvr": {},
}

_EVENTS_CAM1 = [
    {
        "id": f"evt-{i:03d}",
        "type": "MOTION" if i % 2 == 0 else "PERSON",
        "timestamp": f"2026-05-17T{i:02d}:00:00Z",
        "imageUrl": None,
        "clipUrl": f"https://example.com/clip/{i}" if i < 5 else None,
    }
    for i in range(50)
]


def _make_fake_bc(cfg=_CFG):
    import requests as req_lib

    m = MagicMock()
    m.CLOUD_API = "https://residential.cbs.boschsecurity.com"
    m.DEFAULT_CONFIG = {"account": {}, "cameras": {}, "settings": {}, "nvr": {}}
    m.load_config.return_value = cfg
    m._merge_defaults = lambda c, d: None
    m._is_token_near_expiry.return_value = False
    m.make_session.return_value = req_lib.Session()
    m.save_config.return_value = None
    m.api_ping.side_effect = lambda session, cam_id: (
        "ONLINE" if cam_id == CAM_ID_1 else "OFFLINE"
    )
    m.api_get_events.side_effect = lambda session, cam_id, limit=50: (
        _EVENTS_CAM1[:limit] if cam_id == CAM_ID_1 else []
    )
    m.snap_from_proxy.return_value = b"\xff\xd8\xff" + b"\x00" * 100
    m.snap_from_local.return_value = None
    m.snap_from_events.return_value = (b"\xff\xd8\xff" + b"\x00" * 50, "2026-05-17T08:00:00")
    return m


@pytest.fixture(autouse=True)
def patch_bridge(monkeypatch):
    """Inject fake bosch_camera module and skip CLI path injection."""
    fake_bc = _make_fake_bc()
    monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
    monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())

    import bosch_camera_mcp.adapters.cli_bridge as bridge

    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)

    import requests as req_lib

    fake_session = req_lib.Session()

    def _fake_get_session(config_path=None):
        return _CFG, fake_session, _CFG["cameras"]

    monkeypatch.setattr(bridge, "get_session_and_cameras", _fake_get_session)

    import bosch_camera_mcp.server as srv

    monkeypatch.setattr(srv, "_get_session", _fake_get_session)

    # Also patch the resources module's _get_session helper
    import bosch_camera_mcp.resources as res_mod

    monkeypatch.setattr(res_mod, "_get_session", _fake_get_session)

    yield fake_bc


# ---------------------------------------------------------------------------
# bosch://cameras resource
# ---------------------------------------------------------------------------


class TestCamerasListResource:
    def test_cameras_resource_returns_valid_json(self):
        from bosch_camera_mcp.resources import cameras_list

        raw = cameras_list()
        data = json.loads(raw)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_cameras_resource_has_expected_fields(self):
        from bosch_camera_mcp.resources import cameras_list

        data = json.loads(cameras_list())
        by_name = {c["name"]: c for c in data}
        garten = by_name["Garten"]
        assert garten["id"] == CAM_ID_1
        assert garten["model"] == "HOME_Eyes_Outdoor"
        assert "firmware_version" in garten
        assert "mac" in garten
        assert "description" in garten
        assert garten["firmware_version"] == "3.1.0"
        assert garten["mac"] == "aa:bb:cc:dd:ee:01"

    def test_cameras_resource_includes_status(self):
        from bosch_camera_mcp.resources import cameras_list

        data = json.loads(cameras_list())
        by_name = {c["name"]: c for c in data}
        assert by_name["Garten"]["status"] == "ONLINE"
        assert by_name["Innen"]["status"] == "OFFLINE"

    def test_cameras_resource_returns_string(self):
        from bosch_camera_mcp.resources import cameras_list

        result = cameras_list()
        assert isinstance(result, str)

    def test_cameras_resource_registered_on_mcp(self):
        """Resource must appear in mcp._resource_manager."""
        from bosch_camera_mcp.server import mcp

        uris = [str(r.uri) for r in mcp._resource_manager.list_resources()]
        assert "bosch://cameras" in uris


# ---------------------------------------------------------------------------
# bosch://cameras/{name}/snapshot.jpg resource
# ---------------------------------------------------------------------------


class TestCameraSnapshotResource:
    def test_snapshot_resource_returns_bytes_from_cache(self, tmp_path):
        """Cache hit: existing JPEG in cache dir → returned without new capture."""
        safe_name = "Garten"
        cache_dir = tmp_path / ".cache" / "bosch-camera-mcp" / "snapshots" / safe_name
        cache_dir.mkdir(parents=True)
        fake_jpeg = b"\xff\xd8\xff" + b"\x42" * 80
        (cache_dir / "2026-05-17T10-00-00.jpg").write_bytes(fake_jpeg)

        from bosch_camera_mcp.resources import camera_snapshot

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = camera_snapshot("Garten")

        assert isinstance(result, bytes)
        assert result == fake_jpeg

    def test_snapshot_resource_returns_bytes_type(self, tmp_path):
        """No cache → triggers fresh capture via bosch_camera_snapshot tool."""
        from bosch_camera_mcp.resources import camera_snapshot

        # Ensure cache dir does NOT exist
        cache_dir = (
            tmp_path / ".cache" / "bosch-camera-mcp" / "snapshots" / "Garten"
        )
        assert not cache_dir.exists()

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = camera_snapshot("Garten")

        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_camera_snapshot_resource_triggers_fresh_capture_when_cache_empty(
        self, tmp_path, patch_bridge
    ):
        """Cache miss path must delegate to bosch_camera_snapshot and return bytes."""
        from bosch_camera_mcp.resources import camera_snapshot

        with patch("pathlib.Path.home", return_value=tmp_path):
            data = camera_snapshot("Garten")

        # snap_from_proxy returns fake JPEG bytes
        assert data[:3] == b"\xff\xd8\xff"

    def test_snapshot_resource_template_registered(self):
        """Template bosch://cameras/{name}/snapshot.jpg must be in resource manager."""
        from bosch_camera_mcp.server import mcp

        templates = mcp._resource_manager.list_templates()
        uris = [t.uri_template for t in templates]
        assert "bosch://cameras/{name}/snapshot.jpg" in uris

    def test_unknown_camera_in_snapshot_resource_raises_MCPError(self, tmp_path):
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.resources import camera_snapshot

        with patch("pathlib.Path.home", return_value=tmp_path):
            with pytest.raises(MCPError) as exc_info:
                camera_snapshot("Ghost")
        assert exc_info.value.code == "unknown_camera"


# ---------------------------------------------------------------------------
# bosch://cameras/{name}/events resource
# ---------------------------------------------------------------------------


class TestCameraEventsResource:
    def test_events_resource_returns_json_list(self):
        from bosch_camera_mcp.resources import camera_events

        raw = camera_events("Garten")
        data = json.loads(raw)
        assert isinstance(data, list)

    def test_events_resource_returns_up_to_50_items(self):
        from bosch_camera_mcp.resources import camera_events

        data = json.loads(camera_events("Garten"))
        assert len(data) == 50

    def test_events_resource_item_has_expected_keys(self):
        from bosch_camera_mcp.resources import camera_events

        data = json.loads(camera_events("Garten"))
        first = data[0]
        assert "event_id" in first
        assert "type" in first
        assert "timestamp_iso" in first
        assert "has_clip" in first

    def test_events_resource_offline_camera_returns_empty_list(self):
        from bosch_camera_mcp.resources import camera_events

        data = json.loads(camera_events("Innen"))
        assert data == []

    def test_unknown_camera_in_events_resource_raises_MCPError(self):
        from bosch_camera_mcp.errors import MCPError
        from bosch_camera_mcp.resources import camera_events

        with pytest.raises(MCPError) as exc_info:
            camera_events("Phantom")
        assert exc_info.value.code == "unknown_camera"

    def test_events_resource_template_registered(self):
        """Template bosch://cameras/{name}/events must be in resource manager."""
        from bosch_camera_mcp.server import mcp

        templates = mcp._resource_manager.list_templates()
        uris = [t.uri_template for t in templates]
        assert "bosch://cameras/{name}/events" in uris

    def test_events_resource_clip_flag(self):
        from bosch_camera_mcp.resources import camera_events

        data = json.loads(camera_events("Garten"))
        # Events 0–4 have clipUrl → has_clip=True
        assert data[0]["has_clip"] is True
        # Event 5+ have no clipUrl → has_clip=False
        assert data[5]["has_clip"] is False
