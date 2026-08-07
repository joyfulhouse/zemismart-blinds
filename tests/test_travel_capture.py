"""Unit tests for travel-time capture: matching, timing, and the run machine.

Every frame here is synthesized through the hardware-validated codec, never
pasted from a house capture, so a protocol regression fails loudly without any
real remote's replayable command material entering the repository.
"""

from __future__ import annotations

from custom_components.zemismart_blinds.codec import (
    CommandBases,
    encode_b0,
    infer_action_button,
    make_payload,
)
from custom_components.zemismart_blinds.config_models import MAX_TRAVEL_SECONDS, RemoteIdentity
from custom_components.zemismart_blinds.travel_capture import (
    TimedPress,
    TravelRun,
    identify_button,
    interval_seconds,
    stored_value,
)
from tests.synthetic import (
    TEST_BASES,
    TEST_PREFIX,
    TEST_REMOTE_ID,
    UNTABLED_BASES,
    UNTABLED_PREFIX,
    UNTABLED_REMOTE_ID,
)


def b1_frame(
    prefix: int,
    remote_id: int,
    channels: tuple[int, ...],
    button: str,
    bases: CommandBases,
) -> str:
    """Synthesize one bridge RX capture for a press we choose."""
    body = encode_b0(make_payload(prefix, remote_id, channels, button, bases=bases))[6:-2]
    return f"AAB1{body[:2]}{body[4:]}3855"


def test_identify_button_matches_each_calibrated_base() -> None:
    """Every action of a calibrated remote is identified from its own frame."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    for button in ("UP", "DOWN", "STOP"):
        frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), button, TEST_BASES)
        assert identify_button(identity, (1, 2), frame) == button


def test_identify_button_works_where_opcode_inference_fails() -> None:
    """The untabled remote from #26 is identified exactly.

    ``infer_action_button`` reads the opcode byte against a 10-sample empirical
    table, and this remote's opcodes fall outside it -- gating on that table
    silently dropped every press of a remote that was transmitting perfectly.
    Matching against the remote's OWN calibrated bases has no table to be wrong
    about, which is why travel capture can be exact where the Learn wizard,
    running before any calibration exists, cannot.
    """
    identity = RemoteIdentity(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, UNTABLED_BASES)
    for button in ("UP", "DOWN", "STOP"):
        payload = make_payload(
            UNTABLED_PREFIX,
            UNTABLED_REMOTE_ID,
            (1,),
            button,
            bases=UNTABLED_BASES,
        )
        assert infer_action_button((1,), payload & 0xFFFF) is None, "fixture must be untabled"
        frame = b1_frame(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1,), button, UNTABLED_BASES)
        assert identify_button(identity, (1,), frame) == button


def test_identify_button_rejects_another_remote() -> None:
    """A press on a different remote is not this cover's run."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    frame = b1_frame(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1, 2), "UP", UNTABLED_BASES)
    assert identify_button(identity, (1, 2), frame) is None


def test_identify_button_rejects_other_channels() -> None:
    """The same remote on a different channel set drives another blind."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (3,), "UP", TEST_BASES)
    assert identify_button(identity, (1, 2), frame) is None


def test_identify_button_rejects_the_oem_trailer_burst() -> None:
    """The trailer frame that follows UP and DOWN matches no action base.

    A real remote emits it on every direction press, so treating an unmatched
    base as an action would close a run the instant it opened.
    """
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "TRAILER", TEST_BASES)
    assert identify_button(identity, (1, 2), frame) is None


def test_identify_button_rejects_undecodable_input() -> None:
    """Garbage on the RX topic is ignored rather than raising."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    assert identify_button(identity, (1, 2), "not-a-frame") is None


def test_identify_button_requires_a_calibration() -> None:
    """An uncalibrated identity can match nothing."""
    identity = RemoteIdentity(0x010203, 0x04)
    assert identity.bases is None, "fixture must be uncalibrated"
    frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", TEST_BASES)
    assert identify_button(identity, (1, 2), frame) is None


