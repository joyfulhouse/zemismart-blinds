"""Tests for pure RF receive classification and state synchronization."""

from __future__ import annotations

import logging
from typing import Final

import pytest

from custom_components.zemismart_blinds import state_sync as state_sync_module
from custom_components.zemismart_blinds.codec import encode_b0, make_payload
from custom_components.zemismart_blinds.state_sync import (
    BridgeClock,
    CommandLedger,
    FrameSignature,
    HeardEvent,
    LedgerFrameSpec,
    StateSyncConsumer,
    frame_signature,
)
from tests.synthetic import TEST_BASES, TEST_PREFIX, TEST_REMOTE_ID

_BRIDGE_A: Final = "bridge-a"
_BRIDGE_B: Final = "bridge-b"
_BOOT: Final = 7
_REMOTE_KEY: Final = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
_UINT32_MAX: Final = (1 << 32) - 1
_CLAMPED_RECV_TIME: Final = 10.5
_OUTLIER_FUTURE_T: Final = 1_000_000
_CLOCK_STEP_RECV_TIME: Final = 112.0
_INTERMEDIATE_PROJECTION_RECV_TIME: Final = 40.0
_LONG_QUIET_GAP_SECONDS: Final = 30 * 24 * 60 * 60
_MILLISECONDS_PER_SECOND: Final = 1_000
_OVERSIZED_RAW_FRAME_LENGTH: Final = 5 * 1_024 * 1_024
_LEDGER_HANDOFF_TIME: Final = 10.0
_LEDGER_AFTER_WINDOW_TIME: Final = 12.0
_LEDGER_DISPLACED_TIME: Final = 30.0
_LEDGER_FLUSH_STOP_TIME: Final = 30.1
_LEDGER_STOP_OFFSET_MS: Final = 60_000
_LEDGER_ORIGINAL_STOP_TIME: Final = 70.1
_OLDER_PRESS_TIME: Final = 100.0
_COMMANDED_START_TIME: Final = 105.0
_NEWER_PRESS_TIME: Final = 106.0
_LATE_DELIVERY_TIME: Final = 110.0


def _frame(channels: tuple[int, ...], button: str) -> str:
    """Build one complete synthetic movement or trailer frame."""
    payload = make_payload(
        TEST_PREFIX,
        TEST_REMOTE_ID,
        channels,
        button,
        bases=TEST_BASES,
    )
    return encode_b0(payload)


def _required_signature(
    channels: tuple[int, ...],
    button: str,
) -> tuple[
    str,
    frozenset[int],
    str,
]:
    """Return a decoded signature, failing if synthetic setup is invalid."""
    signature = frame_signature(_frame(channels, button))
    assert signature is not None
    return signature


def test_frame_signature_decodes_single_movement() -> None:
    """A synthetic single-channel UP frame has the configured identity."""
    assert frame_signature(_frame((1,), "UP")) == (
        _REMOTE_KEY,
        frozenset({1}),
        "UP",
    )


def test_frame_signature_decodes_group_channels() -> None:
    """A group frame retains every addressed channel in its signature."""
    assert frame_signature(_frame((1, 2), "DOWN")) == (
        _REMOTE_KEY,
        frozenset({1, 2}),
        "DOWN",
    )


@pytest.mark.parametrize("frame", [_frame((1,), "TRAILER"), "not hex"])
def test_frame_signature_ignores_non_movement_and_garbage(frame: str) -> None:
    """Non-movement commands and malformed captures are not signatures."""
    assert frame_signature(frame) is None


def test_frame_signature_decodes_rekeyed_field_truncated_trailer_capture() -> None:
    """A re-keyed OEM field capture with a truncated trailer still classifies.

    The bucket timings and 65-pair single-0-read trailer structure came from
    a real field capture; its payload is re-keyed to the synthetic test
    identity and is no longer verbatim. This structure was silently dropped
    here while receive decoding was strict.
    """
    rekeyed_up = (
        "AAB10413EC026C012C143C381A192A192929292A1A192A1A19292A192A1A192929292A1A192A"
        "192929292A192A1A1929292929292A1A1A1A1A1A1A1A1A1A1A1A192A192929292A192A192A"
        "1A1955"
    )
    assert frame_signature(rekeyed_up) == (
        "a1b2c3:42",
        frozenset({1, 2, 3, 4, 5, 6}),
        "UP",
    )


def test_bridge_clock_tracks_steady_samples() -> None:
    """Steady samples preserve the bridge-to-HA time offset."""
    clock = BridgeClock()
    clock.observe(_BOOT, 1_000, 10.0)
    clock.observe(_BOOT, 2_000, 11.0)

    assert clock.to_ha_time(_BOOT, 2_500, 11.5) == pytest.approx(11.5)


def test_bridge_clock_reseeds_on_boot_change() -> None:
    """A new firmware boot discards the previous clock offset."""
    clock = BridgeClock()
    clock.observe(_BOOT, 1_000, 10.0)
    clock.observe(_BOOT + 1, 500, 50.0)

    assert clock.to_ha_time(_BOOT + 1, 750, 50.25) == pytest.approx(50.25)


def test_bridge_clock_rejects_stale_sample() -> None:
    """An out-of-order sample cannot rewind an established offset."""
    clock = BridgeClock()
    clock.observe(_BOOT, 100_000, 100.0)
    clock.observe(_BOOT, 101_000, 101.0)
    clock.observe(_BOOT, 100_500, 120.0)

    assert clock.to_ha_time(_BOOT, 101_500, 120.0) == pytest.approx(101.5)


def test_bridge_clock_handles_uint32_wrap() -> None:
    """Forward serial time remains ordered across uint32 wraparound."""
    clock = BridgeClock()
    clock.observe(_BOOT, _UINT32_MAX - 499, 100.0)
    clock.observe(_BOOT, 500, 101.0)

    assert clock.to_ha_time(_BOOT, 1_000, 101.5) == pytest.approx(101.5)


def test_bridge_clock_clamps_future_projection() -> None:
    """Projected capture time never exceeds local receipt time."""
    clock = BridgeClock()
    clock.observe(_BOOT, 1_000, 10.0)

    assert clock.to_ha_time(_BOOT, 2_000, _CLAMPED_RECV_TIME) == _CLAMPED_RECV_TIME


def test_bridge_clock_rejects_single_outlier_without_shifting_projection() -> None:
    """One forged future timestamp cannot replace an established offset."""
    clock = BridgeClock()
    clock.observe(_BOOT, 1_000, 10.0)
    clock.observe(_BOOT, 2_000, 11.0)

    clock.observe(_BOOT, _OUTLIER_FUTURE_T, 12.0)

    assert clock.to_ha_time(_BOOT, 3_000, 12.0) == pytest.approx(12.0)


def test_bridge_clock_reseeds_after_two_consistent_outliers() -> None:
    """A confirmed wall-clock step replaces the stale correlation offset."""
    clock = BridgeClock()
    clock.observe(_BOOT, 1_000, 10.0)
    clock.observe(_BOOT, 2_000, 11.0)

    clock.observe(_BOOT, 3_000, _CLOCK_STEP_RECV_TIME)
    assert clock.to_ha_time(
        _BOOT,
        3_000,
        _INTERMEDIATE_PROJECTION_RECV_TIME,
    ) == pytest.approx(12.0)

    clock.observe(_BOOT, 4_000, _CLOCK_STEP_RECV_TIME + 1.0)

    assert clock.to_ha_time(
        _BOOT,
        4_500,
        _CLOCK_STEP_RECV_TIME + 1.5,
    ) == pytest.approx(_CLOCK_STEP_RECV_TIME + 1.5)


def test_ledger_pending_then_confirmed_matches_full_envelope() -> None:
    """Action and delayed STOP frames transition from pending to confirmed."""
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    stop = _required_signature((1,), "STOP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [
            LedgerFrameSpec(action, offset_ms=0, airtime_ms=500),
            LedgerFrameSpec(stop, offset_ms=2_000, airtime_ms=500),
        ],
    )

    assert ledger.match(action, -1_000.0) == ("pending", "command-1", _BRIDGE_A)

    ledger.confirm("command-1", 10.0)

    assert ledger.match(action, 10.25) == ("confirmed", "command-1", _BRIDGE_A)
    assert ledger.match(stop, 12.25) == ("confirmed", "command-1", _BRIDGE_A)
    assert ledger.match(action, 100.0) is None


