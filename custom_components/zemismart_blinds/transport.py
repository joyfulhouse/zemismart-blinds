"""MQTT transport models for Zemismart Blinds."""

from __future__ import annotations

import asyncio
import json
import logging
import math as math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum as StrEnum
from typing import TYPE_CHECKING, Final, Literal, cast

from .air import MAX_AIR_HOLD_MS, AirArbiter, AirMode, plan_for_body
from .bridge_registry import (
    _BRIDGE_MAX_ENTRIES as _BRIDGE_REGISTRY_MAX_ENTRIES,
)
from .bridge_registry import (
    BridgeInfo,
    BridgeRegistry,
    NoOnlineBridgeError,
)
from .calibrations import KNOWN_CALIBRATIONS as KNOWN_CALIBRATIONS
from .codec import (
    CommandBases,
    decode_b0,
    encode_b0,
    make_payload,
    validate_b0_frame,
)
from .codec import (
    validate_channels as validate_channels,
)
from .config_models import (
    _COERCION_ERRORS as _COERCION_ERRORS,
)
from .config_models import (
    MAX_COALESCE_WINDOW_MS as MAX_COALESCE_WINDOW_MS,
)
from .config_models import (
    MAX_REPEATS,
    MIN_REPEATS,
    BlindConfig,
    RemoteConfig,
    RemoteIdentity,
)
from .config_models import (
    MAX_TRAVEL_SECONDS as MAX_TRAVEL_SECONDS,
)
from .config_models import (
    CoverConfig as CoverConfig,
)
from .config_models import (
    Role as Role,
)
from .config_models import (
    _as_float as _as_float,
)
from .config_models import (
    _bases_as_dict as _bases_as_dict,
)
from .config_models import (
    _bases_from_mapping as _bases_from_mapping,
)
from .config_models import (
    _number_scalar as _number_scalar,
)
from .config_models import (
    _optional_travel as _optional_travel,
)
from .config_models import (
    _required as _required,
)
from .config_models import (
    _validate_pacing as _validate_pacing,
)
from .config_models import (
    derive_role as derive_role,
)
from .config_models import (
    laminar_conflict as laminar_conflict,
)
from .config_models import (
    member_covers as member_covers,
)
from .config_models import (
    parse_channels as parse_channels,
)
from .config_models import (
    parse_hex as parse_hex,
)
from .config_models import (
    whole_number as whole_number,
)
from .const import (
    CONF_AREA_ID as CONF_AREA_ID,
)
from .const import (
    CONF_BASE_DOWN as CONF_BASE_DOWN,
)
from .const import (
    CONF_BASE_STOP as CONF_BASE_STOP,
)
from .const import (
    CONF_BASE_TRAILER as CONF_BASE_TRAILER,
)
from .const import (
    CONF_BASE_UP as CONF_BASE_UP,
)
from .const import (
    CONF_CHANNELS as CONF_CHANNELS,
)
from .const import (
    CONF_COALESCE_WINDOW_MS as CONF_COALESCE_WINDOW_MS,
)
from .const import (
    CONF_COVER_ID as CONF_COVER_ID,
)
from .const import (
    CONF_COVERS as CONF_COVERS,
)
from .const import (
    CONF_NAME as CONF_NAME,
)
from .const import (
    CONF_PREFIX as CONF_PREFIX,
)
from .const import (
    CONF_REMOTE_ID as CONF_REMOTE_ID,
)
from .const import (
    CONF_REPEATS as CONF_REPEATS,
)
from .const import (
    CONF_TRAVEL_DOWN as CONF_TRAVEL_DOWN,
)
from .const import (
    CONF_TRAVEL_UP as CONF_TRAVEL_UP,
)
from .const import (
    DEFAULT_COALESCE_WINDOW_MS as DEFAULT_COALESCE_WINDOW_MS,
)
from .const import (
    MQTT_ROOT,
)
from .state_sync import (
    BridgeClock,
    CommandLedger,
    HeardEvent,
    LedgerFrameSpec,
    StateSyncConsumer,
    frame_signature,
)

if TYPE_CHECKING:
    from .air import AirPlan
    from .coordinator import RemoteCoordinator
    from .state_sync import BasesResolver

__all__ = [
    "DEFAULT_ACK_TIMEOUT_SECONDS",
    "DEFAULT_STARTED_TIMEOUT_SECONDS",
    "MAX_COALESCE_WINDOW_MS",
    "MAX_REPEATS",
    "MAX_TRAVEL_SECONDS",
    "MIN_REPEATS",
    "BlindConfig",
    "BridgeInfo",
    "BridgeRegistry",
    "Button",
    "Clock",
    "CommandAck",
    "CommandAckTimeoutError",
    "CommandDisplacedError",
    "CommandIdFactory",
    "CommandQueueFullError",
    "CommandRejectedError",
    "CommandResult",
    "CommandStartedTimeoutError",
    "CommandStatusValue",
    "CoverConfig",
    "DomainRuntime",
    "NoOnlineBridgeError",
    "Publisher",
    "RemoteConfig",
    "RemoteIdentity",
    "RemoteRuntime",
    "Role",
    "TakeoverCoverState",
    "Unsubscriber",
    "ZemismartHub",
    "derive_role",
    "laminar_conflict",
    "member_covers",
    "parse_channels",
    "parse_hex",
    "whole_number",
]

# The legacy namespace is retained deliberately by the pure-move contract.
_LOGGER = logging.getLogger("custom_components.zemismart_blinds.models")
# Exception tuples are bound to names rather than written inline. `ruff format`
# at this project's target-version rewrites the parenthesised `except (A, B):`
# straight back into the bare PEP 758 `except A, B:`, which is a SyntaxError on
# Python < 3.14 -- so a manual or zip install onto an older core fails at import
# with no useful message. A named tuple is left alone by the formatter and
# parses everywhere (#43).
_PAYLOAD_ERRORS: Final = (UnicodeDecodeError, json.JSONDecodeError)

Button = Literal["UP", "DOWN", "STOP", "TRAILER"]
Publisher = Callable[[str, str], Awaitable[None]]
Unsubscriber = Callable[[], None]
Clock = Callable[[], float]
CommandIdFactory = Callable[[], str]
CommandStatusValue = Literal["accepted", "rejected"]

DEFAULT_ACK_TIMEOUT_SECONDS: Final = 2.0
DEFAULT_STARTED_TIMEOUT_SECONDS: Final = 30.0
_DISPLACED_MEMORY_SECONDS: Final = 60.0
_DISPLACED_MAX_ID_LENGTH: Final = 64
_DISPLACED_MAX_ENTRIES: Final = 256
_BRIDGE_AFFINITY_SECONDS: Final = 120.0
_BRIDGE_MAX_ID_LENGTH: Final = 64
# Single definition shared with BridgeRegistry: the RX-admission cap and the
# registry cap were one constant before the #39 split and must not drift.
_BRIDGE_MAX_ENTRIES: Final = _BRIDGE_REGISTRY_MAX_ENTRIES
_BRIDGE_CLOCK_CAP: Final = 64
_EMISSION_PROOF_MEMORY_SECONDS: Final = 60.0
_EMISSION_PROOF_MAX_ID_LENGTH: Final = 64
_EMISSION_PROOF_MAX_ENTRIES: Final = 256
_DISARM_RETRY_SECONDS: Final = 0.25
_DISARM_RETRY_MAX_SECONDS: Final = 5.0
_PRESTART_DISARM_DEADLINE_SECONDS: Final = 10.0
# Emission envelope charged to ONE repeat of a movement frame: the bridge's
# airtime-paced scheduler holds the air for serialization + ~550 ms of AOK
# airtime (embedded hardware repeat 8) + margin per copy, and only then
# dispatches the next. 1 s per repeat carries that with headroom.
_LEDGER_REPEAT_AIRTIME_MS: Final = 1_000
# Floor on the whole train, so the production repeats=2 envelope is exactly the
# 2 s this was fixed at before repeats was taken into account.
_LEDGER_FRAME_AIRTIME_MS: Final = 2_000
_PRESS_SEQ_CAP: Final = 512
# Same bound as _PRESS_SEQ_CAP beside it: 16 channels per remote, so this is
# 32 identities' worth of publish state. Raw frames can introduce arbitrary
# remote keys, which is what made the unbounded version reachable (#34).
_PUBLISH_SEQ_CAP: Final = 512
# Movements and raw frames the hub will hold outstanding at once (queued +
# fast lane + in flight). cover.py's per-entity _command_lock already
# serialises real usage one-per-cover, so a household fleet never approaches
# this; the admin send_raw service bypasses those locks entirely and is the
# surface this bounds. STOP is deliberately NOT counted against the cap and
# can never be rejected by it -- see _async_enqueue.
_MAX_OUTSTANDING_COMMANDS: Final = 128
_UINT32_MAX: Final = (1 << 32) - 1
_MAX_STARTED_AGE_MS: Final = 7_200_000
_MILLISECONDS_PER_SECOND: Final = 1_000.0
_STARTED_PROJECTION_TOLERANCE_SECONDS: Final = 30.0


def _ledger_airtime_ms(repeats: object) -> int:
    """Return the emission envelope one frame's full repeat train occupies.

    The bridge sends `repeats` copies of every frame, so a fixed envelope
    under-covers the tail of any command configured above the production
    default: our own late copies then fall outside the ledger window, get
    classified as a PHYSICAL remote press, and spuriously take the cover over.
    Widening is the safe direction -- an over-wide window only suppresses
    takeover detection while we are demonstrably still transmitting.
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int):
        return _LEDGER_FRAME_AIRTIME_MS
    bounded = min(max(repeats, MIN_REPEATS), MAX_REPEATS)
    return max(_LEDGER_FRAME_AIRTIME_MS, bounded * _LEDGER_REPEAT_AIRTIME_MS)


def _strict_uint32(value: object) -> int | None:
    """Return a real uint32 integer, rejecting booleans and coercions."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _UINT32_MAX:
        return None
    return value


class CommandAckTimeoutError(RuntimeError):
    """Raised when a bridge may have received a command but did not acknowledge it."""


class CommandStartedTimeoutError(RuntimeError):
    """Raised when an admitted command does not report its first RF dispatch."""


class CommandRejectedError(RuntimeError):
    """Raised when a bridge explicitly rejects a correlated command."""


class CommandQueueFullError(RuntimeError):
    """Raised when outstanding movement and raw work is already at its cap.

    A ``RuntimeError`` so both command boundaries -- the cover entity and the
    admin ``send_raw`` service -- already translate it into a user-facing
    ``HomeAssistantError`` rather than a traceback. STOP never raises this.
    """


class CommandDisplacedError(RuntimeError):
    """Raised internally when a newer overlapping command displaced this one.

    Never surfaces to callers: the hub translates it into the ``superseded``
    command result, exactly like queue-level supersession.
    """


def _cancel_waiters(commands: Iterable[_QueuedCommand]) -> None:
    """Cancel every caller still awaiting these commands, on final teardown."""
    for command in commands:
        for future in command.futures:
            future.cancel()


@dataclass(frozen=True, slots=True)
class _BridgeStatus:
    """One accepted/rejected result correlated by bridge and command ID."""

    status: CommandStatusValue
    acknowledged_at: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _StartedStatus:
    """Carry one RF start in both publication and live clock domains."""

    started_at: float
    started_at_monotonic: float


@dataclass(slots=True)
class _PendingStatuses:
    """Lifecycle waiters and RF identity for one correlated command."""

    admission: asyncio.Future[_BridgeStatus]
    started: asyncio.Future[_StartedStatus]
    remote_key: str | None
    channels: frozenset[int]


@dataclass(slots=True)
class _DisarmRequest:
    """One deadline-bounded disarm waiter shared by duplicate requests."""

    bridge_id: str
    command_id: str
    waiter: asyncio.Future[None]
    deadline: float
    loop_deadline: float
    command_channels: frozenset[int] = frozenset()
    pressed_channels: set[int] = field(default_factory=set)
    remote_key: str | None = None
    command_button: str | None = None
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class TakeoverCoverState:
    """Expose one cover's live state to takeover targeting and resolution."""

    bridge_id: str | None
    command_id: str | None
    button: Button | None
    disarm_deadline_monotonic: float | None
    stopped_by_heard: bool


