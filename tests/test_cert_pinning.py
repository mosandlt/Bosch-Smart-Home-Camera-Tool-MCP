"""Regression tests for TOFU certificate fingerprint pinning in lan_rcp.py.

Introduced in security fix: CertPinningError, _fetch_fingerprint_sync,
pin_or_verify_cam added to lan_rcp.py to replace raw verify=False on LAN
camera RCP connections.

Test strategy:
- Unit-test _fetch_fingerprint_sync against mocked SSL sockets.
- Test TOFU state machine: first_connect stores, match succeeds, mismatch raises.
- Test rcp_local_write calls pin_or_verify_cam before the httpx request.
- Test cfg=None degrades gracefully (no CertPinningError).
- Test that CertPinningError propagates out of rcp_local_write.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bosch_camera_mcp.lan_rcp import (
    CertPinningError,
    _CFG_KEY,
    _fetch_fingerprint_sync,
    pin_or_verify_cam,
    rcp_local_write,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_DER_A = b"fake-cert-der-camera-mcp-A"
_FAKE_DER_B = b"fake-cert-der-camera-mcp-B-rotated"
_FP_A = hashlib.sha256(_FAKE_DER_A).hexdigest()
_FP_B = hashlib.sha256(_FAKE_DER_B).hexdigest()


def _mock_ssl_wrap(der_bytes: bytes):
    """Return a fake wrap_socket function that emits der_bytes via getpeercert."""
    tls_sock = MagicMock()
    tls_sock.getpeercert.return_value = der_bytes
    tls_sock.__enter__ = MagicMock(return_value=tls_sock)
    tls_sock.__exit__ = MagicMock(return_value=False)

    def _fake_wrap(sock, server_hostname=None):
        return tls_sock

    return _fake_wrap


def _make_raw_sock():
    raw = MagicMock()
    raw.__enter__ = MagicMock(return_value=raw)
    raw.__exit__ = MagicMock(return_value=False)
    return raw


# ---------------------------------------------------------------------------
# _fetch_fingerprint_sync
# ---------------------------------------------------------------------------


class TestFetchFingerprintSync:
    def test_returns_sha256_hex_of_der_cert(self) -> None:
        """_fetch_fingerprint_sync returns SHA-256 hex of the DER certificate."""
        raw = _make_raw_sock()
        with (
            patch("bosch_camera_mcp.lan_rcp.socket.create_connection", return_value=raw),
            patch("bosch_camera_mcp.lan_rcp.ssl.SSLContext") as mock_ctx_cls,
        ):
            mock_ctx = MagicMock()
            mock_ctx.wrap_socket = _mock_ssl_wrap(_FAKE_DER_A)
            mock_ctx_cls.return_value = mock_ctx

            result = _fetch_fingerprint_sync("192.168.1.100")

        assert result == _FP_A

    def test_raises_on_empty_certificate(self) -> None:
        """Empty DER bytes → CertPinningError."""
        raw = _make_raw_sock()
        tls_sock = MagicMock()
        tls_sock.getpeercert.return_value = b""
        tls_sock.__enter__ = MagicMock(return_value=tls_sock)
        tls_sock.__exit__ = MagicMock(return_value=False)

        with (
            patch("bosch_camera_mcp.lan_rcp.socket.create_connection", return_value=raw),
            patch("bosch_camera_mcp.lan_rcp.ssl.SSLContext") as mock_ctx_cls,
        ):
            mock_ctx = MagicMock()
            mock_ctx.wrap_socket = lambda s, server_hostname=None: tls_sock
            mock_ctx_cls.return_value = mock_ctx

            with pytest.raises(CertPinningError, match="No certificate"):
                _fetch_fingerprint_sync("192.168.1.100")

    def test_wraps_network_errors_in_cert_pinning_error(self) -> None:
        """OSError from create_connection is wrapped in CertPinningError."""
        with patch(
            "bosch_camera_mcp.lan_rcp.socket.create_connection",
            side_effect=OSError("refused"),
        ):
            with pytest.raises(CertPinningError, match="Cannot fetch cert"):
                _fetch_fingerprint_sync("192.0.2.1")


# ---------------------------------------------------------------------------
# pin_or_verify_cam — TOFU state machine
# ---------------------------------------------------------------------------


class TestPinOrVerifyCam:
    def test_first_connect_stores_fingerprint(self) -> None:
        """First call stores SHA-256 fingerprint in cfg[_CFG_KEY][host]."""
        cfg: dict[str, Any] = {}
        with patch("bosch_camera_mcp.lan_rcp._fetch_fingerprint_sync", return_value=_FP_A):
            pin_or_verify_cam("192.168.1.100", cfg)

        assert cfg[_CFG_KEY]["192.168.1.100"] == _FP_A

    def test_subsequent_connect_with_matching_fingerprint_succeeds(self) -> None:
        """Matching stored fingerprint → no exception."""
        cfg = {_CFG_KEY: {"192.168.1.100": _FP_A}}
        with patch("bosch_camera_mcp.lan_rcp._fetch_fingerprint_sync", return_value=_FP_A):
            pin_or_verify_cam("192.168.1.100", cfg)  # should not raise

    def test_subsequent_connect_with_different_fingerprint_raises_cert_pinning_error(
        self,
    ) -> None:
        """Fingerprint mismatch raises CertPinningError."""
        cfg = {_CFG_KEY: {"192.168.1.100": _FP_A}}
        with patch("bosch_camera_mcp.lan_rcp._fetch_fingerprint_sync", return_value=_FP_B):
            with pytest.raises(CertPinningError, match="fingerprint mismatch"):
                pin_or_verify_cam("192.168.1.100", cfg)

    def test_no_cfg_does_not_raise(self) -> None:
        """cfg=None → no fingerprint stored or checked, no exception."""
        pin_or_verify_cam("192.168.1.100", None)  # must not raise

    def test_cam_cert_fingerprints_key_auto_created(self) -> None:
        """cfg[_CFG_KEY] is created if missing."""
        cfg: dict[str, Any] = {"account": {}}
        with patch("bosch_camera_mcp.lan_rcp._fetch_fingerprint_sync", return_value=_FP_A):
            pin_or_verify_cam("10.0.0.1", cfg)

        assert _CFG_KEY in cfg


# ---------------------------------------------------------------------------
# rcp_local_write — integration with TOFU
# ---------------------------------------------------------------------------


class TestRcpLocalWriteTOFU:
    @pytest.mark.asyncio
    async def test_pin_or_verify_called_before_httpx_request(self) -> None:
        """rcp_local_write calls pin_or_verify_cam before sending the HTTP request."""
        call_order: list[str] = []

        def _fake_pin(host: str, cfg: Any, port: int = 443, timeout: float = 3.0) -> None:
            call_order.append("pin")

        with patch("bosch_camera_mcp.lan_rcp.pin_or_verify_cam", side_effect=_fake_pin):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.content = b"<ok/>"

                async def _record_get(url, **kwargs):
                    call_order.append("get")
                    return mock_resp

                mock_client.get = _record_get
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_cls.return_value = mock_client

                await rcp_local_write("192.168.1.100", "0x0d00", "00010000", cfg={})

        assert call_order == ["pin", "get"], (
            f"Expected ['pin', 'get'] but got {call_order!r} — "
            "pin_or_verify_cam must run before httpx.get"
        )

    @pytest.mark.asyncio
    async def test_cert_pinning_error_propagates_out_of_rcp_local_write(self) -> None:
        """CertPinningError from pin_or_verify_cam propagates out of rcp_local_write."""
        with patch(
            "bosch_camera_mcp.lan_rcp.pin_or_verify_cam",
            side_effect=CertPinningError("mismatch"),
        ):
            with pytest.raises(CertPinningError, match="mismatch"):
                await rcp_local_write("192.168.1.100", "0x0d00", "00010000", cfg={})

    @pytest.mark.asyncio
    async def test_no_cfg_skips_pinning_and_proceeds(self) -> None:
        """cfg=None → pin_or_verify_cam runs without storing, request proceeds normally."""
        with (
            patch("bosch_camera_mcp.lan_rcp.pin_or_verify_cam") as mock_pin,
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_pin.return_value = None  # no-op
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<ok/>"
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await rcp_local_write("192.168.1.100", "0x0d00", "00010000", cfg=None)

        assert result is True
        mock_pin.assert_called_once_with("192.168.1.100", None)

    @pytest.mark.asyncio
    async def test_existing_tests_unaffected_when_no_cfg(self) -> None:
        """rcp_local_write with no cfg arg works identically to before (backward compat)."""
        with (
            patch("bosch_camera_mcp.lan_rcp.pin_or_verify_cam"),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"<success/>"
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            result = await rcp_local_write("192.168.1.100", "0x0d00", "00010000")

        assert result is True
