"""Tests for pure RF receive classification and state synchronization."""

from __future__ import annotations

from typing import Final

import pytest

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
    clock.observe(_BOOT, 100_500, 200.0)

    assert clock.to_ha_time(_BOOT, 101_500, 200.0) == pytest.approx(101.5)


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
) -> StateSyncConsumer:
    """Build a deterministic consumer around mutable observation lists."""
    return StateSyncConsumer(
        ledger=ledger,
        clock=BridgeClock(),
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


def test_consumer_close_clears_state_and_stops_dispatch() -> None:
    """Closing is idempotent and prevents later capture delivery."""
    dispatched: list[HeardEvent] = []
    consumer = _consumer(CommandLedger(), dispatched, [], [10.0])
    consumer.handle_rx(_BRIDGE_A, _BOOT, 1_000, _frame((1,), "UP"), 10.0)

    consumer.close()
    consumer.close()
    consumer.handle_rx(_BRIDGE_A, _BOOT, 2_000, _frame((1,), "DOWN"), 11.0)

    assert len(dispatched) == 1