def press(
    button: str,
    *,
    boot: int | None = 7,
    bridge_millis: int | None = 0,
    monotonic: float = 0.0,
) -> TimedPress:
    """Build one accepted press with explicit clocks."""
    return TimedPress(
        button=button,
        boot=boot,
        bridge_millis=bridge_millis,
        received_at_monotonic=monotonic,
    )


def test_interval_uses_the_bridge_clock() -> None:
    """The bridge's own millis cancel broker and event-loop jitter.

    The monotonic receive times here are deliberately inconsistent with the
    bridge stamps: if the fallback were preferred this would measure 40 s.
    """
    start = press("DOWN", bridge_millis=1_000, monotonic=100.0)
    stop = press("STOP", bridge_millis=15_310, monotonic=140.0)
    assert interval_seconds(start, stop) == 14.31


def test_interval_survives_a_uint32_wrap() -> None:
    """A run that straddles the bridge's 32-bit millisecond rollover."""
    start = press("UP", bridge_millis=0xFFFFF000)
    stop = press("STOP", bridge_millis=0x00000AC8)
    assert interval_seconds(start, stop) == 6.856


def test_interval_rejects_a_backwards_delta() -> None:
    """Reordered delivery, or a bridge that rebooted its clock to near zero."""
    start = press("UP", bridge_millis=20_000)
    stop = press("STOP", bridge_millis=1_000)
    assert interval_seconds(start, stop) is None


def test_interval_rejects_a_boot_change() -> None:
    """A bridge that restarted mid-run cannot have timed it."""
    start = press("UP", boot=7, bridge_millis=1_000)
    stop = press("STOP", boot=8, bridge_millis=15_000)
    assert interval_seconds(start, stop) is None


def test_interval_falls_back_to_monotonic() -> None:
    """Older firmware omits `t`; the receive times still bound the run."""
    start = press("DOWN", bridge_millis=None, monotonic=100.0)
    stop = press("STOP", bridge_millis=None, monotonic=114.5)
    assert interval_seconds(start, stop) == 14.5


def test_interval_rejects_backwards_monotonic_fallback() -> None:
    """Out-of-order delivery is not a negative travel time."""
    start = press("DOWN", bridge_millis=None, monotonic=114.5)
    stop = press("STOP", bridge_millis=None, monotonic=100.0)
    assert interval_seconds(start, stop) is None


def test_stored_value_rounds_up() -> None:
    """Ceiling errs long, which is the safe direction for an open-loop model."""
    assert stored_value(14.0) == 14
    assert stored_value(14.0001) == 15
    assert stored_value(16.08) == 17


def test_stored_value_rejects_an_impossibly_fast_run() -> None:
    """A double-tap is not a shade running to its limit."""
    assert stored_value(0.5) is None


def test_stored_value_rejects_beyond_the_storable_maximum() -> None:
    """A value CoverConfig would refuse must never reach it."""
    assert stored_value(float(MAX_TRAVEL_SECONDS) + 1.0) is None


def run_for(wanted: tuple[str, ...] = ("UP", "DOWN")) -> TravelRun:
    """Build one run against the calibrated test remote on channels 1 and 2."""
    return TravelRun(
        identity=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1, 2),
        wanted=frozenset(wanted),
    )


def rx(button: str, millis: int) -> dict[str, object]:
    """Build one RX payload the bridge would publish for a press."""
    return {
        "frame": b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), button, TEST_BASES),
        "t": millis,
        "boot": 7,
    }


def test_a_direction_then_stop_yields_a_measurement() -> None:
    """The happy path: press DOWN, watch it arrive, press STOP."""
    run = run_for()
    assert run.offer_payload(rx("DOWN", 1_000), 100.0) is None
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31)
    assert measurement is not None
    assert measurement.direction == "DOWN"
    assert measurement.measured_seconds == 14.31
    assert measurement.stored_seconds == 15


