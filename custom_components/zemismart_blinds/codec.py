"""Codec for the live-validated AOK/Zemismart 433.92 MHz roller-blind protocol.

The protocol is used by AOK OEM tubular motors, most commonly sold under the
Zemismart brand (other resellers of AOK motors are expected to be compatible).
The 64-bit payload is ``prefix:24 | remote_id:8 | channel:16 | command:16``.
Portisch B0 frames encode it as constant-period OOK PWM followed by the OEM's
two trailing bits.  The formulas in this module deliberately mirror
``PROTOCOL.md``; changing them requires live protocol validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict

if TYPE_CHECKING:
    from collections.abc import Iterable

BUTTONS: Final = ("UP", "DOWN", "STOP", "TRAILER")

_ACTION_COMMAND_HIGH: Final[dict[str, int]] = {
    "UP": 0xF4,
    "DOWN": 0xBC,
    "STOP": 0xDC,
}
_ACTION_LOW_FROM_UP: Final[dict[str, int]] = {
    "UP": 0,
    "DOWN": -0x38,
    "STOP": -0x18,
}
_CALIBRATION_CHANNELS: Final = (1, 2, 3, 4, 5, 6)

DEFAULT_BUCKETS: Final = "1414026C01181414"
_FRAME_BITS: Final = 64
_MAX_PAYLOAD: Final = (1 << _FRAME_BITS) - 1
_MAX_B0_FRAME_BYTES: Final = 260
# Mirrors the firmware's per-handoff limits so a frame the integration accepts
# is never rejected at the bridge: embedded hardware repeat 1..16, and at most
# two seconds of requested RF airtime (buckets are 16-bit microsecond values
# and every pulse nibble spends one bucket, multiplied by the embedded repeat;
# a real AOK frame runs ~550 ms at the controller's embedded repeat of 8).
_MAX_B0_EMBEDDED_REPEAT: Final = 0x10
_MAX_B0_AIRTIME_US: Final = 2_000_000
_SYNC_MIN_US: Final = 1_000
_BIT_MAX_US: Final = 1_000
_SHORT_MAX_US: Final = 450
_CAPTURE_PADDING_PULSES: Final = 2


def _require_uint(value: object, bits: int, name: str) -> int:
    """Validate one unsigned fixed-width protocol integer (booleans rejected)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << bits):
        msg = f"{name} must be an unsigned {bits}-bit integer"
        raise ValueError(msg)
    return value


class DecodedFrame(TypedDict):
    """Fields decoded from a Zemismart payload."""

    prefix: int
    remote_id: int
    channel: int
    chans: list[int]
    cmd: int


@dataclass(frozen=True, slots=True)
class CommandBases:
    """Per-remote command bases for the three actions and optional OEM trailer."""

    up: int
    down: int
    stop: int
    trailer: int | None = None

    def __post_init__(self) -> None:
        """Validate every configured command as an unsigned 16-bit integer."""
        for name, value in (
            ("up", self.up),
            ("down", self.down),
            ("stop", self.stop),
            ("trailer", self.trailer),
        ):
            if value is not None:
                _require_uint(value, 16, f"{name} base")

    def base(self, button: str) -> int:
        """Return the calibrated base for one button."""
        values = {
            "UP": self.up,
            "DOWN": self.down,
            "STOP": self.stop,
            "TRAILER": self.trailer,
        }
        try:
            value = values[button]
        except KeyError as exc:
            msg = f"button must be one of {', '.join(values)}"
            raise ValueError(msg) from exc
        if value is None:
            msg = f"button {button} is not calibrated for this remote"
            raise ValueError(msg)
        return value


def validate_channels(channels: Iterable[int], *, allow_empty: bool = False) -> tuple[int, ...]:
    """Validate and materialize a channel collection once.

    The single definition of the 1..16, unique, (optionally) non-empty
    channel-set rule, shared by the codec, config parsing, and target-key
    construction.
    """
    materialized = tuple(channels)
    if not materialized and not allow_empty:
        msg = "channel set must not be empty"
        raise ValueError(msg)
    if any(channel < 1 or channel > 16 for channel in materialized):
        msg = "channel values must be in the range 1..16"
        raise ValueError(msg)
    if len(set(materialized)) != len(materialized):
        msg = "channel values must be unique"
        raise ValueError(msg)
    return materialized


