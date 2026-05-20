"""Regression tests for LAN-RCP credential rotation (v1.3.4).

Root cause: Bosch Gen2 cameras rotate Digest credentials on every
PUT /v11/video_inputs/{id}/connection LOCAL.  Static creds in
bosch_config.json become stale → HTTP 401 on every subsequent
rcp_local_write → all LAN writes silently fail until manual refresh.

Fix: rcp_local_write accepts an optional ``on_401`` async callback.  On HTTP
401 the callback is invoked (it does PUT /connection LOCAL, extracts fresh
creds, persists them to bosch_config.json), and the write is retried once
with the new creds.  If the callback is absent or raises, the 401 is returned
as False (original best-effort behaviour).

Test strategy:
- Unit: rcp_local_write on_401 happy path → callback invoked, write retried with
  new creds, final result True.
- Unit: rcp_local_write on_401 absent → 401 returns False immediately (no retry).
- Unit: rcp_local_write callback raises → 401 returns False (cloud unavailable path).
- Unit: rcp_local_write max-1-retry cap → callback called once even if second
  attempt also returns 401 (no infinite loop).
- Unit: rcp_local_write_privacy / rcp_local_write_front_light forward on_401 kwarg.
- Integration: refresh_local_creds() → PUT /connection body contains
  {"type": "LOCAL"} and response user/password are returned.
- Integration: refresh_local_creds() → creds persisted to bosch_config.json.
- Integration: refresh_local_creds() returns None when PUT /connection returns non-200.
- Server: bosch_camera_privacy_set prefer_local=True wires refresh callback into
  rcp_local_write_privacy.
- Server: bosch_camera_light_set prefer_local=True wires refresh callback into
  rcp_local_write_front_light.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures / constants
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
            "local_username": "old-user",
            "local_password": "old-pass",
            "has_light": True,
            "pan_limit": 0,
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
# Helpers
# ---------------------------------------------------------------------------


def _make_http_response(status: int, content: bytes = b"<ok/>") -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.content = content
    return mock_resp


def _make_async_client(responses: list[MagicMock]) -> tuple[MagicMock, MagicMock]:
    """Return (mock_client_cls, mock_client) where client.get yields responses in order."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=responses)
    mock_client_cls = MagicMock(return_value=mock_client)
    return mock_client_cls, mock_client


# ---------------------------------------------------------------------------
# Unit: rcp_local_write on_401 callback — happy path
# ---------------------------------------------------------------------------


class TestCredRotationHappyPath:
    @pytest.mark.asyncio
    async def test_on_401_callback_invoked_and_write_retried(self) -> None:
        """On HTTP 401: on_401 callback is awaited, write retried with new creds."""
        first_401 = _make_http_response(401)
        second_200 = _make_http_response(200, b"<rcp><payload>ok</payload></rcp>")

        mock_client_cls, mock_client = _make_async_client([first_401, second_200])

        new_user = "fresh-user"
        new_pass = "fresh-pass"
        on_401_cb = AsyncMock(return_value=(new_user, new_pass))

        with patch("httpx.AsyncClient", mock_client_cls):
            with patch("httpx.DigestAuth") as mock_digest_cls:
                # DigestAuth is called twice: first with old creds, then with new
                mock_digest_cls.side_effect = [MagicMock(), MagicMock()]

                from bosch_camera_mcp.lan_rcp import rcp_local_write

                result = await rcp_local_write(
                    "192.0.2.149",
                    "0x0d00",
                    "00010000",
                    user="old-user",
                    password="old-pass",
                    on_401=on_401_cb,
                )

        assert result is True
        on_401_cb.assert_awaited_once()

        # DigestAuth called twice: old creds for first attempt, new creds for retry
        assert mock_digest_cls.call_count == 2
        mock_digest_cls.assert_any_call("old-user", "old-pass")
        mock_digest_cls.assert_any_call("fresh-user", "fresh-pass")

    @pytest.mark.asyncio
    async def test_on_401_callback_not_invoked_on_200(self) -> None:
        """If first attempt succeeds (200), on_401 callback is never invoked."""
        first_200 = _make_http_response(200, b"<rcp><payload>ok</payload></rcp>")
        mock_client_cls, _ = _make_async_client([first_200])
        on_401_cb = AsyncMock(return_value=("u", "p"))

        with patch("httpx.AsyncClient", mock_client_cls):
            with patch("httpx.DigestAuth", return_value=MagicMock()):
                from bosch_camera_mcp.lan_rcp import rcp_local_write

                result = await rcp_local_write(
                    "192.0.2.149",
                    "0x0d00",
                    "00010000",
                    user="old-user",
                    password="old-pass",
                    on_401=on_401_cb,
                )

        assert result is True
        on_401_cb.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unit: rcp_local_write on_401 absent — original best-effort behaviour