def test_burst_repeats_neither_restart_nor_close_the_run() -> None:
    """One press is 8 frames across ~609 ms; the run starts once, at the first.

    A bridge hears an unreliable subset of a burst, so a later copy must not
    re-stamp the start -- that would silently shorten every measurement by
    however much of the opening burst happened to be heard.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    for index in range(1, 8):
        assert run.offer_payload(rx("DOWN", 1_000 + index * 76), 100.0 + index * 0.076) is None
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31)
    assert measurement is not None
    assert measurement.measured_seconds == 14.31, "the FIRST frame must stamp the start"


def test_a_later_press_restarts_the_run() -> None:
    """A direction press outside the burst window is the user starting over."""
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    run.offer_payload(rx("DOWN", 5_000), 104.0)
    measurement = run.offer_payload(rx("STOP", 19_000), 118.0)
    assert measurement is not None
    assert measurement.measured_seconds == 14.0


def test_a_reversal_restarts_on_the_new_direction() -> None:
    """DOWN then UP with no STOP between is a user changing their mind."""
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    run.offer_payload(rx("UP", 4_000), 103.0)
    measurement = run.offer_payload(rx("STOP", 20_000), 119.0)
    assert measurement is not None
    assert measurement.direction == "UP"
    assert measurement.stored_seconds == 16


def test_a_stop_with_no_open_run_is_ignored() -> None:
    """A stray STOP is the tail of something else, not a zero-length run."""
    run = run_for()
    assert run.offer_payload(rx("STOP", 1_000), 100.0) is None


def test_a_stop_burst_closes_the_run_only_once() -> None:
    """The seven repeats behind the closing STOP resolve nothing further."""
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    assert run.offer_payload(rx("STOP", 15_310), 114.31) is not None
    for index in range(1, 8):
        assert run.offer_payload(rx("STOP", 15_310 + index * 76), 114.31 + index * 0.076) is None


def test_the_second_run_ignores_the_direction_already_measured() -> None:
    """Only the direction still wanted may open the second run.

    The screen has explicitly asked for the other one, and silently overwriting
    a good measurement would make the redo menu option meaningless.
    """
    run = run_for(wanted=("UP",))
    assert run.offer_payload(rx("DOWN", 1_000), 100.0) is None
    assert run.started is None
    assert run.offer_payload(rx("STOP", 15_000), 114.0) is None
    run.offer_payload(rx("UP", 20_000), 119.0)
    measurement = run.offer_payload(rx("STOP", 36_000), 135.0)
    assert measurement is not None
    assert measurement.direction == "UP"


def test_a_run_too_fast_to_be_real_yields_nothing() -> None:
    """A double-tap does not become a half-second travel time."""
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    assert run.offer_payload(rx("STOP", 1_400), 100.4) is None


def test_a_foreign_press_never_opens_a_run() -> None:
    """Another remote's traffic on the same bridge is not this cover's run."""
    run = run_for()
    foreign = {
        "frame": b1_frame(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1, 2), "DOWN", UNTABLED_BASES),
        "t": 1_000,
        "boot": 7,
    }
    assert run.offer_payload(foreign, 100.0) is None
    assert run.started is None


def test_a_malformed_payload_is_ignored() -> None:
    """Missing and mistyped fields never raise out of the handler."""
    run = run_for()
    assert run.offer_payload({}, 100.0) is None
    assert run.offer_payload({"frame": 42}, 100.0) is None
    assert run.offer_payload({"frame": "not-a-frame", "t": 1}, 100.0) is None


def test_a_boolean_timestamp_is_not_a_bridge_clock() -> None:
    """`True` is an int in Python; it is not a uint32 millisecond stamp."""
    run = run_for()
    run.offer_payload({**rx("DOWN", 1_000), "t": True}, 100.0)
    measurement = run.offer_payload({**rx("STOP", 15_000), "t": True}, 114.5)
    assert measurement is not None
    assert measurement.measured_seconds == 14.5, "must fall back to monotonic"
