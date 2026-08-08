"""Bridge discovery registry for Zemismart Blinds."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

# The legacy namespace is retained deliberately by the pure-move contract.
_LOGGER = logging.getLogger("custom_components.zemismart_blinds.models")

_BRIDGE_MAX_ID_LENGTH: Final = 64
_BRIDGE_MAX_ENTRIES: Final = 256


class NoOnlineBridgeError(RuntimeError):
    """Raised when no discovered bridge is currently online."""


@dataclass(frozen=True, slots=True)
class BridgeInfo:
    """Retained discovery state for one ESPHome bridge beacon."""

    bridge_id: str
    area_id: str | None = None
    online: bool = False
    is_default: bool = False
    # Whether an availability payload has ever been applied: online=False
    # without it only means "not discovered yet", not "reported offline".
    availability_seen: bool = False
    boot: int | None = None
    listen: bool | None = None
    contract_v: int | None = None


class BridgeRegistry:
    """Track retained bridge availability/info and resolve one TX target."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._bridges: dict[str, BridgeInfo] = {}

    @property
    def bridges(self) -> tuple[BridgeInfo, ...]:
        """Return a stable snapshot ordered by bridge id."""
        return tuple(self._bridges[key] for key in sorted(self._bridges))

    def _update_target(self, bridge_id: str) -> tuple[str, BridgeInfo] | None:
        """Normalize one bounded wildcard id and return its current state."""
        bridge_id = bridge_id.strip()
        if not bridge_id or len(bridge_id) > _BRIDGE_MAX_ID_LENGTH:
            return None
        current = self._bridges.get(bridge_id)
        if current is None:
            if len(self._bridges) >= _BRIDGE_MAX_ENTRIES:
                return None
            current = BridgeInfo(bridge_id)
        return bridge_id, current

    def _store(self, bridge: BridgeInfo) -> None:
        """Store meaningful retained state, pruning a complete withdrawal."""
        if (
            bridge.area_id is None
            and not bridge.online
            and not bridge.is_default
            and not bridge.availability_seen
            and bridge.boot is None
            and bridge.listen is None
            and bridge.contract_v is None
        ):
            self._bridges.pop(bridge.bridge_id, None)
            return
        self._bridges[bridge.bridge_id] = bridge

    def update_availability(self, bridge_id: str, payload: str) -> None:
        """Apply a retained LWT availability message."""
        target = self._update_target(bridge_id)
        if target is None:
            return
        bridge_id, current = target
        normalized = payload.strip().lower()
        # An empty payload is a retained-topic deletion, not an explicit
        # offline report: all availability knowledge for the bridge is gone.
        availability_seen = bool(normalized)
        online = normalized == "online"
        if online != current.online:
            _LOGGER.debug("Bridge %s is now %s", bridge_id, "online" if online else "offline")
        self._store(
            replace(
                current,
                online=online,
                availability_seen=availability_seen,
            )
        )

    def update_info(self, bridge_id: str, payload: Mapping[str, object]) -> None:
        """Apply retained bridge metadata, including its HA area tag."""
        target = self._update_target(bridge_id)
        if target is None:
            return
        bridge_id, current = target
        # Retained info is the COMPLETE metadata document: missing keys (or
        # an emptied retained topic, delivered here as an empty mapping)
        # clear the fields rather than preserving stale area/default values.
        raw_area = payload.get("area_id", payload.get("area"))
        area_id = str(raw_area).strip() if raw_area is not None else None
        if not area_id:
            area_id = None
        raw_default = payload.get("default", False)
        is_default = (
            raw_default.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(raw_default, str)
            else bool(raw_default)
        )
        raw_boot = payload.get("boot")
        boot = raw_boot if isinstance(raw_boot, int) and not isinstance(raw_boot, bool) else None
        raw_listen = payload.get("listen")
        listen = raw_listen if isinstance(raw_listen, bool) else None
        raw_contract_v = payload.get("v")
        contract_v = (
            raw_contract_v
            if isinstance(raw_contract_v, int) and not isinstance(raw_contract_v, bool)
            else None
        )
        self._store(
            replace(
                current,
                area_id=area_id,
                is_default=is_default,
                boot=boot,
                listen=listen,
                contract_v=contract_v,
            )
        )

    def resolve(self, area_id: str) -> BridgeInfo:
        """Choose one online bridge: same area, default, then deterministic fallback."""
        online = [bridge for bridge in self.bridges if bridge.online]
        same_area = [bridge for bridge in online if bridge.area_id == area_id]
        if same_area:
            return same_area[0]
        defaults = [bridge for bridge in online if bridge.is_default]
        if defaults:
            return defaults[0]
        if online:
            return online[0]
        msg = "no RF433 bridge is online"
        raise NoOnlineBridgeError(msg)

    def online_bridge_ids(self) -> tuple[str, ...]:
        """Return every online bridge, for a fleet-wide listening session.

        A single bridge hears a remote only ~30% of the time while its peers
        hear nearly every press (#57), so capture flows listen on all of
        them; TX still resolves exactly one bridge through ``resolve``.

        Sorted here rather than only by ``bridges``, whose snapshot is already
        ordered: the set is joined into the screens that name which bridges are
        listening, and a text that reorders itself between two visits to the
        same form reads as a changed fleet, so the order is this method's own
        promise rather than a property inherited from how it happens to read.
        """
        online = tuple(sorted(bridge.bridge_id for bridge in self.bridges if bridge.online))
        if not online:
            msg = "no RF433 bridge is online"
            raise NoOnlineBridgeError(msg)
        return online

    def is_known_offline(self, bridge_id: str) -> bool:
        """Return whether this bridge has EXPLICITLY reported itself offline.

        A bridge that has never announced availability (registry empty at
        startup, or only retained info seen so far) is unknown, not offline;
        conflating the two would irreversibly invalidate restored motion on
        every restart.
        """
        bridge = self._bridges.get(bridge_id)
        return bridge is not None and bridge.availability_seen and not bridge.online

    def online_bridge(self, bridge_id: str) -> BridgeInfo:
        """Resolve a specific online bridge for the debug raw service."""
        bridge = self._bridges.get(bridge_id)
        if bridge is None or not bridge.online:
            msg = f"RF433 bridge {bridge_id!r} is not online"
            raise NoOnlineBridgeError(msg)
        return bridge
