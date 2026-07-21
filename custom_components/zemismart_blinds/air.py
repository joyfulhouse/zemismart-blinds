"""Cross-bridge RF air arbitration for the shared 433 MHz channel.

Every bridge paces its OWN airtime (``TargetScheduler::record_dispatch_`` in
rf433_scheduler.h), but nothing in firmware coordinates different bridges.
This process-local calendar serializes normal HA-originated trains across the
conservative collision domain and reserves known future fail-safe STOP windows.

Invariants that must survive every change here:

1. A STOP never waits. Not in shadow, not in enforcing mode.
2. Fewer than two online bridges means OFF -- exactly single-bridge behavior.
3. Every failure path publishes. Invalid or exhausted calendar state yields;
   it never becomes a reason not to transmit.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from bisect import insort_right
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from .codec import estimate_b0_slot_ms

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

type Clock = Callable[[], float]

GUARD_MS: Final = 100
MIN_BRIDGES_FOR_ARBITRATION: Final = 2
AIR_STATE_CAP: Final = 256
MAX_FEASIBILITY_PASSES: Final = AIR_STATE_CAP * 2 + 1
MAX_AIR_HOLD_MS: Final = 130_000
MAX_REPEATS: Final = 20
MAX_STOP_AFTER_MS: Final = 3_600_000

type FailOpenReason = Literal[
    "unplannable",
    "started_timeout",
    "cancelled_after_publish",
    "online_below_two",
    "pending_cap",
    "reservation_cap",
    "iteration_bound",
    "ceiling",
    "stop_preemption",
    "internal_error",
]

_FAIL_OPEN_REASONS: Final[tuple[FailOpenReason, ...]] = (
    "unplannable",
    "started_timeout",
    "cancelled_after_publish",
    "online_below_two",
    "pending_cap",
    "reservation_cap",
    "iteration_bound",
    "ceiling",
    "stop_preemption",
    "internal_error",
)


class AirMode(StrEnum):
    """Select whether calendar decisions delay normal publication."""

    ENFORCE = "enforce"
    SHADOW = "shadow"


@dataclass(frozen=True, slots=True)
class AirPlan:
    """One command's predicted occupancy of the shared channel."""

    action_ms: int
    stop_offset_ms: int | None
    stop_ms: int

    def busy_ms(self, guard_ms: int) -> int:
        """Return immediate train occupancy including its trailing guard."""
        return self.action_ms + guard_ms


@dataclass(frozen=True, slots=True)
class PendingAirPlan:
    """One published command awaiting proof of its first RF handoff."""

    bridge_id: str
    command_id: str
    boot: int | None
    plan: AirPlan
    published_at: float
    expires_at: float
    is_stop: bool


@dataclass(frozen=True, slots=True, order=True)
class AirReservation:
    """One actual future fail-safe STOP interval, sorted by time and key."""

    starts_at: float
    ends_at: float
    bridge_id: str
    command_id: str
    boot: int | None = field(compare=False)
    stop_ms: int = field(compare=False)


@dataclass(frozen=True, slots=True)
class AirDecision:
    """One immutable feasibility result and its lost-wakeup-safe event."""

    earliest: float
    event: asyncio.Event
    should_wait: bool
    would_wait: bool
    disabled: bool = False
    fail_open: bool = False


@dataclass(slots=True)
class AirStats:
    """Bounded counters for shadow calculation and enforced holds."""

    planned: int = 0
    unplannable: int = 0
    would_wait: int = 0
    would_wait_total_ms: int = 0
    would_wait_max_ms: int = 0
    commands_held: int = 0
    held_total_ms: int = 0
    held_max_ms: int = 0
    stop_bypasses: int = 0
    stop_window_conflicts: int = 0
    ceiling_hits: int = 0
    fail_opens: int = 0
    reservation_evictions: int = 0
    disabled_single_bridge: int = 0
    waits_by_bridge: dict[str, int] = field(default_factory=dict)
    fail_open_reasons: dict[FailOpenReason, int] = field(
        default_factory=lambda: {reason: 0 for reason in _FAIL_OPEN_REASONS}
    )

    def as_dict(
        self,
        *,
        mode: AirMode,
        active_reservations: int,
        pending_starts: int,
    ) -> dict[str, object]:
        """Return a stable diagnostics snapshot."""
        return {
            "mode": mode.value,
            "planned": self.planned,
            "unplannable": self.unplannable,
            "would_wait": self.would_wait,
            "would_wait_total_ms": self.would_wait_total_ms,
            "would_wait_max_ms": self.would_wait_max_ms,
            "commands_held": self.commands_held,
            "held_total_ms": self.held_total_ms,
            "held_max_ms": self.held_max_ms,
            "stop_bypasses": self.stop_bypasses,
            "stop_window_conflicts": self.stop_window_conflicts,
            "ceiling_hits": self.ceiling_hits,
            "fail_opens": self.fail_opens,
            "reservation_evictions": self.reservation_evictions,
            "disabled_single_bridge": self.disabled_single_bridge,
            "waits_by_bridge": dict(self.waits_by_bridge),
            "active_reservations": active_reservations,
            "pending_starts": pending_starts,
            "fail_open_reasons": dict(self.fail_open_reasons),
        }


