"""Cross-bridge RF air arbitration for the shared 433 MHz channel.

Every bridge paces its OWN airtime (``TargetScheduler::record_dispatch_`` in
rf433_scheduler.h), but nothing coordinates across bridges: the hub releases a
command as soon as the bridge reports ``started`` -- first RF dispatch -- while
the rest of its repeat train, trailer and later timed STOP are still on air. A
scene fanning out to several bridges therefore keys the channel concurrently,
and 18 of 21 bridge pairs in the reference deployment can hear each other
(docs/claude/2026-07-20-bridge-overlap-graph-measurement.md).

This module owns the calendar that closes that gap. It runs in SHADOW mode
first: it computes what it *would* have delayed and records it, without
delaying anything, so real contention can be measured before any latency is
paid.

Invariants that must survive any future change here:

1. A STOP never waits. Not in shadow, not in enforcing mode.
2. Fewer than two online bridges means OFF -- exactly today's behavior.
3. Every failure path publishes. A calendar that cannot compute an estimate
   yields immediately; it never becomes a reason not to transmit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from .codec import estimate_b0_slot_ms

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

# Cross-bridge guard beyond a predicted train end. Derived from the observed
# 10-50 ms MQTT delivery scale plus one 5 ms firmware tick, event-loop
# scheduling and rounding; doubling the 50 ms upper typical value is a
# deliberately conservative start. Tune from shadow-mode p99, never below the
# firmware's own per-frame margin.
GUARD_MS: Final = 100
MIN_BRIDGES_FOR_ARBITRATION: Final = 2


@dataclass(frozen=True, slots=True)
class AirPlan:
    """One command's predicted occupancy of the shared channel."""

    action_ms: int
    # Carried for Phase 2, which must reserve the future fail-safe STOP window
    # so later normal work does not collide with it. Phase 1 does NOT model it:
    # the shadow horizon covers only the immediate train, so a STOP whose
    # deadline lands inside another command's train is not yet measured.
    stop_offset_ms: int | None
    stop_ms: int

    def busy_ms(self, guard_ms: int) -> int:
        """Return how long the immediate train occupies air, including guard.

        The guard is passed in rather than read from the module constant so a
        tuned arbiter actually changes the horizon it enforces.
        """
        return self.action_ms + guard_ms


@dataclass(slots=True)
class ShadowStats:
    """Counters describing what enforcement WOULD have done."""

    planned: int = 0
    unplannable: int = 0
    would_wait: int = 0
    would_wait_total_ms: int = 0
    would_wait_max_ms: int = 0
    stop_bypasses: int = 0
    disabled_single_bridge: int = 0
    waits_by_bridge: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Return a log/diagnostics-friendly snapshot."""
        return {
            "planned": self.planned,
            "unplannable": self.unplannable,
            "would_wait": self.would_wait,
            "would_wait_total_ms": self.would_wait_total_ms,
            "would_wait_max_ms": self.would_wait_max_ms,
            "stop_bypasses": self.stop_bypasses,
            "disabled_single_bridge": self.disabled_single_bridge,
            "waits_by_bridge": dict(self.waits_by_bridge),
        }


def plan_for_body(body: Mapping[str, object]) -> AirPlan | None:
    """Build one command's occupancy plan from the body actually published.

    Returns None when the body is not a plannable movement command; the caller
    must treat that as "publish immediately", never as a rejection.
    """
    raw = body.get("raw")
    if not isinstance(raw, str):
        return None
    repeats = body.get("repeats")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        return None
    try:
        action_ms = repeats * estimate_b0_slot_ms(raw)
        trailer = body.get("trailer_raw")
        if isinstance(trailer, str):
            action_ms += repeats * estimate_b0_slot_ms(trailer)
        stop_raw = body.get("stop_raw")
        stop_ms = repeats * estimate_b0_slot_ms(stop_raw) if isinstance(stop_raw, str) else 0
    except ValueError:
        # A frame the estimator cannot parse is one we must not schedule
        # around. Publish it and count it; do not guess an occupancy.
        return None
    stop_after = body.get("stop_after_ms")
    stop_offset = (
        stop_after
        if not isinstance(stop_after, bool) and isinstance(stop_after, int) and stop_after >= 0
        else None
    )
    return AirPlan(action_ms=action_ms, stop_offset_ms=stop_offset, stop_ms=stop_ms)


class AirArbiter:
    """Track predicted channel occupancy across every bridge.

    Shadow mode only: :meth:`observe` reports what a waiter would have paid
    without making anyone wait.
    """

    def __init__(self, *, guard_ms: int = GUARD_MS) -> None:
        """Initialize an empty calendar."""
        self._guard_ms = guard_ms
        self._busy_until: float | None = None
        self._busy_owner: str | None = None
        self.stats = ShadowStats()

    def observe(
        self,
        *,
        bridge_id: str,
        body: Mapping[str, object],
        is_stop: bool,
        online_bridges: int,
        now: float,
    ) -> int:
        """Record one about-to-publish command, returning the would-be wait ms.

        Shadow mode: the return value is advisory. It is always safe to ignore.
        """
        if online_bridges < MIN_BRIDGES_FOR_ARBITRATION:
            self.stats.disabled_single_bridge += 1
            return 0

        plan = plan_for_body(body)
        if plan is None:
            self.stats.unplannable += 1
            return 0
        self.stats.planned += 1

        wait_ms = 0
        if (
            self._busy_until is not None
            and now < self._busy_until
            # SAME-bridge work is already serialized by that bridge's own
            # TargetScheduler, which paces every handoff by real airtime.
            # Counting it as cross-bridge contention inflates the shadow
            # numbers this phase exists to produce, and would add pointless
            # latency in Phase 2.
            and bridge_id != self._busy_owner
        ):
            wait_ms = int((self._busy_until - now) * 1_000)

        if is_stop:
            # A STOP is never held, by design: turning a possible STOP
            # collision into a guaranteed late STOP is the worse trade. It
            # still extends the horizon for later normal work.
            if wait_ms > 0:
                self.stats.stop_bypasses += 1
        elif wait_ms > 0:
            self.stats.would_wait += 1
            self.stats.would_wait_total_ms += wait_ms
            self.stats.would_wait_max_ms = max(self.stats.would_wait_max_ms, wait_ms)
            self.stats.waits_by_bridge[bridge_id] = self.stats.waits_by_bridge.get(bridge_id, 0) + 1
            _LOGGER.debug(
                "air: %s would wait %d ms behind %s (train %d ms)",
                bridge_id,
                wait_ms,
                self._busy_owner,
                plan.action_ms,
            )

        # In shadow mode the command publishes now, so the horizon advances
        # from now regardless of what the wait would have been.
        busy_until = now + plan.busy_ms(self._guard_ms) / 1_000
        if self._busy_until is None or busy_until > self._busy_until:
            self._busy_until = busy_until
            self._busy_owner = bridge_id
        return wait_ms

    def reset(self) -> None:
        """Drop all horizon state, keeping accumulated statistics."""
        self._busy_until = None
        self._busy_owner = None
