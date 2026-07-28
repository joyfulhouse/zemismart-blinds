"""Config and options flows for adding one Zemismart blind/group at a time."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.util.ulid import ulid_now

from .codec import (
    CommandBases,
    decode_reference_b0,
    decode_rx_capture,
    derive_base,
    derive_bases,
    derive_bases_from_base,
    infer_action_button,
)
from .const import (
    CONF_AREA_ID,
    CONF_BASE_DOWN,
    CONF_BASE_STOP,
    CONF_BASE_TRAILER,
    CONF_BASE_UP,
    CONF_BRIDGE,
    CONF_CALIBRATION_BASE,
    CONF_CALIBRATION_BUTTON,
    CONF_CALIBRATION_FRAME,
    CONF_CHANNELS,
    CONF_COALESCE_WINDOW_MS,
    CONF_COVER_ID,
    CONF_COVERS,
    CONF_NAME,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    CONF_TRAVEL_DOWN,
    CONF_TRAVEL_UP,
    DEFAULT_COALESCE_WINDOW_MS,
    DEFAULT_REPEATS,
    DEFAULT_SNIFF_WINDOW_SECONDS,
    DOMAIN,
    MQTT_AVAILABILITY_TOPIC,
    MQTT_CMD_ACTION_SNIFF,
    MQTT_CMD_FIELD_ACTION,
    MQTT_CMD_FIELD_SECONDS,
    MQTT_CMD_TEMPLATE,
    MQTT_INFO_TOPIC,
    MQTT_ROOT,
    MQTT_RX_FIELD_FRAME,
)
from .models import (
    BridgeRegistry,
    CoverConfig,
    NoOnlineBridgeError,
    RemoteConfig,
    RemoteIdentity,
    RemoteRuntime,
    laminar_conflict,
    parse_channels,
    parse_hex,
    whole_number,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant

    type Unsubscriber = Callable[[], None]
    type MessageCallback = Callable[
        [ReceiveMessage],
        Coroutine[Any, Any, None] | None,
    ]


_LOGGER = logging.getLogger(__name__)
# Exception tuples are bound to names rather than written inline. `ruff format`
# at this project's target-version rewrites the parenthesised `except (A, B):`
# straight back into the bare PEP 758 `except A, B:`, which is a SyntaxError on
# Python < 3.14 -- so a manual or zip install onto an older core fails at import
# with no useful message. A named tuple is left alone by the formatter and
# parses everywhere (#43).
_COERCION_ERRORS: Final = (TypeError, ValueError)
_PAYLOAD_ERRORS: Final = (UnicodeDecodeError, json.JSONDecodeError)
_ADVANCED_SECTION = "advanced"
_AUTOMATIC_BRIDGE = "automatic"
_BRIDGE_DISCOVERY_SECONDS = 0.25
_MQTT_BOOTSTRAP_TIMEOUT_SECONDS = 5.0
_CAPTURE_TIMEOUT_SECONDS = float(DEFAULT_SNIFF_WINDOW_SECONDS)
# The order the wizard walks the user through. Every one of these is MEASURED
# from its own press: the codec can extrapolate two of the three from the
# remaining one, but its action opcode table holds for only 10 of the 11
# remotes surveyed in #26, and the eleventh's extrapolated UP frame was
# transmitted, heard by five bridges, and ignored by the motor.
_LEARN_ACTIONS: Final = ("UP", "DOWN", "STOP")
_CAPTURE_OWNERS: dict[tuple[int, str], str] = {}
# CoverConfig enforces storage identity while this validation draft is not yet
# stored. Every terminal add replaces the sentinel with a fresh ULID; edits
# preserve the selected row's existing cover_id.
_PENDING_COVER_ID = "pending"


def _float_value(value: object, fallback: float) -> float:
    """Convert a persisted selector value to a display float."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def _int_value(value: object, fallback: int) -> int:
    """Convert a persisted selector value to a display integer."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


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


@callback
def _release_capture_owner(
    owner_key: tuple[int, str],
    session_id: str,
    stop_task: asyncio.Future[None],
) -> None:
    """Release bridge ownership only after its stop publication finishes."""
    if not stop_task.cancelled():
        with suppress(Exception):
            stop_task.result()
    if _CAPTURE_OWNERS.get(owner_key) == session_id:
        del _CAPTURE_OWNERS[owner_key]


def _rekey_remote_device(hass: HomeAssistant, old_key: str, new_key: str) -> None:
    """Move the remote's device onto a new remote identity, in place.

    Re-identifying preserves the device_id, its area override, and every entity
    attached to it. Recreating would not, and the device_id is what automations
    target -- so a relearn must never be allowed to mint a new one.
    """
    from homeassistant.helpers import device_registry as dr

    if old_key == new_key:
        return
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, old_key)})
    if device is None:
        return
    if registry.async_get_device(identifiers={(DOMAIN, new_key)}) is not None:
        # Something already owns the new identity; async_update_device would
        # raise a collision and abort the flow. Leave both rows alone and let
        # _ensure_remote_device converge on the survivor at reload.
        return
    registry.async_update_device(device.id, new_identifiers={(DOMAIN, new_key)})


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


@dataclass(slots=True)
class _SniffAttempt:
    """Mutable state for one prompted action's capture window."""

    action: str
    measured: dict[str, _LearnCapture]
    future: asyncio.Future[_LearnCapture]
    # A structurally valid capture whose opcode byte is not in the codec's
    # action table. That table is a 10-sample empirical fit, not protocol, so
    # an unrecognised opcode is not evidence of a bad capture -- it is held
    # here and used if the window closes without a recognised match, instead
    # of being dropped as it was until #26. A recognised frame for the
    # prompted action still wins outright, which keeps the OEM TRAILER burst
    # that follows UP/DOWN from being mistaken for the action itself.
    unrecognized: _LearnCapture | None = None


