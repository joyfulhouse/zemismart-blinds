"""Real Home Assistant fixture tests for the shared integration runtime."""

from __future__ import annotations

import asyncio
import json
import secrets
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

import custom_components.zemismart_blinds as integration_module
from custom_components.zemismart_blinds import (
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.zemismart_blinds.codec import CommandBases, derive_bases_from_base
from custom_components.zemismart_blinds.const import (
    DOMAIN,
    MQTT_AVAILABILITY_TOPIC,
    MQTT_INFO_TOPIC,
    MQTT_STATUS_TOPIC,
    SERVICE_NEW_VIRTUAL_REMOTE,
    SERVICE_SEND_RAW,
)
from custom_components.zemismart_blinds.models import CommandAck, DomainRuntime, EntryRuntime

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant


def config_entry(entry_id: str) -> ConfigEntry[EntryRuntime]:
    """Build one real HA ConfigEntry without invoking the flow manager."""
    return ConfigEntry(
        data={
            "name": f"Blind {entry_id}",
            "prefix": "a1b2c3",
            "remote_id": "42",
            "channels": [1],
            "travel_up": 14.0,
            "travel_down": 13.0,
            "area_id": "living_room",
            "repeats": 5,
            "base_up": "f42a",
            "base_down": "bcf2",
            "base_stop": "dc12",
        },
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title=f"Blind {entry_id}",
        unique_id=f"a1b2c3:42:{entry_id}",
        version=1,
    )


def message(topic: str, payload: str, *, retain: bool) -> ReceiveMessage:
    """Build one actual MQTT ReceiveMessage for a wildcard subscription."""
    return ReceiveMessage(
        topic=topic,
        payload=payload,
        qos=1,
        retain=retain,
        subscribed_topic=topic,
        timestamp=1.0,
    )


@pytest.mark.asyncio
async def test_concurrent_setup_and_unload_share_one_runtime(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent entries install exactly three subscriptions/services and release them once."""
    callbacks: dict[str, Callable[[ReceiveMessage], None]] = {}
    unsubscribed: list[str] = []
    runtime_visible_before_subscribe: list[bool] = []

    async def subscribe(
        _hass: HomeAssistant,
        topic: str,
        callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        runtime_visible_before_subscribe.append(DOMAIN in hass.data)
        await asyncio.sleep(0)
        callbacks[topic] = callback
        return lambda: unsubscribed.append(topic)

    async def publish(
        _hass: HomeAssistant,
        _topic: str,
        _payload: str,
        *,
        qos: int,
        retain: bool,
    ) -> None:
        assert qos == 1
        assert not retain

    async def forward(_entry: ConfigEntry[EntryRuntime], _platforms: list[Any]) -> None:
        await asyncio.sleep(0)

    async def unload(_entry: ConfigEntry[EntryRuntime], _platforms: list[Any]) -> bool:
        await asyncio.sleep(0)
        return True

    async def assign_area(
        _hass: HomeAssistant,
        _entry: ConfigEntry[EntryRuntime],
        _config: Any,
    ) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    monkeypatch.setattr(integration_module, "_async_assign_device_area", assign_area)
    first = config_entry("one")
    second = config_entry("two")

    assert await async_setup(hass, {})
    setup_results = await asyncio.gather(
        async_setup_entry(hass, first),
        async_setup_entry(hass, second),
    )
    assert len(setup_results) == 2
    assert all(setup_results)

    runtime = hass.data[DOMAIN]
    assert isinstance(runtime, DomainRuntime)
    assert runtime.loaded_entries == {"one", "two"}
    assert set(callbacks) == {
        MQTT_AVAILABILITY_TOPIC,
        MQTT_INFO_TOPIC,
        MQTT_STATUS_TOPIC,
    }
    assert runtime_visible_before_subscribe == [True, True, True]
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_RAW)
    assert hass.services.has_service(DOMAIN, SERVICE_NEW_VIRTUAL_REMOTE)

    unload_results = await asyncio.gather(
        async_unload_entry(hass, first),
        async_unload_entry(hass, second),
    )
    assert len(unload_results) == 2
    assert all(unload_results)

    assert DOMAIN not in hass.data
    assert sorted(unsubscribed) == sorted(callbacks)
    # Domain services persist for the integration's lifetime so the virtual
    # remote workflow works before/after any entry exists.
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_RAW)
    assert hass.services.has_service(DOMAIN, SERVICE_NEW_VIRTUAL_REMOTE)


@pytest.mark.asyncio
async def test_setup_waiter_survives_final_unload_generation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final unload cannot clean a runtime while a setup user waits for its lock."""
    subscribe_count = 0
    unsubscribed = 0
    unload_entered = asyncio.Event()
    release_unload = asyncio.Event()

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        nonlocal subscribe_count, unsubscribed
        assert qos == 1
        subscribe_count += 1

        def unsubscribe() -> None:
            nonlocal unsubscribed
            unsubscribed += 1

        return unsubscribe

    async def forward(_entry: ConfigEntry[EntryRuntime], _platforms: list[Any]) -> None:
        return

    async def unload(entry: ConfigEntry[EntryRuntime], _platforms: list[Any]) -> bool:
        if entry.entry_id == "one":
            unload_entered.set()
            await release_unload.wait()
        return True

    async def assign_area(
        _hass: HomeAssistant,
        _entry: ConfigEntry[EntryRuntime],
        _config: Any,
    ) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    monkeypatch.setattr(integration_module, "_async_assign_device_area", assign_area)
    first = config_entry("one")
    second = config_entry("two")
    await async_setup_entry(hass, first)
    runtime: DomainRuntime = hass.data[DOMAIN]

    unloading = asyncio.create_task(async_unload_entry(hass, first))
    await unload_entered.wait()
    setting_up = asyncio.create_task(async_setup_entry(hass, second))
    await asyncio.sleep(0)
    assert runtime.setup_users == 1

    release_unload.set()
    assert await unloading
    assert await setting_up

    assert hass.data[DOMAIN] is runtime
    assert runtime.loaded_entries == {"two"}
    assert subscribe_count == 3
    assert unsubscribed == 0

    assert await async_unload_entry(hass, second)
    assert DOMAIN not in hass.data
    assert unsubscribed == 3


@pytest.mark.asyncio
async def test_retained_discovery_order_and_status_ack_filtering(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Info/availability commute, while retained or malformed statuses never ack TX."""
    callbacks: dict[str, Callable[[ReceiveMessage], None]] = {}
    published: list[tuple[str, dict[str, Any]]] = []
    published_event = asyncio.Event()

    async def subscribe(
        _hass: HomeAssistant,
        topic: str,
        callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        callbacks[topic] = callback
        return lambda: None

    async def publish(
        _hass: HomeAssistant,
        topic: str,
        payload: str,
        *,
        qos: int,
        retain: bool,
    ) -> None:
        assert qos == 1
        assert not retain
        published.append((topic, json.loads(payload)))
        published_event.set()

    async def forward(_entry: ConfigEntry[EntryRuntime], _platforms: list[Any]) -> None:
        return

    async def assign_area(
        _hass: HomeAssistant,
        _entry: ConfigEntry[EntryRuntime],
        _config: Any,
    ) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(integration_module, "_async_assign_device_area", assign_area)
    entry = config_entry("one")
    await async_setup_entry(hass, entry)
    runtime: DomainRuntime = hass.data[DOMAIN]

    callbacks[MQTT_INFO_TOPIC](
        message("rf433/bridge-a/info", '{"area":"living_room","default":true}', retain=True)
    )
    callbacks[MQTT_AVAILABILITY_TOPIC](
        message("rf433/bridge-a/availability", "online", retain=True)
    )
    bridge = runtime.hub.registry.resolve("living_room")
    assert bridge.area_id == "living_room"
    assert bridge.online
    assert bridge.is_default

    callbacks[MQTT_INFO_TOPIC](message("rf433/bridge-a/info", "{bad", retain=True))
    callbacks[MQTT_AVAILABILITY_TOPIC](
        message("rf433/bridge-a/availability", "online", retain=True)
    )
    assert runtime.hub.registry.resolve("living_room") == bridge

    transmit = asyncio.create_task(
        entry.runtime_data.hub.async_transmit(entry.runtime_data.config, "UP")
    )
    await published_event.wait()
    body = published[0][1]
    status_payload = json.dumps(
        {
            "status": "accepted",
            "command_id": body["command_id"],
        }
    )
    callbacks[MQTT_STATUS_TOPIC](message("rf433/bridge-a/status", status_payload, retain=True))
    callbacks[MQTT_STATUS_TOPIC](message("rf433/bridge-a/status", "not-json", retain=False))
    await asyncio.sleep(0)
    assert not transmit.done()

    callbacks[MQTT_STATUS_TOPIC](message("rf433/bridge-a/status", status_payload, retain=False))
    await asyncio.sleep(0)
    assert not transmit.done()
    callbacks[MQTT_STATUS_TOPIC](
        message(
            "rf433/bridge-a/status",
            json.dumps({"status": "started", "command_id": body["command_id"]}),
            retain=False,
        )
    )
    ack = await transmit
    assert isinstance(ack, CommandAck)
    assert ack.bridge.bridge_id == "bridge-a"


@pytest.mark.asyncio
async def test_virtual_remote_keeps_known_family_prefix(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Virtual identities randomize the low prefix bits, remote byte, and calibration seed."""

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        return lambda: None

    async def forward(_entry: ConfigEntry[EntryRuntime], _platforms: list[Any]) -> None:
        return

    async def assign_area(
        _hass: HomeAssistant,
        _entry: ConfigEntry[EntryRuntime],
        _config: Any,
    ) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(integration_module, "_async_assign_device_area", assign_area)
    values = iter((0x1234, 0x56, 0x2B))
    monkeypatch.setattr(secrets, "randbelow", lambda _limit: next(values))
    assert await async_setup(hass, {})
    await async_setup_entry(hass, config_entry("one"))

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_NEW_VIRTUAL_REMOTE,
        {},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert response["prefix"] == "0x5c1234"
    assert response["remote_id"] == "0x56"

    base_up = int(str(response["base_up"]), 16)
    base_down = int(str(response["base_down"]), 16)
    base_stop = int(str(response["base_stop"]), 16)
    assert all(0 <= value <= 0xFFFF for value in (base_up, base_down, base_stop))

    # The returned UP base alone must reconstruct the whole calibration, so the
    # response is directly usable in the manual config flow's direct-base path.
    assert derive_bases_from_base("UP", base_up, 0x56) == CommandBases(
        base_up, base_down, base_stop
    )


@pytest.mark.asyncio
async def test_send_raw_service_rejects_malformed_input_before_mqtt(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed service input is rejected on the real HA service path before publication."""
    callbacks: dict[str, Callable[[ReceiveMessage], None]] = {}
    published: list[str] = []

    async def subscribe(
        _hass: HomeAssistant,
        topic: str,
        callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        callbacks[topic] = callback
        return lambda: None

    async def publish(
        _hass: HomeAssistant,
        _topic: str,
        payload: str,
        *,
        qos: int,
        retain: bool,
    ) -> None:
        published.append(payload)

    async def forward(_entry: ConfigEntry[EntryRuntime], _platforms: list[Any]) -> None:
        return

    async def assign_area(
        _hass: HomeAssistant,
        _entry: ConfigEntry[EntryRuntime],
        _config: Any,
    ) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(integration_module, "_async_assign_device_area", assign_area)
    assert await async_setup(hass, {})
    await async_setup_entry(hass, config_entry("one"))
    callbacks[MQTT_AVAILABILITY_TOPIC](
        message("rf433/bridge-a/availability", "online", retain=True)
    )

    with pytest.raises(HomeAssistantError, match="hex"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_RAW,
            {"bridge": "bridge-a", "raw": "AAB0GG55", "repeats": 1},
            blocking=True,
        )

    assert published == []