# ---------------------------------------------------------------------------


class TestCredRotationNoCallback:
    @pytest.mark.asyncio
    async def test_no_callback_401_returns_false(self) -> None:
        """Without on_401 callback, HTTP 401 still returns False immediately."""
        first_401 = _make_http_response(401)
        mock_client_cls, _ = _make_async_client([first_401])

        with patch("httpx.AsyncClient", mock_client_cls):
            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write(
                "192.0.2.149", "0x0d00", "00010000",
                user="stale-user", password="stale-pass",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_no_callback_no_creds_401_returns_false(self) -> None:
        """Without creds or callback, 401 returns False — no regression from v1.3.3."""
        first_401 = _make_http_response(401)
        mock_client_cls, _ = _make_async_client([first_401])

        with patch("httpx.AsyncClient", mock_client_cls):
            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write("192.0.2.149", "0x0d00", "00010000")

        assert result is False


# ---------------------------------------------------------------------------
# Unit: rcp_local_write callback raises → 401 returned as False
# ---------------------------------------------------------------------------


class TestCredRotationCallbackFailure:
    @pytest.mark.asyncio
    async def test_callback_raises_returns_false(self) -> None:
        """If on_401 callback raises (cloud unavailable), False is returned — original 401 surfaced."""
        first_401 = _make_http_response(401)
        mock_client_cls, _ = _make_async_client([first_401])

        async def _failing_cb() -> tuple[str, str] | None:
            raise RuntimeError("cloud is down")

        with patch("httpx.AsyncClient", mock_client_cls):
            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write(
                "192.0.2.149",
                "0x0d00",
                "00010000",
                user="old-user",
                password="old-pass",
                on_401=_failing_cb,  # type: ignore[arg-type]
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_callback_returns_none_returns_false(self) -> None:
        """If on_401 callback returns None (no creds), False is returned."""
        first_401 = _make_http_response(401)
        mock_client_cls, _ = _make_async_client([first_401])

        on_401_cb: AsyncMock = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", mock_client_cls):
            from bosch_camera_mcp.lan_rcp import rcp_local_write

            result = await rcp_local_write(
                "192.0.2.149",
                "0x0d00",
                "00010000",
                user="old-user",
                password="old-pass",
                on_401=on_401_cb,
            )

        assert result is False


# ---------------------------------------------------------------------------
# Unit: max-1-retry cap — no infinite loop if camera keeps returning 401
# ---------------------------------------------------------------------------


class TestCredRotationMaxOneRetry:
    @pytest.mark.asyncio
    async def test_second_401_returns_false_no_second_callback(self) -> None:
        """Even with on_401 callback, second HTTP 401 returns False (cap = 1 retry)."""
        first_401 = _make_http_response(401)
        second_401 = _make_http_response(401)
        mock_client_cls, _ = _make_async_client([first_401, second_401])

        on_401_cb = AsyncMock(return_value=("new-user", "new-pass"))

        with patch("httpx.AsyncClient", mock_client_cls):
            with patch("httpx.DigestAuth", return_value=MagicMock()):
                from bosch_camera_mcp.lan_rcp import rcp_local_write

                result = await rcp_local_write(
                    "192.0.2.149",
                    "0x0d00",
                    "00010000",
                    user="old-user",
                    password="old-pass",
                    on_401=on_401_cb,
                )

        assert result is False
        # Callback invoked exactly once (not looped)
        assert on_401_cb.await_count == 1


# ---------------------------------------------------------------------------
# Unit: rcp_local_write_privacy / rcp_local_write_front_light forward on_401
# ---------------------------------------------------------------------------


class TestPrivacyLightForwardOn401:
    @pytest.mark.asyncio
    async def test_privacy_forwards_on_401_kwarg(self) -> None:
        """rcp_local_write_privacy passes on_401 through to rcp_local_write."""
        cb = AsyncMock(return_value=("u", "p"))

        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_privacy

            result = await rcp_local_write_privacy(
                "192.0.2.149", True,
                user="u", password="p",
                on_401=cb,
            )

        assert result is True
        _, kwargs = mock_write.call_args
        assert kwargs.get("on_401") is cb

    @pytest.mark.asyncio
    async def test_privacy_without_on_401_still_works(self) -> None:
        """rcp_local_write_privacy without on_401 is unchanged (backward compat)."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_privacy

            result = await rcp_local_write_privacy("192.0.2.149", True)

        assert result is True
        _, kwargs = mock_write.call_args
        # on_401 either absent or None — both are acceptable
        assert kwargs.get("on_401") is None

    @pytest.mark.asyncio
    async def test_front_light_forwards_on_401_kwarg(self) -> None:
        """rcp_local_write_front_light passes on_401 through to rcp_local_write."""
        cb = AsyncMock(return_value=("u", "p"))

        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_front_light

            result = await rcp_local_write_front_light(
                "192.0.2.149", 100,
                user="u", password="p",
                on_401=cb,
            )

        assert result is True
        _, kwargs = mock_write.call_args
        assert kwargs.get("on_401") is cb

    @pytest.mark.asyncio
    async def test_front_light_without_on_401_still_works(self) -> None:
        """rcp_local_write_front_light without on_401 is unchanged (backward compat)."""
        with patch(
            "bosch_camera_mcp.lan_rcp.rcp_local_write",
            new=AsyncMock(return_value=True),
        ) as mock_write:
            from bosch_camera_mcp.lan_rcp import rcp_local_write_front_light

            result = await rcp_local_write_front_light("192.0.2.149", 50)

        assert result is True
        _, kwargs = mock_write.call_args
        assert kwargs.get("on_401") is None


# ---------------------------------------------------------------------------
# Integration: refresh_local_creds() — PUT /connection LOCAL
# ---------------------------------------------------------------------------


class TestRefreshLocalCreds:
    @pytest.mark.asyncio
    async def test_refresh_calls_put_connection_local(self) -> None:
        """refresh_local_creds sends PUT /connection with type=LOCAL."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"user": "new-user", "password": "new-pass"}

        fake_session = req_lib.Session()
        with patch.object(fake_session, "put", return_value=mock_resp) as mock_put:
            from bosch_camera_mcp.lan_rcp import refresh_local_creds

            result = await refresh_local_creds(
                cam_id=CAM_ID_1,
                session=fake_session,
                cfg={
                    "cameras": {
                        "Terrasse": {
                            "id": CAM_ID_1,
                            "local_username": "old-user",
                            "local_password": "old-pass",
                        }
                    }
                },
                cam_name="Terrasse",
                config_path=None,
            )

        assert result == ("new-user", "new-pass")
        mock_put.assert_called_once()
        call_args = mock_put.call_args
        # URL contains cam_id
        assert CAM_ID_1 in call_args[0][0]
        # Body contains type=LOCAL
        body = call_args[1].get("json") or call_args[0][1]
        assert body.get("type") == "LOCAL"

    @pytest.mark.asyncio
    async def test_refresh_persists_creds_to_config(self) -> None:
        """refresh_local_creds writes fresh user/password back to bosch_config.json."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"user": "rotated-user", "password": "rotated-pass"}

        cfg: dict[str, Any] = {
            "cameras": {
                "Terrasse": {
                    "id": CAM_ID_1,
                    "local_username": "old-user",
                    "local_password": "old-pass",
                }
            }
        }

        # Write config to a temp file so we can verify persistence
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(cfg, f)
            tmp_path = f.name

        try:
            fake_session = req_lib.Session()
            with patch.object(fake_session, "put", return_value=mock_resp):
                from bosch_camera_mcp.lan_rcp import refresh_local_creds

                result = await refresh_local_creds(
                    cam_id=CAM_ID_1,
                    session=fake_session,
                    cfg=cfg,
                    cam_name="Terrasse",
                    config_path=tmp_path,
                )

            # In-memory cfg updated
            assert cfg["cameras"]["Terrasse"]["local_username"] == "rotated-user"
            assert cfg["cameras"]["Terrasse"]["local_password"] == "rotated-pass"

            # On-disk config updated
            with open(tmp_path) as fh:
                on_disk = json.load(fh)
            assert on_disk["cameras"]["Terrasse"]["local_username"] == "rotated-user"
            assert on_disk["cameras"]["Terrasse"]["local_password"] == "rotated-pass"

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_refresh_returns_none_on_non_200(self) -> None:
        """refresh_local_creds returns None when PUT /connection returns non-200."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 503

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
    async def test_refresh_returns_none_on_network_error(self) -> None:
        """refresh_local_creds returns None if the PUT /connection raises (cloud down)."""
        import requests as req_lib
        import requests.exceptions

        fake_session = req_lib.Session()
        with patch.object(
            fake_session, "put",
            side_effect=requests.exceptions.ConnectionError("offline"),
        ):
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
    async def test_refresh_returns_none_if_response_missing_creds(self) -> None:
        """refresh_local_creds returns None when PUT /connection response has no user/password."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"streamUrl": "rtsp://..."}  # no user/password

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
# Server integration: prefer_local wires refresh callback
# ---------------------------------------------------------------------------


class TestServerWiresRefreshCallback:
    @pytest.mark.asyncio
    async def test_privacy_set_prefer_local_passes_on_401_callback(self) -> None:
        """bosch_camera_privacy_set with prefer_local=True passes on_401 to rcp_local_write_privacy."""
        cb_sentinel: list[Any] = []

        async def _capture_write(
            ip: str,
            enabled: bool,
            *,
            user: Any = None,
            password: Any = None,
            on_401: Any = None,
        ) -> bool:
            cb_sentinel.append(on_401)
            return True

        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=_capture_write,
            ),
            patch("bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode"),
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            await bosch_camera_privacy_set(camera="Terrasse", enabled=True, prefer_local=True)

        assert len(cb_sentinel) == 1
        assert cb_sentinel[0] is not None, (
            "on_401 callback must be wired in — got None (cred-rotation broken)"
        )
        assert callable(cb_sentinel[0])

    @pytest.mark.asyncio
    async def test_light_set_prefer_local_passes_on_401_callback(self) -> None:
        """bosch_camera_light_set with prefer_local=True passes on_401 to rcp_local_write_front_light."""
        cb_sentinel: list[Any] = []

        async def _capture_write(
            ip: str,
            brightness: int,
            *,
            user: Any = None,
            password: Any = None,
            on_401: Any = None,
        ) -> bool:
            cb_sentinel.append(on_401)
            return True

        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_front_light",
                new=_capture_write,
            ),
            patch("bosch_camera_mcp.adapters.cli_bridge.set_light"),
        ):
            from bosch_camera_mcp.server import bosch_camera_light_set

            await bosch_camera_light_set(camera="Terrasse", enabled=True, prefer_local=True)

        assert len(cb_sentinel) == 1
        assert cb_sentinel[0] is not None, (
            "on_401 callback must be wired in — got None (cred-rotation broken)"
        )
        assert callable(cb_sentinel[0])

    @pytest.mark.asyncio
    async def test_privacy_set_prefer_local_false_no_callback(self) -> None:
        """When prefer_local=False, cloud path is taken — no on_401 callback involved."""
        rcp_called: list[bool] = []

        async def _should_not_be_called(*args: Any, **kwargs: Any) -> bool:
            rcp_called.append(True)
            return False

        with (
            patch(
                "bosch_camera_mcp.lan_rcp.rcp_local_write_privacy",
                new=_should_not_be_called,
            ),
            patch(
                "bosch_camera_mcp.adapters.cli_bridge.set_privacy_mode",
                return_value=True,
            ),
        ):
            from bosch_camera_mcp.server import bosch_camera_privacy_set

            await bosch_camera_privacy_set(camera="Terrasse", enabled=True, prefer_local=False)

        assert rcp_called == [], "RCP path must not be called when prefer_local=False"
