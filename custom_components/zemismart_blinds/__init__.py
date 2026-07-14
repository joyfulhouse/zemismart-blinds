"""Set up the Zemismart Blinds Home Assistant integration."""

from __future__ import annotations

import functools
import json
import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from homeassistant.core import callback

from .codec import CommandBases, synthesize_bases
from .const import (
    ATTR_BRIDGE,
    ATTR_RAW,
    ATTR_REPEATS,
    CONF_BASE_DOWN,
    CONF_BASE_STOP,
    CONF_BASE_UP,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    DEFAULT_REPEATS,
    DOMAIN,
    MQTT_AVAILABILITY_TOPIC,
    MQTT_INFO_TOPIC,
    MQTT_ROOT,
    MQTT_STATUS_TOPIC,
    SERVICE_NEW_VIRTUAL_REMOTE,
    SERVICE_SEND_RAW,
)
from .models import (
    BlindConfig,
    BridgeRegistry,
    DomainRuntime,
    EntryRuntime,
    ZemismartHub,
)

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
    from homeassistant.helpers.typing import ConfigType

    type ZemismartConfigEntry = ConfigEntry[EntryRuntime]


def _entry_config(entry: ZemismartConfigEntry) -> BlindConfig:
    """Build the typed config from an entry's effective values."""
    from .config_flow import effective_values

    return BlindConfig.from_mapping(effective_values(entry))


