"""Unit tests for travel-time capture: matching, timing, and the run machine.

Every frame here is synthesized through the hardware-validated codec, never
pasted from a house capture, so a protocol regression fails loudly without any
real remote's replayable command material entering the repository.
"""

from __future__ import annotations

from custom_components.zemismart_blinds.codec import (
    DEFAULT_BUCKETS,
    CommandBases,
    encode_b0,
    infer_action_button,
    make_payload,
)
from custom_components.zemismart_blinds.config_models import MAX_TRAVEL_SECONDS, RemoteIdentity
from custom_components.zemismart_blinds.const import TRAVEL_BURST_WINDOW_SECONDS
from custom_components.zemismart_blinds.travel_capture import (
    _HEARD_CAP,
    TimedPress,
    TravelRun,
    identify_button,
    interval_seconds,
    press_signature,
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
    *,
    buckets: str = DEFAULT_BUCKETS,
) -> str:
    """Synthesize one bridge RX capture for a press we choose.

    ``buckets`` are the pulse widths the receiving bridge MEASURED, so two
    bridges' captures of one physical press differ here and nowhere else --
    which is the whole reason a raw-frame comparison cannot deduplicate them.
    """
    payload = make_payload(prefix, remote_id, channels, button, bases=bases)
    body = encode_b0(payload, buckets)[6:-2]
    return f"AAB1{body[:2]}{body[4:]}3855"


def as_measured_by(index: int) -> str:
    """Return one bridge's own reading of the standard OEM pulse widths.

    Real captures of one burst differ by a few microseconds per bridge; only
    the long-bit bucket is varied here, well inside the codec's short/long
    thresholds, so every variant decodes to the same press.
    """
    return f"1414{0x0264 + index:04X}01181414"


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


def test_a_later_press_of_the_same_direction_never_restarts_the_run() -> None:
    """Nothing distinguishes a late copy of one press from a genuine re-press.

    The frames are identical -- the protocol carries no sequence number and no
    per-press nonce -- so "same button, long enough after" is the only rule
    available, and a bridge lagging by more than a burst satisfies it. Keeping
    the first anchor is therefore the only safe rule: the shade started moving
    on the first press, and erring long stalls a motor against its own limit
    switch where erring short leaves "closed" visibly open.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    run.offer_payload(rx("DOWN", 5_000), 104.0)
    measurement = run.offer_payload(rx("STOP", 19_000), 118.0)
    assert measurement is not None
    assert measurement.measured_seconds == 18.0, "the run stays anchored at the FIRST press"


def test_an_isolated_late_copy_never_reanchors_the_run() -> None:
    """One copy arriving just past the window has no chain to slide.

    The sliding window absorbs a burst whose copies keep coming, but a bridge
    that delivers a single copy of the press more than a whole window after
    the last one escapes it entirely. Accepting that copy as a fresh press
    re-anchors the run and stores a travel time short by the delivery lag --
    the unsafe direction. `_open` refuses on the SIGNATURE, so no window can
    be too narrow for it.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0, bridge_id="bridge-a")
    late = 100.0 + TRAVEL_BURST_WINDOW_SECONDS + 0.01
    assert run.offer_payload(rx("DOWN", 700_000), late, bridge_id="bridge-b") is None
    assert run.started is not None
    assert run.started.bridge_id == "bridge-a", "the first copy still owns the run"
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.measured_seconds == 14.31, (
        "re-anchoring on the late copy would have stored 12.8s -- 1.5 seconds short"
    )


