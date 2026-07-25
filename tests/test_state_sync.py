"""Tests for pure RF receive classification and state synchronization."""

from __future__ import annotations

from typing import Final

import pytest

from custom_components.zemismart_blinds import state_sync as state_sync_module
from custom_components.zemismart_blinds.codec import encode_b0, make_payload
from custom_components.zemismart_blinds.state_sync import (
    BridgeClock,
    CommandLedger,
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
    """A late older physical capture cannot replace a newer commanded start."""
    dispatched: list[HeardEvent] = []
    consumer = _consumer(CommandLedger(), dispatched, [], [_LATE_DELIVERY_TIME])
    signature = _required_signature((1,), "UP")
    consumer.record_commanded_start(
        _REMOTE_KEY,
        frozenset({1}),
        _COMMANDED_START_TIME,
    )

    consumer._dispatch_press(
        signature,
        _OLDER_PRESS_TIME,
        _BRIDGE_A,
        _LATE_DELIVERY_TIME,
    )
    consumer._dispatch_press(
        signature,
        _NEWER_PRESS_TIME,
        _BRIDGE_A,
        _LATE_DELIVERY_TIME,
    )

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
        _GENUINE_PRESS_REGISTER_TIME,
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


def test_held_capture_trust_bound_is_inclusive_at_its_edge() -> None:
    """Pin the exact cutoff: at the bound we own it, just past it we do not."""
    signature = _required_signature((1,), "STOP")

    def resolve(gap: float) -> tuple[str, str, str] | None:
        ledger = CommandLedger()
        ledger.register_pending(
            "edge",
            _BRIDGE_A,
            (1,),
            "STOP",
            [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=3_000)],
            _GENUINE_PRESS_REGISTER_TIME,
        )
        handoff = _GENUINE_PRESS_REGISTER_TIME + state_sync_module._LEDGER_ANCHOR_LAG_SECONDS
        ledger.confirm("edge", handoff)
        return ledger.resolve_held("edge", signature, handoff - gap)

    bound = state_sync_module._LEDGER_ANCHOR_LAG_SECONDS
    assert resolve(bound) is not None
    assert resolve(bound - 0.5) is not None
    assert resolve(bound + 0.001) is None


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
        _LATE_ANCHOR_HANDOFF,
    )
    # The status lagged, so confirm() anchors every window late by that skew.
    ledger.confirm("timed-move", _LATE_ANCHOR_HANDOFF + _LATE_ANCHOR_SKEW_SECONDS)

    # Our own STOP goes on air at its TRUE scheduled time, before the window
    # the late anchor implies.
    true_stop_time = _LATE_ANCHOR_HANDOFF + _LATE_ANCHOR_STOP_OFFSET_MS / 1_000
    assert ledger.match(stop, true_stop_time) == ("confirmed", "timed-move", _BRIDGE_A)

    # Tolerance is lower-edge only: long after the train ends it is a real press.
    assert ledger.match(stop, true_stop_time + 60.0) is None
