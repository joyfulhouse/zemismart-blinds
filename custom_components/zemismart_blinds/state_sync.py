"""Pure RF receive classification for blind state synchronization."""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from .codec import decode_rx_capture, infer_action_button

if TYPE_CHECKING:
    from collections.abc import Callable

FrameSignature = tuple[str, frozenset[int], str]
LedgerMatch = tuple[Literal["pending", "confirmed"], str, str]

_LOGGER = logging.getLogger(__name__)

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
# How late a confirmed window's own anchor may be. `started_at` is derived as
# `recv_time - age_ms/1000`: age_ms corrects the FIRMWARE's queueing delay, but
# nothing corrects the MQTT transport leg between the bridge publishing its
# status and HA's callback running. The anchor is therefore biased LATE and
# never early, so every window built from it sits later than the RF it
# describes -- measured at 1.117 s during a concurrent seven-cover burst.
#
# Applied to the LOWER edge only. Widening the upper edge would suppress
# genuine presses long after we stopped transmitting, which the late bias never
# justifies. NOT an architectural ceiling: heavier congestion can exceed it, so
# near-misses are logged rather than passing silently.
_LEDGER_ANCHOR_LAG_SECONDS: Final = 5.0
_LEDGER_ENTRY_TTL_SECONDS: Final = 60.0
_LEDGER_PENDING_TTL_SECONDS: Final = 30.0
_DISPLACED_STOP_DRAIN_SECONDS: Final = 30.0
_LEDGER_PER_BRIDGE_CAP: Final = 64
_LEDGER_GLOBAL_CAP: Final = 256
# Mirrors models._LEDGER_REPEAT_AIRTIME_MS: this module is imported BY
# models.py and must not import back, so the shared calibration is
# duplicated rather than centralized -- keep both in step.
_LEDGER_REPEAT_AIRTIME_MS: Final = 1_000
# The firmware's channel field is a 1..16 range (validate_channels in
# codec.py), so one bridge can never be round-robining more targets than
# that. Caps the concurrency counted at match() time so a counting bug
# cannot stretch a window past a known bound (#21).
_LEDGER_MAX_CONCURRENT_TARGETS: Final = 16

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


def frame_signature(frame_hex: str) -> FrameSignature | None:
    """Decode one movement frame into its remote, channels, and button.

    Captures use the trailer-tolerant RX decoder: physical remotes may put a
    truncated trailer on air, and our own transmitted frames (always nominal)
    still decode to the identical signature, so echo comparison is unaffected.
    """
    try:
        decoded = decode_rx_capture(frame_hex)
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
    """Hold one confirmed signature's inclusive HA-time window.

    Two lower edges, because they are different strengths of evidence.
    ``nominal_starts_at`` is where the frame belongs if its anchor was honest;
    ``starts_at`` additionally spends _LEDGER_ANCHOR_LAG_SECONDS on the chance
    that it was not. Sequential commands for one cover share a signature, so
    the spent-budget region of a newer window reaches back over an older
    command's honest one and match() has to prefer the honest fit.
    """

    signature: FrameSignature
    nominal_starts_at: float
    starts_at: float
    ends_at: float
    # How long this frame's full repeat train occupies the air. Already
    # implied by ends_at, but needed on its own by _early_flushed_stop: a
    # displaced STOP train drains BELOW the admission that flushed it, so the
    # same length has to be budgeted downwards from a different anchor.
    train_seconds: float


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
    # When this command's own first frame went on air. Displacement of an
    # OLDER command happens as the bridge admits this one, so for any command
    # this is also the instant it flushed its predecessor's armed frames.
    handoff: float | None = None


