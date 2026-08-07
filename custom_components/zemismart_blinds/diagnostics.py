"""Diagnostics support for Zemismart Blinds."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BASE_DOWN,
    CONF_BASE_STOP,
    CONF_BASE_TRAILER,
    CONF_BASE_UP,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .models import DomainRuntime

# This repository is public and diagnostics downloads get pasted into issues.
# The RF identity IS the security boundary of a 433 MHz remote: anyone holding
# prefix + remote id + calibration bases can synthesize frames the motors obey.
_REDACT_ENTRY = {
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_BASE_UP,
    CONF_BASE_DOWN,
    CONF_BASE_STOP,
    CONF_BASE_TRAILER,
}
# Attributes each cover contributes to the per-cover section. `remote` is the
# remote_key (f"{prefix:06x}:{remote_id:02x}") and is replaced by a dump-local
# label rather than dropped: covers of one remote must stay correlatable inside
# a single dump. See _Pseudonyms for why a digest is not good enough.
_COVER_ATTRIBUTES = (
    "channels",
    # Aggregates only, and worth a dump line of its own: a group deriving state
    # from fewer channels than it addresses is the first thing to check when a
    # reported position disagrees with the room.
    "unmodelled_channels",
    "role",
    "position_confidence",
    "position_suspect",
    "last_bridge",
    "degraded_bridge",
    "motion_direction",
    "motion_target",
    "motion_started",
    "motion_deadline",
    "motion_start_position",
    "motion_bridge",
    "motion_command_id",
    "motion_timed",
    "motion_absolute_anchor",
    "unverified_anchor_bridge",
    "unverified_anchor_command_id",
    "unverified_anchor_offline",
)


class _Pseudonyms:
    """Allocate dump-local stand-ins for remote identities.

    A digest of the identity is NOT redaction here. `remote_key` is
    `f"{prefix:06x}:{remote_id:02x}"` -- 32 bits at most, and virtual remotes
    are minted from a 24-bit space (see `new_virtual_remote_identity`). Any
    deterministic function of it is recovered by exhaustive search in seconds,
    and being deterministic it also correlates the same remote across unrelated
    public dumps. The first version of this file hashed with SHA-256 and was
    reversed in about seven seconds of plain Python during review.

    Numbering restarts every call, so a label means nothing outside the dump it
    came from -- which is all the correlation a bug report needs.
    """

    def __init__(self) -> None:
        """Start an empty, dump-local mapping."""
        self._labels: dict[str, str] = {}

    def of(self, remote_key: str) -> str:
        """Return this dump's label for one remote identity."""
        if remote_key not in self._labels:
            self._labels[remote_key] = f"remote-{len(self._labels) + 1}"
        return self._labels[remote_key]


async def async_get_config_entry_diagnostics[RuntimeT](
    hass: HomeAssistant,
    entry: ConfigEntry[RuntimeT],
) -> dict[str, object]:
    """Return the model state a bug report needs, with no RF identity in it."""
    runtime = cast("DomainRuntime", hass.data[DOMAIN])
    hub = runtime.hub

    registry = er.async_get(hass)
    pseudonyms = _Pseudonyms()
    covers: list[dict[str, object]] = []
    for row in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(row.entity_id)
        if state is None:
            covers.append({"entity_id": row.entity_id, "state": None})
            continue
        attributes = state.attributes
        remote_key = attributes.get("remote")
        cover: dict[str, object] = {
            "entity_id": row.entity_id,
            "state": state.state,
            "current_position": attributes.get("current_position"),
            "remote": pseudonyms.of(str(remote_key)) if remote_key is not None else None,
        }
        cover.update(
            {name: attributes.get(name) for name in _COVER_ATTRIBUTES if name in attributes}
        )
        covers.append(cover)

    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "source": entry.source,
            "data": async_redact_data(entry.data, _REDACT_ENTRY),
            "options": async_redact_data(entry.options, _REDACT_ENTRY),
        },
        "bridges": [
            {
                "bridge_id": bridge.bridge_id,
                "area_id": bridge.area_id,
                "online": bridge.online,
                "availability_seen": bridge.availability_seen,
                "is_default": bridge.is_default,
                "boot": bridge.boot,
                "listen": bridge.listen,
                "contract_v": bridge.contract_v,
            }
            for bridge in hub.registry.bridges
        ],
        "covers": covers,
        "hub": {
            **hub.diagnostics_snapshot(),
            "has_pending_disarms": hub.has_pending_disarms,
            "displaced_listeners": len(hub.displaced_listeners),
            "emission_proof_listeners": len(hub.emission_proof_listeners),
            "bridge_listeners": len(hub.bridge_listeners),
        },
        "air_arbitration": hub.air_shadow_stats(),
    }
