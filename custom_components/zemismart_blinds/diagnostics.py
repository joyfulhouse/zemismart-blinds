"""Diagnostics support for Zemismart Blinds."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .models import DomainRuntime


async def async_get_config_entry_diagnostics[RuntimeT](
    hass: HomeAssistant,
    entry: ConfigEntry[RuntimeT],
) -> dict[str, object]:
    """Return domain-global air arbitration counters without RF data."""
    del entry
    runtime = cast("DomainRuntime", hass.data[DOMAIN])
    return {"air_arbitration": runtime.hub.air_shadow_stats()}
