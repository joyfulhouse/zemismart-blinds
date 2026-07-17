"""Exhaustive tests for the AOK/Zemismart 433.92 MHz codec.

The golden vectors in ``tests/synthetic.py`` were generated with the
hardware-validated codec for fabricated remote identities, so any change to
the protocol math fails byte-exactly without shipping real remote data.
"""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING

import pytest

from custom_components.zemismart_blinds.codec import (
    BUTTONS,
    CommandBases,
    channel_field,
    decode_b0,
    decode_reference_b0,
    decode_rx_capture,
    derive_base,
    derive_bases,
    derive_bases_from_base,
    encode_b0,
    group_offset,
    infer_action_button,
    make_payload,
    synthesize_bases,
    validate_b0_frame,
)
from tests.synthetic import (
    SYNTHETIC_REMOTES,
    TEST_ALL_UP_B0,
    TEST_ALL_UP_PAYLOAD,
    TEST_BASES,
    TEST_CH12_DOWN_B0,
    TEST_CH12_DOWN_PAYLOAD,
    TEST_CH12_UP_B0,
    TEST_CH12_UP_PAYLOAD,
    TEST_PREFIX,
    TEST_REMOTE_ID,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def nonempty_channel_groups() -> Iterable[tuple[int, ...]]:
    """Yield every non-empty subset of channels 1 through 6."""
    for size in range(1, 7):
        yield from combinations(range(1, 7), size)


def test_regenerates_stored_all_channel_golden_vector_byte_exact() -> None:
    """The codec must reproduce the stored ALL/UP golden vector byte-for-byte."""
    payload = make_payload(TEST_PREFIX, TEST_REMOTE_ID, range(1, 7), "UP", bases=TEST_BASES)

    assert payload == TEST_ALL_UP_PAYLOAD
    assert encode_b0(payload) == TEST_ALL_UP_B0


@pytest.mark.parametrize(
    ("button", "expected_payload", "expected_b0"),
    (
        ("UP", TEST_CH12_UP_PAYLOAD, TEST_CH12_UP_B0),
        ("DOWN", TEST_CH12_DOWN_PAYLOAD, TEST_CH12_DOWN_B0),
    ),
)
def test_channel_1_2_group_golden_vectors(
    button: str,
    expected_payload: int,
    expected_b0: str,
) -> None:
    """Grouped UP/DOWN frames stay byte-exact against the stored golden vectors."""
    payload = make_payload(TEST_PREFIX, TEST_REMOTE_ID, {1, 2}, button, bases=TEST_BASES)

    assert payload == expected_payload
    assert encode_b0(payload) == expected_b0


@pytest.mark.parametrize(("name", "prefix", "remote_id", "bases", "_expected"), SYNTHETIC_REMOTES)
def test_every_action_capture_derives_one_consistent_calibration(
    name: str,
    prefix: int,
    remote_id: int,
    bases: CommandBases,
    _expected: int,
) -> None:
    """Captured UP, DOWN, and STOP frames each reconstruct the same three bases."""
    del name
    calibrations = []
    for button in ("UP", "DOWN", "STOP"):
        # Synthesize the "capture" this remote would transmit on channel 1.
        raw = encode_b0(make_payload(prefix, remote_id, [1], button, bases=bases))
        decoded = decode_reference_b0(raw)
        calibration = derive_bases(
            decoded["chans"],
            button,
            decoded["cmd"],
            decoded["remote_id"],
        )
        calibrations.append(calibration)
        assert calibration.base(button) == derive_base(
            decoded["chans"],
            button,
            decoded["cmd"],
            decoded["remote_id"],
        )

    assert calibrations[0] == calibrations[1] == calibrations[2]
    assert calibrations[0] == CommandBases(bases.up, bases.down, bases.stop)


def test_reference_decoder_accepts_short_trailer_without_weakening_decode() -> None:
    """Calibration accepts a legacy capture with a truncated trailer; decode stays strict."""
    # Strip the final [1, 0] OEM trailer bits (two pulse pairs = 4 nibbles).
    full = TEST_CH12_UP_B0
    body = full[6:-2]
    short_body = body[:-4]
    short = f"AAB0{len(short_body) // 2:02X}{short_body}55"

    assert decode_reference_b0(short) == {
        "prefix": TEST_PREFIX,
        "remote_id": TEST_REMOTE_ID,
        "channel": 0xFCFF,
        "chans": [1, 2],
        "cmd": TEST_CH12_UP_PAYLOAD & 0xFFFF,
    }
    with pytest.raises(ValueError, match="64 payload bits"):
        decode_b0(short)


LIVE_OFFICE_B1_CAPTURES = (
    # Live Office-bridge captures of the physical 5cad7c:da remote's ALL
    # presses (2026-07-17). The remote transmits 64 payload bits plus a
    # trailer that captures as a single 0-read — one pair short of nominal.
    (
        "AAB10413EC026C012C143C38192A192A1A1A19292A192A192A1A192A192A1A1A1A1A19292A"
        "1A192A1A192A192A1A1929292929292A1A1A1A1A1A1A1A1A1A1A1A192A19292A192A1A1A19"
        "2A1A1955",
        0xF4BB,  # UP
    ),
    (
        "AAB10413EC0276012C144638192A192A1A1A19292A192A192A1A192A192A1A1A1A1A19292A"
        "1A192A1A192A192A1A1929292929292A1A1A1A1A1A1A1A1A192A1A1A1A19292A1929292929"
        "2A1A1955",
        0xBC83,  # DOWN
    ),
    (
        "AAB10413EC0276012C145038192A192A1A1A19292A192A192A1A192A192A1A1A1A1A19292A"
        "1A192A1A192A192A1A1929292929292A1A1A1A1A1A1A1A1A1A192A1A1A19292A192A192929"
        "2A1A1955",
        0xDCA3,  # STOP
    ),
)


@pytest.mark.parametrize(("raw", "cmd"), LIVE_OFFICE_B1_CAPTURES)
def test_rx_capture_decoder_accepts_live_oem_truncated_trailer(raw: str, cmd: int) -> None:
    """Live captures of a truncated-trailer OEM remote decode for RX use only."""
    assert decode_rx_capture(raw) == {
        "prefix": 0x5CAD7C,
        "remote_id": 0xDA,
        "channel": 0xC0FF,
        "chans": [1, 2, 3, 4, 5, 6],
        "cmd": cmd,
    }
    # Transport decoding stays strict: these frames are receive-only evidence.
    with pytest.raises(ValueError, match="64 payload bits"):
        decode_b0(raw)


def test_rx_capture_decoder_matches_strict_decode_on_full_frames() -> None:
    """The RX decoder is a strict superset: nominal frames decode identically."""
    assert decode_rx_capture(TEST_CH12_UP_B0) == decode_b0(TEST_CH12_UP_B0)
    assert decode_rx_capture(TEST_ALL_UP_B0) == decode_b0(TEST_ALL_UP_B0)


def test_all_channel_reference_derives_single_channel_command() -> None:
    """A captured ALL-channel command derives the correct single-channel payload."""
    _, prefix, remote_id, bases, expected_ch1 = SYNTHETIC_REMOTES[1]
    all_cmd = make_payload(prefix, remote_id, range(1, 7), "UP", bases=bases) & 0xFFFF

    derived = derive_bases(range(1, 7), "UP", all_cmd, remote_id)
    payload = make_payload(prefix, remote_id, [1], "UP", bases=derived)

    assert payload == expected_ch1


@pytest.mark.parametrize(
    ("channels", "command", "expected"),
    (
        ((1, 2, 3, 4, 5, 6), 0xF42B, "UP"),
        ((1, 2), 0xF467, "UP"),
        ((1, 2), 0xBD2F, "DOWN"),
        ((1, 2), 0xDC4F, "STOP"),
        ((1,), 0xF53C, "UP"),
        ((1, 2), 0xAA45, None),
    ),
)
def test_infer_action_button_normalizes_golden_commands(
    channels: tuple[int, ...],
    command: int,
    expected: str | None,
) -> None:
    """Captured commands identify their action after channel normalization."""
    assert infer_action_button(channels, command) == expected


def test_uncalibrated_trailer_is_rejected() -> None:
    """Action-only remote calibration must not invent an OEM trailer command."""
    bases = synthesize_bases(0x7D, 0x9C)

    with pytest.raises(ValueError, match=r"TRAILER.*not calibrated"):
        make_payload(0x654321, 0x7D, [1], "TRAILER", bases=bases)


@pytest.mark.parametrize(("name", "prefix", "remote_id", "expected", "_payload"), SYNTHETIC_REMOTES)
@pytest.mark.parametrize("button", ("UP", "DOWN", "STOP"))
def test_every_direct_base_completes_the_same_action_calibration(
    name: str,
    prefix: int,
    remote_id: int,
    expected: CommandBases,
    _payload: int,
    button: str,
) -> None:
    """Any one stored action base reconstructs all three bases despite 16-bit carries."""
    del name, prefix
    actual = derive_bases_from_base(button, expected.base(button), remote_id)

    assert actual == CommandBases(expected.up, expected.down, expected.stop)


def test_arbitrary_channel_capture_normalizes_before_action_derivation() -> None:
    """A captured action stays derivable when its command crossed an opcode byte."""
    _, prefix, remote_id, expected, _ = SYNTHETIC_REMOTES[3]
    command = make_payload(prefix, remote_id, [1], "UP", bases=expected) & 0xFFFF

    # The channel-1 command carries into 0xF5xx while the UP opcode byte is 0xF4.
    assert command == 0xF53C
    assert derive_bases([1], "UP", command, remote_id) == CommandBases(
        expected.up,
        expected.down,
        expected.stop,
    )


def test_synthesized_bases_round_trip_through_direct_base_entry() -> None:
    """A virtual remote's synthesized calibration survives the manual-flow base path."""
    bases = synthesize_bases(0x42, 0x2B)

    assert bases == CommandBases(TEST_BASES.up, TEST_BASES.down, TEST_BASES.stop)
    assert derive_bases_from_base("UP", bases.up, 0x42) == bases


@pytest.mark.parametrize("up_low", (-1, 0x100, True))
def test_synthesize_bases_rejects_invalid_low_byte(up_low: object) -> None:
    """The synthesized-calibration helper validates its seed byte."""
    with pytest.raises(ValueError, match="up_low"):
        synthesize_bases(0x42, up_low)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("channel", "expected_field", "expected_up", "expected_down"),
    (
        (1, 0xFEFF, 0xF469, 0xBD31),
        (2, 0xFDFF, 0xF468, 0xBD30),
        (3, 0xFBFF, 0xF466, 0xBD2E),
        (4, 0xF7FF, 0xF462, 0xBD2A),
        (5, 0xEFFF, 0xF45A, 0xBD22),
        (6, 0xDFFF, 0xF44A, 0xBD12),
    ),
)
def test_all_single_channels(
    channel: int,
    expected_field: int,
    expected_up: int,
    expected_down: int,
) -> None:
    """Every single-channel field and channel-dependent command is exact."""
    assert channel_field({channel}) == expected_field
    assert make_payload(TEST_PREFIX, TEST_REMOTE_ID, {channel}, "UP", bases=TEST_BASES) == (
        TEST_PREFIX << 40 | TEST_REMOTE_ID << 32 | expected_field << 16 | expected_up
    )
    assert make_payload(TEST_PREFIX, TEST_REMOTE_ID, {channel}, "DOWN", bases=TEST_BASES) & (
        0xFFFF
    ) == (expected_down)


