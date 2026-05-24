"""Regression tests for 9 MCP bugs surfaced in live testing 2026-05-24.

Source: Thomas asked Claude to fetch today's Terrasse events via MCP, and the
MCP returned an incomplete + incorrect picture compared to the live cloud:
  - bosch_camera_list missing 2/4 cameras (returned only the stale config)
  - bosch_camera_status('Terrasse'/'Haustüre') not found
  - bosch_camera_events: type='UNKNOWN', has_clip=False for every event
  - bosch_camera_events(<UUID>) not found — name-only resolver
  - bosch_camera_audio_set silently no-op (PascalCase 'SpeakerLevel')
  - bosch_camera_intrusion_get/set always 'hardware_unsupported' (has_sound
    gate is never populated by CLI scan)
  - bosch_camera_lan_ping with no args → wrong error code 'api_unreachable'
  - bosch_camera_audio_set / intrusion_set range errors → 'permission_denied'
  - bosch_camera_snapshot timestamp str-replace mangled date

Each test PINs the post-fix behavior. Run order: red before fix, green after.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests as req_lib

CLOUD_API = "https://residential.cbs.boschsecurity.com"
CAM_ID_TERRASSE = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM_ID_KAMERA = "09ECD6E9-D2BF-42E1-8377-E316A180BAB9"


_VALID_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJleHAiOjQxMDI0NDQ4MDB9"
    ".placeholder"
)


def _cfg_stale() -> dict:
    """Local config with only 2 cameras — mirrors today's stale state."""
    return {
        "account": {
            "username": "test@example.com",
            "bearer_token": _VALID_TOKEN,
            "refresh_token": "",
        },
        "cameras": {
            "Garten": {
                "id": "732DB414-OLD-CACHED-ID",
                "name": "Garten",
                "model": "OUTDOOR",
                "firmware": "7.91.56",
                "mac": "aa:bb:cc:dd:ee:01",
                "local_ip": "",
                "local_username": "",
                "local_password": "",
                "download_folder": "Garten",
            },
            "Kamera": {
                "id": CAM_ID_KAMERA,
                "name": "Kamera",
                "model": "INDOOR",
                "firmware": "7.91.56",
                "mac": "aa:bb:cc:dd:ee:02",
                "local_ip": "",
                "local_username": "",
                "local_password": "",
                "download_folder": "Kamera",
            },
        },
        "settings": {},
        "nvr": {},
    }


_LIVE_VIDEO_INPUTS = [
    {
        "id": CAM_ID_TERRASSE,
        "title": "Terrasse",
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "firmwareVersion": "9.40.102",
        "macAddress": "64-da-a0-33-14-ae",
        "privacyMode": "OFF",
        "connectionStatus": "ONLINE",
        "featureSupport": {"sound": True, "light": True, "panLimit": 0},
        "featureStatus": {},
    },
    {
        "id": CAM_ID_KAMERA,
        "title": "Kamera",
        "hardwareVersion": "INDOOR",
        "firmwareVersion": "7.91.56",
        "macAddress": "64-da-a0-08-36-27",
        "privacyMode": "OFF",
        "connectionStatus": "OFFLINE",
        "featureSupport": {"sound": False, "light": False, "panLimit": 120},
        "featureStatus": {},
    },
    {
        "id": "732DB414-BD88-4CEE-AA2E-0DC5CC733C5E",
        "title": "Haustüre",
        "hardwareVersion": "OUTDOOR",
        "firmwareVersion": "7.91.56",
        "macAddress": "64-da-a0-09-eb-6e",
        "privacyMode": "ON",
        "connectionStatus": "OFFLINE",
        "featureSupport": {"sound": False, "light": True, "panLimit": 0},
        "featureStatus": {},
    },
]


_TODAY_EVENTS = [
    {
        "id": "F4F28955-896D-40C0-A8C1-3AFF23E009AE",
        "videoInputId": CAM_ID_TERRASSE,
        "eventType": "MOVEMENT",
        "eventTags": ["PERSON", "MOVEMENT"],
        "title": "Bewegung",
        "timestamp": "2026-05-24T08:18:02.893+02:00[Europe/Berlin]",
        "videoClipUrl": f"{CLOUD_API}/v11/events/F4F28955.../clip.mp4",
        "videoClipUploadStatus": "Done",
        "imageUrl": f"{CLOUD_API}/v11/events/F4F28955.../snap.jpg",
    },
    {
        "id": "056769F0-BA8B-4E50-A30B-EC413B6208A0",
        "videoInputId": CAM_ID_TERRASSE,
        "eventType": "MOVEMENT",
        "eventTags": ["PERSON", "MOVEMENT"],
        "title": "Bewegung",
        "timestamp": "2026-05-24T08:28:01.130+02:00[Europe/Berlin]",
        "videoClipUrl": None,
        "videoClipUploadStatus": "Unavailable",
        "imageUrl": f"{CLOUD_API}/v11/events/056769F0.../snap.jpg",
    },
]


