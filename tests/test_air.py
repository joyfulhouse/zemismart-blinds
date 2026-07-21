"""Tests for enforcing cross-bridge RF air arbitration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

import custom_components.zemismart_blinds.air as air_module
from custom_components.zemismart_blinds.air import (
    AIR_STATE_CAP,
    GUARD_MS,
    MAX_AIR_HOLD_MS,
    AirArbiter,
    AirMode,
    AirPlan,
    plan_for_body,
)
from custom_components.zemismart_blinds.codec import (
    encode_b0,
    estimate_b0_slot_ms,
    make_payload,
)
from tests.synthetic import TEST_BASES, TEST_PREFIX, TEST_REMOTE_ID

if TYPE_CHECKING:
    from collections.abc import Mapping


def _frame(button: str = "UP", channels: tuple[int, ...] = (1,)) -> str:
    """Build one production-shaped movement frame."""
    return encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, channels, button, bases=TEST_BASES))


def _body(
    *,
    repeats: int = 3,
    trailer: bool = False,
    stop_after_ms: int | None = None,
) -> dict[str, object]:
    """Build one plannable final command body."""
    body: dict[str, object] = {"raw": _frame(), "repeats": repeats}
    if trailer:
        body["trailer_raw"] = _frame("TRAILER")
    if stop_after_ms is not None:
        body["stop_after_ms"] = stop_after_ms
        body["stop_raw"] = _frame("STOP")
    return body


def _plan(
    *,
    repeats: int = 3,
    trailer: bool = False,
    stop_after_ms: int | None = None,
) -> AirPlan:
    """Return a plan for a known-valid test body."""
    plan = plan_for_body(
        _body(
            repeats=repeats,
            trailer=trailer,
            stop_after_ms=stop_after_ms,
        )
    )
    assert plan is not None
    return plan


def _fail_open_reasons(snapshot: Mapping[str, object]) -> dict[str, int]:
    """Narrow the fixed diagnostics reason mapping for strict typing."""
    return cast("dict[str, int]", snapshot["fail_open_reasons"])


def _online(arbiter: AirArbiter, count: int = 2, *, boot: int = 1, now: float = 0.0) -> None:
    """Publish one deterministic online bridge snapshot."""
    arbiter.update_bridges(
        {f"bridge-{index}": (True, boot) for index in range(count)},
        now=now,
    )


def _start(
    arbiter: AirArbiter,
    *,
    bridge_id: str,
    command_id: str,
    plan: AirPlan,
    started_at: float,
    now: float | None = None,
    boot: int = 1,
    is_stop: bool = False,
) -> None:
    """Provision and confirm one command."""
    assert arbiter.provision(
        bridge_id=bridge_id,
        command_id=command_id,
        boot=boot,
        plan=plan,
        published_at=started_at,
        expires_at=started_at + 32.0,
        is_stop=is_stop,
    )
    arbiter.started(
        bridge_id,
        command_id,
        started_at=started_at,
        boot=boot,
        now=started_at if now is None else now,
    )


def test_slot_matches_the_firmware_dispatch_contract() -> None:
    """The estimator must reproduce the firmware's occupancy arithmetic."""
    frame = _frame()
    assert estimate_b0_slot_ms(frame) == 609
    assert estimate_b0_slot_ms(frame, repeat_gap_ms=100_000) == 100_000


def test_slot_rejects_unparseable_frames_instead_of_guessing() -> None:
    """A bad estimate would silently mis-schedule the air, so refuse it."""
    for bad in ("", "not-hex", "AAB10101", "AAB0FF0108FFFF0855"):
        with pytest.raises(ValueError):
            estimate_b0_slot_ms(bad)


def test_zero_airtime_frame_matches_firmware_margin_only_hold() -> None:
    """A valid zero-airtime frame still pays firmware margin or repeat gap."""
    frame = "AAB005" + "01" + "08" + "0000" + "08" + "55"
    assert estimate_b0_slot_ms(frame, repeat_gap_ms=0) == 5
    assert estimate_b0_slot_ms(frame) == 35


