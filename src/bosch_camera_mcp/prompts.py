"""MCP Prompts — v0.4.0-alpha.

Two prompts registered on the shared FastMCP app:

  daily-camera-summary   — multi-step daily report across all cameras
  pre-leave-check        — snapshot + anomaly check before leaving home

Prompts are pure instruction templates — they perform no API calls.
All tool names referenced must be registered in server.py.
"""

from __future__ import annotations

from mcp.server.fastmcp.prompts.base import UserMessage

from .server import mcp

# ── daily-camera-summary ──────────────────────────────────────────────────────


@mcp.prompt(
    name="daily-camera-summary",
    description=(
        "Walk through today's events on all cameras and produce a structured daily "
        "report: motion count, person count, audio events, time distribution, and "
        "anomaly highlights."
    ),
)
def daily_camera_summary(hours: int = 24) -> list[UserMessage]:
    """Generate a multi-step instruction sequence for Claude to produce a daily camera report.

    Args:
        hours: Look-back window in hours (default 24 = last 24 h).
    """
    return [
        UserMessage(
            f"Please produce a daily camera activity report for the last {hours} hours.\n\n"
            "Follow these steps in order:\n\n"
            "1. Call `bosch_camera_list` to retrieve all configured cameras.\n"
            "2. For each camera returned, call `bosch_camera_events` with an appropriate "
            f"   limit to cover the last {hours} hours of activity.\n"
            "3. For each camera, summarise:\n"
            "   - Total event count broken down by type (MOTION, PERSON, AUDIO, OTHER).\n"
            "   - Time distribution: morning (06–12), afternoon (12–18), evening (18–24), "
            "     night (00–06).\n"
            "   - Whether any clip is available for the most recent event.\n"
            "4. Highlight anomalies, for example:\n"
            "   - Unusually high event count compared to typical (>10 events in one hour).\n"
            "   - Activity during sleeping hours (00:00–05:00).\n"
            "   - PERSON events on outdoor cameras between 22:00 and 06:00.\n"
            "5. Close with a one-sentence overall summary (quiet day / moderate activity / "
            "   high activity) and flag any cameras that are currently OFFLINE.\n\n"
            "Format the report in clear Markdown with one section per camera, "
            "followed by a brief overall summary section."
        )
    ]


# ── pre-leave-check ───────────────────────────────────────────────────────────


@mcp.prompt(
    name="pre-leave-check",
    description=(
        "Pre-departure routine: snapshot every camera, describe what is visible, "
        "flag anomalies (open windows, unexpected occupants, lights on), and "
        "recommend enabling privacy mode on indoor cameras."
    ),
)
def pre_leave_check() -> list[UserMessage]:
    """Generate a pre-departure safety check instruction sequence."""
    return [
        UserMessage(
            "Please run a pre-departure camera check. Follow these steps in order:\n\n"
            "1. Call `bosch_camera_list` to get all configured cameras and their current "
            "   status.\n"
            "2. For each camera that is ONLINE:\n"
            "   a. Call `bosch_camera_snapshot` with `prefer_local=True` to obtain the "
            "      latest image.\n"
            "   b. Examine the snapshot and briefly describe what is visible "
            "      (people, objects, lighting conditions, approximate scene).\n"
            "3. Flag any of the following anomalies if observed:\n"
            "   - Open windows or doors visible in an indoor camera frame.\n"
            "   - Unexpected occupants (people visible on indoor cameras).\n"
            "   - Lights left on in empty rooms.\n"
            "   - Motion detected in the last 5 minutes on any camera "
            "     (`bosch_camera_events` with limit=5).\n"
            "4. For every indoor camera (model contains 'Indoor' or name contains "
            "   'Innen' / 'Indoor'):\n"
            "   - Check whether privacy mode is currently ON.\n"
            "   - If privacy mode is OFF, recommend calling `bosch_camera_privacy_set` "
            "     with `enabled=True` before leaving.\n"
            "5. Provide a concise checklist summary at the end:\n"
            "   - ✅ / ❌ per camera (snapshot OK, no anomalies vs. issues found).\n"
            "   - Clear YES/NO: 'Safe to leave now.'\n\n"
            "Be concise — this check should read in under 30 seconds."
        )
    ]