@dataclass(slots=True)
class _TakeoverTarget:
    """Merge ledger and cover-owned evidence for one live command."""

    bridge_id: str
    command_id: str
    channels: frozenset[int]
    button: str
    confirmed: bool | None
    owned_deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True, eq=False)
class _RxListener:
    """Bind one cover callback to its configured remote metadata."""

    remote_key: str
    channels: frozenset[int]
    callback: Callable[[HeardEvent], None]
    takeover_state: Callable[[], TakeoverCoverState] | None = None
    invalidate_takeover: Callable[[], None] | None = None
    # The registering cover's measured calibration. This is how the hub learns
    # a loaded remote's bases at all: a listener exists exactly for a
    # configured cover, which is exactly when the bases are known (#30).
    bases: CommandBases | None = None


@dataclass(slots=True)
class _Contributor:
    """One caller's share of a coalesced movement batch."""

    channels: frozenset[int]
    repeats: int
    press_token: int
    futures: list[asyncio.Future[CommandResult]]

    @property
    def live(self) -> bool:
        """Return whether any caller still awaits this contribution."""
        return any(not future.done() for future in self.futures)


@dataclass(frozen=True, slots=True)
class CommandAck:
    """Correlated bridge admission and actual first RF dispatch."""

    bridge: BridgeInfo
    command_id: str
    acknowledged_at: float
    started_at: float
    started_at_monotonic: float
    deadline: float | None
    deadline_monotonic: float | None


type CommandResult = CommandAck | Literal["superseded"]


# eq=False keeps identity hashing: instances live in the fast-lane tracking
# set, and two distinct commands must never compare equal anyway.
@dataclass(slots=True, eq=False)
class _QueuedCommand:
    """One unpublished command waiting for the hub's global worker."""

    target: str
    area_id: str | None
    bridge_id: str | None
    body: dict[str, object]
    stop_after_ms: int | None
    is_movement: bool
    is_stop: bool
    remote: RemoteIdentity | None
    channels: frozenset[int]
    coalesce_config: BlindConfig | None
    coalesce_button: Button | None
    enqueued_at: float
    coalesce_deadline: float | None
    futures: list[asyncio.Future[CommandResult]]
    # Set the moment this command's MQTT publish completes (or the command
    # dies unpublished). Later-requested overlapping commands wait on it —
    # only PUBLICATION order must match request order; waiting on full
    # acknowledgement lifecycles would park safety STOPs behind slow acks.
    published: asyncio.Event | None = None
    publish_barriers: list[asyncio.Event] = field(default_factory=list)
    # Coalesced batches keep each contributor's channels/repeats paired with
    # its futures so the frame can be rebuilt at publish time from LIVE
    # contributors only (a cancelled caller's channel must not move).
    contributors: list[_Contributor] = field(default_factory=list)
    # Optimistic multi-frame operation guard: a snapshot of the per-channel
    # publish sequence taken by the caller; the hub refuses to publish (as
    # "superseded") if any overlapping publication happened since.
    overlap_token: int | None = None
    # Snapshot of the physical-press-only generation for every addressed
    # channel. Unlike overlap_token, ordinary sibling publishes never change it.
    press_token: int = 0
    # Which config entry issued this command; None for ownerless surfaces
    # (debug raw frames). Unloading an entry drains its queued commands.
    owner: str | None = None
    # Set only after the final under-lock frame is registered immediately
    # before enqueue; execute cleanup uses it to confirm or retire the entry.
    ledger_registered: bool = False
    # A fast-lane STOP may wake an unpublished overlapping command without
    # relaxing the existing paho publication-order barrier.
    air_waiting: bool = False
    air_bypass_requested: bool = False
    # Persistent across retry generations: a STOP that observed this movement
    # unpublished must not inherit any later calendar wait through its barrier.
    air_preempted: bool = False

    @property
    def coalesce_key(self) -> tuple[str, Button, str] | None:
        """Derive the merge key: one remote identity, action, and area.

        The area is part of the key: merging covers from different areas
        would route one RF frame through a bridge that may not physically
        reach the other room's blind.
        """
        if self.coalesce_config is None or self.coalesce_button is None:
            return None
        return (
            self.coalesce_config.remote.key,
            self.coalesce_button,
            self.coalesce_config.area_id,
        )

    def overlaps(self, other: _QueuedCommand) -> bool:
        """Return whether both commands address intersecting channels of one remote."""
        return (
            self.remote is not None
            and other.remote is not None
            and self.remote.key == other.remote.key
            and bool(self.channels & other.channels)
        )