def _signed8(value: int) -> int:
    """Convert an integer to its signed eight-bit representation."""
    return ((value + 128) % 256) - 128


def group_offset(channels: Iterable[int]) -> int:
    """Return the signed-eight command offset for a channel set.

    This is exactly ``signed8(2 + SUM(1 << ((ch - 1) % 8)))``.  The reference
    codec defines the empty-set offset as zero, although empty groups cannot be
    transmitted by :func:`make_payload`.
    """
    normalized = validate_channels(channels, allow_empty=True)
    if not normalized:
        return 0
    return _signed8(2 + sum(1 << ((channel - 1) % 8) for channel in normalized))


def _recover_base(remote_id: int, chans: Iterable[int], cmd: int) -> int:
    """Recover a calibrated base using the inverse protocol command formula."""
    return (cmd - remote_id + group_offset(chans)) & 0xFFFF


def infer_action_button(chans: Iterable[int], cmd: int) -> str | None:
    """Infer an action from a captured command after channel normalization."""
    normalized = validate_channels(chans, allow_empty=False)
    _require_uint(cmd, 16, "command")
    calibration_command = (
        cmd + group_offset(normalized) - group_offset(_CALIBRATION_CHANNELS)
    ) & 0xFFFF
    for button, command_high in _ACTION_COMMAND_HIGH.items():
        if calibration_command >> 8 == command_high:
            return button
    return None


def channel_field(channels: Iterable[int]) -> int:
    """Encode channels as the protocol's 16-bit active-low bit field."""
    normalized = validate_channels(channels, allow_empty=True)
    mask = 0
    for channel in normalized:
        mask |= 1 << ((channel + 7) % 16)
    return 0xFFFF ^ mask


def derive_base(
    ref_channels: Iterable[int],
    button: str,
    ref_cmd: int,
    remote_id: int,
) -> int:
    """Derive one button base from a captured command reference."""
    normalized = validate_channels(ref_channels, allow_empty=False)
    if button not in _ACTION_COMMAND_HIGH:
        msg = f"reference button must be one of {', '.join(_ACTION_COMMAND_HIGH)}"
        raise ValueError(msg)
    _require_uint(ref_cmd, 16, "reference command")
    _require_uint(remote_id, 8, "remote_id")
    return _recover_base(remote_id, normalized, ref_cmd)


def derive_bases(
    ref_channels: Iterable[int],
    button: str,
    ref_cmd: int,
    remote_id: int,
) -> CommandBases:
    """Derive all action bases from one labeled UP, DOWN, or STOP reference."""
    normalized = validate_channels(ref_channels, allow_empty=False)
    derive_base(normalized, button, ref_cmd, remote_id)
    calibration_command = (
        ref_cmd + group_offset(normalized) - group_offset(_CALIBRATION_CHANNELS)
    ) & 0xFFFF
    if calibration_command >> 8 != _ACTION_COMMAND_HIGH[button]:
        msg = f"normalized reference command does not have the {button} opcode byte"
        raise ValueError(msg)
    up_low = ((calibration_command & 0xFF) - _ACTION_LOW_FROM_UP[button]) & 0xFF

    def button_base(action: str) -> int:
        command = (_ACTION_COMMAND_HIGH[action] << 8) | (
            (up_low + _ACTION_LOW_FROM_UP[action]) & 0xFF
        )
        return derive_base(_CALIBRATION_CHANNELS, action, command, remote_id)

    return CommandBases(
        up=button_base("UP"),
        down=button_base("DOWN"),
        stop=button_base("STOP"),
    )


def derive_bases_from_base(button: str, base: int, remote_id: int) -> CommandBases:
    """Complete all action bases from one labeled per-remote action base."""
    _require_uint(base, 16, "command base")
    reference_channels = (1,)
    reference_command = (base + remote_id - group_offset(reference_channels)) & 0xFFFF
    return derive_bases(reference_channels, button, reference_command, remote_id)


