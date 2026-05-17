"""Tests for transport-mode CLI arg parsing and main() dispatch — v0.5.0-alpha."""

from __future__ import annotations

import pytest

from bosch_camera_mcp.server import _parse_args


# ── Argument parsing ──────────────────────────────────────────────────────────


class TestParseArgsTransport:
    def test_parse_args_default_transport_is_stdio(self) -> None:
        args = _parse_args([])
        assert args.transport == "stdio"

    def test_parse_args_http_transport_accepted(self) -> None:
        args = _parse_args(["--transport", "http"])
        assert args.transport == "http"

    def test_parse_args_sse_transport_accepted(self) -> None:
        args = _parse_args(["--transport", "sse"])
        assert args.transport == "sse"

    def test_parse_args_stdio_transport_accepted(self) -> None:
        args = _parse_args(["--transport", "stdio"])
        assert args.transport == "stdio"

    def test_parse_args_invalid_transport_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--transport", "websocket"])
        assert exc_info.value.code != 0

    def test_http_host_defaults_to_localhost(self) -> None:
        """Security default: never bind to 0.0.0.0 unless explicitly set."""
        args = _parse_args(["--transport", "http"])
        assert args.http_host == "127.0.0.1"

    def test_http_port_defaults_to_8765(self) -> None:
        args = _parse_args([])
        assert args.http_port == 8765

    def test_http_port_custom_value_accepted(self) -> None:
        args = _parse_args(["--transport", "http", "--http-port", "9000"])
        assert args.http_port == 9000

    def test_http_host_custom_value_accepted(self) -> None:
        args = _parse_args(["--transport", "http", "--http-host", "0.0.0.0"])
        assert args.http_host == "0.0.0.0"

    def test_transport_and_config_coexist(self) -> None:
        args = _parse_args(["--transport", "http", "--config", "/tmp/cfg.json"])
        assert args.transport == "http"
        assert args.config == "/tmp/cfg.json"


# ── main() dispatch ───────────────────────────────────────────────────────────


class TestMainDispatch:
    """Verify that main() calls mcp.run with the correct transport kwarg.

    We mock mcp.run so no real server sockets are opened.  The test patches
    the `mcp` instance that lives inside bosch_camera_mcp.server.
    """

    def test_main_dispatches_to_stdio_by_default(self, mocker) -> None:
        mock_run = mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main

        main([])
        mock_run.assert_called_once_with(transport="stdio")

    def test_main_dispatches_to_stdio_when_explicit(self, mocker) -> None:
        mock_run = mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main

        main(["--transport", "stdio"])
        mock_run.assert_called_once_with(transport="stdio")

    def test_main_dispatches_to_streamable_http_when_requested(self, mocker) -> None:
        mock_run = mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main

        main(["--transport", "http"])
        mock_run.assert_called_once_with(transport="streamable-http")

    def test_main_dispatches_to_sse_when_requested(self, mocker) -> None:
        mock_run = mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main

        main(["--transport", "sse"])
        mock_run.assert_called_once_with(transport="sse")

    def test_main_sets_host_on_mcp_settings_for_http(self, mocker) -> None:
        mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main, mcp

        main(["--transport", "http", "--http-host", "192.168.1.5", "--http-port", "8888"])
        assert mcp.settings.host == "192.168.1.5"
        assert mcp.settings.port == 8888

    def test_main_sets_port_on_mcp_settings_for_sse(self, mocker) -> None:
        mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main, mcp

        main(["--transport", "sse", "--http-port", "9999"])
        assert mcp.settings.port == 9999

    def test_main_does_not_mutate_settings_for_stdio(self, mocker) -> None:
        mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main, mcp

        original_host = mcp.settings.host
        original_port = mcp.settings.port
        main(["--transport", "stdio"])
        # stdio path must NOT touch settings
        assert mcp.settings.host == original_host
        assert mcp.settings.port == original_port

    def test_main_returns_zero(self, mocker) -> None:
        mocker.patch("bosch_camera_mcp.server.mcp.run")
        from bosch_camera_mcp.server import main

        rc = main([])
        assert rc == 0
