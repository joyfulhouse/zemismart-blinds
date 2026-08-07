"""Measuring one cover's travel time from its own remote's presses.

The user runs the shade to a limit and presses STOP on arrival. The interval
between the direction frame and the STOP frame is the travel time. Everything
here is pure -- no MQTT, no Home Assistant -- so the state machine is testable
directly from payload dictionaries.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .codec import decode_rx_capture, derive_base
from .config_models import MAX_TRAVEL_SECONDS
from .const import MIN_MEASURED_SECONDS

if TYPE_CHECKING:
    from .config_models import RemoteIdentity

__all__ = [
    "BUTTONS",
    "DIRECTIONS",
    "TimedPress",
    "TravelMeasurement",
    "identify_button",
    "interval_seconds",
    "stored_value",
]

DIRECTIONS: Final = ("UP", "DOWN")
BUTTONS: Final = ("UP", "DOWN", "STOP")

_DECODE_ERRORS: Final = (KeyError, TypeError, ValueError)
_UINT32_MODULUS: Final = 1 << 32
_UINT32_HALF_RANGE: Final = _UINT32_MODULUS // 2
_MILLISECONDS_PER_SECOND: Final = 1_000.0


@dataclass(frozen=True, slots=True)
class TimedPress:
    """One accepted press: which button, and when the bridge heard it."""

    button: str
    boot: int | None
    bridge_millis: int | None
    received_at_monotonic: float


@dataclass(frozen=True, slots=True)
class TravelMeasurement:
    """One completed direction run, raw and as it will be stored."""

    direction: str
    measured_seconds: float
    stored_seconds: int


def interval_seconds(start: TimedPress, stop: TimedPress) -> float | None:
    """Return the run's duration, preferring the bridge's own clock.

    Both frames come from one bridge over one MQTT path, so subtracting their
    ``t`` values cancels broker and event-loop jitter outright. This is NOT a
    ``BridgeClock`` projection: that class places events on Home Assistant's
    timeline, and a run needs only an interval on the bridge's own.

    A bridge that rebooted mid-run restarted ``t`` near zero, which reads as a
    backwards delta and is rejected by the half-modulus guard even when the
    payload carries no ``boot`` to compare.
    """
    if start.bridge_millis is not None and stop.bridge_millis is not None:
        if start.boot is not None and stop.boot is not None and start.boot != stop.boot:
            return None
        delta = (stop.bridge_millis - start.bridge_millis) % _UINT32_MODULUS
        if delta >= _UINT32_HALF_RANGE:
            return None
        return delta / _MILLISECONDS_PER_SECOND
    elapsed = stop.received_at_monotonic - start.received_at_monotonic
    if not math.isfinite(elapsed) or elapsed < 0:
        return None
    return elapsed


def stored_value(seconds: float) -> int | None:
    """Round one measured interval up to the whole seconds we store.

    Ceiling errs long, and the user's reaction on the STOP press already errs
    long. For an open-loop model that is the safe direction: a generous travel
    time stalls the motor against its own limit switch, where a short one
    leaves "fully closed" visibly open.
    """
    if not math.isfinite(seconds) or seconds < MIN_MEASURED_SECONDS:
        return None
    value = math.ceil(seconds)
    if value > MAX_TRAVEL_SECONDS:
        return None
    return value


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