def _make_fake_bc(cfg: dict) -> MagicMock:
    m = MagicMock()
    m.CLOUD_API = CLOUD_API
    m.DEFAULT_CONFIG = {"account": {}, "cameras": {}, "settings": {}, "nvr": {}}
    m.load_config.return_value = cfg
    m._merge_defaults = lambda c, d: None
    m._is_token_near_expiry.return_value = False
    m._is_token_expired.return_value = False
    m.save_config.return_value = None
    session = req_lib.Session()
    m.make_session.return_value = session
    m.api_ping.side_effect = lambda s, cid: (
        "ONLINE" if cid == CAM_ID_TERRASSE else "OFFLINE"
    )
    m.api_get_events.side_effect = lambda s, cid, limit=10: (
        _TODAY_EVENTS[:limit] if cid == CAM_ID_TERRASSE else []
    )
    m.snap_from_local.return_value = b"\xff\xd8\xff" + b"\x00" * 80
    m.api_get_camera.return_value = _LIVE_VIDEO_INPUTS[0]
    return m


@pytest.fixture()
def stale_cfg() -> dict:
    return _cfg_stale()


@pytest.fixture(autouse=True)
def patch_env(monkeypatch: pytest.MonkeyPatch, stale_cfg: dict):
    """Patch sys.modules['bosch_camera'] + bridge helpers.

    Stages a *stale* local config (2 cams) + a *fresh* cloud response (3 cams)
    — fix should reconcile to the live cloud picture.
    """
    fake_bc = _make_fake_bc(stale_cfg)
    monkeypatch.setitem(sys.modules, "bosch_camera", fake_bc)
    monkeypatch.setitem(sys.modules, "bosch_i18n", MagicMock())

    import bosch_camera_mcp.adapters.cli_bridge as bridge
    import bosch_camera_mcp.server as srv

    monkeypatch.setattr(bridge, "ensure_cli_importable", lambda: None)

    # Mock the cloud GET /v11/video_inputs response. Bridge should ALWAYS
    # fetch (post-fix), not honor a stale local cache.
    import responses as resp_lib
    rsps = resp_lib.RequestsMock(assert_all_requests_are_fired=False)
    rsps.start()
    rsps.add(
        resp_lib.GET,
        f"{CLOUD_API}/v11/video_inputs",
        json=_LIVE_VIDEO_INPUTS,
        status=200,
    )
    yield fake_bc
    rsps.stop()
    rsps.reset()


# ── Bug 1 — camera list always fresh from cloud, never stale cache ──────────


def test_list_picks_up_renamed_and_new_cameras_from_cloud() -> None:
    """Stale config has [Garten, Kamera]; cloud has [Terrasse, Kamera, Haustüre].
    Post-fix: bosch_camera_list must surface the live cloud picture."""
    from bosch_camera_mcp.server import bosch_camera_list

    result = bosch_camera_list()
    names = {s.name for s in result}
    assert "Terrasse" in names, f"missing Terrasse (live cloud), got: {names}"
    assert "Haustüre" in names, f"missing Haustüre (renamed Garten), got: {names}"
    assert len(result) == 3, f"expected 3 live cams, got {len(result)}: {names}"


# ── Bug 2 — hw_version is generation (Gen1/Gen2), not duplicate of model ────


def test_list_hw_version_distinguishes_gen1_from_gen2() -> None:
    """HOME_* hardwareVersion = Gen2. Plain OUTDOOR/INDOOR = Gen1."""
    from bosch_camera_mcp.server import bosch_camera_list

    by_name = {s.name: s for s in bosch_camera_list()}
    assert by_name["Terrasse"].hw_version == "Gen2"  # HOME_Eyes_Outdoor
    assert by_name["Kamera"].hw_version == "Gen1"    # INDOOR
    assert by_name["Haustüre"].hw_version == "Gen1"  # OUTDOOR


# ── Bug 3 — _resolve_cam accepts UUID directly ──────────────────────────────


def test_resolve_cam_accepts_uuid() -> None:
    """Calling status(<UUID>) must work; UUID is the stable identifier."""
    from bosch_camera_mcp.server import bosch_camera_status

    s = bosch_camera_status(camera=CAM_ID_TERRASSE)
    assert s.name == "Terrasse"


# ── Bug 4 — bosch_camera_events maps Bosch field names correctly ───────────


def test_events_use_eventType_and_videoClipUploadStatus() -> None:
    """Cloud uses 'eventType'/'videoClipUploadStatus'. Pre-fix the handler
    read 'type' (always missing → UNKNOWN) and 'clipUrl' (never present)."""
    from bosch_camera_mcp.server import bosch_camera_events

    evs = bosch_camera_events(camera="Terrasse", limit=2)
    assert len(evs) == 2

    e0, e1 = evs[0], evs[1]
    assert e0["type"] == "MOVEMENT", f"expected MOVEMENT, got {e0['type']}"
    assert e0["has_clip"] is True, "Done-status event must report has_clip=True"
    assert e1["type"] == "MOVEMENT"
    assert e1["has_clip"] is False, "Unavailable event must report has_clip=False"


