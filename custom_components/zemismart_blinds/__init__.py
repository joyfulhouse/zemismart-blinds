"""Set up the Zemismart Blinds Home Assistant integration."""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from .codec import synthesize_bases
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

    type ZemismartConfigEntry = ConfigEntry[EntryRuntime]


def _entry_config(entry: ZemismartConfigEntry) -> BlindConfig:
    """Merge entry options over immutable setup data."""
    values: dict[str, object] = dict(entry.data)
    values.update(entry.options)
    return BlindConfig.from_mapping(values)


def _bridge_id(topic: str, leaf: str) -> str | None:
    """Extract a bridge id from a <MQTT_ROOT>/<bridge>/<leaf> topic."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != MQTT_ROOT or parts[2] != leaf:
        return None
    return parts[1] or None


def _payload_text(payload: str | bytes | bytearray) -> str:
    """Normalize an MQTT payload received with or without text decoding."""
    return payload.decode() if isinstance(payload, bytes | bytearray) else payload


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
        (MQTT_AVAILABILITY_TOPIC, lambda message: _handle_availability(runtime, message)),
        (MQTT_INFO_TOPIC, lambda message: _handle_info(runtime, message)),
        (MQTT_STATUS_TOPIC, lambda message: _handle_status(runtime, message)),
    )
    try:
        for topic, handler in subscriptions:
            runtime.unsubscribers.append(await mqtt.async_subscribe(hass, topic, handler, qos=1))
        _register_services(hass, runtime)
    except BaseException:
        _clear_domain_registrations(hass, runtime)
        raise
    runtime.initialized = True


def _known_remote_pairs(hass: HomeAssistant) -> set[tuple[int, int]]:
    """Return remote identities already stored in config entries."""
    pairs: set[tuple[int, int]] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        values: dict[str, object] = dict(entry.data)
        values.update(entry.options)
        try:
            config = BlindConfig.from_mapping(values)
        except TypeError, ValueError:
            continue
        pairs.add((config.remote.prefix, config.remote.remote_id))
    return pairs


def _register_services(
    hass: HomeAssistant,
    runtime: DomainRuntime,
) -> None:
    """Register domain services once while at least one entry is loaded."""
    import voluptuous as vol
    from homeassistant.core import SupportsResponse

    async def async_send_raw(call: ServiceCall) -> None:
        await runtime.hub.async_send_raw(
            str(call.data[ATTR_BRIDGE]),
            str(call.data[ATTR_RAW]),
            int(call.data[ATTR_REPEATS]),
        )

    async def async_new_virtual_remote(_call: ServiceCall) -> ServiceResponse:
        used = _known_remote_pairs(hass)
        while True:
            pair = (0x5C0000 | secrets.randbelow(1 << 16), secrets.randbelow(1 << 8))
            if pair not in used:
                break
        # A virtual remote is never captured over the air, so synthesize a
        # complete, internally consistent calibration.  Returning the UP base
        # lets the config flow's manual path accept this identity directly
        # (prefix + remote id + UP calibration base).
        bases = synthesize_bases(pair[1], secrets.randbelow(1 << 8))
        return {
            CONF_PREFIX: f"0x{pair[0]:06x}",
            CONF_REMOTE_ID: f"0x{pair[1]:02x}",
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
        supports_response=SupportsResponse.OPTIONAL,
    )


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
    for unsubscribe in runtime.unsubscribers:
        unsubscribe()
    runtime.unsubscribers.clear()
    if hass.services.has_service(DOMAIN, SERVICE_SEND_RAW):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_RAW)
    if hass.services.has_service(DOMAIN, SERVICE_NEW_VIRTUAL_REMOTE):
        hass.services.async_remove(DOMAIN, SERVICE_NEW_VIRTUAL_REMOTE)
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
