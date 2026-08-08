"""MQTT Learn session helpers for Zemismart Blinds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import callback

from .codec import CommandBases, derive_bases
from .const import MQTT_ROOT
from .models import BridgeRegistry, RemoteIdentity

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.core import HomeAssistant

    from .command_ledger import FrameSignature

    type Unsubscriber = Callable[[], None]
    type MessageCallback = Callable[
        [ReceiveMessage],
        Coroutine[Any, Any, None] | None,
    ]

__all__ = [
    "_ContestedWindow",
    "_DiscoverySession",
    "_LearnCapture",
    "_SniffAttempt",
    "_async_subscribe_ready",
    "_handle_flow_availability",
    "_handle_flow_info",
    "_remote_identity_from_captures",
]

_LEARN_ACTIONS: Final = ("UP", "DOWN", "STOP")


def _payload_text(payload: str | bytes | bytearray) -> str:
    """Normalize an MQTT payload received with or without text decoding."""
    return payload.decode() if isinstance(payload, bytes | bytearray) else payload


def _bridge_id(topic: str, leaf: str) -> str | None:
    """Extract a bridge id from an exact three-part MQTT topic."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != MQTT_ROOT or parts[2] != leaf:
        return None
    return parts[1] or None


async def _async_subscribe_ready(
    hass: HomeAssistant,
    topic: str,
    msg_callback: MessageCallback,
) -> Unsubscriber:
    """Subscribe and wait until the broker has acknowledged the topic."""
    from homeassistant.components import mqtt

    ready = asyncio.Event()
    unsubscribe: Unsubscriber | None = None
    stop_monitoring: Unsubscriber | None = None
    completed = False
    try:
        unsubscribe = await mqtt.async_subscribe(
            hass,
            topic,
            msg_callback,
            qos=1,
        )
        stop_monitoring = mqtt.async_on_subscribe_done(hass, topic, 1, ready.set)
        await ready.wait()
        completed = True
        return unsubscribe
    finally:
        if stop_monitoring is not None:
            stop_monitoring()
        if not completed and unsubscribe is not None:
            unsubscribe()


@dataclass(slots=True)
class _DiscoverySession:
    """Retained bridge state collected by one bounded flow-local subscription."""

    registry: BridgeRegistry


@dataclass(frozen=True, slots=True)
class _LearnCapture:
    """One decoded action frame accepted by the current sniff attempt."""

    frame: str
    prefix: int
    remote_id: int
    channels: tuple[int, ...]
    command: int
    # The action the WIZARD asked the user to press. Authoritative: the button
    # is known from the prompt, so it is never inferred from the opcode.
    button: str
    # What infer_action_button() made of the opcode byte, or None when the byte
    # is outside the codec's table. Retained as a hint only -- it decides
    # whether this frame resolves the attempt immediately or is held as the
    # fallback, never whether the capture is valid (#26).
    inferred_button: str | None
    # The per-remote calibrated base recovered from this exact capture.
    base: int
    # Which bridge delivered this capture. The wizard arms the whole fleet, so
    # "the bridge" is only knowable per capture -- and which bridge can hear a
    # given remote is the useful half of what #57 measured.
    bridge_id: str | None = None


@dataclass(slots=True)
class _ContestedWindow:
    """What ELSE was on air within one settle window of one candidate winner.

    A capture becomes a candidate winner the moment it resolves the attempt or
    is held as the unrecognised fallback, and that is when its window opens.
    Rivals are recorded into the window they actually fall in: the half BEFORE
    the anchor is read out of ``recent`` as the window opens, the half AFTER as
    each later press arrives. So a press is only ever compared with an anchor
    that exists at the time of the comparison, and the verdict is never
    re-derived afterwards.

    That is the whole point of this type. ``resolved_at`` MOVES -- the
    held-unrecognised path stamps it and a recognised winner supersedes it --
    and three review rounds each found another way for a single stored float per
    press to be measured against the wrong one of those two anchors (#57).
    Deciding per window, as it happens, leaves nothing to rebase.
    """

    # When this candidate winner arrived, on the event-loop clock.
    anchor: float
    # Remote and channel set: what would be ADOPTED. A rival is a press that
    # answers "which blind is this" differently, so the same remote's other
    # button on the same selector is not one -- it agrees with the winner.
    press: tuple[str, frozenset[int]]
    # Names of the rivals heard in this window, for the screen that refuses.
    # Capped for display only: the verdict is whether this set is EMPTY, and a
    # name is dropped only once the cap's worth are already in it, so no cap can
    # turn a contested window into an uncontested one.
    rivals: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _SniffAttempt:
    """Mutable state for one prompted action's capture window."""

    action: str
    measured: dict[str, _LearnCapture]
    future: asyncio.Future[_LearnCapture]
    # Every distinct press heard RECENTLY, mapped to when its last copy arrived,
    # pruned to one settle window and bounded by dropping the oldest. It exists
    # only to answer, at the moment a window opens, "what else was on air just
    # before this".
    #
    # Keyed by the FULL press signature -- remote, channel set and action -- the
    # same key `state_sync` and the travel run dedup on: two channel sets from
    # one remote are two different presses, and collapsing them by remote id
    # would hide the second. Last copy rather than first, because within a
    # window that is the copy nearest anything that can still open one.
    recent: dict[FrameSignature, float] = field(default_factory=dict)
    # When the capture that WON this attempt arrived, on the event loop clock.
    # The settle sleeps to the end of this window; which presses COMPETED with
    # it is recorded in that winner's own window, not measured from here.
    resolved_at: float | None = None
    # A structurally valid capture whose opcode byte is not in the codec's
    # action table. That table is a 10-sample empirical fit, not protocol, so
    # an unrecognised opcode is not evidence of a bad capture -- it is held
    # here and used if the window closes without a recognised match, instead
    # of being dropped as it was until #26. A recognised frame for the
    # prompted action still wins outright, which keeps the OEM TRAILER burst
    # that follows UP/DOWN from being mistaken for the action itself.
    unrecognized: _LearnCapture | None = None
    # Each candidate winner's judging window, stored BESIDE the capture it
    # judges. Both can be open at once -- the held fallback and the recognised
    # winner that supersedes it -- and both keep collecting rivals, because
    # either may still be the capture that gets adopted.
    #
    # One field per candidate rather than a list the settle reads the end of.
    # That ordering was an implicit invariant with a reachable break: if a
    # recognised frame resolves the future in the same ready-batch as the
    # capture timeout and is ordered first, the flow adopts the HELD capture
    # while the newest window belongs to the recognised one -- judging the
    # wrong window and losing a refusal. Pairing each capture with its own
    # window makes that mismatch unexpressible (#57).
    unrecognized_window: _ContestedWindow | None = None
    resolved_window: _ContestedWindow | None = None


