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
from homeassistant.config_entries import ConfigSubentryData
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
    CONF_BASE_TRAILER,
    CONF_BRIDGE,
    CONF_CALIBRATION_BASE,
    CONF_CALIBRATION_BUTTON,
    CONF_CALIBRATION_FRAME,
    CONF_CHANNELS,
    CONF_COALESCE_WINDOW_MS,
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


def _remote_identity_from_capture(capture: _LearnCapture) -> RemoteIdentity:
    """Derive the calibrated identity from one accepted sniff capture."""
    return RemoteIdentity(
        prefix=capture.prefix,
        remote_id=capture.remote_id,
        bases=derive_bases(
            capture.channels,
            capture.button,
            capture.command,
            capture.remote_id,
        ),
    )


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


def _validate_cover_input(
    user_input: Mapping[str, Any],
    collected: list[CoverConfig],
) -> tuple[CoverConfig | None, dict[str, str]]:
    """Validate one wizard cover form against the covers collected so far."""
    try:
        channels = parse_channels(user_input.get(CONF_CHANNELS, ""))
    except ValueError:
        return None, {CONF_CHANNELS: "invalid_config"}
    conflict = laminar_conflict(channels, [cover.channels for cover in collected])
    if conflict is not None:
        return None, {CONF_CHANNELS: conflict}
    born_aggregate = any(frozenset(cover.channels) < frozenset(channels) for cover in collected)
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
        )
    except TypeError, ValueError:
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


class ZemismartBlindsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add exactly one blind or group device per config entry."""

    VERSION = 1

    _capture: _LearnCapture | None = None
    _covers: list[CoverConfig] | None = None
    _identity: RemoteIdentity | None = None
    _learn_area_id: str | None = None
    _learn_bridge: str | None = None
    _learn_name: str | None = None
    _learn_registry: BridgeRegistry | None = None
    _learn_suggested: dict[str, object] | None = None
    _remote: RemoteConfig | None = None
    _sniff_session_id: str | None = None
    _sniff_task: asyncio.Task[Literal["captured", "timeout"]] | None = None

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
        try:
            self._identity = _remote_identity_from_capture(capture)
        except ValueError:
            return await self.async_step_learn_timeout()
        return self.async_show_menu(
            step_id="learn_confirm",
            menu_options=["remote_settings", "learn_retry", "advanced"],
            description_placeholders={
                "prefix": f"0x{capture.prefix:06x}",
                "remote_id": f"0x{capture.remote_id:02x}",
                "channels": ",".join(map(str, capture.channels)),
                "button": capture.button,
                "name": self._learn_name or "",
                "bridge": self._learn_bridge or "",
            },
        )

    async def async_step_advanced(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer manual and virtual identity paths."""
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
            except TypeError, ValueError:
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
            cover, errors = _validate_cover_input(user_input, self._covers)
            if cover is not None:
                self._covers.append(cover)
                return await self.async_step_cover_menu()
        suggested: dict[str, object] = {}
        if not self._covers and self._capture is not None:
            suggested[CONF_CHANNELS] = ",".join(map(str, self._capture.channels))
        if user_input is not None:
            suggested = dict(user_input)
        return self.async_show_form(
            step_id="cover",
            data_schema=_cover_schema(suggested),
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
        """Create the remote entry with every collected cover subentry."""
        del user_input
        remote = self._remote
        covers = self._covers
        if remote is None or not covers:
            return await self.async_step_user()
        # Final whole-list backstop: HA does not validate subentry unique_ids
        # at initial entry creation, and flow-state replay could bypass the
        # per-iteration checks.
        for index, cover in enumerate(covers):
            others = [c.channels for i, c in enumerate(covers) if i != index]
            if laminar_conflict(cover.channels, others) is not None:
                return self.async_abort(reason="channel_conflict")
        await self.async_set_unique_id(remote.key)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=remote.name,
            data=remote.as_dict(),
            subentries=[
                ConfigSubentryData(
                    data=cover.as_dict(),
                    subentry_type="cover",
                    title=cover.name,
                    unique_id=cover.channel_key,
                )
                for cover in covers
            ],
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

    @callback
    def async_remove(self) -> None:
        """Invalidate callbacks while HA cancels the registered progress task."""
        self._sniff_session_id = None
        super().async_remove()
