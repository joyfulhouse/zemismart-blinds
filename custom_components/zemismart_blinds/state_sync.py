"""Pure RF receive classification for blind state synchronization."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from .codec import decode_b0, infer_action_button

if TYPE_CHECKING:
    from collections.abc import Callable

FrameSignature = tuple[str, frozenset[int], str]
LedgerMatch = tuple[Literal["pending", "confirmed"], str, str]

_MOVEMENT_BUTTONS: Final = frozenset({"UP", "DOWN", "STOP"})
_MILLISECONDS_PER_SECOND: Final = 1_000.0
_UINT32_MODULUS: Final = 1 << 32
_UINT32_MASK: Final = _UINT32_MODULUS - 1
_UINT32_HALF_RANGE: Final = _UINT32_MODULUS // 2
_CLOCK_EMA_ALPHA: Final = 0.2
_CLOCK_RESEED_RESIDUAL_SECONDS: Final = 30.0
_CLOCK_LONG_GAP_SECONDS: Final = 1_036_800.0
_CLOCK_MAX_PROJECTION_LAG_SECONDS: Final = 30.0

_LEDGER_WINDOW_SLACK_SECONDS: Final = 0.75
_LEDGER_ENTRY_TTL_SECONDS: Final = 60.0
_LEDGER_PENDING_TTL_SECONDS: Final = 30.0
_DISPLACED_STOP_DRAIN_SECONDS: Final = 30.0
_LEDGER_PER_BRIDGE_CAP: Final = 64
_LEDGER_GLOBAL_CAP: Final = 256

_EXACT_EVENT_TTL_SECONDS: Final = 60.0
_EXACT_EVENT_CAP: Final = 1_024
_DEBOUNCE_WINDOW_SECONDS: Final = 1.5
_DEBOUNCE_TTL_SECONDS: Final = 60.0
_DEBOUNCE_CAP: Final = 512
_COMMANDED_START_TTL_SECONDS: Final = 60.0
_COMMANDED_START_CAP: Final = 512
_HOLD_TTL_SECONDS: Final = 30.0
_HOLD_CAP: Final = 256
_MAX_BRIDGE_ID_LENGTH: Final = 64
_MAX_NORMALIZED_FRAME_LENGTH: Final = 520
_MAX_RAW_FRAME_LENGTH: Final = 4 * _MAX_NORMALIZED_FRAME_LENGTH


def frame_signature(frame_hex: str) -> FrameSignature | None:
    """Decode one movement frame into its remote, channels, and button."""
    try:
        decoded = decode_b0(frame_hex)
        button = infer_action_button(decoded["chans"], decoded["cmd"])
    except ValueError:
        return None
    if button not in _MOVEMENT_BUTTONS:
        return None
    remote_key = f"{decoded['prefix']:06x}:{decoded['remote_id']:02x}"
    return remote_key, frozenset(decoded["chans"]), button


@dataclass(frozen=True, slots=True)
class _ClockOutlier:
    """Retain one unapplied offset outlier for next-sample confirmation."""

    boot: int
    raw_t: int
    recv_time: float
    implied_offset: float


class BridgeClock:
    """Correlate one bridge's uint32 millisecond clock with HA time."""

    def __init__(self) -> None:
        """Initialize an unseeded clock correlation."""
        self._boot: int | None = None
        self._raw_t: int | None = None
        self._unwrapped_seconds: float | None = None
        self._offset_seconds: float | None = None
        self._last_recv_time: float | None = None
        self._outlier: _ClockOutlier | None = None

    def observe(self, boot: int, t: int, recv_time: float) -> None:
        """Incorporate one ordered bridge timestamp sample into the EMA."""
        raw_t = t & _UINT32_MASK
        if self._boot is None or boot != self._boot:
            self._seed(boot, raw_t, recv_time)
            return
        if self._is_long_gap(recv_time):
            self._seed(boot, raw_t, recv_time)
            return

        raw_delta = self._forward_delta(raw_t)
        if raw_delta == _UINT32_HALF_RANGE:
            self._seed(boot, raw_t, recv_time)
            return
        if raw_delta == 0 or raw_delta > _UINT32_HALF_RANGE:
            self._outlier = None
            return

        previous_unwrapped = self._unwrapped_seconds
        previous_offset = self._offset_seconds
        if previous_unwrapped is None or previous_offset is None:
            self._seed(boot, raw_t, recv_time)
            return
        unwrapped = previous_unwrapped + raw_delta / _MILLISECONDS_PER_SECOND
        observed_offset = recv_time - unwrapped
        residual = observed_offset - previous_offset
        if not math.isfinite(residual):
            self._outlier = None
            return
        if abs(residual) > _CLOCK_RESEED_RESIDUAL_SECONDS:
            self._record_or_confirm_outlier(boot, raw_t, recv_time, observed_offset)
            return

        self._outlier = None
        self._raw_t = raw_t
        self._unwrapped_seconds = unwrapped
        self._offset_seconds = previous_offset + _CLOCK_EMA_ALPHA * residual
        self._last_recv_time = recv_time

    def can_project(self, boot: int) -> bool:
        """Return whether this boot has a seeded HA-time correlation."""
        return (
            boot == self._boot
            and self._raw_t is not None
            and self._unwrapped_seconds is not None
            and self._offset_seconds is not None
        )

    def to_ha_time(self, boot: int, t: int, recv_time: float) -> float:
        """Project a bridge timestamp into HA time, never after receipt."""
        if boot != self._boot:
            return recv_time
        raw_t = t & _UINT32_MASK
        signed_delta = self._signed_delta(raw_t)
        unwrapped = self._unwrapped_seconds
        offset = self._offset_seconds
        if signed_delta is None or unwrapped is None or offset is None:
            return recv_time
        projected = unwrapped + signed_delta / _MILLISECONDS_PER_SECOND + offset
        if not math.isfinite(projected):
            return recv_time
        if projected > recv_time or projected < recv_time - _CLOCK_MAX_PROJECTION_LAG_SECONDS:
            return recv_time
        return projected

    def _record_or_confirm_outlier(
        self,
        boot: int,
        raw_t: int,
        recv_time: float,
        implied_offset: float,
    ) -> None:
        """Remember one offset outlier or reseed after a consistent successor."""
        previous = self._outlier
        if (
            previous is not None
            and previous.boot == boot
            and math.isfinite(implied_offset)
            and abs(implied_offset - previous.implied_offset) <= _CLOCK_RESEED_RESIDUAL_SECONDS
        ):
            self._seed(boot, raw_t, recv_time)
            return
        self._outlier = _ClockOutlier(
            boot=boot,
            raw_t=raw_t,
            recv_time=recv_time,
            implied_offset=implied_offset,
        )

    def _is_long_gap(self, recv_time: float) -> bool:
        """Return whether serial ordering is unsafe after a quiet interval."""
        return (
            self._last_recv_time is not None
            and recv_time - self._last_recv_time > _CLOCK_LONG_GAP_SECONDS
        )

    def _seed(self, boot: int, raw_t: int, recv_time: float) -> None:
        """Reset the correlation from one receive-time sample."""
        unwrapped = raw_t / _MILLISECONDS_PER_SECOND
        self._boot = boot
        self._raw_t = raw_t
        self._unwrapped_seconds = unwrapped
        self._offset_seconds = recv_time - unwrapped
        self._last_recv_time = recv_time
        self._outlier = None

    def _forward_delta(self, raw_t: int) -> int:
        """Return the unsigned serial delta from the most recent sample."""
        previous = self._raw_t
        if previous is None:
            return _UINT32_HALF_RANGE
        return (raw_t - previous) & _UINT32_MASK

    def _signed_delta(self, raw_t: int) -> int | None:
        """Return an unambiguous signed serial delta from the latest sample."""
        raw_delta = self._forward_delta(raw_t)
        if raw_delta == _UINT32_HALF_RANGE:
            return None
        if raw_delta < _UINT32_HALF_RANGE:
            return raw_delta
        return raw_delta - _UINT32_MODULUS

    def clear(self) -> None:
        """Forget the current bridge correlation."""
        self._boot = None
        self._raw_t = None
        self._unwrapped_seconds = None
        self._offset_seconds = None
        self._last_recv_time = None
        self._outlier = None


