"""Regression tests: blocking tool bodies must never run on the event loop.

Source: code analysis 2026-06-10 — 25 sync @mcp.tool functions issued blocking
requests calls directly on the event-loop thread. FastMCP (mcp 1.27.2,
func_metadata.py `return fn(**arguments_parsed_dict)`) executes sync tools
inline, so one slow Bosch cloud call froze the whole server for every parallel
client. Fix: `_blocking_tool` registers an async wrapper that offloads via
asyncio.to_thread; async tools offload `_get_session` / cloud-write sections.
"""

from __future__ import annotations

import inspect
import threading

import pytest

from bosch_camera_mcp import server
from bosch_camera_mcp.errors import MCPError
from bosch_camera_mcp.server import mcp


class TestAllToolsAsync:
    async def test_every_registered_tool_is_async(self) -> None:
        """No registered tool may execute its body on the event-loop thread."""
        tools = mcp._tool_manager.list_tools()
        assert len(tools) == 70
        sync_tools = [t.name for t in tools if not t.is_async]
        assert sync_tools == [], (
            f"sync tools would block the event loop (FastMCP runs them inline): {sync_tools}"
        )

    def test_module_level_functions_stay_sync(self) -> None:
        """_blocking_tool must return the original sync fn for direct in-process calls."""
        for name in ("bosch_camera_list", "bosch_camera_status", "bosch_camera_token_status"):
            fn = getattr(server, name)
            assert callable(fn)
            assert not inspect.iscoroutinefunction(fn), f"{name} should stay a plain sync fn"


class TestOffloadExecution:
    async def test_blocking_tool_body_runs_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling a wrapped tool through MCP must execute the body in a worker thread."""
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}

        def _fake_get_session(config_path: str | None = None) -> tuple[dict, object, dict]:
            seen["thread"] = threading.get_ident()
            raise MCPError(code="probe", detail="stop here — thread recorded")

        monkeypatch.setattr(server, "_get_session", _fake_get_session)

        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await mcp.call_tool("bosch_camera_list", {})

        assert "thread" in seen, "tool body never ran"
        assert seen["thread"] != loop_thread, (
            "blocking tool body executed on the event-loop thread — offload regressed"
        )

    async def test_offload_serializes_via_shared_lock(self) -> None:
        """_locked must serialize bodies — the shared requests.Session is not thread-safe."""
        order: list[str] = []
        gate = threading.Event()

        def first() -> None:
            order.append("first-start")
            gate.wait(timeout=5)
            order.append("first-end")

        def second() -> None:
            order.append("second")

        import asyncio

        t1 = asyncio.create_task(asyncio.to_thread(server._locked, first))
        await asyncio.sleep(0.1)  # first holds the lock now
        t2 = asyncio.create_task(asyncio.to_thread(server._locked, second))
        await asyncio.sleep(0.1)
        gate.set()
        await asyncio.gather(t1, t2)

        assert order == ["first-start", "first-end", "second"]


class TestSchemaPreserved:
    async def test_wrapper_preserves_name_doc_and_parameters(self) -> None:
        """functools.wraps + __wrapped__: published schema must match the sync signature."""
        tools = {t.name: t for t in await mcp.list_tools()}

        status = tools["bosch_camera_status"]
        assert "camera" in status.inputSchema.get("properties", {})
        assert "camera" in status.inputSchema.get("required", [])
        assert (status.description or "").strip() != ""

        events = tools["bosch_camera_events"]
        props = events.inputSchema.get("properties", {})
        assert "camera" in props and "limit" in props
        assert props["limit"].get("default") == 10