def test_plan_covers_three_repeat_action_trailer_and_stop_trains() -> None:
    """Every emitted frame family contributes all three production repeats."""
    action, trailer, stop = _frame("UP"), _frame("TRAILER"), _frame("STOP")
    plan = plan_for_body(
        {
            "raw": action,
            "trailer_raw": trailer,
            "stop_raw": stop,
            "repeats": 3,
            "stop_after_ms": 3_000,
        }
    )
    assert plan is not None
    assert plan.action_ms == 3 * estimate_b0_slot_ms(action) + 3 * estimate_b0_slot_ms(trailer)
    assert plan.stop_ms == 3 * estimate_b0_slot_ms(stop)
    assert plan.stop_offset_ms == 3_000


@pytest.mark.parametrize(
    "body",
    [
        {"repeats": 3},
        {"raw": _frame(), "repeats": 0},
        {"raw": _frame(), "repeats": 21},
        {"raw": _frame(), "repeats": True},
        {"raw": _frame(), "repeats": "3"},
        {"raw": "garbage", "repeats": 3},
        {"raw": _frame(), "trailer_raw": 1, "repeats": 3},
        {"raw": _frame(), "stop_raw": _frame("STOP"), "repeats": 3},
        {"raw": _frame(), "stop_after_ms": 1_000, "repeats": 3},
        {"raw": _frame(), "stop_raw": _frame("STOP"), "stop_after_ms": 0, "repeats": 3},
        {
            "raw": _frame(),
            "stop_raw": _frame("STOP"),
            "stop_after_ms": 3_600_001,
            "repeats": 3,
        },
    ],
)
def test_unplannable_bodies_yield_none_rather_than_a_guess(body: Mapping[str, object]) -> None:
    """Anything outside the firmware plan contract must fail open."""
    assert plan_for_body(body) is None


def test_single_bridge_is_off_in_enforce_and_shadow_modes() -> None:
    """One transmitter cannot create cross-bridge contention."""
    for mode in AirMode:
        arbiter = AirArbiter(mode=mode)
        _online(arbiter, 1)
        decision = arbiter.decide("bridge-0", _plan(), now=1.0)
        assert decision.disabled
        assert not decision.should_wait
        assert not decision.would_wait


def test_actual_start_holds_only_other_bridges_in_both_directions() -> None:
    """Same-bridge work is exempt; either genuinely different bridge waits."""
    plan = _plan()
    for owner, peer in (("bridge-0", "bridge-1"), ("bridge-1", "bridge-0")):
        arbiter = AirArbiter()
        _online(arbiter)
        _start(
            arbiter,
            bridge_id=owner,
            command_id=f"started-{owner}",
            plan=plan,
            started_at=10.0,
        )
        assert not arbiter.decide(owner, plan, now=10.1).should_wait
        peer_decision = arbiter.decide(peer, plan, now=10.1)
        assert peer_decision.should_wait
        assert peer_decision.earliest == pytest.approx(11.927)


def test_tuned_guard_changes_the_actual_start_horizon() -> None:
    """The configured guard, not the module default, controls peer feasibility."""
    plan = _plan()
    tight = AirArbiter(guard_ms=0)
    wide = AirArbiter(guard_ms=5_000)
    for arbiter in (tight, wide):
        _online(arbiter)
        _start(
            arbiter,
            bridge_id="bridge-0",
            command_id="owner",
            plan=plan,
            started_at=10.0,
        )

    probe_at = 10.0 + plan.action_ms / 1_000 + 0.001
    assert not tight.decide("bridge-1", plan, now=probe_at).should_wait
    assert wide.decide("bridge-1", plan, now=probe_at).should_wait


