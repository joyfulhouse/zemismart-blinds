"""Config and options flows for adding one Zemismart blind/group at a time."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .codec import (
    CommandBases,
    decode_b0,
    decode_reference_b0,
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
    CONF_KNOWN_REMOTE,
    CONF_NAME,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    CONF_TRAVEL_DOWN,
    CONF_TRAVEL_UP,
    DEFAULT_COALESCE_WINDOW_MS,
    DEFAULT_REPEATS,
    DEFAULT_SNIFF_WINDOW_SECONDS,
    DEFAULT_TRAVEL_DOWN,
    DEFAULT_TRAVEL_UP,
    DOMAIN,
    MANUAL_REMOTE,
    MQTT_AVAILABILITY_TOPIC,
    MQTT_CMD_ACTION_SNIFF,
    MQTT_CMD_FIELD_ACTION,
    MQTT_CMD_FIELD_SECONDS,
    MQTT_CMD_TEMPLATE,
    MQTT_INFO_TOPIC,
    MQTT_ROOT,
    MQTT_RX_FIELD_FRAME,
    VIRTUAL_REMOTE,
)
from .models import (
    BlindConfig,
    BridgeRegistry,
    NoOnlineBridgeError,
    RemoteIdentity,
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
_ADVANCED_SECTION = "advanced"
_AUTOMATIC_BRIDGE = "automatic"
_BRIDGE_DISCOVERY_SECONDS = 0.25
_MQTT_BOOTSTRAP_TIMEOUT_SECONDS = 5.0
_CAPTURE_TIMEOUT_SECONDS = float(DEFAULT_SNIFF_WINDOW_SECONDS)
_CAPTURE_OWNERS: dict[tuple[int, str], str] = {}


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


def effective_values(entry: config_entries.ConfigEntry[Any]) -> dict[str, object]:
    """Merge an existing entry's mutable options over initial data."""
    values: dict[str, object] = dict(entry.data)
    values.update(entry.options)
    return values


def known_remotes(
    entries: list[config_entries.ConfigEntry[Any]],
) -> dict[str, tuple[RemoteIdentity, str]]:
    """Collect unique calibrated remote identities from existing entries."""
    remotes: dict[str, tuple[RemoteIdentity, str]] = {}
    for entry in entries:
        try:
            config = BlindConfig.from_mapping(effective_values(entry))
        except TypeError, ValueError:
            continue
        remotes.setdefault(config.remote_key, (config.remote, entry.title))
    return remotes


def _known_remote_options(
    remotes: Mapping[str, tuple[RemoteIdentity, str]],
) -> list[selector.SelectOptionDict]:
    """Build select options for calibrated identities from existing entries."""
    return [
        {
            "value": key,
            "label": f"{label} — prefix 0x{remote.prefix:06x}, id 0x{remote.remote_id:02x}",
        }
        for key, (remote, label) in sorted(remotes.items())
    ]