@pytest.mark.parametrize(("name", "prefix", "remote_id", "bases", "expected"), SYNTHETIC_REMOTES)
def test_all_synthetic_remotes(
    name: str,
    prefix: int,
    remote_id: int,
    bases: CommandBases,
    expected: int,
) -> None:
    """Every synthetic remote identity generates its independently stored payload."""
    del name
    payload = make_payload(prefix, remote_id, {1}, "UP", bases=bases)

    assert payload == expected
    assert decode_b0(encode_b0(payload)) == {
        "prefix": prefix,
        "remote_id": remote_id,
        "channel": 0xFEFF,
        "chans": [1],
        "cmd": expected & 0xFFFF,
    }


@pytest.mark.parametrize("channels", tuple(nonempty_channel_groups()))
@pytest.mark.parametrize("button", BUTTONS)
def test_every_group_and_button_round_trips(channels: tuple[int, ...], button: str) -> None:
    """Every arbitrary subgroup of channels 1-6 round-trips for every button."""
    payload = make_payload(TEST_PREFIX, TEST_REMOTE_ID, channels, button, bases=TEST_BASES)
    decoded = decode_b0(encode_b0(payload))

    assert decoded == {
        "prefix": TEST_PREFIX,
        "remote_id": TEST_REMOTE_ID,
        "channel": channel_field(channels),
        "chans": list(channels),
        "cmd": payload & 0xFFFF,
    }