def _remote_identity_from_captures(
    captures: Mapping[str, _LearnCapture],
) -> tuple[RemoteIdentity, tuple[str, ...]]:
    """Build the calibrated identity from measured captures, deriving any gaps.

    Every captured action contributes its OWN measured base. Only a button the
    user could not capture falls back to ``derive_bases``, whose action opcode
    table held for 10 of the 11 remotes surveyed in #26 -- for the eleventh it
    produced an UP command the motor provably ignored while the measured one
    worked. So derivation is now the exception, and the returned action names
    exist so the confirmation step can tell the user which bases are guesses.

    ``derive_bases`` raises when the reference's own opcode byte is outside
    that table, which is exactly the remote this fallback cannot serve; the
    caller surfaces that as a failed capture rather than storing a guess.
    """
    if not captures:
        msg = "at least one captured action is required"
        raise ValueError(msg)
    reference = next(iter(captures.values()))
    measured = {action: capture.base for action, capture in captures.items()}
    derived_actions = tuple(action for action in _LEARN_ACTIONS if action not in measured)
    if derived_actions:
        # Try every measured capture, not just the first. `derive_bases` only
        # works from a reference whose own opcode is inside the table, and
        # insertion order is the order the WIZARD asked for buttons -- so a user
        # whose UP is untabled but whose DOWN is not was refused the fallback
        # they had explicitly chosen, purely because UP came first.
        fallback = None
        for candidate in captures.values():
            try:
                fallback = derive_bases(
                    candidate.channels,
                    candidate.button,
                    candidate.command,
                    candidate.remote_id,
                )
            except ValueError:
                continue
            reference = candidate
            break
        if fallback is None:
            # No captured button can serve as a reference. Surfaced as a failed
            # capture rather than stored as a guess.
            msg = "no captured action can serve as a derivation reference"
            raise ValueError(msg)
        measured.update({action: fallback.base(action) for action in derived_actions})
    identity = RemoteIdentity(
        prefix=reference.prefix,
        remote_id=reference.remote_id,
        bases=CommandBases(
            up=measured["UP"],
            down=measured["DOWN"],
            stop=measured["STOP"],
        ),
    )
    return identity, derived_actions


@callback
def _handle_flow_availability(
    session: _DiscoverySession,
    message: ReceiveMessage,
) -> None:
    """Collect one retained availability beacon for bridge selection."""
    bridge_id = _bridge_id(message.topic, "availability")
    if bridge_id is None:
        return
    try:
        payload = _payload_text(message.payload)
    except UnicodeDecodeError:
        return
    session.registry.update_availability(bridge_id, payload)


@callback
def _handle_flow_info(
    session: _DiscoverySession,
    message: ReceiveMessage,
) -> None:
    """Collect retained area/default metadata for bridge selection."""
    bridge_id = _bridge_id(message.topic, "info")
    if bridge_id is None:
        return
    try:
        text = _payload_text(message.payload)
    except UnicodeDecodeError:
        return
    if not text.strip():
        session.registry.update_info(bridge_id, {})
        return
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError:
        return
    if isinstance(decoded, Mapping):
        session.registry.update_info(
            bridge_id,
            {str(key): value for key, value in decoded.items()},
        )
