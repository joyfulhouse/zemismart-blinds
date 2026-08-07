"""Measuring one cover's travel time from its own remote's presses.

The user runs the shade to a limit and presses STOP on arrival. The interval
between the direction frame and the STOP frame is the travel time. Everything
here is pure -- no MQTT, no Home Assistant -- so the state machine is testable
directly from payload dictionaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .codec import decode_rx_capture, derive_base

if TYPE_CHECKING:
    from .config_models import RemoteIdentity

__all__ = [
    "BUTTONS",
    "DIRECTIONS",
    "identify_button",
]

DIRECTIONS: Final = ("UP", "DOWN")
BUTTONS: Final = ("UP", "DOWN", "STOP")

_DECODE_ERRORS: Final = (KeyError, TypeError, ValueError)


def identify_button(
    identity: RemoteIdentity,
    channels: tuple[int, ...],
    frame: str,
) -> str | None:
    """Return which calibrated button a captured frame is, or None.

    Exact, not inferred. ``derive_base`` validates its ``button`` argument and
    then never uses it -- the recovery keeps the capture's opcode byte and
    inverts only the low byte -- so one physical frame yields one
    channel-normalized base whatever action is claimed, and that base can be
    compared against this remote's own calibration.

    Deliberately NOT ``infer_action_button``: that reads the opcode byte
    against a 10-sample empirical table which #26 proved wrong for a real
    remote in the field. The Learn wizard tolerates it because it runs BEFORE
    any calibration exists. This runs after, so it can be exact.

    Decoding is trailer-tolerant for the same reason the Learn path became so
    in #27: not every OEM remote puts the nominal ``[1, 0]`` trailer on air.
    """
    bases = identity.bases
    if bases is None:
        return None
    try:
        decoded = decode_rx_capture(frame)
    except _DECODE_ERRORS:
        return None
    if (decoded["prefix"], decoded["remote_id"]) != (identity.prefix, identity.remote_id):
        return None
    if tuple(decoded["chans"]) != channels:
        return None
    try:
        base = derive_base(decoded["chans"], "UP", decoded["cmd"], decoded["remote_id"])
    except _DECODE_ERRORS:
        return None
    for button in BUTTONS:
        if base == bases.base(button):
            return button
    return None