@dataclass(frozen=True, slots=True)
class LedgerFrameSpec:
    """Describe one emitted frame relative to a command handoff."""

    signature: FrameSignature
    offset_ms: int
    airtime_ms: int


@dataclass(frozen=True, slots=True)
class LiveCommand:
    """Identify one pending or confirmed live command overlapping a takeover."""

    bridge_id: str
    command_id: str
    channels: frozenset[int]
    button: str
    confirmed: bool


@dataclass(frozen=True, slots=True)
class _LedgerWindow:
    """Hold one confirmed signature's inclusive HA-time window."""

    signature: FrameSignature
    starts_at: float
    ends_at: float


@dataclass(slots=True)
class _LedgerEntry:
    """Hold one command's pending or confirmed emission envelope."""

    command_id: str
    bridge_id: str
    channels: tuple[int, ...]
    button: str
    frames: tuple[LedgerFrameSpec, ...]
    phase: Literal["pending", "confirmed"] = "pending"
    windows: tuple[_LedgerWindow, ...] = ()
    pending_since: float | None = None
    expires_at: float | None = None
    displaced: bool = False


class CommandLedger:
    """Correlate known command frames with received RF captures."""

    def __init__(self) -> None:
        """Initialize an empty bounded command ledger."""
        self._entries: dict[str, _LedgerEntry] = {}

    def register_pending(
        self,
        command_id: str,
        bridge_id: str,
        channels: tuple[int, ...],
        button: str,
        frames: list[LedgerFrameSpec],
    ) -> None:
        """Register a complete command envelope before broker publication."""
        self._entries.pop(command_id, None)
        self._entries[command_id] = _LedgerEntry(
            command_id=command_id,
            bridge_id=bridge_id,
            channels=channels,
            button=button,
            frames=tuple(frames),
        )
        self._enforce_caps()

    def confirm(self, command_id: str, handoff: float) -> None:
        """Confirm a pending command and calculate every frame window."""
        entry = self._entries.get(command_id)
        if entry is None or entry.displaced:
            # A displaced entry's STOP windows describe the bridge's flush
            # drain; rebuilding them from the original handoff would resurrect
            # the retired deadline and lose the drain window.
            return
        windows = tuple(self._window(frame, handoff) for frame in entry.frames)
        latest_end = max((window.ends_at for window in windows), default=handoff)
        entry.phase = "confirmed"
        entry.windows = windows
        entry.expires_at = latest_end + _LEDGER_ENTRY_TTL_SECONDS

    def live_overlapping(
        self,
        remote_key: str,
        channels: frozenset[int],
    ) -> tuple[LiveCommand, ...]:
        """Return identities and phases for live overlapping transmissions."""
        return tuple(
            LiveCommand(
                bridge_id=entry.bridge_id,
                command_id=entry.command_id,
                channels=frozenset(entry.channels),
                button=entry.button,
                confirmed=entry.phase == "confirmed",
            )
            for entry in self._entries.values()
            if (entry.phase == "pending" or (entry.phase == "confirmed" and not entry.displaced))
            and not channels.isdisjoint(entry.channels)
            and any(frame.signature[0] == remote_key for frame in entry.frames)
        )

    def retire(self, command_id: str) -> None:
        """Remove all frame state for one command."""
        self._entries.pop(command_id, None)

    def release(self, command_id: str) -> None:
        """Retire a command unless its displaced STOP drain is still active."""
        entry = self._entries.get(command_id)
        if entry is not None and not entry.displaced:
            self.retire(command_id)

    def displace(self, command_id: str, now: float) -> None:
        """Retire unstarted RF or re-window a confirmed command's flushed STOPs."""
        entry = self._entries.get(command_id)
        if entry is None:
            return
        if entry.phase == "pending":
            self.retire(command_id)
            return
        entry.windows = tuple(
            _LedgerWindow(
                signature=window.signature,
                starts_at=now - _LEDGER_WINDOW_SLACK_SECONDS,
                ends_at=(now + _DISPLACED_STOP_DRAIN_SECONDS + _LEDGER_WINDOW_SLACK_SECONDS),
            )
            if window.signature[2] == "STOP"
            else window
            for window in entry.windows
        )
        latest_end = max((window.ends_at for window in entry.windows), default=now)
        entry.expires_at = latest_end + _LEDGER_ENTRY_TTL_SECONDS
        entry.displaced = True

    def match(self, signature: FrameSignature, heard_at: float) -> LedgerMatch | None:
        """Return the newest pending or windowed confirmed command match."""
        for command_id in reversed(self._entries):
            entry = self._entries[command_id]
            if entry.phase == "pending" and any(
                frame.signature == signature for frame in entry.frames
            ):
                return "pending", entry.command_id, entry.bridge_id
            if entry.phase == "confirmed" and any(
                window.signature == signature and window.starts_at <= heard_at <= window.ends_at
                for window in entry.windows
            ):
                return "confirmed", entry.command_id, entry.bridge_id
        return None

    def gc(self, now: float) -> None:
        """Expire stale entries and reassert bridge and global bounds."""
        for entry in self._entries.values():
            if entry.phase == "pending" and entry.pending_since is None:
                entry.pending_since = now
        expired = [
            command_id
            for command_id, entry in self._entries.items()
            if self._is_expired(entry, now)
        ]
        for command_id in expired:
            del self._entries[command_id]
        self._enforce_caps()

    @staticmethod
    def _window(frame: LedgerFrameSpec, handoff: float) -> _LedgerWindow:
        """Build one symmetric-slack confirmed frame window."""
        frame_handoff = handoff + frame.offset_ms / _MILLISECONDS_PER_SECOND
        return _LedgerWindow(
            signature=frame.signature,
            starts_at=frame_handoff - _LEDGER_WINDOW_SLACK_SECONDS,
            ends_at=(
                frame_handoff
                + frame.airtime_ms / _MILLISECONDS_PER_SECOND
                + _LEDGER_WINDOW_SLACK_SECONDS
            ),
        )

    @staticmethod
    def _is_expired(entry: _LedgerEntry, now: float) -> bool:
        """Return whether one entry exceeded its phase-specific lifetime."""
        if entry.phase == "confirmed":
            return entry.expires_at is not None and now > entry.expires_at
        return (
            entry.pending_since is not None
            and now - entry.pending_since > _LEDGER_PENDING_TTL_SECONDS
        )

    def _enforce_caps(self) -> None:
        """Evict oldest entries until all configured limits hold."""
        bridge_counts: dict[str, int] = {}
        for entry in reversed(tuple(self._entries.values())):
            count = bridge_counts.get(entry.bridge_id, 0)
            if count >= _LEDGER_PER_BRIDGE_CAP:
                del self._entries[entry.command_id]
            else:
                bridge_counts[entry.bridge_id] = count + 1
        while len(self._entries) > _LEDGER_GLOBAL_CAP:
            del self._entries[next(iter(self._entries))]

    def clear(self) -> None:
        """Remove every command and collection timestamp."""
        self._entries.clear()