@pytest.mark.parametrize("channels", ((7,), (16,), (1, 9, 16), tuple(range(1, 17))))
def test_high_channels_round_trip(channels: tuple[int, ...]) -> None:
    """Channels 7 through 16 encode and decode with the same exact math."""
    payload = make_payload(TEST_PREFIX, TEST_REMOTE_ID, channels, "UP", bases=TEST_BASES)
    decoded = decode_b0(encode_b0(payload))

    assert decoded["chans"] == sorted(channels)
    assert decoded["channel"] == channel_field(channels)


@pytest.mark.parametrize(
    ("channels", "expected"),
    (
        ((), 0),
        ((1,), 3),
        ((1, 2, 3, 4, 5, 6), 65),
        ((1, 3, 4, 5, 6, 7), 127),
        ((2, 3, 4, 5, 6, 7), -128),
        ((1, 2, 3, 4, 5, 6, 7), -127),
        ((8,), -126),
        ((1, 2, 3, 4, 5, 6, 7, 8), 1),
    ),
)
def test_signed_8_group_offset_edges(channels: tuple[int, ...], expected: int) -> None:
    """Offset conversion has exact two's-complement behavior at both signed-8 boundaries."""
    assert group_offset(channels) == expected


def test_decoder_accepts_spaced_lowercase_frame() -> None:
    """Captured strings can include spaces and lower-case hexadecimal."""
    spaced = " ".join(
        TEST_CH12_UP_B0.lower()[index : index + 2] for index in range(0, len(TEST_CH12_UP_B0), 2)
    )

    assert decode_b0(spaced)["chans"] == [1, 2]


