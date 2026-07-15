"""RF-start-gated travel-time cover entities for Zemismart blinds and groups."""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    FULL_TRAVEL_MARGIN_SECONDS,
    POSITION_UPDATE_INTERVAL_SECONDS,
)
from .models import (
    BlindConfig,
    Button,
    CommandAck,
    CommandAckTimeoutError,
    CommandStartedTimeoutError,
    EntryRuntime,
    ZemismartHub,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_ATTR_DEGRADED = "degraded_bridge"
_ATTR_LAST_BRIDGE = "last_bridge"
_ATTR_MOTION_BRIDGE = "motion_bridge"
_ATTR_MOTION_COMMAND_ID = "motion_command_id"
_ATTR_MOTION_DEADLINE = "motion_deadline"
_ATTR_MOTION_DIRECTION = "motion_direction"
_ATTR_MOTION_STARTED = "motion_started"
_ATTR_MOTION_START_POSITION = "motion_start_position"
_ATTR_MOTION_TARGET = "motion_target"
_ATTR_MOTION_TIMED = "motion_timed"
_ATTR_UNVERIFIED_ANCHOR = "unverified_anchor_bridge"
WALL_CLOCK = time.time
_COVERS: weakref.WeakKeyDictionary[ZemismartHub, weakref.WeakSet[ZemismartCover]] = (
    weakref.WeakKeyDictionary()
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[EntryRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create exactly one cover entity for this blind/group entry."""
    del hass
    async_add_entities([ZemismartCover(entry.entry_id, entry.runtime_data)])


def _number(value: object) -> float | None:
    """Return a JSON number without treating booleans as positions."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class ZemismartCover(CoverEntity, RestoreEntity):
    """An assumed-state cover committed only after first RF dispatch."""

    _attr_assumed_state = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, entry_id: str, runtime: EntryRuntime) -> None:
        """Initialize one cover with its own travel-time estimate."""
        self._config: BlindConfig = runtime.config
        self._hub = runtime.hub
        self._entry_id = entry_id
        self._attr_unique_id = entry_id
        self._position: float | None = None
        self._direction = 0
        self._motion_started = 0.0
        self._motion_start_position: float | None = None
        self._motion_target: float | None = None
        self._motion_duration = 0.0
        self._motion_deadline = 0.0
        self._motion_bridge: str | None = None
        self._motion_command_id: str | None = None
        self._motion_timed = False
        self._motion_absolute_anchor = False
        self._unverified_anchor_bridge: str | None = None
        self._motion_token: object | None = None
        self._motion_task: asyncio.Task[None] | None = None
        self._last_bridge: str | None = None
        self._degraded = False
        # Serializes this entity's own commands: without it, a set_position
        # racing an unstarted open/close computes travel from a stale
        # estimate and physically overshoots.
        self._command_lock = asyncio.Lock()

    @property
    def device_info(self) -> DeviceInfo:
        """Represent each blind/group entry as its own area-assignable HA device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._config.name,
            manufacturer="Zemismart",
            model="433 MHz blind group" if self._config.is_group else "433 MHz blind",
        )

    @property
    def available(self) -> bool:
        """Reflect whether any RF bridge is currently online."""
        return any(bridge.online for bridge in self._hub.registry.bridges)

    @property
    def current_cover_position(self) -> int | None:
        """Return the current estimate without integrating or mutating it."""
        return round(self._position) if self._position is not None else None

    @property
    def is_opening(self) -> bool:
        """Return whether started elapsed-time integration is moving upward."""
        return self._direction > 0

    @property
    def is_closing(self) -> bool:
        """Return whether started elapsed-time integration is moving downward."""
        return self._direction < 0

    @property
    def is_closed(self) -> bool | None:
        """Return whether the estimate is anchored closed, or unknown."""
        position = self.current_cover_position
        return position == 0 if position is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose routing plus restart-safe started motion metadata."""
        return {
            "channels": list(self._config.channels),
            "remote": self._config.remote_key,
            _ATTR_LAST_BRIDGE: self._last_bridge,
            _ATTR_DEGRADED: self._degraded,
            _ATTR_MOTION_DIRECTION: self._direction,
            _ATTR_MOTION_TARGET: self._motion_target,
            _ATTR_MOTION_STARTED: self._motion_started or None,
            _ATTR_MOTION_DEADLINE: self._motion_deadline or None,
            _ATTR_MOTION_START_POSITION: self._motion_start_position,
            _ATTR_MOTION_BRIDGE: self._motion_bridge,
            _ATTR_MOTION_COMMAND_ID: self._motion_command_id,
            _ATTR_MOTION_TIMED: self._motion_timed,
            _ATTR_UNVERIFIED_ANCHOR: self._unverified_anchor_bridge,
        }

    async def async_added_to_hass(self) -> None:
        """Restore a stopped estimate or reconstruct complete started motion."""
        await super().async_added_to_hass()
        _COVERS.setdefault(self._hub, weakref.WeakSet()).add(self)
        self._hub.displaced_listeners.append(self._on_displaced)
        self._hub.bridge_listeners.append(self._on_bridge_change)
        state = await self.async_get_last_state()
        if state is None:
            return
        if state.attributes.get("remote") != self._config.remote_key or state.attributes.get(
            "channels"
        ) != list(self._config.channels):
            # The entry was re-pointed at different hardware (remote or
            # channel set changed in options): the persisted position and
            # motion describe the OLD physical target and must not be
            # assigned to the new one.
            return
        restored = _number(state.attributes.get(ATTR_CURRENT_POSITION))
        if restored is not None and 0 <= restored <= 100:
            self._position = restored
        self._last_bridge = self._optional_text(state.attributes.get(_ATTR_LAST_BRIDGE))
        self._degraded = bool(state.attributes.get(_ATTR_DEGRADED, False))
        # A questioned restore anchor survives repeated restarts: without
        # this, a second restart before the anchor bridge's availability
        # arrives would silently promote the unverified target to trusted.
        self._unverified_anchor_bridge = self._optional_text(
            state.attributes.get(_ATTR_UNVERIFIED_ANCHOR)
        )

        raw_direction = state.attributes.get(_ATTR_MOTION_DIRECTION, 0)
        direction = (
            raw_direction
            if isinstance(raw_direction, int) and not isinstance(raw_direction, bool)
            else 0
        )
        if direction not in {-1, 1}:
            if state.state in {"opening", "closing"}:
                self._mark_unknown()
            return

        target = _number(state.attributes.get(_ATTR_MOTION_TARGET))
        deadline = _number(state.attributes.get(_ATTR_MOTION_DEADLINE))
        bridge = self._optional_text(state.attributes.get(_ATTR_MOTION_BRIDGE))
        command_id = self._optional_text(state.attributes.get(_ATTR_MOTION_COMMAND_ID))
        if (
            target is None
            or not 0 <= target <= 100
            or deadline is None
            or deadline <= 0
            or bridge is None
            or command_id is None
        ):
            self._mark_unknown()
            return

        self._last_bridge = bridge
        timed = bool(state.attributes.get(_ATTR_MOTION_TIMED, False))
        if timed and self._hub.registry.is_known_offline(bridge):
            # The restored motion depends on a bridge-armed fail-safe STOP,
            # and that bridge has explicitly reported itself offline: its
            # RAM-only scheduler state (and the STOP) may be gone, whether
            # the deadline has passed or not. A bridge merely not discovered
            # yet is NOT treated as offline — later drops are caught by
            # _on_bridge_change via the restored _motion_timed flag.
            self._mark_unknown()
            return
        now = WALL_CLOCK()
        if now >= deadline:
            self._position = target
            self._clear_motion()
            if timed and not self._bridge_seen_online(bridge):
                # The anchored target assumed the bridge's armed STOP fired
                # while HA was down, but retained availability has not
                # arrived yet on this cold start. Remember the bridge: if it
                # later reports offline, the STOP may never have fired and
                # the anchor is invalidated. (Not persisted — a second
                # restart before any availability arrives loses the marker.)
                self._unverified_anchor_bridge = bridge
            return
        # Prefer the persisted motion origin: interpolating from the original
        # start keeps the transient estimate accurate across the restart gap
        # instead of restarting the ramp from the last stored snapshot.
        started = _number(state.attributes.get(_ATTR_MOTION_STARTED))
        start_position = _number(state.attributes.get(_ATTR_MOTION_START_POSITION))
        if (
            started is not None
            and start_position is not None
            and 0 <= start_position <= 100
            and started < deadline
            and started <= now
        ):
            self._motion_started = started
            self._motion_start_position = start_position
        else:
            self._motion_started = now
            self._motion_start_position = self._position
        self._direction = direction
        self._motion_target = target
        self._motion_deadline = deadline
        self._motion_duration = deadline - self._motion_started
        self._motion_bridge = bridge
        self._motion_command_id = command_id
        self._motion_timed = timed
        self._sync_position(now)
        self._create_motion_task("recovered travel")

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the local timer and unregister direct group notifications."""
        covers = _COVERS.get(self._hub)
        if covers is not None:
            covers.discard(self)
        if self._on_displaced in self._hub.displaced_listeners:
            self._hub.displaced_listeners.remove(self._on_displaced)
        if self._on_bridge_change in self._hub.bridge_listeners:
            self._hub.bridge_listeners.remove(self._on_bridge_change)
        self._cancel_motion_task()
        await super().async_will_remove_from_hass()

    def _on_displaced(self, bridge_id: str, command_id: str) -> None:
        """React when the bridge displaced this cover's active command.

        Only a TIMED motion is frozen: its flushed fail-safe STOP physically
        lands within the next pacing gaps, so the current estimate is within
        one gap of truth. A displaced full travel keeps running to its
        endpoint on the motor's own limit switch — the model rides to its
        target, and channels re-driven by the displacing command get a fresh
        model from that command's own cover.
        """
        del bridge_id
        if not command_id or command_id != self._motion_command_id:
            return
        if self._motion_timed:
            self._interrupt_motion(WALL_CLOCK())
            self.async_write_ha_state()

    def _bridge_seen_online(self, bridge_id: str) -> bool:
        """Return whether this bridge has explicitly announced itself online."""
        return any(
            bridge.online for bridge in self._hub.registry.bridges if bridge.bridge_id == bridge_id
        )

    def _on_bridge_change(self) -> None:
        """Re-evaluate availability and timed-motion safety on bridge changes."""
        if self._unverified_anchor_bridge is not None:
            anchor_bridge = self._unverified_anchor_bridge
            if self._hub.registry.is_known_offline(anchor_bridge):
                # The restore-time anchor trusted a STOP this bridge never
                # got to fire: the retained availability that just arrived
                # says it was offline. The motor may sit at its hard limit.
                self._unverified_anchor_bridge = None
                if self._direction == 0:
                    # No live motion depends on the questioned position, so
                    # it is now known-bad. A currently RUNNING motion (e.g. a
                    # full OPEN commanded through another bridge) already owns
                    # the estimate and must NOT be cancelled here — it will
                    # settle a genuine anchor on completion or be superseded.
                    self._mark_unknown()
            elif self._bridge_seen_online(anchor_bridge):
                self._unverified_anchor_bridge = None
        if (
            self._direction != 0
            and self._motion_timed
            and self._motion_bridge is not None
            and self._hub.registry.is_known_offline(self._motion_bridge)
        ):
            # The bridge holding this motion's armed fail-safe STOP has
            # explicitly reported offline; its scheduler state is RAM-only,
            # so the STOP may be lost and the motor may run to its limit.
            # Only unknown is honest. (A bridge merely not discovered yet is
            # not offline — during startup, unrelated bridges announce first.)
            self._mark_unknown()
        self.async_write_ha_state()

    @staticmethod
    def _optional_text(value: object) -> str | None:
        """Normalize optional state attribute text."""
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    def _estimated_position(self, now: float) -> float | None:
        """Calculate motion progress without changing entity state."""
        if (
            self._direction == 0
            or self._motion_duration <= 0
            or self._motion_start_position is None
            or self._motion_target is None
        ):
            return self._position
        progress = min(1.0, max(0.0, (now - self._motion_started) / self._motion_duration))
        estimated = (
            self._motion_start_position
            + (self._motion_target - self._motion_start_position) * progress
        )
        # Hold just short of an endpoint until the model completes — but only
        # when actually traveling toward it; a member already sitting at its
        # endpoint must not blip to 99/1 while its group runs a full travel.
        if progress < 1.0 and self._motion_target == 100 and self._motion_start_position != 100:
            return min(99.0, estimated)
        if progress < 1.0 and self._motion_target == 0 and self._motion_start_position != 0:
            return max(1.0, estimated)
        return estimated

    def _sync_position(self, now: float | None = None) -> None:
        """Commit elapsed integration from a timer or started command path."""
        self._position = self._estimated_position(now if now is not None else WALL_CLOCK())

    def _cancel_motion_task(self) -> None:
        """Cancel the current completion task without changing model fields."""
        task = self._motion_task
        self._motion_token = None
        self._motion_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _clear_motion(self) -> None:
        """Clear completed or interrupted motion metadata."""
        self._direction = 0
        self._motion_started = 0.0
        self._motion_start_position = self._position
        self._motion_target = None
        self._motion_duration = 0.0
        self._motion_deadline = 0.0
        self._motion_bridge = None
        self._motion_command_id = None
        self._motion_timed = False
        self._motion_absolute_anchor = False

    def _interrupt_motion(self, at: float) -> None:
        """Freeze prior tracking only after the replacing command starts."""
        self._sync_position(at)
        self._cancel_motion_task()
        self._clear_motion()

    def _mark_unknown(self) -> None:
        """Discard ambiguous motion after a lifecycle timeout or recovery gap."""
        self._cancel_motion_task()
        self._position = None
        self._clear_motion()
        self._unverified_anchor_bridge = None
        self._degraded = True

    def _mark_unknown_and_notify_members(self) -> None:
        """Invalidate this target, its members, and overlapping aggregates.

        Used on ambiguous acknowledgement/start timeouts: the frame MAY have
        reached RF, so every cover whose channels it addresses — direct
        members and any overlapping group whose aggregate could now be stale
        — loses its estimate.
        """
        self._mark_unknown()
        self.async_write_ha_state()
        for member in self._member_covers():
            member._mark_unknown()
            member.async_write_ha_state()
        self._reconcile_overlaps(moving=True)

    def _member_covers(self) -> tuple[ZemismartCover, ...]:
        """Return registered single-channel members addressed by this group."""
        if not self._config.is_group:
            return ()
        covers = _COVERS.get(self._hub, ())
        channels = set(self._config.channels)
        return tuple(
            cover
            for cover in covers
            if cover is not self
            and cover._config.remote == self._config.remote
            and len(cover._config.channels) == 1
            and set(cover._config.channels) <= channels
        )

    def _overlapping_covers(self) -> tuple[ZemismartCover, ...]:
        """Return registered covers sharing any channel of this cover's remote."""
        covers = _COVERS.get(self._hub, ())
        channels = set(self._config.channels)
        return tuple(
            cover
            for cover in covers
            if cover is not self
            and cover._config.remote == self._config.remote
            and channels & set(cover._config.channels)
        )

    def _reconcile_overlaps(self, *, moving: bool) -> None:
        """Invalidate every overlapping cover this RF frame made stale.

        Members fully inside a group frame are modeled explicitly by the
        caller and excluded here. Everything else sharing a channel — a group
        containing an individually driven or stopped channel, or an
        arbitrarily overlapping group — now has an aggregate estimate that no
        longer describes its physical members; only unknown is honest. A STOP
        (moving=False) leaves idle overlapping covers alone: stopping an idle
        motor does not move anything.
        """
        members = set(self._member_covers())
        for cover in self._overlapping_covers():
            if cover in members:
                continue
            if cover._direction != 0 or (moving and cover._position is not None):
                cover._mark_unknown()
                cover.async_write_ha_state()

    def _record_ack(self, ack: CommandAck) -> None:
        """Record the bridge selected at worker publish time."""
        self._last_bridge = ack.bridge.bridge_id
        self._degraded = ack.bridge.area_id != self._config.area_id

    def _start_motion(
        self,
        ack: CommandAck,
        *,
        direction: int,
        target: float,
        duration: float,
        notify_members: bool = True,
        absolute_anchor: bool = False,
    ) -> None:
        """Commit a fresh local model from correlated first RF dispatch."""
        self._interrupt_motion(ack.started_at)
        self._record_ack(ack)
        # An absolute anchor settles an unverified restore-time anchor only
        # when the full travel COMPLETES at the motor's own limit switch (in
        # _async_track_motion), never at its start: a travel interrupted by a
        # STOP before completion did not reach the limit, so the questioned
        # position stays revocable by a late offline report. A relative
        # partial move — even one whose target clamps to an endpoint — is not
        # an absolute anchor at all.
        self._motion_absolute_anchor = absolute_anchor
        self._motion_start_position = self._position
        self._motion_target = target
        self._motion_duration = duration
        self._motion_started = ack.started_at
        # The model ends at whichever comes first: this cover's own travel
        # (a clamped member reaches its limit switch before the group frame
        # ends) or the bridge-armed STOP deadline. For the cover that owns
        # the command the two coincide.
        deadline = ack.started_at + duration
        if ack.deadline is not None:
            deadline = min(deadline, ack.deadline)
        self._motion_deadline = deadline
        self._direction = direction
        self._motion_bridge = ack.bridge.bridge_id
        self._motion_command_id = ack.command_id
        self._motion_timed = ack.deadline is not None
        if self._motion_timed and self._hub.was_displaced(ack.command_id):
            # The displaced status raced ahead of this model commit: the
            # bridge already flushed this timed motion's fail-safe STOP, so
            # freeze immediately instead of tracking a retired command. (A
            # displaced FULL travel still rides to its endpoint on the
            # motor's own limit switch, so its model proceeds normally.)
            self._interrupt_motion(WALL_CLOCK())
            return
        self._create_motion_task("travel")
        if notify_members:
            for member in self._member_covers():
                member._start_member_motion(
                    ack,
                    direction=direction,
                    duration=duration,
                    group_target=target,
                )
            self._reconcile_overlaps(moving=True)

    def _start_member_motion(
        self,
        ack: CommandAck,
        *,
        direction: int,
        duration: float,
        group_target: float,
    ) -> None:
        """Model a group command against this member's own position estimate.

        The RF frame moves every member for the same duration, so each member
        travels the same fraction of full travel from wherever it physically
        is — not from the group's aggregate estimate.
        """
        full_travel = self._config.travel_up if direction > 0 else self._config.travel_down
        # Compute from the member's estimate AT RF start: if this member was
        # itself still moving, its stored position is up to one update
        # interval stale, and _start_motion will sync the model origin to
        # ack.started_at — the target must come from the same instant.
        origin = self._estimated_position(ack.started_at)
        if group_target in (0.0, 100.0):
            # A full travel runs each motor to its own limit switch: model it
            # over this member's OWN calibration, not the group's duration (a
            # slower member would otherwise report done while moving, and a
            # faster one would report moving long after its limit switch).
            target = group_target
            duration = full_travel + FULL_TRAVEL_MARGIN_SECONDS
        elif origin is None:
            # The member moved with the group but its origin is unknown; only
            # an unknown estimate is honest here.
            self._mark_unknown()
            self.async_write_ha_state()
            return
        else:
            delta = duration / full_travel * 100.0 * (1 if direction > 0 else -1)
            target = max(0.0, min(100.0, origin + delta))
            # A clamped target means this member reaches its own limit switch
            # long before the group's frame duration elapses: model only the
            # physical distance (plus the usual endpoint margin), so the
            # member does not report moving after it stopped.
            duration = abs(target - origin) / 100.0 * full_travel
            if target in (0.0, 100.0):
                duration += FULL_TRAVEL_MARGIN_SECONDS
        self._start_motion(
            ack,
            direction=direction,
            target=target,
            duration=duration,
            notify_members=False,
            absolute_anchor=group_target in (0.0, 100.0),
        )
        self.async_write_ha_state()

    def _create_motion_task(self, label: str) -> None:
        """Start the one local travel-time integration task."""
        token = object()
        self._motion_token = token
        self._motion_task = self.hass.async_create_task(
            self._async_track_motion(token),
            f"Zemismart {self._config.name} {label}",
        )

    async def _async_track_motion(self, token: object) -> None:
        """Integrate this cover until its RF-start-based motion deadline."""
        while self._motion_token is token:
            remaining = self._motion_deadline - WALL_CLOCK()
            if remaining <= 0:
                break
            await asyncio.sleep(min(POSITION_UPDATE_INTERVAL_SECONDS, remaining))
            if self._motion_token is not token:
                return
            self._sync_position()
            self.async_write_ha_state()
        if self._motion_token is not token:
            return
        self._position = self._motion_target
        if self._motion_absolute_anchor:
            # A commanded full travel ran its whole configured duration plus
            # margin and is now at the hard limit: the questioned restore
            # anchor is settled by a genuine physical reference.
            self._unverified_anchor_bridge = None
        self._motion_token = None
        self._motion_task = None
        self._clear_motion()
        self.async_write_ha_state()

    async def _async_transmit(
        self,
        button: Button,
        *,
        stop_after_ms: int | None = None,
        overlap_token: int | None = None,
    ) -> CommandAck | None:
        """Await the queued result and translate transport errors into HA state."""
        try:
            result = await self._hub.async_transmit(
                self._config,
                button,
                stop_after_ms=stop_after_ms,
                overlap_token=overlap_token,
            )
        except (CommandAckTimeoutError, CommandStartedTimeoutError) as exc:
            self._mark_unknown_and_notify_members()
            raise HomeAssistantError(str(exc)) from exc
        except Exception as exc:
            self._degraded = True
            self.async_write_ha_state()
            raise HomeAssistantError(str(exc)) from exc
        if result == "superseded":
            return None
        return result

    async def _async_move_full(
        self,
        button: Button,
        direction: int,
        target: float,
    ) -> None:
        """Run a full configured calibration regardless of the prior estimate."""
        ack = await self._async_transmit(button)
        if ack is None:
            return
        configured = self._config.travel_up if direction > 0 else self._config.travel_down
        self._start_motion(
            ack,
            direction=direction,
            target=target,
            duration=configured + FULL_TRAVEL_MARGIN_SECONDS,
            absolute_anchor=True,
        )
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open fully and anchor only after full configured travel plus margin."""
        del kwargs
        async with self._command_lock:
            await self._async_move_full("UP", 1, 100.0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close fully and anchor only after full configured travel plus margin."""
        del kwargs
        async with self._command_lock:
            await self._async_move_full("DOWN", -1, 0.0)

    async def _async_stop(self) -> bool:
        """Stop and freeze tracking only when STOP first dispatches.

        Returns False when the STOP was superseded by a newer overlapping
        command: the caller's multi-frame operation must abort rather than
        publish an older intent over the newer command.
        """
        ack = await self._async_transmit("STOP")
        if ack is None:
            return False
        self._interrupt_motion(ack.started_at)
        self._record_ack(ack)
        for member in self._member_covers():
            # Freeze each member at its OWN integrated estimate; the group's
            # aggregate says nothing about where an individual blind stopped.
            member._interrupt_motion(ack.started_at)
            member._record_ack(ack)
            member.async_write_ha_state()
        self._reconcile_overlaps(moving=False)
        self.async_write_ha_state()
        return True

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Queue a priority STOP and commit interruption after RF dispatch."""
        del kwargs
        async with self._command_lock:
            await self._async_stop()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move partially from a known estimate with an acknowledged timed STOP."""
        target = max(0, min(100, int(kwargs[ATTR_POSITION])))
        async with self._command_lock:
            await self._async_set_position_locked(target)

    async def _async_set_position_locked(self, target: int) -> None:
        """Run one serialized partial or endpoint move for this entity."""
        if target == 0:
            await self._async_move_full("DOWN", -1, 0.0)
            return
        if target == 100:
            await self._async_move_full("UP", 1, 100.0)
            return

        # Stop first so the travel duration is computed from a settled
        # estimate: computing it against a still-moving blind would bake the
        # queue/transit delay into the physical stopping point. A superseded
        # STOP means a newer overlapping command owns the channels now —
        # abort instead of publishing an older intent over it.
        if self._direction != 0 and not await self._async_stop():
            return
        # Snapshot channel publish state: if any overlapping command
        # publishes between this measurement and our movement frame, the
        # hub resolves the movement as superseded instead of letting the
        # OLDER intent overwrite the newer command on air.
        overlap_token = self._hub.overlap_token(self._config)
        current = self._estimated_position(WALL_CLOCK())
        if current is None:
            msg = "position is unknown; run a full open or close calibration first"
            raise HomeAssistantError(msg)
        if abs(target - current) < 0.5:
            return

        direction = 1 if target > current else -1
        full_travel = self._config.travel_up if direction > 0 else self._config.travel_down
        duration = abs(target - current) / 100 * full_travel
        stop_after_ms = max(1, round(duration * 1_000))
        ack = await self._async_transmit(
            "UP" if direction > 0 else "DOWN",
            stop_after_ms=stop_after_ms,
            overlap_token=overlap_token,
        )
        if ack is None:
            return
        acknowledged_duration = (
            max(0.001, ack.deadline - ack.started_at) if ack.deadline is not None else duration
        )
        self._start_motion(
            ack,
            direction=direction,
            target=float(target),
            duration=acknowledged_duration,
        )
        self.async_write_ha_state()
