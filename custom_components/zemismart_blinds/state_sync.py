"""Pure RF receive classification for blind state synchronization.

Canonical patch point for the tunable clock/ledger constants: the extracted
``bridge_clock`` and ``command_ledger`` modules read them back through this
module at call time, so patching ``state_sync.<constant>`` reaches their
runtime behaviour (pinned by tests). Structural constants and private type
aliases re-exported for import compatibility are NOT live patch seams —
patch the module that owns the code under test.
"""

from __future__ import annotations

import logging
import math as math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from typing import Literal as Literal

from .bridge_clock import (
    _CLOCK_EMA_ALPHA as _CLOCK_EMA_ALPHA,
)
from .bridge_clock import (
    _CLOCK_LONG_GAP_SECONDS as _CLOCK_LONG_GAP_SECONDS,
)
from .bridge_clock import (
    _CLOCK_MAX_PROJECTION_LAG_SECONDS as _CLOCK_MAX_PROJECTION_LAG_SECONDS,
)
from .bridge_clock import (
    _CLOCK_RESEED_RESIDUAL_SECONDS as _CLOCK_RESEED_RESIDUAL_SECONDS,
)
from .bridge_clock import (
    _MILLISECONDS_PER_SECOND as _MILLISECONDS_PER_SECOND,
)
from .bridge_clock import (
    _UINT32_HALF_RANGE as _UINT32_HALF_RANGE,
)
from .bridge_clock import (
    BridgeClock,
)
from .bridge_clock import (
    _ClockOutlier as _ClockOutlier,
)
from .codec import button_for_command, decode_rx_capture, infer_action_button
from .command_ledger import (
    _DISPLACED_STOP_DRAIN_SECONDS as _DISPLACED_STOP_DRAIN_SECONDS,
)
from .command_ledger import (
    _LEDGER_ANCHOR_LAG_SECONDS as _LEDGER_ANCHOR_LAG_SECONDS,
)
from .command_ledger import (
    _LEDGER_ENTRY_TTL_SECONDS as _LEDGER_ENTRY_TTL_SECONDS,
)
from .command_ledger import (
    _LEDGER_GLOBAL_CAP as _LEDGER_GLOBAL_CAP,
)
from .command_ledger import (
    _LEDGER_MAX_CONCURRENT_TARGETS as _LEDGER_MAX_CONCURRENT_TARGETS,
)
from .command_ledger import (
    _LEDGER_PENDING_TTL_SECONDS as _LEDGER_PENDING_TTL_SECONDS,
)
from .command_ledger import (
    _LEDGER_PER_BRIDGE_CAP as _LEDGER_PER_BRIDGE_CAP,
)
from .command_ledger import (
    _LEDGER_REPEAT_AIRTIME_MS as _LEDGER_REPEAT_AIRTIME_MS,
)
from .command_ledger import (
    _LEDGER_WINDOW_SLACK_SECONDS as _LEDGER_WINDOW_SLACK_SECONDS,
)
from .command_ledger import (
    CommandLedger,
    FrameSignature,
    LedgerFrameSpec,
    LedgerMatch,
    LiveCommand,
)
from .command_ledger import (
    _LedgerEntry as _LedgerEntry,
)
from .command_ledger import (
    _LedgerWindow as _LedgerWindow,
)
from .command_ledger import (
    _round_robin_stretch_seconds as _round_robin_stretch_seconds,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .codec import CommandBases

    # Supplies one remote's configured calibration to the receive path, keyed
    # by the same "prefix:remote_id" string a FrameSignature carries.
    type BasesResolver = Callable[[str], CommandBases | None]

__all__ = [
    "BridgeClock",
    "CommandLedger",
    "FrameSignature",
    "HeardEvent",
    "LedgerFrameSpec",
    "LedgerMatch",
    "LiveCommand",
    "StateSyncConsumer",
    "frame_signature",
]

_LOGGER = logging.getLogger(__name__)

_MOVEMENT_BUTTONS: Final = frozenset({"UP", "DOWN", "STOP"})
_UINT32_MODULUS: Final = 1 << 32
_UINT32_MASK: Final = _UINT32_MODULUS - 1

_EXACT_EVENT_TTL_SECONDS: Final = 60.0
_EXACT_EVENT_CAP: Final = 1_024
_DEBOUNCE_WINDOW_SECONDS: Final = 1.5
_DEBOUNCE_TTL_SECONDS: Final = 60.0
_DEBOUNCE_CAP: Final = 512
# Retention only. A stamp cannot reach further back than the clock projection
# clamp allows a capture to be old (_CLOCK_MAX_PROJECTION_LAG_SECONDS), so the
# extra retention here bounds memory rather than suppression depth.
_COMMANDED_START_TTL_SECONDS: Final = 60.0
_COMMANDED_START_CAP: Final = 512
_HOLD_TTL_SECONDS: Final = 30.0
_HOLD_CAP: Final = 256
_MAX_BRIDGE_ID_LENGTH: Final = 64
_MAX_NORMALIZED_FRAME_LENGTH: Final = 520
_MAX_RAW_FRAME_LENGTH: Final = 4 * _MAX_NORMALIZED_FRAME_LENGTH


def frame_signature(
    frame_hex: str,
    resolve_bases: BasesResolver | None = None,
) -> FrameSignature | None:
    """Decode one movement frame into its remote, channels, and button.

    Captures use the trailer-tolerant RX decoder: physical remotes may put a
    truncated trailer on air, and our own transmitted frames (always nominal)
    still decode to the identical signature, so echo comparison is unaffected.

    ``resolve_bases`` supplies the decoded remote's configured calibration.
    When it yields one, the action comes from matching the WHOLE 16-bit
    command against that remote's own measured bases and a frame matching none
    of them is refused -- the opcode-high-byte inference used otherwise
    accepts frames the motor would reject, and those still start a heard-motion
    model, supersede queued commands, and disarm live takeovers (#30). The
    signature itself is unchanged, so callers holding it need no edit.

    Without a resolver it falls back to that inference. That is what the Learn
    flow needs: there the bases are by definition not yet known.
    """
    try:
        decoded = decode_rx_capture(frame_hex)
        remote_key = f"{decoded['prefix']:06x}:{decoded['remote_id']:02x}"
        bases = None if resolve_bases is None else resolve_bases(remote_key)
        button = (
            infer_action_button(decoded["chans"], decoded["cmd"])
            if bases is None
            else button_for_command(
                decoded["chans"],
                decoded["cmd"],
                decoded["remote_id"],
                bases,
            )
        )
    except ValueError:
        return None
    if button not in _MOVEMENT_BUTTONS:
        return None
    return remote_key, frozenset(decoded["chans"]), button


@dataclass(frozen=True, slots=True)
class HeardEvent:
    """Describe one debounced physical remote movement event."""

    button: str
    chans: frozenset[int]
    remote_key: str
    heard_at: float
    heard_at_monotonic: float
    bridge_id: str


@dataclass(frozen=True, slots=True)
class _HeldCapture:
    """Retain a capture while a matching command awaits confirmation."""

    command_id: str
    signature: FrameSignature
    heard_at: float
    heard_at_monotonic: float
    bridge_id: str
    held_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _DebounceStamp:
    """Retain event and receipt times for one recent signature."""

    heard_at_monotonic: float
    seen_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _CommandedStartStamp:
    """Retain commanded start and receipt times for stale-press rejection."""

    started_at_monotonic: float
    seen_at_monotonic: float


_ExactEventKey = tuple[str, int, int, str]


class StateSyncConsumer:
    """Classify decoded RF captures as echoes or physical presses."""

    def __init__(
        self,
        *,
        ledger: CommandLedger,
        clock_resolver: Callable[[str], BridgeClock],
        dispatch: Callable[[HeardEvent], None],
        on_emission_proof: Callable[[str], None],
        monotonic_now: Callable[[], float],
        resolve_bases: BasesResolver | None = None,
    ) -> None:
        """Initialize the classifier with injected state and side effects."""
        self._ledger = ledger
        self._clock_resolver = clock_resolver
        self._resolve_bases = resolve_bases
        self._dispatch = dispatch
        self._on_emission_proof = on_emission_proof
        self._monotonic_now = monotonic_now
        self._exact_events: dict[_ExactEventKey, float] = {}
        self._debounce: dict[FrameSignature, _DebounceStamp] = {}
        self._commanded_starts: dict[
            tuple[str, frozenset[int]],
            _CommandedStartStamp,
        ] = {}
        self._holds: deque[_HeldCapture] = deque()
        self._closed = False
        self._ledger.gc(self._monotonic_now())

    def handle_rx(
        self,
        bridge_id: str,
        boot: int,
        t: int,
        frame_hex: str,
        received_at: float,
        *,
        received_at_monotonic: float | None = None,
    ) -> None:
        """Run exact deduplication, decoding, timing, and classification."""
        if self._closed or len(bridge_id) > _MAX_BRIDGE_ID_LENGTH:
            return
        normalized_frame = self._normalize_frame(frame_hex)
        if normalized_frame is None:
            return
        if received_at_monotonic is None:
            received_at_monotonic = self._monotonic_now()
        self._maintain(received_at_monotonic)
        exact_key = (bridge_id, boot, t & _UINT32_MASK, normalized_frame)
        if self._remember_exact(exact_key, received_at_monotonic):
            return
        signature = frame_signature(normalized_frame, self._resolve_bases)
        if signature is None:
            return
        clock = self._clock_resolver(bridge_id)
        heard_at_monotonic = clock.to_monotonic_time(
            boot,
            t,
            received_at_monotonic,
        )
        clock.observe(boot, t, received_at_monotonic)
        # The event needs a wall companion only because a cover may persist it.
        # Its age is a duration measured on the monotonic axis; no live decision
        # ever compares the resulting wall stamp.
        heard_at = received_at - (received_at_monotonic - heard_at_monotonic)
        self._classify(
            signature,
            heard_at,
            heard_at_monotonic,
            bridge_id,
            received_at_monotonic,
            received_at_monotonic=received_at_monotonic,
            hold_pending=True,
        )

    @property
    def held_count(self) -> int:
        """Return how many captures are parked awaiting a command outcome."""
        return len(self._holds)

    def resume_holds(self, command_id: str) -> None:
        """Re-run captures held for one command after its phase changes."""
        if self._closed:
            return
        seen_at_monotonic = self._monotonic_now()
        selected: list[_HeldCapture] = []
        remaining: deque[_HeldCapture] = deque()
        for capture in self._holds:
            if capture.command_id == command_id:
                selected.append(capture)
            else:
                remaining.append(capture)
        self._holds = remaining
        for capture in selected:
            self._classify(
                capture.signature,
                capture.heard_at,
                capture.heard_at_monotonic,
                capture.bridge_id,
                seen_at_monotonic,
                received_at_monotonic=capture.held_at_monotonic,
                hold_pending=True,
            )
        self._maintain(seen_at_monotonic)

    def maintain(self) -> None:
        """Collect expired state without waiting for the next RF capture.

        `_maintain` otherwise runs only from `handle_rx` and `resume_holds`,
        so a hold whose command never resumes at all sits until unrelated RF
        traffic happens to arrive, and is then dispatched with an arbitrarily
        old `heard_at` (#42). This consumer stays deliberately loop-free with
        an injected clock, so the HA layer drives this on a timer instead.

        Idempotent and cheap: with nothing expired it is a few empty scans.
        """
        if self._closed:
            return
        self._maintain(self._monotonic_now())

    def record_commanded_start(
        self,
        remote_key: str,
        channels: frozenset[int],
        started_at_monotonic: float,
    ) -> None:
        """Record a commanded RF start that outranks older overlapping presses."""
        if self._closed:
            return
        seen_at_monotonic = self._monotonic_now()
        self._drop_expired_commanded_starts(seen_at_monotonic)
        key = (remote_key, channels)
        previous = self._commanded_starts.pop(key, None)
        if previous is not None:
            started_at_monotonic = max(
                started_at_monotonic,
                previous.started_at_monotonic,
            )
        self._commanded_starts[key] = _CommandedStartStamp(
            started_at_monotonic=started_at_monotonic,
            seen_at_monotonic=seen_at_monotonic,
        )
        while len(self._commanded_starts) > _COMMANDED_START_CAP:
            del self._commanded_starts[next(iter(self._commanded_starts))]

    def close(self) -> None:
        """Clear all bounded state and prevent later callback delivery."""
        self._closed = True
        self._exact_events.clear()
        self._debounce.clear()
        self._commanded_starts.clear()
        self._holds.clear()
        self._ledger.clear()

    @staticmethod
    def _normalize_frame(frame_hex: str) -> str | None:
        """Canonicalize a bounded capture for exact-event identity."""
        if len(frame_hex) > _MAX_RAW_FRAME_LENGTH:
            return None
        normalized = "".join(frame_hex.split()).upper()
        if not normalized or len(normalized) > _MAX_NORMALIZED_FRAME_LENGTH:
            return None
        return normalized

    def _remember_exact(self, key: _ExactEventKey, seen_at_monotonic: float) -> bool:
        """Record an exact event and report whether it was already recent."""
        previous = self._exact_events.get(key)
        if previous is not None and seen_at_monotonic - previous <= _EXACT_EVENT_TTL_SECONDS:
            return True
        self._exact_events.pop(key, None)
        self._exact_events[key] = seen_at_monotonic
        while len(self._exact_events) > _EXACT_EVENT_CAP:
            del self._exact_events[next(iter(self._exact_events))]
        return False

    def _maintain(self, seen_at_monotonic: float) -> None:
        """Collect expired cache state and resolve timed-out holds."""
        self._ledger.gc(seen_at_monotonic)
        self._drop_expired_exact_events(seen_at_monotonic)
        self._drop_expired_debounce_stamps(seen_at_monotonic)
        self._drop_expired_commanded_starts(seen_at_monotonic)
        expired_holds: list[_HeldCapture] = []
        retained_holds: deque[_HeldCapture] = deque()
        for capture in self._holds:
            if seen_at_monotonic - capture.held_at_monotonic > _HOLD_TTL_SECONDS:
                expired_holds.append(capture)
            else:
                retained_holds.append(capture)
        self._holds = retained_holds
        for capture in expired_holds:
            self._classify(
                capture.signature,
                capture.heard_at,
                capture.heard_at_monotonic,
                capture.bridge_id,
                seen_at_monotonic,
                received_at_monotonic=capture.held_at_monotonic,
                hold_pending=False,
            )

    def _drop_expired_exact_events(self, seen_at_monotonic: float) -> None:
        """Discard exact-event keys past their replay horizon."""
        expired = [
            key
            for key, recorded_at in self._exact_events.items()
            if seen_at_monotonic - recorded_at > _EXACT_EVENT_TTL_SECONDS
        ]
        for key in expired:
            del self._exact_events[key]

    def _drop_expired_debounce_stamps(self, seen_at_monotonic: float) -> None:
        """Discard debounce signatures past their retention horizon."""
        expired = [
            signature
            for signature, stamp in self._debounce.items()
            if (seen_at_monotonic - stamp.seen_at_monotonic > _DEBOUNCE_TTL_SECONDS)
        ]
        for signature in expired:
            del self._debounce[signature]

    def _drop_expired_commanded_starts(self, seen_at_monotonic: float) -> None:
        """Discard commanded-start stamps past their retention horizon."""
        expired = [
            key
            for key, stamp in self._commanded_starts.items()
            if (seen_at_monotonic - stamp.seen_at_monotonic > _COMMANDED_START_TTL_SECONDS)
        ]
        for key in expired:
            del self._commanded_starts[key]

    def _classify(
        self,
        signature: FrameSignature,
        heard_at: float,
        heard_at_monotonic: float,
        bridge_id: str,
        seen_at_monotonic: float,
        *,
        received_at_monotonic: float,
        hold_pending: bool,
    ) -> None:
        """Apply ledger classification, holding, proof, and press dispatch."""
        match = self._ledger.match(signature, heard_at_monotonic)
        if match is not None:
            phase, command_id, command_bridge = match
            if phase == "confirmed":
                if bridge_id != command_bridge:
                    self._on_emission_proof(command_id)
                return
            if hold_pending:
                self._hold(
                    command_id,
                    signature,
                    heard_at,
                    heard_at_monotonic,
                    bridge_id,
                    seen_at_monotonic,
                )
                return
        self._dispatch_press(
            signature,
            heard_at,
            heard_at_monotonic,
            bridge_id,
            seen_at_monotonic,
            received_at_monotonic,
        )

    def _hold(
        self,
        command_id: str,
        signature: FrameSignature,
        heard_at: float,
        heard_at_monotonic: float,
        bridge_id: str,
        seen_at_monotonic: float,
    ) -> None:
        """Append one pending capture while preserving a strict queue cap."""
        self._holds.append(
            _HeldCapture(
                command_id=command_id,
                signature=signature,
                heard_at=heard_at,
                heard_at_monotonic=heard_at_monotonic,
                bridge_id=bridge_id,
                held_at_monotonic=seen_at_monotonic,
            ),
        )
        if len(self._holds) <= _HOLD_CAP:
            return
        # At the cap the oldest capture used to be discarded unclassified and
        # unlogged: a real physical press parked behind a pending command was
        # then lost permanently and its cover never re-synced (#42). Resolve
        # it the conservative way TTL expiry already does instead.
        #
        # Recursion is bounded: `hold_pending=False` is precisely the branch
        # of `_classify` that cannot reach `_hold` again. Appending BEFORE the
        # eviction also matters -- `_classify` can dispatch a press, and a
        # dispatch that re-enters this consumer (`resume_holds`) must see a
        # complete `_holds`, not one missing the capture we are mid-way
        # through recording.
        evicted = self._holds.popleft()
        _LOGGER.warning(
            "state_sync: held-capture queue hit its cap of %d; classifying %s on channels %s "
            "(held %.3fs for command %s) without waiting for that command to resolve",
            _HOLD_CAP,
            evicted.signature[2],
            sorted(evicted.signature[1]),
            seen_at_monotonic - evicted.held_at_monotonic,
            evicted.command_id,
        )
        self._classify(
            evicted.signature,
            evicted.heard_at,
            evicted.heard_at_monotonic,
            evicted.bridge_id,
            seen_at_monotonic,
            received_at_monotonic=evicted.held_at_monotonic,
            hold_pending=False,
        )

    def _superseding_commanded_start(
        self,
        remote_key: str,
        channels: frozenset[int],
        heard_at_monotonic: float,
        received_at_monotonic: float,
    ) -> float | None:
        """Return an overlapping commanded start this press is stale news against.

        A commanded start outranks a press only when the press is genuinely
        LATE NEWS: heard before our own RF went on air, and still undelivered
        to us at the moment that RF started. Both halves matter.

        Dropping the second half is what made this guard deaf to real people.
        A capture held while its command was pending is re-classified only once
        the bridge confirms -- up to _LEDGER_PENDING_TTL_SECONDS later -- so its
        `heard_at` trails the eventual `started_at` by that whole interval even
        though we had the capture in hand first. That is somebody pressing STOP
        on a moving blind, and comparing `heard_at` alone absorbed it entirely:
        no dispatch, no takeover, no disarm, no log.

        This is not the echo defence and must not be widened back into one. Our
        own emission is recognised by the ledger window, whose lower edge
        already carries _LEDGER_ANCHOR_LAG_SECONDS for precisely the anchor bias
        that would put an echo below its own commanded start; an echo that
        outran even that tolerance is reported by _log_near_miss rather than
        silently eaten here.
        """
        return next(
            (
                stamp.started_at_monotonic
                for (recent_remote, recent_channels), stamp in self._commanded_starts.items()
                if recent_remote == remote_key
                and not recent_channels.isdisjoint(channels)
                and heard_at_monotonic < stamp.started_at_monotonic <= received_at_monotonic
            ),
            None,
        )

    def _dispatch_press(
        self,
        signature: FrameSignature,
        heard_at: float,
        heard_at_monotonic: float,
        bridge_id: str,
        seen_at_monotonic: float,
        received_at_monotonic: float,
    ) -> None:
        """Debounce and dispatch the first copy of a physical press."""
        remote_key, channels, button = signature
        if any(
            recent_remote == remote_key
            and not recent_channels.isdisjoint(channels)
            and stamp.heard_at_monotonic > heard_at_monotonic
            for (recent_remote, recent_channels, _recent_button), stamp in self._debounce.items()
        ):
            return
        superseding_start = self._superseding_commanded_start(
            remote_key,
            channels,
            heard_at_monotonic,
            received_at_monotonic,
        )
        if superseding_start is not None:
            _LOGGER.debug(
                "state_sync: dropping %s on channels %s heard at %.3f as stale delivery -- "
                "our own start at %.3f was already on air %.3fs before it reached us",
                button,
                sorted(channels),
                heard_at_monotonic,
                superseding_start,
                received_at_monotonic - superseding_start,
            )
            return
        previous = self._debounce.get(signature)
        if (
            previous is not None
            and abs(heard_at_monotonic - previous.heard_at_monotonic) <= _DEBOUNCE_WINDOW_SECONDS
        ):
            return
        self._debounce.pop(signature, None)
        # A dispatched press ends every overlapping earlier signature's
        # repeat train: their stamps must not swallow a genuine re-press
        # (UP → STOP → UP inside one debounce window). Stale late copies
        # stay dropped by the heard_at ordering guard above.
        for stale in [
            key
            for key in self._debounce
            if key[0] == remote_key and not key[1].isdisjoint(channels)
        ]:
            del self._debounce[stale]
        self._debounce[signature] = _DebounceStamp(
            heard_at_monotonic=heard_at_monotonic,
            seen_at_monotonic=seen_at_monotonic,
        )
        while len(self._debounce) > _DEBOUNCE_CAP:
            del self._debounce[next(iter(self._debounce))]
        self._dispatch(
            HeardEvent(
                button=button,
                chans=channels,
                remote_key=remote_key,
                heard_at=heard_at,
                heard_at_monotonic=heard_at_monotonic,
                bridge_id=bridge_id,
            ),
        )
