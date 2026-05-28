"""Tests for packaging correctness — pyproject.toml consistency — v0.5.0-alpha."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Path to pyproject.toml (two levels up from tests/)
_PROJECT_ROOT = Path(__file__).parent.parent
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"


def _parse_pyproject_raw() -> str:
    return _PYPROJECT.read_text(encoding="utf-8")


class TestPyprojectConsoleScript:
    def test_pyproject_has_console_script_entry(self) -> None:
        """[project.scripts] must declare the MCP server entry point."""
        content = _parse_pyproject_raw()
        assert "bosch-smart-home-camera-mcp" in content
        assert "bosch_camera_mcp.server:main" in content

    def test_console_script_points_to_main(self) -> None:
        """Entry point must reference the `main` function specifically."""
        content = _parse_pyproject_raw()
        # Ensure it's under [project.scripts] and not just a stray mention
        scripts_section = content.split("[project.scripts]", 1)
        assert len(scripts_section) == 2, "Missing [project.scripts] section"
        scripts_body = scripts_section[1].split("[", 1)[0]  # up to next section
        assert "bosch_camera_mcp.server:main" in scripts_body


class TestPyprojectVersionConsistency:
    def test_pyproject_version_matches_init_version(self) -> None:
        """pyproject.toml version and __init__.__version__ must be in sync."""
        from bosch_camera_mcp import __version__

        content = _parse_pyproject_raw()
        # Find the version line in [project] section
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("version") and "=" in stripped:
                pyproject_version = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                assert pyproject_version == __version__, (
                    f"pyproject.toml version {pyproject_version!r} "
                    f"!= __init__.__version__ {__version__!r}"
                )
                return
        pytest.fail("Could not find version line in pyproject.toml")


class TestModuleImport:
    def test_module_imports_cleanly_from_install_path(self) -> None:
        """Smoke: the package must be importable without side-effects or errors."""
        # Re-import to ensure no cached failure state masks a real issue
        import bosch_camera_mcp
        assert hasattr(bosch_camera_mcp, "__version__")
        assert isinstance(bosch_camera_mcp.__version__, str)
        assert bosch_camera_mcp.__version__  # non-empty

    def test_server_module_importable(self) -> None:
        """server.py must import without requiring bosch_camera on the path."""
        import bosch_camera_mcp.server as srv
        assert hasattr(srv, "main")
        assert hasattr(srv, "_parse_args")
        assert hasattr(srv, "mcp")

    def test_version_is_v1_6_0(self) -> None:
        """Canonical version check for v1.6.0 — motion, recording, autofollow, privacy_sound, unread, health_check_all, token_status."""
        from bosch_camera_mcp import __version__
        assert __version__ == "1.6.0"