def test_ledger_displace_rewindows_only_confirmed_stop_frames() -> None:
    """Displacement recognizes flushed STOPs without hiding the old deadline."""
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    stop = _required_signature((1,), "STOP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [
            LedgerFrameSpec(action, offset_ms=0, airtime_ms=500),
            LedgerFrameSpec(
                stop,
                offset_ms=_LEDGER_STOP_OFFSET_MS,
                airtime_ms=500,
            ),
        ],
    )
    ledger.confirm("command-1", _LEDGER_HANDOFF_TIME)

    assert ledger.displace("command-1", _LEDGER_DISPLACED_TIME)

    assert ledger.match(action, _LEDGER_HANDOFF_TIME) == (
        "confirmed",
        "command-1",
        _BRIDGE_A,
    )
    assert ledger.match(stop, _LEDGER_FLUSH_STOP_TIME) == (
        "confirmed",
        "command-1",
        _BRIDGE_A,
    )
    assert ledger.match(stop, _LEDGER_ORIGINAL_STOP_TIME) is None


def test_ledger_release_preserves_a_displaced_stop_drain() -> None:
    """A disarm ack cannot retire a displaced command's flushed STOP window."""
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    stop = _required_signature((1,), "STOP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [
            LedgerFrameSpec(action, offset_ms=0, airtime_ms=500),
            LedgerFrameSpec(
                stop,
                offset_ms=_LEDGER_STOP_OFFSET_MS,
                airtime_ms=500,
            ),
        ],
    )
    ledger.confirm("command-1", _LEDGER_HANDOFF_TIME)
    ledger.displace("command-1", _LEDGER_DISPLACED_TIME)

    ledger.release("command-1")

    assert ledger.match(stop, _LEDGER_FLUSH_STOP_TIME) == (
        "confirmed",
        "command-1",
        _BRIDGE_A,
    )


def test_ledger_displace_retires_a_pending_command() -> None:
    """A never-started displaced command cannot leave a pending echo hold."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "UP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "UP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )

    assert not ledger.displace("command-1", _LEDGER_DISPLACED_TIME)

    assert ledger.match(signature, _LEDGER_DISPLACED_TIME) is None
    assert not ledger.displace("missing", _LEDGER_DISPLACED_TIME)


def test_ledger_retire_and_gc_remove_entries() -> None:
    """Explicit retirement and TTL collection remove complete commands."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "UP")
    frame = LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)

    ledger.register_pending("retired", _BRIDGE_A, (1,), "UP", [frame])
    ledger.retire("retired")
    assert ledger.match(signature, 10.0) is None

    ledger.register_pending("expired", _BRIDGE_A, (1,), "UP", [frame])
    ledger.confirm("expired", 10.0)
    ledger.gc(1_000.0)
    assert ledger.match(signature, 10.25) is None


def test_ledger_finds_only_live_overlapping_commands() -> None:
    """Takeover targets pending and non-displaced confirmed overlaps."""
    ledger = CommandLedger()
    first = _required_signature((1,), "DOWN")
    second = _required_signature((2,), "DOWN")
    foreign = ("123456:0d", frozenset({1}), "DOWN")
    ledger.register_pending(
        "matching",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [LedgerFrameSpec(first, offset_ms=0, airtime_ms=500)],
    )
    ledger.register_pending(
        "disjoint",
        _BRIDGE_A,
        (2,),
        "DOWN",
        [LedgerFrameSpec(second, offset_ms=0, airtime_ms=500)],
    )
    ledger.register_pending(
        "foreign",
        _BRIDGE_B,
        (1,),
        "DOWN",
        [LedgerFrameSpec(foreign, offset_ms=0, airtime_ms=500)],
    )
    ledger.register_pending(
        "started",
        _BRIDGE_B,
        (1,),
        "DOWN",
        [LedgerFrameSpec(first, offset_ms=0, airtime_ms=500)],
    )
    ledger.confirm("started", _LEDGER_HANDOFF_TIME)
    ledger.register_pending(
        "displaced",
        _BRIDGE_B,
        (1,),
        "DOWN",
        [LedgerFrameSpec(first, offset_ms=0, airtime_ms=500)],
    )
    ledger.confirm("displaced", _LEDGER_HANDOFF_TIME)
    assert not ledger.displace("displaced", _LEDGER_DISPLACED_TIME)

    assert ledger.live_overlapping(
        _REMOTE_KEY,
        frozenset({1}),
        _LEDGER_HANDOFF_TIME,
    ) == (
        state_sync_module.LiveCommand(
            bridge_id=_BRIDGE_A,
            command_id="matching",
            channels=frozenset({1}),
            button="DOWN",
            confirmed=False,
        ),
        state_sync_module.LiveCommand(
            bridge_id=_BRIDGE_B,
            command_id="started",
            channels=frozenset({1}),
            button="DOWN",
            confirmed=True,
        ),
    )


def test_ledger_excludes_completed_confirmed_overlap_during_echo_tail() -> None:
    """A confirmed command stops being takeover-live when every window ends."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "UP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "UP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    ledger.confirm("command-1", _LEDGER_HANDOFF_TIME)
    live_command = state_sync_module.LiveCommand(
        bridge_id=_BRIDGE_A,
        command_id="command-1",
        channels=frozenset({1}),
        button="UP",
        confirmed=True,
    )

    assert ledger.live_overlapping(
        _REMOTE_KEY,
        frozenset({1}),
        _LEDGER_HANDOFF_TIME,
    ) == (live_command,)
    assert (
        ledger.live_overlapping(
            _REMOTE_KEY,
            frozenset({1}),
            _LEDGER_AFTER_WINDOW_TIME,
        )
        == ()
    )


def test_ledger_reports_command_liveness_for_cover_owned_takeover() -> None:
    """Command-scoped takeover stays conservative except after RF completion."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "UP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "UP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )

    assert ledger.command_live_for_takeover("missing", _LEDGER_AFTER_WINDOW_TIME)
    assert ledger.command_live_for_takeover("command-1", _LEDGER_AFTER_WINDOW_TIME)

    ledger.confirm("command-1", _LEDGER_HANDOFF_TIME)

    assert ledger.command_live_for_takeover("command-1", _LEDGER_HANDOFF_TIME)
    assert not ledger.command_live_for_takeover("command-1", _LEDGER_AFTER_WINDOW_TIME)


def test_ledger_enforces_per_bridge_and_global_caps() -> None:
    """Old commands are evicted under both per-bridge and global pressure."""
    signature = _required_signature((1,), "UP")
    frame = LedgerFrameSpec(signature, offset_ms=0, airtime_ms=100)
    ledger = CommandLedger()

    for index in range(300):
        command_id = f"same-bridge-{index}"
        ledger.register_pending(command_id, _BRIDGE_A, (1,), "UP", [frame])
        ledger.confirm(command_id, float(index * 10))

    assert ledger.match(signature, 0.05) is None
    assert ledger.match(signature, 2_990.05) == (
        "confirmed",
        "same-bridge-299",
        _BRIDGE_A,
    )

    ledger = CommandLedger()
    for index in range(300):
        command_id = f"global-{index}"
        ledger.register_pending(command_id, f"bridge-{index}", (1,), "UP", [frame])
        ledger.confirm(command_id, float(index * 10))

    assert ledger.match(signature, 0.05) is None
    assert ledger.match(signature, 2_990.05) == (
        "confirmed",
        "global-299",
        "bridge-299",
    )


def _consumer(
    ledger: CommandLedger,
    dispatched: list[HeardEvent],
    proofs: list[str],
    now_value: list[float],
    bridge_clocks: dict[str, BridgeClock] | None = None,
) -> StateSyncConsumer:
    """Build a deterministic consumer around mutable observation lists."""
    clocks = {} if bridge_clocks is None else bridge_clocks
    return StateSyncConsumer(
        ledger=ledger,
        clock_resolver=lambda bridge_id: clocks.setdefault(bridge_id, BridgeClock()),
        dispatch=dispatched.append,
        on_emission_proof=proofs.append,
        now=lambda: now_value[0],
    )


def test_consumer_dispatches_fresh_press() -> None:
    """An unmatched movement capture dispatches one fully timed event."""
    dispatched: list[HeardEvent] = []
    consumer = _consumer(CommandLedger(), dispatched, [], [10.0])

    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, _frame((1,), "UP"), 10.0)

    assert dispatched == [
        HeardEvent(
            button="UP",
            chans=frozenset({1}),
            remote_key=_REMOTE_KEY,
            heard_at=10.0,
            bridge_id=_BRIDGE_A,
        ),
    ]


