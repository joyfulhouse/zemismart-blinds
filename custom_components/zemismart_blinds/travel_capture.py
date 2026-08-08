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
    from .command_ledger import FrameSignature
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
    "press_signature",
    "stored_value",
]

_HEARD_CAP: Final = 8
# Stands in for the button in a REJECTED press's dedup key when the opcode byte
# is outside the codec's table. It is not a button name and never reaches a
# screen; it only has to be distinct from one, so two untabled presses of one
# remote collapse while a tabled press of the same remote stays its own row.
_UNTABLED_BUTTON: Final = "?"

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
    """One accepted press: which button, when, and which bridge heard it.

    ``bridge_id`` is ``None`` only for payloads with no attribution (the pure
    unit tests, or a caller that listens on a single known bridge); fleet
    listening always attributes, because two bridges' clocks share no epoch.
    """

    button: str
    boot: int | None
    bridge_millis: int | None
    received_at_monotonic: float
    bridge_id: str | None = None


@dataclass(frozen=True, slots=True)
class TravelMeasurement:
    """One completed direction run, raw and as it will be stored."""

    direction: str
    measured_seconds: float
    stored_seconds: int


def interval_seconds(start: TimedPress, stop: TimedPress) -> float | None:
    """Return the run's duration, preferring the bridge's own clock.

    When both frames came from the SAME bridge they travelled one MQTT path, so
    subtracting their ``t`` values cancels broker and event-loop jitter
    outright. That is the preferred case and the reason the bridge clock is
    consulted at all; the fleet-wide case is the paragraph below. This is NOT a
    ``BridgeClock`` projection: that class places events on Home Assistant's
    timeline, and a run needs only an interval on the bridge's own.

    A bridge that rebooted mid-run restarted ``t`` near zero, which reads as a
    backwards delta and is rejected by the half-modulus guard even when the
    payload carries no ``boot`` to compare.

    Two DIFFERENT bridges' ``t`` counters share no epoch, and their ``boot``
    counters can collide by coincidence, so the bridge clock is used only when
    both frames were heard by the same bridge; a cross-bridge pair falls back
    to the monotonic receive times like a frame with no ``t`` at all.
    """
    if (
        start.bridge_millis is not None
        and stop.bridge_millis is not None
        and start.bridge_id == stop.bridge_id
    ):
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