def test_decoder_accepts_real_b1_shape_with_documented_capture_padding() -> None:
    """A B1 capture may retain one idle low/high pair after the OEM trailer."""
    body = TEST_CH12_UP_B0[6:-2]
    b1_capture = f"AAB1{body[:2]}{body[4:]}3855"

    assert decode_b0(b1_capture)["chans"] == [1, 2]


@pytest.mark.parametrize(
    ("mutate_at", "replacement", "message"),
    (
        (28, "9", "polarity"),
        (-3, "A", "trailer"),
    ),
)
def test_decoder_rejects_mutated_envelope(
    mutate_at: int,
    replacement: str,
    message: str,
) -> None:
    """Valid payload bytes cannot excuse invalid pulse polarity or OEM trailer bits."""
    frame = list(TEST_CH12_UP_B0)
    frame[mutate_at] = replacement

    with pytest.raises(ValueError, match=message):
        decode_b0("".join(frame))


def test_decoder_rejects_more_than_64_payload_bits() -> None:
    """An extra valid-looking pulse pair cannot be silently ignored as a 65th payload bit."""
    body = TEST_CH12_UP_B0[6:-2]
    extended_body = f"{body[:-4]}1A{body[-4:]}"
    frame = f"AAB0{len(extended_body) // 2:02X}{extended_body}55"

    with pytest.raises(ValueError, match="exactly 64"):
        decode_b0(frame)