def synthesize_bases(remote_id: int, up_low: int) -> CommandBases:
    """Create a complete calibration for a new virtual remote identity.

    A virtual remote is never captured over the air, so any internally
    consistent command set works: the motor learns whatever frames the virtual
    remote transmits during pairing.  The synthesized set uses the protocol's
    observed per-action opcode bytes with a caller-chosen UP low byte, exactly
    as :func:`derive_bases` would derive them from a capture.
    """
    _require_uint(up_low, 8, "up_low")
    reference_command = (_ACTION_COMMAND_HIGH["UP"] << 8) | up_low
    return derive_bases(_CALIBRATION_CHANNELS, "UP", reference_command, remote_id)


def make_payload(
    prefix: int,
    remote_id: int,
    channels: Iterable[int],
    button: str,
    *,
    bases: CommandBases,
) -> int:
    """Build a 64-bit payload for one channel or an arbitrary group."""
    _require_uint(prefix, 24, "prefix")
    _require_uint(remote_id, 8, "remote_id")
    normalized = validate_channels(channels, allow_empty=False)
    if button not in BUTTONS:
        msg = f"button must be one of {', '.join(BUTTONS)}"
        raise ValueError(msg)
    base = bases.base(button)

    command = (base + remote_id - group_offset(normalized)) & 0xFFFF
    return (prefix << 40) | (remote_id << 32) | (channel_field(normalized) << 16) | command


def encode_b0(payload64: int, buckets: str = DEFAULT_BUCKETS) -> str:
    """Encode a 64-bit payload as an uppercase Portisch B0 raw frame."""
    if not 0 <= payload64 <= _MAX_PAYLOAD:
        msg = "payload must be an unsigned 64-bit integer"
        raise ValueError(msg)
    bucket_hex = "".join(buckets.split()).upper()
    try:
        bucket_bytes = bytes.fromhex(bucket_hex)
    except ValueError as exc:
        msg = "buckets must be hexadecimal"
        raise ValueError(msg) from exc
    if len(bucket_bytes) != 8:
        msg = "buckets must contain exactly four 16-bit values"
        raise ValueError(msg)

    # Constant-period PWM: bit 0 = long-high/short-low, bit 1 =
    # short-high/long-low.  OEM frames include the final [1, 0] pair.
    bits = [
        *((payload64 >> (63 - index)) & 1 for index in range(_FRAME_BITS)),
        1,
        0,
    ]
    data_parts = ["38"]
    previous = 1
    for bit in bits:
        data_parts.append("1" if previous == 1 else "2")
        data_parts.append("A" if bit == 1 else "9")
        previous = bit

    body = f"0408{bucket_hex}{''.join(data_parts)}"
    return f"AAB0{len(body) // 2:02X}{body}55"


def _clean_hex(frame: str) -> str:
    """Normalize a possibly space-delimited bridge frame."""
    normalized = "".join(frame.split()).upper()
    if not normalized:
        msg = "B0 frame is empty"
        raise ValueError(msg)
    if len(normalized) % 2:
        msg = "frame must contain complete hex bytes"
        raise ValueError(msg)
    try:
        bytes.fromhex(normalized)
    except ValueError as exc:
        msg = "frame must contain only hex bytes"
        raise ValueError(msg) from exc
    return normalized


def _bucket_data(
    frame: str,
    *,
    bucket_count: int,
    bucket_start: int,
    data_end: int,
) -> tuple[list[int], str]:
    """Extract and bounds-check a Portisch bucket table and pulse data."""
    if not 1 <= bucket_count <= 8:
        msg = "bucket count must be in the range 1..8"
        raise ValueError(msg)
    data_start = bucket_start + bucket_count * 4
    if data_start > data_end:
        msg = "frame bucket table is truncated"
        raise ValueError(msg)
    buckets = [int(frame[offset : offset + 4], 16) for offset in range(bucket_start, data_start, 4)]
    pulse_data = frame[data_start:data_end]
    for pulse_nibble in pulse_data:
        if int(pulse_nibble, 16) & 0x07 >= bucket_count:
            msg = "frame references an undefined bucket"
            raise ValueError(msg)
    return buckets, pulse_data