def test_consumer_resolves_independent_clock_per_bridge() -> None:
    """Alternating bridge boots retain separate time correlations."""
    now_value = [100.0]
    bridge_clocks: dict[str, BridgeClock] = {}
    consumer = _consumer(CommandLedger(), [], [], now_value, bridge_clocks)

    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, _frame((1,), "UP"), 100.0)
    now_value[0] = 100.1
    consumer.handle_rx(_BRIDGE_B, _BOOT + 1, 9_000, _frame((1,), "DOWN"), 100.1)
    now_value[0] = 101.0
    consumer.handle_rx(_BRIDGE_A, _BOOT, 2_000, _frame((1,), "STOP"), 101.0)
    now_value[0] = 101.1
    consumer.handle_rx(_BRIDGE_B, _BOOT + 1, 10_000, _frame((2,), "UP"), 101.1)

    assert bridge_clocks[_BRIDGE_A].to_ha_time(_BOOT, 2_500, 101.5) == pytest.approx(101.5)
    assert bridge_clocks[_BRIDGE_B].to_ha_time(
        _BOOT + 1,
        10_500,
        101.6,
    ) == pytest.approx(101.6)


def test_consumer_exact_event_deduplicates_normalized_frame() -> None:
    """A QoS duplicate is dropped despite harmless frame formatting changes."""
    dispatched: list[HeardEvent] = []
    consumer = _consumer(CommandLedger(), dispatched, [], [10.0])
    frame = _frame((1,), "UP")

    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, frame, 10.0)
    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, frame.lower(), 10.1)

    assert len(dispatched) == 1


def test_consumer_debounces_different_repeat_timestamps() -> None:
    """Distinct bridge timestamps in one RF burst still dispatch once."""
    dispatched: list[HeardEvent] = []
    now_value = [10.0]
    consumer = _consumer(CommandLedger(), dispatched, [], now_value)
    frame = _frame((1,), "DOWN")

    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, frame, 10.0)
    now_value[0] = 10.1
    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_100, frame, 10.1)

    assert len(dispatched) == 1


def test_consumer_suppresses_confirmed_peer_echo_and_records_proof() -> None:
    """A peer-heard confirmed command is proof, never a mirrored press."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "UP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "UP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    ledger.confirm("command-1", 10.0)
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    consumer = _consumer(ledger, dispatched, proofs, [10.0])

    consumer.handle_rx(_BRIDGE_B, _BOOT, 1_000, _frame((1,), "UP"), 10.0)

    assert dispatched == []
    assert proofs == ["command-1"]


def test_consumer_projects_delayed_echo_before_observing_sample() -> None:
    """Delivery delay cannot move an echo outside its confirmed window."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "UP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "UP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    ledger.confirm("command-1", 101.0)
    clock = BridgeClock()
    clock.observe(_BOOT, 1_000, 100.0)
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    consumer = StateSyncConsumer(
        ledger=ledger,
        clock_resolver=lambda _bridge_id: clock,
        dispatch=dispatched.append,
        on_emission_proof=proofs.append,
        now=lambda: 116.0,
    )

    consumer.handle_rx(_BRIDGE_B, _BOOT, 2_000, _frame((1,), "UP"), 116.0)

    assert dispatched == []
    assert proofs == ["command-1"]


def test_consumer_holds_pending_echo_until_confirmation() -> None:
    """A pre-start peer capture is reclassified after its command confirms."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "DOWN")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    consumer = _consumer(ledger, dispatched, proofs, [10.0])

    consumer.handle_rx(_BRIDGE_B, _BOOT, 1_000, _frame((1,), "DOWN"), 10.0)
    assert dispatched == []

    ledger.confirm("command-1", 10.0)
    consumer.resume_holds("command-1")

    assert dispatched == []
    assert proofs == ["command-1"]


def test_consumer_does_not_age_new_pending_command_from_old_gc() -> None:
    """A command registered after an idle period starts a fresh pending TTL."""
    ledger = CommandLedger()
    dispatched: list[HeardEvent] = []
    now_value = [0.0]
    consumer = _consumer(ledger, dispatched, [], now_value)
    signature = _required_signature((1,), "UP")

    now_value[0] = 100.0
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "UP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    consumer.handle_rx(_BRIDGE_B, _BOOT, 1_000, _frame((1,), "UP"), 100.0)

    assert dispatched == []


def test_consumer_reclassifies_delayed_hold_before_gc() -> None:
    """A delayed confirmation still suppresses the capture it confirms."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "DOWN")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [10.0]
    consumer = _consumer(ledger, dispatched, proofs, now_value)
    consumer.handle_rx(_BRIDGE_B, _BOOT, 1_000, _frame((1,), "DOWN"), 10.0)

    ledger.confirm("command-1", 10.0)
    now_value[0] = 100.0
    consumer.resume_holds("command-1")

    assert dispatched == []
    assert proofs == ["command-1"]


def test_consumer_resumes_retired_hold_as_press() -> None:
    """A held capture becomes a physical press when its command retires."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "STOP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "STOP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    dispatched: list[HeardEvent] = []
    consumer = _consumer(ledger, dispatched, [], [10.0])
    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, _frame((1,), "STOP"), 10.0)

    ledger.retire("command-1")
    consumer.resume_holds("command-1")

    assert [event.button for event in dispatched] == ["STOP"]


def test_consumer_drops_resumed_hold_older_than_overlapping_press() -> None:
    """Releasing a hold cannot overwrite a newer overlapping press."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "UP")
    ledger.register_pending(
        "command-1",
        _BRIDGE_A,
        (1,),
        "UP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    dispatched: list[HeardEvent] = []
    now_value = [10.0]
    consumer = _consumer(ledger, dispatched, [], now_value)
    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, _frame((1,), "UP"), 10.0)

    now_value[0] = 11.0
    consumer.handle_rx(_BRIDGE_A, _BOOT, 2_000, _frame((1, 2), "DOWN"), 11.0)
    ledger.retire("command-1")
    consumer.resume_holds("command-1")

    assert [event.button for event in dispatched] == ["DOWN"]


def test_consumer_drops_press_older_than_overlapping_commanded_start() -> None:
    """A late older physical capture cannot replace a newer commanded start.

    Driven through ``handle_rx`` rather than ``_dispatch_press`` so the guard
    is measured on the path production actually takes: both captures are
    delivered at _LATE_DELIVERY_TIME, after our own RF had already started.
    """
    dispatched: list[HeardEvent] = []
    consumer = _consumer(CommandLedger(), dispatched, [], [_LATE_DELIVERY_TIME])
    consumer.record_commanded_start(
        _REMOTE_KEY,
        frozenset({1}),
        _COMMANDED_START_TIME,
    )

    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, _frame((1,), "UP"), _OLDER_PRESS_TIME)
    consumer.handle_rx(_BRIDGE_A, _BOOT, 7_000, _frame((1,), "UP"), _NEWER_PRESS_TIME)

    assert [event.heard_at for event in dispatched] == [_NEWER_PRESS_TIME]


def test_consumer_projects_fresh_press_at_receipt_after_long_quiet_gap() -> None:
    """A serially ambiguous quiet gap cannot create an ancient press time."""
    clock = BridgeClock()
    clock.observe(_BOOT, 1_000, 100.0)
    recv_time = 100.0 + _LONG_QUIET_GAP_SECONDS
    raw_t = (1_000 + _LONG_QUIET_GAP_SECONDS * _MILLISECONDS_PER_SECOND) & _UINT32_MAX
    dispatched: list[HeardEvent] = []
    consumer = StateSyncConsumer(
        ledger=CommandLedger(),
        clock_resolver=lambda _bridge_id: clock,
        dispatch=dispatched.append,
        on_emission_proof=lambda _command_id: None,
        now=lambda: recv_time,
    )

    consumer.handle_rx(_BRIDGE_A, _BOOT, raw_t, _frame((1,), "UP"), recv_time)

    assert dispatched[0].heard_at == pytest.approx(recv_time)


def test_normalize_frame_rejects_oversized_raw_input() -> None:
    """The raw-size guard rejects huge input before whitespace normalization."""
    frame = " " * _OVERSIZED_RAW_FRAME_LENGTH

    assert len(frame) > state_sync_module._MAX_RAW_FRAME_LENGTH
    assert StateSyncConsumer._normalize_frame(frame) is None


def test_consumer_close_clears_state_and_stops_dispatch() -> None:
    """Closing is idempotent and prevents later capture delivery."""
    dispatched: list[HeardEvent] = []
    consumer = _consumer(CommandLedger(), dispatched, [], [10.0])
    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, _frame((1,), "UP"), 10.0)
    consumer.record_commanded_start(_REMOTE_KEY, frozenset({1}), 10.0)

    consumer.close()
    consumer.close()
    consumer.handle_rx(_BRIDGE_A, _BOOT, 2_000, _frame((1,), "DOWN"), 11.0)

    assert len(dispatched) == 1
    assert consumer._commanded_starts == {}