def _bridge_id(topic: str, leaf: str) -> str | None:
    """Extract a bridge id from a <MQTT_ROOT>/<bridge>/<leaf> topic."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != MQTT_ROOT or parts[2] != leaf:
        return None
    return parts[1] or None


def _payload_text(payload: str | bytes | bytearray) -> str:
    """Normalize an MQTT payload received with or without text decoding."""
    return payload.decode() if isinstance(payload, bytes | bytearray) else payload


# The three MQTT handlers are @callback functions subscribed via
# functools.partial: HA's MQTT client infers the job type from the callable
# (unwrapping partials), and a plain lambda would be classified as an
# executor job — mutating the hub's asyncio futures from a worker thread.


@callback
def _handle_availability(runtime: DomainRuntime, message: ReceiveMessage) -> None:
    """Apply a retained bridge availability message."""
    try:
        payload = _payload_text(message.payload)
    except UnicodeDecodeError:
        return
    if bridge_id := _bridge_id(message.topic, "availability"):
        runtime.hub.registry.update_availability(
            bridge_id,
            payload,
        )
        runtime.hub.notify_bridge_change()


@callback
def _handle_info(runtime: DomainRuntime, message: ReceiveMessage) -> None:
    """Apply retained bridge area/default metadata."""
    bridge_id = _bridge_id(message.topic, "info")
    if bridge_id is None:
        return
    try:
        decoded: object = json.loads(_payload_text(message.payload))
    except UnicodeDecodeError, json.JSONDecodeError:
        return
    if isinstance(decoded, Mapping):
        info = {str(key): value for key, value in decoded.items()}
        runtime.hub.registry.update_info(bridge_id, info)
        runtime.hub.notify_bridge_change()


@callback
def _handle_status(runtime: DomainRuntime, message: ReceiveMessage) -> None:
    """Apply only live, correlated bridge lifecycle statuses."""
    if message.retain:
        return
    bridge_id = _bridge_id(message.topic, "status")
    if bridge_id is not None:
        runtime.hub.handle_status(bridge_id, message.payload)


def _create_domain_runtime(hass: HomeAssistant) -> DomainRuntime:
    """Construct the shared runtime synchronously before any setup await."""
    from homeassistant.components import mqtt

    async def async_publish(topic: str, payload: str) -> None:
        await mqtt.async_publish(hass, topic, payload, qos=1, retain=False)

    return DomainRuntime(
        hub=ZemismartHub(BridgeRegistry(), async_publish),
        unsubscribers=[],
    )


async def _async_initialize_domain_runtime(
    hass: HomeAssistant,
    runtime: DomainRuntime,
) -> None:
    """Install the three shared subscriptions and services exactly once."""
    from homeassistant.components import mqtt

    subscriptions = (
        (MQTT_AVAILABILITY_TOPIC, functools.partial(_handle_availability, runtime)),
        (MQTT_INFO_TOPIC, functools.partial(_handle_info, runtime)),
        (MQTT_STATUS_TOPIC, functools.partial(_handle_status, runtime)),
    )
    try:
        for topic, handler in subscriptions:
            runtime.unsubscribers.append(await mqtt.async_subscribe(hass, topic, handler, qos=1))
    except BaseException:
        _clear_domain_registrations(hass, runtime)
        raise
    runtime.initialized = True


def new_virtual_remote_identity(hass: HomeAssistant) -> tuple[int, int, CommandBases]:
    """Allocate an unused remote identity with a synthesized calibration.

    Shared by the ``new_virtual_remote`` service and the config flow's
    virtual-remote path so both produce identical, collision-checked
    identities. A virtual remote is never captured over the air, so any
    internally consistent calibration works — the motor learns whatever the
    virtual remote transmits during pairing.
    """
    used = _known_remote_pairs(hass)
    while True:
        pair = (0x5C0000 | secrets.randbelow(1 << 16), secrets.randbelow(1 << 8))
        if pair not in used:
            break
    return pair[0], pair[1], synthesize_bases(pair[1], secrets.randbelow(1 << 8))


def _known_remote_pairs(hass: HomeAssistant) -> set[tuple[int, int]]:
    """Return remote identities already stored in config entries."""
    from .config_flow import known_remotes

    return {
        (remote.prefix, remote.remote_id)
        for remote, _ in known_remotes(hass.config_entries.async_entries(DOMAIN)).values()
    }


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain services for the lifetime of the integration."""
    del config
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError

    async def async_send_raw(call: ServiceCall) -> None:
        runtime = cast("DomainRuntime | None", hass.data.get(DOMAIN))
        if runtime is None or not runtime.initialized:
            msg = "no Zemismart Blinds entry is loaded, so no bridge registry exists"
            raise HomeAssistantError(msg)
        try:
            await runtime.hub.async_send_raw(
                str(call.data[ATTR_BRIDGE]),
                str(call.data[ATTR_RAW]),
                int(call.data[ATTR_REPEATS]),
            )
        except (ValueError, RuntimeError) as exc:
            # Frame validation, routing, rejection, and timeout errors are
            # user-actionable service failures, not tracebacks.
            raise HomeAssistantError(str(exc)) from exc

    async def async_new_virtual_remote(_call: ServiceCall) -> ServiceResponse:
        prefix, remote_id, bases = new_virtual_remote_identity(hass)
        # Returning the UP base lets the config flow's manual path accept
        # this identity directly (prefix + remote id + UP calibration base).
        return {
            CONF_PREFIX: f"0x{prefix:06x}",
            CONF_REMOTE_ID: f"0x{remote_id:02x}",
            CONF_BASE_UP: f"0x{bases.up:04x}",
            CONF_BASE_DOWN: f"0x{bases.down:04x}",
            CONF_BASE_STOP: f"0x{bases.stop:04x}",
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RAW,
        async_send_raw,
        schema=vol.Schema(
            {
                vol.Required(ATTR_BRIDGE): str,
                vol.Required(ATTR_RAW): str,
                vol.Optional(ATTR_REPEATS, default=DEFAULT_REPEATS): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=1, max=20),
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_NEW_VIRTUAL_REMOTE,
        async_new_virtual_remote,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def _async_assign_device_area(
    hass: HomeAssistant,
    entry: ZemismartConfigEntry,
    config: BlindConfig,
) -> None:
    """Put the entry's one device in the area selected by the user."""
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if device is not None and device.area_id != config.area_id:
        registry.async_update_device(device.id, area_id=config.area_id)


def _clear_domain_registrations(hass: HomeAssistant, runtime: DomainRuntime) -> None:
    """Release registrations while leaving the runtime available for setup retry."""
    del hass
    for unsubscribe in runtime.unsubscribers:
        unsubscribe()
    runtime.unsubscribers.clear()
    runtime.initialized = False


def _cleanup_domain_runtime(hass: HomeAssistant, runtime: DomainRuntime) -> None:
    """Release the final shared runtime after every setup/unload user leaves."""
    _clear_domain_registrations(hass, runtime)
    runtime.hub.close()
    if hass.data.get(DOMAIN) is runtime:
        hass.data.pop(DOMAIN)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZemismartConfigEntry,
) -> bool:
    """Set up one blind/group entry and the shared MQTT runtime."""
    from homeassistant.const import Platform

    config = _entry_config(entry)
    while True:
        candidate = _create_domain_runtime(hass)
        runtime = cast("DomainRuntime", hass.data.setdefault(DOMAIN, candidate))
        runtime.setup_users += 1
        failed = True
        retry = False
        try:
            async with runtime.lifecycle_lock:
                if hass.data.get(DOMAIN) is not runtime:
                    retry = True
                    continue
                if not runtime.initialized:
                    await _async_initialize_domain_runtime(hass, runtime)
                entry.runtime_data = EntryRuntime(config=config, hub=runtime.hub)
                if entry.entry_id in runtime.loaded_entries:
                    failed = False
                    return True
                await hass.config_entries.async_forward_entry_setups(entry, [Platform.COVER])
                await _async_assign_device_area(hass, entry, config)
                runtime.loaded_entries.add(entry.entry_id)
                failed = False
                return True
        finally:
            runtime.setup_users -= 1
            if failed and not retry and not runtime.loaded_entries and runtime.setup_users == 0:
                _cleanup_domain_runtime(hass, runtime)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ZemismartConfigEntry,
) -> bool:
    """Unload one entry and release shared subscriptions after the final entry."""
    from homeassistant.const import Platform

    runtime = cast("DomainRuntime | None", hass.data.get(DOMAIN))
    if runtime is None:
        return bool(await hass.config_entries.async_unload_platforms(entry, [Platform.COVER]))
    async with runtime.lifecycle_lock:
        if not await hass.config_entries.async_unload_platforms(entry, [Platform.COVER]):
            return False
        runtime.loaded_entries.discard(entry.entry_id)
        if not runtime.loaded_entries and runtime.setup_users == 0:
            _cleanup_domain_runtime(hass, runtime)
        return True