def _b0_parts(frame: str) -> tuple[list[int], str]:
    """Validate and extract one exact Portisch B0 envelope."""
    if not frame.startswith("AAB0"):
        msg = "B0 frame must start with the AAB0 marker"
        raise ValueError(msg)
    if len(frame) // 2 > _MAX_B0_FRAME_BYTES:
        msg = "B0 frame exceeds the maximum size"
        raise ValueError(msg)
    if len(frame) < 10:
        msg = "B0 frame header is truncated"
        raise ValueError(msg)
    body_length = int(frame[4:6], 16)
    body_start = 6
    body_end = body_start + body_length * 2
    if len(frame) != body_end + 2:
        msg = "B0 frame declared length is invalid"
        raise ValueError(msg)
    if frame[body_end:] != "55":
        msg = "B0 frame trailer is invalid"
        raise ValueError(msg)
    if body_length < 2:
        msg = "B0 frame body is too short for its bucket header"
        raise ValueError(msg)
    bucket_count = int(frame[body_start : body_start + 2], 16)
    embedded_repeat = int(frame[body_start + 2 : body_start + 4], 16)
    if not 1 <= embedded_repeat <= _MAX_B0_EMBEDDED_REPEAT:
        msg = "B0 frame embedded repeat count is out of range"
        raise ValueError(msg)
    buckets, pulse_data = _bucket_data(
        frame,
        bucket_count=bucket_count,
        bucket_start=body_start + 4,
        data_end=body_end,
    )
    airtime_us = sum(buckets[int(nibble, 16) & 0x07] for nibble in pulse_data)
    if airtime_us * embedded_repeat > _MAX_B0_AIRTIME_US:
        msg = "B0 frame requested airtime exceeds the limit"
        raise ValueError(msg)
    return buckets, pulse_data


def validate_b0_frame(frame: str) -> str:
    """Return one normalized, transport-safe Portisch B0 frame."""
    normalized = _clean_hex(frame)
    _b0_parts(normalized)
    return normalized


def _raw_parts(frame: str) -> tuple[list[int], str, bool]:
    """Extract Portisch bucket values and pulse nibbles from B0 or B1."""
    if frame.startswith("AAB0"):
        buckets, pulse_data = _b0_parts(frame)
        return buckets, pulse_data, False
    if not frame.startswith("AAB1"):
        msg = "frame does not contain an AAB0 or AAB1 marker"
        raise ValueError(msg)

    # B1 is the Portisch receive form used by bucket sniffing. It has no B0
    # body-length/header byte: the bucket count immediately follows AAB1.
    # Bound it like B0 so a pasted oversized capture cannot burn event-loop
    # CPU/memory in the pulse decoder.
    if len(frame) // 2 > _MAX_B0_FRAME_BYTES:
        msg = "B1 frame exceeds the maximum size"
        raise ValueError(msg)
    if len(frame) < 8:
        msg = "B1 frame header is truncated"
        raise ValueError(msg)
    if not frame.endswith("55"):
        msg = "B1 frame trailer is missing"
        raise ValueError(msg)
    bucket_count = int(frame[4:6], 16)
    buckets, pulse_data = _bucket_data(
        frame,
        bucket_count=bucket_count,
        bucket_start=6,
        data_end=len(frame) - 2,
    )
    return buckets, pulse_data, True