def _round_robin_stretch_seconds(train_seconds: float, concurrency: int) -> float:
    """Return extra tail seconds one train gains when interleaved with peers.

    A command alone on its bridge sends its whole repeat train back-to-back --
    ``train_seconds`` already models exactly that contiguous case, and this
    returns 0 for it, collapsing to today's exact behaviour whenever a bridge
    serves only one target.

    With ``concurrency`` total targets sharing the bridge, the firmware's
    TargetScheduler round-robins every currently-armed target one slot each
    (rf433_scheduler.h), so only the FIRST of the command's own repeats keeps
    its native timing: each of the remaining repeats is pushed out behind
    (concurrency - 1) other targets' slots instead of behind none. One slot
    is charged _LEDGER_REPEAT_AIRTIME_MS, the same conservative per-copy
    envelope the contiguous case already uses, so this stays proportional to
    OBSERVED concurrency rather than a blanket multiplier of the whole train.
    """
    if concurrency <= 1 or train_seconds <= 0:
        return 0.0
    repeats = max(1, round(train_seconds * _MILLISECONDS_PER_SECOND / _LEDGER_REPEAT_AIRTIME_MS))
    extra_ms = (repeats - 1) * (concurrency - 1) * _LEDGER_REPEAT_AIRTIME_MS
    return extra_ms / _MILLISECONDS_PER_SECOND


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
        entry.handoff = handoff
        entry.expires_at = latest_end + _LEDGER_ENTRY_TTL_SECONDS

    def live_overlapping(
        self,
        remote_key: str,
        channels: frozenset[int],
        now: float,
    ) -> tuple[LiveCommand, ...]:
        """Return pending or still-airing confirmed overlapping transmissions."""
        return tuple(
            LiveCommand(
                bridge_id=entry.bridge_id,
                command_id=entry.command_id,
                channels=frozenset(entry.channels),
                button=entry.button,
                confirmed=entry.phase == "confirmed",
            )
            for entry in self._entries.values()
            if (
                entry.phase == "pending"
                or (
                    entry.phase == "confirmed"
                    and not entry.displaced
                    and any(window.ends_at >= now for window in entry.windows)
                )
            )
            and not channels.isdisjoint(entry.channels)
            and any(frame.signature[0] == remote_key for frame in entry.frames)
        )

    def command_live_for_takeover(self, command_id: str, now: float) -> bool:
        """Return whether one command can still affect a physical takeover."""
        entry = self._entries.get(command_id)
        if entry is None or entry.phase == "pending":
            return True
        return not entry.displaced and any(window.ends_at >= now for window in entry.windows)

    def disarm_deadline(self, command_id: str, *, fallback: float) -> float:
        """Return how long a disarm for this command stays worth retrying.

        A confirmed command's bridge-armed frames stay dangerous until its
        last emission window closes; before confirmation only the caller's
        fallback bound applies.
        """
        entry = self._entries.get(command_id)
        if entry is None or not entry.windows:
            return fallback
        return max(fallback, *(window.ends_at for window in entry.windows))

    def retire(self, command_id: str) -> None:
        """Remove all frame state for one command."""
        self._entries.pop(command_id, None)

    def release(self, command_id: str) -> None:
        """Retire a command unless its displaced STOP drain is still active."""
        entry = self._entries.get(command_id)
        if entry is not None and not entry.displaced:
            self.retire(command_id)

    def displace(self, command_id: str, now: float) -> bool:
        """Retire or re-window RF, reporting whether confirmed STOPs flushed."""
        entry = self._entries.get(command_id)
        if entry is None:
            return False
        if entry.phase == "pending":
            self.retire(command_id)
            return False
        flushed = any(window.signature[2] == "STOP" for window in entry.windows)
        entry.windows = tuple(
            _LedgerWindow(
                signature=window.signature,
                # `now` is the receipt of the "displaced" status, and that
                # status carries no age_ms -- so unlike started_at it is not
                # even corrected for the firmware's own queueing, let alone the
                # MQTT leg. The flush always precedes this receipt and never
                # follows it, so the drain takes the same lower-edge tolerance
                # every other confirmed window gets. (Firmware stamping age_ms
                # on "displaced" as it does on "started" would let this be
                # measured instead of budgeted.)
                train_seconds=window.train_seconds,
                nominal_starts_at=now - _LEDGER_WINDOW_SLACK_SECONDS,
                starts_at=(now - _LEDGER_WINDOW_SLACK_SECONDS - _LEDGER_ANCHOR_LAG_SECONDS),
                ends_at=(now + _DISPLACED_STOP_DRAIN_SECONDS + _LEDGER_WINDOW_SLACK_SECONDS),
            )
            if window.signature[2] == "STOP"
            else window
            for window in entry.windows
        )
        latest_end = max((window.ends_at for window in entry.windows), default=now)
        entry.expires_at = latest_end + _LEDGER_ENTRY_TTL_SECONDS
        entry.displaced = True
        return flushed

    @staticmethod
    def _nominal_span(entry: _LedgerEntry) -> tuple[float, float] | None:
        """Return one CONFIRMED command's own contiguous transmission span.

        Ignores any round-robin stretch: this is the span the command would
        occupy if it were the bridge's only target, used purely to detect
        whether another command's own train was scheduled to run at the same
        time -- the condition that makes the firmware interleave them.
        """
        if entry.handoff is None or not entry.frames:
            return None
        latest_end_ms = max(frame.offset_ms + frame.airtime_ms for frame in entry.frames)
        return entry.handoff, entry.handoff + latest_end_ms / _MILLISECONDS_PER_SECOND

    def _round_robin_concurrency(self, entry: _LedgerEntry) -> int:
        """Count same-bridge commands whose own trains overlapped this one's.

        Evaluated fresh on every call rather than once at registration: a
        command confirmed while it is alone must still pick up concurrency
        from a peer that is only admitted (and only then gets a ``handoff``)
        afterward, and the reverse — an earlier peer's own count growing once
        this command joins it — has to hold too, since the firmware round-
        robins EVERY currently-armed target, not just ones queued after this
        one. Only non-displaced, CONFIRMED peers count: a displaced entry's
        window describes a flush drain (see ``displace``), a different
        mechanism that round-robin stretch does not apply to.

        Bounded by the firmware's 1..16 channel range so a counting bug can
        widen a window at most sixteen-fold, never without limit.
        """
        span = self._nominal_span(entry)
        if span is None or not entry.frames:
            return 1
        start, end = span
        train_seconds = max(frame.airtime_ms for frame in entry.frames) / _MILLISECONDS_PER_SECOND
        peers: list[tuple[float, float]] = []
        for other in self._entries.values():
            if (
                other.command_id == entry.command_id
                or other.bridge_id != entry.bridge_id
                or other.displaced
            ):
                continue
            other_span = self._nominal_span(other)
            if other_span is not None:
                peers.append(other_span)
        if not peers:
            return 1

        # Fixed point, because the question is circular: whether a peer shares
        # the antenna with us depends on how long we are really on it, which
        # depends on how many peers share it. Comparing nominal span against
        # nominal span answers a strictly smaller question and undercounts a
        # STAGGERED burst -- peers admitted after our nominal window closes but
        # while the firmware is demonstrably still interleaving us. Start from
        # no stretch, widen by what the current count implies, recount, and
        # settle. The count only ever grows, so this converges, and the cap
        # bounds the passes.
        concurrency = 1
        for _pass in range(_LEDGER_MAX_CONCURRENT_TARGETS):
            reach = _round_robin_stretch_seconds(train_seconds, concurrency)
            counted = min(
                1
                + sum(
                    1
                    for other_start, other_end in peers
                    if other_start <= end + reach and start <= other_end + reach
                ),
                _LEDGER_MAX_CONCURRENT_TARGETS,
            )
            if counted == concurrency:
                break
            concurrency = counted
        return concurrency

    def _effective_ends_at(
        self,
        entry: _LedgerEntry,
        window: _LedgerWindow,
        concurrency: int | None = None,
    ) -> float:
        """Return one window's upper edge, stretched for observed round-robin.

        A displaced entry's window already describes a flush drain rather
        than a normal repeat train, so it is excluded here exactly as it is
        in ``_round_robin_concurrency``.

        ``concurrency`` may be supplied by a caller that already counted it for
        this entry, so a multi-window entry counts once per classification
        rather than once per window -- this runs inside match(), on every
        received capture.
        """
        if entry.displaced:
            return window.ends_at
        if concurrency is None:
            concurrency = self._round_robin_concurrency(entry)
        return window.ends_at + _round_robin_stretch_seconds(window.train_seconds, concurrency)

    def match(self, signature: FrameSignature, heard_at: float) -> LedgerMatch | None:
        """Return the best-fitting pending or windowed confirmed command match.

        Ranked, not merely newest-first. Repeating one cover's command gives
        two entries the same signature, and the anchor-lag budget on the newer
        window reaches back across the older command's nominal window -- so
        newest-first credits the older command's own echo to its successor.
        Both are ours and neither dispatches, but the emission proof goes to
        the wrong command_id, and a cover awaiting proof for one specific
        command after a restart never gets it.

        So a capture that fits a window WITHOUT spending the anchor-lag budget
        wins outright; only when nothing fits on those terms does the budget
        get spent, newest-first as before.
        """
        # match() sweeps the ledger twice -- once refusing the anchor-lag
        # budget, once spending it -- so without this an entry considered in
        # both passes pays for the same peer scan twice.
        counts: dict[str, int] = {}
        for spend_anchor_lag in (False, True):
            for command_id in reversed(self._entries):
                entry = self._entries[command_id]
                if entry.phase == "pending" and any(
                    frame.signature == signature for frame in entry.frames
                ):
                    return "pending", entry.command_id, entry.bridge_id
                if entry.phase != "confirmed" or not any(
                    window.signature == signature for window in entry.windows
                ):
                    continue
                # Counted once for this entry, not once per window: a
                # multi-window entry that does not win outright would
                # otherwise repeat the whole peer scan per window, and that
                # is exactly the near-miss case this runs hottest in.
                concurrency = counts.get(command_id)
                if concurrency is None:
                    concurrency = counts[command_id] = self._round_robin_concurrency(entry)
                if any(
                    window.signature == signature
                    and (window.starts_at if spend_anchor_lag else window.nominal_starts_at)
                    <= heard_at
                    <= self._effective_ends_at(entry, window, concurrency)
                    for window in entry.windows
                ):
                    return "confirmed", entry.command_id, entry.bridge_id
        early_flush = self._early_flushed_stop(signature, heard_at)
        if early_flush is not None:
            return early_flush
        self._log_near_miss(signature, heard_at)
        return None

    def _early_flushed_stop(
        self,
        signature: FrameSignature,
        heard_at: float,
    ) -> LedgerMatch | None:
        """Return a command whose armed STOP a newer command has just flushed.

        A timed move's stop_raw sits armed on the bridge from handoff until its
        deadline, and latest-command-wins flushes it the instant an overlapping
        newer command lands there -- anywhere inside that span, not at the
        deadline the confirmed window describes. displace() re-windows for
        exactly this, but only once the "displaced" status arrives, and nothing
        orders that status before a peer bridge's report of the flushed frame:
        both cross the same broker, and the queueing that biases "started" late
        biases "displaced" too.

        So the flush is recognised from the command that CAUSES it instead,
        and pinned to the instant that command was admitted rather than to how
        long it stays armed -- see _flushed_at_a_newer_admission.

        Deliberately NOT a blanket widening of the armed span. Our stop_raw is
        byte-identical to the frame a person's remote puts on air, so owning
        that span whenever a STOP is heard would bypass takeover for the whole
        stop_after_ms and leave the model travelling while the blind stands
        still. Ownership needs a displacement actually in flight.
        """
        if signature[2] != "STOP":
            return None
        for entry in reversed(tuple(self._entries.values())):
            if entry.phase != "confirmed" or entry.displaced or not entry.windows:
                continue
            # An armed frame cannot have emitted before its own command did.
            if heard_at < min(window.starts_at for window in entry.windows):
                continue
            queued = next(
                (
                    window
                    for window in entry.windows
                    # Still queued: a STOP already inside or past its own
                    # window was matched above and needs no help here.
                    if window.signature == signature and heard_at < window.starts_at
                ),
                None,
            )
            if queued is None:
                continue
            if self._flushed_at_a_newer_admission(entry, heard_at, queued.train_seconds):
                return "confirmed", entry.command_id, entry.bridge_id
        return None

    def _flushed_at_a_newer_admission(
        self,
        entry: _LedgerEntry,
        heard_at: float,
        drain_seconds: float,
    ) -> bool:
        """Report whether a newer command's admission flushed this one's RF then.

        Displacement is a ONE-TIME event. The bridge flushes an older command's
        armed frames as it admits a newer one and never again, so the newer
        command's HANDOFF -- the measured instant its own first frame went on
        air -- is where the flush ENDS. The owed copies drain in full ahead of
        it, so the flush spans the victim's own train length below that.

        The displacer's own liveness is emphatically NOT the bound. A displacer
        that is itself a timed move stays armed until its own deadline, which
        firmware caps at MAX_TRAVEL_SECONDS (one hour); adjusting a blind twice
        in quick succession would then leave a real STOP on the first command's
        channels invisible for the whole remaining span of the second. That is
        the deafness _superseding_commanded_start was narrowed to eliminate,
        and it must not reappear here.

        Only same-bridge overlaps count: armed scheduler state lives in the
        selected bridge's RAM, so a command routed elsewhere cannot flush it.

        A still-PENDING newer command is deliberately not trusted, because
        its admission instant is precisely what has not happened yet. A TIMED
        displacer registers its own stop_raw, so a flushed capture shares that
        pending entry's signature and is held against it and re-decided once it
        confirms -- but that only defers the decision to this bound, it does
        not make it, so the bound has to be right for the deferral to help.

        An UNTIMED pending displacer has no such signature and the capture
        simply falls through and dispatches as a press. No ordering guarantee
        is claimed for that case: generation order and delivery order diverge
        under MQTT transport delay -- that divergence is the premise of this
        whole module -- and the peer is reporting the victim's flushed frame,
        not the displacer's own, so nothing sequences the two. It is left
        uncovered because it fails to the SAFE side: a press dispatches, a
        takeover happens, and the outcome is no worse than before any of this
        existed.
        """
        newer = False
        for candidate in self._entries.values():
            if candidate.command_id == entry.command_id:
                newer = True
                continue
            admitted_at = candidate.handoff
            if (
                not newer
                or admitted_at is None
                or candidate.displaced
                or candidate.bridge_id != entry.bridge_id
                or set(candidate.channels).isdisjoint(entry.channels)
            ):
                continue
            # The owed STOP copies drain in full before the frame whose
            # admission flushed them, so the flush occupies drain_seconds BELOW
            # that admission -- not one frame's worth. The admission anchor also
            # carries the same late bias as every other, hence lag tolerance
            # below and slack only above.
            if (
                admitted_at
                - drain_seconds
                - _LEDGER_WINDOW_SLACK_SECONDS
                - _LEDGER_ANCHOR_LAG_SECONDS
                <= heard_at
                <= admitted_at + _LEDGER_WINDOW_SLACK_SECONDS
            ):
                return True
        return False

    def _log_near_miss(self, signature: FrameSignature, heard_at: float) -> None:
        """Report a capture we own the signature of but classified as a press.

        Emitted at WARNING because it really is the only warning: the capture
        becomes a phantom physical press and takes a cover over, and every
        other symptom of that is silent -- no error, no unavailable, just a
        position that stops matching the window. It stayed invisible at DEBUG
        through two production freezes on 2026-07-25 (#21, #23), which is
        precisely the evidence this line exists to provide.

        A genuine press landing just outside a window we own logs here too.
        That false positive is worth accepting: it is rare, it names the
        command and the miss distance, and the alternative is the phantom
        takeover staying undiagnosable.
        """
        for entry in self._entries.values():
            if entry.phase != "confirmed":
                continue
            for window in entry.windows:
                if window.signature != signature:
                    continue
                # Report the bounds match() actually judged against, including
                # any round-robin stretch -- logging the nominal edge would
                # understate the miss and send a reader hunting the wrong gap.
                ends_at = self._effective_ends_at(
                    entry, window, self._round_robin_concurrency(entry)
                )
                _LOGGER.warning(
                    "state_sync: %s capture outside command %s window "
                    "[%.3f, %.3f] by %.3fs; treating as a physical press",
                    signature[2],
                    entry.command_id,
                    window.starts_at,
                    ends_at,
                    min(abs(heard_at - window.starts_at), abs(heard_at - ends_at)),
                )
                return

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
        """Build one confirmed frame window: slack above, anchor lag below."""
        frame_handoff = handoff + frame.offset_ms / _MILLISECONDS_PER_SECOND
        return _LedgerWindow(
            signature=frame.signature,
            nominal_starts_at=frame_handoff - _LEDGER_WINDOW_SLACK_SECONDS,
            train_seconds=frame.airtime_ms / _MILLISECONDS_PER_SECOND,
            # Asymmetric by design -- a frame can be heard well BEFORE the
            # window its own late anchor implies. Most acutely a stop_raw
            # frame, which fires stop_after_ms after the action frame and so is
            # always classified here rather than while its command is pending.
            starts_at=(frame_handoff - _LEDGER_WINDOW_SLACK_SECONDS - _LEDGER_ANCHOR_LAG_SECONDS),
            ends_at=(
                frame_handoff
                + frame.airtime_ms / _MILLISECONDS_PER_SECOND
                + _LEDGER_WINDOW_SLACK_SECONDS
            ),
        )

    def _is_expired(self, entry: _LedgerEntry, now: float) -> bool:
        """Return whether one entry exceeded its phase-specific lifetime.

        ``expires_at`` is computed once, at ``confirm()``, from that command's
        OWN windows only -- it cannot yet know about a peer that stretches its
        round-robin concurrency by confirming later. So a confirmed entry past
        its nominal ``expires_at`` gets one more look at its CURRENT
        round-robin-stretched close before eviction: without it, GC could
        delete the entry -- and with it, match()'s stretched window -- before
        a legitimately late own-repeat ever reached this ledger to be
        classified by it.
        """
        if entry.phase == "confirmed":
            if entry.expires_at is None:
                return False
            if now <= entry.expires_at:
                return False
            if entry.displaced or not entry.windows:
                return True
            concurrency = self._round_robin_concurrency(entry)
            latest_effective_end = max(
                self._effective_ends_at(entry, window, concurrency)
                for window in entry.windows
            )
            return now > latest_effective_end + _LEDGER_ENTRY_TTL_SECONDS
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
        self._classify(
            signature,
            heard_at,
            bridge_id,
            seen_at,
            received_at=seen_at,
            hold_pending=True,
        )

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
                received_at=capture.held_at,
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
                received_at=capture.held_at,
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
        received_at: float,
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
        self._dispatch_press(signature, heard_at, bridge_id, seen_at, received_at)

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

    def _superseding_commanded_start(
        self,
        remote_key: str,
        channels: frozenset[int],
        heard_at: float,
        received_at: float,
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
                stamp.started_at
                for (recent_remote, recent_channels), stamp in self._commanded_starts.items()
                if recent_remote == remote_key
                and not recent_channels.isdisjoint(channels)
                and heard_at < stamp.started_at <= received_at
            ),
            None,
        )

    def _dispatch_press(
        self,
        signature: FrameSignature,
        heard_at: float,
        bridge_id: str,
        seen_at: float,
        received_at: float,
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
        superseding_start = self._superseding_commanded_start(
            remote_key,
            channels,
            heard_at,
            received_at,
        )
        if superseding_start is not None:
            _LOGGER.debug(
                "state_sync: dropping %s on channels %s heard at %.3f as stale delivery -- "
                "our own start at %.3f was already on air %.3fs before it reached us",
                button,
                sorted(channels),
                heard_at,
                superseding_start,
                received_at - superseding_start,
            )
            return
        previous = self._debounce.get(signature)
        if previous is not None and abs(heard_at - previous.heard_at) <= _DEBOUNCE_WINDOW_SECONDS:
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
