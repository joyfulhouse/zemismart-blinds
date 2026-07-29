"""Per-remote coordination between leaf covers and their aggregates."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, cast

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .models import CoverConfig, Role, derive_role, member_covers

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import (
        CALLBACK_TYPE,
        Event,
        EventStateChangedData,
        HomeAssistant,
    )


_POSITION_INVALIDATIONS_DATA_KEY: Final = f"{DOMAIN}_position_invalidations"
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _PositionInvalidationState:
    """Share current leaf topology and outstanding markers across reloads."""

    remote_key: str
    valid_leaf_channels: dict[str, frozenset[int]]
    coordinator: RemoteCoordinator | None = None
    invalidated: dict[str, int] = field(default_factory=dict)
    generation: int = 0


class MemberCover(Protocol):
    """The leaf-entity surface the coordinator and aggregates rely on."""

    entity_id: str

    @callback
    def async_write_ha_state(self) -> None:
        """Schedule one HA state write."""

    @callback
    def invalidate_for_cancelled_command(self) -> None:
        """Discard position after a command with an unknowable outcome."""


class AggregateCover(Protocol):
    """The aggregate-entity surface the coordinator flushes."""

    @callback
    def async_write_ha_state(self) -> None:
        """Schedule one HA state write."""


class RemoteCoordinator:
    """Track one remote's cover topology and batch member→aggregate updates.

    Membership is recomputed only on entry reload (the coordinator is rebuilt
    with the platform), matching the spec's reload-driven topology. Failed
    group-frame invalidations live separately in ``hass.data`` so they survive
    that rebuild and can be consumed by replacement leaf entities.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        covers: Mapping[str, CoverConfig],
        remote_entry_id: str,
        remote_key: str,
    ) -> None:
        """Derive roles and leaves-only membership from entry-data covers."""
        self._hass = hass
        self._remote_key = remote_key
        self._remote_entry_id = remote_entry_id
        self.covers: dict[str, CoverConfig] = dict(covers)
        family = list(self.covers.values())
        self.roles: dict[str, Role] = {
            cover_id: derive_role(cover, family) for cover_id, cover in self.covers.items()
        }
        self._attach_position_invalidations(remote_entry_id)
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
        # The subscription is SCOPED to the registered leaf entity ids, through
        # HA's indexed per-entity dispatcher: an unfiltered EVENT_STATE_CHANGED
        # listener made every coordinator in the instance — one per config
        # entry, ~16 on this fleet — do a dict lookup for every state change
        # anywhere, with moving covers themselves among the loudest producers.
        # The entity ids are not known here, so it is (re)installed from
        # register_leaf/unregister_leaf instead of from the constructor.
        self._unsub_state_changed: CALLBACK_TYPE | None = None

    @callback
    def detach(self) -> None:
        """Stop listening when the owning entry unloads."""
        state = self._position_invalidation_state(self._remote_entry_id)
        if state is not None and state.coordinator is self:
            state.coordinator = None
        if self._unsub_state_changed is not None:
            self._unsub_state_changed()
            self._unsub_state_changed = None
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
    def _resubscribe(self) -> None:
        """Point the state-change subscription at exactly the live leaves.

        The new subscription is installed before the old one is released, so
        no leaf write can fall between them.
        """
        previous = self._unsub_state_changed
        self._unsub_state_changed = (
            async_track_state_change_event(
                self._hass,
                list(self._entity_cover_ids),
                self._on_state_changed,
            )
            if self._entity_cover_ids
            else None
        )
        if previous is not None:
            previous()

    @callback
    def register_leaf(self, cover_id: str, entity: MemberCover) -> None:
        """Register one live leaf entity for fan-out and derivation."""
        self._leaf_entities[cover_id] = entity
        self._entity_cover_ids[entity.entity_id] = cover_id
        self._resubscribe()
        self._mark_containers_dirty(cover_id)

    @callback
    def unregister_leaf(self, cover_id: str) -> None:
        """Drop one leaf entity; containing aggregates re-derive without it."""
        entity = self._leaf_entities.pop(cover_id, None)
        if entity is not None:
            self._entity_cover_ids.pop(entity.entity_id, None)
            self._resubscribe()
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

    def _position_invalidation_states(self) -> dict[str, _PositionInvalidationState]:
        """Return shared marker and current-topology state by config entry."""
        return cast(
            "dict[str, _PositionInvalidationState]",
            self._hass.data.setdefault(_POSITION_INVALIDATIONS_DATA_KEY, {}),
        )

    def _position_invalidation_state(
        self,
        remote_entry_id: str,
    ) -> _PositionInvalidationState | None:
        """Return shared marker state for one config entry, if attached."""
        stored = cast(
            "dict[str, _PositionInvalidationState] | None",
            self._hass.data.get(_POSITION_INVALIDATIONS_DATA_KEY),
        )
        return stored.get(remote_entry_id) if stored is not None else None

    @callback
    def record_position_invalidations(
        self,
        remote_entry_id: str,
        addressed_channels: tuple[int, ...],
    ) -> tuple[MemberCover, ...]:
        """Tombstone affected leaves and invalidate those attached right now."""
        addressed = frozenset(addressed_channels)
        state = self._position_invalidation_states().get(remote_entry_id)
        if state is None or state.remote_key != self._remote_key:
            return ()
        invalidated_cover_ids = tuple(
            cover_id
            for cover_id, channels in state.valid_leaf_channels.items()
            if not addressed.isdisjoint(channels)
        )
        if not invalidated_cover_ids:
            return ()
        state.generation += 1
        for cover_id in invalidated_cover_ids:
            state.invalidated[cover_id] = state.generation
        current = state.coordinator
        if current is None:
            return ()
        live_entities = tuple(
            entity
            for cover_id in invalidated_cover_ids
            if (entity := current._leaf_entities.get(cover_id)) is not None
        )
        for entity in live_entities:
            try:
                entity.invalidate_for_cancelled_command()
            except Exception:
                # Every marker was recorded before live dispatch. A failed
                # state write therefore leaves this entity's marker intact for
                # restore while later entities and aggregate fallback continue.
                _LOGGER.exception(
                    "Failed to invalidate position for %s",
                    entity.entity_id,
                )
        return live_entities

    @callback
    def has_position_invalidation(self, remote_entry_id: str, cover_id: str) -> bool:
        """Return whether a replacement leaf must ignore restored position."""
        state = self._position_invalidation_state(remote_entry_id)
        return state is not None and cover_id in state.invalidated

    @callback
    def position_invalidation_generation(
        self,
        remote_entry_id: str,
        cover_id: str,
    ) -> int | None:
        """Return the current marker generation for one configured leaf."""
        state = self._position_invalidation_state(remote_entry_id)
        return state.invalidated.get(cover_id) if state is not None else None

    @callback
    def _attach_position_invalidations(self, remote_entry_id: str) -> None:
        """Publish current leaf identities and prune obsolete markers."""
        valid_leaf_channels = {
            cover_id: frozenset(self.covers[cover_id].channels)
            for cover_id, role in self.roles.items()
            if role is Role.LEAF
        }
        states = self._position_invalidation_states()
        if (state := states.get(remote_entry_id)) is None:
            states[remote_entry_id] = _PositionInvalidationState(
                self._remote_key,
                valid_leaf_channels,
                coordinator=self,
            )
            return
        if state.remote_key != self._remote_key:
            state.invalidated.clear()
        else:
            state.invalidated = {
                cover_id: generation
                for cover_id, generation in state.invalidated.items()
                if cover_id in valid_leaf_channels
                and state.valid_leaf_channels.get(cover_id) == valid_leaf_channels[cover_id]
            }
        state.remote_key = self._remote_key
        state.valid_leaf_channels = valid_leaf_channels
        state.coordinator = self

    @callback
    def clear_position_invalidation(
        self,
        remote_entry_id: str,
        cover_id: str,
        generation: int,
    ) -> None:
        """Clear only the marker generation consumed before a state write."""
        state = self._position_invalidation_state(remote_entry_id)
        if state is None:
            return
        if state.invalidated.get(cover_id) == generation:
            state.invalidated.pop(cover_id)

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