def _pulse_bits(
    buckets: list[int],
    pulse_data: str,
    *,
    capture: bool,
    allow_missing_trailer: bool = False,
) -> list[int]:
    """Validate the RF envelope and return its 64 payload bits."""
    pulses = [(int(nibble, 16) >> 3, buckets[int(nibble, 16) & 0x07]) for nibble in pulse_data]
    sync_index = next(
        (
            index
            for index in range(len(pulses) - 1)
            if pulses[index][0] == 0
            and pulses[index + 1][0] == 1
            and pulses[index][1] >= _SYNC_MIN_US
            and pulses[index + 1][1] >= _SYNC_MIN_US
        ),
        None,
    )
    if sync_index is None:
        msg = "frame does not contain a valid low/high sync pair"
        raise ValueError(msg)
    leading = pulses[:sync_index]
    if leading and (
        not capture
        or len(leading) > _CAPTURE_PADDING_PULSES
        or any(duration < _SYNC_MIN_US for _, duration in leading)
    ):
        msg = "frame contains invalid capture padding before sync"
        raise ValueError(msg)

    encoded = pulses[sync_index + 2 :]
    payload_pulses = _FRAME_BITS * 2
    expected_pulses = (_FRAME_BITS + 2) * 2
    if allow_missing_trailer and len(encoded) in {payload_pulses, payload_pulses + 2}:
        expected_pulses = len(encoded)
    if len(encoded) < expected_pulses:
        msg = "frame must contain exactly 64 payload bits plus the two-bit trailer"
        raise ValueError(msg)
    trailing = encoded[expected_pulses:]
    if trailing and (
        not capture
        or len(trailing) > _CAPTURE_PADDING_PULSES
        or any(duration < _SYNC_MIN_US for _, duration in trailing)
    ):
        msg = "frame must contain exactly 64 payload bits plus the two-bit trailer"
        raise ValueError(msg)

    bits: list[int] = []
    previous = 1
    for index in range(0, expected_pulses, 2):
        low_polarity, low_duration = encoded[index]
        high_polarity, high_duration = encoded[index + 1]
        if low_polarity != 0 or high_polarity != 1:
            msg = "frame pulse polarity must be paired low then high"
            raise ValueError(msg)
        if low_duration >= _BIT_MAX_US or high_duration >= _BIT_MAX_US:
            msg = "frame bit pulse duration overlaps the sync envelope"
            raise ValueError(msg)
        low_previous = 0 if low_duration < _SHORT_MAX_US else 1
        if low_previous != previous:
            msg = "frame pulse pairing does not preserve the constant period"
            raise ValueError(msg)
        bit = 1 if high_duration < _SHORT_MAX_US else 0
        bits.append(bit)
        previous = bit

    trailer_bits = bits[_FRAME_BITS:]
    # Reference captures from legacy Hubitat exports truncate the OEM [1, 0]
    # trailer either entirely ([]) or after its constant-period low half,
    # which this decoder resolves as a single 0 bit ([0]). Those are the two
    # truncations observed in real captures; a lone [1] never occurs because
    # the trailer's 1-bit cannot terminate a capture without its paired low.
    valid_trailer = trailer_bits == [1, 0] or (allow_missing_trailer and trailer_bits in ([], [0]))
    if not valid_trailer:
        msg = "frame must end with the required [1, 0] trailer bits"
        raise ValueError(msg)
    return bits[:_FRAME_BITS]


def _decode_frame(hexstr: str, *, allow_missing_trailer: bool) -> DecodedFrame:
    """Decode one validated transport or calibration-reference frame."""
    frame = _clean_hex(hexstr)
    buckets, pulse_data, capture = _raw_parts(frame)
    bits = _pulse_bits(
        buckets,
        pulse_data,
        capture=capture,
        allow_missing_trailer=allow_missing_trailer,
    )
    value = 0
    for bit in bits:
        value = (value << 1) | bit

    encoded_channels = (value >> 16) & 0xFFFF
    cleared = 0xFFFF ^ encoded_channels
    channels = sorted(((bit - 7) % 16) or 16 for bit in range(16) if cleared & (1 << bit))
    return {
        "prefix": (value >> 40) & 0xFFFFFF,
        "remote_id": (value >> 32) & 0xFF,
        "channel": encoded_channels,
        "chans": channels,
        "cmd": value & 0xFFFF,
    }


def decode_b0(hexstr: str) -> DecodedFrame:
    """Decode a complete Portisch B0/B1 frame into its Zemismart payload fields."""
    return _decode_frame(hexstr, allow_missing_trailer=False)


def decode_reference_b0(hexstr: str) -> DecodedFrame:
    """Decode a reference, including legacy B0 frames with a truncated trailer."""
    return _decode_frame(hexstr, allow_missing_trailer=True)
