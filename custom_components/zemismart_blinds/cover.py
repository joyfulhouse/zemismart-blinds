"""RF-start-gated travel-time cover entities for Zemismart blinds and groups."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    FULL_TRAVEL_MARGIN_SECONDS,
    POSITION_UPDATE_INTERVAL_SECONDS,
)
from .coordinator import RemoteCoordinator
from .models import (
    BlindConfig,
    Button,
    CommandAck,
    CommandAckTimeoutError,
    CommandStartedTimeoutError,
    CoverConfig,
    RemoteRuntime,
    Role,
    TakeoverCoverState,
    ZemismartHub,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .state_sync import HeardEvent

_LOGGER = logging.getLogger(__name__)

_ATTR_DEGRADED = "degraded_bridge"
_ATTR_LAST_BRIDGE = "last_bridge"
_ATTR_MOTION_ABSOLUTE_ANCHOR = "motion_absolute_anchor"
_ATTR_MOTION_BRIDGE = "motion_bridge"
_ATTR_MOTION_COMMAND_ID = "motion_command_id"
_ATTR_MOTION_DEADLINE = "motion_deadline"
_ATTR_MOTION_DIRECTION = "motion_direction"
_ATTR_MOTION_STARTED = "motion_started"
_ATTR_MOTION_START_POSITION = "motion_start_position"
_ATTR_MOTION_TARGET = "motion_target"
_ATTR_MOTION_TIMED = "motion_timed"
_ATTR_UNVERIFIED_ANCHOR = "unverified_anchor_bridge"
_ATTR_UNVERIFIED_ANCHOR_COMMAND_ID = "unverified_anchor_command_id"
_ATTR_UNVERIFIED_ANCHOR_OFFLINE = "unverified_anchor_offline"
WALL_CLOCK = time.time
_UNTIMED_DISARM_DRAIN_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True)
class _MotionStart:
    """Carry model timing and provenance independently of a transport ack."""

    source: str
    started_at: float
    deadline: float | None
    bridge_id: str | None
    command_id: str | None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[RemoteRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one leaf cover entity per leaf subentry of this remote."""
    from homeassistant.helpers import device_registry as dr

    runtime = entry.runtime_data
    covers: dict[str, CoverConfig] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != "cover":
            continue
        try:
            covers[subentry.subentry_id] = CoverConfig.from_subentry(subentry.data)
        except TypeError, ValueError:
            _LOGGER.warning(
                "Skipping unreadable cover subentry %s of %s",
                subentry.subentry_id,
                entry.title,
            )
    coordinator = RemoteCoordinator(hass, covers)
    runtime.coordinator = coordinator
    entry.async_on_unload(coordinator.detach)
    registry = dr.async_get(hass)
    for subentry_id, cover in covers.items():
        role = coordinator.roles[subentry_id]
        config = BlindConfig.derive(runtime.remote, cover, role)
        entity: ZemismartCover | ZemismartAggregateCover
        if role is Role.LEAF:
            entity = ZemismartCover(
                subentry_id,
                entry.entry_id,
                config,
                runtime.hub,
                coordinator,
            )
        else:
            entity = ZemismartAggregateCover(
                subentry_id,
                entry.entry_id,
                config,
                runtime.hub,
                coordinator,
            )
        async_add_entities([entity], config_subentry_id=subentry_id)
        device = registry.async_get_device(identifiers={(DOMAIN, subentry_id)})
        if device is not None and device.area_id is None:
            # Configured area applies at creation only; a user's later
            # device-page override must survive reloads.
            registry.async_update_device(device.id, area_id=runtime.remote.area_id)


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

    def __init__(
        self,
        subentry_id: str,
        via_entry_id: str,
        config: BlindConfig,
        hub: ZemismartHub,
        coordinator: RemoteCoordinator | None = None,
    ) -> None:
        """Initialize one cover with its own travel-time estimate."""
        if config.travel_up is None or config.travel_down is None:
            msg = "leaf cover entities require travel calibration"
            raise ValueError(msg)
        self._travel_up: float = config.travel_up
        self._travel_down: float = config.travel_down
        self._config: BlindConfig = config
        self._hub = hub
        self._coordinator = coordinator
        self._entry_id = subentry_id
        self._via_entry_id = via_entry_id
        self._attr_unique_id = subentry_id
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
        self._unverified_anchor_command_id: str | None = None
        self._unverified_anchor_offline = False
        self._motion_token: object | None = None
        self._motion_task: asyncio.Task[None] | None = None
        self._last_bridge: str | None = None
        self._degraded = False
        self._intent_generation = 0
        self._restore_epoch = 0
        self._unsubscribe_rx_listener: Callable[[], None] | None = None
        self._stopped_by_heard = False
        # Serializes this entity's own commands: without it, a set_position
        # racing an unstarted open/close computes travel from a stale
        # estimate and physically overshoots.
        self._command_lock = asyncio.Lock()

    async def async_set_member_position(self, target: int) -> None:
        """Run one aggregate-delegated position move under this entity's lock."""
        async with self._command_lock:
            await self._async_set_position_locked(target)

    @property
    def device_info(self) -> DeviceInfo:
        """Represent this cover as a child device of its remote."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._config.name,
            manufacturer="Zemismart",
            model="433 MHz blind group" if self._config.is_group else "433 MHz blind",
            via_device=(DOMAIN, self._via_entry_id),
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
            "role": self._config.role.value,
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
            _ATTR_MOTION_ABSOLUTE_ANCHOR: self._motion_absolute_anchor,
            _ATTR_UNVERIFIED_ANCHOR: self._unverified_anchor_bridge,
            _ATTR_UNVERIFIED_ANCHOR_COMMAND_ID: self._unverified_anchor_command_id,
            _ATTR_UNVERIFIED_ANCHOR_OFFLINE: self._unverified_anchor_offline,
        }

    async def async_added_to_hass(self) -> None:
        """Restore a stopped estimate or reconstruct complete started motion."""
        await super().async_added_to_hass()
        if self._coordinator is not None:
            self._coordinator.register_leaf(self._entry_id, self)
        self._unsubscribe_rx_listener = self._hub.register_rx_listener(
            self._config.remote.key,
            frozenset(self._config.channels),
            self._on_heard_press,
            takeover_state=self._takeover_state,
            invalidate_takeover=self._invalidate_for_takeover,
        )
        self._hub.displaced_listeners.append(self._on_displaced)
        self._hub.emission_proof_listeners.append(self._on_emission_proof)
        self._hub.bridge_listeners.append(self._on_bridge_change)
        restore_guard = (self._intent_generation, self._restore_epoch)
        await self._async_restore_state(restore_guard)

    async def _async_restore_state(self, restore_guard: tuple[int, int]) -> None:
        """Restore one stopped estimate or complete started motion."""
        state = await self.async_get_last_state()
        if state is None or restore_guard != (self._intent_generation, self._restore_epoch):
            return
        if state.attributes.get("remote") != self._config.remote_key or state.attributes.get(
            "channels"
        ) != list(self._config.channels):
            # The entry was re-pointed at different hardware (remote or
            # channel set changed in options): the persisted position and
            # motion describe the OLD physical target and must not be
            # assigned to the new one.
            return
        restored_role = state.attributes.get("role", Role.LEAF.value)
        if restored_role != self._config.role.value:
            # A topology change flipped this cover's role since the state
            # was persisted; the old model does not describe the new shape.
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
        self._unverified_anchor_command_id = (
            self._optional_text(state.attributes.get(_ATTR_UNVERIFIED_ANCHOR_COMMAND_ID))
            if self._unverified_anchor_bridge is not None
            else None
        )
        self._unverified_anchor_offline = (
            self._unverified_anchor_bridge is not None
            and state.attributes.get(_ATTR_UNVERIFIED_ANCHOR_OFFLINE) is True
        )
        self._replay_emission_proof()

        raw_direction = state.attributes.get(_ATTR_MOTION_DIRECTION, 0)
        direction = (
            raw_direction
            if isinstance(raw_direction, int) and not isinstance(raw_direction, bool)
            else 0
        )
        if direction not in {-1, 1}:
            if state.state in {"opening", "closing"}:
                self._mark_unknown()
            else:
                self._reconcile_unverified_anchor()
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
        if timed and self._hub.was_displaced(command_id):
            # The status listener was installed before awaiting last state, so
            # a displaced report can arrive while the command id is not yet
            # restored. The hub's bounded recent-id memory closes that gap.
            self._mark_unknown()
            return
        absolute_anchor = (
            state.attributes.get(_ATTR_MOTION_ABSOLUTE_ANCHOR, False) is True
            and not timed
            and target in {0.0, 100.0}
        )
        self._direction = direction
        self._motion_absolute_anchor = absolute_anchor
        self._reconcile_unverified_anchor()
        if self._direction == 0:
            return
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
            if absolute_anchor:
                # A full travel that finished during downtime reached its hard
                # limit just like one completed by _async_track_motion.
                self._clear_unverified_anchor()
            elif (
                timed
                and not self._bridge_seen_online(bridge)
                and self._unverified_anchor_bridge is None
            ):
                # The anchored target assumed the bridge's armed STOP fired
                # while HA was down, but retained availability has not
                # arrived yet on this cold start. Remember the bridge: if it
                # later reports offline, the STOP may never have fired and
                # the anchor is invalidated.
                self._set_unverified_anchor(bridge, command_id)
            self._reconcile_unverified_anchor()
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
        if self._unsubscribe_rx_listener is not None:
            self._unsubscribe_rx_listener()
            self._unsubscribe_rx_listener = None
        if self._coordinator is not None:
            self._coordinator.unregister_leaf(self._entry_id)
        if self._on_displaced in self._hub.displaced_listeners:
            self._hub.displaced_listeners.remove(self._on_displaced)
        if self._on_emission_proof in self._hub.emission_proof_listeners:
            self._hub.emission_proof_listeners.remove(self._on_emission_proof)
        if self._on_bridge_change in self._hub.bridge_listeners:
            self._hub.bridge_listeners.remove(self._on_bridge_change)
        self._cancel_motion_task()
        await super().async_will_remove_from_hass()

    @callback
    def _on_heard_press(self, event: HeardEvent) -> None:
        """Mirror one intersecting physical press without transmitting.

        Laminar topology makes ownership trivial: a press fully covering this
        leaf is modeled here (aggregates re-derive from members); a partial
        intersection moved only part of this leaf's motor set, so only
        unknown is honest.
        """
        channels = frozenset(self._config.channels)
        if channels.isdisjoint(event.chans):
            return
        self._intent_generation += 1
        if not channels <= event.chans:
            self._mark_unknown()
            self.async_write_ha_state()
            return
        self._start_heard_motion(event)

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

    @callback
    def _on_emission_proof(self, command_id: str) -> None:
        """Verify only the restored anchor derived from this exact command."""
        if not command_id or command_id != self._unverified_anchor_command_id:
            return
        self._clear_unverified_anchor()
        self.async_write_ha_state()

    def _replay_emission_proof(self) -> None:
        """Apply proof that raced ahead of restoring or creating its marker."""
        command_id = self._unverified_anchor_command_id
        if command_id is not None and self._hub.was_emission_proven(command_id):
            self._on_emission_proof(command_id)

    def _set_unverified_anchor(self, bridge_id: str, command_id: str) -> None:
        """Question one restore target under its exact scheduler command."""
        self._unverified_anchor_bridge = bridge_id
        self._unverified_anchor_command_id = command_id
        self._unverified_anchor_offline = False
        self._replay_emission_proof()

    def _clear_unverified_anchor(self) -> None:
        """Clear all bridge- and command-scoped anchor evidence together."""
        self._unverified_anchor_bridge = None
        self._unverified_anchor_command_id = None
        self._unverified_anchor_offline = False

    def _bridge_seen_online(self, bridge_id: str) -> bool:
        """Return whether this bridge has explicitly announced itself online."""
        return any(
            bridge.online for bridge in self._hub.registry.bridges if bridge.bridge_id == bridge_id
        )

    def _on_bridge_change(self) -> None:
        """Re-evaluate availability and timed-motion safety on bridge changes."""
        self._reconcile_unverified_anchor()
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

    def _reconcile_unverified_anchor(self) -> None:
        """Apply existing bridge state to a questioned restore-time anchor."""
        anchor_bridge = self._unverified_anchor_bridge
        if anchor_bridge is None:
            self._unverified_anchor_offline = False
            self._unverified_anchor_command_id = None
            return
        if self._hub.registry.is_known_offline(anchor_bridge):
            # A relative motion still derives from the questioned origin and
            # must be revoked with it. Only a live commanded full travel is
            # exempt: completing at the hard limit will establish a genuine
            # physical anchor independent of that origin.
            if self._direction == 0 or not self._motion_absolute_anchor:
                self._mark_unknown()
            else:
                # Keep the offline evidence even if this bridge reconnects
                # before the full travel either reaches its limit or stops.
                self._unverified_anchor_offline = True
        elif self._unverified_anchor_offline:
            if self._direction == 0 or not self._motion_absolute_anchor:
                self._mark_unknown()
        elif self._bridge_seen_online(anchor_bridge):
            self._clear_unverified_anchor()

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
        self._stopped_by_heard = False
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
        self._clear_unverified_anchor()
        self._degraded = True

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
        absolute_anchor: bool = False,
    ) -> None:
        """Commit a fresh local model from correlated first RF dispatch."""
        motion = _MotionStart(
            source="commanded",
            started_at=ack.started_at,
            deadline=ack.deadline,
            bridge_id=ack.bridge.bridge_id,
            command_id=ack.command_id,
        )
        self._record_ack(ack)
        self._commit_motion(
            motion,
            direction=direction,
            target=target,
            duration=duration,
            absolute_anchor=absolute_anchor,
        )

    def _start_heard_motion(self, event: HeardEvent) -> None:
        """Mirror one fully addressed physical movement event."""
        if event.button == "STOP":
            self._apply_stop(event.heard_at, provenance="heard")
            return
        if event.button == "UP":
            direction = 1
            target = 100.0
            configured = self._travel_up
        elif event.button == "DOWN":
            direction = -1
            target = 0.0
            configured = self._travel_down
        else:
            return
        motion = _MotionStart(
            source="heard",
            started_at=event.heard_at,
            deadline=None,
            bridge_id=None,
            command_id=None,
        )
        duration = configured + FULL_TRAVEL_MARGIN_SECONDS
        self._commit_motion(
            motion,
            direction=direction,
            target=target,
            duration=duration,
            absolute_anchor=True,
        )
        self.async_write_ha_state()

    def _takeover_state(self) -> TakeoverCoverState:
        """Return current modeled-command and heard-STOP takeover state."""
        button: Button | None = None
        if self._direction > 0:
            button = "UP"
        elif self._direction < 0:
            button = "DOWN"
        disarm_deadline: float | None = None
        if self._motion_bridge is not None and self._motion_command_id is not None:
            disarm_deadline = (
                self._motion_deadline
                if self._motion_timed
                else WALL_CLOCK() + _UNTIMED_DISARM_DRAIN_SECONDS
            )
        return TakeoverCoverState(
            bridge_id=self._motion_bridge,
            command_id=self._motion_command_id,
            button=button,
            disarm_deadline=disarm_deadline,
            stopped_by_heard=self._stopped_by_heard,
        )

    @callback
    def _invalidate_for_takeover(self) -> None:
        """Apply one hub-classified takeover invalidation to this cover."""
        if self._unsubscribe_rx_listener is None:
            return
        self._restore_epoch += 1
        self._mark_unknown()
        self.async_write_ha_state()

    def _commit_motion(
        self,
        motion: _MotionStart,
        *,
        direction: int,
        target: float,
        duration: float,
        absolute_anchor: bool,
    ) -> None:
        """Commit travel fields from either a command ack or a heard press."""
        self._interrupt_motion(motion.started_at)
        if self._timed_motion_bridge_offline(motion):
            # The started status and retained offline LWT can be delivered in
            # one broker batch. The bridge's RAM-only armed STOP may already
            # be gone, so committing the partial target would be false trust.
            self._mark_unknown()
            return
        if not absolute_anchor:
            # Interrupting an exempt full travel can expose a questioned
            # origin that went offline while the hard-limit motion was live.
            # A relative replacement must revalidate before it can establish
            # another position derived from that origin.
            self._reconcile_unverified_anchor()
            if self._position is None:
                return
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
        self._motion_started = motion.started_at
        # The model ends at whichever comes first: this cover's own travel
        # (a clamped member reaches its limit switch before the group frame
        # ends) or the bridge-armed STOP deadline. For the cover that owns
        # the command the two coincide.
        deadline = motion.started_at + duration
        if motion.deadline is not None:
            deadline = min(deadline, motion.deadline)
        self._motion_deadline = deadline
        self._direction = direction
        self._motion_bridge = motion.bridge_id
        self._motion_command_id = motion.command_id
        self._motion_timed = motion.deadline is not None
        displaced = (
            motion.source == "commanded"
            and self._motion_timed
            and motion.command_id is not None
            and self._hub.was_displaced(motion.command_id)
        )
        if displaced:
            # The displaced status raced ahead of this model commit: the
            # bridge already flushed this timed motion's fail-safe STOP, so
            # freeze immediately instead of tracking a retired command. (A
            # displaced FULL travel still rides to its endpoint on the
            # motor's own limit switch, so its model proceeds normally.)
            self._interrupt_motion(WALL_CLOCK())
        else:
            label = "heard travel" if motion.source == "heard" else "travel"
            self._create_motion_task(label)

    def _timed_motion_bridge_offline(self, motion: _MotionStart) -> bool:
        """Return whether a timed start depends on a bridge already offline."""
        return (
            motion.deadline is not None
            and motion.bridge_id is not None
            and self._hub.registry.is_known_offline(motion.bridge_id)
        )

    def _start_member_motion(
        self,
        motion: _MotionStart,
        *,
        ack: CommandAck | None,
        direction: int,
        duration: float,
        group_target: float,
    ) -> None:
        """Model a group command against this member's own position estimate.

        The RF frame moves every member for the same duration, so each member
        travels the same fraction of full travel from wherever it physically
        is — not from the group's aggregate estimate.
        """
        full_travel = self._travel_up if direction > 0 else self._travel_down
        # Compute from the member's estimate AT RF start: if this member was
        # itself still moving, its stored position is up to one update
        # interval stale, and _commit_motion will sync the model origin to
        # motion.started_at — the target must come from the same instant.
        origin = self._estimated_position(motion.started_at)
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
        if ack is not None:
            self._record_ack(ack)
        self._commit_motion(
            motion,
            direction=direction,
            target=target,
            duration=duration,
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
            self._clear_unverified_anchor()
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
            # The frame MAY have reached RF; only unknown is honest. Aggregates
            # containing this leaf re-derive through the coordinator.
            self._mark_unknown()
            self.async_write_ha_state()
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
        intent_generation = self._intent_generation
        ack = await self._async_transmit(button)
        if ack is None or intent_generation != self._intent_generation:
            return
        configured = self._travel_up if direction > 0 else self._travel_down
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

    def _apply_stop(self, at: float, *, provenance: str) -> None:
        """Freeze this cover at one commanded or heard STOP."""
        if provenance not in {"commanded", "heard"}:
            msg = f"unsupported STOP provenance: {provenance}"
            raise ValueError(msg)
        self._interrupt_motion(at)
        self._reconcile_unverified_anchor()
        self.async_write_ha_state()
        if provenance == "heard":
            self._stopped_by_heard = True

    async def _async_stop(self) -> bool:
        """Stop and freeze tracking only when STOP first dispatches.

        Returns False when the STOP was superseded by a newer overlapping
        command: the caller's multi-frame operation must abort rather than
        publish an older intent over the newer command.
        """
        intent_generation = self._intent_generation
        ack = await self._async_transmit("STOP")
        if ack is None or intent_generation != self._intent_generation:
            return False
        # A displaced STOP still STARTED — its frame went on air and halted the
        # motors — before a newer command replaced it. Freeze self + members at
        # that instant REGARDLESS of displacement: a full-travel group member is
        # untimed, so the timed-only _on_displaced never freezes it, and nothing
        # else would correct a member the displacer does not re-drive. Only the
        # RETURN VALUE reports the displacement, so a chained set-position caller
        # still aborts rather than publishing an older intent over the newer one.
        displaced = self._hub.was_displaced(ack.command_id)
        self._record_ack(ack)
        self._apply_stop(ack.started_at, provenance="commanded")
        return not displaced

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
        full_travel = self._travel_up if direction > 0 else self._travel_down
        duration = abs(target - current) / 100 * full_travel
        stop_after_ms = max(1, round(duration * 1_000))
        intent_generation = self._intent_generation
        ack = await self._async_transmit(
            "UP" if direction > 0 else "DOWN",
            stop_after_ms=stop_after_ms,
            overlap_token=overlap_token,
        )
        if ack is None or intent_generation != self._intent_generation:
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


class ZemismartAggregateCover(CoverEntity):
    """A cover whose state derives from its leaf members.

    RF behavior matches the retired group entries: open/close/stop transmit
    ONE frame addressed to the full channel set; position commands fan out to
    each member's own timed positioning. The aggregate owns no position model
    of its own — members are the single source of truth.
    """

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

    def __init__(
        self,
        subentry_id: str,
        via_entry_id: str,
        config: BlindConfig,
        hub: ZemismartHub,
        coordinator: RemoteCoordinator,
    ) -> None:
        """Initialize one aggregate bound to its coordinator topology."""
        self._config = config
        self._hub = hub
        self._coordinator = coordinator
        self._subentry_id = subentry_id
        self._via_entry_id = via_entry_id
        self._attr_unique_id = subentry_id
        self._last_command_bridge: str | None = None
        self._last_command_id: str | None = None
        self._last_command_button: Button | None = None
        self._stopped_by_heard = False
        self._fanout_tasks: set[asyncio.Task[None]] = set()
        self._unsubscribe_rx_listener: Callable[[], None] | None = None
        # Serializes the aggregate's own single-frame commands. Position
        # fan-out deliberately runs OUTSIDE this lock so STOP never queues
        # behind an in-flight fan-out (it cancels the fan-out instead).
        self._command_lock = asyncio.Lock()

    @property
    def device_info(self) -> DeviceInfo:
        """Represent this aggregate as a child device of its remote."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._subentry_id)},
            name=self._config.name,
            manufacturer="Zemismart",
            model="433 MHz blind group",
            via_device=(DOMAIN, self._via_entry_id),
        )

    async def async_added_to_hass(self) -> None:
        """Register with the coordinator and the hub's takeover machinery."""
        await super().async_added_to_hass()
        self._coordinator.register_aggregate(self._subentry_id, self)
        self._unsubscribe_rx_listener = self._hub.register_rx_listener(
            self._config.remote.key,
            frozenset(self._config.channels),
            self._on_heard_press,
            takeover_state=self._takeover_state,
            invalidate_takeover=self._invalidate_for_takeover,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister and cancel any in-flight fan-out."""
        if self._unsubscribe_rx_listener is not None:
            self._unsubscribe_rx_listener()
            self._unsubscribe_rx_listener = None
        self._coordinator.unregister_aggregate(self._subentry_id)
        self._cancel_fanout()
        await super().async_will_remove_from_hass()

    def _members(self) -> tuple[ZemismartCover, ...]:
        """Return the live leaf entities this aggregate derives from."""
        return cast(
            "tuple[ZemismartCover, ...]",
            self._coordinator.members_of(self._subentry_id),
        )

    @property
    def available(self) -> bool:
        """Available while RF works and at least one member is registered."""
        return bool(self._members()) and any(bridge.online for bridge in self._hub.registry.bridges)

    @property
    def current_cover_position(self) -> int | None:
        """Return the unweighted mean of members with known positions."""
        positions = [
            position
            for member in self._members()
            if (position := member.current_cover_position) is not None
        ]
        if not positions:
            return None
        return round(sum(positions) / len(positions))

    @property
    def is_opening(self) -> bool:
        """Return whether any member is opening (HA reports opening first)."""
        return any(member.is_opening for member in self._members())

    @property
    def is_closing(self) -> bool:
        """Return whether any member is closing."""
        return any(member.is_closing for member in self._members())

    @property
    def is_closed(self) -> bool | None:
        """Closed iff every member is closed; open if any is open; else unknown."""
        states = [member.is_closed for member in self._members()]
        if not states:
            return None
        if any(state is False for state in states):
            return False
        if all(state is True for state in states):
            return True
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose topology metadata for diagnostics and restore discrimination."""
        return {
            "channels": list(self._config.channels),
            "remote": self._config.remote_key,
            "role": self._config.role.value,
        }

    @callback
    def _on_heard_press(self, event: HeardEvent) -> None:
        """Track heard STOPs for takeover; members model their own motion."""
        del event
        # Members receive their own callbacks and model the press; the
        # aggregate's displayed state re-derives through the coordinator.

    def _takeover_state(self) -> TakeoverCoverState:
        """Expose the last single-frame command for hub takeover disarms."""
        disarm_deadline: float | None = None
        if self._last_command_bridge is not None and self._last_command_id is not None:
            disarm_deadline = WALL_CLOCK() + _UNTIMED_DISARM_DRAIN_SECONDS
        return TakeoverCoverState(
            bridge_id=self._last_command_bridge,
            command_id=self._last_command_id,
            button=self._last_command_button,
            disarm_deadline=disarm_deadline,
            stopped_by_heard=self._stopped_by_heard,
        )

    @callback
    def _invalidate_for_takeover(self) -> None:
        """Clear the tracked command after a hub-classified takeover."""
        self._last_command_bridge = None
        self._last_command_id = None
        self._last_command_button = None
        self.async_write_ha_state()

    def _cancel_fanout(self) -> None:
        """Cancel every pending member position delegation."""
        for task in tuple(self._fanout_tasks):
            task.cancel()
        self._fanout_tasks.clear()

    async def _async_transmit(self, button: Button) -> CommandAck | None:
        """Send one untimed full-channel-set frame and record its identity."""
        try:
            result = await self._hub.async_transmit(self._config, button)
        except Exception as exc:
            raise HomeAssistantError(str(exc)) from exc
        if result == "superseded":
            return None
        self._last_command_bridge = result.bridge.bridge_id
        self._last_command_id = result.command_id
        self._last_command_button = button
        self._stopped_by_heard = False
        return result

    async def _async_move_full(self, button: Button, direction: int, target: float) -> None:
        """Run one group frame and start every member's own motion model."""
        ack = await self._async_transmit(button)
        if ack is None:
            return
        motion = _MotionStart(
            source="commanded",
            started_at=ack.started_at,
            deadline=ack.deadline,
            bridge_id=ack.bridge.bridge_id,
            command_id=ack.command_id,
        )
        for member in self._members():
            member._start_member_motion(
                motion,
                ack=ack,
                direction=direction,
                duration=0.0,
                group_target=target,
            )
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open every channel with one frame; members model their own travel."""
        del kwargs
        async with self._command_lock:
            await self._async_move_full("UP", 1, 100.0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close every channel with one frame; members model their own travel."""
        del kwargs
        async with self._command_lock:
            await self._async_move_full("DOWN", -1, 0.0)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Cancel fan-out, stop every channel with one frame, freeze members."""
        del kwargs
        self._cancel_fanout()
        async with self._command_lock:
            ack = await self._async_transmit("STOP")
            if ack is None:
                return
            for member in self._members():
                member._record_ack(ack)
                member._apply_stop(ack.started_at, provenance="commanded")
            self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Delegate a position move to every member's own timed positioning."""
        target = max(0, min(100, int(kwargs[ATTR_POSITION])))
        if target == 0:
            await self.async_close_cover()
            return
        if target == 100:
            await self.async_open_cover()
            return
        members = self._members()
        if not members:
            msg = "no member covers are available to position"
            raise HomeAssistantError(msg)
        failures: list[str] = []

        async def delegate(member: ZemismartCover) -> None:
            try:
                await member.async_set_member_position(target)
            except HomeAssistantError as exc:
                failures.append(f"{member._config.name}: {exc}")

        tasks = [
            self.hass.async_create_task(
                delegate(member),
                f"Zemismart {self._config.name} fan-out",
            )
            for member in members
        ]
        self._fanout_tasks.update(tasks)
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._fanout_tasks.difference_update(tasks)
        if failures:
            msg = f"position delegation failed for: {'; '.join(failures)}"
            raise HomeAssistantError(msg)