def plan_for_body(body: Mapping[str, object]) -> AirPlan | None:
    """Build occupancy from the exact final body, or decline to guess."""
    raw = body.get("raw")
    repeats = body.get("repeats")
    if (
        not isinstance(raw, str)
        or isinstance(repeats, bool)
        or not isinstance(repeats, int)
        or not 1 <= repeats <= MAX_REPEATS
    ):
        return None
    trailer = body.get("trailer_raw")
    if trailer is not None and not isinstance(trailer, str):
        return None
    has_stop_raw = "stop_raw" in body
    has_stop_after = "stop_after_ms" in body
    if has_stop_raw != has_stop_after:
        return None
    stop_raw = body.get("stop_raw")
    stop_after = body.get("stop_after_ms")
    if has_stop_raw and (
        not isinstance(stop_raw, str)
        or isinstance(stop_after, bool)
        or not isinstance(stop_after, int)
        or not 1 <= stop_after <= MAX_STOP_AFTER_MS
    ):
        return None
    try:
        action_ms = repeats * estimate_b0_slot_ms(raw)
        if isinstance(trailer, str):
            action_ms += repeats * estimate_b0_slot_ms(trailer)
        stop_ms = repeats * estimate_b0_slot_ms(stop_raw) if isinstance(stop_raw, str) else 0
    except ValueError:
        return None
    return AirPlan(
        action_ms=action_ms,
        stop_offset_ms=stop_after if isinstance(stop_after, int) else None,
        stop_ms=stop_ms,
    )


def _intervals(
    plan: AirPlan,
    start: float,
    guard_ms: int,
) -> tuple[tuple[float, float], tuple[float, float] | None]:
    """Return the immediate and optional future STOP half-open intervals."""
    immediate_end = start + (plan.action_ms + guard_ms) / 1_000
    stop_offset = plan.stop_offset_ms
    if stop_offset is None:
        return (start, immediate_end), None
    stop_end = start + (stop_offset + plan.stop_ms + guard_ms) / 1_000
    if stop_offset <= plan.action_ms:
        return (start, max(immediate_end, stop_end)), None
    future = (
        start + (stop_offset - guard_ms) / 1_000,
        stop_end,
    )
    return (start, immediate_end), future


def _intersects(first: tuple[float, float], second: tuple[float, float]) -> bool:
    """Return whether two half-open intervals overlap."""
    return first[0] < second[1] and second[0] < first[1]


def _milliseconds(seconds: float) -> int:
    """Floor elapsed monotonic seconds to integer milliseconds."""
    return math.floor(max(0.0, seconds) * 1_000 + 1e-6)