def test_a_rapid_repress_after_a_discarded_run_still_registers() -> None:
    """A press that would OPEN a run is never swallowed as a duplicate.

    A double-tap closes a run too fast to store, and the user immediately
    presses again -- inside the repeat window of their own first press.
    Filtering copies ahead of the run swallowed that second press, and with
    no run open the wizard then waited out its whole deadline having heard
    the user twice. A press with no run to shorten cannot be unsafe.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    assert run.offer_payload(rx("STOP", 1_400), 100.4) is None, "too fast to be a real run"
    assert run.started is None

    run.offer_payload(rx("DOWN", 2_000), 101.0)
    assert run.started is not None, "the re-press must open a run"
    measurement = run.offer_payload(rx("STOP", 16_000), 115.0)
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


def test_a_foreign_press_is_recorded_for_the_timeout_screen() -> None:
    """A rejected real press is kept -- deduplicated -- so a timeout can name it.

    The burst repeats of one press must collapse to one record, or the
    timeout screen would show a wall of duplicates instead of the remote
    actually in the user's hand.
    """
    run = run_for()
    foreign = {
        "frame": b1_frame(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1, 2), "DOWN", UNTABLED_BASES),
        "t": 1_000,
        "boot": 7,
    }
    for _ in range(8):
        assert run.offer_payload(foreign, 100.0) is None
    assert len(run.heard) == 1
    press = run.heard[0]
    assert (press.prefix, press.remote_id) == (UNTABLED_PREFIX, UNTABLED_REMOTE_ID)
    assert press.channels == (1, 2)
    assert press.button is None, "the untabled remote's opcode must not be inferred"


def test_own_remote_on_other_channels_is_recorded() -> None:
    """The same remote heard on another channel set is the actionable near-miss."""
    run = run_for()
    other = {
        "frame": b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (3,), "UP", TEST_BASES),
        "t": 1_000,
        "boot": 7,
    }
    assert run.offer_payload(other, 100.0) is None
    assert len(run.heard) == 1
    press = run.heard[0]
    assert (press.prefix, press.remote_id) == (TEST_PREFIX, TEST_REMOTE_ID)
    assert press.channels == (3,)
    assert press.button == "UP", "a tabled opcode seeds the identity-update offer"


def test_the_own_trailer_burst_is_not_recorded() -> None:
    """The non-action trailer frame is noise, not a press worth reporting."""
    run = run_for()
    trailer = {
        "frame": b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "TRAILER", TEST_BASES),
        "t": 1_000,
        "boot": 7,
    }
    assert run.offer_payload(trailer, 100.0) is None
    assert run.heard == []


def test_a_second_bridge_copy_of_the_press_does_not_reanchor_the_run() -> None:
    """The same physical press heard by two bridges opens ONE run.

    Fleet listening delivers a press once per bridge that heard it. A later
    copy from another bridge lands inside the burst window and must be
    absorbed exactly like the burst's own repeats -- re-anchoring on it would
    silently shorten the measurement by the inter-bridge delivery skew.
    """
    run = run_for()
    assert run.offer_payload(rx("DOWN", 1_000), 100.0, bridge_id="bridge-a") is None
    assert run.offer_payload(rx("DOWN", 40_500), 100.04, bridge_id="bridge-b") is None
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.measured_seconds == 14.31, "bridge-a's own clock must time the run"


def test_a_stop_heard_only_by_another_bridge_falls_back_to_monotonic() -> None:
    """The Kaelyn case: the opening bridge never hears the STOP.

    The STOP bridge carries a ``t`` and even the same ``boot`` number, but its
    clock shares no epoch with the bridge that heard the press, so the run
    must be timed on the monotonic receive times instead.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0, bridge_id="bridge-a")
    measurement = run.offer_payload(rx("STOP", 900_000), 114.5, bridge_id="bridge-b")
    assert measurement is not None
    assert measurement.measured_seconds == 14.5, "cross-bridge t subtraction is meaningless"


def test_one_press_heard_by_every_bridge_is_counted_once() -> None:
    """A press the whole fleet heard opens ONE run, timed from the first copy.

    Seven bridges each delivering the 8-frame burst is 56 copies of one
    physical press. Every copy after the first must be filtered out ahead of
    the run: the measurement is bounded by when the press HAPPENED, not by
    which bridge's delivery happened to arrive last.
    """
    run = run_for()
    # Every bridge reports its own `t`; only the receive clock relates them.
    # Copies interleave across bridges, so they are offered in arrival order.
    arrivals = sorted(
        (repeat * 0.076 + index * 0.011, index, bridge, repeat)
        for index, bridge in enumerate(f"bridge-{letter}" for letter in "abcdefg")
        for repeat in range(8)
    )
    for offset, index, bridge, repeat in arrivals:
        assert (
            run.offer_payload(
                rx("DOWN", 1_000 + index * 40_000 + repeat * 76),
                100.0 + offset,
                bridge_id=bridge,
            )
            is None
        )
    assert run.started is not None
    assert run.started.bridge_id == "bridge-a", "the first copy heard opens the run"
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.measured_seconds == 14.31, "56 copies of one press are one press"


