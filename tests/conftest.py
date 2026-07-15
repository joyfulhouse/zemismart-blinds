"""Real Home Assistant fixtures for integration paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
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


@pytest.fixture(autouse=True)
def _mqtt_client_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the MQTT client as ready; integration tests stub the transport."""
    from homeassistant.components import mqtt

    async def ready(_hass: object) -> bool:
        return True

    monkeypatch.setattr(mqtt, "async_wait_for_mqtt_client", ready, raising=False)