@pytest.mark.parametrize(
    "frame",
    (
        "AAB0GG55",
        "AAB001055",
        "AAB002040855",
        "AAB00302000155",
        "AAB005010000011155",
        f"AAB0FF01{'0001' * 126}0055",
    ),
)
def test_strict_b0_input_validation_rejects_malformed_frames(frame: str) -> None:
    """The integration-facing validator pins hex, length, buckets, trailer, and size."""
    with pytest.raises(ValueError, match=r"hex|length|bucket|trailer|size|frame"):
        validate_b0_frame(frame)


def test_strict_b0_validation_mirrors_firmware_repeat_and_airtime_limits() -> None:
    """A frame the validator accepts is never rejected at the bridge.

    The firmware bounds the embedded Portisch hardware repeat to 1..16 and
    the total requested airtime (pulses x bucket microseconds x embedded
    repeat) to two seconds; the Python validator enforces the same limits.
    """
    repeat_ff = f"{TEST_CH12_UP_B0[:8]}FF{TEST_CH12_UP_B0[10:]}"
    with pytest.raises(ValueError, match="embedded repeat"):
        validate_b0_frame(repeat_ff)

    # One 0xFFFF-microsecond bucket, two pulses, embedded repeat 16: ~2.1 s.
    with pytest.raises(ValueError, match="airtime"):
        validate_b0_frame("AAB0050110FFFF0855")
    # The same frame at the controller's embedded repeat of 8 (~1 s) passes.
    frame = "AAB0050108FFFF0855"
    assert validate_b0_frame(frame) == frame


def test_strict_b0_input_validation_normalizes_a_frame() -> None:
    """Whitespace/case normalization retains the exact B0 bytes."""
    spaced = " ".join(
        TEST_CH12_UP_B0.lower()[index : index + 2] for index in range(0, len(TEST_CH12_UP_B0), 2)
    )

    assert validate_b0_frame(spaced) == TEST_CH12_UP_B0


@pytest.mark.parametrize(
    ("prefix", "remote_id", "channels", "button"),
    (
        (-1, 0, (1,), "UP"),
        (0x1000000, 0, (1,), "UP"),
        (0, -1, (1,), "UP"),
        (0, 0x100, (1,), "UP"),
        (0, 0, (0,), "UP"),
        (0, 0, (17,), "UP"),
        (0, 0, (1,), "LEFT"),
    ),
)
def test_make_payload_rejects_invalid_fields(
    prefix: int,
    remote_id: int,
    channels: tuple[int, ...],
    button: str,
) -> None:
    """Invalid protocol values fail early instead of silently producing a malformed frame."""
    with pytest.raises(ValueError, match=r"prefix|remote_id|channel|button"):
        make_payload(prefix, remote_id, channels, button, bases=TEST_BASES)


@pytest.mark.parametrize("payload", (-1, 1 << 64))
def test_encode_rejects_non_64_bit_payload(payload: int) -> None:
    """The encoder accepts exactly an unsigned 64-bit payload."""
    with pytest.raises(ValueError, match="64-bit"):
        encode_b0(payload)


@pytest.mark.parametrize("frame", ("", "AAB0", "not hex", "AAB0010055"))
def test_decode_rejects_malformed_frames(frame: str) -> None:
    """Malformed bridge traffic produces a useful value error."""
    with pytest.raises(ValueError, match=r"B0|hex|frame|bucket|payload"):
        decode_b0(frame)