class AirArbiter:
    """Own the bounded monotonic calendar for one collision domain."""

    def __init__(
        self,
        *,
        mode: AirMode = AirMode.ENFORCE,
        guard_ms: int = GUARD_MS,
        monotonic_now: Clock = time.monotonic,
    ) -> None:
        """Initialize an empty calendar."""
        self.mode = mode
        self._guard_ms = guard_ms
        self._monotonic_now = monotonic_now
        self._online_bridges = 0
        self._bridges: dict[str, tuple[bool, int | None]] = {}
        self._pending: dict[tuple[str, str], PendingAirPlan] = {}
        self._drain_until_by_bridge: dict[str, float] = {}
        self._reservations: list[AirReservation] = []
        self._wake_event = asyncio.Event()
        self._closed = False
        self.stats = AirStats()
        _LOGGER.info(
            "air: mode=%s guard=%dms ceiling=%dms reservation_cap=%d",
            mode.value,
            guard_ms,
            MAX_AIR_HOLD_MS,
            AIR_STATE_CAP,
        )

    @property
    def current_event(self) -> asyncio.Event:
        """Return the event representing the current calendar generation."""
        return self._wake_event

    def _wake(self) -> None:
        """Wake every current waiter and rotate to a fresh generation."""
        self._wake_event.set()
        self._wake_event = asyncio.Event()

    def wake(self) -> None:
        """Wake waiters after an external command-lifecycle change."""
        self._wake()

    def _record_fail_open(self, reason: FailOpenReason) -> None:
        """Increment one reason and the aggregate fail-open count."""
        self.stats.fail_opens += 1
        self.stats.fail_open_reasons[reason] += 1

    def record_plan(self, *, plannable: bool) -> None:
        """Count one final command plan exactly once."""
        if plannable:
            self.stats.planned += 1
            return
        self.stats.unplannable += 1
        self._record_fail_open("unplannable")

    def record_disabled(self) -> None:
        """Count one command published while arbitration is OFF."""
        self.stats.disabled_single_bridge += 1

    def record_shadow_wait(self, bridge_id: str, seconds: float) -> None:
        """Count the first delay shadow mode would have imposed."""
        wait_ms = _milliseconds(seconds)
        self.stats.would_wait += 1
        self.stats.would_wait_total_ms += wait_ms
        self.stats.would_wait_max_ms = max(self.stats.would_wait_max_ms, wait_ms)
        self.stats.waits_by_bridge[bridge_id] = self.stats.waits_by_bridge.get(bridge_id, 0) + 1

    def record_hold_started(self, bridge_id: str) -> None:
        """Count one command's first enforcing wait."""
        self.stats.commands_held += 1
        self.stats.waits_by_bridge[bridge_id] = self.stats.waits_by_bridge.get(bridge_id, 0) + 1
        _LOGGER.debug("air: %s held", bridge_id)

    def record_hold_finished(self, bridge_id: str, seconds: float) -> None:
        """Record one held command's terminal elapsed delay."""
        held_ms = _milliseconds(seconds)
        self.stats.held_total_ms += held_ms
        self.stats.held_max_ms = max(self.stats.held_max_ms, held_ms)
        _LOGGER.debug("air: %s released after %d ms", bridge_id, held_ms)

    def record_ceiling_hit(self) -> None:
        """Record publication at the absolute arbitration ceiling."""
        self.stats.ceiling_hits += 1
        self._record_fail_open("ceiling")
        _LOGGER.warning("air: %d ms hold ceiling reached; publishing", MAX_AIR_HOLD_MS)

    def record_online_fail_open(self) -> None:
        """Record one held command released because arbitration turned OFF."""
        self._record_fail_open("online_below_two")

    def record_stop_preemption(self) -> None:
        """Record a raw command forced open to preserve STOP ordering."""
        self._record_fail_open("stop_preemption")
        _LOGGER.warning("air: held raw frame forced open for a following STOP")

    def record_internal_error(self) -> None:
        """Record an unexpected calendar exception at the hub boundary."""
        self._record_fail_open("internal_error")

    def record_cancelled_after_publish(self) -> None:
        """Record uncertain RF state after execution-task cancellation."""
        self._record_fail_open("cancelled_after_publish")

    def update_bridges(
        self,
        bridges: Mapping[str, tuple[bool, int | None]],
        *,
        now: float,
    ) -> None:
        """Apply availability/boot state and retire proven old RAM state."""
        self._prune(now)
        snapshot = dict(bridges)
        self._bridges = snapshot
        self._online_bridges = sum(online for online, _boot in snapshot.values())

        stale_pending = [
            key
            for key, pending in self._pending.items()
            if pending.boot is not None
            and (current := snapshot.get(pending.bridge_id)) is not None
            and current[1] is not None
            and current[1] != pending.boot
        ]
        for key in stale_pending:
            del self._pending[key]
        retained = [
            reservation
            for reservation in self._reservations
            if reservation.boot is None
            or (current := snapshot.get(reservation.bridge_id)) is None
            or current[1] is None
            or current[1] == reservation.boot
        ]
        if stale_pending or len(retained) != len(self._reservations):
            self._reservations = retained
        # This hook is also the injected-clock test seam: every explicit
        # notification invalidates decisions even when the snapshot is equal.
        self._wake()

    def _prune(self, now: float) -> None:
        """Drop every naturally expired bounded state record."""
        expired_pending = [
            key for key, pending in self._pending.items() if pending.expires_at <= now
        ]
        for key in expired_pending:
            del self._pending[key]
            self._record_fail_open("started_timeout")
            _LOGGER.warning("air: started status timed out; calendar failed open")
        self._reservations = [
            reservation for reservation in self._reservations if reservation.ends_at > now
        ]
        expired_drains = [
            bridge_id
            for bridge_id, ends_at in self._drain_until_by_bridge.items()
            if ends_at <= now
        ]
        for bridge_id in expired_drains:
            del self._drain_until_by_bridge[bridge_id]
        if expired_pending:
            self._wake()

    def decide(self, bridge_id: str, plan: AirPlan, *, now: float) -> AirDecision:
        """Calculate one normal command's earliest feasible publish time."""
        self._prune(now)
        event = self._wake_event
        if self._closed or self._online_bridges < MIN_BRIDGES_FOR_ARBITRATION:
            return AirDecision(now, event, False, False, disabled=True)

        pending_expiries = [
            pending.expires_at
            for pending in self._pending.values()
            if pending.bridge_id != bridge_id
        ]
        if pending_expiries:
            earliest = min(pending_expiries)
            would_wait = earliest > now
            return AirDecision(
                earliest,
                event,
                would_wait and self.mode is AirMode.ENFORCE,
                would_wait,
            )

        other_drain = max(
            (
                ends_at
                for owner, ends_at in self._drain_until_by_bridge.items()
                if owner != bridge_id
            ),
            default=now,
        )
        start = max(now, other_drain)
        for _pass in range(MAX_FEASIBILITY_PASSES):
            immediate, future_stop = _intervals(plan, start, self._guard_ms)
            required = max(start, other_drain)
            for reservation in self._reservations:
                if reservation.bridge_id == bridge_id:
                    continue
                reserved = (reservation.starts_at, reservation.ends_at)
                if _intersects(immediate, reserved):
                    required = max(required, reservation.ends_at)
                if future_stop is not None and _intersects(future_stop, reserved):
                    assert plan.stop_offset_ms is not None
                    required = max(
                        required,
                        reservation.ends_at - (plan.stop_offset_ms - self._guard_ms) / 1_000,
                    )
            if required <= start:
                would_wait = start > now
                return AirDecision(
                    start,
                    event,
                    would_wait and self.mode is AirMode.ENFORCE,
                    would_wait,
                )
            start = required

        self._record_fail_open("iteration_bound")
        _LOGGER.warning("air: feasibility iteration bound reached; publishing")
        return AirDecision(now, event, False, False, fail_open=True)

    def probe_stop(self, bridge_id: str, plan: AirPlan, *, now: float) -> bool:
        """Record whether an explicit STOP bypasses known other-bridge state."""
        self._prune(now)
        immediate, _future = _intervals(plan, now, self._guard_ms)
        conflict = any(pending.bridge_id != bridge_id for pending in self._pending.values()) or any(
            owner != bridge_id and ends_at > now
            for owner, ends_at in self._drain_until_by_bridge.items()
        )
        if not conflict:
            conflict = any(
                reservation.bridge_id != bridge_id
                and _intersects(immediate, (reservation.starts_at, reservation.ends_at))
                for reservation in self._reservations
            )
        if conflict:
            self.stats.stop_bypasses += 1
        return conflict

    def provision(
        self,
        *,
        bridge_id: str,
        command_id: str,
        boot: int | None,
        plan: AirPlan,
        published_at: float,
        expires_at: float,
        is_stop: bool,
    ) -> bool:
        """Commit one final published plan before scheduling its publisher."""
        self._prune(published_at)
        key = (bridge_id, command_id)
        if key not in self._pending and len(self._pending) >= AIR_STATE_CAP:
            self.stats.reservation_evictions += 1
            self._record_fail_open("pending_cap")
            _LOGGER.warning("air: pending-start cap reached; publishing without correlation state")
            self._wake()
            return False
        self._pending[key] = PendingAirPlan(
            bridge_id=bridge_id,
            command_id=command_id,
            boot=boot,
            plan=plan,
            published_at=published_at,
            expires_at=expires_at,
            is_stop=is_stop,
        )
        self._wake()
        return True

    def _insert_reservation(self, reservation: AirReservation) -> None:
        """Insert one sorted reservation, retaining the nearest cap entries."""
        self._reservations = [
            current
            for current in self._reservations
            if (current.bridge_id, current.command_id)
            != (reservation.bridge_id, reservation.command_id)
        ]
        if len(self._reservations) < AIR_STATE_CAP:
            insort_right(self._reservations, reservation)
            return
        candidates = [*self._reservations, reservation]
        candidates.sort()
        self._reservations = candidates[:AIR_STATE_CAP]
        self.stats.reservation_evictions += 1
        self._record_fail_open("reservation_cap")
        _LOGGER.warning("air: reservation cap reached; farthest STOP window dropped")

    def started(
        self,
        bridge_id: str,
        command_id: str,
        *,
        started_at: float,
        boot: int | None,
        now: float,
    ) -> bool:
        """Replace one provisional record with actual-start calendar state."""
        self._prune(now)
        pending = self._pending.pop((bridge_id, command_id), None)
        if pending is None:
            return False
        actual_boot = boot if boot is not None else pending.boot
        immediate, future_stop = _intervals(pending.plan, started_at, self._guard_ms)
        if immediate[1] > now:
            self._drain_until_by_bridge[bridge_id] = max(
                immediate[1],
                self._drain_until_by_bridge.get(bridge_id, immediate[1]),
            )
        if future_stop is not None and future_stop[1] > now:
            for current in self._reservations:
                if current.bridge_id != bridge_id and _intersects(
                    future_stop,
                    (current.starts_at, current.ends_at),
                ):
                    self.stats.stop_window_conflicts += 1
            self._insert_reservation(
                AirReservation(
                    starts_at=future_stop[0],
                    ends_at=future_stop[1],
                    bridge_id=bridge_id,
                    command_id=command_id,
                    boot=actual_boot,
                    stop_ms=pending.plan.stop_ms,
                )
            )
        self._wake()
        return True

    def release_pending(
        self,
        bridge_id: str,
        command_id: str,
        *,
        fail_open_reason: FailOpenReason | None = None,
    ) -> bool:
        """Remove one unstarted command and optionally record uncertainty."""
        removed = self._pending.pop((bridge_id, command_id), None)
        if removed is None:
            return False
        if fail_open_reason is not None:
            self._record_fail_open(fail_open_reason)
            if fail_open_reason == "started_timeout":
                _LOGGER.warning("air: started status timed out; calendar failed open")
        self._wake()
        return True

    def displaced(self, bridge_id: str, command_id: str, *, now: float) -> bool:
        """Retire a victim and turn an owed future STOP into current drain."""
        self._prune(now)
        key = (bridge_id, command_id)
        changed = self._pending.pop(key, None) is not None
        victim = next(
            (
                reservation
                for reservation in self._reservations
                if (reservation.bridge_id, reservation.command_id) == key
            ),
            None,
        )
        if victim is not None:
            self._reservations.remove(victim)
            drain_end = now + (victim.stop_ms + self._guard_ms) / 1_000
            self._drain_until_by_bridge[bridge_id] = max(
                drain_end,
                self._drain_until_by_bridge.get(bridge_id, drain_end),
            )
            changed = True
        if changed:
            self._wake()
        return changed

    def disarmed(self, bridge_id: str, command_id: str, *, now: float) -> bool:
        """Remove pending/future state without shortening current drain."""
        self._prune(now)
        key = (bridge_id, command_id)
        changed = self._pending.pop(key, None) is not None
        retained = [
            reservation
            for reservation in self._reservations
            if (reservation.bridge_id, reservation.command_id) != key
        ]
        if len(retained) != len(self._reservations):
            self._reservations = retained
            changed = True
        if changed:
            self._wake()
        return changed

    def drain_until(self, bridge_id: str, *, now: float) -> float | None:
        """Return one bridge's non-expired current drain horizon."""
        self._prune(now)
        return self._drain_until_by_bridge.get(bridge_id)

    def reservation_snapshot(self, *, now: float) -> tuple[AirReservation, ...]:
        """Return the current sorted future reservations for tests/diagnostics."""
        self._prune(now)
        return tuple(self._reservations)

    def pending_count(self, *, now: float) -> int:
        """Return the current provisional-plan count after expiry."""
        self._prune(now)
        return len(self._pending)

    def stats_snapshot(self, *, now: float) -> dict[str, object]:
        """Return pruned shadow and enforcement statistics."""
        self._prune(now)
        return self.stats.as_dict(
            mode=self.mode,
            active_reservations=len(self._reservations),
            pending_starts=len(self._pending),
        )

    def close(self) -> None:
        """Wake waiters and clear every process-local calendar record."""
        self._closed = True
        self._pending.clear()
        self._reservations.clear()
        self._drain_until_by_bridge.clear()
        self._wake()