def test_half_open_adjacency_is_feasible() -> None:
    """A normal interval ending exactly at a STOP pre-guard does not overlap."""
    arbiter = AirArbiter()
    _online(arbiter)
    owner = AirPlan(action_ms=1_000, stop_offset_ms=5_000, stop_ms=1_000)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="timed",
        plan=owner,
        started_at=0.0,
    )
    candidate = AirPlan(action_ms=1_000, stop_offset_ms=None, stop_ms=0)
    decision = arbiter.decide("bridge-1", candidate, now=3.8)
    assert not decision.should_wait
    assert decision.earliest == 3.8


def test_deadline_inside_own_train_becomes_one_union_hold() -> None:
    """No command can be placed between an action and its preempting STOP."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = AirPlan(action_ms=1_000, stop_offset_ms=500, stop_ms=700)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="union",
        plan=plan,
        started_at=10.0,
    )
    assert arbiter.drain_until("bridge-0", now=10.0) == pytest.approx(11.3)
    assert arbiter.reservation_snapshot(now=10.0) == ()


def test_future_conflict_moves_candidate_stop_pre_guard_after_reservation() -> None:
    """The future-window shift formula is applied on the monotonic seconds axis."""
    arbiter = AirArbiter()
    _online(arbiter)
    timed = AirPlan(action_ms=1_000, stop_offset_ms=5_000, stop_ms=1_000)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="owner",
        plan=timed,
        started_at=0.0,
    )
    decision = arbiter.decide("bridge-1", timed, now=0.0)
    assert decision.should_wait
    assert decision.earliest == pytest.approx(1.2)


def test_pending_start_blocks_other_bridge_until_actual_start_or_expiry() -> None:
    """Publication is a start-unknown blocker, never an actual-time horizon."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = _plan()
    assert arbiter.provision(
        bridge_id="bridge-0",
        command_id="pending",
        boot=1,
        plan=plan,
        published_at=1.0,
        expires_at=33.0,
        is_stop=False,
    )
    decision = arbiter.decide("bridge-1", plan, now=2.0)
    assert decision.should_wait
    assert decision.earliest == 33.0
    assert not arbiter.decide("bridge-0", plan, now=2.0).should_wait


def test_started_replaces_provisional_with_age_anchored_actual_state() -> None:
    """The supplied actual monotonic handoff wholly replaces publication timing."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = _plan()
    assert arbiter.provision(
        bridge_id="bridge-0",
        command_id="late-status",
        boot=1,
        plan=plan,
        published_at=50.0,
        expires_at=82.0,
        is_stop=False,
    )
    arbiter.started(
        "bridge-0",
        "late-status",
        started_at=45.0,
        boot=1,
        now=50.0,
    )
    assert arbiter.pending_count(now=50.0) == 0
    assert arbiter.drain_until("bridge-0", now=50.0) is None


def test_two_actual_stop_reservations_overlap_is_counted() -> None:
    """Unexpected real STOP-window overlap remains live and observable."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = AirPlan(action_ms=100, stop_offset_ms=5_000, stop_ms=1_000)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="first",
        plan=plan,
        started_at=0.0,
    )
    _start(
        arbiter,
        bridge_id="bridge-1",
        command_id="second",
        plan=plan,
        started_at=0.05,
    )
    stats = arbiter.stats_snapshot(now=0.05)
    assert stats["stop_window_conflicts"] == 1
    assert stats["active_reservations"] == 2
    assert arbiter.probe_stop("bridge-0", AirPlan(100, None, 0), now=0.05)
    assert arbiter.stats_snapshot(now=0.05)["stop_bypasses"] == 1