def _remote_identity_from_manual(user_input: Mapping[str, Any]) -> RemoteIdentity:
    """Validate manual identity input into a calibrated RemoteIdentity."""
    prefix = parse_hex(user_input.get(CONF_PREFIX), CONF_PREFIX, 24)
    remote_id = parse_hex(user_input.get(CONF_REMOTE_ID), CONF_REMOTE_ID, 8)
    calibration_button = str(user_input.get(CONF_CALIBRATION_BUTTON, "UP"))
    raw_base = str(user_input.get(CONF_CALIBRATION_BASE, "")).strip()
    raw_frame = str(user_input.get(CONF_CALIBRATION_FRAME, "")).strip()
    if raw_base and raw_frame:
        msg = "provide either a command base or a captured reference, not both"
        raise ValueError(msg)
    bases: CommandBases | None = None
    if raw_base:
        bases = derive_bases_from_base(
            calibration_button,
            parse_hex(raw_base, CONF_CALIBRATION_BASE, 16),
            remote_id,
        )
    elif raw_frame:
        decoded = decode_reference_b0(raw_frame)
        if decoded["prefix"] != prefix or decoded["remote_id"] != remote_id:
            msg = "captured reference identity does not match the entered remote"
            raise ValueError(msg)
        bases = derive_bases(
            decoded["chans"],
            calibration_button,
            decoded["cmd"],
            remote_id,
        )
    raw_trailer = str(user_input.get(CONF_BASE_TRAILER, "")).strip()
    if raw_trailer:
        if bases is None:
            bases = RemoteIdentity(prefix, remote_id).bases
        if bases is None:
            msg = "action calibration is required before a trailer base"
            raise ValueError(msg)
        bases = CommandBases(
            up=bases.up,
            down=bases.down,
            stop=bases.stop,
            trailer=parse_hex(raw_trailer, CONF_BASE_TRAILER, 16),
        )
    identity = RemoteIdentity(prefix=prefix, remote_id=remote_id, bases=bases)
    if identity.bases is None:
        msg = "remote calibration is required"
        raise ValueError(msg)
    return identity


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


def _is_own_emission(hass: HomeAssistant, frame: str) -> bool:
    """Return whether any loaded remote is transmitting this frame right now.

    Learning is a raw-RF capture, so a command published by an automation
    while the wizard is armed comes back off the sniffing bridge looking
    exactly like a human's remote press. Already-loaded hubs know what they
    put on air; a first-ever setup has no hub and nothing to confuse.
    """
    return any(
        isinstance(runtime := getattr(entry, "runtime_data", None), RemoteRuntime)
        and runtime.hub.frame_is_own_emission(frame)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )


def _capture_belongs_to_this_action(
    attempt: _SniffAttempt,
    capture: _LearnCapture,
) -> bool:
    """Reject a frame that cannot be this action's press on this remote.

    Two ways a capture is disqualified without ever consulting its opcode:
    it comes from a different remote than the actions already measured, or it
    repeats a COMMAND we already measured for a different action -- which is
    what a lingering repeat of the previous button (or the user not pressing
    anything) looks like on air.

    Compared on the DERIVED BASE rather than the raw command, deliberately.
    `derive_base` validates its button argument but does not use it -- the
    recovery is `(cmd - remote_id + group_offset(chans))`, which is
    action-independent -- so one physical frame yields one base whichever
    action is currently being solicited, and the comparison works across the
    action boundary. Because the base is also CHANNEL-normalised, it catches a
    lingering repeat of the same button on a different channel, which comparing
    raw commands would miss.
    """
    for action, measured in attempt.measured.items():
        if (measured.prefix, measured.remote_id) != (capture.prefix, capture.remote_id):
            _LOGGER.debug(
                "Learn: ignoring a capture from %06x:%02x while learning %s for %06x:%02x",
                capture.prefix,
                capture.remote_id,
                attempt.action,
                measured.prefix,
                measured.remote_id,
            )
            return False
        if measured.base == capture.base:
            _LOGGER.debug(
                "Learn: ignoring a %s capture that repeats the base already measured for %s",
                attempt.action,
                action,
            )
            return False
    return True


@callback
def _handle_sniff_message(
    flow: ZemismartBlindsConfigFlow,
    session_id: str,
    expected_topic: str,
    attempt: _SniffAttempt,
    message: ReceiveMessage,
) -> None:
    """Offer one received frame to the attempt for the prompted action.

    Capture acceptance is deliberately NOT gated on ``infer_action_button``.
    In this flow the button is known from the prompt the user is answering, so
    inference is only a hint about whether to stop listening now; a capture
    that decodes structurally is accepted even when its opcode byte is
    unrecognised. Gating on it dropped every UP and STOP press of a real
    remote silently, with no log and no error (#26).
    """
    if (
        flow._sniff_session_id != session_id
        or attempt.future.done()
        or message.retain
        or message.topic != expected_topic
    ):
        return
    try:
        text = _payload_text(message.payload)
        decoded_payload: object = json.loads(text)
    except _PAYLOAD_ERRORS:
        return
    if not isinstance(decoded_payload, Mapping):
        return
    frame = decoded_payload.get(MQTT_RX_FIELD_FRAME)
    if not isinstance(frame, str):
        return
    try:
        # Learning is a RECEIVE path, so it decodes like every other one:
        # trailer-tolerant. Not every OEM remote puts the nominal [1, 0]
        # trailer on air (PROTOCOL.md), and the strict decoder used here until
        # #27 rejected those captures before any handler saw them -- the user
        # got a capture timeout on a remote that was transmitting perfectly.
        decoded = decode_rx_capture(frame)
        channels = tuple(decoded["chans"])
        inferred = infer_action_button(channels, decoded["cmd"])
        base = derive_base(channels, attempt.action, decoded["cmd"], decoded["remote_id"])
    except _COERCION_ERRORS:
        return
    if _is_own_emission(flow.hass, frame):
        return
    capture = _LearnCapture(
        frame=frame,
        prefix=decoded["prefix"],
        remote_id=decoded["remote_id"],
        channels=channels,
        command=decoded["cmd"],
        button=attempt.action,
        inferred_button=inferred,
        base=base,
    )
    if not _capture_belongs_to_this_action(attempt, capture):
        return
    if inferred == attempt.action:
        attempt.future.set_result(capture)
        return
    if inferred is not None:
        _LOGGER.debug(
            "Learn: ignoring a frame that decodes as %s while %s was requested",
            inferred,
            attempt.action,
        )
        return
    if attempt.unrecognized is None:
        _LOGGER.debug(
            "Learn: holding a capture with unrecognised opcode 0x%02x as the %s candidate",
            decoded["cmd"] >> 8,
            attempt.action,
        )
        attempt.unrecognized = capture


