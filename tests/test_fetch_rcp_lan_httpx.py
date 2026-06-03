"""Regression tests for _fetch_rcp_lan — the LAN RCP READ helper.

Pins the 2026-06-03 bug: `_fetch_rcp_lan` used `aiohttp.DigestAuth`, which does
NOT exist (aiohttp exposes only `DigestAuthMiddleware`, never a public
`DigestAuth` class). The `auth = aiohttp.DigestAuth(...)` line therefore raised
`AttributeError` on every call; the broad `except Exception` swallowed it and
returned None, so the READ path was silently dead in production — and the two
tools that depend on it (`bosch_camera_onvif_scopes`, `bosch_camera_rcp_version`)
always failed with a misleading "camera offline / credentials invalid" error.

Fix: use `httpx.DigestAuth`, mirroring the working sibling module lan_rcp.py.

These tests would FAIL against the old aiohttp implementation (it returned None
on success) and guard against any revert to a non-existent aiohttp.DigestAuth.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bosch_camera_mcp.server import _fetch_rcp_lan

CAM_IP = "192.0.2.149"
USER = "admin"
PASSWORD = "secret"  # noqa: S105 — fake test credential


def _patch_async_client(mock_resp: MagicMock) -> Any:
    """Return a patch context for httpx.AsyncClient whose .get yields mock_resp."""
    captured_urls: list[str] = []

    async def _fake_get(url: Any, **kwargs: Any) -> MagicMock:
        captured_urls.append(str(url))
        return mock_resp

    ctx = patch("httpx.AsyncClient")
    mock_client = AsyncMock()
    mock_client.get = _fake_get
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return ctx, mock_client, captured_urls


def test_aiohttp_has_no_digest_auth() -> None:
    """The root cause: aiohttp never exposed a public DigestAuth class."""
    import aiohttp

    assert not hasattr(aiohttp, "DigestAuth")


def test_httpx_has_digest_auth() -> None:
    """The fix relies on httpx.DigestAuth, which does exist."""
    import httpx

    assert hasattr(httpx, "DigestAuth")


@pytest.mark.asyncio
async def test_returns_body_bytes_on_http_200() -> None:
    """HTTP 200 → raw body bytes (old aiohttp code wrongly returned None here)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"<rcp><payload>cafe</payload></rcp>"

    ctx, mock_client, captured_urls = _patch_async_client(mock_resp)
    with ctx as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await _fetch_rcp_lan(CAM_IP, USER, PASSWORD, "0x0a98")

    assert result == b"<rcp><payload>cafe</payload></rcp>"
    assert len(captured_urls) == 1
    assert captured_urls[0].startswith(f"https://{CAM_IP}/rcp.xml")


@pytest.mark.asyncio
async def test_uses_httpx_digest_auth_not_aiohttp() -> None:
    """Auth must be constructed via httpx.DigestAuth — regression guard."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"ok"

    ctx, mock_client, _ = _patch_async_client(mock_resp)
    with (
        ctx as mock_client_cls,
        patch("httpx.DigestAuth") as mock_digest,
    ):
        mock_client_cls.return_value = mock_client
        await _fetch_rcp_lan(CAM_IP, USER, PASSWORD, "0xff00")

    mock_digest.assert_called_once_with(USER, PASSWORD)


@pytest.mark.asyncio
async def test_opcode_gets_0x_prefix_when_missing() -> None:
    """A bare opcode (no 0x) is normalised to 0x-prefixed in the query."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"ok"

    captured_params: list[Any] = []

    async def _fake_get(url: Any, **kwargs: Any) -> MagicMock:
        captured_params.append(kwargs.get("params"))
        return mock_resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = _fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client
        await _fetch_rcp_lan(CAM_IP, USER, PASSWORD, "ff04")

    assert captured_params[0]["command"] == "0xff04"


@pytest.mark.asyncio
async def test_returns_none_on_non_200() -> None:
    """A non-200 (e.g. 401 auth failure) yields None, not an exception."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.content = b""

    ctx, mock_client, _ = _patch_async_client(mock_resp)
    with ctx as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await _fetch_rcp_lan(CAM_IP, USER, PASSWORD, "0x0a98")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_transport_error() -> None:
    """A network/transport exception is swallowed and yields None."""
    import httpx

    async def _boom(url: Any, **kwargs: Any) -> MagicMock:
        raise httpx.ConnectError("connection refused")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = _boom
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client
        result = await _fetch_rcp_lan(CAM_IP, USER, PASSWORD, "0x0a98")

    assert result is None