def press_signature(
    prefix: int,
    remote_id: int,
    channels: tuple[int, ...],
    button: str,
) -> FrameSignature:
    """Identify one physical press: which remote, which channels, which button.

    The same shape and key order ``state_sync`` debounces its fleet-wide
    captures on, because it answers the same question: two captures share a
    signature exactly when they are copies of one press on air. Channels and
    remote are part of the key deliberately -- a bare button would collapse
    two different remotes, or one remote on two channel selectors, into a
    single press.
    """
    return f"{prefix:06x}:{remote_id:02x}", frozenset(channels), button


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
) -> tuple[str | None, HeardPress | None, FrameSignature | None]:
    """Match one frame against the calibration, or explain the rejection.

    Returns ``(button, None, signature)`` on a match; ``(None, HeardPress,
    None)`` when the frame is a real press that fails the identity or channel
    gate -- the two rejections a user can act on; ``(None, None, None)`` for
    everything else (undecodable input and this remote's own non-action
    trailer burst).

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
        return None, None, None
    try:
        decoded = decode_rx_capture(frame)
    except _DECODE_ERRORS:
        return None, None, None
    observed_channels = tuple(decoded["chans"])
    if (decoded["prefix"], decoded["remote_id"]) != (identity.prefix, identity.remote_id):
        _LOGGER.debug(
            "travel: ignoring a press from %06x:%02x while measuring %06x:%02x",
            decoded["prefix"],
            decoded["remote_id"],
            identity.prefix,
            identity.remote_id,
        )
        return None, _heard(frame, decoded, observed_channels), None
    if observed_channels != channels:
        _LOGGER.debug(
            "travel: ignoring this remote's press on channels %s -- the cover being "
            "measured stores %s, and the remote's channel selector must match it exactly",
            decoded["chans"],
            list(channels),
        )
        return None, _heard(frame, decoded, observed_channels), None
    try:
        base = derive_base(observed_channels, "UP", decoded["cmd"], decoded["remote_id"])
    except _DECODE_ERRORS:
        return None, None, None
    for button in BUTTONS:
        if base == bases.base(button):
            return (
                button,
                None,
                press_signature(
                    decoded["prefix"],
                    decoded["remote_id"],
                    observed_channels,
                    button,
                ),
            )
    _LOGGER.debug(
        "travel: base 0x%04x matches none of this remote's calibrated actions "
        "(likely the OEM trailer burst)",
        base,
    )
    return None, None, None


def _mismatch_signature(press: HeardPress) -> FrameSignature:
    """Key one REJECTED press the way the run keys an accepted one.

    ``HeardPress`` equality includes the raw frame, and two bridges' captures
    of one press differ in their bucket timings, so value equality alone let a
    single press fill the whole list once the fleet was listening -- crowding
    out the later, DISTINCT remote the timeout screen exists to name (#57).
    """
    return press_signature(
        press.prefix,
        press.remote_id,
        press.channels,
        press.button or _UNTABLED_BUTTON,
    )


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
    # Every press that has anchored THIS run, so a later copy of any of them can
    # never re-anchor it -- see `_open`. Tracking only the CURRENT anchor left
    # the superseded one open to a late copy: a run restarted UP would re-anchor
    # on a lagging bridge's copy of the DOWN burst it replaced, and then store
    # that interval as the UP time for a shade that ran UP (#57).
    anchored: set[FrameSignature] = field(default_factory=set)
    heard: list[HeardPress] = field(default_factory=list)
    # Which rejected presses `heard` already lists, keyed by signature so the
    # fleet's copies of one press collapse into its single row.
    heard_signatures: set[FrameSignature] = field(default_factory=set)
    # Set when a DISTINCT rejected press had to be dropped for want of room.
    # `heard` bounds what a screen can NAME; it must never be read as proof
    # that only one foreign remote was heard, because the mismatch screen
    # offers to rewrite this device's stored identity on exactly that basis --
    # and one stranger's remote worked across enough selector positions fills
    # the cap while the user's OWN press is what gets dropped (#57).
    heard_overflowed: bool = False
    # When each signature was last heard, for the repeat filter below. Bounded
    # by construction: `classify_frame` pins the remote and the channel set
    # before a signature exists, so one run can only ever see UP, DOWN, STOP.
    recent: dict[FrameSignature, float] = field(default_factory=dict)

    def offer_payload(
        self,
        payload: Mapping[str, object],
        received_at_monotonic: float,
        bridge_id: str | None = None,
    ) -> TravelMeasurement | None:
        """Feed one RX payload in; return a measurement when a run closes."""
        frame = payload.get(MQTT_RX_FIELD_FRAME)
        if not isinstance(frame, str):
            return None
        button, mismatch, signature = classify_frame(self.identity, self.channels, frame)
        if mismatch is not None:
            self._record_mismatch(mismatch)
        if button is None or signature is None:
            return None
        repeat = self._is_repeat(signature, received_at_monotonic)
        press = TimedPress(
            button=button,
            boot=_uint32(payload.get(MQTT_RX_FIELD_BOOT)),
            bridge_millis=_uint32(payload.get(MQTT_RX_FIELD_T)),
            received_at_monotonic=received_at_monotonic,
            bridge_id=bridge_id,
        )
        if button in DIRECTIONS:
            # Deliberately NOT gated on `repeat`: a press that would OPEN a run
            # can never shorten one, and dropping it would silently swallow a
            # user's re-press after a discarded run. What a duplicate must
            # never do is re-anchor an OPEN run, which `_open` enforces on the
            # signature itself rather than on a window.
            self._open(press, signature)
            return None
        if repeat:
            return None
        return self._close(press)

    def _record_mismatch(self, mismatch: HeardPress) -> None:
        """List one rejected press per DISTINCT press, not per delivered copy.

        A repeat burst is 8 copies of one press, times every bridge that heard
        it. Keeping distinct presses only is what lets the timeout screen name
        the remote actually in the user's hand: deduplicating on the raw frame
        instead let one remote's copies -- whose bucket timings differ per
        bridge -- exhaust the cap and hide every other remote.

        A press the cap had no room for is REPORTED rather than dropped in
        silence. What consumes this list decides whether to rewrite the
        device's identity, and "only one foreign remote was heard" is a claim
        the list can no longer support once it has overflowed -- the press it
        could not hold may have been the user's own.
        """
        signature = _mismatch_signature(mismatch)
        if signature in self.heard_signatures:
            return
        if len(self.heard) >= _HEARD_CAP:
            self.heard_overflowed = True
            _LOGGER.debug(
                "travel: more than %d distinct presses rejected -- %06x:%02x on %s cannot be named",
                _HEARD_CAP,
                mismatch.prefix,
                mismatch.remote_id,
                mismatch.channels,
            )
            return
        self.heard_signatures.add(signature)
        self.heard.append(mismatch)

    def _is_repeat(self, signature: FrameSignature, received_at_monotonic: float) -> bool:
        """Report whether this is another copy of a press already counted.

        One physical press reaches this run many times over: 8 embedded OEM
        frames on air, times every bridge that heard them.

        The window slides -- each copy re-stamps its signature -- so a burst
        chains however long its copies keep arriving. A fixed window anchored
        at the first copy would expire mid-burst on a lagging bridge and let a
        late copy through as a fresh press. Human re-presses are seconds apart
        and land well outside the window either way.

        A sliding window alone is NOT enough to protect the run's anchor: an
        isolated copy from a bridge lagging by more than a whole burst has no
        chain to slide and escapes the window entirely. `_open` therefore
        refuses to re-anchor on the opening signature regardless of what this
        says, and the window's remaining job is to keep a duplicate STOP from
        closing a run twice.
        """
        previous = self.recent.get(signature)
        self.recent[signature] = received_at_monotonic
        if previous is None:
            return False
        return 0.0 <= received_at_monotonic - previous <= TRAVEL_BURST_WINDOW_SECONDS

    def _open(self, press: TimedPress, signature: FrameSignature) -> None:
        """Start a run, unless this press has already anchored it once.

        A signature that anchored this run is a duplicate for the rest of it,
        ALWAYS -- no window, however late it arrives. Nothing in the protocol
        identifies one physical press (there is no sequence number and no
        per-press nonce), so a second copy of one press and a genuine re-press
        of the same button on the same channels are indistinguishable here.
        Re-anchoring on the wrong one stores a travel time short by the
        delivery spread -- the unsafe direction, since a short time leaves
        "closed" visibly open -- while keeping the first anchor is at worst
        long, and is exactly right when the shade started moving on the first
        press.

        Every anchor is remembered, not just the current one. A run restarted
        on UP still had DOWN anchoring it a moment earlier, and a bridge
        lagging by seconds -- the observed envelope -- then delivers its copy
        of that superseded DOWN burst. Re-anchoring on it would time a run the
        shade never made and store it under the WRONG DIRECTION, which no
        window can separate from a genuine re-press (#57).

        The cost is that changing your mind BACK -- DOWN, then UP, then DOWN
        again -- leaves the run anchored on the UP press rather than the last
        DOWN, so the wizard measures the interval the user did not intend and
        the screen's redo is the remedy. That is the same trade the
        same-signature rule already makes, in the same direction: a late copy
        is common (a bridge under backpressure) where pressing three directions
        inside one measurement is not, and refusing to re-anchor is the choice
        that cannot silently produce a SHORT time.

        The FIRST press of the other direction has its own signature and still
        restarts the run: that is the user changing their mind, and it is the
        restart the wizard actually needs.
        """
        if press.button not in self.wanted:
            # The screen has asked for the other direction. Re-pressing the one
            # already measured is not an overwrite -- redo is a menu option.
            _LOGGER.debug(
                "travel: ignoring %s -- waiting for %s",
                press.button,
                sorted(self.wanted),
            )
            return
        if signature in self.anchored:
            _LOGGER.debug(
                "travel: absorbing another copy of a %s press that already anchored this run",
                press.button,
            )
            return
        started = self.started
        if started is not None:
            _LOGGER.debug(
                "travel: restarting the run on %s (was %s)",
                press.button,
                started.button,
            )
        else:
            _LOGGER.debug("travel: run opened on %s", press.button)
        self.started = press
        self.anchored.add(signature)

    def _close(self, press: TimedPress) -> TravelMeasurement | None:
        """Resolve a STOP against the open run, if there is one.

        Timed against the first copy heard of the opening press. When that
        copy and this STOP came from the same bridge the bridge clock times
        it; otherwise ``interval_seconds`` falls back to the monotonic receive
        clocks, since cross-bridge ``t`` subtraction is never meaningful.
        """
        started = self.started
        if started is None:
            _LOGGER.debug(
                "travel: STOP heard with no run open -- the direction press "
                "was never heard, or a previous STOP already closed the run"
            )
            return None
        self.started = None
        # The next run is a new one: a press that anchored the run just closed
        # must be able to open the next, or a user re-pressing the same
        # direction after a run too fast to store would never be heard again.
        self.anchored.clear()
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
