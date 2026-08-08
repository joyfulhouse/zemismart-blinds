"""Config and options flows for adding one Zemismart blind/group at a time.

Schema builders and row validation live in ``config_flow_schema``; the MQTT
Learn session lives in ``learn_session``. Helpers re-exported here exist for
import compatibility — patch the module that owns the code under test.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
import time
from collections.abc import Iterable as Iterable
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass as dataclass
from dataclasses import field
from typing import TYPE_CHECKING, Any, Final, Literal

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section as section
from homeassistant.helpers import selector as selector
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
from .config_flow_schema import (
    MEASURE_REQUESTED as MEASURE_REQUESTED,
)
from .config_flow_schema import (
    _cover_display_values as _cover_display_values,
)
from .config_flow_schema import (
    _cover_picker_schema as _cover_picker_schema,
)
from .config_flow_schema import (
    _cover_removal_refusal as _cover_removal_refusal,
)
from .config_flow_schema import (
    _cover_schema as _cover_schema,
)
from .config_flow_schema import (
    _entry_cover_rows as _entry_cover_rows,
)
from .config_flow_schema import (
    _find_cover_row as _find_cover_row,
)
from .config_flow_schema import (
    _flatten_details as _flatten_details,
)
from .config_flow_schema import (
    _float_value as _float_value,
)
from .config_flow_schema import (
    _int_value as _int_value,
)
from .config_flow_schema import (
    _learn_setup_schema as _learn_setup_schema,
)
from .config_flow_schema import (
    _manual_schema as _manual_schema,
)
from .config_flow_schema import (
    _measure_confirm_schema as _measure_confirm_schema,
)
from .config_flow_schema import (
    _measure_setup_schema as _measure_setup_schema,
)
from .config_flow_schema import (
    _reconfigure_edit_schema as _reconfigure_edit_schema,
)
from .config_flow_schema import (
    _remote_settings_schema as _remote_settings_schema,
)
from .config_flow_schema import (
    _row_cover_id as _row_cover_id,
)
from .config_flow_schema import (
    _sibling_channel_sets as _sibling_channel_sets,
)
from .config_flow_schema import (
    _validate_cover_input as _validate_cover_input,
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
    TRAVEL_ARM_TIMEOUT_SECONDS,
    TRAVEL_REARM_INTERVAL_SECONDS,
    TRAVEL_RUN_TIMEOUT_SECONDS,
)
from .const import (
    DEFAULT_REPEATS as DEFAULT_REPEATS,
)
from .learn_session import (
    _async_subscribe_ready as _async_subscribe_ready,
)
from .learn_session import (
    _bridge_id as _bridge_id,
)
from .learn_session import (
    _DiscoverySession as _DiscoverySession,
)
from .learn_session import (
    _handle_flow_availability as _handle_flow_availability,
)
from .learn_session import (
    _handle_flow_info as _handle_flow_info,
)
from .learn_session import (
    _LearnCapture as _LearnCapture,
)
from .learn_session import (
    _payload_text as _payload_text,
)
from .learn_session import (
    _remote_identity_from_captures as _remote_identity_from_captures,
)
from .learn_session import (
    _SniffAttempt as _SniffAttempt,
)
from .models import (
    BridgeRegistry,
    CoverConfig,
    NoOnlineBridgeError,
    RemoteConfig,
    RemoteIdentity,
    RemoteRuntime,
    laminar_conflict,
    parse_hex,
    whole_number,
)
from .models import (
    parse_channels as parse_channels,
)
from .travel_capture import DIRECTIONS, HeardPress, TravelMeasurement, TravelRun

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


__all__ = ["ZemismartBlindsConfigFlow"]

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
    recovery keeps the capture's opcode byte and inverts only the low byte,
    `(cmd & 0xFF00) | ((cmd - remote_id + group_offset(chans)) & 0xFF)`, which
    is action-independent -- so one physical frame yields one base whichever
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


@dataclass(slots=True)
class _PendingMeasure:
    """Where a travel measurement came from and where its result must go."""

    origin: Literal["wizard", "add", "edit"]
    name: str
    channels: tuple[int, ...]
    cover_id: str | None = None
    bridges: tuple[str, ...] = ()
    measured: dict[str, TravelMeasurement] = field(default_factory=dict)

    @property
    def wanted(self) -> frozenset[str]:
        """Return the directions still to be measured."""
        return frozenset(DIRECTIONS) - frozenset(self.measured)


@callback
def _handle_travel_message(
    flow: ZemismartBlindsConfigFlow,
    session_id: str,
    expected_topic: str,
    bridge_id: str,
    run: TravelRun,
    armed: asyncio.Event,
    future: asyncio.Future[TravelMeasurement],
    message: ReceiveMessage,
) -> None:
    """Offer one bridge's received frame to the shared travel run.

    Every subscribed bridge feeds the SAME run machine; ``bridge_id`` lets it
    time a run on one bridge's clock and absorb the other bridges' copies of
    the same physical press as burst repeats.
    """
    if (
        flow._sniff_session_id != session_id
        or future.done()
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
    # Checked before the run sees it: an automation driving this very cover
    # mid-measurement echoes back off the sniffing bridge looking exactly like
    # a human's press.
    if isinstance(frame, str) and _is_own_emission(flow.hass, frame):
        return
    measurement = run.offer_payload(decoded_payload, time.monotonic(), bridge_id)
    if run.started is not None:
        armed.set()
    if measurement is not None:
        future.set_result(measurement)


def _sniff_command(seconds: int) -> str:
    """Serialize one bridge sniff-window command."""
    return json.dumps(
        {
            MQTT_CMD_FIELD_ACTION: MQTT_CMD_ACTION_SNIFF,
            MQTT_CMD_FIELD_SECONDS: seconds,
        },
        separators=(",", ":"),
    )


async def _async_hold_sniff_open(hass: HomeAssistant, command_topic: str) -> None:
    """Keep one bridge's bounded sniff window open for a whole run.

    The firmware's ``start_sniff`` takes the LATER of its current and candidate
    deadlines rather than replacing it, so re-publishing extends the window.
    That is the only way to measure a run longer than the command contract's
    60-second cap.

    One holder task per bridge: a failed publish -- a broker hiccup, or a
    bridge that dropped off mid-run -- keeps this bridge's loop trying and
    never touches the other bridges' holds.
    """
    from homeassistant.components import mqtt

    while True:
        try:
            await mqtt.async_publish(
                hass,
                command_topic,
                _sniff_command(DEFAULT_SNIFF_WINDOW_SECONDS),
                qos=1,
                retain=False,
            )
        except Exception:
            _LOGGER.debug("Sniff re-arm publish to %s failed", command_topic, exc_info=True)
        await asyncio.sleep(TRAVEL_REARM_INTERVAL_SECONDS)


@dataclass(slots=True)
class _SniffChannel:
    """One bridge's share of a fleet-wide sniff session."""

    bridge_id: str
    owner_key: tuple[int, str]
    command_topic: str
    unsubscribe: Unsubscriber | None = None
    holder: asyncio.Task[None] | None = None