# ── Bug 5 — set_audio writes camelCase 'speakerLevel' (not PascalCase) ─────


def test_set_audio_uses_camelCase_speakerLevel() -> None:
    """PascalCase 'SpeakerLevel' is silently rejected by the Bosch API."""
    import responses as resp_lib

    import bosch_camera_mcp.adapters.cli_bridge as bridge

    captured: list[dict] = []

    def _cap_audio_get(req):
        return (200, {"Content-Type": "application/json"},
                b'{"microphoneLevel": 60, "speakerLevel": 50}')

    def _cap_audio_put(req):
        import json as _json
        captured.append(_json.loads(req.body or "{}"))
        return (204, {}, b"")

    with resp_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_TERRASSE}/audio",
            callback=_cap_audio_get,
        )
        rsps.add_callback(
            resp_lib.PUT,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_TERRASSE}/audio",
            callback=_cap_audio_put,
        )
        session = req_lib.Session()
        bridge.set_audio(session, CAM_ID_TERRASSE, speaker_level=75)

    assert captured, "PUT body was not captured"
    body = captured[0]
    assert body.get("speakerLevel") == 75, (
        f"camelCase 'speakerLevel' missing; body={body}"
    )
    assert "SpeakerLevel" not in body, (
        f"PascalCase 'SpeakerLevel' must not be sent; body={body}"
    )


# ── Bug 6 — Gen2 gate derives from hardwareVersion (HOME_*), not has_sound ─


def test_intrusion_get_works_for_gen2_without_has_sound_flag() -> None:
    """Gen2 cams (hardwareVersion startswith 'HOME_') support intrusion;
    config's missing 'has_sound' must not block them."""
    import responses as resp_lib

    from bosch_camera_mcp.server import bosch_camera_intrusion_get

    with resp_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        # Bridge always refreshes /v11/video_inputs; must be mocked too.
        rsps.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs",
            json=_LIVE_VIDEO_INPUTS,
            status=200,
        )
        rsps.add(
            resp_lib.GET,
            f"{CLOUD_API}/v11/video_inputs/{CAM_ID_TERRASSE}/intrusionDetectionConfig",
            json={"mode": "ACTIVE", "sensitivity": 3, "distance": 8},
            status=200,
        )
        result = bosch_camera_intrusion_get(camera="Terrasse")

    assert result.mode == "ACTIVE"
    assert result.sensitivity == 3
    assert result.distance == 8


def test_intrusion_get_still_blocks_gen1() -> None:
    """Gen1 cams (hardwareVersion 'INDOOR'/'OUTDOOR') must still raise."""
    from bosch_camera_mcp.errors import MCPError
    from bosch_camera_mcp.server import bosch_camera_intrusion_get

    with pytest.raises(MCPError) as exc_info:
        bosch_camera_intrusion_get(camera="Kamera")
    assert exc_info.value.code == "hardware_unsupported"


# ── Bug 7 — lan_ping with no args uses invalid_argument, not api_unreachable


def test_lan_ping_no_args_raises_invalid_argument() -> None:
    import asyncio

    from bosch_camera_mcp.errors import MCPError
    from bosch_camera_mcp.server import bosch_camera_lan_ping

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(bosch_camera_lan_ping())
    assert exc_info.value.code == "invalid_argument", (
        f"expected invalid_argument, got {exc_info.value.code}"
    )


# ── Bug 8 — audio_set / intrusion_set range errors use invalid_argument ────


def test_audio_set_out_of_range_uses_invalid_argument() -> None:
    from bosch_camera_mcp.errors import MCPError
    from bosch_camera_mcp.server import bosch_camera_audio_set

    with pytest.raises(MCPError) as exc_info:
        bosch_camera_audio_set(camera="Terrasse", mic_level=999)
    assert exc_info.value.code == "invalid_argument"


def test_intrusion_set_out_of_range_uses_invalid_argument() -> None:
    from bosch_camera_mcp.errors import MCPError
    from bosch_camera_mcp.server import bosch_camera_intrusion_set

    with pytest.raises(MCPError) as exc_info:
        bosch_camera_intrusion_set(camera="Terrasse", sensitivity=99)
    assert exc_info.value.code == "invalid_argument"


# ── Bug 9 — snapshot timestamp is ISO 8601, not the broken replace chain ──


def test_snapshot_returns_iso_timestamp() -> None:
    """`%Y-%m-%dT%H-%M-%S` with .replace('_','T').replace('-',':') turns
    '2026-05-24T10-30-00' into '2026:05:24T10:30:00' (colons in date)."""
    from bosch_camera_mcp.server import bosch_camera_snapshot

    result = bosch_camera_snapshot(camera="Terrasse")
    ts = result.timestamp
    # ISO 8601: YYYY-MM-DDTHH:MM:SS — dashes in date, colons in time
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", ts), (
        f"expected ISO-8601 timestamp, got {ts!r}"
    )