@dataclass(frozen=True, slots=True)
class HeardEvent:
    """Describe one debounced physical remote movement event."""

    button: str
    chans: frozenset[int]
    remote_key: str
    heard_at: float
    bridge_id: str


@dataclass(frozen=True, slots=True)
class _HeldCapture:
    """Retain a capture while a matching command awaits confirmation."""

    command_id: str
    signature: FrameSignature
    heard_at: float
    bridge_id: str
    held_at: float


@dataclass(frozen=True, slots=True)
class _DebounceStamp:
    """Retain event and receipt times for one recent signature."""

    heard_at: float
    seen_at: float


@dataclass(frozen=True, slots=True)
class _CommandedStartStamp:
    """Retain commanded start and receipt times for stale-press rejection."""

    started_at: float
    seen_at: float


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
        now: Callable[[], float],
    ) -> None:
        """Initialize the classifier with injected state and side effects."""
        self._ledger = ledger
        self._clock_resolver = clock_resolver
        self._dispatch = dispatch
        self._on_emission_proof = on_emission_proof
        self._now = now
        self._exact_events: dict[_ExactEventKey, float] = {}
        self._debounce: dict[FrameSignature, _DebounceStamp] = {}
        self._commanded_starts: dict[
            tuple[str, frozenset[int]],
            _CommandedStartStamp,
        ] = {}
        self._holds: deque[_HeldCapture] = deque()
        self._closed = False
        self._ledger.gc(self._now())

    def handle_rx(
        self,
        bridge_id: str,
        boot: int,
        t: int,
        frame_hex: str,
        recv_time: float,
    ) -> None:
        """Run exact deduplication, decoding, timing, and classification."""
        if self._closed or len(bridge_id) > _MAX_BRIDGE_ID_LENGTH:
            return
        normalized_frame = self._normalize_frame(frame_hex)
        if normalized_frame is None:
            return
        seen_at = self._now()
        self._maintain(seen_at)
        exact_key = (bridge_id, boot, t & _UINT32_MASK, normalized_frame)
        if self._remember_exact(exact_key, seen_at):
            return
        signature = frame_signature(normalized_frame)
        if signature is None:
            return
        clock = self._clock_resolver(bridge_id)
        heard_at = clock.to_ha_time(boot, t, recv_time)
        clock.observe(boot, t, recv_time)
        self._classify(signature, heard_at, bridge_id, seen_at, hold_pending=True)

    def resume_holds(self, command_id: str) -> None:
        """Re-run captures held for one command after its phase changes."""
        if self._closed:
            return
        seen_at = self._now()
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
                capture.bridge_id,
                seen_at,
                hold_pending=True,
            )
        self._maintain(seen_at)

    def record_commanded_start(
        self,
        remote_key: str,
        channels: frozenset[int],
        started_at: float,
    ) -> None:
        """Record a commanded RF start that outranks older overlapping presses."""
        if self._closed:
            return
        seen_at = self._now()
        self._drop_expired_commanded_starts(seen_at)
        key = (remote_key, channels)
        previous = self._commanded_starts.pop(key, None)
        if previous is not None:
            started_at = max(started_at, previous.started_at)
        self._commanded_starts[key] = _CommandedStartStamp(
            started_at=started_at,
            seen_at=seen_at,
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

    def _remember_exact(self, key: _ExactEventKey, seen_at: float) -> bool:
        """Record an exact event and report whether it was already recent."""
        previous = self._exact_events.get(key)
        if previous is not None and seen_at - previous <= _EXACT_EVENT_TTL_SECONDS:
            return True
        self._exact_events.pop(key, None)
        self._exact_events[key] = seen_at
        while len(self._exact_events) > _EXACT_EVENT_CAP:
            del self._exact_events[next(iter(self._exact_events))]
        return False

    def _maintain(self, seen_at: float) -> None:
        """Collect expired cache state and resolve timed-out holds."""
        self._ledger.gc(seen_at)
        self._drop_expired_exact_events(seen_at)
        self._drop_expired_debounce_stamps(seen_at)
        self._drop_expired_commanded_starts(seen_at)
        expired_holds: list[_HeldCapture] = []
        retained_holds: deque[_HeldCapture] = deque()
        for capture in self._holds:
            if seen_at - capture.held_at > _HOLD_TTL_SECONDS:
                expired_holds.append(capture)
            else:
                retained_holds.append(capture)
        self._holds = retained_holds
        for capture in expired_holds:
            self._classify(
                capture.signature,
                capture.heard_at,
                capture.bridge_id,
                seen_at,
                hold_pending=False,
            )

    def _drop_expired_exact_events(self, seen_at: float) -> None:
        """Discard exact-event keys past their replay horizon."""
        expired = [
            key
            for key, recorded_at in self._exact_events.items()
            if seen_at - recorded_at > _EXACT_EVENT_TTL_SECONDS
        ]
        for key in expired:
            del self._exact_events[key]

    def _drop_expired_debounce_stamps(self, seen_at: float) -> None:
        """Discard debounce signatures past their retention horizon."""
        expired = [
            signature
            for signature, stamp in self._debounce.items()
            if seen_at - stamp.seen_at > _DEBOUNCE_TTL_SECONDS
        ]
        for signature in expired:
            del self._debounce[signature]

    def _drop_expired_commanded_starts(self, seen_at: float) -> None:
        """Discard commanded-start stamps past their retention horizon."""
        expired = [
            key
            for key, stamp in self._commanded_starts.items()
            if seen_at - stamp.seen_at > _COMMANDED_START_TTL_SECONDS
        ]
        for key in expired:
            del self._commanded_starts[key]

    def _classify(
        self,
        signature: FrameSignature,
        heard_at: float,
        bridge_id: str,
        seen_at: float,
        *,
        hold_pending: bool,
    ) -> None:
        """Apply ledger classification, holding, proof, and press dispatch."""
        match = self._ledger.match(signature, heard_at)
        if match is not None:
            phase, command_id, command_bridge = match
            if phase == "confirmed":
                if bridge_id != command_bridge:
                    self._on_emission_proof(command_id)
                return
            if hold_pending:
                self._hold(command_id, signature, heard_at, bridge_id, seen_at)
                return
        self._dispatch_press(signature, heard_at, bridge_id, seen_at)

    def _hold(
        self,
        command_id: str,
        signature: FrameSignature,
        heard_at: float,
        bridge_id: str,
        seen_at: float,
    ) -> None:
        """Append one pending capture while preserving a strict queue cap."""
        if len(self._holds) >= _HOLD_CAP:
            self._holds.popleft()
        self._holds.append(
            _HeldCapture(
                command_id=command_id,
                signature=signature,
                heard_at=heard_at,
                bridge_id=bridge_id,
                held_at=seen_at,
            ),
        )

    def _dispatch_press(
        self,
        signature: FrameSignature,
        heard_at: float,
        bridge_id: str,
        seen_at: float,
    ) -> None:
        """Debounce and dispatch the first copy of a physical press."""
        remote_key, channels, button = signature
        if any(
            recent_remote == remote_key
            and not recent_channels.isdisjoint(channels)
            and stamp.heard_at > heard_at
            for (recent_remote, recent_channels, _recent_button), stamp in self._debounce.items()
        ):
            return
        if any(
            recent_remote == remote_key
            and not recent_channels.isdisjoint(channels)
            and stamp.started_at > heard_at
            for (recent_remote, recent_channels), stamp in self._commanded_starts.items()
        ):
            return
        previous = self._debounce.get(signature)
        if previous is not None and abs(heard_at - previous.heard_at) <= _DEBOUNCE_WINDOW_SECONDS:
            return
        self._debounce.pop(signature, None)
        self._debounce[signature] = _DebounceStamp(heard_at=heard_at, seen_at=seen_at)
        while len(self._debounce) > _DEBOUNCE_CAP:
            del self._debounce[next(iter(self._debounce))]
        self._dispatch(
            HeardEvent(
                button=button,
                chans=channels,
                remote_key=remote_key,
                heard_at=heard_at,
                bridge_id=bridge_id,
            ),
        )
