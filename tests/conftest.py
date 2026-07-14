"""Real Home Assistant fixtures for integration paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest_asyncio.fixture
async def hass(tmp_path: str) -> AsyncIterator[HomeAssistant]:
    """Run a minimal real Home Assistant core for entity and lifecycle tests."""
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    await instance.async_start()
    try:
        yield instance
    finally:
        await instance.async_stop(force=True)