def _flatten_details(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the UI-only collapsed section into the persisted field shape."""
    advanced = user_input.get(_ADVANCED_SECTION)
    if not isinstance(advanced, Mapping):
        msg = "advanced settings are required"
        raise ValueError(msg)
    return {
        **{key: value for key, value in user_input.items() if key != _ADVANCED_SECTION},
        **advanced,
    }


def _manual_schema(suggested: Mapping[str, object] | None = None) -> vol.Schema:
    """Build the Advanced manual identity/calibration form."""
    values = suggested or {}
    button = str(values.get(CONF_CALIBRATION_BUTTON, "UP"))
    if button not in {"UP", "DOWN", "STOP"}:
        button = "UP"
    return vol.Schema(
        {
            vol.Required(
                CONF_PREFIX,
                default=str(values.get(CONF_PREFIX, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_REMOTE_ID,
                default=str(values.get(CONF_REMOTE_ID, "")),
            ): selector.TextSelector(),
            vol.Required(CONF_CALIBRATION_BUTTON, default=button): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["UP", "DOWN", "STOP"])
            ),
            vol.Optional(
                CONF_CALIBRATION_BASE,
                default=str(values.get(CONF_CALIBRATION_BASE, "")),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_CALIBRATION_FRAME,
                default=str(values.get(CONF_CALIBRATION_FRAME, "")),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_BASE_TRAILER,
                default=str(values.get(CONF_BASE_TRAILER, "")),
            ): selector.TextSelector(),
        }
    )


def _remote_settings_schema(suggested: Mapping[str, object] | None) -> vol.Schema:
    """Build the remote name/area/transport form."""
    values = suggested or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME,
                default=str(values.get(CONF_NAME, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_AREA_ID,
                default=str(values.get(CONF_AREA_ID, "")),
            ): selector.AreaSelector(),
            vol.Required(_ADVANCED_SECTION): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_REPEATS,
                            default=_int_value(values.get(CONF_REPEATS), DEFAULT_REPEATS),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1,
                                max=20,
                                step=1,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Required(
                            CONF_COALESCE_WINDOW_MS,
                            default=_int_value(
                                values.get(CONF_COALESCE_WINDOW_MS),
                                DEFAULT_COALESCE_WINDOW_MS,
                            ),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0,
                                max=2000,
                                step=10,
                                mode=selector.NumberSelectorMode.BOX,
                                unit_of_measurement="ms",
                            )
                        ),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def _reconfigure_edit_schema(suggested: Mapping[str, object]) -> vol.Schema:
    """Extend remote settings with editable command calibration bases."""
    return _remote_settings_schema(suggested).extend(
        {
            vol.Required(
                CONF_BASE_UP,
                default=str(suggested.get(CONF_BASE_UP, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_BASE_DOWN,
                default=str(suggested.get(CONF_BASE_DOWN, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_BASE_STOP,
                default=str(suggested.get(CONF_BASE_STOP, "")),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_BASE_TRAILER,
                default=str(suggested.get(CONF_BASE_TRAILER, "")),
            ): selector.TextSelector(),
        }
    )


def _cover_schema(suggested: Mapping[str, object] | None) -> vol.Schema:
    """Build one wizard cover form: name, channels, optional travel times."""
    values = suggested or {}
    travel_selector = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0.1,
            max=600,
            step=0.1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )
    fields: dict[vol.Marker, object] = {
        vol.Required(CONF_NAME, default=str(values.get(CONF_NAME, ""))): selector.TextSelector(),
        vol.Required(
            CONF_CHANNELS,
            default=str(values.get(CONF_CHANNELS, "")),
        ): selector.TextSelector(),
        # Travel fields carry NO defaults, ever: a default harvested from a
        # previous (failed) submission would silently backfill an omitted
        # field on the next attempt and defeat the travel_required check.
        vol.Optional(CONF_TRAVEL_UP): travel_selector,
        vol.Optional(CONF_TRAVEL_DOWN): travel_selector,
    }
    return vol.Schema(fields)


def _cover_display_values(
    cover_id: str,
    data: Mapping[str, object],
    title: str,
) -> dict[str, object]:
    """Convert stored cover data to values suitable for form suggestions."""
    try:
        cover = CoverConfig.from_stored(cover_id, data)
    except _COERCION_ERRORS:
        suggested: dict[str, object] = {CONF_NAME: title}
        if (raw_channels := data.get(CONF_CHANNELS)) is not None:
            if isinstance(raw_channels, str):
                channels_text = raw_channels
            elif isinstance(raw_channels, Iterable):
                channels_text = ",".join(str(channel) for channel in raw_channels)
            else:
                channels_text = str(raw_channels)
            suggested[CONF_CHANNELS] = channels_text
        return suggested

    suggested = {
        CONF_NAME: cover.name,
        CONF_CHANNELS: ",".join(str(channel) for channel in cover.channels),
    }
    for key in (CONF_TRAVEL_UP, CONF_TRAVEL_DOWN):
        if (stored := data.get(key)) not in (None, ""):
            suggested[key] = stored
    return suggested


def _validate_cover_input(
    user_input: Mapping[str, Any],
    existing: list[tuple[int, ...]],
) -> tuple[CoverConfig | None, dict[str, str]]:
    """Validate one wizard cover form against the covers collected so far."""
    try:
        channels = parse_channels(user_input.get(CONF_CHANNELS, ""))
    except ValueError:
        return None, {CONF_CHANNELS: "invalid_config"}
    conflict = laminar_conflict(channels, existing)
    if conflict is not None:
        return None, {CONF_CHANNELS: conflict}
    born_aggregate = any(
        frozenset(sibling_channels) < frozenset(channels) for sibling_channels in existing
    )
    raw_up = user_input.get(CONF_TRAVEL_UP)
    raw_down = user_input.get(CONF_TRAVEL_DOWN)
    if not born_aggregate and (raw_up is None or raw_down is None):
        return None, {"base": "travel_required"}
    try:
        cover = CoverConfig(
            name=str(user_input.get(CONF_NAME, "")),
            channels=channels,
            travel_up=float(raw_up) if raw_up is not None else None,
            travel_down=float(raw_down) if raw_down is not None else None,
            cover_id=_PENDING_COVER_ID,
        )
    except _COERCION_ERRORS:
        return None, {"base": "invalid_config"}
    return cover, {}


def _learn_setup_schema(
    registry: BridgeRegistry,
    suggested: Mapping[str, object] | None,
) -> vol.Schema:
    """Build name/area/bridge fields from one discovery snapshot."""
    values = suggested or {}
    area_id = str(values.get(CONF_AREA_ID, ""))
    online = [bridge for bridge in registry.bridges if bridge.online]
    requested_bridge = str(values.get(CONF_BRIDGE, _AUTOMATIC_BRIDGE))
    bridge_ids = {bridge.bridge_id for bridge in online}
    if requested_bridge != _AUTOMATIC_BRIDGE and requested_bridge not in bridge_ids:
        requested_bridge = _AUTOMATIC_BRIDGE
    options: list[selector.SelectOptionDict] = [{"value": _AUTOMATIC_BRIDGE, "label": "Automatic"}]
    for bridge in online:
        label = bridge.bridge_id
        if bridge.area_id:
            label = f"{label} — {bridge.area_id}"
        options.append({"value": bridge.bridge_id, "label": label})
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME,
                default=str(values.get(CONF_NAME, "")),
            ): selector.TextSelector(),
            vol.Required(CONF_AREA_ID, default=area_id): selector.AreaSelector(),
            vol.Required(CONF_BRIDGE, default=requested_bridge): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    translation_key="bridge",
                )
            ),
        }
    )


def _sibling_channel_sets(
    entry: config_entries.ConfigEntry,
    *,
    exclude_cover_id: str | None = None,
) -> list[tuple[int, ...]]:
    """Load every sibling channel set, failing closed on unreadable channels."""
    channel_sets: list[tuple[int, ...]] = []
    for row in _entry_cover_rows(entry):
        cover_id = _row_cover_id(row)
        if exclude_cover_id is not None and cover_id == exclude_cover_id:
            continue
        try:
            channels = CoverConfig.from_stored(cover_id, row).channels
        except _COERCION_ERRORS:
            try:
                channels = parse_channels(row.get(CONF_CHANNELS, ""))
            except (TypeError, ValueError) as err:
                msg = f"invalid sibling cover channels: {cover_id}"
                raise ValueError(msg) from err
        channel_sets.append(channels)
    return channel_sets


def _entry_cover_rows(
    entry: config_entries.ConfigEntry,
) -> list[dict[str, object]]:
    """Copy stored cover rows without dropping unknown keys."""
    raw_rows = entry.data.get(CONF_COVERS)
    if not isinstance(raw_rows, list | tuple):
        msg = "covers must be a list"
        raise ValueError(msg)
    rows: list[dict[str, object]] = []
    for index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, Mapping) or not all(isinstance(key, str) for key in raw_row):
            msg = f"invalid cover row {index}"
            raise ValueError(msg)
        rows.append({str(key): value for key, value in raw_row.items()})
    return rows


def _row_cover_id(row: Mapping[str, object]) -> str:
    """Return one non-empty stored cover identity."""
    cover_id = row.get(CONF_COVER_ID)
    if not isinstance(cover_id, str) or not cover_id.strip():
        msg = "invalid cover_id"
        raise ValueError(msg)
    return cover_id


def _find_cover_row(
    rows: list[dict[str, object]],
    cover_id: str,
) -> tuple[int, dict[str, object]]:
    """Find one stored row by stable identity."""
    for index, row in enumerate(rows):
        if _row_cover_id(row) == cover_id:
            return index, row
    msg = f"unknown cover_id: {cover_id}"
    raise ValueError(msg)


def _cover_picker_schema(rows: list[dict[str, object]]) -> vol.Schema:
    """Build an identity-keyed picker with unambiguous duplicate names."""
    names = [str(row.get(CONF_NAME, "")).strip() for row in rows]
    duplicate_names = {name for name in names if names.count(name) > 1}
    options: list[selector.SelectOptionDict] = []
    for row, name in zip(rows, names, strict=True):
        cover_id = _row_cover_id(row)
        label = name or cover_id
        if name in duplicate_names:
            label = f"{label} — {cover_id}"
        options.append({"value": cover_id, "label": label})
    return vol.Schema(
        {
            vol.Required(CONF_COVER_ID): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            )
        }
    )


def _cover_removal_refusal(
    entry: config_entries.ConfigEntry,
    cover_id: str,
) -> str | None:
    """Return why a stored cover cannot safely be removed."""
    rows = _entry_cover_rows(entry)
    if len(rows) <= 1:
        return "last_cover"
    _index, selected_row = _find_cover_row(rows, cover_id)
    selected = CoverConfig.from_stored(cover_id, selected_row)
    siblings = _sibling_channel_sets(entry, exclude_cover_id=cover_id)
    selected_channels = frozenset(selected.channels)
    is_leaf = not any(frozenset(channels) < selected_channels for channels in siblings)
    if is_leaf and any(selected_channels < frozenset(channels) for channels in siblings):
        return "aggregate_dependency"
    return None


class ZemismartBlindsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add exactly one blind or group device per config entry."""

    VERSION = 2

    _captures: dict[str, _LearnCapture] | None = None
    _cover_id: str | None = None
    _covers: list[CoverConfig] | None = None
    _identity: RemoteIdentity | None = None
    _learn_action: str = _LEARN_ACTIONS[0]
    _learn_area_id: str | None = None
    _learn_captured: str | None = None
    _learn_bridge: str | None = None
    _learn_name: str | None = None
    _learn_registry: BridgeRegistry | None = None
    _learn_suggested: dict[str, object] | None = None
    _remote: RemoteConfig | None = None
    _sniff_session_id: str | None = None
    _sniff_task: asyncio.Task[Literal["captured", "timeout"]] | None = None

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer remote and per-cover management."""
        if CONF_CHANNELS in self._get_reconfigure_entry().data:
            return self.async_abort(reason="legacy_not_supported")
        del user_input
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                "reconfigure_learn",
                "reconfigure_edit",
                "cover_add",
                "cover_pick_edit",
                "cover_pick_remove",
            ],
        )

    def _update_covers_and_abort(
        self,
        rows: list[dict[str, object]],
        reason: str,
    ) -> ConfigFlowResult:
        """Persist one cover-list mutation and let the update listener reload."""
        entry = self._get_reconfigure_entry()
        self.hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_COVERS: rows},
        )
        return self.async_abort(reason=reason)

    async def async_step_cover_add(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Append one validated cover row with a fresh identity."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                rows = _entry_cover_rows(entry)
                existing = _sibling_channel_sets(entry)
            except ValueError:
                errors = {"base": "invalid_config"}
            else:
                cover, errors = _validate_cover_input(user_input, existing)
                if cover is not None:
                    rows.append(
                        {
                            CONF_COVER_ID: ulid_now(),
                            **cover.as_dict(),
                        }
                    )
                    return self._update_covers_and_abort(rows, "cover_added")
        return self.async_show_form(
            step_id="cover_add",
            data_schema=self.add_suggested_values_to_schema(
                _cover_schema(None),
                user_input or {},
            ),
            errors=errors,
        )

    async def async_step_cover_pick_edit(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose one cover row to edit by stable identity."""
        try:
            rows = _entry_cover_rows(self._get_reconfigure_entry())
        except ValueError:
            return self.async_abort(reason="invalid_config")
        errors: dict[str, str] = {}
        if user_input is not None:
            cover_id = str(user_input.get(CONF_COVER_ID, ""))
            try:
                _find_cover_row(rows, cover_id)
            except ValueError:
                errors[CONF_COVER_ID] = "cover_not_found"
            else:
                self._cover_id = cover_id
                return await self.async_step_cover_edit()
        return self.async_show_form(
            step_id="cover_pick_edit",
            data_schema=_cover_picker_schema(rows),
            errors=errors,
        )

    async def async_step_cover_edit(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Merge validated cover fields into the selected stored row."""
        if self._cover_id is None:
            return await self.async_step_cover_pick_edit()
        entry = self._get_reconfigure_entry()
        try:
            rows = _entry_cover_rows(entry)
            index, stored = _find_cover_row(rows, self._cover_id)
        except ValueError:
            return self.async_abort(reason="cover_not_found")
        errors: dict[str, str] = {}
        suggested: Mapping[str, object] = _cover_display_values(
            self._cover_id,
            stored,
            str(stored.get(CONF_NAME, "")),
        )
        if user_input is not None:
            merged = dict(user_input)
            for key in (CONF_TRAVEL_UP, CONF_TRAVEL_DOWN):
                stored_value = stored.get(key)
                if key not in merged and stored_value not in (None, ""):
                    merged[key] = stored_value
            try:
                existing = _sibling_channel_sets(
                    entry,
                    exclude_cover_id=self._cover_id,
                )
            except ValueError:
                errors = {"base": "invalid_config"}
            else:
                cover, errors = _validate_cover_input(merged, existing)
                if cover is not None:
                    rows[index] = {
                        **stored,
                        **cover.as_dict(),
                        CONF_COVER_ID: self._cover_id,
                    }
                    return self._update_covers_and_abort(rows, "cover_updated")
            suggested = user_input
        return self.async_show_form(
            step_id="cover_edit",
            data_schema=self.add_suggested_values_to_schema(
                _cover_schema(None),
                suggested,
            ),
            errors=errors,
        )

    async def async_step_cover_pick_remove(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose one cover row to remove by stable identity."""
        entry = self._get_reconfigure_entry()
        try:
            rows = _entry_cover_rows(entry)
        except ValueError:
            return self.async_abort(reason="invalid_config")
        errors: dict[str, str] = {}
        if user_input is not None:
            cover_id = str(user_input.get(CONF_COVER_ID, ""))
            try:
                _find_cover_row(rows, cover_id)
                refusal = _cover_removal_refusal(entry, cover_id)
            except _COERCION_ERRORS:
                errors[CONF_COVER_ID] = "cover_not_found"
            else:
                if refusal is not None:
                    return self.async_abort(reason=refusal)
                self._cover_id = cover_id
                return await self.async_step_cover_remove_confirm()
        return self.async_show_form(
            step_id="cover_pick_remove",
            data_schema=_cover_picker_schema(rows),
            errors=errors,
        )

    async def async_step_cover_remove_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Delete the selected entity row, then remove its stored cover row."""
        if self._cover_id is None:
            return await self.async_step_cover_pick_remove()
        entry = self._get_reconfigure_entry()
        try:
            rows = _entry_cover_rows(entry)
            index, stored = _find_cover_row(rows, self._cover_id)
            refusal = _cover_removal_refusal(entry, self._cover_id)
        except _COERCION_ERRORS:
            return self.async_abort(reason="cover_not_found")
        if refusal is not None:
            return self.async_abort(reason=refusal)
        if user_input is not None:
            from homeassistant.helpers import entity_registry as er

            ent_reg = er.async_get(self.hass)
            entity_id = ent_reg.async_get_entity_id(
                "cover",
                DOMAIN,
                self._cover_id,
            )
            if (
                entity_id is not None
                and (reg_entry := ent_reg.async_get(entity_id)) is not None
                and reg_entry.config_entry_id == entry.entry_id
            ):
                ent_reg.async_remove(entity_id)
            rows.pop(index)
            return self._update_covers_and_abort(rows, "cover_removed")
        return self.async_show_form(
            step_id="cover_remove_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": str(stored.get(CONF_NAME, "")),
                "cover_id": self._cover_id,
            },
        )

    async def async_step_reconfigure_learn(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Reuse guided capture to replace only the remote identity."""
        del user_input
        current = RemoteConfig.from_entry(self._get_reconfigure_entry().data)
        self._learn_suggested = {
            CONF_NAME: current.name,
            CONF_AREA_ID: current.area_id,
        }
        self._learn_registry = None
        return await self.async_step_learn_setup()

    async def async_step_reconfigure_apply(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Apply a captured identity while preserving remote settings."""
        del user_input
        if self._identity is None:
            return await self.async_step_reconfigure_learn()
        entry = self._get_reconfigure_entry()
        current = RemoteConfig.from_entry(entry.data)
        updated = RemoteConfig(
            name=self._learn_name if self._learn_name is not None else current.name,
            remote=self._identity,
            area_id=(self._learn_area_id if self._learn_area_id is not None else current.area_id),
            repeats=current.repeats,
            coalesce_window_ms=current.coalesce_window_ms,
            cover_rows=current.cover_rows,
        )
        if any(
            other.entry_id != entry.entry_id and other.unique_id == updated.key
            for other in self.hass.config_entries.async_entries(DOMAIN)
        ):
            return self.async_abort(reason="already_configured")
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, RemoteRuntime):
            # Drain first: a queued-unpublished old-identity frame must not
            # slip onto the air between the disarm and the reload. Then
            # disarm bridge-held state (acknowledged, bounded await; the
            # request keeps retrying in the background until the real STOP
            # window closes).
            runtime.hub.drain_owner(entry.entry_id)
            await runtime.hub.async_disarm_remote(current.key)
        # The remote device is keyed by the remote identity, and a relearn is
        # the one flow that changes that identity on an EXISTING entry. Re-key
        # in place before the entry update: otherwise the reload finds neither
        # the new key nor the retired entry-id key, mints a fresh device, and
        # the old one is pruned once its covers re-home -- churning the
        # device_id every automation targets and silently dropping the user's
        # area override with it.
        _rekey_remote_device(self.hass, current.key, updated.key)
        self.hass.config_entries.async_update_entry(
            entry,
            title=updated.name,
            unique_id=updated.key,
            data=updated.as_dict(),
        )
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_reconfigure_edit(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit remote settings and calibration without changing identity."""
        entry = self._get_reconfigure_entry()
        current = RemoteConfig.from_entry(entry.data)
        errors: dict[str, str] = {}
        suggested: Mapping[str, object] = current.as_dict()
        if user_input is not None:
            try:
                flattened = _flatten_details(user_input)
                suggested = flattened
                raw_trailer = str(flattened.get(CONF_BASE_TRAILER, "")).strip()
                identity = RemoteIdentity(
                    prefix=current.remote.prefix,
                    remote_id=current.remote.remote_id,
                    bases=CommandBases(
                        up=parse_hex(flattened.get(CONF_BASE_UP), CONF_BASE_UP, 16),
                        down=parse_hex(flattened.get(CONF_BASE_DOWN), CONF_BASE_DOWN, 16),
                        stop=parse_hex(flattened.get(CONF_BASE_STOP), CONF_BASE_STOP, 16),
                        trailer=(
                            parse_hex(raw_trailer, CONF_BASE_TRAILER, 16) if raw_trailer else None
                        ),
                    ),
                )
                updated = RemoteConfig(
                    name=str(flattened.get(CONF_NAME, "")),
                    remote=identity,
                    area_id=str(flattened.get(CONF_AREA_ID, "")),
                    repeats=whole_number(flattened.get(CONF_REPEATS), CONF_REPEATS),
                    coalesce_window_ms=whole_number(
                        flattened.get(
                            CONF_COALESCE_WINDOW_MS,
                            DEFAULT_COALESCE_WINDOW_MS,
                        ),
                        CONF_COALESCE_WINDOW_MS,
                    ),
                    cover_rows=current.cover_rows,
                )
            except _COERCION_ERRORS:
                errors["base"] = "invalid_config"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    title=updated.name,
                    data=updated.as_dict(),
                )
                return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure_edit",
            data_schema=_reconfigure_edit_schema(suggested),
            errors=errors,
        )

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer guided learning before the Advanced fallback paths."""
        del user_input
        return self.async_show_menu(step_id="user", menu_options=["learn", "advanced"])

    async def async_step_learn(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Enter guided learning from the top-level menu."""
        del user_input
        self._learn_suggested = {}
        return await self.async_step_learn_setup()

    async def async_step_learn_setup(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Discover online bridges and collect capture routing details."""
        if self._learn_registry is None:
            self._learn_registry = await self._async_discover_bridges()
        if self._learn_registry is None:
            return await self.async_step_learn_unavailable()

        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_NAME, "")).strip()
            area_id = str(user_input.get(CONF_AREA_ID, "")).strip()
            bridge_id = str(user_input.get(CONF_BRIDGE, "")).strip()
            if not name or not area_id:
                errors["base"] = "invalid_config"
            else:
                try:
                    if bridge_id == _AUTOMATIC_BRIDGE:
                        bridge_id = self._learn_registry.resolve(area_id).bridge_id
                    else:
                        self._learn_registry.online_bridge(bridge_id)
                except NoOnlineBridgeError:
                    errors[CONF_BRIDGE] = "bridge_unavailable"
                else:
                    self._learn_name = name
                    self._learn_area_id = area_id
                    self._learn_bridge = bridge_id
                    self._captures = {}
                    self._learn_action = _LEARN_ACTIONS[0]
                    self._learn_suggested = {
                        **(self._learn_suggested or {}),
                        CONF_NAME: name,
                        CONF_AREA_ID: area_id,
                        CONF_BRIDGE: bridge_id,
                    }
                    return await self.async_step_learn_sniff()

        suggested: Mapping[str, object] | None = self._learn_suggested
        if user_input is not None:
            suggested = user_input
        return self.async_show_form(
            step_id="learn_setup",
            data_schema=_learn_setup_schema(self._learn_registry, suggested),
            errors=errors,
        )

    def _learn_failure_menu_options(
        self,
        retry_step: str,
        *,
        offer_derive: bool = False,
    ) -> list[str]:
        """Return failure recovery paths appropriate to the flow source."""
        menu_options = [retry_step]
        if offer_derive and self._captures:
            # Something was already measured, so the derivation fallback is
            # available -- and only here, where the user has demonstrably run
            # out of buttons the wizard can hear.
            menu_options.append("learn_derive")
        if self.source != config_entries.SOURCE_RECONFIGURE:
            menu_options.append("advanced")
        return menu_options

    async def async_step_learn_unavailable(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer retry and Advanced when flow-local MQTT discovery fails."""
        del user_input
        self._learn_registry = None
        return self.async_show_menu(
            step_id="learn_unavailable",
            menu_options=self._learn_failure_menu_options("learn_setup"),
        )

    async def async_step_learn_sniff(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start one capture task, then report its transition when re-invoked."""
        del user_input
        if self._sniff_task is not None and self._sniff_task.done():
            outcome = "timeout" if self._sniff_task.cancelled() else self._sniff_task.result()
            self._sniff_task = None
            next_step = "learn_next" if outcome == "captured" else "learn_timeout"
            return self.async_show_progress_done(next_step_id=next_step)

        if self._sniff_task is None:
            session_id = secrets.token_hex(16)
            self._sniff_session_id = session_id
            self._sniff_task = self.hass.async_create_task(
                self._async_capture(session_id),
                f"{DOMAIN} learn capture",
            )

        return self.async_show_progress(
            step_id="learn_sniff",
            progress_action="sniffing",
            progress_task=self._sniff_task,
            description_placeholders={
                "action": self._learn_action,
                "bridge": self._learn_bridge or "",
                "seconds": str(DEFAULT_SNIFF_WINDOW_SECONDS),
            },
        )

    async def async_step_learn_retry(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Invalidate this action's capture before starting a fresh attempt."""
        del user_input
        self._sniff_session_id = None
        self._sniff_task = None
        if self._captures is not None:
            self._captures.pop(self._learn_action, None)
        return await self.async_step_learn_sniff()

    async def async_step_learn_recapture(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Discard the action just measured and capture that one again.

        Distinct from learn_retry, which always targets the action currently
        being prompted: by the time learn_next is on screen the prompt has
        already advanced, so retrying there would silently re-arm the NEXT
        button instead of the one the user is unhappy with.
        """
        del user_input
        if self._learn_captured is not None:
            self._learn_action = self._learn_captured
        return await self.async_step_learn_retry()

    async def async_step_learn_next(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Report the action just measured and prompt for the next one."""
        del user_input
        captures = self._captures
        if not captures:
            return await self.async_step_learn_timeout()
        remaining = [action for action in _LEARN_ACTIONS if action not in captures]
        if not remaining:
            return await self.async_step_learn_confirm()
        captured = self._learn_captured = self._learn_action
        self._learn_action = remaining[0]
        # No `learn_derive` here. This is the SUCCESS path -- a button was just
        # measured and the next one is being asked for -- and offering
        # extrapolation from it makes derivation an ordinary way to finish
        # onboarding rather than the fallback #26 requires. Taking it after UP
        # invents DOWN and STOP from an opcode table that held for only 10 of
        # the 11 remotes surveyed, and the eleventh's invented UP frame was
        # transmitted, heard by five bridges, and ignored by the motor.
        #
        # A user who genuinely cannot capture a button still reaches it: press
        # the button we ask for, let the window time out, and `learn_timeout`
        # offers derivation because a capture attempt has demonstrably failed.
        return self.async_show_menu(
            step_id="learn_next",
            menu_options=["learn_sniff", "learn_recapture"],
            description_placeholders={
                "captured": captured,
                "measured": ", ".join(captures),
                "action": self._learn_action,
            },
        )

    async def async_step_learn_derive(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Stop capturing and complete the uncaptured bases by extrapolation."""
        del user_input
        return await self.async_step_learn_confirm()

    async def async_step_learn_timeout(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer a fresh capture attempt, derivation, or Advanced fallbacks."""
        del user_input
        return self.async_show_menu(
            step_id="learn_timeout",
            menu_options=self._learn_failure_menu_options("learn_retry", offer_derive=True),
            description_placeholders={"action": self._learn_action},
        )

    async def async_step_learn_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Summarize the calibration without exposing raw capture hex."""
        del user_input
        captures = self._captures
        if not captures:
            return await self.async_step_learn_timeout()
        try:
            self._identity, derived = _remote_identity_from_captures(captures)
        except ValueError:
            return await self.async_step_learn_timeout()
        reference = next(iter(captures.values()))
        menu_options = ["remote_settings", "learn_retry", "advanced"]
        if self.source == config_entries.SOURCE_RECONFIGURE:
            menu_options = ["reconfigure_apply", "learn_retry"]
        return self.async_show_menu(
            step_id="learn_confirm",
            menu_options=menu_options,
            description_placeholders={
                "prefix": f"0x{reference.prefix:06x}",
                "remote_id": f"0x{reference.remote_id:02x}",
                "channels": ",".join(map(str, reference.channels)),
                "measured": ", ".join(captures),
                # Named explicitly rather than silently substituted: a derived
                # base is a guess from a table that is wrong for some real
                # remotes, and the user is the only one who can test it (#26).
                "derived": ", ".join(derived) or "none",
                "name": self._learn_name or "",
                "bridge": self._learn_bridge or "",
            },
        )

    async def async_step_advanced(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer manual and virtual identity paths."""
        self._captures = None
        self._sniff_session_id = None
        del user_input
        return self.async_show_menu(
            step_id="advanced",
            menu_options=["manual", "virtual"],
        )

    async def async_step_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect an identity plus one calibration source."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._identity = _remote_identity_from_manual(user_input)
            except ValueError:
                errors["base"] = "invalid_config"
            else:
                return await self.async_step_remote_settings()
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(user_input),
            errors=errors,
        )

    async def async_step_remote_settings(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Name the remote, choose its area, and confirm transport settings."""
        if self._identity is None:
            return await self.async_step_user()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                flattened = _flatten_details(user_input)
                remote = RemoteConfig(
                    name=str(flattened.get(CONF_NAME, "")),
                    remote=self._identity,
                    area_id=str(flattened.get(CONF_AREA_ID, "")),
                    repeats=whole_number(flattened.get(CONF_REPEATS), CONF_REPEATS),
                    coalesce_window_ms=whole_number(
                        flattened.get(
                            CONF_COALESCE_WINDOW_MS,
                            DEFAULT_COALESCE_WINDOW_MS,
                        ),
                        CONF_COALESCE_WINDOW_MS,
                    ),
                )
            except _COERCION_ERRORS:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(remote.key)
                self._abort_if_unique_id_configured()
                self._remote = remote
                self._covers = []
                return await self.async_step_cover()
        suggested: Mapping[str, object] | None = self._learn_suggested
        if user_input is not None:
            with suppress(TypeError, ValueError):
                suggested = _flatten_details(user_input)
        return self.async_show_form(
            step_id="remote_settings",
            data_schema=_remote_settings_schema(suggested),
            errors=errors,
        )

    async def async_step_cover(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect one cover: name, channels, and leaf travel times."""
        if self._remote is None or self._covers is None:
            return await self.async_step_user()
        errors: dict[str, str] = {}
        if user_input is not None:
            cover, errors = _validate_cover_input(
                user_input,
                [cover_config.channels for cover_config in self._covers],
            )
            if cover is not None:
                self._covers.append(cover)
                return await self.async_step_cover_menu()
        suggested: dict[str, object] = {}
        if not self._covers and self._captures:
            reference = next(iter(self._captures.values()))
            suggested[CONF_CHANNELS] = ",".join(map(str, reference.channels))
        if user_input is not None:
            suggested = dict(user_input)
        data_schema = _cover_schema(suggested)
        if user_input is not None:
            data_schema = self.add_suggested_values_to_schema(
                _cover_schema(None),
                suggested,
            )
        return self.async_show_form(
            step_id="cover",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"count": str(len(self._covers))},
        )

    async def async_step_cover_menu(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer another cover or finishing the remote."""
        del user_input
        return self.async_show_menu(
            step_id="cover_menu",
            menu_options=["cover", "finish"],
            description_placeholders={"count": str(len(self._covers or []))},
        )

    async def async_step_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create the remote entry with data-backed covers."""
        del user_input
        remote = self._remote
        covers = self._covers
        if remote is None or not covers:
            return await self.async_step_user()
        # Final whole-list backstop: flow-state replay could bypass the
        # per-iteration channel checks.
        for index, cover in enumerate(covers):
            others = [c.channels for i, c in enumerate(covers) if i != index]
            if laminar_conflict(cover.channels, others) is not None:
                return self.async_abort(reason="channel_conflict")
        await self.async_set_unique_id(remote.key)
        self._abort_if_unique_id_configured()
        cover_rows = [{CONF_COVER_ID: ulid_now(), **cover.as_dict()} for cover in covers]
        return self.async_create_entry(
            title=remote.name,
            data={**remote.as_dict(), CONF_COVERS: cover_rows},
        )

    async def async_step_virtual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allocate a calibrated virtual identity before the wizard."""
        del user_input
        from . import new_virtual_remote_identity

        prefix, remote_id, bases = new_virtual_remote_identity(self.hass)
        self._identity = RemoteIdentity(
            prefix=prefix,
            remote_id=remote_id,
            bases=bases,
        )
        return await self.async_step_remote_settings()

    async def _async_discover_bridges(self) -> BridgeRegistry | None:
        """Collect retained discovery state without relying on a loaded hub."""
        from homeassistant.components import mqtt

        session = _DiscoverySession(BridgeRegistry())
        unsubscribers: list[Unsubscriber] = []
        try:
            async with asyncio.timeout(_MQTT_BOOTSTRAP_TIMEOUT_SECONDS):
                if not await mqtt.async_wait_for_mqtt_client(self.hass):
                    return None
                unsubscribers.append(
                    await _async_subscribe_ready(
                        self.hass,
                        MQTT_AVAILABILITY_TOPIC,
                        functools.partial(_handle_flow_availability, session),
                    )
                )
                unsubscribers.append(
                    await _async_subscribe_ready(
                        self.hass,
                        MQTT_INFO_TOPIC,
                        functools.partial(_handle_flow_info, session),
                    )
                )
                await asyncio.sleep(_BRIDGE_DISCOVERY_SECONDS)
        except TimeoutError:
            return None
        except Exception:
            _LOGGER.debug("Flow-local MQTT bridge discovery failed", exc_info=True)
            return None
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()

        if not any(bridge.online for bridge in session.registry.bridges):
            return None
        return session.registry

    def _store_capture(
        self,
        session_id: str,
        capture: _LearnCapture,
    ) -> Literal["captured", "timeout"]:
        """Record one measured action unless its attempt was already retired."""
        if self._sniff_session_id != session_id or self._captures is None:
            return "timeout"
        self._captures[capture.button] = capture
        return "captured"

    async def _async_capture(self, session_id: str) -> Literal["captured", "timeout"]:
        """Capture one action and always release/stop the bridge sniff session."""
        from homeassistant.components import mqtt

        bridge = self._learn_bridge
        captures = self._captures
        if bridge is None or captures is None:
            return "timeout"
        owner_key = (id(self.hass), bridge)
        if owner_key in _CAPTURE_OWNERS:
            if self._sniff_session_id == session_id:
                self._sniff_session_id = None
            return "timeout"
        _CAPTURE_OWNERS[owner_key] = session_id
        rx_topic = f"{MQTT_ROOT}/{bridge}/rx"
        command_topic = MQTT_CMD_TEMPLATE.format(bridge=bridge)
        attempt = _SniffAttempt(
            action=self._learn_action,
            measured=dict(captures),
            future=self.hass.loop.create_future(),
        )
        capture_future = attempt.future
        unsubscribe: Unsubscriber | None = None
        try:
            async with asyncio.timeout(_CAPTURE_TIMEOUT_SECONDS):
                async with asyncio.timeout(_MQTT_BOOTSTRAP_TIMEOUT_SECONDS):
                    if not await mqtt.async_wait_for_mqtt_client(self.hass):
                        return "timeout"
                    unsubscribe = await _async_subscribe_ready(
                        self.hass,
                        rx_topic,
                        functools.partial(
                            _handle_sniff_message,
                            self,
                            session_id,
                            rx_topic,
                            attempt,
                        ),
                    )
                    await mqtt.async_publish(
                        self.hass,
                        command_topic,
                        json.dumps(
                            {
                                MQTT_CMD_FIELD_ACTION: MQTT_CMD_ACTION_SNIFF,
                                MQTT_CMD_FIELD_SECONDS: DEFAULT_SNIFF_WINDOW_SECONDS,
                            },
                            separators=(",", ":"),
                        ),
                        qos=1,
                        retain=False,
                    )
                capture = await capture_future
            return self._store_capture(session_id, capture)
        except TimeoutError:
            # An unrecognised opcode cannot end the window early: nothing
            # distinguishes it from the OEM trailer burst until the window
            # closes with no recognised frame for this action. Only then is
            # the held candidate the best evidence we have (#26).
            if attempt.unrecognized is None:
                return "timeout"
            return self._store_capture(session_id, attempt.unrecognized)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Flow-local MQTT sniff failed", exc_info=True)
            return "timeout"
        finally:
            if self._sniff_session_id == session_id:
                self._sniff_session_id = None
            if unsubscribe is not None:
                unsubscribe()
            if not capture_future.done():
                capture_future.cancel()
            stop_task = self.hass.async_create_task(
                mqtt.async_publish(
                    self.hass,
                    command_topic,
                    json.dumps(
                        {
                            MQTT_CMD_FIELD_ACTION: MQTT_CMD_ACTION_SNIFF,
                            MQTT_CMD_FIELD_SECONDS: 0,
                        },
                        separators=(",", ":"),
                    ),
                    qos=1,
                    retain=False,
                ),
                f"{DOMAIN} learn sniff stop",
            )
            stop_task.add_done_callback(
                functools.partial(_release_capture_owner, owner_key, session_id)
            )
            try:
                with suppress(Exception):
                    await asyncio.shield(stop_task)
            finally:
                if stop_task.done():
                    _release_capture_owner(owner_key, session_id, stop_task)

    @callback
    def async_remove(self) -> None:
        """Invalidate callbacks while HA cancels the registered progress task."""
        self._sniff_session_id = None
        super().async_remove()
