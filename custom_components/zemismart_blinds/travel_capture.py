"""Measuring one cover's travel time from its own remote's presses.

The user runs the shade to a limit and presses STOP on arrival. The interval
between the direction frame and the STOP frame is the travel time. Everything
here is pure -- no MQTT, no Home Assistant -- so the state machine is testable
directly from payload dictionaries.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from .codec import decode_rx_capture, derive_base, infer_action_button
from .config_models import MAX_TRAVEL_SECONDS
from .const import (
    MIN_MEASURED_SECONDS,
    MQTT_RX_FIELD_BOOT,
    MQTT_RX_FIELD_FRAME,
    MQTT_RX_FIELD_T,
    TRAVEL_BURST_WINDOW_SECONDS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .codec import DecodedFrame
    from .config_models import RemoteIdentity

__all__ = [
    "BUTTONS",
    "DIRECTIONS",
    "HeardPress",
    "TimedPress",
    "TravelMeasurement",
    "TravelRun",
    "classify_frame",
    "identify_button",
    "interval_seconds",
    "stored_value",
]

_HEARD_CAP: Final = 8

DIRECTIONS: Final = ("UP", "DOWN")
BUTTONS: Final = ("UP", "DOWN", "STOP")

_LOGGER = logging.getLogger(__name__)

_DECODE_ERRORS: Final = (KeyError, TypeError, ValueError)
_UINT32_MODULUS: Final = 1 << 32
_UINT32_HALF_RANGE: Final = _UINT32_MODULUS // 2
_MILLISECONDS_PER_SECOND: Final = 1_000.0


@dataclass(frozen=True, slots=True)
class HeardPress:
    """A decodable press the matcher rejected, kept so timeouts can explain.

    ``button`` is ``infer_action_button``'s guess -- an empirical-table
    inference, not a calibrated match -- and is ``None`` for untabled
    opcodes. It exists so a mismatch heard during measurement can seed a
    replacement calibration the same way the Learn wizard would.
    """

    frame: str
    prefix: int
    remote_id: int
    channels: tuple[int, ...]
    command: int
    button: str | None


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
    """Return which calibrated button a captured frame is, or None."""
    return classify_frame(identity, channels, frame)[0]


def classify_frame(
    identity: RemoteIdentity,
    channels: tuple[int, ...],
    frame: str,
) -> tuple[str | None, HeardPress | None]:
    """Match one frame against the calibration, or explain the rejection.

    Returns ``(button, None)`` on a match; ``(None, HeardPress)`` when the
    frame is a real press that fails the identity or channel gate -- the two
    rejections a user can act on; ``(None, None)`` for everything else
    (undecodable input and this remote's own non-action trailer burst).

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
        return None, None
    try:
        decoded = decode_rx_capture(frame)
    except _DECODE_ERRORS:
        return None, None
    observed_channels = tuple(decoded["chans"])
    if (decoded["prefix"], decoded["remote_id"]) != (identity.prefix, identity.remote_id):
        _LOGGER.debug(
            "travel: ignoring a press from %06x:%02x while measuring %06x:%02x",
            decoded["prefix"],
            decoded["remote_id"],
            identity.prefix,
            identity.remote_id,
        )
        return None, _heard(frame, decoded, observed_channels)
    if observed_channels != channels:
        _LOGGER.debug(
            "travel: ignoring this remote's press on channels %s -- the cover being "
            "measured stores %s, and the remote's channel selector must match it exactly",
            decoded["chans"],
            list(channels),
        )
        return None, _heard(frame, decoded, observed_channels)
    try:
        base = derive_base(observed_channels, "UP", decoded["cmd"], decoded["remote_id"])
    except _DECODE_ERRORS:
        return None, None
    for button in BUTTONS:
        if base == bases.base(button):
            return button, None
    _LOGGER.debug(
        "travel: base 0x%04x matches none of this remote's calibrated actions "
        "(likely the OEM trailer burst)",
        base,
    )
    return None, None


def _heard(
    frame: str,
    decoded: DecodedFrame,
    channels: tuple[int, ...],
) -> HeardPress:
    """Record one rejected-but-real press for the timeout screen."""
    return HeardPress(
        frame=frame,
        prefix=decoded["prefix"],
        remote_id=decoded["remote_id"],
        channels=channels,
        command=decoded["cmd"],
        button=infer_action_button(channels, decoded["cmd"]),
    )


def _uint32(value: object) -> int | None:
    """Return a real uint32, rejecting booleans and coercions."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value < _UINT32_MODULUS:
        return None
    return value


@dataclass(slots=True)
class TravelRun:
    """One cover's measurement session for one direction.

    Edge-triggered: the first direction frame opens a run and the first STOP
    after it closes one. A fresh run is constructed per direction, so ``wanted``
    is set once at construction and never mutated.
    """

    identity: RemoteIdentity
    channels: tuple[int, ...]
    wanted: frozenset[str]
    started: TimedPress | None = None
    heard: list[HeardPress] = field(default_factory=list)

    def offer_payload(
        self,
        payload: Mapping[str, object],
        received_at_monotonic: float,
    ) -> TravelMeasurement | None:
        """Feed one RX payload in; return a measurement when a run closes."""
        frame = payload.get(MQTT_RX_FIELD_FRAME)
        if not isinstance(frame, str):
            return None
        button, mismatch = classify_frame(self.identity, self.channels, frame)
        if mismatch is not None and len(self.heard) < _HEARD_CAP and mismatch not in self.heard:
            # A repeat burst is 8 copies of one press; keeping distinct
            # presses only is what lets the timeout screen name the remote
            # actually in the user's hand instead of a wall of duplicates.
            self.heard.append(mismatch)
        if button is None:
            return None
        press = TimedPress(
            button=button,
            boot=_uint32(payload.get(MQTT_RX_FIELD_BOOT)),
            bridge_millis=_uint32(payload.get(MQTT_RX_FIELD_T)),
            received_at_monotonic=received_at_monotonic,
        )
        if button in DIRECTIONS:
            self._open(press)
            return None
        return self._close(press)

    def _open(self, press: TimedPress) -> None:
        """Start a run, ignoring the repeats of the burst that already did."""
        if press.button not in self.wanted:
            # The screen has asked for the other direction. Re-pressing the one
            # already measured is not an overwrite -- redo is a menu option.
            _LOGGER.debug(
                "travel: ignoring %s -- waiting for %s",
                press.button,
                sorted(self.wanted),
            )
            return
        started = self.started
        if (
            started is not None
            and started.button == press.button
            and press.received_at_monotonic - started.received_at_monotonic
            <= TRAVEL_BURST_WINDOW_SECONDS
        ):
            return
        if started is not None:
            _LOGGER.debug(
                "travel: restarting the run on %s (was %s)",
                press.button,
                started.button,
            )
        else:
            _LOGGER.debug("travel: run opened on %s", press.button)
        self.started = press

    def _close(self, press: TimedPress) -> TravelMeasurement | None:
        """Resolve a STOP against the open run, if there is one."""
        started = self.started
        if started is None:
            _LOGGER.debug(
                "travel: STOP heard with no run open -- the direction press "
                "was never heard, or a previous STOP already closed the run"
            )
            return None
        self.started = None
        elapsed = interval_seconds(started, press)
        if elapsed is None:
            _LOGGER.debug(
                "travel: discarding the %s run -- the two frames' clocks "
                "cannot be compared (reboot or reordered delivery)",
                started.button,
            )
            return None
        stored = stored_value(elapsed)
        if stored is None:
            _LOGGER.debug(
                "travel: discarding a %.2fs %s run -- outside the storable range",
                elapsed,
                started.button,
            )
            return None
        return TravelMeasurement(
            direction=started.button,
            measured_seconds=elapsed,
            stored_seconds=stored,
        )