class ZemismartHub:
    """Publish and await first RF dispatch for one command at a time globally."""

    def __init__(
        self,
        registry: BridgeRegistry,
        publisher: Publisher,
        *,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT_SECONDS,
        started_timeout: float = DEFAULT_STARTED_TIMEOUT_SECONDS,
        command_id_factory: CommandIdFactory | None = None,
        now: Clock = time.time,
        monotonic_now: Clock = time.monotonic,
        air_mode: AirMode = AirMode.ENFORCE,
    ) -> None:
        """Initialize the global command queue and correlated status transport."""
        if ack_timeout <= 0 or started_timeout <= 0:
            msg = "ack_timeout and started_timeout must be greater than zero"
            raise ValueError(msg)
        self.registry = registry
        self._publisher = publisher
        self._ack_timeout = ack_timeout
        self._started_timeout = started_timeout
        self._command_id_factory = command_id_factory or (lambda: uuid.uuid4().hex)
        self._now = now
        self._monotonic_now = monotonic_now
        self._bridge_clocks: dict[str, BridgeClock] = {}
        self._ledger = CommandLedger()
        self._air = AirArbiter(mode=air_mode, monotonic_now=monotonic_now)
        self._air.update_bridges(self._air_bridge_snapshot(), now=self._monotonic_now())
        self._rx_listeners: list[_RxListener] = []
        # Entry-scoped calibrations, registered before the cover platform is
        # forwarded. The RX listeners carry the same bases, but they only exist
        # once entities have been added -- and `runtime.initialized` is true
        # before that, so the raw service is callable in the gap. Resolving from
        # listeners alone left a raw command in that window unledgered while its
        # echo was later validated with the now-available bases and taken for a
        # physical press.
        self._remote_bases: dict[str, CommandBases] = {}
        self._rx_bridge_ids: dict[str, bool] = {}
        self._recent_emission_proofs: dict[str, float] = {}
        self._state_sync = StateSyncConsumer(
            ledger=self._ledger,
            clock_resolver=self._resolve_bridge_clock,
            resolve_bases=self._resolve_remote_bases,
            dispatch=self._dispatch_heard,
            on_emission_proof=self._record_emission_proof,
            monotonic_now=self._monotonic_now,
        )
        self._pending: dict[tuple[str, str], _PendingStatuses] = {}
        self._disarm_requests: dict[tuple[str, str], _DisarmRequest] = {}
        self._on_disarms_idle: Callable[[], None] | None = None
        self._queue: deque[_QueuedCommand] = deque()
        self._queue_ready = asyncio.Condition()
        self._worker_task: asyncio.Task[None] | None = None
        self._inflight: _QueuedCommand | None = None
        self._fast_inflight: set[_QueuedCommand] = set()
        self._fast_stops: set[asyncio.Task[None]] = set()
        self._recent_displaced: dict[str, float] = {}
        self._bridge_affinity: dict[tuple[str, str], tuple[str, float]] = {}
        self._publish_seq: dict[tuple[str, int], int] = {}
        self._press_seq: dict[tuple[str, int], int] = {}
        # Serializes just the synchronous broker enqueue so bridge-receipt
        # order matches request order without any command holding the order
        # barrier across a QoS-1 broker acknowledgment.
        self._publish_lock = asyncio.Lock()
        self._publish_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self.displaced_listeners: list[Callable[[str, str], None]] = []
        self.emission_proof_listeners: list[Callable[[str], None]] = []
        self.bridge_listeners: list[Callable[[], None]] = []

    def notify_bridge_change(self) -> None:
        """Tell registered entities that retained bridge state changed."""
        self._air.update_bridges(self._air_bridge_snapshot(), now=self._monotonic_now())
        for listener in self.bridge_listeners:
            listener()

    def _air_bridge_snapshot(self) -> dict[str, tuple[bool, int | None]]:
        """Return immutable availability and valid boot evidence for the calendar."""
        return {
            bridge.bridge_id: (bridge.online, _strict_uint32(bridge.boot))
            for bridge in self.registry.bridges
        }

    def _remember_displaced(self, command_id: str) -> None:
        """Keep a short, bounded displaced-id memory for late motion models.

        Both the ID length and the entry count are capped: anything able to
        publish bridge statuses could otherwise grow this dict (and the
        per-insert expiry scan) without limit. Real command IDs are UUIDs and
        the bridge caps in-flight commands, so the bounds are generous.
        """
        if len(command_id) > _DISPLACED_MAX_ID_LENGTH:
            return
        now = self._monotonic_now()
        self._recent_displaced[command_id] = now
        expired = [
            key
            for key, seen in self._recent_displaced.items()
            if now - seen > _DISPLACED_MEMORY_SECONDS
        ]
        for key in expired:
            del self._recent_displaced[key]
        while len(self._recent_displaced) > _DISPLACED_MAX_ENTRIES:
            # dict preserves insertion order: drop the oldest entry.
            del self._recent_displaced[next(iter(self._recent_displaced))]

    def was_displaced(self, command_id: str) -> bool:
        """Return whether this command was recently displaced by the bridge.

        Covers consult this when they commit a motion model: a ``displaced``
        status that raced ahead of the cover recording its command id would
        otherwise be lost.
        """
        seen = self._recent_displaced.get(command_id)
        return seen is not None and self._monotonic_now() - seen <= _DISPLACED_MEMORY_SECONDS

    def diagnostics_snapshot(self) -> dict[str, int]:
        """Return the hub's outstanding-work counts for a diagnostics dump.

        All of this lives behind private attributes, and without it a stuck hub
        dumps identically to an idle one -- a queue that has stopped draining,
        a pile of unresolved commands or a capture held forever are exactly the
        states a bug report is trying to describe. Counts only: no identity, no
        frame material, nothing replayable.

        `publish_seq` and `bridge_affinity` are the maps #34 bounds, so their
        sizes are what make that bound observable in the field.
        """
        return {
            "queue_depth": len(self._queue),
            "fast_lane_depth": len(self._fast_inflight),
            "inflight": int(self._inflight is not None),
            "pending_commands": len(self._pending),
            "pending_disarms": len(self._disarm_requests),
            "ledger_entries": self._ledger.entry_count,
            "held_captures": self._state_sync.held_count,
            "publish_seq": len(self._publish_seq),
            "press_seq": len(self._press_seq),
            "bridge_affinity": len(self._bridge_affinity),
            "rx_listeners": len(self._rx_listeners),
        }

    def air_shadow_stats(self) -> dict[str, object]:
        """Return current shadow and enforcement arbitration statistics."""
        return self._air.stats_snapshot(now=self._monotonic_now())

    def command_takeover_live(self, command_id: str) -> bool:
        """Return whether a cover-owned command can still affect takeover."""
        return self._ledger.command_live_for_takeover(
            command_id,
            self._monotonic_now(),
        )

    def frame_is_own_emission(self, frame_hex: str) -> bool:
        """Return whether a captured frame is one this hub PROVABLY put on air.

        The Learn wizard sniffs raw RF and cannot otherwise tell a human's
        remote press from our OWN frame echoing back off the sniffing bridge,
        so a command in flight during the wizard could be learned as if it
        were the user's remote.

        Only a CONFIRMED emission counts. A pending command has been published
        but has not reported `started`, so there is no proof it ever keyed RF
        -- and unlike state sync, which holds a capture and re-evaluates it,
        Learn discards permanently. An accepted-but-never-started command
        would otherwise mask every matching press for the whole 30 s started
        timeout, burning the user's entire Learn attempt.
        """
        signature = frame_signature(frame_hex, self._resolve_remote_bases)
        if signature is None:
            return False
        match = self._ledger.match(signature, self._monotonic_now())
        return match is not None and match[0] == "confirmed"

    def _record_emission_proof(self, command_id: str) -> None:
        """Remember proof and notify only command-id-aware cover listeners."""
        if self._closed or len(command_id) > _EMISSION_PROOF_MAX_ID_LENGTH:
            return
        now = self._monotonic_now()
        self._recent_emission_proofs.pop(command_id, None)
        self._recent_emission_proofs[command_id] = now
        expired = [
            key
            for key, seen in self._recent_emission_proofs.items()
            if now - seen > _EMISSION_PROOF_MEMORY_SECONDS
        ]
        for key in expired:
            del self._recent_emission_proofs[key]
        while len(self._recent_emission_proofs) > _EMISSION_PROOF_MAX_ENTRIES:
            del self._recent_emission_proofs[next(iter(self._recent_emission_proofs))]
        for listener in tuple(self.emission_proof_listeners):
            listener(command_id)

    def was_emission_proven(self, command_id: str) -> bool:
        """Return whether a peer recently proved this exact command emitted."""
        seen = self._recent_emission_proofs.get(command_id)
        return seen is not None and self._monotonic_now() - seen <= _EMISSION_PROOF_MEMORY_SECONDS

    def register_rx_listener(
        self,
        remote_key: str,
        channels: frozenset[int],
        callback: Callable[[HeardEvent], None],
        *,
        takeover_state: Callable[[], TakeoverCoverState] | None = None,
        invalidate_takeover: Callable[[], None] | None = None,
        bases: CommandBases | None = None,
    ) -> Callable[[], None]:
        """Register one metadata-bearing RX callback and return its remover."""
        listener = _RxListener(
            remote_key,
            channels,
            callback,
            takeover_state,
            invalidate_takeover,
            bases,
        )

        def unsubscribe() -> None:
            with suppress(ValueError):
                self._rx_listeners.remove(listener)

        if not self._closed:
            self._rx_listeners.append(listener)
        return unsubscribe

    def maintain(self) -> None:
        """Collect expired RX state on a schedule rather than on RF traffic.

        The state-sync consumer's TTL sweep only runs when a capture arrives
        or a command resumes its holds, so a hold whose command never resumes
        waits for unrelated traffic before it is dispatched (#42). HA drives
        this periodically; it is idempotent, cheap when idle, and safe to
        call after ``close()``.
        """
        if self._closed:
            return
        self._state_sync.maintain()

    @staticmethod
    def _own_bases_resolver(command: _QueuedCommand) -> BasesResolver:
        """Resolve bases for a frame WE built, from the command that built it.

        An outbound frame's calibration is not a lookup -- it is right there on
        the command. Falling back to opcode inference here silently dropped
        every frame of a remote outside the 10-sample opcode table (#26): the
        signature came back None, `_ledger_registration` bailed before
        registering the action OR its armed `stop_raw`, and the command was
        absent from the ledger entirely. Our own echo then classified as a
        physical press, and `async_disarm_remote` found nothing to disarm --
        leaving the previous identity's fail-safe STOP armed through a relearn.
        """

        def resolve(remote_key: str) -> CommandBases | None:
            if command.remote is None or command.remote.key != remote_key:
                return None
            return command.remote.bases

        return resolve

    def register_remote_bases(
        self, remote_key: str, bases: CommandBases | None
    ) -> Callable[[], None]:
        """Register one loaded entry's calibration; returns its remover."""
        if bases is None:
            return lambda: None
        self._remote_bases[remote_key] = bases

        def unregister() -> None:
            if self._remote_bases.get(remote_key) is bases:
                del self._remote_bases[remote_key]

        return unregister

    def _resolve_remote_bases(self, remote_key: str) -> CommandBases | None:
        """Return a loaded remote's measured calibration, if any cover carries it.

        This is what makes #30's exact-command validation real rather than
        latent: without it `frame_signature` falls back to opcode-high-byte
        inference, which accepts frames the motor rejects AND -- since the
        opcode table is a 10-sample fit, not protocol (#26) -- rejects genuine
        presses from remotes outside it.

        Returns None for an unknown remote, which is the correct answer during
        the Learn flow: there the bases are by definition not yet measured, and
        inference is all there is.
        """
        registered = self._remote_bases.get(remote_key)
        if registered is not None:
            return registered
        for listener in self._rx_listeners:
            if listener.remote_key == remote_key and listener.bases is not None:
                return listener.bases
        return None

    def handle_rx(
        self,
        bridge_id: str,
        payload: Mapping[str, object],
    ) -> None:
        """Validate and classify one bridge RX contract payload."""
        received_at = self._now()
        received_at_monotonic = self._monotonic_now()
        frame_hex = payload.get("frame")
        t = _strict_uint32(payload.get("t"))
        boot = _strict_uint32(payload.get("boot"))
        if not isinstance(frame_hex, str) or t is None or boot is None:
            return
        normalized_bridge_id = self._admit_rx_bridge(bridge_id)
        if normalized_bridge_id is None:
            return
        self._state_sync.handle_rx(
            normalized_bridge_id,
            boot,
            t,
            frame_hex,
            received_at,
            received_at_monotonic=received_at_monotonic,
        )

    def _resolve_bridge_clock(self, bridge_id: str) -> BridgeClock:
        """Return one recently observed bridge clock under a strict LRU cap."""
        clock = self._bridge_clocks.pop(bridge_id, None)
        if clock is None:
            clock = BridgeClock()
        self._bridge_clocks[bridge_id] = clock
        while len(self._bridge_clocks) > _BRIDGE_CLOCK_CAP:
            del self._bridge_clocks[next(iter(self._bridge_clocks))]
        return clock

    def _admit_rx_bridge(self, bridge_id: str) -> str | None:
        """Bound bridge identities that may allocate state in the RX consumer."""
        normalized = bridge_id.strip()
        if not normalized or len(normalized) > _BRIDGE_MAX_ID_LENGTH:
            return None
        known = any(bridge.bridge_id == normalized for bridge in self.registry.bridges)
        if normalized in self._rx_bridge_ids:
            self._rx_bridge_ids[normalized] = known
            return normalized
        if len(self._rx_bridge_ids) >= _BRIDGE_MAX_ENTRIES:
            if not known:
                return None
            forged = next(
                (key for key, is_known in self._rx_bridge_ids.items() if not is_known),
                None,
            )
            if forged is None:
                return None
            del self._rx_bridge_ids[forged]
        self._rx_bridge_ids[normalized] = known
        return normalized

    def _dispatch_heard(self, event: HeardEvent) -> None:
        """Invoke a snapshot of matching listeners intersecting the press."""
        listeners = tuple(
            listener
            for listener in self._rx_listeners
            if listener.remote_key == event.remote_key
            and not listener.channels.isdisjoint(event.chans)
        )
        if not listeners:
            return
        configured_channels = frozenset(
            channel for listener in listeners for channel in listener.channels
        )
        self._supersede_channels(
            event.remote_key,
            event.chans & configured_channels,
        )
        self._request_takeover_disarms(event, listeners)
        for listener in listeners:
            listener.callback(event)

    def _request_takeover_disarms(
        self,
        event: HeardEvent,
        listeners: tuple[_RxListener, ...],
    ) -> None:
        """Gather, deduplicate, and disarm every live takeover target."""
        if self._closed:
            return
        now = self._monotonic_now()
        for target in self._takeover_targets(event, listeners, now):
            key = (target.bridge_id, target.command_id)
            request = self._disarm_requests.get(key)
            if target.owned_deadline_monotonic is not None:
                request = self._start_disarm_request(
                    target.bridge_id,
                    target.command_id,
                    target.owned_deadline_monotonic,
                )
            elif request is None or request.waiter.done():
                request = self._start_disarm_request(
                    target.bridge_id,
                    target.command_id,
                    now + _PRESTART_DISARM_DEADLINE_SECONDS,
                )
            self._merge_takeover_context(request, event, target)
            self._evaluate_takeover_resolution(request, None)

    def _takeover_targets(
        self,
        event: HeardEvent,
        listeners: tuple[_RxListener, ...],
        now: float,
    ) -> tuple[_TakeoverTarget, ...]:
        """Combine ledger overlaps with intersecting covers' modeled commands."""
        targets: dict[tuple[str, str], _TakeoverTarget] = {}
        for command in self._ledger.live_overlapping(event.remote_key, event.chans, now):
            targets[(command.bridge_id, command.command_id)] = _TakeoverTarget(
                bridge_id=command.bridge_id,
                command_id=command.command_id,
                channels=command.channels,
                button=command.button,
                confirmed=command.confirmed,
            )
        if event.button in {"UP", "DOWN"}:
            self._merge_cover_owned_targets(targets, listeners)
        return tuple(
            target
            for target in targets.values()
            if self.command_takeover_live(target.command_id)
            and not (event.button == "STOP" and target.confirmed is not False)
        )

    def _merge_cover_owned_targets(
        self,
        targets: dict[tuple[str, str], _TakeoverTarget],
        listeners: tuple[_RxListener, ...],
    ) -> None:
        """Add cover-owned commands, widening shared commands to all members."""
        for listener in listeners:
            self._merge_cover_owned_target(targets, listener, create=True)
        for listener in tuple(self._rx_listeners):
            if listener not in listeners:
                self._merge_cover_owned_target(targets, listener, create=False)

    @staticmethod
    def _merge_cover_owned_target(
        targets: dict[tuple[str, str], _TakeoverTarget],
        listener: _RxListener,
        *,
        create: bool,
    ) -> None:
        """Merge one current cover state into a candidate command target."""
        if listener.takeover_state is None:
            return
        state = listener.takeover_state()
        if (
            state.bridge_id is None
            or state.command_id is None
            or state.button is None
            or state.disarm_deadline_monotonic is None
        ):
            return
        key = (state.bridge_id, state.command_id)
        target = targets.get(key)
        if target is None:
            if create:
                targets[key] = _TakeoverTarget(
                    bridge_id=state.bridge_id,
                    command_id=state.command_id,
                    channels=listener.channels,
                    button=state.button,
                    confirmed=None,
                    owned_deadline_monotonic=state.disarm_deadline_monotonic,
                )
            return
        target.channels |= listener.channels
        if target.owned_deadline_monotonic is None:
            target.owned_deadline_monotonic = state.disarm_deadline_monotonic
        else:
            target.owned_deadline_monotonic = max(
                target.owned_deadline_monotonic,
                state.disarm_deadline_monotonic,
            )

    @staticmethod
    def _merge_takeover_context(
        request: _DisarmRequest,
        event: HeardEvent,
        target: _TakeoverTarget,
    ) -> None:
        """Accumulate current command channels and every pressed intersection."""
        request.remote_key = event.remote_key
        request.command_channels |= target.channels
        request.command_button = target.button
        request.pressed_channels.update(event.chans & target.channels)

    def _evaluate_takeover_resolution(
        self,
        request: _DisarmRequest,
        outcome: Literal["disarmed", "timed_out", "displaced"] | None,
        *,
        displaced_flushed: bool = False,
    ) -> None:
        """Apply the takeover truth table to current listeners and live state."""
        if request.remote_key is None or request.command_button is None:
            return
        movement_command = request.command_button in {"UP", "DOWN"}
        for listener in tuple(self._rx_listeners):
            if (
                listener.remote_key != request.remote_key
                or listener.channels.isdisjoint(request.command_channels)
                or listener.takeover_state is None
                or listener.invalidate_takeover is None
            ):
                continue
            state = listener.takeover_state()
            if state.stopped_by_heard or (
                state.command_id is not None and state.command_id != request.command_id
            ):
                continue
            pressed = not listener.channels.isdisjoint(request.pressed_channels)
            if outcome is None:
                invalidate = movement_command and not pressed
            elif pressed:
                invalidate = outcome == "timed_out" or (
                    outcome == "displaced" and displaced_flushed
                )
            else:
                invalidate = movement_command
            if invalidate:
                listener.invalidate_takeover()

    def _new_command_id(self) -> str:
        """Allocate a non-empty command ID suitable for status correlation."""
        command_id = self._command_id_factory().strip()
        if not command_id:
            msg = "command_id factory returned an empty value"
            raise ValueError(msg)
        return command_id

    def handle_status(
        self,
        bridge_id: str,
        payload: str | bytes | bytearray | Mapping[str, object],
    ) -> bool:
        """Resolve only a correlated admission or first-dispatch status."""
        decoded: object
        if isinstance(payload, bytes | bytearray | str):
            try:
                text = payload.decode() if isinstance(payload, bytes | bytearray) else payload
                decoded = json.loads(text)
            except _PAYLOAD_ERRORS:
                return False
        else:
            decoded = payload
        if not isinstance(decoded, Mapping):
            return False
        raw_status = decoded.get("status")
        command_id = decoded.get("command_id")
        if (
            not isinstance(raw_status, str)
            or raw_status not in {"accepted", "rejected", "started", "displaced", "disarmed"}
            or not isinstance(command_id, str)
            or not command_id
        ):
            return False
        if raw_status == "disarmed":
            self.on_disarmed(bridge_id, command_id)
            return True
        if raw_status == "displaced":
            return self._handle_displaced_status(bridge_id, command_id)
        pending = self._pending.get((bridge_id, command_id))
        if pending is None:
            return False
        if raw_status == "started":
            return self._handle_started_status(bridge_id, command_id, pending, decoded)
        if pending.admission.done():
            return False
        raw_reason = decoded.get("reason")
        pending.admission.set_result(
            _BridgeStatus(
                status=cast("CommandStatusValue", raw_status),
                acknowledged_at=self._now(),
                reason=str(raw_reason) if raw_reason is not None else None,
            )
        )
        return True

    def _handle_displaced_status(self, bridge_id: str, command_id: str) -> bool:
        """Resolve queue and cover state for one displaced command."""
        self._air.displaced(
            bridge_id,
            command_id,
            now=self._monotonic_now(),
        )
        # Latest-command-wins on the bridge retired this command's RF state.
        displaced_pending = self._pending.get((bridge_id, command_id))
        if displaced_pending is not None:
            # Resolve exactly the future the caller is awaiting so no
            # exception goes unretrieved.
            if not displaced_pending.admission.done():
                displaced_pending.admission.set_exception(CommandDisplacedError(command_id))
            elif not displaced_pending.started.done():
                displaced_pending.started.set_exception(CommandDisplacedError(command_id))
            elif (
                not displaced_pending.started.cancelled()
                and displaced_pending.started.exception() is None
            ):
                # started resolved but _async_execute has not resumed to
                # confirm the ledger yet (a started+displaced broker batch
                # runs both callbacks before the awaiter). Confirm from the
                # resolved future first so displace() re-windows the flushed
                # STOPs instead of retiring the still-pending entry.
                self._ledger.confirm(
                    command_id,
                    displaced_pending.started.result().started_at_monotonic,
                )
        # Unlike "started" this payload carries no age_ms, so the only anchor
        # available is raw receipt and the ledger has to budget the transport
        # lag instead of measuring it. Firmware stamping age_ms here would let
        # this call pass a corrected instant, as _handle_started_status does.
        flushed = self._ledger.displace(command_id, self._monotonic_now())
        self._state_sync.resume_holds(command_id)
        disarm_request = self._disarm_requests.get((bridge_id, command_id))
        if disarm_request is not None and not disarm_request.waiter.done():
            self._evaluate_takeover_resolution(
                disarm_request,
                "displaced",
                displaced_flushed=flushed,
            )
            disarm_request.waiter.set_result(None)
        self._remember_displaced(command_id)
        _LOGGER.debug("Bridge %s displaced command %s", bridge_id, command_id)
        for listener in self.displaced_listeners:
            listener(bridge_id, command_id)
        return True

    def _handle_started_status(
        self,
        bridge_id: str,
        command_id: str,
        pending: _PendingStatuses,
        decoded: Mapping[str, object],
    ) -> bool:
        """Resolve first dispatch and correlate an optional bridge clock sample."""
        if pending.started.done():
            return False
        received_at = self._now()
        received_at_monotonic = self._monotonic_now()
        t = _strict_uint32(decoded.get("t"))
        boot = _strict_uint32(decoded.get("boot"))
        raw_age = decoded.get("age_ms")
        age_ms = (
            raw_age
            if isinstance(raw_age, int)
            and not isinstance(raw_age, bool)
            and 0 <= raw_age <= _MAX_STARTED_AGE_MS
            else 0
        )
        self._air.started(
            bridge_id,
            command_id,
            started_at=received_at_monotonic - age_ms / _MILLISECONDS_PER_SECOND,
            boot=boot,
            now=received_at_monotonic,
        )
        started_at_monotonic = received_at_monotonic - age_ms / _MILLISECONDS_PER_SECOND
        if t is not None and boot is not None:
            clock = self._resolve_bridge_clock(bridge_id)
            if clock.can_project(boot):
                handoff_t = (t - age_ms) & _UINT32_MAX
                projected_monotonic = clock.to_monotonic_time(
                    boot,
                    handoff_t,
                    received_at_monotonic,
                )
                # The projection refines the age-based estimate by removing
                # network delivery delay — but a QoS-1 REPLAYED handoff can be
                # legitimately hours old, and the bridge clock's plausibility
                # clamp collapses any projection older than 30 s to receipt.
                # Accept the projection only when it corroborates the
                # age-based estimate; otherwise keep recv - age (the shipped
                # baseline anchor), never a clamped delivery-time anchor. An
                # exact receipt result means the bridge clock clamped an
                # implausible projection — with a small age_ms the tolerance
                # alone would accept that clamp and anchor a delayed delivery
                # at NOW, so a clamped value is always rejected.
                if (
                    projected_monotonic != received_at_monotonic
                    and abs(projected_monotonic - started_at_monotonic)
                    <= _STARTED_PROJECTION_TOLERANCE_SECONDS
                ):
                    started_at_monotonic = projected_monotonic
            clock.observe(boot, t, received_at_monotonic)
        # Preserve the diagnostic/persistence wall companion by applying the
        # monotonic age once. No live comparison consumes this projected wall
        # stamp.
        started_at = received_at - (received_at_monotonic - started_at_monotonic)
        if pending.remote_key is not None:
            self._state_sync.record_commanded_start(
                pending.remote_key,
                pending.channels,
                started_at_monotonic,
            )
        pending.started.set_result(
            _StartedStatus(
                started_at=started_at,
                started_at_monotonic=started_at_monotonic,
            ),
        )
        return True

    def on_disarmed(self, bridge_id: str, command_id: str) -> None:
        """Resolve the separate waiter for an ack received before its deadline."""
        self._air.disarmed(
            bridge_id,
            command_id,
            now=self._monotonic_now(),
        )
        request = self._disarm_requests.get((bridge_id, command_id))
        if (
            request is None
            or request.waiter.done()
            or request.waiter.get_loop().time() >= request.loop_deadline
        ):
            return
        self._resolve_disarmed_pending(bridge_id, command_id)
        self._ledger.release(command_id)
        self._state_sync.resume_holds(command_id)
        self._evaluate_takeover_resolution(request, "disarmed")
        request.waiter.set_result(None)

    def _resolve_disarmed_pending(self, bridge_id: str, command_id: str) -> None:
        """Displace the pending lifecycle future aborted by a disarm ack."""
        pending = self._pending.get((bridge_id, command_id))
        if pending is None:
            return
        error = CommandDisplacedError(command_id)
        if not pending.admission.done():
            pending.admission.set_exception(error)
        elif not pending.started.done():
            pending.started.set_exception(error)

    def _start_disarm_request(
        self,
        bridge_id: str,
        command_id: str,
        deadline: float,
    ) -> _DisarmRequest:
        """Start or join one request, replacing only a resolved predecessor."""
        loop = asyncio.get_running_loop()
        key = (bridge_id, command_id)
        existing = self._disarm_requests.get(key)
        if existing is not None and not existing.waiter.done():
            existing.deadline = max(existing.deadline, deadline)
            existing.loop_deadline = max(
                existing.loop_deadline,
                loop.time() + max(0.0, deadline - self._monotonic_now()),
            )
            return existing
        remaining = max(0.0, deadline - self._monotonic_now())
        request = _DisarmRequest(
            bridge_id=bridge_id,
            command_id=command_id,
            waiter=loop.create_future(),
            deadline=deadline,
            loop_deadline=loop.time() + remaining,
        )
        self._disarm_requests[key] = request
        request.task = asyncio.create_task(
            self._disarm(bridge_id, command_id, request),
            name=f"Zemismart disarm {bridge_id}/{command_id}",
        )
        return request

    async def _disarm(
        self,
        bridge_id: str,
        command_id: str,
        request: _DisarmRequest,
    ) -> None:
        """Retry one deduped control publish until ack or the old STOP deadline."""
        key = (bridge_id, command_id)
        if self._disarm_requests.get(key) is not request:
            return
        last_publish: asyncio.Task[None] | None = None
        retry_seconds = _DISARM_RETRY_SECONDS
        try:
            while (
                not self._closed
                and not request.waiter.done()
                and request.waiter.get_loop().time() < request.loop_deadline
            ):
                if last_publish is None or last_publish.done():
                    last_publish = await self._publish_disarm(bridge_id, command_id)
                    if last_publish is None:
                        return
                await self._wait_for_disarm_retry(request, retry_seconds)
                retry_seconds = min(
                    retry_seconds * 2,
                    _DISARM_RETRY_MAX_SECONDS,
                )
            if not self._closed and not request.waiter.done():
                self._evaluate_takeover_resolution(request, "timed_out")
        finally:
            if self._disarm_requests.get(key) is request:
                del self._disarm_requests[key]
            if not request.waiter.done():
                request.waiter.cancel()
            if not self._disarm_requests and self._on_disarms_idle is not None:
                idle_callback, self._on_disarms_idle = self._on_disarms_idle, None
                idle_callback()

    async def _wait_for_disarm_retry(
        self,
        request: _DisarmRequest,
        retry_seconds: float,
    ) -> None:
        """Wait for the ack, one retry interval, or the absolute deadline."""
        remaining = request.loop_deadline - request.waiter.get_loop().time()
        if remaining <= 0 or request.waiter.done():
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(request.waiter),
                timeout=min(retry_seconds, remaining),
            )

    async def _publish_disarm(
        self,
        bridge_id: str,
        command_id: str,
    ) -> asyncio.Task[None] | None:
        """Enqueue one QoS-1 control message through the shared publish path."""
        topic = f"{MQTT_ROOT}/{bridge_id}/cmd"
        payload = json.dumps(
            {"action": "disarm", "command_id": command_id},
            separators=(",", ":"),
        )
        async with self._publish_lock:
            if self._closed:
                return None
            publish_task, transport_error = await self._enqueue_publish(
                self._publisher(topic, payload)
            )
        if transport_error is not None:
            _LOGGER.warning("MQTT disarm publish failed: %s", transport_error)
        return publish_task

    def _ensure_worker(self) -> None:
        """Start the one queue worker lazily on the current event loop."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._async_worker(),
                name="Zemismart global command worker",
            )

    def _overlap_seq(self, remote: RemoteIdentity | None, channels: frozenset[int]) -> int:
        """Sum the publish sequence numbers covering these channels."""
        if remote is None:
            return 0
        return sum(self._publish_seq.get((remote.key, channel), 0) for channel in channels)

    def _press_generation(
        self,
        remote: RemoteIdentity | None,
        channels: frozenset[int],
    ) -> int:
        """Sum the physical-press-only generations covering these channels."""
        if remote is None:
            return 0
        return sum(self._press_seq.get((remote.key, channel), 0) for channel in channels)

    def _raise_if_overlap_displaced(self, command: _QueuedCommand) -> None:
        """Reject a multi-frame movement whose channel generation is stale."""
        if command.overlap_token is not None and command.overlap_token != self._overlap_seq(
            command.remote,
            command.channels,
        ):
            raise CommandDisplacedError(command.target)

    def _raise_if_press_displaced(self, command: _QueuedCommand) -> None:
        """Reject any command enqueued before an overlapping physical press."""
        if command.press_token != self._press_generation(command.remote, command.channels):
            raise CommandDisplacedError(command.target)

    @staticmethod
    def _raise_if_air_preempted(command: _QueuedCommand) -> None:
        """Retire an unpublished movement observed by a later safety STOP."""
        if not command.air_preempted:
            return
        for future in command.futures:
            if not future.done():
                future.set_result("superseded")
        if command.published is not None:
            command.published.set()
        raise CommandDisplacedError(command.target)

    def _revalidated_body(
        self,
        command: _QueuedCommand,
        command_id: str,
    ) -> dict[str, object]:
        """Run every final validity check, then build the body to put on air.

        Both no-await points before enqueue go through here — the one under
        _publish_lock in _ordered_publish and the one inside the scheduled
        task in _finalize_and_publish. Keeping the sequence in one place is
        deliberate: these checks are what stop a command that a cancellation,
        an overlapping publication, a heard physical press, or a safety STOP
        has already retired from still reaching the air, and two copies drifting
        apart would reopen exactly that gap on whichever path was missed.

        Order matters. The air-preempt check settles the command's futures
        before raising, so it runs before the checks that only raise; the
        contributor rebuild narrows command.channels and must precede the two
        generation checks that read them.
        """
        if all(future.done() for future in command.futures):
            raise CommandDisplacedError(command.target)
        self._raise_if_air_preempted(command)
        self._rebuild_from_live_contributors(command)
        self._raise_if_overlap_displaced(command)
        self._raise_if_press_displaced(command)
        body = dict(command.body)
        body["command_id"] = command_id
        return body

    def overlap_token(self, config: BlindConfig) -> int:
        """Snapshot the publish state of a config's channels.

        A caller running a multi-frame operation (stop, measure, move) takes
        the token between frames and passes it to the final transmit; the
        hub refuses to publish (resolving ``superseded``) if any overlapping
        publication happened in between.
        """
        return self._overlap_seq(config.remote, frozenset(config.channels))

    def _supersede_channels(self, remote_key: str, channels: frozenset[int]) -> None:
        """Advance each physically pressed channel's command generations."""
        for channel in channels:
            key = (remote_key, channel)
            self._publish_seq[key] = self._publish_seq.get(key, 0) + 1
            press_generation = self._press_seq.pop(key, 0) + 1
            self._press_seq[key] = press_generation
        while len(self._press_seq) > _PRESS_SEQ_CAP:
            del self._press_seq[next(iter(self._press_seq))]
        self._prune_publish_seq()

    def _record_publish(self, command: _QueuedCommand) -> None:
        """Advance every published channel's sequence number."""
        if command.remote is None:
            return
        for channel in command.channels:
            key = (command.remote.key, channel)
            self._publish_seq[key] = self._publish_seq.get(key, 0) + 1
        self._prune_publish_seq()

    def _token_protected_channels(self) -> set[tuple[str, int]]:
        """Return publish-sequence keys a live overlap token still depends on."""
        # A configured cover takes its token BETWEEN the frames of a
        # multi-frame move (cover.py's measure → STOP → move), so its key is
        # spoken for even while nothing of its is queued. Every cover
        # registers an RX listener for exactly its remote and channels.
        protected = {
            (listener.remote_key, channel)
            for listener in self._rx_listeners
            for channel in listener.channels
        }
        commands = [*self._queue, *self._fast_inflight]
        if self._inflight is not None:
            commands.append(self._inflight)
        for queued in commands:
            if queued.remote is None:
                continue
            protected.update((queued.remote.key, channel) for channel in queued.channels)
        return protected

    def _prune_publish_seq(self) -> None:
        """Bound the publish-sequence map without invalidating a live token.

        `_press_seq` beside it has always been capped; `_publish_seq` grew
        without limit, and raw frames can introduce arbitrary remote keys
        (#34). Eviction is not free here: `overlap_token` is a SUM over a
        config's channels, so a dropped key silently reads back as 0 and
        `_raise_if_overlap_displaced` would supersede a command that nothing
        actually displaced. Keys some holder could still be comparing against
        are therefore never evicted -- which is also why the map can sit above
        the cap, bounded instead by covers plus outstanding work.
        """
        if len(self._publish_seq) <= _PUBLISH_SEQ_CAP:
            return
        protected = self._token_protected_channels()
        for key in list(self._publish_seq):
            if len(self._publish_seq) <= _PUBLISH_SEQ_CAP:
                return
            if key not in protected:
                # dict preserves insertion order: oldest key first, exactly
                # like the _press_seq eviction above.
                del self._publish_seq[key]

    def _rebuild_from_live_contributors(self, command: _QueuedCommand) -> None:
        """Drop cancelled contributors' channels from a coalesced batch.

        Between merge and publish a contributing caller can cancel; its
        channel must not move with the surviving batch.
        """
        if not command.contributors or command.coalesce_config is None:
            return
        live = [contributor for contributor in command.contributors if contributor.live]
        if not live:
            return
        channels: set[int] = set()
        repeats = 0
        press_tokens: dict[int, int] = {}
        for contributor in live:
            channels.update(contributor.channels)
            repeats = max(repeats, contributor.repeats)
            # Only single-cover, single-channel commands are coalescible.
            press_tokens[next(iter(contributor.channels))] = contributor.press_token
        command.press_token = sum(press_tokens.values())
        if frozenset(channels) == command.channels and command.body.get("repeats") == repeats:
            return
        config = replace(
            command.coalesce_config,
            channels=tuple(sorted(channels)),
            repeats=repeats,
        )
        command.target = config.target_key
        command.channels = frozenset(channels)
        assert command.coalesce_button is not None
        command.body = self._command_body(
            config,
            command.coalesce_button,
            stop_after_ms=command.stop_after_ms,
        )

    def _overlap_publish_barriers(self, command: _QueuedCommand) -> list[asyncio.Event]:
        """Snapshot unpublished earlier overlapping commands' publish events."""
        barriers = [
            running.published
            for running in self._fast_inflight
            if running.overlaps(command)
            and running.published is not None
            and not running.published.is_set()
        ]
        inflight = self._inflight
        if (
            inflight is not None
            and inflight.overlaps(command)
            and inflight.published is not None
            and not inflight.published.is_set()
        ):
            barriers.append(inflight.published)
        return barriers

    def _raise_if_outstanding_cap_reached(self, command: _QueuedCommand) -> None:
        """Refuse one movement or raw frame once outstanding work is at the cap.

        Counts every command still holding hub resources, resolved or not:
        the queue retains superseded entries until the worker reaches them, so
        counting only live ones would leave the flood this bounds unbounded.

        STOPs are excluded from the COUNT as well as from the check. A live
        fast-lane STOP sits in `_fast_inflight` for its whole life, so counting
        them meant a backlog of unresolved STOPs -- a bridge that has stopped
        reporting status, say -- could refuse every legitimate movement while
        nothing was actually flooding anything. The flood this bounds comes
        from `send_raw`, which always builds with `is_stop=False`, and entity
        STOPs are serialised one-per-cover by cover.py's `_command_lock`, so
        excluding them cedes no real headroom.
        """
        outstanding = sum(
            1
            for command in (*self._queue, *self._fast_inflight, self._inflight)
            if command is not None and not command.is_stop
        )
        if outstanding < _MAX_OUTSTANDING_COMMANDS:
            return
        msg = (
            f"refusing {command.target}: {outstanding} commands are already outstanding "
            f"(cap {_MAX_OUTSTANDING_COMMANDS}); the bridge queue is not draining"
        )
        raise CommandQueueFullError(msg)

    async def _async_enqueue(self, command: _QueuedCommand) -> CommandResult:
        """Queue a command, giving STOP front priority and overlap supersession."""
        if self._closed:
            # The entry was unloaded; a caller that was blocked on its command
            # lock during teardown must not resurrect the worker or publish.
            return "superseded"
        future = command.futures[0]
        fast_lane = False
        command.published = asyncio.Event()
        async with self._queue_ready:
            if self._closed:
                # close() may have run while this caller waited on a CONTENDED
                # _queue_ready acquire (the entry check above only covers an
                # uncontended fast path). Re-check under the lock so a
                # teardown-race command neither queues nor resurrects the
                # worker via _ensure_worker() below.
                return "superseded"
            if not command.is_stop:
                # Outstanding work was otherwise unbounded (#34): entity
                # callers are serialised one-per-cover by cover.py's
                # _command_lock, but the admin send_raw service bypasses
                # entity locks entirely and can enqueue faster than the single
                # worker drains -- each queued command pinning futures, an
                # asyncio.Event and barrier references. Reject rather than
                # grow.
                #
                # A STOP is never counted against the cap and can never be
                # refused by it. STOP is the safety path: it takes the fast
                # lane below precisely so it can never sit behind another
                # command, and the whole air-arbitration design guarantees it
                # never waits. A cap able to refuse a STOP would be a worse
                # failure than the growth it prevents.
                self._raise_if_outstanding_cap_reached(command)
            if command.is_stop:
                # A STOP supersedes every queued movement whose channels
                # intersect its own on the same remote (exact-target and
                # member-vs-group alike); the bridge's latest-command-wins
                # replacement handles anything already on air.
                retained: deque[_QueuedCommand] = deque()
                while self._queue:
                    queued = self._queue.popleft()
                    if queued.is_movement and queued.overlaps(command):
                        for queued_future in queued.futures:
                            if not queued_future.done():
                                queued_future.set_result("superseded")
                    else:
                        retained.append(queued)
                self._queue = retained
                inflight = self._inflight
                if (
                    inflight is not None
                    and inflight.overlaps(command)
                    and inflight.published is not None
                    and not inflight.published.is_set()
                ):
                    if inflight.is_movement:
                        inflight.air_preempted = True
                        if inflight.air_waiting:
                            for inflight_future in inflight.futures:
                                if not inflight_future.done():
                                    inflight_future.set_result("superseded")
                            inflight.published.set()
                    elif not inflight.air_bypass_requested:
                        inflight.air_bypass_requested = True
                        self._air.record_stop_preemption()
                    self._air.wake()
                # Safety fast lane: a STOP skips the global one-at-a-time
                # lane so it can never sit behind another command's slow
                # acknowledgement. Publication ORDER against earlier
                # overlapping commands (the worker's in-flight command or a
                # running fast-lane STOP) is preserved by waiting on their
                # publish events only — never their full lifecycles.
                #
                # A queued live overlapping command at this point is a raw
                # debug frame (overlapping movements were superseded above).
                # It was requested BEFORE this STOP: publishing the STOP
                # first would let the raw frame displace it on the bridge
                # and re-drive the just-stopped motor, so the STOP queues
                # directly behind it instead of taking the fast lane.
                last_overlap: int | None = None
                for index, queued in enumerate(self._queue):
                    if queued.overlaps(command) and any(
                        not queued_future.done() for queued_future in queued.futures
                    ):
                        last_overlap = index
                # Snapshot barriers on BOTH paths: the queued path preserves
                # order against the raw frame via queue position, but still
                # needs the barrier against an earlier unpublished fast STOP.
                command.publish_barriers = self._overlap_publish_barriers(command)
                if last_overlap is not None:
                    self._queue[last_overlap].air_bypass_requested = True
                    # Behind the queued overlap (and therefore also behind
                    # any overlapping in-flight command).
                    self._queue.insert(last_overlap + 1, command)
                else:
                    fast_lane = True
                    self._fast_inflight.add(command)
            else:
                # Movements and raw frames go through the worker in queue
                # order, but must also publish AFTER any earlier overlapping
                # fast-lane STOP: without the barrier a movement could reach
                # the bridge first and then be displaced by the older STOP.
                command.publish_barriers = self._overlap_publish_barriers(command)
                self._queue.append(command)
            if not fast_lane:
                self._ensure_worker()
            # Notify even on the fast lane: the STOP may have superseded the
            # queued movement the worker is currently sleeping on in its
            # coalesce window, and the wake-up lets it discard that head
            # immediately instead of idling out the window.
            self._queue_ready.notify()
        if fast_lane:
            task = asyncio.create_task(
                self._async_run_fast(command),
                name="Zemismart fast-lane STOP",
            )
            self._fast_stops.add(task)
            task.add_done_callback(self._fast_stops.discard)
        try:
            return await future
        except asyncio.CancelledError:
            self._air.wake()
            async with self._queue_ready:
                self._queue_ready.notify()
            raise

    async def _async_run_fast(self, command: _QueuedCommand) -> None:
        """Run a fast-lane STOP once earlier overlapping commands published."""
        try:
            await self._async_run_direct(command)
        finally:
            self._fast_inflight.discard(command)
            if command.published is not None:
                command.published.set()

    async def _async_run_direct(self, command: _QueuedCommand) -> None:
        """Execute one command, resolving its futures (worker or fast lane)."""
        result: CommandResult
        try:
            # Request order becomes publish order: wait for earlier
            # overlapping commands (snapshotted at enqueue) to reach the
            # broker first — for BOTH lanes.
            for barrier in command.publish_barriers:
                await barrier.wait()
            # Re-check after the barrier wait: a caller may have canceled or
            # a newer command superseded every waiter meanwhile — a resolved
            # command must not still reach RF.
            if all(future.done() for future in command.futures):
                return
            result = await self._async_execute(command)
        except asyncio.CancelledError:
            for future in command.futures:
                future.cancel()
            raise
        except CommandDisplacedError:
            # A newer overlapping command replaced this one on the bridge —
            # exactly a supersession, not an error.
            result = "superseded"
        except Exception as exc:
            for future in command.futures:
                if not future.done():
                    future.set_exception(exc)
            return
        finally:
            # A command that died before publishing imposes no ordering
            # constraint; release anything barriered on it.
            if command.published is not None:
                command.published.set()
        for future in command.futures:
            if not future.done():
                future.set_result(result)

    async def _async_pop(self) -> _QueuedCommand:
        """Wait for the next command and union one expired movement batch."""
        async with self._queue_ready:
            while True:
                while not self._queue:
                    await self._queue_ready.wait()
                while self._queue and all(future.done() for future in self._queue[0].futures):
                    self._queue.popleft()
                if not self._queue:
                    continue
                command = self._queue[0]
                if command.coalesce_deadline is None:
                    return self._queue.popleft()
                # Only siblings that may actually merge below can SHRINK the
                # head's window (min() never extends it): a sibling behind an
                # overlapping intervening command is barred from merging (see
                # _coalesce_queued_movements), so its earlier deadline must
                # not truncate the head's window either.
                blocked: set[tuple[str, int]] = set()
                for queued in self._queue:
                    if queued is command or all(future.done() for future in queued.futures):
                        # A fully resolved command is discarded by the merge
                        # pass; it must not extend the window or act as a
                        # merge barrier here either.
                        continue
                    if self._coalesce_eligible(queued, command) and not self._blocks(
                        blocked, queued
                    ):
                        assert queued.coalesce_deadline is not None
                        command.coalesce_deadline = min(
                            command.coalesce_deadline,
                            queued.coalesce_deadline,
                        )
                    else:
                        self._block(blocked, queued)
                remaining = command.coalesce_deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._queue_ready.wait(), timeout=remaining)
                    continue
                command = self._queue.popleft()
                self._coalesce_queued_movements(command)
                return command

    @staticmethod
    def _coalesce_eligible(queued: _QueuedCommand, command: _QueuedCommand) -> bool:
        """Return whether a queued sibling may merge into the leading command.

        Only untimed full-travel moves ever set coalesce_deadline, so both
        commands necessarily share ``stop_after_ms is None`` — no stop-time
        comparison is needed.
        """
        return (
            queued.coalesce_key == command.coalesce_key
            and queued.coalesce_deadline is not None
            and command.coalesce_deadline is not None
            and queued.enqueued_at <= command.coalesce_deadline
        )

    @staticmethod
    def _blocks(blocked: set[tuple[str, int]], queued: _QueuedCommand) -> bool:
        """Return whether earlier retained commands bar merging this sibling.

        Merging moves the sibling's effect to the head of the queue; if any
        command positionally between the head and the sibling addresses one
        of the sibling's channels, the merge would reverse the per-channel
        command order on air (the older intervening command would win).
        """
        return queued.remote is not None and any(
            (queued.remote.key, channel) in blocked for channel in queued.channels
        )

    @staticmethod
    def _block(blocked: set[tuple[str, int]], queued: _QueuedCommand) -> None:
        """Record a retained command's channels as merge barriers."""
        if queued.remote is not None:
            blocked.update((queued.remote.key, channel) for channel in queued.channels)

    def _coalesce_queued_movements(self, command: _QueuedCommand) -> None:
        """Absorb eligible siblings that arrived within the first command's window."""
        if (
            command.coalesce_key is None
            or command.coalesce_config is None
            or command.coalesce_button is None
            or command.coalesce_deadline is None
        ):
            return
        channels = set(command.coalesce_config.channels)
        repeats = command.coalesce_config.repeats
        retained: deque[_QueuedCommand] = deque()
        blocked: set[tuple[str, int]] = set()
        merged = False
        while self._queue:
            queued = self._queue.popleft()
            if all(future.done() for future in queued.futures):
                continue
            if (
                self._coalesce_eligible(queued, command)
                and queued.coalesce_config is not None
                and not self._blocks(blocked, queued)
            ):
                channels.update(queued.coalesce_config.channels)
                repeats = max(repeats, queued.coalesce_config.repeats)
                command.futures.extend(queued.futures)
                # The sibling's ordering constraints and its identity within
                # the batch both survive the merge: its barriers gate the
                # union, and its contributor entry lets a later cancellation
                # remove exactly its channels before publish.
                command.publish_barriers.extend(
                    barrier
                    for barrier in queued.publish_barriers
                    if not barrier.is_set() and barrier not in command.publish_barriers
                )
                command.contributors.append(
                    _Contributor(
                        channels=frozenset(queued.coalesce_config.channels),
                        repeats=queued.coalesce_config.repeats,
                        press_token=queued.press_token,
                        futures=list(queued.futures),
                    )
                )
                merged = True
            else:
                retained.append(queued)
                self._block(blocked, queued)
        self._queue = retained
        if not merged:
            return
        config = replace(
            command.coalesce_config,
            channels=tuple(sorted(channels)),
            repeats=repeats,
        )
        command.target = config.target_key
        command.channels = frozenset(channels)
        command.body = self._command_body(
            config,
            command.coalesce_button,
            stop_after_ms=command.stop_after_ms,
        )

    async def _async_worker(self) -> None:
        """Resolve, publish, and await one command before popping another."""
        try:
            while True:
                command = await self._async_pop()
                if all(future.done() for future in command.futures):
                    if command.published is not None:
                        command.published.set()
                    continue
                self._inflight = command
                try:
                    await self._async_run_direct(command)
                finally:
                    self._inflight = None
        finally:
            self._worker_task = None

    def _register_pending(
        self,
        bridge: BridgeInfo,
        command_id: str,
        remote_key: str | None,
        channels: frozenset[int],
    ) -> _PendingStatuses:
        """Register admission and start correlation before MQTT publication."""
        key = (bridge.bridge_id, command_id)
        if key in self._pending:
            msg = f"duplicate pending command_id {command_id!r} for {bridge.bridge_id!r}"
            raise ValueError(msg)
        loop = asyncio.get_running_loop()
        pending = _PendingStatuses(
            admission=loop.create_future(),
            started=loop.create_future(),
            remote_key=remote_key,
            channels=channels,
        )
        self._pending[key] = pending
        return pending

    @classmethod
    def _ledger_registration(
        cls,
        command: _QueuedCommand,
    ) -> tuple[str, list[LedgerFrameSpec]] | None:
        """Build one movement command's complete classifiable RF envelope."""
        action_raw = command.body.get("raw")
        if not isinstance(action_raw, str):
            return None
        resolve = cls._own_bases_resolver(command)
        action_signature = frame_signature(action_raw, resolve)
        if action_signature is None:
            return None
        train_ms = _ledger_airtime_ms(command.body.get("repeats"))
        # An armed timed STOP PREEMPTS the remaining action/trailer repeats:
        # at the deadline the firmware promotes the command to Phase::STOP and
        # dispatches STOP ahead of normal work. Charging the full repeat train
        # anyway would keep classifying the UP/DOWN signature as our own long
        # after we stopped sending it -- discarding a real remote press (and,
        # in the Learn wizard, discarding it permanently). Allow one extra
        # slot for the frame already in flight at the deadline.
        action_ms = train_ms
        if command.stop_after_ms is not None:
            action_ms = min(train_ms, command.stop_after_ms + _LEDGER_REPEAT_AIRTIME_MS)
        frames = [
            LedgerFrameSpec(
                signature=action_signature,
                offset_ms=0,
                airtime_ms=action_ms,
            )
        ]
        for body_field in ("trailer_raw", "stop_raw"):
            raw = command.body.get(body_field)
            if not isinstance(raw, str) or (signature := frame_signature(raw, resolve)) is None:
                continue
            offset_ms = (
                command.stop_after_ms
                if body_field == "stop_raw" and command.stop_after_ms is not None
                else 0
            )
            frames.append(
                LedgerFrameSpec(
                    signature=signature,
                    offset_ms=offset_ms,
                    # The STOP train runs to completion; only the pre-deadline
                    # action/trailer phases get preempted by the deadline.
                    airtime_ms=train_ms if body_field == "stop_raw" else action_ms,
                )
            )
        return action_signature[2], frames

    def _register_command_ledger(
        self,
        command: _QueuedCommand,
        bridge_id: str,
        command_id: str,
    ) -> None:
        """Register the final under-lock RF envelope immediately before enqueue."""
        registration = self._ledger_registration(command)
        if registration is None:
            return
        button, frames = registration
        self._ledger.register_pending(
            command_id,
            bridge_id,
            tuple(sorted(command.channels)),
            button,
            frames,
        )
        command.ledger_registered = True

    async def _await_status(
        self,
        future: asyncio.Future[_BridgeStatus],
        command_id: str,
    ) -> _BridgeStatus:
        """Await the one accepted/rejected result with a fixed bound."""
        try:
            status = await asyncio.wait_for(future, timeout=self._ack_timeout)
        except TimeoutError as exc:
            msg = f"bridge acknowledgement timed out for command {command_id}"
            raise CommandAckTimeoutError(msg) from exc
        if status.status == "rejected":
            detail = f": {status.reason}" if status.reason else ""
            msg = f"bridge rejected command {command_id}{detail}"
            raise CommandRejectedError(msg)
        return status

    async def _await_started(
        self,
        future: asyncio.Future[_StartedStatus],
        command_id: str,
    ) -> _StartedStatus:
        """Await actual first RF dispatch with a scheduler-sized fixed bound."""
        try:
            return await asyncio.wait_for(future, timeout=self._started_timeout)
        except TimeoutError as exc:
            msg = f"bridge RF start timed out for command {command_id}"
            raise CommandStartedTimeoutError(msg) from exc

    def _resolve_with_affinity(self, command: _QueuedCommand) -> BridgeInfo:
        """Route consecutive commands for one remote through one bridge.

        Scheduler and armed fail-safe STOP state live in the selected
        bridge's RAM. If availability or preference changes re-routed a
        follow-up STOP or movement to a different bridge, the first bridge
        would keep transmitting its stale command (never displaced). Affinity
        holds while the previous bridge could still hold active state for the
        remote, and breaks immediately if that bridge goes offline.
        """
        assert command.area_id is not None
        now = asyncio.get_running_loop().time()
        # The key includes the area: channels of one remote can live in
        # different rooms served by different bridges, and affinity must
        # never override that RF-reachability partition.
        key = (command.remote.key, command.area_id) if command.remote is not None else None
        if key is not None:
            held = self._bridge_affinity.get(key)
            if held is not None and held[1] > now:
                with suppress(NoOnlineBridgeError):
                    bridge = self.registry.online_bridge(held[0])
                    self._remember_affinity(key, bridge.bridge_id, command, now)
                    return bridge
        bridge = self.registry.resolve(command.area_id)
        if key is not None:
            self._remember_affinity(key, bridge.bridge_id, command, now)
        return bridge

    def _remember_affinity(
        self,
        key: tuple[str, str],
        bridge_id: str,
        command: _QueuedCommand,
        now: float,
    ) -> None:
        """Hold affinity for as long as this command can occupy the bridge."""
        hold = max(
            _BRIDGE_AFFINITY_SECONDS,
            (command.stop_after_ms or 0) / 1_000 + 60.0,
        )
        self._bridge_affinity[key] = (bridge_id, now + hold)
        # Entries used to be overwritten but never swept, so every identity
        # retired by a relearn stayed forever (#34). Collect expired holds on
        # insert, the same place _recent_displaced and _recent_emission_proofs
        # are collected: growth pays for its own collection, and every entry
        # carries an expiry, so this alone bounds the map.
        expired = [
            stale
            for stale, (_bridge_id, expires_at) in self._bridge_affinity.items()
            if expires_at <= now
        ]
        for stale in expired:
            del self._bridge_affinity[stale]

    def _finish_air_hold(self, bridge_id: str, elapsed: float) -> None:
        """Close one air hold, failing open.

        Hold accounting is telemetry. Both the in-loop exit and the outer
        `finally` have to close an open hold, and neither may let a metrics
        fault stop a frame the user asked for.
        """
        try:
            self._air.record_hold_finished(bridge_id, elapsed)
        except Exception:
            self._record_air_internal_failure("air: hold accounting failed open")

    async def _commit_and_enqueue(
        self,
        command: _QueuedCommand,
        topic: str,
        bridge_id: str,
        command_id: str,
        *,
        plan: AirPlan | None,
        arbiter_failed: bool,
        now: float,
    ) -> BaseException | None:
        """Provision the air calendar for a command we have decided to send.

        Split from the wait loop because everything here happens once the
        decision to transmit is made: the loop's job is deciding WHETHER the
        air is free, this one's is committing to it. Returns the transport
        error, if any, rather than raising, so the caller can tell an immediate
        broker failure apart from a displacement.

        Every arbiter fault fails OPEN. A calendar that cannot be provisioned
        makes the transmission unaccounted-for, not forbidden.
        """
        commit_air_plan = not arbiter_failed
        if commit_air_plan and plan is not None:
            try:
                commit_air_plan = self._air.provision(
                    bridge_id=bridge_id,
                    command_id=command_id,
                    boot=self._air_bridge_boot(bridge_id),
                    plan=plan,
                    published_at=now,
                    expires_at=now + self._ack_timeout + self._started_timeout,
                    is_stop=command.is_stop,
                )
            except Exception:
                commit_air_plan = False
                self._record_air_internal_failure("air: calendar commit failed open")
        publisher_wrapper = self._finalize_and_publish(
            command,
            topic,
            bridge_id,
            command_id,
            count_air_plan=not arbiter_failed,
            commit_air_plan=commit_air_plan,
        )
        _, transport_error = await self._enqueue_publish(publisher_wrapper)
        return transport_error

    async def _ordered_publish(
        self,
        command: _QueuedCommand,
        topic: str,
        bridge_id: str,
        command_id: str,
    ) -> None:
        """Wait for feasible air, then atomically finalize and enqueue one tx."""
        hold_started: float | None = None
        hold_finished = False
        shadow_recorded = False
        transport_error: BaseException | None = None
        try:
            while True:
                wait_event: asyncio.Event | None = None
                wait_seconds = 0.0
                async with self._publish_lock:
                    if all(future.done() for future in command.futures):
                        raise CommandDisplacedError(command.target)
                    # A ready cancellation or physical-press callback must run
                    # before the authoritative final-body and validity checks.
                    await asyncio.sleep(0)
                    body = self._revalidated_body(command, command_id)
                    now = self._monotonic_now()
                    plan = None
                    arbiter_failed = False
                    try:
                        plan = plan_for_body(body)
                        if plan is not None and command.is_stop:
                            self._air.probe_stop(bridge_id, plan, now=now)
                        elif plan is not None and not command.air_bypass_requested:
                            decision = self._air.decide(bridge_id, plan, now=now)
                            if decision.disabled:
                                self._air.record_disabled()
                                if hold_started is not None:
                                    self._air.record_online_fail_open()
                            elif self._air.mode is AirMode.SHADOW:
                                if decision.would_wait and not shadow_recorded:
                                    self._air.record_shadow_wait(
                                        bridge_id,
                                        decision.earliest - now,
                                    )
                                    shadow_recorded = True
                            elif decision.should_wait:
                                if hold_started is None:
                                    hold_started = now
                                    self._air.record_hold_started(bridge_id)
                                ceiling = hold_started + MAX_AIR_HOLD_MS / 1_000
                                if now < ceiling:
                                    wait_event = decision.event
                                    wait_seconds = min(decision.earliest, ceiling) - now
                                    command.air_waiting = True
                                else:
                                    self._air.record_ceiling_hit()
                    except Exception:
                        arbiter_failed = True
                        plan = None
                        self._record_air_internal_failure("air: planning failed open")

                    if wait_event is None:
                        if hold_started is not None and not hold_finished:
                            hold_finished = True
                            self._finish_air_hold(bridge_id, now - hold_started)
                        transport_error = await self._commit_and_enqueue(
                            command,
                            topic,
                            bridge_id,
                            command_id,
                            plan=plan,
                            arbiter_failed=arbiter_failed,
                            now=now,
                        )
                if wait_event is None:
                    break
                try:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(wait_event.wait(), timeout=wait_seconds)
                finally:
                    command.air_waiting = False
            if isinstance(transport_error, CommandDisplacedError):
                # A ready physical-press callback ran before the publisher task.
                # Preserve displacement semantics instead of wrapping it as a
                # transport failure.
                raise transport_error
            if transport_error is not None:
                # An immediate transport failure (e.g. broker down) surfaces
                # fast instead of waiting out the admission timeout. The order
                # barrier is still released by _async_run_direct's finally.
                raise transport_error
            self._record_publish(command)
            if command.published is not None:
                command.published.set()
        finally:
            command.air_waiting = False
            if hold_started is not None and not hold_finished:
                self._finish_air_hold(bridge_id, self._monotonic_now() - hold_started)

    async def _finalize_and_publish(
        self,
        command: _QueuedCommand,
        topic: str,
        bridge_id: str,
        command_id: str,
        *,
        count_air_plan: bool,
        commit_air_plan: bool,
    ) -> None:
        """Revalidate inside the scheduled task, then commit and enqueue to paho."""
        body = self._revalidated_body(command, command_id)
        if count_air_plan:
            try:
                final_plan = plan_for_body(body)
                self._air.record_plan(plannable=final_plan is not None)
                if final_plan is None:
                    self._air.release_pending(bridge_id, command_id)
                elif commit_air_plan:
                    now = self._monotonic_now()
                    self._air.provision(
                        bridge_id=bridge_id,
                        command_id=command_id,
                        boot=self._air_bridge_boot(bridge_id),
                        plan=final_plan,
                        published_at=now,
                        expires_at=now + self._ack_timeout + self._started_timeout,
                        is_stop=command.is_stop,
                    )
            except Exception:
                self._air.release_pending(bridge_id, command_id)
                self._record_air_internal_failure("air: final calendar commit failed open")
        pending = self._pending.get((bridge_id, command_id))
        if pending is not None:
            pending.channels = command.channels
        payload = json.dumps(body, separators=(",", ":"))
        self._register_command_ledger(command, bridge_id, command_id)
        await self._publisher(topic, payload)

    def _air_bridge_boot(self, bridge_id: str) -> int | None:
        """Return the selected bridge's current strict boot snapshot."""
        return next(
            (
                _strict_uint32(bridge.boot)
                for bridge in self.registry.bridges
                if bridge.bridge_id == bridge_id
            ),
            None,
        )

    def _record_air_internal_failure(self, message: str) -> None:
        """Count a calendar fault when possible without blocking publication."""
        try:
            self._air.record_internal_error()
        except Exception:
            _LOGGER.warning("air: internal-error accounting failed", exc_info=True)
        _LOGGER.warning(message, exc_info=True)

    async def _enqueue_publish(
        self,
        publish: Awaitable[None],
    ) -> tuple[asyncio.Task[None], BaseException | None]:
        """Start and track one broker enqueue without awaiting its QoS-1 ack."""
        task: asyncio.Task[None] = asyncio.ensure_future(publish)
        # Track the task BEFORE the yield so a cancellation during it
        # (e.g. final unload) cannot orphan an untracked publish that then
        # enqueues after teardown; close() cancels the tracked set.
        self._publish_tasks.add(task)
        try:
            # Yield once so the task runs up to its PUBACK await — past the
            # synchronous paho enqueue — before the lock and barrier release.
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            task.cancel()
            self._publish_tasks.discard(task)
            raise
        transport_error = task.exception() if task.done() and not task.cancelled() else None
        if transport_error is None:
            # Still enqueuing/awaiting its PUBACK: reap it in the background
            # so ordering never waits on the acknowledgment.
            task.add_done_callback(self._on_publish_done)
        else:
            self._publish_tasks.discard(task)
        return task, transport_error

    def _on_publish_done(self, task: asyncio.Task[None]) -> None:
        """Retire a background publish, surfacing a transport error to the log."""
        self._publish_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # The missing admission/started ack is the caller-facing symptom
            # (a timeout); log the underlying publish failure for diagnosis.
            _LOGGER.warning("MQTT publish failed: %s", exc)

    async def _async_execute(self, command: _QueuedCommand) -> CommandAck:
        """Resolve, publish, then await admission and first RF dispatch."""
        # Rebuild from live contributors before resolution. The AUTHORITATIVE
        # rebuild runs under _publish_lock in _ordered_publish (the final
        # no-await point before enqueue); this earlier call's channel view is
        # redundant with it, but it is deliberately kept: it is the pre-lock
        # point at which a mid-flight contributor-cancellation test synchronizes
        # (that test holds the publish lock and waits for this call), so removing
        # it deadlocks the coalesced-cancel path's coverage for no real gain.
        self._rebuild_from_live_contributors(command)
        # The caller's multi-frame operation (measure -> move) was based on
        # channel state that a newer overlapping publication may have replaced.
        # Keep this cheap pre-lock fast-fail; _ordered_publish repeats it at the
        # authoritative final no-await point before enqueue.
        self._raise_if_overlap_displaced(command)
        self._raise_if_press_displaced(command)
        if command.bridge_id is not None:
            bridge = self.registry.online_bridge(command.bridge_id)
        else:
            bridge = self._resolve_with_affinity(command)
        command_id = self._new_command_id()
        action_raw = command.body.get("raw")
        pending_remote_key = (
            command.remote.key
            if command.remote is not None
            and isinstance(action_raw, str)
            and frame_signature(action_raw, self._own_bases_resolver(command)) is not None
            else None
        )
        pending = self._register_pending(
            bridge,
            command_id,
            pending_remote_key,
            command.channels,
        )
        key = (bridge.bridge_id, command_id)
        ledger_confirmed = False
        air_failure_reason: Literal["started_timeout", "cancelled_after_publish"] | None = None
        _LOGGER.debug(
            "Publishing command %s (target %s) via bridge %s (area %s)",
            command_id,
            command.target,
            bridge.bridge_id,
            bridge.area_id,
        )
        try:
            await self._ordered_publish(
                command,
                f"{MQTT_ROOT}/{bridge.bridge_id}/tx",
                bridge.bridge_id,
                command_id,
            )
            status = await self._await_status(pending.admission, command_id)
            started = await self._await_started(pending.started, command_id)
            if command.ledger_registered:
                self._ledger.confirm(command_id, started.started_at_monotonic)
                ledger_confirmed = True
                self._state_sync.resume_holds(command_id)
        except CommandStartedTimeoutError:
            air_failure_reason = "started_timeout"
            raise
        except asyncio.CancelledError:
            air_failure_reason = "cancelled_after_publish"
            raise
        finally:
            # Pop on EVERY exit, including an immediate publish transport
            # error, so a failed command never leaks its pending entry.
            self._pending.pop(key, None)
            self._air.release_pending(
                bridge.bridge_id,
                command_id,
                fail_open_reason=air_failure_reason,
            )
            if pending.started.done() and not pending.started.cancelled():
                pending.started.exception()
            if command.ledger_registered and not ledger_confirmed:
                self._ledger.retire(command_id)
                self._state_sync.resume_holds(command_id)
        _LOGGER.debug(
            "Command %s %s by bridge %s; RF started",
            command_id,
            status.status,
            bridge.bridge_id,
        )
        deadline = (
            started.started_at + command.stop_after_ms / 1_000
            if command.stop_after_ms is not None
            else None
        )
        deadline_monotonic = (
            started.started_at_monotonic + command.stop_after_ms / 1_000
            if command.stop_after_ms is not None
            else None
        )
        return CommandAck(
            bridge=bridge,
            command_id=command_id,
            acknowledged_at=status.acknowledged_at,
            started_at=started.started_at,
            started_at_monotonic=started.started_at_monotonic,
            deadline=deadline,
            deadline_monotonic=deadline_monotonic,
        )

    @staticmethod
    def _frame(config: BlindConfig, button: Button) -> str:
        """Generate and transport-validate one protocol frame."""
        assert config.remote.bases is not None
        return validate_b0_frame(
            encode_b0(
                make_payload(
                    config.remote.prefix,
                    config.remote.remote_id,
                    config.channels,
                    button,
                    bases=config.remote.bases,
                )
            )
        )

    @classmethod
    def _command_body(
        cls,
        config: BlindConfig,
        button: Button,
        *,
        stop_after_ms: int | None,
    ) -> dict[str, object]:
        """Build one validated firmware command body for a cover target."""
        body: dict[str, object] = {
            "target": config.target_key,
            "raw": cls._frame(config, button),
            "repeats": config.repeats,
        }
        if (
            button in {"UP", "DOWN"}
            and config.remote.bases is not None
            and config.remote.bases.trailer is not None
        ):
            body["trailer_raw"] = cls._frame(config, "TRAILER")
        if stop_after_ms is not None:
            body["stop_after_ms"] = stop_after_ms
            body["stop_raw"] = cls._frame(config, "STOP")
        return body

    async def async_transmit(
        self,
        config: BlindConfig,
        button: Button,
        *,
        stop_after_ms: int | None = None,
        overlap_token: int | None = None,
        owner: str | None = None,
    ) -> CommandResult:
        """Queue one validated cover command and await its result."""
        if stop_after_ms is not None and stop_after_ms <= 0:
            msg = "stop_after_ms must be greater than zero"
            raise ValueError(msg)
        body = self._command_body(config, button, stop_after_ms=stop_after_ms)
        loop = asyncio.get_running_loop()
        enqueued_at = loop.time()
        channels = frozenset(config.channels)
        press_token = self._press_generation(config.remote, channels)
        # Only untimed full-travel opens/closes coalesce: merging two precise
        # timed partial moves into one shared frame would force one
        # stop_after_ms on both, and a timed move carries an overlap_token
        # whose per-channel sequence sum cannot be reconciled once the merge
        # expands the frame to the contributor-channel union.
        coalesces = (
            button in {"UP", "DOWN"}
            and stop_after_ms is None
            and overlap_token is None
            and not config.is_group
            and config.coalesce_window_ms > 0
        )
        future: asyncio.Future[CommandResult] = loop.create_future()
        return await self._async_enqueue(
            _QueuedCommand(
                target=config.target_key,
                area_id=config.area_id,
                bridge_id=None,
                body=body,
                stop_after_ms=stop_after_ms,
                is_movement=button in {"UP", "DOWN"},
                is_stop=button == "STOP",
                remote=config.remote,
                channels=channels,
                coalesce_config=config if coalesces else None,
                coalesce_button=button if coalesces else None,
                enqueued_at=enqueued_at,
                coalesce_deadline=(
                    enqueued_at + config.coalesce_window_ms / 1_000 if coalesces else None
                ),
                futures=[future],
                contributors=(
                    [
                        _Contributor(
                            channels=channels,
                            repeats=config.repeats,
                            press_token=press_token,
                            futures=[future],
                        )
                    ]
                    if coalesces
                    else []
                ),
                overlap_token=overlap_token,
                press_token=press_token,
                owner=owner,
            )
        )

    async def async_send_raw(self, bridge_id: str, raw: str, repeats: int) -> CommandAck:
        """Queue an acknowledged debug B0 frame for one explicitly named bridge."""
        if not MIN_REPEATS <= repeats <= MAX_REPEATS:
            msg = f"repeats must be in the range {MIN_REPEATS}..{MAX_REPEATS}"
            raise ValueError(msg)
        normalized = validate_b0_frame(raw)
        decoded = decode_b0(normalized)
        # Bases from the LOADED remote, when we have one. Without them
        # `_own_bases_resolver` yields None and the raw path silently drops back
        # to opcode inference -- so a raw movement frame for a remote outside
        # `_ACTION_COMMAND_HIGH` never enters the ledger, never stamps a
        # commanded start, and has its own echo dispatched as a physical press
        # (#26/#30). The raw service is the one surface that can address a
        # remote it was not configured from, so an unknown identity still
        # resolves to None and falls back, exactly as before.
        raw_remote_key = f"{decoded['prefix']:06x}:{decoded['remote_id']:02x}"
        remote = RemoteIdentity(
            decoded["prefix"],
            decoded["remote_id"],
            self._resolve_remote_bases(raw_remote_key),
        )
        decoded_channels = tuple(cast("Iterable[int]", decoded["chans"]))
        channels = frozenset(decoded_channels)
        target = remote.target_key(decoded_channels)
        result = await self._async_enqueue(
            _QueuedCommand(
                target=target,
                area_id=None,
                bridge_id=bridge_id,
                body={"target": target, "raw": normalized, "repeats": repeats},
                stop_after_ms=None,
                is_movement=False,
                is_stop=False,
                remote=remote,
                channels=channels,
                coalesce_config=None,
                coalesce_button=None,
                enqueued_at=asyncio.get_running_loop().time(),
                coalesce_deadline=None,
                futures=[asyncio.get_running_loop().create_future()],
                press_token=self._press_generation(remote, channels),
            )
        )
        if result == "superseded":
            # Another controller sharing the bridge displaced this frame in
            # its pre-start window (bridge latest-command-wins). Raw commands
            # have no cover model to reconcile, so surface it as a plain
            # command failure the service layer can report.
            msg = "raw command was displaced by a newer overlapping command"
            raise CommandRejectedError(msg)
        return result

    def drain_owner(self, owner: str) -> None:
        """Resolve every queued-unpublished command of one owner as superseded.

        Unload-time safety: a pending service-call future is NOT proof its
        command should transmit — the worker skips commands whose futures
        are all resolved, so draining here guarantees no post-unload publish
        for the departing entry while other entries' commands stay queued.
        """
        retained: deque[_QueuedCommand] = deque()
        while self._queue:
            queued = self._queue.popleft()
            if queued.owner == owner:
                for future in queued.futures:
                    if not future.done():
                        future.set_result("superseded")
                if queued.published is not None:
                    queued.published.set()
            else:
                retained.append(queued)
        self._queue = retained
        # Fast-lane STOPs never enter the queue; one still waiting on its
        # publish barriers must not transmit for a departed owner either.
        # _async_run_direct re-checks resolved futures after the barriers,
        # so resolving here is enough — an already-publishing command is
        # in-flight and out of drain scope by design.
        for fast in tuple(self._fast_inflight):
            if fast.owner == owner and fast.published is not None and not fast.published.is_set():
                for future in fast.futures:
                    if not future.done():
                        future.set_result("superseded")
        inflight = self._inflight
        if (
            inflight is not None
            and inflight.owner == owner
            and inflight.air_waiting
            and inflight.published is not None
            and not inflight.published.is_set()
        ):
            for future in inflight.futures:
                if not future.done():
                    future.set_result("superseded")
            inflight.published.set()
            self._air.wake()

    async def async_disarm_remote(
        self,
        remote_key: str,
        *,
        deadline_seconds: float = 10.0,
    ) -> None:
        """Issue and await bridge disarms for every live command of one remote.

        Relearn-time safety: a published timed command's fail-safe STOP lives
        on the BRIDGE; cancelling queue state cannot retract it. Each live
        command gets an acknowledged disarm request, awaited (bounded) so no
        old-identity frame can transmit after an identity swap completes.
        """
        now = self._monotonic_now()
        waiters: list[asyncio.Future[None]] = []
        for command in self._ledger.live_overlapping(
            remote_key,
            frozenset(range(1, 17)),
            now,
        ):
            # Retry each disarm until the command's REAL bridge-side STOP
            # window ends (hours for a long timed move), not a flat bound;
            # the flow only awaits the short bound below. The background
            # request survives the reload this flow triggers because entry
            # teardown defers hub cleanup while disarms are pending
            # (_release_domain_runtime), even for the last loaded entry.
            request = self._start_disarm_request(
                command.bridge_id,
                command.command_id,
                self._ledger.disarm_deadline(
                    command.command_id,
                    fallback=now + deadline_seconds,
                ),
            )
            if not request.waiter.done():
                waiters.append(asyncio.shield(request.waiter))
        if not waiters:
            return
        with suppress(TimeoutError):
            async with asyncio.timeout(deadline_seconds):
                await asyncio.gather(*waiters, return_exceptions=True)

    @property
    def has_pending_disarms(self) -> bool:
        """Return whether any bridge-side disarm retry is still unresolved."""
        return bool(self._disarm_requests)

    def set_disarm_idle_callback(self, idle_callback: Callable[[], None] | None) -> None:
        """Arm a one-shot callback invoked when the last disarm request resolves."""
        self._on_disarms_idle = idle_callback

    def close(self) -> None:
        """Cancel the worker and all queued or in-flight waiters on final unload."""
        self._closed = True
        self._air.close()
        self._on_disarms_idle = None
        self._state_sync.close()
        for clock in self._bridge_clocks.values():
            clock.clear()
        self._bridge_clocks.clear()
        self._rx_listeners.clear()
        self._rx_bridge_ids.clear()
        self._recent_emission_proofs.clear()
        for request in self._disarm_requests.values():
            request.waiter.cancel()
            if request.task is not None:
                request.task.cancel()
        self._disarm_requests.clear()
        if self._worker_task is not None:
            self._worker_task.cancel()
            self._worker_task = None
        for task in self._fast_stops:
            task.cancel()
        self._fast_stops.clear()
        for publish_task in self._publish_tasks:
            publish_task.cancel()
        self._publish_tasks.clear()
        if self._inflight is not None:
            _cancel_waiters((self._inflight,))
        _cancel_waiters(self._fast_inflight)
        self._fast_inflight.clear()
        self._recent_displaced.clear()
        self._bridge_affinity.clear()
        self._press_seq.clear()
        _cancel_waiters(self._queue)
        self._queue.clear()
        for pending in self._pending.values():
            pending.admission.cancel()
            pending.started.cancel()
        self._pending.clear()
        self.displaced_listeners.clear()
        self.emission_proof_listeners.clear()
        self.bridge_listeners.clear()


@dataclass(slots=True)
class RemoteRuntime:
    """Runtime data owned by one remote-centric config entry."""

    remote: RemoteConfig
    hub: ZemismartHub
    # Built during cover-platform setup; None only before the platform loads.
    coordinator: RemoteCoordinator | None = None


@dataclass(slots=True)
class DomainRuntime:
    """MQTT registry/subscriptions shared by every config entry."""

    hub: ZemismartHub
    unsubscribers: list[Unsubscriber]
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loaded_entries: set[str] = field(default_factory=set)
    initialized: bool = False
    setup_users: int = 0
