"""Shared pytest fixtures for MCP tests."""

from __future__ import annotations

import asyncio
from typing import Iterator

import pytest


@pytest.fixture()
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Provide an asyncio event loop for tests (required by pytest_homeassistant_custom_component)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
