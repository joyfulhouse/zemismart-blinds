"""Per-remote coordination between leaf covers and their aggregates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback

from .models import CoverConfig, Role, derive_role, member_covers

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import Event, EventStateChangedData, HomeAssistant


class MemberCover(Protocol):
    """The leaf-entity surface the coordinator and aggregates rely on."""

    entity_id: str

    @callback
    def async_write_ha_state(self) -> None:
        """Schedule one HA state write."""


class AggregateCover(Protocol):
    """The aggregate-entity surface the coordinator flushes."""

    @callback
    def async_write_ha_state(self) -> None:
        """Schedule one HA state write."""


class RemoteCoordinator:
    """Track one remote's cover topology and batch member→aggregate updates.

    Purely in-process plumbing: no storage, no MQTT surface. Membership is
    recomputed only on entry reload (the coordinator is rebuilt with the
    platform), matching the spec's reload-driven topology.
    """

    def __init__(self, hass: HomeAssistant, covers: Mapping[str, CoverConfig]) -> None:
        """Derive roles and leaves-only membership from entry-data covers."""
        self._hass = hass
        self.covers: dict[str, CoverConfig] = dict(covers)
        family = list(self.covers.values())
        self.roles: dict[str, Role] = {
            cover_id: derive_role(cover, family) for cover_id, cover in self.covers.items()
        }
        key_to_id = {cover.channel_key: cover_id for cover_id, cover in self.covers.items()}
        self.members: dict[str, tuple[str, ...]] = {
            cover_id: tuple(
                key_to_id[member.channel_key] for member in member_covers(cover, family)
            )
            for cover_id, cover in self.covers.items()
            if self.roles[cover_id] is Role.AGGREGATE
        }
        # Reverse index: leaf cover id -> aggregate cover ids containing it.
        self._containers: dict[str, tuple[str, ...]] = {}
        for aggregate_id, member_ids in self.members.items():
            for member_id in member_ids:
                self._containers[member_id] = (
                    *self._containers.get(member_id, ()),
                    aggregate_id,
                )
        self._leaf_entities: dict[str, MemberCover] = {}
        self._aggregate_entities: dict[str, AggregateCover] = {}
        self._entity_cover_ids: dict[str, str] = {}
        self._dirty: set[str] = set()
        self._flush_scheduled = False
        # Entity.async_write_ha_state is final, so member mutations are
        # observed through the state machine instead of an entity override:
        # every leaf write lands here exactly once, whatever triggered it.
        self._unsub_state_changed = hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            self._on_state_changed,
        )

    @callback
    def detach(self) -> None:
        """Stop listening when the owning entry unloads."""
        self._unsub_state_changed()
        self._leaf_entities.clear()
        self._aggregate_entities.clear()
        self._entity_cover_ids.clear()
        self._dirty.clear()

    @callback
    def _on_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Re-derive aggregates when one of their members wrote state."""
        cover_id = self._entity_cover_ids.get(event.data["entity_id"])
        if cover_id is not None:
            self.member_changed(cover_id)

    @callback
    def register_leaf(self, cover_id: str, entity: MemberCover) -> None:
        """Register one live leaf entity for fan-out and derivation."""
        self._leaf_entities[cover_id] = entity
        self._entity_cover_ids[entity.entity_id] = cover_id
        self._mark_containers_dirty(cover_id)

    @callback
    def unregister_leaf(self, cover_id: str) -> None:
        """Drop one leaf entity; containing aggregates re-derive without it."""
        entity = self._leaf_entities.pop(cover_id, None)
        if entity is not None:
            self._entity_cover_ids.pop(entity.entity_id, None)
        self._mark_containers_dirty(cover_id)

    @callback
    def register_aggregate(self, cover_id: str, entity: AggregateCover) -> None:
        """Register one live aggregate entity for batched flushes."""
        self._aggregate_entities[cover_id] = entity

    @callback
    def unregister_aggregate(self, cover_id: str) -> None:
        """Drop one aggregate entity."""
        self._aggregate_entities.pop(cover_id, None)
        self._dirty.discard(cover_id)

    def members_of(self, aggregate_id: str) -> tuple[MemberCover, ...]:
        """Return the live leaf entities inside one aggregate."""
        return tuple(
            self._leaf_entities[member_id]
            for member_id in self.members.get(aggregate_id, ())
            if member_id in self._leaf_entities
        )

    @callback
    def member_changed(self, cover_id: str) -> None:
        """Mark aggregates containing this leaf dirty; flush once per iteration.

        Every member-model mutation funnels through the leaf's state write, so
        one RX event touching several members coalesces into a single state
        write per aggregate instead of intermediate partial recomputations.
        """
        self._mark_containers_dirty(cover_id)

    @callback
    def _mark_containers_dirty(self, cover_id: str) -> None:
        containers = self._containers.get(cover_id)
        if not containers:
            return
        self._dirty.update(containers)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            self._hass.loop.call_soon(self._flush)

    @callback
    def _flush(self) -> None:
        """Write every dirty aggregate's derived state exactly once."""
        self._flush_scheduled = False
        dirty, self._dirty = self._dirty, set()
        for aggregate_id in dirty:
            entity = self._aggregate_entities.get(aggregate_id)
            if entity is not None:
                entity.async_write_ha_state()