# Production capture 2026-07-25 07:45:39: a bridge put DOWN on air at 39.248
# and its own "started" status did not reach HA until 40.365 — 1.117 s later,
# against 0.75 s of window slack. Reproduced here with the same shape.
_LATE_START_HEARD_TIME: Final = 100.0
_LATE_START_CONFIRM_TIME: Final = 101.2


def test_held_capture_survives_a_started_status_later_than_its_own_rf() -> None:
    """A capture held while pending stays ours when 'started' lands late.

    The confirmed window's lower bound is derived from the START STATUS, which
    is published separately from the RF it describes and can arrive AFTER a
    peer bridge already reported hearing the frame; a concurrent multi-remote
    burst measured 1.117 s of that skew against 0.75 s of slack.

    SCOPE, measured rather than assumed: the ``dispatched == []`` assertion
    below ALSO passes without this fix, because ``_dispatch_press`` already
    drops a press predating a recorded commanded start, and the hub always
    records one before resolving the future that unblocks ``confirm()``. What
    this fix actually changes is the ``proofs`` assertion -- the window miss
    cost us the EMISSION PROOF that the command reached the air, and losing
    that clears the unverified anchor. Do not read this test as evidence that
    a phantom press was prevented.
    """
    ledger = CommandLedger()
    signature = _required_signature((1,), "DOWN")
    ledger.register_pending(
        "command-late",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=3_000)],
    )
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [_LATE_START_HEARD_TIME]
    consumer = _consumer(ledger, dispatched, proofs, now_value)

    # A peer bridge hears our frame BEFORE the transmitting bridge's status
    # reaches HA. The entry is still pending, so the capture is held.
    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        1_000,
        _frame((1,), "DOWN"),
        _LATE_START_HEARD_TIME,
    )
    assert dispatched == []

    # Production ALWAYS records a commanded start before resume_holds: the hub
    # calls record_commanded_start immediately before resolving the `started`
    # future that unblocks confirm(). Omitting it measures a path the running
    # integration never takes.
    now_value[0] = _LATE_START_CONFIRM_TIME
    consumer.record_commanded_start(_REMOTE_KEY, frozenset({1}), _LATE_START_CONFIRM_TIME)
    ledger.confirm("command-late", _LATE_START_CONFIRM_TIME)
    consumer.resume_holds("command-late")

    # Our own transmission must never surface as a physical remote press.
    assert dispatched == []
    # Heard on a different bridge than the one that sent it: still proof the
    # command actually reached the air.
    assert proofs == ["command-late"]


_BURST_COMMAND_COUNT: Final = 7
_BURST_RF_SPACING_SECONDS: Final = 0.25
_BURST_STATUS_LAG_SECONDS: Final = 1.2


def test_concurrent_burst_across_bridges_dispatches_no_phantom_press() -> None:
    """Seven overlapping commands with lagging start statuses stay ours.

    This is the Night sweep's real workload: one parallel burst across several
    covers, each transmitted by a different bridge, every frame overheard by
    peer bridges, and every "started" status delayed behind the RF it
    describes.

    As above, the discriminating assertion is ``proofs``, not ``dispatched``:
    ``_commanded_starts`` already suppresses the press. This pins that seven
    concurrent commands each retain proof they reached the air.
    """
    ledger = CommandLedger()
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [_LATE_START_HEARD_TIME]
    consumer = _consumer(ledger, dispatched, proofs, now_value)

    commands = []
    for index in range(_BURST_COMMAND_COUNT):
        channels = (index + 1,)
        command_id = f"burst-{index}"
        sender = f"bridge-{index}"
        heard_at = _LATE_START_HEARD_TIME + index * _BURST_RF_SPACING_SECONDS
        ledger.register_pending(
            command_id,
            sender,
            channels,
            "DOWN",
            [
                LedgerFrameSpec(
                    _required_signature(channels, "DOWN"),
                    offset_ms=0,
                    airtime_ms=3_000,
                ),
            ],
        )
        commands.append((command_id, channels, sender, heard_at))

    # Every frame goes on air and is overheard by a PEER bridge first.
    for _command_id, channels, sender, heard_at in commands:
        now_value[0] = heard_at
        listener = f"listener-{sender}"
        consumer.handle_rx(
            listener,
            _BOOT,
            int(heard_at * _MILLISECONDS_PER_SECOND),
            _frame(channels, "DOWN"),
            heard_at,
        )
    # Nothing may dispatch while the commands are still unconfirmed.
    assert dispatched == []

    # Each bridge's "started" status arrives only after its own RF was heard.
    for command_id, channels, _sender, heard_at in commands:
        confirmed_at = heard_at + _BURST_STATUS_LAG_SECONDS
        now_value[0] = confirmed_at
        consumer.record_commanded_start(_REMOTE_KEY, frozenset(channels), confirmed_at)
        ledger.confirm(command_id, confirmed_at)
        consumer.resume_holds(command_id)

    # Not one of the seven may surface as a physical remote press.
    assert dispatched == []
    assert sorted(proofs) == sorted(command_id for command_id, *_ in commands)


_GENUINE_PRESS_REGISTER_TIME: Final = 100.0
_GENUINE_PRESS_HEARD_TIME: Final = 105.0
_GENUINE_PRESS_LATE_CONFIRM_TIME: Final = 130.0


def test_held_capture_far_before_its_handoff_is_still_a_real_press() -> None:
    """A signature-colliding press outside the status-lag envelope dispatches.

    A timed move registers its own ``stop_raw``, so a person pressing STOP on
    the physical remote produces a capture IDENTICAL to a frame we have
    registered, and it is held like any other. Trusting every held capture
    unconditionally would silently absorb that press — bypassing the takeover
    machinery entirely — for as long as the bridge took to confirm, up to the
    30 s started-status timeout. Ownership is therefore bounded to the plausible
    status lag; a press heard 25 s before the eventual handoff is a person.
    """
    ledger = CommandLedger()
    signature = _required_signature((1,), "STOP")
    ledger.register_pending(
        "command-timed",
        _BRIDGE_A,
        (1,),
        "STOP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=3_000)],
    )
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [_GENUINE_PRESS_HEARD_TIME]
    consumer = _consumer(ledger, dispatched, proofs, now_value)

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        1_000,
        _frame((1,), "STOP"),
        _GENUINE_PRESS_HEARD_TIME,
    )
    assert dispatched == []  # held while pending, as before

    # The bridge only confirms 25 s later: far outside any plausible lag.
    now_value[0] = _GENUINE_PRESS_LATE_CONFIRM_TIME
    ledger.confirm("command-timed", _GENUINE_PRESS_LATE_CONFIRM_TIME)
    consumer.resume_holds("command-timed")

    assert [event.button for event in dispatched] == ["STOP"]
    assert proofs == []


_STALE_PRESS_HEARD_TIME: Final = 104.0
_STALE_PRESS_DELIVERY_TIME: Final = 110.0


def test_held_press_far_before_a_commanded_start_still_dispatches() -> None:
    """The commanded-start guard must not swallow a 25 s older genuine press.

    The test above deliberately omits ``record_commanded_start``; production
    never does. The hub records the stamp immediately before resolving the
    ``started`` future that unblocks ``confirm()``, so the very same person
    pressing STOP 25 s before the bridge confirmed hits BOTH guards, and the
    ordering guard used to be unbounded below: any same-remote overlapping
    press heard before the stamp was dropped, invisibly, for the stamp's whole
    retention. That is where the integration went deaf to a real person.
    """
    ledger = CommandLedger()
    signature = _required_signature((1,), "STOP")
    ledger.register_pending(
        "command-timed",
        _BRIDGE_A,
        (1,),
        "STOP",
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=3_000)],
    )
    dispatched: list[HeardEvent] = []
    now_value = [_GENUINE_PRESS_HEARD_TIME]
    consumer = _consumer(ledger, dispatched, [], now_value)

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        1_000,
        _frame((1,), "STOP"),
        _GENUINE_PRESS_HEARD_TIME,
    )
    assert dispatched == []  # held while pending

    now_value[0] = _GENUINE_PRESS_LATE_CONFIRM_TIME
    consumer.record_commanded_start(
        _REMOTE_KEY,
        frozenset({1}),
        _GENUINE_PRESS_LATE_CONFIRM_TIME,
    )
    ledger.confirm("command-timed", _GENUINE_PRESS_LATE_CONFIRM_TIME)
    consumer.resume_holds("command-timed")

    assert [event.button for event in dispatched] == ["STOP"]