def test_displacement_converts_reservation_to_current_drain() -> None:
    """Owed STOP copies move from a future window to a current bridge hold."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = AirPlan(action_ms=100, stop_offset_ms=5_000, stop_ms=1_000)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="victim",
        plan=plan,
        started_at=0.0,
    )
    assert arbiter.displaced("bridge-0", "victim", now=1.0)
    assert arbiter.reservation_snapshot(now=1.0) == ()
    assert arbiter.drain_until("bridge-0", now=1.0) == pytest.approx(2.1)


def test_disarm_removes_future_state_without_shortening_current_drain() -> None:
    """Disarm proves the future STOP is gone, not that a handed frame ended."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = AirPlan(action_ms=1_000, stop_offset_ms=5_000, stop_ms=1_000)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="armed",
        plan=plan,
        started_at=0.0,
    )
    assert arbiter.disarmed("bridge-0", "armed", now=0.2)
    assert arbiter.reservation_snapshot(now=0.2) == ()
    assert arbiter.drain_until("bridge-0", now=0.2) == pytest.approx(1.1)


def test_offline_retains_reservation_but_changed_boot_removes_it() -> None:
    """Only positive reboot evidence retires bridge-owned scheduler state."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = AirPlan(action_ms=100, stop_offset_ms=5_000, stop_ms=1_000)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="armed",
        plan=plan,
        started_at=0.0,
    )
    arbiter.update_bridges(
        {"bridge-0": (False, 1), "bridge-1": (True, 1)},
        now=1.0,
    )
    assert len(arbiter.reservation_snapshot(now=1.0)) == 1
    arbiter.update_bridges(
        {"bridge-0": (False, None), "bridge-1": (True, 1)},
        now=1.1,
    )
    assert len(arbiter.reservation_snapshot(now=1.1)) == 1
    arbiter.update_bridges(
        {"bridge-0": (True, 2), "bridge-1": (True, 1)},
        now=1.2,
    )
    assert arbiter.reservation_snapshot(now=1.2) == ()


def test_natural_expiry_prunes_reservations_drains_and_pending_starts() -> None:
    """All calendar state disappears at finite monotonic bounds."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = AirPlan(action_ms=100, stop_offset_ms=500, stop_ms=100)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="armed",
        plan=plan,
        started_at=0.0,
    )
    assert arbiter.provision(
        bridge_id="bridge-1",
        command_id="orphan",
        boot=1,
        plan=plan,
        published_at=0.0,
        expires_at=1.0,
        is_stop=False,
    )
    snapshot = arbiter.stats_snapshot(now=1.0)
    assert snapshot["active_reservations"] == 0
    assert snapshot["pending_starts"] == 0
    assert arbiter.drain_until("bridge-0", now=1.0) is None
    assert _fail_open_reasons(snapshot)["started_timeout"] == 1


def test_reservation_cap_keeps_nearest_deadlines_and_fails_open() -> None:
    """The sorted cap degrades far-future state without rejecting commands."""
    arbiter = AirArbiter()
    _online(arbiter)
    for index in range(AIR_STATE_CAP + 1):
        plan = AirPlan(
            action_ms=1,
            stop_offset_ms=100_000 + index * 1_000,
            stop_ms=1,
        )
        _start(
            arbiter,
            bridge_id=f"bridge-{index % 2}",
            command_id=f"command-{index}",
            plan=plan,
            started_at=0.0,
        )
    reservations = arbiter.reservation_snapshot(now=0.0)
    assert len(reservations) == AIR_STATE_CAP
    assert tuple(reservations) == tuple(sorted(reservations))
    assert all(reservation.command_id != f"command-{AIR_STATE_CAP}" for reservation in reservations)
    stats = arbiter.stats_snapshot(now=0.0)
    assert stats["reservation_evictions"] == 1
    assert _fail_open_reasons(stats)["reservation_cap"] == 1