def test_a_lagging_bridges_spread_copies_never_shorten_the_run() -> None:
    """Copies that keep arriving must not expire into a phantom re-press.

    A bridge under broker backpressure delivers its share of the burst
    stretched out, so the LAST copy of one press can land more than a burst
    window after the FIRST. Treating that copy as a new press restarts the
    run and stores a travel time short by the whole spread -- the unsafe
    direction, since a short time leaves "closed" visibly open. The filter
    slides per signature, so the chain holds however long it runs.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0, bridge_id="bridge-a")
    for step in (1.0, 2.0, 3.0):
        assert (
            run.offer_payload(rx("DOWN", 500_000 + int(step * 1_000)), 100.0 + step, "bridge-b")
            is None
        ), f"the copy {step}s in is still one press, not a re-press"
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.measured_seconds == 14.31, (
        "a re-anchor on the 3.0s copy would have stored 11.31s -- 3 seconds short"
    )


def test_a_stop_inside_the_window_is_not_swallowed_as_a_repeat() -> None:
    """The button is part of the dedup key, so a fast STOP still closes.

    A user who stops the shade 1.4 s after starting it presses inside the
    repeat window. Keying the filter on the remote and channels alone would
    read that STOP as another copy of the DOWN and never close the run.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0, bridge_id="bridge-a")
    measurement = run.offer_payload(rx("STOP", 2_400), 101.4, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.direction == "DOWN"
    assert measurement.measured_seconds == 1.4


def test_a_signature_separates_remotes_and_channels_not_just_buttons() -> None:
    """Two presses collapse only when they are copies of ONE press on air.

    The measure run gates on a calibrated identity before the filter sees a
    frame, so within one run the signature can only vary by button. The Learn
    wizard has no such gate -- it is the reason the key carries the remote and
    the channel set as well.
    """
    base = press_signature(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "DOWN")
    assert press_signature(TEST_PREFIX, TEST_REMOTE_ID, (2, 1), "DOWN") == base, (
        "one selector, whatever order the channels decode in"
    )
    assert press_signature(UNTABLED_PREFIX, TEST_REMOTE_ID, (1, 2), "DOWN") != base
    assert press_signature(TEST_PREFIX, UNTABLED_REMOTE_ID, (1, 2), "DOWN") != base
    assert press_signature(TEST_PREFIX, TEST_REMOTE_ID, (1, 3), "DOWN") != base
    assert press_signature(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP") != base


def test_a_stale_stop_copy_cannot_close_a_newly_reopened_run() -> None:
    """A lagging copy of an OLD stop must not close the run that replaced it.

    After the close, ``started`` is None, so a duplicate STOP arriving with
    nothing open is refused by the run machine whatever the repeat filter says.
    The filter's real job is this: a run too fast to store closes, the user
    presses again, and a bridge under backpressure then delivers its copy of
    the FIRST stop into the second run -- which would end it after the fraction
    of a second between the two presses and hand the wizard nothing again.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0, bridge_id="bridge-a")
    assert run.offer_payload(rx("STOP", 1_500), 100.5, bridge_id="bridge-a") is None, (
        "half a second is too fast to store, so the run closes with no measurement"
    )
    assert run.offer_payload(rx("DOWN", 2_000), 101.0, bridge_id="bridge-a") is None
    assert run.started is not None, "the re-press must open a second run"
    assert run.offer_payload(rx("STOP", 900_000), 101.6, bridge_id="bridge-b") is None
    assert run.started is not None, "the lagging copy of the FIRST stop closes nothing"
    measurement = run.offer_payload(rx("STOP", 16_000), 115.0, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.measured_seconds == 14.0, "the second run is timed from its own press"


def test_a_superseded_directions_late_copy_never_reanchors_the_run() -> None:
    """A direction the run has ALREADY been anchored on cannot anchor it again.

    The user starts the shade DOWN, changes their mind and presses UP, and a
    bridge lagging by seconds -- the observed envelope -- then delivers its copy
    of the DOWN burst that UP replaced. Nothing in the frames separates that
    copy from a fresh press, so re-anchoring on it times a run the shade never
    made and stores it under the WRONG DIRECTION (#57).
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0, bridge_id="bridge-a")
    assert run.offer_payload(rx("UP", 3_000), 102.0, bridge_id="bridge-a") is None
    assert run.started is not None
    assert run.started.button == "UP", "the mind-change restart is the one the wizard needs"
    assert run.offer_payload(rx("DOWN", 600_000), 102.5, bridge_id="bridge-b") is None
    measurement = run.offer_payload(rx("STOP", 16_000), 115.0, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.direction == "UP", "the shade ran UP; the stale DOWN copy is not a run"
    assert measurement.measured_seconds == 13.0


def test_both_directions_bursts_arriving_interleaved_keep_the_second_press() -> None:
    """The fleet delivers both bursts mixed together; the run stays on the last.

    Every bridge that heard the abandoned DOWN press and the UP press that
    replaced it delivers its own copies, so the two bursts arrive interleaved
    and the last frame in is a DOWN. The run must still be the UP one, anchored
    at the FIRST copy of that press.
    """
    run = run_for()
    arrivals = (
        ("DOWN", "bridge-a", 100.0),
        ("DOWN", "bridge-b", 100.1),
        ("UP", "bridge-a", 100.6),
        ("DOWN", "bridge-c", 100.7),
        ("UP", "bridge-b", 100.8),
        ("DOWN", "bridge-b", 101.4),
        ("UP", "bridge-c", 102.2),
        ("DOWN", "bridge-c", 103.9),
    )
    for button, bridge, received_at in arrivals:
        assert (
            run.offer_payload(
                rx(button, round(received_at * 1_000)),
                received_at,
                bridge_id=bridge,
            )
            is None
        )
    assert run.started is not None
    assert run.started.button == "UP"
    assert run.started.received_at_monotonic == 100.6, "anchored at the first UP copy"
    measurement = run.offer_payload(rx("STOP", 115_000), 115.0, bridge_id="bridge-a")
    assert measurement is not None
    assert measurement.direction == "UP"
    assert measurement.measured_seconds == 14.4


def test_one_foreign_press_heard_by_the_fleet_leaves_room_for_the_next() -> None:
    """Fleet copies of ONE press must not fill the mismatch list (#57).

    Each bridge reports the pulse widths it measured, so their captures of one
    press differ byte for byte while decoding identically. Deduplicating on the
    raw frame therefore counted every bridge's copy as a new press: nine copies
    exhausted the cap, the NEXT remote to press was dropped, and the timeout
    screen offered one-click adoption of the only remote it could still see.
    """
    run = run_for()
    for index in range(_HEARD_CAP + 1):
        heard_by_one_bridge = {
            "frame": b1_frame(
                UNTABLED_PREFIX,
                UNTABLED_REMOTE_ID,
                (1, 2),
                "DOWN",
                UNTABLED_BASES,
                buckets=as_measured_by(index),
            ),
            "t": 1_000 + index,
            "boot": 7,
        }
        assert (
            run.offer_payload(heard_by_one_bridge, 100.0 + index * 0.05, f"bridge-{index}") is None
        )
    assert len(run.heard) == 1, "one press, however many bridges measured it"

    second_remote = {
        "frame": b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (3,), "UP", TEST_BASES),
        "t": 2_000,
        "boot": 7,
    }
    assert run.offer_payload(second_remote, 101.0, "bridge-0") is None
    assert [(press.prefix, press.remote_id) for press in run.heard] == [
        (UNTABLED_PREFIX, UNTABLED_REMOTE_ID),
        (TEST_PREFIX, TEST_REMOTE_ID),
    ], "the remote that pressed next is still there to be named"
