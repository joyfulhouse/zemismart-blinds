"""Tests for privacy-bounded config-entry diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.zemismart_blinds.const import DOMAIN
from custom_components.zemismart_blinds.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.zemismart_blinds.models import (
    BridgeRegistry,
    DomainRuntime,
    ZemismartHub,
)
from tests.test_init import config_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.mark.asyncio
async def test_diagnostics_returns_only_domain_global_air_stats(
    hass: HomeAssistant,
) -> None:
    """Diagnostics expose counters without entries, targets, IDs, or frames."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    hass.data[DOMAIN] = DomainRuntime(hub=hub, unsubscribers=[])
    result = await async_get_config_entry_diagnostics(hass, config_entry("diagnostics"))

    assert set(result) == {"air_arbitration"}
    air = result["air_arbitration"]
    assert isinstance(air, dict)
    assert air["mode"] == "enforce"
    assert "command_id" not in repr(result)
    assert "raw" not in repr(result)