def _details_schema(
    suggested: Mapping[str, object] | None = None,
    *,
    include_name: bool = True,
) -> vol.Schema:
    """Build target, travel-time, area, and RF tuning fields."""
    values = suggested or {}
    raw_channels = values.get(CONF_CHANNELS, "1")
    if isinstance(raw_channels, list | tuple | set):
        raw_channels = ",".join(str(channel) for channel in raw_channels)
    elif not isinstance(raw_channels, str):
        raw_channels = "1"

    fields: dict[vol.Marker, object] = {}
    if include_name:
        fields[
            vol.Required(
                CONF_NAME,
                default=str(values.get(CONF_NAME, "")),
            )
        ] = selector.TextSelector()
    fields.update(
        {
            vol.Required(CONF_CHANNELS, default=raw_channels): selector.TextSelector(),
            vol.Required(
                CONF_TRAVEL_UP,
                default=_float_value(
                    values.get(CONF_TRAVEL_UP),
                    DEFAULT_TRAVEL_UP,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=600,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_TRAVEL_DOWN,
                default=_float_value(
                    values.get(CONF_TRAVEL_DOWN),
                    DEFAULT_TRAVEL_DOWN,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=600,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_AREA_ID,
                default=str(values.get(CONF_AREA_ID, "")),
            ): selector.AreaSelector(),
            vol.Required(_ADVANCED_SECTION): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_REPEATS,
                            default=_int_value(
                                values.get(CONF_REPEATS),
                                DEFAULT_REPEATS,
                            ),
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
    return vol.Schema(fields)


def _config_from_input(
    user_input: Mapping[str, Any],
    remotes: Mapping[str, tuple[RemoteIdentity, str]],
) -> BlindConfig:
    """Validate flow input and resolve either manual or reused remote identity."""
    selection = str(user_input[CONF_KNOWN_REMOTE])
    if selection == MANUAL_REMOTE:
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
        remote = RemoteIdentity(prefix=prefix, remote_id=remote_id, bases=bases)
    else:
        try:
            remote = remotes[selection][0]
        except KeyError as exc:
            msg = "selected known remote no longer exists"
            raise ValueError(msg) from exc
    if remote.bases is None:
        msg = "remote calibration is required"
        raise ValueError(msg)
    return BlindConfig(
        name=str(user_input[CONF_NAME]),
        remote=remote,
        channels=parse_channels(user_input[CONF_CHANNELS]),
        travel_up=float(user_input[CONF_TRAVEL_UP]),
        travel_down=float(user_input[CONF_TRAVEL_DOWN]),
        area_id=str(user_input[CONF_AREA_ID]),
        repeats=whole_number(user_input[CONF_REPEATS], CONF_REPEATS),
        coalesce_window_ms=whole_number(
            user_input.get(CONF_COALESCE_WINDOW_MS, DEFAULT_COALESCE_WINDOW_MS),
            CONF_COALESCE_WINDOW_MS,
        ),
    )


def _suggested_for(config: BlindConfig) -> dict[str, object]:
    """Convert typed storage into edit-flow display values."""
    values = config.as_dict()
    values[CONF_KNOWN_REMOTE] = config.remote_key
    values[CONF_CHANNELS] = ",".join(str(channel) for channel in config.channels)
    values[CONF_CALIBRATION_BUTTON] = "UP"
    values[CONF_CALIBRATION_BASE] = values[CONF_BASE_UP]
    values[CONF_CALIBRATION_FRAME] = ""
    return values


def _cross_area_overlap(
    hass: HomeAssistant,
    config: BlindConfig,
    skip_entry_id: str | None,
) -> bool:
    """Detect a same-remote channel overlap configured in a DIFFERENT area.

    Bridge routing is partitioned by area. Overlapping channel sets split
    across areas would let two bridges hold state for one physical motor
    that neither can displace (an armed fail-safe STOP on bridge A would
    later halt a movement commanded through bridge B without any status).
    A physical blind lives in one room; its groups belong to that room too.
    """
    channels = set(config.channels)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == skip_entry_id:
            continue
        try:
            other = BlindConfig.from_mapping(effective_values(entry))
        except TypeError, ValueError:
            continue
        if (
            other.remote.key == config.remote.key
            and other.area_id != config.area_id
            and channels & set(other.channels)
        ):
            return True
    return False


def _unique_id(config: BlindConfig) -> str:
    """Return the identity of one remote channel set."""
    return f"{config.remote_key}:{'-'.join(map(str, config.channels))}"


def _materialize_virtual_remote(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Rewrite a virtual-remote selection as a fully calibrated manual entry."""
    from . import new_virtual_remote_identity

    prefix, remote_id, bases = new_virtual_remote_identity(hass)
    return {
        **user_input,
        CONF_KNOWN_REMOTE: MANUAL_REMOTE,
        CONF_PREFIX: f"{prefix:06x}",
        CONF_REMOTE_ID: f"{remote_id:02x}",
        CONF_CALIBRATION_BUTTON: "UP",
        CONF_CALIBRATION_BASE: f"{bases.up:04x}",
        CONF_CALIBRATION_FRAME: "",
        CONF_BASE_TRAILER: "",
    }


def _propagate_calibration(
    hass: HomeAssistant,
    config: BlindConfig,
    exclude_entry_id: str | None,
) -> None:
    """Copy a manually recalibrated identity to every sibling entry.

    One physical remote has exactly one calibration; entries sharing its
    identity must never transmit conflicting command bases.
    """
    assert config.remote.bases is not None
    base_keys = {CONF_BASE_UP, CONF_BASE_DOWN, CONF_BASE_STOP, CONF_BASE_TRAILER}
    base_values = {key: value for key, value in config.as_dict().items() if key in base_keys}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id:
            continue
        try:
            sibling = BlindConfig.from_mapping(effective_values(entry))
        except TypeError, ValueError:
            continue
        if sibling.remote_key != config.remote_key or sibling.remote.bases == config.remote.bases:
            continue

        def rewritten(values: Mapping[str, object]) -> dict[str, object]:
            # Drop stale base keys first: a recalibration without a trailer
            # must also remove a sibling's previous trailer base.
            return {
                **{key: value for key, value in values.items() if key not in base_keys},
                **base_values,
            }

        options = rewritten(entry.options) if entry.options else entry.options
        hass.config_entries.async_update_entry(entry, data=rewritten(entry.data), options=options)
        hass.config_entries.async_schedule_reload(entry.entry_id)


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
    button: str


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


@callback
def _handle_sniff_message(
    flow: ZemismartBlindsConfigFlow,
    session_id: str,
    expected_topic: str,
    capture_future: asyncio.Future[_LearnCapture],
    message: ReceiveMessage,
) -> None:
    """Resolve the current attempt with its first decodable action frame."""
    if (
        flow._sniff_session_id != session_id
        or capture_future.done()
        or message.retain
        or message.topic != expected_topic
    ):
        return
    try:
        text = _payload_text(message.payload)
        decoded_payload: object = json.loads(text)
    except UnicodeDecodeError, json.JSONDecodeError:
        return
    if not isinstance(decoded_payload, Mapping):
        return
    frame = decoded_payload.get(MQTT_RX_FIELD_FRAME)
    if not isinstance(frame, str):
        return
    try:
        decoded = decode_b0(frame)
        channels = tuple(decoded["chans"])
        button = infer_action_button(channels, decoded["cmd"])
    except TypeError, ValueError:
        return
    if button not in {"UP", "DOWN", "STOP"}:
        return
    capture_future.set_result(
        _LearnCapture(
            frame=frame,
            prefix=decoded["prefix"],
            remote_id=decoded["remote_id"],
            channels=channels,
            command=decoded["cmd"],
            button=button,
        )
    )


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


def _reuse_schema(
    remotes: Mapping[str, tuple[RemoteIdentity, str]],
    selected: str | None = None,
) -> vol.Schema:
    """Build the Advanced known-remote identity form."""
    options = _known_remote_options(remotes)
    field = (
        vol.Required(CONF_KNOWN_REMOTE, default=selected)
        if selected
        else vol.Required(CONF_KNOWN_REMOTE)
    )
    return vol.Schema(
        {field: selector.SelectSelector(selector.SelectSelectorConfig(options=options))}
    )


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


class ZemismartBlindsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add exactly one blind or group device per config entry."""

    VERSION = 1

    _advanced_identity: dict[str, Any] | None = None
    _capture: _LearnCapture | None = None
    _learn_area_id: str | None = None
    _learn_bridge: str | None = None
    _learn_name: str | None = None
    _learn_registry: BridgeRegistry | None = None
    _learn_suggested: dict[str, object] | None = None
    _offer_reuse_continuation = False
    _reconfigure_config: BlindConfig | None = None
    _reuse_selected: str | None = None
    _sniff_session_id: str | None = None
    _sniff_task: asyncio.Task[Literal["captured", "timeout"]] | None = None

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer guided learning before the Advanced fallback paths."""
        if user_input is not None:
            selected = str(user_input.get(CONF_KNOWN_REMOTE, ""))
            if selected:
                self._reuse_selected = selected
                return await self.async_step_reuse()
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

    async def async_step_learn_unavailable(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer retry and Advanced when flow-local MQTT discovery fails."""
        del user_input
        self._learn_registry = None
        return self.async_show_menu(
            step_id="learn_unavailable",
            menu_options=["learn_setup", "advanced"],
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
            next_step = "learn_confirm" if outcome == "captured" else "learn_timeout"
            return self.async_show_progress_done(next_step_id=next_step)

        if self._sniff_task is None:
            self._capture = None
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
                "bridge": self._learn_bridge or "",
                "seconds": str(DEFAULT_SNIFF_WINDOW_SECONDS),
            },
        )

    async def async_step_learn_retry(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Invalidate the prior capture before starting a fresh attempt."""
        del user_input
        self._sniff_session_id = None
        self._sniff_task = None
        return await self.async_step_learn_sniff()

    async def async_step_learn_timeout(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer a fresh capture attempt or the Advanced fallbacks."""
        del user_input
        return self.async_show_menu(
            step_id="learn_timeout",
            menu_options=["learn_retry", "advanced"],
        )

    async def async_step_learn_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Summarize decoded identity/action without exposing raw capture hex."""
        del user_input
        capture = self._capture
        if capture is None:
            return await self.async_step_learn_timeout()
        return self.async_show_menu(
            step_id="learn_confirm",
            menu_options=["learn_details", "learn_retry", "advanced"],
            description_placeholders={
                "prefix": f"0x{capture.prefix:06x}",
                "remote_id": f"0x{capture.remote_id:02x}",
                "channels": ",".join(map(str, capture.channels)),
                "button": capture.button,
                "name": self._learn_name or "",
                "bridge": self._learn_bridge or "",
            },
        )

    async def async_step_learn_details(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect target/timing settings and persist capture-derived bases."""
        capture = self._capture
        if capture is None or self._learn_name is None or self._learn_area_id is None:
            return await self.async_step_learn_timeout()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                details = _flatten_details(user_input)
                config = _config_from_input(
                    {
                        **details,
                        CONF_NAME: self._learn_name,
                        CONF_KNOWN_REMOTE: MANUAL_REMOTE,
                        CONF_PREFIX: f"{capture.prefix:06x}",
                        CONF_REMOTE_ID: f"{capture.remote_id:02x}",
                        CONF_CALIBRATION_BUTTON: capture.button,
                        CONF_CALIBRATION_BASE: "",
                        CONF_CALIBRATION_FRAME: capture.frame,
                        CONF_BASE_TRAILER: "",
                    },
                    {},
                )
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                result, error = await self._async_finish_config(config)
                if result is not None:
                    self._offer_reuse_continuation = self.source == config_entries.SOURCE_USER
                    return result
                assert error is not None
                errors["base"] = error

        suggested: dict[str, object] = {
            **(self._learn_suggested or {}),
            CONF_CHANNELS: ",".join(map(str, capture.channels)),
            CONF_AREA_ID: self._learn_area_id,
        }
        if user_input is not None:
            with suppress(TypeError, ValueError):
                suggested.update(_flatten_details(user_input))
        return self.async_show_form(
            step_id="learn_details",
            data_schema=_details_schema(
                suggested,
                include_name=False,
            ),
            errors=errors,
        )

    async def async_step_advanced(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer separated known, manual, and virtual identity paths."""
        del user_input
        return self.async_show_menu(
            step_id="advanced",
            menu_options=["reuse", "manual", "virtual"],
        )

    async def async_step_reuse(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select one calibrated identity from existing entries."""
        remotes = known_remotes(self._async_current_entries())
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = str(user_input.get(CONF_KNOWN_REMOTE, ""))
            if selected not in remotes:
                errors["base"] = "invalid_config"
            else:
                self._advanced_identity = {CONF_KNOWN_REMOTE: selected}
                return await self.async_step_advanced_details()
        elif not remotes:
            errors["base"] = "no_known_remotes"
        return self.async_show_form(
            step_id="reuse",
            data_schema=_reuse_schema(remotes, self._reuse_selected),
            errors=errors,
        )

    async def async_step_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect an identity plus one calibration source."""
        errors: dict[str, str] = {}
        if user_input is not None:
            identity = {**user_input, CONF_KNOWN_REMOTE: MANUAL_REMOTE}
            try:
                _config_from_input(
                    {
                        **identity,
                        CONF_NAME: "Manual remote",
                        CONF_CHANNELS: "1",
                        CONF_TRAVEL_UP: DEFAULT_TRAVEL_UP,
                        CONF_TRAVEL_DOWN: DEFAULT_TRAVEL_DOWN,
                        CONF_AREA_ID: "validation",
                        CONF_REPEATS: DEFAULT_REPEATS,
                        CONF_COALESCE_WINDOW_MS: DEFAULT_COALESCE_WINDOW_MS,
                    },
                    {},
                )
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                self._advanced_identity = identity
                return await self.async_step_advanced_details()
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(user_input),
            errors=errors,
        )

    async def async_step_virtual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allocate a calibrated virtual identity before common details."""
        del user_input
        self._advanced_identity = _materialize_virtual_remote(
            self.hass,
            {CONF_KNOWN_REMOTE: VIRTUAL_REMOTE},
        )
        return await self.async_step_advanced_details()

    async def async_step_advanced_details(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect common device settings for any Advanced identity path."""
        if self._advanced_identity is None:
            return await self.async_step_advanced()
        remotes = known_remotes(self._async_current_entries())
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = _config_from_input(
                    {**self._advanced_identity, **_flatten_details(user_input)},
                    remotes,
                )
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                result, error = await self._async_finish_config(config)
                if result is not None:
                    return result
                assert error is not None
                errors["base"] = error

        suggested = dict(self._learn_suggested or {})
        if self._capture is not None:
            suggested[CONF_CHANNELS] = ",".join(map(str, self._capture.channels))
        if user_input is not None:
            with suppress(TypeError, ValueError):
                suggested.update(_flatten_details(user_input))
        return self.async_show_form(
            step_id="advanced_details",
            data_schema=_details_schema(
                suggested,
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer relearning or settings-only editing for one existing entry."""
        del user_input
        self._reconfigure_config = BlindConfig.from_mapping(
            effective_values(self._get_reconfigure_entry())
        )
        self._learn_suggested = _suggested_for(self._reconfigure_config)
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["reconfigure_learn", "reconfigure_edit"],
        )

    async def async_step_reconfigure_learn(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Reuse the Learn subtree with current name/area suggestions."""
        del user_input
        if self._reconfigure_config is None:
            self._reconfigure_config = BlindConfig.from_mapping(
                effective_values(self._get_reconfigure_entry())
            )
        self._learn_suggested = _suggested_for(self._reconfigure_config)
        self._learn_registry = None
        return await self.async_step_learn_setup()

    async def async_step_reconfigure_edit(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit device settings while retaining the effective remote calibration."""
        current = self._reconfigure_config
        if current is None:
            current = BlindConfig.from_mapping(effective_values(self._get_reconfigure_entry()))
            self._reconfigure_config = current
        current_remote = {current.remote_key: (current.remote, current.name)}
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = _config_from_input(
                    {
                        CONF_KNOWN_REMOTE: current.remote_key,
                        **_flatten_details(user_input),
                    },
                    current_remote,
                )
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                result, error = await self._async_finish_config(config)
                if result is not None:
                    return result
                assert error is not None
                errors["base"] = error
        suggested: Mapping[str, object] = _suggested_for(current)
        if user_input is not None:
            with suppress(TypeError, ValueError):
                suggested = _flatten_details(user_input)
        return self.async_show_form(
            step_id="reconfigure_edit",
            data_schema=_details_schema(
                suggested,
            ),
            errors=errors,
        )

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

    async def _async_capture(self, session_id: str) -> Literal["captured", "timeout"]:
        """Capture one action and always release/stop the bridge sniff session."""
        from homeassistant.components import mqtt

        bridge = self._learn_bridge
        if bridge is None:
            return "timeout"
        owner_key = (id(self.hass), bridge)
        if owner_key in _CAPTURE_OWNERS:
            if self._sniff_session_id == session_id:
                self._sniff_session_id = None
            return "timeout"
        _CAPTURE_OWNERS[owner_key] = session_id
        rx_topic = f"{MQTT_ROOT}/{bridge}/rx"
        command_topic = MQTT_CMD_TEMPLATE.format(bridge=bridge)
        capture_future: asyncio.Future[_LearnCapture] = self.hass.loop.create_future()
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
                            capture_future,
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
            if self._sniff_session_id != session_id:
                return "timeout"
            self._capture = capture
            return "captured"
        except TimeoutError:
            return "timeout"
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

    async def _async_finish_config(
        self,
        config: BlindConfig,
    ) -> tuple[ConfigFlowResult | None, str | None]:
        """Validate identity/area constraints and finish add or reconfigure."""
        unique_id = _unique_id(config)
        if self.source == config_entries.SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            if any(
                candidate.entry_id != entry.entry_id and candidate.unique_id == unique_id
                for candidate in self.hass.config_entries.async_entries(DOMAIN)
            ):
                return None, "already_configured"
            if _cross_area_overlap(self.hass, config, entry.entry_id):
                return None, "cross_area_overlap"
            _propagate_calibration(self.hass, config, entry.entry_id)
            return (
                self.async_update_reload_and_abort(
                    entry,
                    title=config.name,
                    unique_id=unique_id,
                    data=config.as_dict(),
                    options={},
                ),
                None,
            )

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        if _cross_area_overlap(self.hass, config, None):
            return None, "cross_area_overlap"
        _propagate_calibration(self.hass, config, None)
        return self.async_create_entry(title=config.name, data=config.as_dict()), None

    async def async_on_create_entry(
        self,
        result: ConfigFlowResult,
    ) -> ConfigFlowResult:
        """Offer another channel on a remote learned by this flow."""
        if not self._offer_reuse_continuation:
            return result
        config = BlindConfig.from_mapping(result["result"].data)
        continuation = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_KNOWN_REMOTE: config.remote_key},
        )
        result["next_flow"] = (
            config_entries.FlowType.CONFIG_FLOW,
            continuation["flow_id"],
        )
        return result

    @callback
    def async_remove(self) -> None:
        """Invalidate callbacks while HA cancels the registered progress task."""
        self._sniff_session_id = None
        super().async_remove()

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry[Any],
    ) -> ZemismartBlindsOptionsFlow:
        """Return the edit flow for one existing device."""
        return ZemismartBlindsOptionsFlow()


class ZemismartBlindsOptionsFlow(config_entries.OptionsFlowWithReload):
    """Edit one blind/group and reload its entity."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit timing, area, channels, and presentation settings."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        current = BlindConfig.from_mapping(effective_values(self.config_entry))
        current_remote = {current.remote_key: (current.remote, current.name)}
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = _config_from_input(
                    {
                        CONF_KNOWN_REMOTE: current.remote_key,
                        **_flatten_details(user_input),
                    },
                    current_remote,
                )
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                new_unique_id = _unique_id(config)
                if any(
                    entry.entry_id != self.config_entry.entry_id
                    and entry.unique_id == new_unique_id
                    for entry in entries
                ):
                    errors["base"] = "already_configured"
                elif _cross_area_overlap(self.hass, config, self.config_entry.entry_id):
                    errors["base"] = "cross_area_overlap"
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        title=config.name,
                        unique_id=new_unique_id,
                    )
                    _propagate_calibration(self.hass, config, self.config_entry.entry_id)
                    return self.async_create_entry(title=config.name, data=config.as_dict())

        suggested: Mapping[str, object] = _suggested_for(current)
        if user_input is not None:
            with suppress(TypeError, ValueError):
                suggested = _flatten_details(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=_details_schema(
                suggested,
            ),
            errors=errors,
        )
