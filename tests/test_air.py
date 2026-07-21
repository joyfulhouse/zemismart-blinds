"""Tests for shadow-mode cross-bridge RF air arbitration."""

from __future__ import annotations

import pytest

from custom_components.zemismart_blinds.air import (
    GUARD_MS,
    AirArbiter,
    plan_for_body,
)
from custom_components.zemismart_blinds.codec import (
    encode_b0,
    estimate_b0_slot_ms,
    make_payload,
)
from tests.synthetic import TEST_BASES, TEST_PREFIX, TEST_REMOTE_ID


def _frame(button: str = "UP", channels: tuple[int, ...] = (1,)) -> str:
    """Build one production-shaped movement frame."""
    return encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, channels, button, bases=TEST_BASES))


def test_slot_matches_the_firmware_dispatch_contract() -> None:
    """The estimator must reproduce the firmware's occupancy arithmetic."""
    frame = _frame()
    # serialize + airtime + margin for a production AOK frame, independently
    # derived as 609 ms during design review.
    assert estimate_b0_slot_ms(frame) == 609
    # A frame shorter than the pacing gap still costs a whole gap.
    assert estimate_b0_slot_ms(frame, repeat_gap_ms=100_000) == 100_000


def test_slot_rejects_unparseable_frames_instead_of_guessing() -> None:
    """A bad estimate would silently mis-schedule the air, so refuse it."""
    for bad in ("", "not-hex", "AAB10101", "AAB0FF0108FFFF0855"):
        with pytest.raises(ValueError):
            estimate_b0_slot_ms(bad)


def test_plan_covers_action_trailer_and_stop_trains() -> None:
    """Every emitted frame family contributes its own repeat train."""
    action, trailer, stop = _frame("UP"), _frame("TRAILER"), _frame("STOP")
    plan = plan_for_body(
        {
            "raw": action,
            "trailer_raw": trailer,
            "stop_raw": stop,
            "repeats": 2,
            "stop_after_ms": 3_000,
        }
    )
    assert plan is not None
    assert plan.action_ms == 2 * estimate_b0_slot_ms(action) + 2 * estimate_b0_slot_ms(trailer)
    assert plan.stop_ms == 2 * estimate_b0_slot_ms(stop)
    assert plan.stop_offset_ms == 3_000
    assert plan.busy_ms(GUARD_MS) == plan.action_ms + GUARD_MS


def test_a_tuned_guard_actually_changes_the_horizon() -> None:
    """A configured guard must not be silently ignored."""
    body = {"raw": _frame(), "repeats": 2}
    tight, wide = AirArbiter(guard_ms=0), AirArbiter(guard_ms=5_000)
    for arbiter in (tight, wide):
        arbiter.observe(
            bridge_id="bridge-a", body=body, is_stop=False, online_bridges=7, now=1_000.0
        )
    train_end = 1_000.0 + (2 * estimate_b0_slot_ms(_frame())) / 1_000
    # Just past the bare train: the tight guard is clear, the wide one is not.
    probe = train_end + 0.05
    assert (
        tight.observe(bridge_id="bridge-b", body=body, is_stop=False, online_bridges=7, now=probe)
        == 0
    )
    assert (
        wide.observe(bridge_id="bridge-b", body=body, is_stop=False, online_bridges=7, now=probe)
        > 0
    )


@pytest.mark.parametrize(
    "body",
    [
        {"repeats": 2},
        {"raw": _frame(), "repeats": 0},
        {"raw": _frame(), "repeats": True},
        {"raw": _frame(), "repeats": "2"},
        {"raw": "garbage", "repeats": 2},
    ],
)
def test_unplannable_bodies_yield_none_rather_than_a_guess(body: dict[str, object]) -> None:
    """Anything unplannable must publish immediately, never be rejected."""
    assert plan_for_body(body) is None


def test_single_bridge_install_is_completely_off() -> None:
    """One bridge cannot collide with itself; behavior must be unchanged."""
    arbiter = AirArbiter()
    body = {"raw": _frame(), "repeats": 2}
    for tick in range(3):
        assert (
            arbiter.observe(
                bridge_id="bridge-a",
                body=body,
                is_stop=False,
                online_bridges=1,
                now=float(tick),
            )
            == 0
        )
    stats = arbiter.stats.as_dict()
    assert stats["disabled_single_bridge"] == 3
    assert stats["planned"] == 0
    assert stats["would_wait"] == 0


def test_second_bridge_during_a_train_would_wait() -> None:
    """The whole point: a concurrent bridge is told how long it would hold."""
    arbiter = AirArbiter()
    body = {"raw": _frame(), "repeats": 2}
    assert (
        arbiter.observe(
            bridge_id="bridge-a", body=body, is_stop=False, online_bridges=7, now=1_000.0
        )
        == 0
    )
    # 0.2 s later bridge-b publishes while A is still mid-train.
    wait = arbiter.observe(
        bridge_id="bridge-b", body=body, is_stop=False, online_bridges=7, now=1_000.2
    )
    train_ms = 2 * estimate_b0_slot_ms(_frame()) + GUARD_MS
    assert wait == pytest.approx(train_ms - 200, abs=2)

    stats = arbiter.stats.as_dict()
    assert stats["would_wait"] == 1
    assert stats["would_wait_max_ms"] == wait
    assert stats["waits_by_bridge"] == {"bridge-b": 1}


def test_a_clear_channel_never_waits() -> None:
    """Sequential commands past the horizon must report zero wait."""
    arbiter = AirArbiter()
    body = {"raw": _frame(), "repeats": 2}
    arbiter.observe(bridge_id="bridge-a", body=body, is_stop=False, online_bridges=7, now=1_000.0)
    wait = arbiter.observe(
        bridge_id="bridge-b", body=body, is_stop=False, online_bridges=7, now=1_010.0
    )
    assert wait == 0
    assert arbiter.stats.as_dict()["would_wait"] == 0


def test_stop_never_accrues_a_wait_only_a_bypass() -> None:
    """A STOP must never be held, in shadow or in enforcement."""
    arbiter = AirArbiter()
    body = {"raw": _frame(), "repeats": 2}
    arbiter.observe(bridge_id="bridge-a", body=body, is_stop=False, online_bridges=7, now=1_000.0)
    arbiter.observe(
        bridge_id="bridge-b",
        body={"raw": _frame("STOP"), "repeats": 2},
        is_stop=True,
        online_bridges=7,
        now=1_000.2,
    )
    stats = arbiter.stats.as_dict()
    assert stats["stop_bypasses"] == 1
    # Critically: a STOP is never counted as something that WOULD have waited.
    assert stats["would_wait"] == 0
    assert stats["waits_by_bridge"] == {}
