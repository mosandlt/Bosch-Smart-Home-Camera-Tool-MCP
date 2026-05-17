"""Tests for MCP prompts (daily-camera-summary, pre-leave-check).

Prompts are pure instruction templates — no API calls, no mocks needed.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# daily-camera-summary prompt
# ---------------------------------------------------------------------------


class TestDailyCameraSummaryPrompt:
    def test_daily_summary_prompt_returns_message_sequence(self):
        from bosch_camera_mcp.prompts import daily_camera_summary

        messages = daily_camera_summary()
        assert isinstance(messages, list)
        assert len(messages) >= 1

    def test_daily_summary_prompt_default_hours_is_24(self):
        from bosch_camera_mcp.prompts import daily_camera_summary

        messages = daily_camera_summary()
        # Default hours=24 must appear in the message text
        text = messages[0].content.text
        assert "24" in text

    def test_daily_summary_prompt_accepts_hours_arg_12(self):
        from bosch_camera_mcp.prompts import daily_camera_summary

        messages = daily_camera_summary(hours=12)
        text = messages[0].content.text
        assert "12" in text

    def test_daily_summary_prompt_accepts_hours_arg_48(self):
        from bosch_camera_mcp.prompts import daily_camera_summary

        messages = daily_camera_summary(hours=48)
        text = messages[0].content.text
        assert "48" in text

    def test_daily_summary_prompt_references_bosch_camera_list(self):
        from bosch_camera_mcp.prompts import daily_camera_summary

        text = "".join(m.content.text for m in daily_camera_summary())
        assert "bosch_camera_list" in text

    def test_daily_summary_prompt_references_bosch_camera_events(self):
        from bosch_camera_mcp.prompts import daily_camera_summary

        text = "".join(m.content.text for m in daily_camera_summary())
        assert "bosch_camera_events" in text

    def test_daily_summary_messages_have_user_role(self):
        from bosch_camera_mcp.prompts import daily_camera_summary

        messages = daily_camera_summary()
        assert all(m.role == "user" for m in messages)

    def test_daily_summary_prompt_registered_on_mcp(self):
        from bosch_camera_mcp.server import mcp

        names = [p.name for p in mcp._prompt_manager.list_prompts()]
        assert "daily-camera-summary" in names

    def test_daily_summary_prompt_has_hours_argument(self):
        from bosch_camera_mcp.server import mcp

        prompts = {p.name: p for p in mcp._prompt_manager.list_prompts()}
        assert "daily-camera-summary" in prompts
        arg_names = [a.name for a in (prompts["daily-camera-summary"].arguments or [])]
        assert "hours" in arg_names

    def test_daily_summary_hours_arg_is_optional(self):
        """hours has a default value → required=False in MCP prompt argument."""
        from bosch_camera_mcp.server import mcp

        prompts = {p.name: p for p in mcp._prompt_manager.list_prompts()}
        hours_arg = next(
            a for a in (prompts["daily-camera-summary"].arguments or [])
            if a.name == "hours"
        )
        assert hours_arg.required is False


# ---------------------------------------------------------------------------
# pre-leave-check prompt
# ---------------------------------------------------------------------------


class TestPreLeaveCheckPrompt:
    def test_pre_leave_check_prompt_returns_messages(self):
        from bosch_camera_mcp.prompts import pre_leave_check

        messages = pre_leave_check()
        assert isinstance(messages, list)
        assert len(messages) >= 1

    def test_pre_leave_check_references_bosch_camera_list(self):
        from bosch_camera_mcp.prompts import pre_leave_check

        text = "".join(m.content.text for m in pre_leave_check())
        assert "bosch_camera_list" in text

    def test_pre_leave_check_references_bosch_camera_snapshot(self):
        from bosch_camera_mcp.prompts import pre_leave_check

        text = "".join(m.content.text for m in pre_leave_check())
        assert "bosch_camera_snapshot" in text

    def test_pre_leave_check_references_bosch_camera_events(self):
        from bosch_camera_mcp.prompts import pre_leave_check

        text = "".join(m.content.text for m in pre_leave_check())
        assert "bosch_camera_events" in text

    def test_pre_leave_check_references_privacy_set(self):
        from bosch_camera_mcp.prompts import pre_leave_check

        text = "".join(m.content.text for m in pre_leave_check())
        assert "bosch_camera_privacy_set" in text

    def test_pre_leave_check_messages_have_user_role(self):
        from bosch_camera_mcp.prompts import pre_leave_check

        assert all(m.role == "user" for m in pre_leave_check())

    def test_pre_leave_check_registered_on_mcp(self):
        from bosch_camera_mcp.server import mcp

        names = [p.name for p in mcp._prompt_manager.list_prompts()]
        assert "pre-leave-check" in names

    def test_pre_leave_check_has_no_required_arguments(self):
        """pre-leave-check takes no arguments."""
        from bosch_camera_mcp.server import mcp

        prompts = {p.name: p for p in mcp._prompt_manager.list_prompts()}
        args = prompts["pre-leave-check"].arguments or []
        assert args == []


# ---------------------------------------------------------------------------
# Cross-cutting: referenced tools must actually be registered
# ---------------------------------------------------------------------------


class TestPromptToolReferences:
    """Assert every tool name mentioned in prompts is registered on the MCP app."""

    REFERENCED_TOOLS = {
        "bosch_camera_list",
        "bosch_camera_events",
        "bosch_camera_snapshot",
        "bosch_camera_privacy_set",
    }

    def test_referenced_tools_exist(self):
        from bosch_camera_mcp.server import mcp

        registered = {t.name for t in mcp._tool_manager.list_tools()}
        for tool_name in self.REFERENCED_TOOLS:
            assert tool_name in registered, (
                f"Tool {tool_name!r} referenced in a prompt but not registered on mcp"
            )