def test_pending_cap_fails_open_without_growing() -> None:
    """Fast-lane callers cannot grow start-unknown state without bound."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = _plan()
    admitted = [
        arbiter.provision(
            bridge_id=f"bridge-{index % 2}",
            command_id=f"pending-{index}",
            boot=1,
            plan=plan,
            published_at=0.0,
            expires_at=32.0,
            is_stop=False,
        )
        for index in range(AIR_STATE_CAP + 1)
    ]
    assert admitted.count(True) == AIR_STATE_CAP
    assert not admitted[-1]
    assert arbiter.pending_count(now=0.0) == AIR_STATE_CAP
    stats = arbiter.stats_snapshot(now=0.0)
    assert stats["reservation_evictions"] == 1
    assert _fail_open_reasons(stats)["pending_cap"] == 1


def test_iteration_bound_is_a_deterministic_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive pass cap publishes rather than spinning."""
    arbiter = AirArbiter()
    _online(arbiter)
    owner = AirPlan(action_ms=100, stop_offset_ms=5_000, stop_ms=1_000)
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="owner",
        plan=owner,
        started_at=0.0,
    )
    monkeypatch.setattr(air_module, "MAX_FEASIBILITY_PASSES", 0)
    decision = arbiter.decide("bridge-1", _plan(), now=4.9)
    assert not decision.should_wait
    assert decision.fail_open
    assert _fail_open_reasons(arbiter.stats_snapshot(now=4.9))["iteration_bound"] == 1


def test_shadow_mode_reports_but_never_enforces_a_wait() -> None:
    """The rollback path runs the real calendar without delaying publication."""
    arbiter = AirArbiter(mode=AirMode.SHADOW)
    _online(arbiter)
    plan = _plan()
    _start(
        arbiter,
        bridge_id="bridge-0",
        command_id="owner",
        plan=plan,
        started_at=0.0,
    )
    decision = arbiter.decide("bridge-1", plan, now=0.1)
    assert decision.would_wait
    assert not decision.should_wait
    arbiter.record_shadow_wait("bridge-1", decision.earliest - 0.1)
    stats = arbiter.stats_snapshot(now=0.1)
    assert stats["would_wait"] == 1
    assert stats["would_wait_total_ms"] == 1_827


def test_enforcement_stats_surface_exact_keys_and_hold_accounting() -> None:
    """Diagnostics stay stable while exposing real enforcement delay."""
    arbiter = AirArbiter()
    _online(arbiter)
    arbiter.record_plan(plannable=True)
    arbiter.record_hold_started("bridge-1")
    arbiter.record_hold_finished("bridge-1", 1.927)
    arbiter.record_ceiling_hit()
    stats = arbiter.stats_snapshot(now=0.0)
    assert set(stats) == {
        "mode",
        "planned",
        "unplannable",
        "would_wait",
        "would_wait_total_ms",
        "would_wait_max_ms",
        "commands_held",
        "held_total_ms",
        "held_max_ms",
        "stop_bypasses",
        "stop_window_conflicts",
        "ceiling_hits",
        "fail_opens",
        "reservation_evictions",
        "disabled_single_bridge",
        "waits_by_bridge",
        "active_reservations",
        "pending_starts",
        "fail_open_reasons",
    }
    assert stats["commands_held"] == 1
    assert stats["held_total_ms"] == 1_927
    assert stats["held_max_ms"] == 1_927
    assert stats["ceiling_hits"] == 1
    reasons = _fail_open_reasons(stats)
    assert reasons["ceiling"] == 1
    assert sum(reasons.values()) == stats["fail_opens"]


def test_hard_wait_ceiling_is_the_derived_130_seconds() -> None:
    """The operational cap covers the maximum contiguous legal plan."""
    assert MAX_AIR_HOLD_MS == 130_000
    assert GUARD_MS == 100


def test_close_wakes_waiters_and_clears_all_state() -> None:
    """Final unload cannot leave a process-local calendar record behind."""
    arbiter = AirArbiter()
    _online(arbiter)
    plan = _plan(stop_after_ms=5_000)
    assert arbiter.provision(
        bridge_id="bridge-0",
        command_id="pending",
        boot=1,
        plan=plan,
        published_at=0.0,
        expires_at=32.0,
        is_stop=False,
    )
    event = arbiter.current_event
    arbiter.close()
    assert event.is_set()
    assert arbiter.pending_count(now=0.0) == 0
    assert arbiter.reservation_snapshot(now=0.0) == ()