async def _async_stop_sniff_channels(
    hass: HomeAssistant,
    session_id: str,
    channels: Iterable[_SniffChannel],
) -> None:
    """Publish a sniff stop to every armed bridge, releasing each afterwards.

    Ownership is released only once a bridge's stop publication finished (the
    existing single-bridge discipline, per channel): releasing earlier would
    let a new session arm a window this one is still about to close.
    """
    from homeassistant.components import mqtt

    stops: list[tuple[_SniffChannel, asyncio.Task[None]]] = []
    for channel in channels:
        stop_task = hass.async_create_task(
            mqtt.async_publish(
                hass,
                channel.command_topic,
                _sniff_command(0),
                qos=1,
                retain=False,
            ),
            f"{DOMAIN} sniff stop",
        )
        stop_task.add_done_callback(
            functools.partial(_release_capture_owner, channel.owner_key, session_id)
        )
        stops.append((channel, stop_task))
    try:
        with suppress(Exception):
            await asyncio.shield(asyncio.gather(*(task for _channel, task in stops)))
    finally:
        for channel, stop_task in stops:
            if stop_task.done():
                _release_capture_owner(channel.owner_key, session_id, stop_task)


def _claim_sniff_channels(
    hass: HomeAssistant,
    session_id: str,
    bridges: tuple[str, ...],
) -> list[_SniffChannel]:
    """Claim every free target bridge for one sniff session.

    A bridge another session already owns is excluded rather than fatal: a
    Learn sniff holding one bridge no longer blocks a fleet-wide measurement,
    it just narrows it. Zero claims is the caller's failure case.
    """
    channels: list[_SniffChannel] = []
    for bridge in bridges:
        owner_key = (id(hass), bridge)
        if owner_key in _CAPTURE_OWNERS:
            continue
        _CAPTURE_OWNERS[owner_key] = session_id
        channels.append(
            _SniffChannel(
                bridge_id=bridge,
                owner_key=owner_key,
                command_topic=MQTT_CMD_TEMPLATE.format(bridge=bridge),
            )
        )
    return channels


@dataclass(slots=True)
class _MeasureSession:
    """One armed travel-measurement listening session across the bridge fleet.

    Outlives a single progress task deliberately: the RX subscriptions and the
    sniff-holds must span both phases of a run (waiting for the direction
    press, then waiting for its STOP), so their lifetime lives here rather
    than in either task. ``closed`` makes teardown idempotent -- both phase
    tasks and every abandon path may try to close it.

    ``run``, ``armed`` and ``future`` are shared across every channel: one
    machine fed by all bridges is what deduplicates a press heard many times.
    """

    session_id: str
    channels: list[_SniffChannel]
    run: TravelRun
    armed: asyncio.Event
    future: asyncio.Future[TravelMeasurement]
    closed: bool = False


class ZemismartBlindsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add exactly one blind or group device per config entry."""

    VERSION = 3

    _captures: dict[str, _LearnCapture] | None = None
    _cover_id: str | None = None
    _covers: list[CoverConfig] | None = None
    _identity: RemoteIdentity | None = None
    _learn_action: str = _LEARN_ACTIONS[0]
    _learn_area_id: str | None = None
    _learn_captured: str | None = None
    _learn_bridges: tuple[str, ...] = ()
    _learn_name: str | None = None
    _learn_registry: BridgeRegistry | None = None
    _learn_suggested: dict[str, object] | None = None
    _remote: RemoteConfig | None = None
    _sniff_session_id: str | None = None
    _sniff_task: asyncio.Task[Literal["captured", "timeout"]] | None = None
    _pending_measure: _PendingMeasure | None = None
    _measure_session: _MeasureSession | None = None
    _measure_heard: tuple[HeardPress, ...] = ()
    _measure_task: asyncio.Task[TravelMeasurement | str] | None = None
    _measure_outcome: str | None = None
    _measure_error: str | None = None
    _identity_is_virtual: bool = False

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
        errors: dict[str, str] = self._consume_measure_error()
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
                if errors.get("base") == MEASURE_REQUESTED:
                    if self._measure_identity() is None:
                        errors = {"base": "travel_required"}
                    else:
                        self._pending_measure = _PendingMeasure(
                            origin="add",
                            name=str(user_input.get(CONF_NAME, "")).strip(),
                            channels=parse_channels(user_input.get(CONF_CHANNELS, "")),
                        )
                        return await self.async_step_cover_measure_setup()
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
                return await self.async_step_cover_edit_menu()
        return self.async_show_form(
            step_id="cover_pick_edit",
            data_schema=_cover_picker_schema(rows),
            errors=errors,
        )

    async def async_step_cover_edit_menu(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer editing this cover's fields or measuring its travel times."""
        del user_input
        if self._cover_id is None:
            return await self.async_step_cover_pick_edit()
        return self.async_show_menu(
            step_id="cover_edit_menu",
            menu_options=["cover_edit", "cover_measure_start"],
        )

    async def async_step_cover_measure_start(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Begin measuring the already-chosen cover."""
        del user_input
        cover_id = self._cover_id
        if cover_id is None:
            return await self.async_step_cover_pick_edit()
        entry = self._get_reconfigure_entry()
        try:
            _index, stored = _find_cover_row(_entry_cover_rows(entry), cover_id)
            cover = CoverConfig.from_stored(cover_id, stored)
        except _COERCION_ERRORS:
            return self.async_abort(reason="cover_not_found")
        if self._measure_identity() is None:
            return self.async_abort(reason="invalid_config")
        self._pending_measure = _PendingMeasure(
            origin="edit",
            name=cover.name,
            channels=cover.channels,
            cover_id=cover_id,
        )
        return await self.async_step_cover_measure_setup()

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
        errors: dict[str, str] = self._consume_measure_error()
        suggested: Mapping[str, object] = _cover_display_values(
            self._cover_id,
            stored,
            str(stored.get(CONF_NAME, "")),
        )
        if user_input is not None:
            # No travel backfill from the stored row here, deliberately: this
            # form arrives pre-filled through _cover_display_values, so an
            # empty travel field is a user's deliberate clear -- the signal
            # that requests a measurement -- and restoring the stored value
            # would make that signal unreachable on the one form where a user
            # with a wrong travel time actually goes.
            try:
                existing = _sibling_channel_sets(
                    entry,
                    exclude_cover_id=self._cover_id,
                )
            except ValueError:
                errors = {"base": "invalid_config"}
            else:
                cover, errors = _validate_cover_input(user_input, existing)
                if cover is not None:
                    rows[index] = {
                        **stored,
                        **cover.as_dict(),
                        CONF_COVER_ID: self._cover_id,
                    }
                    return self._update_covers_and_abort(rows, "cover_updated")
                if errors.get("base") == MEASURE_REQUESTED:
                    if self._measure_identity() is None:
                        errors = {"base": "travel_required"}
                    else:
                        self._pending_measure = _PendingMeasure(
                            origin="edit",
                            name=str(user_input.get(CONF_NAME, "")).strip(),
                            channels=parse_channels(user_input.get(CONF_CHANNELS, "")),
                            cover_id=self._cover_id,
                        )
                        return await self.async_step_cover_measure_setup()
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
        failure = await self._async_swap_entry_identity(
            self._identity,
            name=self._learn_name,
            area_id=self._learn_area_id,
        )
        if failure is not None:
            return self.async_abort(reason=failure)
        return self.async_abort(reason="reconfigure_successful")

    async def _async_swap_entry_identity(
        self,
        identity: RemoteIdentity,
        *,
        name: str | None = None,
        area_id: str | None = None,
    ) -> str | None:
        """Replace the reconfigured entry's identity; None means it worked.

        The one procedure allowed to change a remote's identity on an
        EXISTING entry, shared by relearn and the measurement mismatch path
        so neither can skip the drain/disarm/re-key discipline.
        """
        entry = self._get_reconfigure_entry()
        current = RemoteConfig.from_entry(entry.data)
        updated = RemoteConfig(
            name=name if name is not None else current.name,
            remote=identity,
            area_id=area_id if area_id is not None else current.area_id,
            repeats=current.repeats,
            coalesce_window_ms=current.coalesce_window_ms,
            cover_rows=current.cover_rows,
        )
        if any(
            other.entry_id != entry.entry_id and other.unique_id == updated.key
            for other in self.hass.config_entries.async_entries(DOMAIN)
        ):
            return "already_configured"
        runtime = getattr(entry, "runtime_data", None)
        if isinstance(runtime, RemoteRuntime):
            # Drain first: a queued-unpublished old-identity frame must not
            # slip onto the air between the disarm and the reload. Then
            # disarm bridge-held state (acknowledged, bounded await; the
            # request keeps retrying in the background until the real STOP
            # window closes).
            runtime.hub.drain_owner(entry.entry_id)
            await runtime.hub.async_disarm_remote(current.key)
        # The remote device is keyed by the remote identity. Re-key in place
        # before the entry update: otherwise the reload finds neither the new
        # key nor the retired entry-id key, mints a fresh device, and the old
        # one is pruned once its covers re-home -- churning the device_id
        # every automation targets and silently dropping the user's area
        # override with it.
        _rekey_remote_device(self.hass, current.key, updated.key)
        self.hass.config_entries.async_update_entry(
            entry,
            title=updated.name,
            unique_id=updated.key,
            data=updated.as_dict(),
        )
        return None

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
                    bridges = self._resolve_listen_bridges(self._learn_registry, bridge_id)
                except NoOnlineBridgeError:
                    errors[CONF_BRIDGE] = "bridge_unavailable"
                else:
                    self._learn_name = name
                    self._learn_area_id = area_id
                    self._learn_bridges = bridges
                    self._captures = {}
                    self._learn_action = _LEARN_ACTIONS[0]
                    # The RAW picker value, so re-showing the form round-trips
                    # "Automatic" instead of pinning a resolved bridge list.
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
                "bridge": ", ".join(self._learn_bridges),
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
                "bridge": ", ".join(self._learn_bridges),
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
        errors: dict[str, str] = self._consume_measure_error()
        if user_input is not None:
            cover, errors = _validate_cover_input(
                user_input,
                [cover_config.channels for cover_config in self._covers],
            )
            if cover is not None:
                self._covers.append(cover)
                return await self.async_step_cover_menu()
            if errors.get("base") == MEASURE_REQUESTED:
                if self._measure_identity() is None:
                    errors = {"base": "travel_required"}
                else:
                    self._pending_measure = _PendingMeasure(
                        origin="wizard",
                        name=str(user_input.get(CONF_NAME, "")).strip(),
                        channels=parse_channels(user_input.get(CONF_CHANNELS, "")),
                    )
                    return await self.async_step_cover_measure_setup()
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
        # Nothing physical transmits a synthesized identity, so travel
        # measurement can never hear this remote. Entries do not record
        # provenance, so this in-memory flag is the only place we know.
        self._identity_is_virtual = True
        return await self.async_step_remote_settings()

    async def async_step_cover_measure_setup(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose the bridge that will listen for this cover's run."""
        pending = self._pending_measure
        if pending is None or self._measure_identity() is None:
            return await self._async_measure_abandoned()
        if self._learn_registry is None:
            self._learn_registry = await self._async_discover_bridges()
        if self._learn_registry is None:
            # Flow-local MQTT discovery failed outright, so no bridge can
            # listen. Retrying cannot help inside this step; the cover form
            # still accepts typed times.
            return await self._async_measure_abandoned(error="measure_no_bridge")

        errors: dict[str, str] = {}
        if user_input is not None:
            bridge_id = str(user_input.get(CONF_BRIDGE, "")).strip()
            try:
                bridges = self._resolve_listen_bridges(self._learn_registry, bridge_id)
            except NoOnlineBridgeError:
                errors[CONF_BRIDGE] = "bridge_unavailable"
            else:
                pending.bridges = bridges
                return await self.async_step_cover_measure_run()

        return self.async_show_form(
            step_id="cover_measure_setup",
            data_schema=_measure_setup_schema(self._learn_registry, user_input),
            errors=errors,
            description_placeholders={"name": pending.name},
        )

    async def async_step_cover_measure_run(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Listen for the direction press; advance the moment one is heard.

        Phase 1 of a run. Its progress task completes as soon as a press of a
        wanted direction is identified, so the screen visibly reacts to the
        press instead of spinning silently until the STOP -- the field test
        in Kaelyn's bedroom showed a single opaque spinner gives the user no
        way to tell "measuring" from "nothing matched".
        """
        del user_input
        pending = self._pending_measure
        if pending is None:
            return await self._async_measure_abandoned()
        if self._measure_task is not None and self._measure_task.done():
            outcome = "failed" if self._measure_task.cancelled() else self._measure_task.result()
            self._measure_task = None
            if outcome == "armed":
                return self.async_show_progress_done(next_step_id="cover_measure_stop")
            self._measure_outcome = str(outcome)
            if outcome == "no_press" and self._measure_heard:
                return self.async_show_progress_done(next_step_id="cover_measure_mismatch")
            return self.async_show_progress_done(next_step_id="cover_measure_timeout")

        if self._measure_task is None:
            session_id = secrets.token_hex(16)
            self._sniff_session_id = session_id
            self._measure_heard = ()
            self._measure_task = self.hass.async_create_task(
                self._async_measure_arm(session_id),
                f"{DOMAIN} travel arm",
            )

        return self.async_show_progress(
            step_id="cover_measure_run",
            progress_action="measuring",
            progress_task=self._measure_task,
            description_placeholders={
                "name": pending.name,
                "bridge": ", ".join(pending.bridges),
                "wanted": " or ".join(sorted(pending.wanted)),
            },
        )

    async def async_step_cover_measure_stop(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Report the direction heard and wait for its STOP.

        Phase 2 of a run. The session (subscription + sniff hold) carries over
        from phase 1, so a STOP that arrives during the step transition still
        lands -- the run machine already holds it in the resolved future.
        """
        del user_input
        pending = self._pending_measure
        if pending is None:
            return await self._async_measure_abandoned()
        # Handle a completed task BEFORE consulting the session: the finish
        # task clears self._measure_session in its own finally, so by the time
        # this step is re-invoked with a result the session is already gone.
        if self._measure_task is not None and self._measure_task.done():
            outcome = "failed" if self._measure_task.cancelled() else self._measure_task.result()
            self._measure_task = None
            if isinstance(outcome, TravelMeasurement):
                pending.measured[outcome.direction] = outcome
                return self.async_show_progress_done(next_step_id="cover_measure_next")
            self._measure_outcome = str(outcome)
            return self.async_show_progress_done(next_step_id="cover_measure_timeout")

        session = self._measure_session
        if session is None:
            return await self._async_measure_abandoned()
        if self._measure_task is None:
            self._measure_task = self.hass.async_create_task(
                self._async_measure_finish(session),
                f"{DOMAIN} travel stop wait",
            )

        started = session.run.started
        return self.async_show_progress(
            step_id="cover_measure_stop",
            progress_action="measuring_stop",
            progress_task=self._measure_task,
            description_placeholders={
                "name": pending.name,
                "direction": started.button if started is not None else "",
            },
        )

    async def async_step_cover_measure_next(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Report the direction just measured and ask for the other one."""
        del user_input
        pending = self._pending_measure
        if pending is None or not pending.measured:
            return await self._async_measure_abandoned()
        if not pending.wanted:
            return await self.async_step_cover_measure_confirm()
        last = next(reversed(list(pending.measured.values())))
        return self.async_show_menu(
            step_id="cover_measure_next",
            menu_options=["cover_measure_run", "cover_measure_redo"],
            description_placeholders={
                "measured": last.direction,
                "seconds": f"{last.measured_seconds:.2f}",
                "stored": str(last.stored_seconds),
                "wanted": " or ".join(sorted(pending.wanted)),
            },
        )

    async def async_step_cover_measure_redo(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Discard the direction just measured and run it again."""
        del user_input
        pending = self._pending_measure
        if pending is None or not pending.measured:
            return await self._async_measure_abandoned()
        pending.measured.pop(next(reversed(list(pending.measured))), None)
        self._measure_task = None
        self._sniff_session_id = None
        return await self.async_step_cover_measure_run()

    async def async_step_cover_measure_timeout(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer another attempt or typing the times by hand."""
        del user_input
        pending = self._pending_measure
        if pending is None:
            return await self._async_measure_abandoned()
        self._measure_task = None
        self._sniff_session_id = None
        session = self._measure_session
        self._measure_session = None
        if session is not None:
            # Whichever phase task failed already closed it; idempotent.
            await self._async_measure_session_close(session)
        return self.async_show_menu(
            step_id="cover_measure_timeout",
            menu_options=["cover_measure_run", "cover_measure_manual"],
            description_placeholders={
                "reason": self._measure_outcome or "failed",
                "wanted": " or ".join(sorted(pending.wanted)),
            },
        )

    async def async_step_cover_measure_mismatch(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Nothing matched, but a real press was heard -- say which, offer to adopt.

        The two actionable rejections are a different remote identity and this
        remote on a different channel set. The first offers a one-click
        identity update (when the heard opcodes are inferable); the second
        names both channel sets so the user can move the selector.
        """
        del user_input
        pending = self._pending_measure
        heard = self._measure_heard
        identity = self._measure_identity()
        if pending is None or not heard or identity is None:
            return await self.async_step_cover_measure_timeout()
        self._measure_task = None
        self._sniff_session_id = None
        first = heard[0]
        heard_channels = ",".join(str(channel) for channel in first.channels)
        stored_channels = ",".join(str(channel) for channel in pending.channels)
        same_identity = (first.prefix, first.remote_id) == (identity.prefix, identity.remote_id)
        menu_options = ["cover_measure_run", "cover_measure_manual"]
        if same_identity:
            detail = (
                f"This device's own remote was heard, but on channels {heard_channels} -- "
                f"the cover being measured stores channels {stored_channels}. Move the "
                "remote's channel selector to match, or edit the cover's channels first."
            )
        else:
            detail = (
                f"A press from remote {first.prefix:06x}:{first.remote_id:02x} on channels "
                f"{heard_channels} was heard, but this device stores remote "
                f"{identity.prefix:06x}:{identity.remote_id:02x}. If the heard remote is the "
                "one that actually drives this shade, the device's stored remote can be "
                "updated to it."
            )
            if any(
                press.button is not None
                for press in heard
                if (press.prefix, press.remote_id) == (first.prefix, first.remote_id)
            ):
                menu_options.insert(0, "cover_measure_use_heard")
        return self.async_show_menu(
            step_id="cover_measure_mismatch",
            menu_options=menu_options,
            description_placeholders={"detail": detail},
        )

    async def async_step_cover_measure_use_heard(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Adopt the heard remote as this device's identity, then re-measure.

        Calibration is seeded from the presses actually heard during the
        arming window (the user was pressing UP/DOWN/STOP at the shade), with
        ``_remote_identity_from_captures`` deriving only the untouched
        buttons -- the same measured-first discipline the Learn wizard uses.
        """
        del user_input
        pending = self._pending_measure
        heard = self._measure_heard
        if pending is None or not heard:
            return await self.async_step_cover_measure_timeout()
        key = (heard[0].prefix, heard[0].remote_id)
        captures: dict[str, _LearnCapture] = {}
        for press in heard:
            if (press.prefix, press.remote_id) != key or press.button is None:
                continue
            if press.button in captures:
                continue
            captures[press.button] = _LearnCapture(
                frame=press.frame,
                prefix=press.prefix,
                remote_id=press.remote_id,
                channels=press.channels,
                command=press.command,
                button=press.button,
                inferred_button=press.button,
                base=derive_base(press.channels, press.button, press.command, press.remote_id),
            )
        try:
            identity, _derived = _remote_identity_from_captures(captures)
        except ValueError:
            return await self.async_step_cover_measure_timeout()
        self._measure_heard = ()
        if self.source == config_entries.SOURCE_RECONFIGURE:
            failure = await self._async_swap_entry_identity(identity)
            if failure is not None:
                return self.async_abort(reason=failure)
        else:
            self._identity = identity
            self._identity_is_virtual = False
        return await self.async_step_cover_measure_run()

    async def async_step_cover_measure_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show what was measured, let it be corrected, then store it.

        A form, not a menu: a config-flow form has one submit, so there is no
        second "measure again" button here. Correcting a mistimed run is done
        by typing the right value, and a full redo lives on cover_measure_next.
        """
        pending = self._pending_measure
        if pending is None or pending.wanted:
            return await self._async_measure_abandoned()
        measured = {
            direction: measurement.stored_seconds
            for direction, measurement in pending.measured.items()
        }
        if user_input is not None:
            return await self._async_store_measured_cover(pending, user_input)
        return self.async_show_form(
            step_id="cover_measure_confirm",
            data_schema=_measure_confirm_schema(measured),
            description_placeholders={
                "name": pending.name,
                "up": f"{pending.measured['UP'].measured_seconds:.2f}",
                "down": f"{pending.measured['DOWN'].measured_seconds:.2f}",
            },
        )

    async def async_step_cover_measure_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Abandon measurement and return to the cover form."""
        del user_input
        return await self._async_measure_abandoned()

    async def _async_store_measured_cover(
        self,
        pending: _PendingMeasure,
        user_input: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Write the confirmed cover back where the measurement came from.

        Dispatches on the recorded origin rather than on ``self.source``: the
        wizard and the reconfigure add path both persist differently, and
        inferring the destination would let a new entry point silently reuse
        the wrong one.
        """
        fields = {
            CONF_NAME: pending.name,
            CONF_CHANNELS: ",".join(str(channel) for channel in pending.channels),
            CONF_TRAVEL_UP: user_input.get(CONF_TRAVEL_UP),
            CONF_TRAVEL_DOWN: user_input.get(CONF_TRAVEL_DOWN),
        }
        self._pending_measure = None
        if pending.origin == "wizard":
            covers = self._covers
            if covers is None:
                return await self.async_step_user()
            cover, errors = _validate_cover_input(
                fields, [existing.channels for existing in covers]
            )
            if cover is None:
                self._measure_error = errors.get("base", "invalid_config")
                return await self.async_step_cover()
            covers.append(cover)
            return await self.async_step_cover_menu()

        entry = self._get_reconfigure_entry()
        try:
            rows = _entry_cover_rows(entry)
        except ValueError:
            return self.async_abort(reason="invalid_config")
        if pending.origin == "add":
            try:
                existing = _sibling_channel_sets(entry)
            except ValueError:
                return self.async_abort(reason="invalid_config")
            cover, errors = _validate_cover_input(fields, existing)
            if cover is None:
                self._measure_error = errors.get("base", "invalid_config")
                return await self.async_step_cover_add()
            rows.append({CONF_COVER_ID: ulid_now(), **cover.as_dict()})
            return self._update_covers_and_abort(rows, "cover_added")

        cover_id = pending.cover_id
        if cover_id is None:
            return self.async_abort(reason="cover_not_found")
        try:
            index, stored = _find_cover_row(rows, cover_id)
        except ValueError:
            return self.async_abort(reason="cover_not_found")
        try:
            existing = _sibling_channel_sets(entry, exclude_cover_id=cover_id)
        except ValueError:
            return self.async_abort(reason="invalid_config")
        cover, errors = _validate_cover_input(fields, existing)
        if cover is None:
            self._measure_error = errors.get("base", "invalid_config")
            self._cover_id = cover_id
            return await self.async_step_cover_edit()
        rows[index] = {**stored, **cover.as_dict(), CONF_COVER_ID: cover_id}
        return self._update_covers_and_abort(rows, "cover_updated")

    def _consume_measure_error(self) -> dict[str, str]:
        """Surface an abandoned measurement's error on the cover form, once."""
        error = self._measure_error
        self._measure_error = None
        return {"base": error} if error else {}

    @staticmethod
    def _resolve_listen_bridges(
        registry: BridgeRegistry,
        bridge_id: str,
    ) -> tuple[str, ...]:
        """Turn one picker value into the set of bridges that will listen.

        Automatic is the whole online fleet: a single bridge hears a remote
        only ~30% of the time while its peers hear nearly every press (#57).
        A named bridge is an explicit single-bridge override -- useful for
        diagnosing what one bridge can hear.
        """
        if bridge_id == _AUTOMATIC_BRIDGE:
            return registry.online_bridge_ids()
        return (registry.online_bridge(bridge_id).bridge_id,)

    async def _async_measure_abandoned(
        self,
        error: str | None = None,
    ) -> ConfigFlowResult:
        """Return to the cover form when measurement cannot continue."""
        pending = self._pending_measure
        self._pending_measure = None
        if self._measure_task is not None and not self._measure_task.done():
            self._measure_task.cancel()
        self._measure_task = None
        self._sniff_session_id = None
        self._measure_heard = ()
        session = self._measure_session
        self._measure_session = None
        if session is not None:
            await self._async_measure_session_close(session)
        self._measure_error = error
        if pending is None or pending.origin == "wizard":
            return await self.async_step_cover()
        if pending.origin == "add":
            return await self.async_step_cover_add()
        return await self.async_step_cover_edit()

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
        """Capture one action and always release/stop every bridge sniff.

        Fleet-wide like the measure path (#57): every claimed bridge's RX
        feeds the one attempt, whose future resolves on the first acceptable
        capture -- later copies of the same press, from any bridge, return at
        the ``future.done()`` gate.
        """
        from homeassistant.components import mqtt

        captures = self._captures
        if not self._learn_bridges or captures is None:
            return "timeout"
        channels = _claim_sniff_channels(self.hass, session_id, self._learn_bridges)
        if not channels:
            if self._sniff_session_id == session_id:
                self._sniff_session_id = None
            return "timeout"
        attempt = _SniffAttempt(
            action=self._learn_action,
            measured=dict(captures),
            future=self.hass.loop.create_future(),
        )
        capture_future = attempt.future
        try:
            async with asyncio.timeout(_CAPTURE_TIMEOUT_SECONDS):
                async with asyncio.timeout(_MQTT_BOOTSTRAP_TIMEOUT_SECONDS):
                    if not await mqtt.async_wait_for_mqtt_client(self.hass):
                        return "timeout"
                    for channel in channels:
                        rx_topic = f"{MQTT_ROOT}/{channel.bridge_id}/rx"
                        channel.unsubscribe = await _async_subscribe_ready(
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
                    for channel in channels:
                        await mqtt.async_publish(
                            self.hass,
                            channel.command_topic,
                            _sniff_command(DEFAULT_SNIFF_WINDOW_SECONDS),
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
            for channel in channels:
                if channel.unsubscribe is not None:
                    channel.unsubscribe()
            if not capture_future.done():
                capture_future.cancel()
            await _async_stop_sniff_channels(self.hass, session_id, channels)

    def _measure_identity(self) -> RemoteIdentity | None:
        """Return the calibrated identity a travel capture must match.

        None for a virtual remote: its bases are synthesized, so no physical
        remote transmits that identity and no press could ever match. Detected
        in memory rather than from stored data, because an entry does not
        record whether its identity was learned or synthesized.
        """
        if self._identity_is_virtual:
            return None
        if self._identity is not None and self._identity.bases is not None:
            return self._identity
        if self.source != config_entries.SOURCE_RECONFIGURE:
            return None
        try:
            remote = RemoteConfig.from_entry(self._get_reconfigure_entry().data)
        except _COERCION_ERRORS:
            return None
        return remote.remote if remote.remote.bases is not None else None

    async def _async_measure_arm(self, session_id: str) -> str:
        """Open one listening session and wait for a direction press.

        Returns ``armed`` with the session left OPEN for the STOP phase; on
        any other outcome the session is closed before returning, so the
        timeout menu never leaves a bridge claimed.
        """
        from homeassistant.components import mqtt

        pending = self._pending_measure
        identity = self._measure_identity()
        if pending is None or not pending.bridges or identity is None:
            return "failed"
        channels = _claim_sniff_channels(self.hass, session_id, pending.bridges)
        if not channels:
            if self._sniff_session_id == session_id:
                self._sniff_session_id = None
            return "failed"
        session = _MeasureSession(
            session_id=session_id,
            channels=channels,
            run=TravelRun(
                identity=identity,
                channels=pending.channels,
                wanted=pending.wanted,
            ),
            armed=asyncio.Event(),
            future=self.hass.loop.create_future(),
        )
        self._measure_session = session
        armed = False
        try:
            try:
                async with asyncio.timeout(_MQTT_BOOTSTRAP_TIMEOUT_SECONDS):
                    if not await mqtt.async_wait_for_mqtt_client(self.hass):
                        return "failed"
                    for channel in session.channels:
                        rx_topic = f"{MQTT_ROOT}/{channel.bridge_id}/rx"
                        channel.unsubscribe = await _async_subscribe_ready(
                            self.hass,
                            rx_topic,
                            functools.partial(
                                _handle_travel_message,
                                self,
                                session_id,
                                rx_topic,
                                channel.bridge_id,
                                session.run,
                                session.armed,
                                session.future,
                            ),
                        )
            except TimeoutError:
                return "failed"
            # Background tasks, deliberately: the holds outlive the arm
            # phase (the session spans both progress tasks), and a tracked
            # task sleeping between re-arms would stall every
            # async_block_till_done for the full re-arm interval.
            for channel in session.channels:
                channel.holder = self.hass.async_create_background_task(
                    _async_hold_sniff_open(self.hass, channel.command_topic),
                    f"{DOMAIN} travel sniff hold {channel.bridge_id}",
                )
            try:
                async with asyncio.timeout(TRAVEL_ARM_TIMEOUT_SECONDS):
                    await session.armed.wait()
            except TimeoutError:
                # What WAS heard survives the session so the timeout screen
                # can name the remote actually in the user's hand.
                self._measure_heard = tuple(session.run.heard)
                return "no_press"
            armed = True
            return "armed"
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Flow-local travel arm failed", exc_info=True)
            return "failed"
        finally:
            if not armed:
                self._measure_session = None
                await self._async_measure_session_close(session)

    async def _async_measure_finish(
        self,
        session: _MeasureSession,
    ) -> TravelMeasurement | str:
        """Wait for the armed run's STOP, then always close the session."""
        try:
            async with asyncio.timeout(TRAVEL_RUN_TIMEOUT_SECONDS):
                return await session.future
        except TimeoutError:
            return "no_stop"
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Flow-local travel finish failed", exc_info=True)
            return "failed"
        finally:
            self._measure_session = None
            await self._async_measure_session_close(session)

    async def _async_measure_session_close(self, session: _MeasureSession) -> None:
        """Tear one listening session down; safe to call more than once."""
        if session.closed:
            return
        session.closed = True
        if self._sniff_session_id == session.session_id:
            self._sniff_session_id = None
        for channel in session.channels:
            if channel.holder is not None:
                channel.holder.cancel()
            if channel.unsubscribe is not None:
                channel.unsubscribe()
        if not session.future.done():
            session.future.cancel()
        await _async_stop_sniff_channels(self.hass, session.session_id, session.channels)

    @callback
    def async_remove(self) -> None:
        """Invalidate callbacks while HA cancels the registered progress task."""
        self._sniff_session_id = None
        super().async_remove()
