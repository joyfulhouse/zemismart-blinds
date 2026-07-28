"""RF-start-gated travel-time cover entities for Zemismart blinds and groups."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ATTR_ENDPOINT,
    DOMAIN,
    ENDPOINT_CLOSE,
    ENDPOINT_OPEN,
    FULL_TRAVEL_MARGIN_SECONDS,
    POSITION_UPDATE_INTERVAL_SECONDS,
    SERVICE_REANCHOR,
)
from .coordinator import RemoteCoordinator
from .models import (
    BlindConfig,
    Button,
    CommandAck,
    CommandAckTimeoutError,
    CommandQueueFullError,
    CommandRejectedError,
    CommandStartedTimeoutError,
    CoverConfig,
    NoOnlineBridgeError,
    RemoteRuntime,
    Role,
    TakeoverCoverState,
    ZemismartHub,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State
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
_ATTR_POSITION_CONFIDENCE = "position_confidence"
# A dedicated persisted flag, kept separate from the derived confidence string
# so the suspect DOUBT survives a restart the way unverified_anchor_* does --
# and deliberately NOT reusing that trio, which tracks restore-time anchor
# provenance rather than a mid-flight interruption.
_ATTR_POSITION_SUSPECT = "position_suspect"
# Closed confidence vocabulary. `unknown` is derived from the entity state
# (no position) rather than stored, so it is intentionally not a stored value.
# `anchored` is deliberately NOT called `verified` (#31): nothing in a one-way
# protocol confirms the motor, so the word must not promise more than "a full
# travel was transmitted, a timer ran to completion, and no contradicting RF
# press was heard".
CONFIDENCE_ANCHORED: Final = "anchored"
CONFIDENCE_ASSUMED: Final = "assumed"
CONFIDENCE_SUSPECT: Final = "suspect"
CONFIDENCE_UNKNOWN: Final = "unknown"
WALL_CLOCK = time.time
_UNTIMED_DISARM_DRAIN_SECONDS: Final = 10.0
# Intermediate travel progress reaches the STATE MACHINE at this rate, while
# the estimate itself keeps integrating every POSITION_UPDATE_INTERVAL_SECONDS.
# The 0.25 s tick is what makes the position smooth for anything reading the
# entity; the recorder gains nothing from 4 Hz history of a dead-reckoned
# estimate, and a 30 s travel used to write ~120 rows per cover.
_PROGRESS_WRITE_INTERVAL_SECONDS: Final = 1.0
# Motion and anchor internals: pure model state, rewritten throughout every
# travel, that no history query would ever ask for. Excluded from the RECORDER
# only -- homeassistant.helpers.restore_state references neither the recorder
# nor _unrecorded_attributes (verified against the installed HA 2026.7.2); it
# snapshots the state machine into its own store, so the unverified_anchor_*
# trio and position_suspect still survive a restart with these listed here.
_UNRECORDED_ATTRIBUTES: Final = frozenset(
    {
        _ATTR_MOTION_STARTED,
        _ATTR_MOTION_DEADLINE,
        _ATTR_MOTION_START_POSITION,
        _ATTR_MOTION_BRIDGE,
        _ATTR_MOTION_COMMAND_ID,
        _ATTR_MOTION_TIMED,
        _ATTR_MOTION_ABSOLUTE_ANCHOR,
        _ATTR_MOTION_DIRECTION,
        _ATTR_MOTION_TARGET,
        _ATTR_UNVERIFIED_ANCHOR,
        _ATTR_UNVERIFIED_ANCHOR_COMMAND_ID,
        _ATTR_UNVERIFIED_ANCHOR_OFFLINE,
    }
)


# The message used when a failure is not one of the defined transport types.
# Reachable only through _failure_translation_key's fallback, never as a literal
# at a raise site, so the catalogue test reads it from here.
_DEFAULT_FAILURE_KEY: Final = "command_failed"
_TRANSPORT_FAILURE_KEYS: Final = {
    # Before NoOnlineBridgeError: both derive from RuntimeError, and isinstance
    # order decides which message a caller sees.
    CommandQueueFullError: "queue_full",
    NoOnlineBridgeError: "no_bridge_online",
    CommandRejectedError: "command_rejected",
    OSError: "transport_failed",
    ValueError: "invalid_frame",
}
# The defined transport failures, derived from the mapping above so the caught
# set and the translated set cannot drift: a type added to only one of them is
# either mapped but never caught, or caught but silently generic. Bound to a
# name rather than written inline because `ruff format` rewrites a literal
# `except (A, B):` back to the bare PEP 758 form.
#
# Deliberately narrow. A TypeError or AttributeError in the command path is a
# programmer defect, and catching it turned a traceback into a bland service
# failure with a `degraded` flag blaming the bridge. OSError stays: a broker
# socket giving way mid-publish is a transport failure, not a defect.
_TRANSPORT_FAILURES: Final[tuple[type[Exception], ...]] = tuple(_TRANSPORT_FAILURE_KEYS)


def _failure_translation_key(exc: BaseException) -> str:
    """Map one defined transport failure to its own translated message.

    Interpolating `str(exc)` into a `{error}` placeholder left English model
    text inside an otherwise translated message -- "no RF433 bridge is online"
    reached every user in every language (#37). Each defined failure gets its
    own key and carries no exception text; the technical detail is logged at the
    raise site instead, where it is actually useful.
    """
    for failure_type, key in _TRANSPORT_FAILURE_KEYS.items():
        if isinstance(exc, failure_type):
            return key
    return _DEFAULT_FAILURE_KEY


def _rf_reachable(hass: HomeAssistant, hub: ZemismartHub) -> bool:
    """Return whether a command issued right now could physically reach the air."""
    from homeassistant.components import mqtt

    # Bridge availability is learned from RETAINED LWT beacons, so every bridge
    # keeps reporting `online` from cached state after HA's own broker link
    # drops -- covers stayed available while nothing could reach a bridge. The
    # local client state is the other half of the path and must be gated too.
    return mqtt.is_connected(hass) and any(bridge.online for bridge in hub.registry.bridges)


def _subscribe_rf_reachability(hass: HomeAssistant, entity: CoverEntity) -> Callable[[], None]:
    """Re-render one entity's availability when the broker link flips."""
    from homeassistant.components import mqtt

    @callback
    def _on_connection_change(_connected: bool) -> None:
        # _rf_reachable() reads the live client state; this only schedules the
        # write. Without it a broker drop leaves every cover rendered available
        # until some unrelated event happens to rewrite the entity.
        entity.async_write_ha_state()

    return mqtt.async_subscribe_connection_status(hass, _on_connection_change)


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
    """Create one entity per stored cover row of this remote."""
    # reanchor is an EXPLICIT recovery action (operator or automation): it drives
    # a full travel to a hard endpoint through the normal command path so the
    # existing outcome-based anchor logic re-verifies against the motor's own
    # limit switch. No autonomous motion, no timer -- it moves only when asked.
    # Registered on the cover platform so HA routes it to these entities; the
    # context is always set when HA drives platform setup. It is unset only when
    # a test invokes this coroutine directly to exercise entity construction, and
    # those exercise no service, so skipping the wiring there is correct.
    if (platform := entity_platform.current_platform.get()) is not None:
        platform.async_register_entity_service(
            SERVICE_REANCHOR,
            {vol.Required(ATTR_ENDPOINT): vol.In((ENDPOINT_OPEN, ENDPOINT_CLOSE))},
            "async_reanchor",
        )

    runtime = entry.runtime_data
    covers: dict[str, CoverConfig] = {cover.cover_id: cover for cover in runtime.remote.covers}
    coordinator = RemoteCoordinator(hass, covers)
    runtime.coordinator = coordinator
    entry.async_on_unload(coordinator.detach)
    entities: list[ZemismartCover | ZemismartAggregateCover] = []
    for cover_id, cover in covers.items():
        role = coordinator.roles[cover_id]
        try:
            config = BlindConfig.derive(runtime.remote, cover, role)
        except ValueError as err:
            # A demoted aggregate without stored travel times must not take
            # the whole entry down; it just has no entity until the user
            # adds travel times via cover reconfigure.
            _LOGGER.warning(
                "Cover %r of %s has no usable configuration (%s); "
                "reconfigure the cover to add travel times",
                cover.name,
                entry.title,
                err,
            )
            continue
        entity: ZemismartCover | ZemismartAggregateCover
        if role is Role.LEAF:
            entity = ZemismartCover(
                cover_id,
                entry.entry_id,
                config,
                runtime.hub,
                coordinator,
            )
        else:
            entity = ZemismartAggregateCover(
                cover_id,
                entry.entry_id,
                config,
                runtime.hub,
                coordinator,
            )
        entities.append(entity)
    # One batched add, not one call per cover: each call schedules its own add
    # pass, so a sixteen-cover remote paid sixteen of them at every reload.
    async_add_entities(entities)


def _number(value: object) -> float | None:
    """Return a JSON number without treating booleans as positions."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class _ZemismartCoverEntity(CoverEntity):
    """The wiring both cover entities on a remote's device share.

    Leaf and aggregate are both assumed-state shades that transmit OEM frames
    and never poll, and both must keep the same high-rate motion attributes out
    of the recorder. Those live here rather than being written twice so they
    cannot drift: an attribute added to one class's extra_state_attributes can
    no longer start silently recording at travel rate in the other.
    """

    _attr_assumed_state = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )
    # channels, remote, role, position_confidence, last_bridge, degraded and
    # position_suspect stay recorded: those are the ones a user or an
    # automation actually looks back at.
    _unrecorded_attributes = _UNRECORDED_ATTRIBUTES

    def __init__(
        self,
        cover_id: str,
        remote_entry_id: str,
        config: BlindConfig,
        hub: ZemismartHub,
    ) -> None:
        """Bind one cover entity to its stored config, remote, and hub."""
        self._config: BlindConfig = config
        self._hub = hub
        self._cover_id = cover_id
        self._remote_entry_id = remote_entry_id
        self._attr_unique_id = cover_id
        # Full name, not a device-prefixed has_entity_name: deployed
        # friendly names predate the shared-device layout and must not gain
        # the remote's name as a prefix.
        self._attr_name = config.name
        self._stopped_by_heard = False
        self._unsubscribe_rx_listener: Callable[[], None] | None = None
        self._unsubscribe_mqtt_status: Callable[[], None] | None = None
        # Serializes this entity's own commands. On a leaf, without it a
        # set_position racing an unstarted open/close computes travel from a
        # stale estimate and physically overshoots. On an aggregate it covers
        # the single-frame commands only -- position fan-out deliberately runs
        # OUTSIDE the lock so STOP never queues behind an in-flight fan-out
        # (it cancels the fan-out instead).
        self._command_lock = asyncio.Lock()

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the remote's own device; covers are entities, not children.

        Identifiers only: the remote device's name/model belong to
        _ensure_remote_device, and repeating them here would let one cover
        rename the shared device.
        """
        return DeviceInfo(identifiers={(DOMAIN, self._config.remote.key)})

    # The three below are the contract a cover must satisfy to take part in RX.
    # Abstract rather than merely conventional because CoverEntity's metaclass
    # is an ABCMeta: a cover that forgets one now fails at instantiation
    # instead of at the first heard press.

    @abstractmethod
    def _on_heard_press(self, event: HeardEvent) -> None:
        """Apply one heard physical press of this remote to this cover."""

    @abstractmethod
    def _takeover_state(self) -> TakeoverCoverState:
        """Return the command state the hub needs to classify a takeover."""

    @abstractmethod
    def _invalidate_for_takeover(self) -> None:
        """Apply one hub-classified takeover invalidation to this cover."""

    def _register_rx_listener(self) -> None:
        """Subscribe this cover to heard presses on its own channels.

        Written once for both covers because the ARGUMENT LIST is the contract.
        Registering without `bases` is silent: RX classification falls back to
        opcode inference, which is a ten-sample empirical fit rather than
        protocol, so the loss shows up only on a remote outside that fit --
        which is exactly how #30 survived. One call site cannot drift from the
        other into that.
        """
        self._unsubscribe_rx_listener = self._hub.register_rx_listener(
            self._config.remote.key,
            frozenset(self._config.channels),
            self._on_heard_press,
            takeover_state=self._takeover_state,
            invalidate_takeover=self._invalidate_for_takeover,
            # The hub has no other route to a loaded remote's calibration.
            bases=self._config.remote.bases,
        )


class ZemismartCover(_ZemismartCoverEntity, RestoreEntity):
    """An assumed-state cover committed only after first RF dispatch."""

    def __init__(
        self,
        cover_id: str,
        remote_entry_id: str,
        config: BlindConfig,
        hub: ZemismartHub,
        coordinator: RemoteCoordinator | None = None,
    ) -> None:
        """Initialize one cover with its own travel-time estimate."""
        if config.travel_up is None or config.travel_down is None:
            msg = "leaf cover entities require travel calibration"
            raise ValueError(msg)
        super().__init__(cover_id, remote_entry_id, config, hub)
        self._travel_up: float = config.travel_up
        self._travel_down: float = config.travel_down
        self._coordinator = coordinator
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
        # position_confidence signals. `_position_anchored` is set only when a
        # travel actually COMPLETES against a hard limit; `_suspect` records an
        # untimed full travel cut short by an uncorroborated heard STOP and is
        # the one confidence signal that survives a restart. Both are cleared by
        # reaching a limit or going unknown -- they never overlap in practice.
        self._position_anchored = False
        self._suspect = False
        self._unverified_anchor_bridge: str | None = None
        self._unverified_anchor_command_id: str | None = None
        self._unverified_anchor_offline = False
        self._motion_token: object | None = None
        self._motion_task: asyncio.Task[None] | None = None
        self._last_bridge: str | None = None
        self._degraded = False
        self._intent_generation = 0
        self._restore_epoch = 0

    async def async_set_member_position(self, target: int) -> None:
        """Run one aggregate-delegated position move under this entity's lock."""
        async with self._command_lock:
            await self._async_set_position_locked(target)

    @property
    def available(self) -> bool:
        """Reflect whether any RF bridge is reachable over a live broker link."""
        return _rf_reachable(self.hass, self._hub)

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
    def position_confidence(self) -> str:
        """Report how far the current position estimate can be trusted.

        The ranking is `unknown < suspect < assumed < anchored`. `anchored` is
        the strongest thing this integration can honestly say and it is NOT
        motor confirmation: it means a full travel was transmitted, a local
        timer ran to completion, and no contradicting RF press was heard.

        `unknown` is deliberately derived from the entity state (no position)
        rather than tracked separately, so it is never a stored value. `suspect`
        -- an untimed full travel interrupted by an uncorroborated heard STOP --
        outranks `anchored`; the two never coexist, but suspect wins if they
        somehow did. See ``_apply_stop`` for how it is raised and
        ``_anchor_if_at_limit`` / ``_mark_unknown`` for how it clears.
        """
        if self._position is None:
            return CONFIDENCE_UNKNOWN
        if self._suspect:
            return CONFIDENCE_SUSPECT
        if self._position_anchored:
            return CONFIDENCE_ANCHORED
        return CONFIDENCE_ASSUMED

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
            _ATTR_POSITION_CONFIDENCE: self.position_confidence,
            _ATTR_POSITION_SUSPECT: self._suspect,
        }

    async def async_added_to_hass(self) -> None:
        """Restore a stopped estimate or reconstruct complete started motion."""
        await super().async_added_to_hass()
        # Handed to async_on_remove() BEFORE anything is acquired: HA runs these
        # callbacks when the entity is removed AND when it fails to finish being
        # added. The restore below raising used to leak every registration made
        # above it onto the hub for the lifetime of the entry, because HA logs
        # the entity-add failure without calling async_will_remove_from_hass().
        # Both releases are idempotent, so the removal path — which calls them
        # directly, and is the path the entity tests drive — cannot double-run
        # them against this one.
        self.async_on_remove(self._release_registrations)
        self.async_on_remove(self._cancel_motion_task)
        if self._coordinator is not None:
            self._coordinator.register_leaf(self._cover_id, self)
        self._register_rx_listener()
        self._hub.displaced_listeners.append(self._on_displaced)
        self._hub.emission_proof_listeners.append(self._on_emission_proof)
        self._hub.bridge_listeners.append(self._on_bridge_change)
        restore_guard = (self._intent_generation, self._restore_epoch)
        await self._async_restore_state(restore_guard)
        # Still subscribed LAST, but no longer for safety: restoration raising
        # once left a callback registered beforehand on the MQTT dispatcher
        # until a later platform unload, and ordering was the only defence
        # available to it. _release_registrations, registered through
        # async_on_remove() above, now covers this subscription wherever it is
        # acquired — there is simply no reason to move it earlier.
        self._unsubscribe_mqtt_status = _subscribe_rf_reachability(self.hass, self)

    def _restored_state_describes_this_cover(self, state: State) -> bool:
        """Return whether a persisted state still describes THIS cover.

        Both checks are about the config having moved out from under the
        stored state, and either one makes every value in it meaningless --
        so they answer one question and are asked in one place.
        """
        if state.attributes.get("remote") != self._config.remote_key or state.attributes.get(
            "channels"
        ) != list(self._config.channels):
            # The entry was re-pointed at different hardware (remote or
            # channel set changed in options): the persisted position and
            # motion describe the OLD physical target and must not be
            # assigned to the new one.
            return False
        # A topology change flipping this cover's role since the state was
        # persisted means the old model does not describe the new shape.
        restored_role: object = state.attributes.get("role", Role.LEAF.value)
        return restored_role == self._config.role.value

    def _restore_confidence_signals(self, state: State) -> None:
        """Reinstate the persisted doubts about the estimate, before any motion.

        Every signal here describes a STOPPED cover, which is why it runs ahead
        of the direction branches: a path that then marks the cover unknown
        clears them, and that is a strictly stronger statement than any of them.
        """
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
        # The incident's wrong estimate survived a restart verbatim; the doubt
        # about it must too. A suspect estimate always persists with direction 0
        # (a heard STOP froze it).
        self._suspect = state.attributes.get(_ATTR_POSITION_SUSPECT) is True

    async def _async_restore_state(self, restore_guard: tuple[int, int]) -> None:
        """Restore one stopped estimate or complete started motion."""
        state = await self.async_get_last_state()
        if state is None or restore_guard != (self._intent_generation, self._restore_epoch):
            return
        if not self._restored_state_describes_this_cover(state):
            return
        restored = _number(state.attributes.get(ATTR_CURRENT_POSITION))
        if restored is not None and 0 <= restored <= 100:
            self._position = restored
        self._restore_confidence_signals(state)
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
            if absolute_anchor or target in {0.0, 100.0}:
                # A travel that finished during downtime reached its hard
                # limit for POSITION purposes, but nobody was listening while
                # it ran -- no anchored, and a restored suspect stays.
                self._anchor_if_at_limit(observed=False)
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

    @callback
    def _release_registrations(self) -> None:
        """Drop every hub and coordinator registration this entity holds.

        Idempotent by construction: it runs from the removal path below and
        from the async_on_remove() callbacks, and a failed add followed by a
        later unload runs it twice.
        """
        if self._unsubscribe_rx_listener is not None:
            self._unsubscribe_rx_listener()
            self._unsubscribe_rx_listener = None
        if self._coordinator is not None:
            self._coordinator.unregister_leaf(self._cover_id)
        if self._on_displaced in self._hub.displaced_listeners:
            self._hub.displaced_listeners.remove(self._on_displaced)
        if self._on_emission_proof in self._hub.emission_proof_listeners:
            self._hub.emission_proof_listeners.remove(self._on_emission_proof)
        if self._on_bridge_change in self._hub.bridge_listeners:
            self._hub.bridge_listeners.remove(self._on_bridge_change)
        if self._unsubscribe_mqtt_status is not None:
            self._unsubscribe_mqtt_status()
            self._unsubscribe_mqtt_status = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the local timer and unregister direct group notifications."""
        self._release_registrations()
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

    def _anchor_if_at_limit(self, *, observed: bool = True) -> None:
        """Re-anchor a motion that actually ENDED against a hard limit.

        Both endpoints are physical stops: a cover that ran a travel out to 0
        or 100 is held there by the motor's own limit switch, so the estimate
        is corroborated by the hardware no matter what the command asked for.
        Any questioned anchor is settled by arriving there.

        Deliberately keyed on the OUTCOME rather than the commanded target.
        A group member whose own travel clamps to its limit reaches that hard
        stop even when the group was aimed somewhere in between, and it was
        previously left questioned because ``absolute_anchor`` records the
        group's intent (see ``_start_member_motion``).

        That shape is currently UNREACHABLE: the only production caller passes
        a ``group_target`` of 0 or 100, so ``absolute_anchor`` is already true
        whenever a member lands on a limit, and the previous intent-based gate
        covered every live case. This keeps the guarantee keyed on the physical
        fact rather than on that caller's argument staying an endpoint.

        Equally deliberately NOT applied to a position that merely reads 0 or
        100 without a travel behind it -- a restored estimate from a
        questioned origin would then launder itself into an anchored one, which
        is exactly what _mark_unknown exists to prevent.

        What this earns is `anchored`, never `verified` (#31): the motor
        confirms nothing back over a one-way protocol, so the claim is about
        the travel and the limit switch it ended against, not about evidence
        from the hardware.
        """
        if self._position in (0.0, 100.0):
            self._clear_unverified_anchor()
            if observed:
                # A completed travel to a hard limit is the ONLY thing that
                # earns `anchored`, and it also settles any suspect doubt:
                # whatever a heard STOP left ambiguous, the blind has now
                # physically reached and rests against its limit switch.
                #
                # OBSERVED means RX was live for the whole travel, so a real
                # STOP press would have been heard and turned into suspect or
                # an interruption. A completion that happened during HA's own
                # downtime carries no such witness -- any press in that gap,
                # real or phantom, was invisible -- so it keeps the position
                # but earns no anchored and settles no doubt. (Residual even
                # when observed: a listener is deaf ~one slot after each
                # capture, so a press CAN be missed. That risk is identical
                # for commanded and heard travels, which is why both earn
                # anchored rather than only our own.)
                self._position_anchored = True
                self._suspect = False

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
        # Any interruption ends the moving estimate BEFORE it reaches a limit, so
        # the prior `anchored` no longer holds. Runs only on interruption:
        # clean completion goes through _async_track_motion, never here, so the
        # anchor it just set survives. `_suspect` is intentionally untouched --
        # it clears only at a completed limit or unknown, not when a fresh
        # travel starts over the doubtful estimate.
        self._position_anchored = False

    def _mark_unknown(self) -> None:
        """Discard ambiguous motion after a lifecycle timeout or recovery gap."""
        self._cancel_motion_task()
        self._position = None
        self._clear_motion()
        self._clear_unverified_anchor()
        self._position_anchored = False
        self._suspect = False
        self._degraded = True

    @callback
    def invalidate_for_cancelled_command(self) -> None:
        """Invalidate this leaf after a command whose outcome we will never see.

        Used by cancellation and by ack/started timeouts, and by an aggregate
        for members that never issued the command themselves. All three share
        one shape: a frame that MAY be on air, and no path left by which this
        entity will learn what it did.

        The aggregate's frame addresses the whole channel set, so if it had
        already published, this member moved -- but the command was never this
        entity's, so nothing here would otherwise record the loss (#28).

        The epoch bump has to happen with the invalidation, not after it: leaves
        register before awaiting restored state, so a member whose restore is
        still in flight would pass its guard in _async_restore_state and put the
        cached confident position straight back over this.

        UNCONDITIONAL, deliberately, and two rejected attempts are why.

        It is tempting to spare a cover whose model a physical press has just
        replaced -- the press looks like better evidence than a cancelled
        command's absence. It is not, because the cancellation says nothing
        about RF ordering. The hub keeps an already-published command alive
        after its caller is cancelled (a published frame is on air whether or
        not anyone still awaits it), so the bridge may first-dispatch it AFTER
        the press was heard, with no cover task left to observe the result. The
        blind would then move under a command nobody is tracking while the cover
        confidently reports the model the earlier press installed.

        Keying the skip on `_intent_generation` failed for a second, simpler
        reason: it advances for every intersecting press, including a STOP heard
        with nothing to stop, which preserves the previous estimate without
        earning it.

        Neither a generation nor a model revision can express what would
        actually be needed here: proof that the cancelled command's own
        `started_at` preceded the heard event. Until the model carries that,
        `unknown` is the only honest answer -- which is the same standard
        `_async_transmit` already applies to an ack timeout.
        """
        self._restore_epoch += 1
        self._mark_unknown()
        self.async_write_ha_state()

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
        # Live motion supersedes a still-pending restore: a member registers
        # with the coordinator BEFORE its restore await, so a group command
        # can commit here first — the stale persisted snapshot must then
        # never be applied over it.
        self._restore_epoch += 1
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
        # The motion's own start write has just happened at the call site; the
        # first throttled progress write is due one interval after it.
        last_write = WALL_CLOCK()
        while self._motion_token is token:
            remaining = self._motion_deadline - WALL_CLOCK()
            if remaining <= 0:
                break
            await asyncio.sleep(min(POSITION_UPDATE_INTERVAL_SECONDS, remaining))
            if self._motion_token is not token:
                return
            # The estimate is re-integrated every tick — that is what keeps the
            # position smooth — but only the throttled subset of those ticks is
            # written. Start, stop, invalidation and completion all write
            # immediately from their own call sites, so nothing a user or an
            # automation waits on is delayed by this.
            self._sync_position()
            now = WALL_CLOCK()
            if now - last_write >= _PROGRESS_WRITE_INTERVAL_SECONDS:
                last_write = now
                self.async_write_ha_state()
        if self._motion_token is not token:
            return
        self._position = self._motion_target
        # Ran its whole configured duration plus margin. If that landed on a
        # limit the estimate now has a genuine physical reference behind it.
        self._anchor_if_at_limit()
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
                owner=self._remote_entry_id,
            )
        except (CommandAckTimeoutError, CommandStartedTimeoutError) as exc:
            # The frame MAY have reached RF; only unknown is honest. Aggregates
            # containing this leaf re-derive through the coordinator.
            #
            # Routed through the shared helper for its epoch bump: HA inserts an
            # entity into the service mapping BEFORE awaiting
            # async_added_to_hass, so a cover can be commanded while its own
            # restore is still suspended. Marking unknown without advancing the
            # epoch let that restore resume, pass its guard, and reinstall the
            # cached position -- reporting a specific estimate after a command
            # that may have reached the air. Identical hazard to the
            # cancellation path, and it wants the identical answer.
            self.invalidate_for_cancelled_command()
            _LOGGER.warning("Command timed out for %s: %s", self._config.name, exc)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_timeout",
            ) from exc
        except asyncio.CancelledError:
            # CancelledError derives from BaseException, so it used to pass
            # straight through every handler and unwind without touching the
            # model -- while the hub deliberately keeps an already-published
            # command alive, because a published frame is on air whether or not
            # anyone is still awaiting it. The blind moved and nobody recorded
            # it, leaving a confident, specific, WRONG position (#28). Reachable
            # from an entry reload mid-move, `script.turn_off`, an automation in
            # mode: restart, and entity unload.
            #
            # Pessimistic on purpose: cancellation BEFORE publication could
            # safely keep the estimate, but only the hub knows which side of
            # publication the cancellation landed on. Narrowing it needs
            # `_QueuedCommand.published` surfaced from models.py.
            #
            # The epoch bump is what makes the invalidation stick: leaves
            # register before awaiting restored state, so a still-pending
            # _async_restore_state would otherwise pass its guard and overwrite
            # this with the cached confident position. Same reason _apply_stop
            # bumps it -- a live invalidation supersedes a pending restore.
            self.invalidate_for_cancelled_command()
            raise
        except HomeAssistantError:
            # The transport refusing the publish outright (MQTT unavailable).
            # It is already a translated, user-facing error, so it is re-raised
            # untouched — but the bridge is still degraded.
            self._degraded = True
            self.async_write_ha_state()
            raise
        except _TRANSPORT_FAILURES as exc:
            self._degraded = True
            self.async_write_ha_state()
            _LOGGER.warning("Command failed for %s: %s", self._config.name, exc)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=_failure_translation_key(exc),
            ) from exc
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

    async def async_reanchor(self, endpoint: str) -> None:
        """Re-anchor by driving a full travel to a hard endpoint (recovery).

        Deliberately no parallel motion path: it reuses the normal full-travel
        command internals, so ledger registration, commanded-start, air
        arbitration and coalescing all apply, and completion re-anchors through
        the same outcome-based _anchor_if_at_limit. It needs no known estimate,
        which is the whole point -- it recovers a cover whose position is
        unknown.
        """
        if endpoint == ENDPOINT_OPEN:
            await self.async_open_cover()
        else:
            await self.async_close_cover()

    def _apply_stop(self, at: float, *, provenance: str) -> None:
        """Freeze this cover at one commanded or heard STOP."""
        if provenance not in {"commanded", "heard"}:
            msg = f"unsupported STOP provenance: {provenance}"
            raise ValueError(msg)
        # Read the interrupted motion's shape BEFORE _interrupt_motion clears it.
        # An UNTIMED full travel runs to the motor's own limit switch; a heard
        # STOP we cannot corroborate leaves the blind either frozen here or
        # resting at that limit -- opposite ground truths. A timed move was
        # going to stop near here anyway, so it is far less suspect.
        suspect_travel = (
            provenance == "heard"
            and self._direction != 0
            and not self._motion_timed
            and self._motion_target in (0.0, 100.0)
        )
        # A live freeze also supersedes a still-pending restore snapshot.
        self._restore_epoch += 1
        self._interrupt_motion(at)
        if suspect_travel:
            # Survives a restart via _ATTR_POSITION_SUSPECT; clears only at a
            # completed hard limit (_anchor_if_at_limit) or unknown
            # (_mark_unknown), which _reconcile_unverified_anchor may trigger
            # next -- and unknown is the stronger, correct statement there.
            self._suspect = True
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
            # A ServiceValidationError, not a HomeAssistantError: nothing
            # failed, the caller asked for a partial move the model cannot
            # compute yet.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="position_unknown",
            )
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


class ZemismartAggregateCover(_ZemismartCoverEntity):
    """A cover whose state derives from its leaf members.

    RF behavior matches the retired group entries: open/close/stop transmit
    ONE frame addressed to the full channel set; position commands fan out to
    each member's own timed positioning. The aggregate owns no position model
    of its own — members are the single source of truth.
    """

    def __init__(
        self,
        cover_id: str,
        remote_entry_id: str,
        config: BlindConfig,
        hub: ZemismartHub,
        coordinator: RemoteCoordinator,
    ) -> None:
        """Initialize one aggregate bound to its coordinator topology."""
        super().__init__(cover_id, remote_entry_id, config, hub)
        self._coordinator = coordinator
        self._last_command_bridge: str | None = None
        self._last_command_id: str | None = None
        self._last_command_button: Button | None = None
        self._last_command_at = 0.0
        self._fanout_tasks: set[asyncio.Task[None]] = set()

    async def async_added_to_hass(self) -> None:
        """Register with the coordinator and the hub's takeover machinery."""
        await super().async_added_to_hass()
        # Registered before anything is acquired, for the reason spelled out on
        # ZemismartCover.async_added_to_hass: HA runs these on a failed add too,
        # which async_will_remove_from_hass() never sees.
        self.async_on_remove(self._release_registrations)
        self.async_on_remove(self._cancel_fanout)
        self._coordinator.register_aggregate(self._cover_id, self)
        self._register_rx_listener()
        self._unsubscribe_mqtt_status = _subscribe_rf_reachability(self.hass, self)

    @callback
    def _release_registrations(self) -> None:
        """Drop every hub and coordinator registration this aggregate holds.

        Idempotent: it runs from the removal path below and from the
        async_on_remove() callbacks.
        """
        if self._unsubscribe_rx_listener is not None:
            self._unsubscribe_rx_listener()
            self._unsubscribe_rx_listener = None
        if self._unsubscribe_mqtt_status is not None:
            self._unsubscribe_mqtt_status()
            self._unsubscribe_mqtt_status = None
        self._coordinator.unregister_aggregate(self._cover_id)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister and cancel any in-flight fan-out."""
        self._release_registrations()
        self._cancel_fanout()
        await super().async_will_remove_from_hass()

    def _members(self) -> tuple[ZemismartCover, ...]:
        """Return the live leaf entities this aggregate derives from."""
        return cast(
            "tuple[ZemismartCover, ...]",
            self._coordinator.members_of(self._cover_id),
        )

    def _failure_members(
        self,
        issued_members: tuple[ZemismartCover, ...],
    ) -> tuple[ZemismartCover, ...]:
        """Return every leaf a failed group frame could have moved.

        The union of the members present when the frame was issued and those
        present now. A leaf that deregistered mid-flight is gone from
        `_members()` but its channels were still addressed; a leaf that joined
        mid-flight was never in the snapshot but is addressed too. Order is kept
        stable and duplicates dropped.
        """
        seen: dict[int, ZemismartCover] = {}
        for member in (*issued_members, *self._members()):
            seen.setdefault(id(member), member)
        return tuple(seen.values())

    def _members_cover_every_channel(self) -> bool:
        """Return whether the live members account for ALL of our channels.

        The laminar topology does not require them to. `members_of` returns the
        live configured LEAVES strictly inside this aggregate, and nothing
        guarantees their union equals ours: an aggregate over {1..6} whose
        leaves are {1,2,3}, {4} and {5} has no model for channel 6 at all, and
        `async_setup_entry` skips any cover whose config fails to derive, so a
        member can also be missing at runtime.

        Left unchecked, the aggregate reported a confident position -- possibly
        `anchored` -- for hardware it had no model of, and `set_position` moved
        the channels it could and returned success (#32).
        """
        covered = frozenset(
            channel for member in self._members() for channel in member._config.channels
        )
        return covered == frozenset(self._config.channels)

    @property
    def available(self) -> bool:
        """Available while RF works and at least one member is registered."""
        return bool(self._members()) and _rf_reachable(self.hass, self._hub)

    @property
    def current_cover_position(self) -> int | None:
        """Return the channel-weighted member mean, or None if any is unknown.

        Unknown members are NOT skipped (#32): averaging the rest produced a
        confident, specific number describing only part of the hardware the
        aggregate claims to represent, and that number is what dashboards and
        automations read. `is_closed` already returns None on a mixed state;
        this now matches its honesty.

        Weighted by channel count because a member is a motor set, not a vote:
        a leaf covering {1,2} moves twice as much hardware as one covering {3},
        so an unweighted mean reported the midpoint of the two LEAVES rather
        than of the three MOTORS.
        """
        if not self._members_cover_every_channel():
            return None
        travelled = 0.0
        channels = 0
        for member in self._members():
            position = member.current_cover_position
            if position is None:
                return None
            weight = len(member._config.channels)
            travelled += position * weight
            channels += weight
        if not channels:
            return None
        return round(travelled / channels)

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
        # `False` needs no completeness: one member demonstrably open makes the
        # GROUP open whatever the unmodelled channels are doing.
        if any(state is False for state in states):
            return False
        # `True` does. This is the entity's PRIMARY state, so an aggregate over
        # {1,2,3,4} whose live members cover only {1,2,3} would otherwise be
        # published to HA as `closed` while channel 4 is entirely unmodelled --
        # the same hole current_cover_position and position_confidence already
        # close (#32).
        if all(state is True for state in states) and self._members_cover_every_channel():
            return True
        return None

    @property
    def position_confidence(self) -> str:
        """Derive confidence from members -- the worst known value wins.

        One rule throughout: no position means `unknown`. That holds for a leaf
        with no estimate, for a group whose members do not cover its channels,
        and for a group with an unknown member -- because `current_cover_position`
        returns None in every one of those cases.

        The older rule capped this at `assumed` instead, which was right while
        the position was the mean of the members that HAD one. Once #32 made a
        single unknown member withhold the whole position, `assumed` started
        claiming an estimate that no longer existed, and an automation gating on
        `position_confidence != 'unknown'` would act on nothing.

        A suspect member still marks the whole group suspect.
        """
        members = list(self._members())
        if not self._members_cover_every_channel():
            # `unknown`, not `assumed`: current_cover_position returns None in
            # this state, and the leaf's rule is that no position means unknown.
            # Reporting `assumed` implied there was an estimate that merely
            # lacked corroboration, so an automation gating on
            # `position_confidence != 'unknown'` would act on a position that
            # does not exist.
            return CONFIDENCE_UNKNOWN
        confidences = [member.position_confidence for member in members]
        if not confidences:
            return CONFIDENCE_UNKNOWN
        # Suspect first: a member frozen by an uncorroborated heard STOP is a
        # stronger statement about the group than a sibling merely being blank.
        if CONFIDENCE_SUSPECT in confidences:
            return CONFIDENCE_SUSPECT
        if CONFIDENCE_UNKNOWN in confidences:
            return CONFIDENCE_UNKNOWN
        if CONFIDENCE_ASSUMED in confidences:
            return CONFIDENCE_ASSUMED
        return CONFIDENCE_ANCHORED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose topology metadata for diagnostics and restore discrimination."""
        return {
            "channels": list(self._config.channels),
            "remote": self._config.remote_key,
            "role": self._config.role.value,
            _ATTR_POSITION_CONFIDENCE: self.position_confidence,
        }

    @callback
    def _on_heard_press(self, event: HeardEvent) -> None:
        """Track heard STOPs for takeover; members model their own motion."""
        # Members receive their own callbacks and model the press; the
        # aggregate's displayed state re-derives through the coordinator.
        # Only the takeover flag is aggregate-owned: a physical STOP covering
        # the whole set halted this aggregate's own command, and the hub's
        # takeover truth table must not re-invalidate a heard-stopped cover.
        if event.button == "STOP" and frozenset(self._config.channels) <= event.chans:
            self._stopped_by_heard = True

    def _takeover_state(self) -> TakeoverCoverState:
        """Expose the last single-frame command for hub takeover disarms.

        The command identity EXPIRES after the untimed drain window: the hub
        treats unknown command ids as takeover-live, so reporting a
        long-retired command forever would spawn pointless disarm retries on
        every later physical press.
        """
        expired = WALL_CLOCK() > self._last_command_at + _UNTIMED_DISARM_DRAIN_SECONDS
        if self._last_command_bridge is None or self._last_command_id is None or expired:
            return TakeoverCoverState(
                bridge_id=None,
                command_id=None,
                button=None,
                disarm_deadline=None,
                stopped_by_heard=self._stopped_by_heard,
            )
        return TakeoverCoverState(
            bridge_id=self._last_command_bridge,
            command_id=self._last_command_id,
            button=self._last_command_button,
            disarm_deadline=WALL_CLOCK() + _UNTIMED_DISARM_DRAIN_SECONDS,
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
        # Membership is read TWICE and the two are unioned. Neither end alone is
        # right: a leaf registers before awaiting its restore, so one can join
        # while the frame is in flight; and a leaf can deregister on unload
        # during the same window, vanishing from `_members()` while the frame
        # that addressed its channels is already on air. Snapshot-only missed
        # the first, current-only missed the second.
        issued_members = self._members()
        try:
            result = await self._hub.async_transmit(
                self._config,
                button,
                owner=self._remote_entry_id,
            )
        except asyncio.CancelledError:
            # Same hazard as the leaf's transmit (#28), but this frame addresses
            # the WHOLE channel set: if it was already published, every member
            # moved. The aggregate owns no position model, so the members are
            # the only place that loss can be recorded -- and they cannot learn
            # it themselves, because this command was never theirs.
            #
            # Each member's epoch is bumped for the same reason the leaf bumps
            # its own: a member whose restore is still pending would otherwise
            # overwrite this invalidation with its cached position.
            for member in self._failure_members(issued_members):
                member.invalidate_for_cancelled_command()
            self.async_write_ha_state()
            raise
        except (CommandAckTimeoutError, CommandStartedTimeoutError) as exc:
            # The leaf invalidates itself on a timeout; the aggregate has to do
            # it for its members. The frame MAY have reached RF, and this
            # command was never any member's own, so nothing else records the
            # loss: _async_move_full never starts member tracking and
            # async_stop_cover never freezes it, leaving members integrating
            # through a STOP that may have fired, still reporting `anchored`.
            _LOGGER.warning("Command timed out for %s: %s", self._config.name, exc)
            for member in self._failure_members(issued_members):
                member.invalidate_for_cancelled_command()
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_timeout",
            ) from exc
        except _TRANSPORT_FAILURES as exc:
            _LOGGER.warning("Command failed for %s: %s", self._config.name, exc)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=_failure_translation_key(exc),
            ) from exc
        if result == "superseded":
            return None
        self._last_command_bridge = result.bridge.bridge_id
        self._last_command_id = result.command_id
        self._last_command_button = button
        self._last_command_at = WALL_CLOCK()
        self._stopped_by_heard = False
        return result

    async def _async_move_full(self, button: Button, direction: int, target: float) -> None:
        """Run one group frame and start every member's own motion model."""
        # Snapshot BEFORE the transmit await: a physical press heard while
        # this frame is queued is NEWER intent for that member, and the older
        # group command must not overwrite it (mirrors the leaf-side
        # intent-generation checks around every transmit).
        generations = {member: member._intent_generation for member in self._members()}
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
            generation = generations.get(member)
            if generation is not None and generation != member._intent_generation:
                continue
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
        # A full-set command supersedes any in-flight position fan-out: a
        # member's timed frame published after the group frame would displace
        # it channel-by-channel on the bridge.
        self._cancel_fanout()
        async with self._command_lock:
            await self._async_move_full("UP", 1, 100.0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close every channel with one frame; members model their own travel."""
        del kwargs
        self._cancel_fanout()
        async with self._command_lock:
            await self._async_move_full("DOWN", -1, 0.0)

    async def async_reanchor(self, endpoint: str) -> None:
        """Re-anchor the whole group with one endpoint frame (recovery).

        Reuses the aggregate's own full open/close, so it is exactly one group
        frame and every member re-anchors through its own outcome-based logic --
        identical to a manual full travel on the aggregate.
        """
        if endpoint == ENDPOINT_OPEN:
            await self.async_open_cover()
        else:
            await self.async_close_cover()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Cancel fan-out, stop every channel with one frame, freeze members."""
        del kwargs
        self._cancel_fanout()
        async with self._command_lock:
            generations = {member: member._intent_generation for member in self._members()}
            ack = await self._async_transmit("STOP")
            if ack is None:
                return
            for member in self._members():
                generation = generations.get(member)
                if generation is not None and generation != member._intent_generation:
                    # A press heard during the STOP await is newer intent for
                    # this member; its own heard model wins the freeze.
                    continue
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
        registered = self._members()
        members = [member for member in registered if member.available]
        skipped = [member for member in registered if not member.available]
        if not members:
            names = ", ".join(member._config.name for member in skipped) or "none"
            # Nothing failed: every member is simply unreachable right now.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_members_available",
                translation_placeholders={"unavailable": names},
            )
        # Nor can a partial group be positioned at all: fanning out to the
        # members we have would move some of this aggregate's channels and
        # leave the rest, then report success (#32).
        if not self._members_cover_every_channel():
            covered = frozenset(
                channel for member in registered for channel in member._config.channels
            )
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="aggregate_incomplete",
                translation_placeholders={
                    "missing": ", ".join(
                        str(channel)
                        for channel in sorted(frozenset(self._config.channels) - covered)
                    )
                },
            )
        # PREFLIGHT before any frame reaches the air (#32). A member with no
        # estimate cannot be positioned, and discovering that inside the
        # fan-out meant reporting failure AFTER part of the group had
        # physically moved -- a state neither the caller nor the model
        # intended, and one that makes a retry hazardous: the already-moved
        # members would move again from their new positions.
        #
        # It cannot close the window entirely -- a heard press can invalidate a
        # member between this check and its frame -- but that member then fails
        # in delegate() as before, which is the narrow residual, not the whole
        # class.
        unpositionable = [member for member in members if member.current_cover_position is None]
        if unpositionable:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="member_position_unknown",
                translation_placeholders={
                    "members": ", ".join(member._config.name for member in unpositionable)
                },
            )
        failures: list[str] = []

        async def delegate(member: ZemismartCover) -> None:
            try:
                await member.async_set_member_position(target)
            except HomeAssistantError as exc:
                # The member NAME only. Interpolating `exc` rendered a
                # translated HomeAssistantError to its English fallback and
                # then nested that inside another translated message, so a
                # non-English user got English text either way (#37). The
                # detail is logged instead.
                _LOGGER.warning("Positioning failed for %s: %s", member._config.name, exc)
                failures.append(member._config.name)

        tasks = [
            self.hass.async_create_task(
                delegate(member),
                f"Zemismart {self._config.name} fan-out",
            )
            for member in members
        ]
        self._fanout_tasks.update(tasks)
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._fanout_tasks.difference_update(tasks)
        # Every gather result is inspected (#33). `return_exceptions=True` is
        # required here -- one member failing must not abandon the others
        # mid-fan-out -- but the results used to be discarded, so a TypeError
        # or AttributeError anywhere in a member's command path produced no
        # traceback, no service failure, and a group where some blinds moved
        # and some did not. delegate() collects only HomeAssistantError;
        # anything else reaching this list is by definition unexpected.
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                # Preserve #28's semantics: the cancelled member already marked
                # itself unknown, and cancellation must propagate as
                # cancellation rather than be reported as a member failure.
                raise result
        for result in results:
            if isinstance(result, BaseException):
                # Re-raised, not wrapped: the original traceback is the whole
                # point of surfacing it.
                raise result
        if failures:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="position_delegation_failed",
                translation_placeholders={"members": ", ".join(failures)},
            )