def test_suppressed_stale_press_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Stale delivery is still dropped, and no longer invisibly.

    Narrowing the guard must not re-admit what it exists for: a capture heard
    before our own RF started and delivered only afterwards is news our command
    already superseded, and it stays dropped. It now says so — the guard's own
    calibration was otherwise unobservable in the field, which is how it went a
    whole release absorbing real presses without leaving a trace.
    """
    dispatched: list[HeardEvent] = []
    consumer = _consumer(CommandLedger(), dispatched, [], [_STALE_PRESS_DELIVERY_TIME])
    consumer.record_commanded_start(_REMOTE_KEY, frozenset({1}), _COMMANDED_START_TIME)

    with caplog.at_level(logging.DEBUG, logger=state_sync_module._LOGGER.name):
        consumer.handle_rx(
            _BRIDGE_A,
            _BOOT,
            1_000,
            _frame((1,), "UP"),
            _STALE_PRESS_HEARD_TIME,
        )

    assert dispatched == []
    assert "stale delivery" in caplog.text


_LATE_ANCHOR_STOP_OFFSET_MS: Final = 15_000
_LATE_ANCHOR_HANDOFF: Final = 200.0
_LATE_ANCHOR_SKEW_SECONDS: Final = 1.2


def test_stop_echo_arriving_before_its_late_anchored_window_stays_ours() -> None:
    """A stop_raw echo is ours even when the whole entry was anchored late.

    A stop_raw frame fires stop_after_ms AFTER the action frame, so by the time
    its own echo is heard the command has long since confirmed — it never goes
    through the held-capture path, only through match(). The anchor that built
    every window in the entry is `recv_time - age_ms/1000`, which cannot correct
    the MQTT transport leg and so runs late; the STOP's window inherits that
    same shift. Without lower-edge tolerance our own STOP echo lands before its
    own window and is classified as somebody stopping the blind by hand, which
    freezes the travel model mid-flight while the motor runs on to its limit.
    """
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    stop = _required_signature((1,), "STOP")
    ledger.register_pending(
        "timed-move",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [
            LedgerFrameSpec(action, offset_ms=0, airtime_ms=3_000),
            LedgerFrameSpec(stop, offset_ms=_LATE_ANCHOR_STOP_OFFSET_MS, airtime_ms=3_000),
        ],
    )
    # The status lagged, so confirm() anchors every window late by that skew.
    ledger.confirm("timed-move", _LATE_ANCHOR_HANDOFF + _LATE_ANCHOR_SKEW_SECONDS)

    # Our own STOP goes on air at its TRUE scheduled time, before the window
    # the late anchor implies.
    true_stop_time = _LATE_ANCHOR_HANDOFF + _LATE_ANCHOR_STOP_OFFSET_MS / 1_000
    assert ledger.match(stop, true_stop_time) == ("confirmed", "timed-move", _BRIDGE_A)

    # Tolerance is lower-edge only: long after the train ends it is a real press.
    assert ledger.match(stop, true_stop_time + 60.0) is None


def test_own_stop_echo_is_not_dispatched_as_a_press_through_the_consumer() -> None:
    """End-to-end: our own STOP echo must not reach dispatch as a press.

    This is the assertion that actually protects the reported bug, and it is
    deliberately at CONSUMER level rather than calling ``ledger.match``
    directly. ``_dispatch_press``'s commanded-start guard cannot help here: it
    only drops presses heard BEFORE a commanded start, and a stop_raw echo is
    heard ``stop_after_ms`` AFTER it. So unlike the held-capture path -- where
    that guard already suppressed the press and this change only restores the
    emission proof -- here the window is the sole line of defence, and a late
    anchor shifting it past our own echo dispatches a phantom STOP that freezes
    the travel model mid-flight while the motor runs on to its limit.
    """
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    stop = _required_signature((1,), "STOP")
    ledger.register_pending(
        "timed",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [
            LedgerFrameSpec(action, offset_ms=0, airtime_ms=3_000),
            LedgerFrameSpec(stop, offset_ms=_LATE_ANCHOR_STOP_OFFSET_MS, airtime_ms=3_000),
        ],
    )
    dispatched: list[HeardEvent] = []
    anchored_at = _LATE_ANCHOR_HANDOFF + _LATE_ANCHOR_SKEW_SECONDS
    now_value = [anchored_at]
    consumer = _consumer(ledger, dispatched, [], now_value)
    consumer.record_commanded_start(_REMOTE_KEY, frozenset({1}), anchored_at)
    ledger.confirm("timed", anchored_at)

    true_stop_time = _LATE_ANCHOR_HANDOFF + _LATE_ANCHOR_STOP_OFFSET_MS / 1_000
    now_value[0] = true_stop_time
    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(true_stop_time * _MILLISECONDS_PER_SECOND),
        _frame((1,), "STOP"),
        true_stop_time,
    )

    assert dispatched == []


# The bridge holds a timed move's stop_raw armed from handoff until its
# deadline. A newer overlapping command displaces the old one and the armed
# STOP is flushed immediately -- long before the deadline the ledger windowed
# it at. Both the flushed frame's /rx and the transmitting bridge's
# "displaced" status then race over MQTT, and the status has no age_ms to
# correct its own transport leg.
_FLUSH_HANDOFF: Final = 300.0
_FLUSH_STOP_OFFSET_MS: Final = 60_000
# The displacer is admitted at 305.0 and stays armed until 425.0 -- its own
# deadline is two minutes out, far longer than the flush it caused.
_DISPLACER_HANDOFF: Final = 305.0
_DISPLACER_STOP_OFFSET_MS: Final = 120_000
_FLUSH_HEARD_TIME: Final = 305.0
_FLUSH_DISPLACED_TIME: Final = 306.2
# 15 s past the displacer's admission, still deep inside its armed span.
_LATE_HUMAN_STOP_TIME: Final = 320.0
_DEFAULT_TRAIN_AIRTIME_MS: Final = 3_000
# A remote at repeats=15 -- config_flow allows up to MAX_REPEATS = 20 -- makes
# _ledger_airtime_ms return max(2000, 15*1000). The whole owed STOP train has
# to drain before the displacer's own action frame can be reported started, so
# the flush occupies the fifteen seconds BELOW that admission.
_HIGH_REPEAT_TRAIN_AIRTIME_MS: Final = 15_000
_HIGH_REPEAT_ADMISSION: Final = 315.0
_HIGH_REPEAT_FIRST_FLUSH_TIME: Final = 300.5


def _timed_move_ledger(
    airtime_ms: int = _DEFAULT_TRAIN_AIRTIME_MS,
) -> tuple[CommandLedger, FrameSignature]:
    """Build a ledger holding one confirmed timed move with a queued STOP.

    ``airtime_ms`` is the frame's full repeat train, exactly as
    ``_ledger_airtime_ms`` computes it from the remote's configured repeats.
    """
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    stop = _required_signature((1,), "STOP")
    ledger.register_pending(
        "timed-move",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [
            LedgerFrameSpec(action, offset_ms=0, airtime_ms=airtime_ms),
            LedgerFrameSpec(stop, offset_ms=_FLUSH_STOP_OFFSET_MS, airtime_ms=airtime_ms),
        ],
    )
    ledger.confirm("timed-move", _FLUSH_HANDOFF)
    return ledger, stop


def _register_displacing_command(
    ledger: CommandLedger,
    handoff: float | None = _DISPLACER_HANDOFF,
) -> None:
    """Confirm the newer overlapping command that makes the bridge flush.

    Itself a TIMED move, with a two-minute deadline of its own. That is the
    ordinary case — a second `set_cover_position` over an in-flight one — and
    it is the case that exposes any bound tied to the displacer's own armed
    span rather than to the instant it was admitted.
    """
    ledger.register_pending(
        "displacer",
        _BRIDGE_A,
        (1,),
        "UP",
        [
            LedgerFrameSpec(_required_signature((1,), "UP"), offset_ms=0, airtime_ms=3_000),
            LedgerFrameSpec(
                _required_signature((1,), "STOP"),
                offset_ms=_DISPLACER_STOP_OFFSET_MS,
                airtime_ms=3_000,
            ),
        ],
    )
    if handoff is not None:
        ledger.confirm("displacer", handoff)


def test_early_flushed_stop_heard_before_its_displaced_status_stays_ours() -> None:
    """The flushed STOP is ours whichever of /rx and "displaced" wins the race.

    Nothing orders a peer bridge's report of the flushed frame after the
    transmitting bridge's own status: both cross the same broker, and the same
    queueing that biases "started" late by a measured 1.117 s biases
    "displaced" too. In the losing order match() still sees the ORIGINAL
    windows -- the STOP sitting a full stop_after_ms away, far outside any
    anchor tolerance -- and our own flushed frame dispatches as a person
    stopping the blind, which then displaces the very command that caused the
    flush.

    The displacement is knowable without waiting for the status, because WE
    caused it: latest-command-wins means the newer overlapping command already
    registered for this bridge is what flushes the older one's armed STOP. That
    signal is local and lag-free, so it always wins the race.
    """
    ledger, _stop = _timed_move_ledger()
    _register_displacing_command(ledger)
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    consumer = _consumer(ledger, dispatched, proofs, [_FLUSH_HEARD_TIME])

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(_FLUSH_HEARD_TIME * _MILLISECONDS_PER_SECOND),
        _frame((1,), "STOP"),
        _FLUSH_HEARD_TIME,
    )

    assert dispatched == []
    # Heard on a peer bridge: still proof the frame reached the air.
    assert proofs == ["timed-move"]

    # The status finally lands and narrows the STOP to its drain.
    ledger.displace("timed-move", _FLUSH_DISPLACED_TIME)
    consumer.resume_holds("timed-move")

    assert dispatched == []


def test_stop_heard_mid_travel_without_a_displacement_is_still_a_press() -> None:
    """No displacing command means a mid-travel STOP is a person, as before.

    This is the bound on the fix above and the reason the queued STOP is not
    simply owned for its whole armed span. A timed move registers its own
    stop_raw, so somebody pressing STOP on the physical remote produces a
    capture IDENTICAL to a frame we have registered. Owning that span
    unconditionally would bypass the takeover machinery for the entire
    stop_after_ms, leaving the model travelling while the blind stands still.
    """
    ledger, _stop = _timed_move_ledger()
    dispatched: list[HeardEvent] = []
    consumer = _consumer(ledger, dispatched, [], [_FLUSH_HEARD_TIME])

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(_FLUSH_HEARD_TIME * _MILLISECONDS_PER_SECOND),
        _frame((1,), "STOP"),
        _FLUSH_HEARD_TIME,
    )

    assert [event.button for event in dispatched] == ["STOP"]


def test_displaced_stop_window_absorbs_the_displaced_status_transport_lag() -> None:
    """The displaced anchor is a receipt time and carries the same late bias.

    "displaced" has no age_ms, so displace() anchors on pure wall-clock
    receipt -- strictly worse than started_at, which at least removes the
    firmware's own queueing. The flush happened before that receipt, never
    after, so the drain window needs the identical lower-edge tolerance every
    other confirmed window already gets.
    """
    ledger, stop = _timed_move_ledger()

    assert ledger.displace("timed-move", _FLUSH_DISPLACED_TIME)

    assert ledger.match(stop, _FLUSH_HEARD_TIME) == (
        "confirmed",
        "timed-move",
        _BRIDGE_A,
    )


# Two sequential commands for the same remote, channels and button — a quick
# repeated close_cover — carry the SAME signature. With the anchor-lag lower
# edge their windows overlap, so an echo of the older command's own repeat
# train lands inside the newer command's window too.
_REPEAT_FIRST_HANDOFF: Final = 100.0
_REPEAT_SECOND_HANDOFF: Final = 104.0
_REPEAT_FIRST_ECHO_TIME: Final = 101.5


def _sequential_same_signature_ledger() -> tuple[CommandLedger, FrameSignature]:
    """Confirm two same-signature commands 4 s apart on one bridge."""
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    for command_id, handoff in (
        ("cmd-first", _REPEAT_FIRST_HANDOFF),
        ("cmd-second", _REPEAT_SECOND_HANDOFF),
    ):
        ledger.register_pending(
            command_id,
            _BRIDGE_A,
            (1,),
            "DOWN",
            [LedgerFrameSpec(action, offset_ms=0, airtime_ms=3_000)],
        )
        ledger.confirm(command_id, handoff)
    return ledger, action


def test_overlapping_windows_credit_the_command_that_actually_emitted() -> None:
    """An echo inside one command's own window is not stolen by its successor.

    match() takes the newest entry whose window contains the capture, and the
    anchor-lag lower edge makes the newer window reach back over the older
    one — far enough here that the older command's own echo, sitting squarely
    inside its NOMINAL window, is credited to a command that had not yet been
    handed off when the frame went on air.

    Press suppression is unaffected either way: both are our own frames and
    neither dispatches. The casualty is _on_emission_proof, which fires for the
    wrong command_id, so a cover waiting on a specific command for restore-time
    anchor verification never gets its proof.

    The tolerance is therefore ranked, not just added: a capture that fits a
    window on its own terms outranks one that only fits by spending the lag
    budget the late anchor might not even have needed.
    """
    ledger, action = _sequential_same_signature_ledger()

    assert ledger.match(action, _REPEAT_FIRST_ECHO_TIME) == (
        "confirmed",
        "cmd-first",
        _BRIDGE_A,
    )
    # The newer command still owns captures that fit it on its own terms.
    assert ledger.match(action, _REPEAT_SECOND_HANDOFF + 1.0) == (
        "confirmed",
        "cmd-second",
        _BRIDGE_A,
    )


def test_emission_proof_reaches_the_command_that_emitted_the_frame() -> None:
    """The consumer proves emission for the older command, not its successor."""
    ledger, _action = _sequential_same_signature_ledger()
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    consumer = _consumer(ledger, dispatched, proofs, [_REPEAT_SECOND_HANDOFF])

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(_REPEAT_FIRST_ECHO_TIME * _MILLISECONDS_PER_SECOND),
        _frame((1,), "DOWN"),
        _REPEAT_FIRST_ECHO_TIME,
    )

    assert dispatched == []
    assert proofs == ["cmd-first"]


def test_timed_displacer_does_not_own_a_stop_long_after_its_admission() -> None:
    """A displacer's armed span is not a licence to absorb later STOP presses.

    Displacement is a ONE-TIME event at admission: the bridge flushes the older
    command's armed frames as it accepts the newer one, and never again. Bounding
    ownership by the displacer's own liveness instead confuses the two, and a
    displacer that is itself a timed move stays "live" until its own deadline —
    which the firmware caps at MAX_TRAVEL_SECONDS, one hour.

    Adjusting a blind twice in quick succession is ordinary usage, so that bound
    left a real STOP on the first command's channels invisible for the entire
    remaining span of the second. This is the deafness #15 exists to eliminate,
    and it must not be reintroduced here in a wider form.
    """
    ledger, stop = _timed_move_ledger()
    _register_displacing_command(ledger)
    dispatched: list[HeardEvent] = []
    consumer = _consumer(ledger, dispatched, [], [_LATE_HUMAN_STOP_TIME])

    # Still armed: the displacer's own STOP is 120 s out, the victim's 60 s.
    assert ledger.match(stop, _LATE_HUMAN_STOP_TIME) is None

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(_LATE_HUMAN_STOP_TIME * _MILLISECONDS_PER_SECOND),
        _frame((1,), "STOP"),
        _LATE_HUMAN_STOP_TIME,
    )

    assert [event.button for event in dispatched] == ["STOP"]


def test_capture_held_against_a_newer_pending_twin_resolves_to_its_own_command() -> None:
    """A lag-funded capture parked behind a same-signature successor is not stranded.

    A hold/resume regression guard, NOT coverage of the #17 ranking: it passes
    against the pre-#17 single-window match() too, because the property it
    asserts — that holding a capture against a newer same-signature entry
    cannot lose it — was already true then. It is pinned here because that
    property had only ever been argued, never measured, and the ranking change
    made it load-bearing enough to be worth measuring.

    #17's actual pins are
    test_overlapping_windows_credit_the_command_that_actually_emitted and
    test_emission_proof_reaches_the_command_that_emitted_the_frame, both of
    which do fail against the original.
    """
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    ledger.register_pending(
        "cmd-anchored-late",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [LedgerFrameSpec(action, offset_ms=0, airtime_ms=3_000)],
    )
    # Anchored late enough that the capture below needs the lag budget.
    ledger.confirm("cmd-anchored-late", _REPEAT_FIRST_HANDOFF + _LATE_ANCHOR_SKEW_SECONDS)
    ledger.register_pending(
        "cmd-successor",
        _BRIDGE_A,
        (1,),
        "DOWN",
        [LedgerFrameSpec(action, offset_ms=0, airtime_ms=3_000)],
    )
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [_REPEAT_FIRST_HANDOFF]
    consumer = _consumer(ledger, dispatched, proofs, now_value)

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(_REPEAT_FIRST_HANDOFF * _MILLISECONDS_PER_SECOND),
        _frame((1,), "DOWN"),
        _REPEAT_FIRST_HANDOFF,
    )
    assert dispatched == []
    assert proofs == []  # held against the pending successor

    # The successor never reaches the air and is retired.
    ledger.retire("cmd-successor")
    consumer.resume_holds("cmd-successor")

    assert dispatched == []
    assert proofs == ["cmd-anchored-late"]


def test_whole_flushed_stop_train_below_the_admission_stays_ours() -> None:
    """The flush is a TRAIN, and every repeat of it precedes the admission.

    `schedule()` moves the owed STOP copies to `flush_stops_` and `next()`
    rotates them ahead of the replacement action, so the displacer's `started`
    cannot be reported until that drain permits it. The flush therefore
    occupies the whole `_ledger_airtime_ms(repeats)` BELOW the admission, not
    just the one frame ahead of it — the same train length `_window()` already
    budgets on the `ends_at` side, for the same reason.

    A flat tolerance is enough at the production default of 3 repeats but not
    at the 20 `config_flow` allows: the early repeats of our own genuinely
    flushed STOP fall outside it and dispatch as a phantom press. Recognising
    a later repeat does not undo the takeover the first one already triggered.
    """
    ledger, stop = _timed_move_ledger(_HIGH_REPEAT_TRAIN_AIRTIME_MS)
    _register_displacing_command(ledger, _HIGH_REPEAT_ADMISSION)
    dispatched: list[HeardEvent] = []
    consumer = _consumer(ledger, dispatched, [], [_HIGH_REPEAT_FIRST_FLUSH_TIME])

    assert ledger.match(stop, _HIGH_REPEAT_FIRST_FLUSH_TIME) == (
        "confirmed",
        "timed-move",
        _BRIDGE_A,
    )

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(_HIGH_REPEAT_FIRST_FLUSH_TIME * _MILLISECONDS_PER_SECOND),
        _frame((1,), "STOP"),
        _HIGH_REPEAT_FIRST_FLUSH_TIME,
    )

    assert dispatched == []


def test_hold_against_a_pending_timed_displacer_resolves_without_a_phantom_press() -> None:
    """Deferring to a pending displacer only helps if the bound is right after.

    A timed displacer registers its own stop_raw, so a flushed capture shares
    that pending entry's signature and is HELD rather than dispatched. That
    defers the decision; it does not make it. When the displacer confirms and
    the hold resumes, the admission bound is what finally classifies the
    capture — so a bound too short for the drain turns the deferral into a
    LATER phantom press rather than no phantom press at all.
    """
    ledger, _stop = _timed_move_ledger(_HIGH_REPEAT_TRAIN_AIRTIME_MS)
    _register_displacing_command(ledger, handoff=None)
    dispatched: list[HeardEvent] = []
    now_value = [_HIGH_REPEAT_FIRST_FLUSH_TIME]
    consumer = _consumer(ledger, dispatched, [], now_value)

    consumer.handle_rx(
        _BRIDGE_B,
        _BOOT,
        int(_HIGH_REPEAT_FIRST_FLUSH_TIME * _MILLISECONDS_PER_SECOND),
        _frame((1,), "STOP"),
        _HIGH_REPEAT_FIRST_FLUSH_TIME,
    )
    assert dispatched == []  # held against the pending displacer

    now_value[0] = _HIGH_REPEAT_ADMISSION
    ledger.confirm("displacer", _HIGH_REPEAT_ADMISSION)
    consumer.resume_holds("displacer")

    assert dispatched == []


# #21: a bridge holding several targets dispatches them ROUND-ROBIN, one slot
# each, so a command's own repeats are spread across the OTHER targets' slots
# too -- not sent back-to-back the way `_ledger_airtime_ms` assumes. Reproduces
# the office-bridge sweep from the issue: seven covers on one bridge, admitted
# at effectively the same instant (repeats=3, the production default).
_ROUND_ROBIN_BRIDGE: Final = "bridge-office"
_ROUND_ROBIN_PEER: Final = "bridge-peer"
_ROUND_ROBIN_TARGET_COUNT: Final = 7
_ROUND_ROBIN_TRAIN_MS: Final = 3_000
_ROUND_ROBIN_HANDOFF: Final = 0.0
# Measured on air (#21) for repeats=3 at this concurrency: an own repeat heard
# at +8.1 s, well outside the un-stretched 3.75 s window (3 s train + 0.75 s
# slack) but inside the round-robin-stretched one this fix computes:
# (repeats - 1) * (targets - 1) * 1 s + 3.75 s = 2 * 6 * 1 + 3.75 = 15.75 s.
_ROUND_ROBIN_LATE_OWN_REPEAT: Final = 8.1
_ROUND_ROBIN_STRETCHED_CLOSE: Final = 15.75
# Comfortably past the stretched close above: proves the window is bounded,
# not merely widened until nothing fires.
_ROUND_ROBIN_GENUINE_PRESS_TIME: Final = 20.0


# The real Mode: Away sweep admitted its covers about two seconds apart over
# roughly eleven seconds, not all at once -- see the logbook reconstruction in
# issue #21. Concurrency has to be counted for that shape too.
_ROUND_ROBIN_STAGGER_SECONDS: Final = 2.0


def _register_staggered_round_robin_burst(
    ledger: CommandLedger,
    consumer: StateSyncConsumer,
) -> None:
    """Confirm seven same-bridge commands admitted two seconds apart."""
    for index in range(_ROUND_ROBIN_TARGET_COUNT):
        channels = (index + 1,)
        command_id = f"sweep-{index}"
        handoff = _ROUND_ROBIN_HANDOFF + index * _ROUND_ROBIN_STAGGER_SECONDS
        ledger.register_pending(
            command_id,
            _ROUND_ROBIN_BRIDGE,
            channels,
            "DOWN",
            [
                LedgerFrameSpec(
                    _required_signature(channels, "DOWN"),
                    offset_ms=0,
                    airtime_ms=_ROUND_ROBIN_TRAIN_MS,
                ),
            ],
        )
        consumer.record_commanded_start(_REMOTE_KEY, frozenset(channels), handoff)
        ledger.confirm(command_id, handoff)


def test_staggered_burst_counts_concurrency_past_its_nominal_span() -> None:
    """A sweep admitted gradually must still be recognised as concurrent.

    Counting a peer only when its UNSTRETCHED span overlaps ours answers a
    strictly smaller question than the one that matters, because stretch is
    precisely what makes real occupancy exceed the nominal span. At the real
    sweep's two-second cadence that undercounted seven concurrent targets as
    two, closing the window at +5.75 s and dispatching this command's own
    +8.1 s repeat as a physical press -- the very failure #21 shipped to fix,
    for the admission shape the incident actually had.

    Consumer level, through ``handle_rx`` WITH ``record_commanded_start``.
    """
    ledger = CommandLedger()
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [_ROUND_ROBIN_HANDOFF]
    consumer = _consumer(ledger, dispatched, proofs, now_value)
    _register_staggered_round_robin_burst(ledger, consumer)

    assert ledger._round_robin_concurrency(ledger._entries["sweep-0"]) == (
        _ROUND_ROBIN_TARGET_COUNT
    )

    heard_at = _ROUND_ROBIN_HANDOFF + 8.1
    now_value[0] = heard_at
    consumer.handle_rx(
        _ROUND_ROBIN_PEER,
        _BOOT,
        int(heard_at * _MILLISECONDS_PER_SECOND),
        _frame((1,), "DOWN"),
        heard_at,
    )

    assert dispatched == []
    assert "sweep-0" in proofs


def _register_round_robin_burst(ledger: CommandLedger, consumer: StateSyncConsumer) -> None:
    """Confirm seven same-bridge, distinct-channel commands at one instant."""
    for index in range(_ROUND_ROBIN_TARGET_COUNT):
        channels = (index + 1,)
        command_id = f"office-{index}"
        ledger.register_pending(
            command_id,
            _ROUND_ROBIN_BRIDGE,
            channels,
            "DOWN",
            [
                LedgerFrameSpec(
                    _required_signature(channels, "DOWN"),
                    offset_ms=0,
                    airtime_ms=_ROUND_ROBIN_TRAIN_MS,
                ),
            ],
        )
        consumer.record_commanded_start(
            _REMOTE_KEY,
            frozenset(channels),
            _ROUND_ROBIN_HANDOFF,
        )
        ledger.confirm(command_id, _ROUND_ROBIN_HANDOFF)


def test_round_robin_burst_late_own_repeat_is_not_dispatched_as_press() -> None:
    """A same-bridge concurrent burst must not phantom-press its own repeats.

    Driven through ``handle_rx`` WITH ``record_commanded_start``, exactly like
    the pinned STOP-echo regression above: the commanded-start guard cannot
    help here either, because this late copy arrives well AFTER our own
    recorded start, not before it. Before this fix, ``_ledger_airtime_ms``
    charged this command's train as if it ran alone on the bridge, closing its
    window at +3.75 s; the office bridge's real round-robin scheduling among
    seven targets pushes the true last repeat out to +8.1 s, well past that,
    and the frame dispatched as a physical press on a cover HA still believed
    it was driving.
    """
    ledger = CommandLedger()
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [_ROUND_ROBIN_HANDOFF]
    consumer = _consumer(ledger, dispatched, proofs, now_value)
    _register_round_robin_burst(ledger, consumer)

    # Command index 2's own late repeat, overheard by a peer bridge.
    now_value[0] = _ROUND_ROBIN_LATE_OWN_REPEAT
    consumer.handle_rx(
        _ROUND_ROBIN_PEER,
        _BOOT,
        int(_ROUND_ROBIN_LATE_OWN_REPEAT * _MILLISECONDS_PER_SECOND),
        _frame((3,), "DOWN"),
        _ROUND_ROBIN_LATE_OWN_REPEAT,
    )

    assert dispatched == []
    assert "office-2" in proofs


def test_genuine_press_after_round_robin_burst_window_still_dispatches() -> None:
    """The round-robin stretch is bounded, not an unbounded widening.

    Requirement, not a nicety: an over-wide window would suppress genuine
    takeover detection for its whole span. This proves detection resumes once
    a capture is genuinely past the round-robin-stretched close -- the fix
    widens proportionally to the SEVEN observed concurrent targets, it does
    not simply stop firing forever.
    """
    ledger = CommandLedger()
    dispatched: list[HeardEvent] = []
    proofs: list[str] = []
    now_value = [_ROUND_ROBIN_HANDOFF]
    consumer = _consumer(ledger, dispatched, proofs, now_value)
    _register_round_robin_burst(ledger, consumer)

    assert _ROUND_ROBIN_GENUINE_PRESS_TIME > _ROUND_ROBIN_STRETCHED_CLOSE

    now_value[0] = _ROUND_ROBIN_GENUINE_PRESS_TIME
    consumer.handle_rx(
        _ROUND_ROBIN_PEER,
        _BOOT,
        int(_ROUND_ROBIN_GENUINE_PRESS_TIME * _MILLISECONDS_PER_SECOND),
        _frame((3,), "DOWN"),
        _ROUND_ROBIN_GENUINE_PRESS_TIME,
    )

    assert [event.button for event in dispatched] == ["DOWN"]


# A direct (ledger-internal) check of the 16-target cap itself: twenty commands
# sharing one channel and bridge would imply concurrency=20 if uncounted, but
# the firmware can never round-robin more than sixteen targets (a 1..16
# channel field), so the stretch must be computed as if only sixteen were
# live. This supplements, and does not replace, the consumer-level tests
# above: it pins the specific bound from requirement 3, which those two
# black-box captures cannot distinguish from an unbounded count.
_CAP_TARGET_COUNT: Final = 20
# repeats=3 (production default) so (repeats - 1) = 2 actually scales with
# concurrency; repeats=1 would zero the stretch term regardless of any cap.
_CAP_TRAIN_MS: Final = 3_000
_CAP_HANDOFF: Final = 0.0
# Capped at 16 targets: (repeats=3 - 1) * (16 - 1) * 1 s + (3 s train + 0.75 s
# slack) = 30 + 3.75 = 33.75 s. Uncapped at all twenty: (3 - 1) * (20 - 1) * 1
# s + 3.75 s = 38 + 3.75 = 41.75 s.
_CAP_BETWEEN_CAPPED_AND_UNCAPPED: Final = 37.0


def test_round_robin_concurrency_is_capped_at_the_firmware_target_limit() -> None:
    """More confirmed peers than the firmware could ever hold stop counting."""
    ledger = CommandLedger()
    signature = _required_signature((1,), "DOWN")
    for index in range(_CAP_TARGET_COUNT):
        ledger.register_pending(
            f"cap-{index}",
            _BRIDGE_A,
            (1,),
            "DOWN",
            [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=_CAP_TRAIN_MS)],
        )
        ledger.confirm(f"cap-{index}", _CAP_HANDOFF)

    assert ledger.match(signature, _CAP_BETWEEN_CAPPED_AND_UNCAPPED) is None


# #22 coherence check: a TIMED move's action frame is deliberately truncated
# below the full repeat train (models.py::_ledger_registration computes
# `action_ms = min(train_ms, stop_after_ms + _LEDGER_REPEAT_AIRTIME_MS)`,
# unchanged by this fix) because its fail-safe STOP promotes and preempts
# the remaining action repeats at its wall-clock deadline. The round-robin
# stretch must compose with that truncation rather than fight it: it is
# applied per-window from THAT window's own `train_seconds`, so the
# (already-shorter) action window is stretched using its own smaller
# effective repeat count, while the untruncated STOP window -- a separate
# window on a separate signature -- is stretched using the full count. This
# pins that composition with a concrete number rather than only reasoning
# about it.
_TIMED_ROUND_ROBIN_HANDOFF: Final = 0.0
_TIMED_ROUND_ROBIN_TARGET_COUNT: Final = 7
_TIMED_ROUND_ROBIN_TRAIN_MS: Final = 3_000  # repeats=3, the production default
# Chosen to avoid an exact .5 s boundary (Python's round() is round-half-to-
# even, which would make the effective repeat count ambiguous at one).
_TIMED_ROUND_ROBIN_STOP_AFTER_MS: Final = 1_200
# action_ms = min(3000, 1200 + 1000) = 2200 -> train_seconds=2.2 -> repeats=2.
# Nominal action window closes at 2.2 + 0.75 = 2.95 s. Stretched by
# concurrency=7: (2 - 1) * (7 - 1) * 1 s = 6 s -> closes at 8.95 s.
_TIMED_ROUND_ROBIN_LATE_TRUNCATED_ECHO: Final = 5.0


def test_round_robin_stretch_composes_with_a_timed_moves_truncated_action_window() -> None:
    """A timed move's shortened action window still stretches correctly (#22).

    Without this composition, a timed move's own late-but-legitimate action
    repeat -- already narrowed by the fail-safe-STOP truncation -- would be
    doubly disadvantaged under concurrency: narrowed AND unstretched. This
    proves the stretch still reaches a truncated window using that window's
    own (smaller) repeat count, exactly as it does for an untruncated one.
    """
    ledger = CommandLedger()
    action = _required_signature((1,), "DOWN")
    stop = _required_signature((1,), "STOP")
    ledger.register_pending(
        "timed-office",
        _ROUND_ROBIN_BRIDGE,
        (1,),
        "DOWN",
        [
            LedgerFrameSpec(
                action,
                offset_ms=0,
                airtime_ms=(
                    _TIMED_ROUND_ROBIN_STOP_AFTER_MS + state_sync_module._LEDGER_REPEAT_AIRTIME_MS
                ),
            ),
            LedgerFrameSpec(
                stop,
                offset_ms=_TIMED_ROUND_ROBIN_STOP_AFTER_MS,
                airtime_ms=_TIMED_ROUND_ROBIN_TRAIN_MS,
            ),
        ],
    )
    ledger.confirm("timed-office", _TIMED_ROUND_ROBIN_HANDOFF)
    for index in range(_TIMED_ROUND_ROBIN_TARGET_COUNT - 1):
        channels = (index + 10,)
        ledger.register_pending(
            f"peer-{index}",
            _ROUND_ROBIN_BRIDGE,
            channels,
            "DOWN",
            [
                LedgerFrameSpec(
                    _required_signature(channels, "DOWN"),
                    offset_ms=0,
                    airtime_ms=_TIMED_ROUND_ROBIN_TRAIN_MS,
                ),
            ],
        )
        ledger.confirm(f"peer-{index}", _TIMED_ROUND_ROBIN_HANDOFF)

    assert ledger.match(action, _TIMED_ROUND_ROBIN_LATE_TRUNCATED_ECHO) == (
        "confirmed",
        "timed-office",
        _ROUND_ROBIN_BRIDGE,
    )
