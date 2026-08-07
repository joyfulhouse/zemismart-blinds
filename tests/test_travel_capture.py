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
from custom_components.zemismart_blinds.config_models import RemoteIdentity
from custom_components.zemismart_blinds.travel_capture import identify_button
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
