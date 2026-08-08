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
class _SniffAttempt:
    """Mutable state for one prompted action's capture window."""

    action: str
    measured: dict[str, _LearnCapture]
    future: asyncio.Future[_LearnCapture]
    # Every distinct press heard for this action, mapped to when its LAST copy
    # arrived. The first capture of a wizard run has no calibrated identity to
    # gate on, so with the whole fleet listening ANY remote pressed anywhere in
    # the house lands here; adopting one while another was pressed alongside it
    # would be a silent guess (#57).
    #
    # Keyed by the FULL press signature -- remote, channel set and action --
    # the same key `state_sync` and the travel run dedup on: two channel sets
    # from one remote are two different presses, and collapsing them by remote
    # id would hide the second. Last-heard rather than first, because a remote
    # that was also pressed minutes earlier still competes if it is pressed
    # again alongside ours.
    candidates: dict[FrameSignature, float] = field(default_factory=dict)
    # When each press the candidate cap had no room for was heard. The cap
    # bounds how many presses a SCREEN can name; it must never decide whether
    # the capture was ambiguous, or a busy bus would buy silence -- so a press
    # dropped for room forces the refusal it could not be listed in (#57).
    #
    # Timestamps rather than a flag, because a flag could only ever be set: an
    # unrelated burst early in the 30-second listen then refused a capture
    # whose winner arrived twenty seconds after every press involved had aged
    # out. These age by the same window rule the candidates do.
    overflowed_at: list[float] = field(default_factory=list)
    # When the capture that WON this attempt arrived, on the event loop clock.
    # The competing-press window is measured from here, not from the start of
    # the 30-second listen: a press heard 20 seconds before ours is ordinary
    # house traffic, and vetoing on it would make a legitimate learn
    # impossible to complete in a house with more than one remote.
    resolved_at: float | None = None
    # A structurally valid capture whose opcode byte is not in the codec's
    # action table. That table is a 10-sample empirical fit, not protocol, so
    # an unrecognised opcode is not evidence of a bad capture -- it is held
    # here and used if the window closes without a recognised match, instead
    # of being dropped as it was until #26. A recognised frame for the
    # prompted action still wins outright, which keeps the OEM TRAILER burst
    # that follows UP/DOWN from being mistaken for the action itself.
    unrecognized: _LearnCapture | None = None


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
