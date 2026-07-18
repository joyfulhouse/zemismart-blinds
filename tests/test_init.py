"""Real Home Assistant fixture tests for the shared integration runtime."""

from __future__ import annotations

import asyncio
import json
import secrets
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

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
    MQTT_RX_TOPIC,
    MQTT_STATUS_TOPIC,
    SERVICE_NEW_VIRTUAL_REMOTE,
    SERVICE_SEND_RAW,
)
from custom_components.zemismart_blinds.models import (
    BlindConfig,
    BridgeRegistry,
    CommandAck,
    DomainRuntime,
    RemoteRuntime,
    ZemismartHub,
)
from custom_components.zemismart_blinds.state_sync import (
    HeardEvent,
    LedgerFrameSpec,
    frame_signature,
)
from tests.synthetic import TEST_BASES, TEST_CH12_UP_B0, TEST_PREFIX, TEST_REMOTE_ID

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


def config_entry(entry_id: str) -> ConfigEntry[RemoteRuntime]:
    """Build one real remote-format ConfigEntry without invoking the flow manager."""
    return ConfigEntry(
        data={
            "name": f"Remote {entry_id}",
            "prefix": "a1b2c3",
            "remote_id": "42",
            "area_id": "living_room",
            "repeats": 5,
            "coalesce_window_ms": 150,
            "base_up": "f42a",
            "base_down": "bcf2",
            "base_stop": "dc12",
            "base_trailer": "",
        },
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title=f"Remote {entry_id}",
        unique_id="a1b2c3:42",
        version=1,
    )


def legacy_config_entry(entry_id: str) -> ConfigEntry[RemoteRuntime]:
    """Build one retired per-blind entry kept only as migration reference."""
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


def add_to_manager(hass: HomeAssistant, entry: ConfigEntry[RemoteRuntime]) -> None:
    """Register a hand-built entry so registries can link devices to it."""
    hass.config_entries._entries[entry.entry_id] = entry


def message(
    topic: str,
    payload: str,
    *,
    retain: bool,
    timestamp: float = 1.0,
) -> ReceiveMessage:
    """Build one actual MQTT ReceiveMessage for a wildcard subscription."""
    return ReceiveMessage(
        topic=topic,
        payload=payload,
        qos=1,
        retain=retain,
        subscribed_topic=topic,
        timestamp=timestamp,
    )


@pytest.mark.asyncio
async def test_remote_format_entry_sets_up_with_no_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-centric entry loads the shared runtime and adds no covers yet."""
    from homeassistant import config_entries as ha_config_entries

    from custom_components.zemismart_blinds.models import (
        RemoteConfig,
        RemoteIdentity,
        RemoteRuntime,
    )

    remote = RemoteConfig(
        name="Kitchen remote",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        area_id="kitchen",
        repeats=5,
    )
    entry = ConfigEntry(
        data=remote.as_dict(),
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id="remote-entry-1",
        minor_version=1,
        options={},
        source=ha_config_entries.SOURCE_USER,
        subentries_data=None,
        title=remote.name,
        unique_id=remote.key,
        version=1,
    )

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        return lambda: None

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    add_to_manager(hass, entry)
    assert await async_setup_entry(hass, entry)

    runtime = entry.runtime_data
    assert isinstance(runtime, RemoteRuntime)
    assert runtime.remote == remote
    assert [state for state in hass.states.async_all("cover")] == []


def test_rx_handler_drops_retained_and_malformed_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a live JSON-object RX message reaches the synchronous hub callback."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    runtime = DomainRuntime(hub=hub, unsubscribers=[])
    handled: list[tuple[str, Mapping[str, object]]] = []

    def handle_rx(
        bridge_id: str,
        payload: Mapping[str, object],
    ) -> None:
        handled.append((bridge_id, payload))

    monkeypatch.setattr(hub, "handle_rx", handle_rx)
    payload = json.dumps({"frame": TEST_CH12_UP_B0, "t": 1, "boot": 1})

    integration_module._handle_rx(
        runtime,
        message("rf433/bridge-a/rx", payload, retain=True),
    )
    integration_module._handle_rx(
        runtime,
        message("rf433/bridge-a/rx", "{malformed", retain=False),
    )
    integration_module._handle_rx(
        runtime,
        message("rf433/bridge-a/rx", payload, retain=False),
    )

    assert handled == [
        (
            "bridge-a",
            {"frame": TEST_CH12_UP_B0, "t": 1, "boot": 1},
        )
    ]


def test_rx_handler_uses_hub_clock_for_confirmed_echo() -> None:
    """HA's monotonic-domain MQTT timestamp cannot classify RF ledger time."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    wall_time = 1_700_000_000.0
    mqtt_timestamp = 12_345.67
    hub = ZemismartHub(BridgeRegistry(), publish, now=lambda: wall_time)
    runtime = DomainRuntime(hub=hub, unsubscribers=[])
    signature = frame_signature(TEST_CH12_UP_B0)
    assert signature is not None
    remote_key, channels, button = signature
    hub._ledger.register_pending(
        "command-1",
        "bridge-a",
        tuple(channels),
        button,
        [LedgerFrameSpec(signature, offset_ms=0, airtime_ms=500)],
    )
    hub._ledger.confirm("command-1", wall_time)
    events: list[HeardEvent] = []
    hub.register_rx_listener(remote_key, channels, events.append)
    payload = json.dumps({"frame": TEST_CH12_UP_B0, "t": 1, "boot": 1})

    integration_module._handle_rx(
        runtime,
        message(
            "rf433/bridge-b/rx",
            payload,
            retain=False,
            timestamp=mqtt_timestamp,
        ),
    )

    assert events == []
    assert hub.was_emission_proven("command-1")


@pytest.mark.asyncio
async def test_concurrent_setup_and_unload_share_one_runtime(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent entries install exactly four subscriptions/services and release them once."""
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

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        await asyncio.sleep(0)

    async def unload(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> bool:
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    first = config_entry("one")
    second = config_entry("two")
    add_to_manager(hass, first)
    add_to_manager(hass, second)

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
        MQTT_RX_TOPIC,
        MQTT_STATUS_TOPIC,
    }
    assert runtime_visible_before_subscribe == [True, True, True, True]
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

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        return

    async def unload(entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> bool:
        if entry.entry_id == "one":
            unload_entered.set()
            await release_unload.wait()
        return True

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    first = config_entry("one")
    second = config_entry("two")
    add_to_manager(hass, first)
    add_to_manager(hass, second)
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
    assert subscribe_count == 4
    assert unsubscribed == 0

    assert await async_unload_entry(hass, second)
    assert DOMAIN not in hass.data
    assert unsubscribed == 4


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

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    entry = config_entry("one")
    add_to_manager(hass, entry)
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

    transmit_config = BlindConfig(
        name="Blind one",
        remote=entry.runtime_data.remote.remote,
        channels=(1,),
        travel_up=14.0,
        travel_down=13.0,
        area_id="living_room",
        repeats=5,
    )
    transmit = asyncio.create_task(entry.runtime_data.hub.async_transmit(transmit_config, "UP"))
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

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    values = iter((0x1234, 0x56, 0x2B))
    monkeypatch.setattr(secrets, "randbelow", lambda _limit: next(values))
    assert await async_setup(hass, {})
    entry_one = config_entry("one")
    add_to_manager(hass, entry_one)
    await async_setup_entry(hass, entry_one)

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

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    assert await async_setup(hass, {})
    entry_one = config_entry("one")
    add_to_manager(hass, entry_one)
    await async_setup_entry(hass, entry_one)
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


@pytest.mark.asyncio
async def test_legacy_entry_fails_setup_and_keeps_data(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retired per-blind entry never loads; its data stays for migration."""
    from homeassistant.exceptions import ConfigEntryError

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        return lambda: None

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    entry = legacy_config_entry("legacy-one")
    add_to_manager(hass, entry)
    original = dict(entry.data)
    with pytest.raises(ConfigEntryError, match="retired per-blind format"):
        await async_setup_entry(hass, entry)
    assert dict(entry.data) == original
    assert DOMAIN not in hass.data  # no shared runtime was leaked


@pytest.mark.asyncio
async def test_remote_entry_builds_leaf_entities_and_devices(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaf subentries become subentry-bound covers sharing the remote's device."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.zemismart_blinds import cover as cover_module

    entry = ConfigEntry(
        data=config_entry("ignored").data,
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id="remote-topology",
        minor_version=1,
        options={},
        source="user",
        subentries_data=[
            {
                "data": {
                    "name": "Slider",
                    "channels": [1, 2, 3],
                    "travel_up": 12.0,
                    "travel_down": 12.0,
                },
                "subentry_type": "cover",
                "title": "Slider",
                "unique_id": "1-2-3",
            },
            {
                "data": {"name": "Sink", "channels": [5], "travel_up": 9.0, "travel_down": 9.0},
                "subentry_type": "cover",
                "title": "Sink",
                "unique_id": "5",
            },
            {
                "data": {
                    "name": "Kitchen shades",
                    "channels": [1, 2, 3, 4, 5, 6],
                    "travel_up": "",
                    "travel_down": "",
                },
                "subentry_type": "cover",
                "title": "Kitchen shades",
                "unique_id": "1-2-3-4-5-6",
            },
        ],
        title="Kitchen remote",
        unique_id="a1b2c3:42",
        version=1,
    )
    add_to_manager(hass, entry)

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        return lambda: None

    added: list[tuple[list[Any], str | None]] = []

    def record_add(
        entities: list[Any],
        update_before_add: bool = False,
        *,
        config_subentry_id: str | None = None,
    ) -> None:
        del update_before_add
        added.append((list(entities), config_subentry_id))

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        await cover_module.async_setup_entry(
            hass,
            entry,
            cast("AddConfigEntryEntitiesCallback", record_add),
        )

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    # A per-cover child device left behind by the pre-0.3.1 layout.
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "stale-child-subentry")},
        name="Old child device",
    )

    assert await async_setup_entry(hass, entry)

    # One entity per subentry, each bound to its own subentry id.
    by_subentry = {subentry.unique_id: subentry for subentry in entry.subentries.values()}
    assert len(added) == 3
    bound_ids = {config_subentry_id for _entities, config_subentry_id in added}
    assert bound_ids == {
        by_subentry["1-2-3"].subentry_id,
        by_subentry["5"].subentry_id,
        by_subentry["1-2-3-4-5-6"].subentry_id,
    }
    from custom_components.zemismart_blinds.cover import ZemismartAggregateCover

    aggregate_subentry_id = by_subentry["1-2-3-4-5-6"].subentry_id
    for entities, config_subentry_id in added:
        assert len(entities) == 1
        entity = entities[0]
        assert entity.unique_id == config_subentry_id
        assert entity._config.area_id == "living_room"  # inherited from remote
        assert entity._config.repeats == 5
        # Covers are entities INSIDE the remote's device, carrying their own
        # unprefixed name (deployed friendly names must stay byte-stable).
        assert entity.device_info == {"identifiers": {(DOMAIN, entry.entry_id)}}
        assert entity.has_entity_name is False
        assert entity.name == entity._config.name
        if config_subentry_id == aggregate_subentry_id:
            assert isinstance(entity, ZemismartAggregateCover)
        else:
            assert not isinstance(entity, ZemismartAggregateCover)
    runtime_data = entry.runtime_data
    assert runtime_data.coordinator is not None
    assert runtime_data.coordinator.members == {
        aggregate_subentry_id: (
            by_subentry["1-2-3"].subentry_id,
            by_subentry["5"].subentry_id,
        )
    }

    # The stale pre-0.3.1 child device was pruned; only the remote remains.
    assert registry.async_get(stale.id) is None
    entry_devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    assert [device.identifiers for device in entry_devices] == [{(DOMAIN, entry.entry_id)}]

    # Remote device exists with the remote's area; reload keeps a user override.
    parent = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert parent is not None
    assert parent.area_id == "living_room"
    registry.async_update_device(parent.id, area_id="pantry")
    integration_module._ensure_remote_device(hass, entry)
    parent_after = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert parent_after is not None
    assert parent_after.area_id == "pantry"

    await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_update_listener_schedules_one_reload_per_mutation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry updates and native subentry mutations each reload exactly once."""
    from types import MappingProxyType as _MPT

    from homeassistant.config_entries import ConfigSubentry

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        return lambda: None

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        return

    reloads: list[str] = []

    def schedule_reload(entry_id: str) -> None:
        reloads.append(entry_id)

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", schedule_reload)
    entry = config_entry("one")
    add_to_manager(hass, entry)
    await async_setup_entry(hass, entry)

    # Entry data mutation -> exactly one scheduled reload.
    hass.config_entries.async_update_entry(entry, data={**entry.data, "repeats": 9})
    await hass.async_block_till_done()
    assert reloads == ["one"]

    # Native subentry addition -> exactly one more.
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=_MPT({"name": "Sink", "channels": [5], "travel_up": 9.0, "travel_down": 9.0}),
            subentry_type="cover",
            title="Sink",
            unique_id="5",
        ),
    )
    await hass.async_block_till_done()
    assert reloads == ["one", "one"]

    # A no-op update fires nothing.
    hass.config_entries.async_update_entry(entry, data=dict(entry.data))
    await hass.async_block_till_done()
    assert reloads == ["one", "one"]

    await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_underivable_cover_skips_without_failing_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A demoted aggregate without travel loses its entity, not the entry."""
    from custom_components.zemismart_blinds import cover as cover_module

    entry = ConfigEntry(
        data=config_entry("ignored").data,
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id="remote-demoted",
        minor_version=1,
        options={},
        source="user",
        subentries_data=[
            {
                # Former aggregate, members deleted: no travel stored, and no
                # contained cover left to make it an aggregate again.
                "data": {"name": "Orphan", "channels": [1, 2], "travel_up": "", "travel_down": ""},
                "subentry_type": "cover",
                "title": "Orphan",
                "unique_id": "1-2",
            },
            {
                "data": {"name": "Solo", "channels": [5], "travel_up": 9.0, "travel_down": 9.0},
                "subentry_type": "cover",
                "title": "Solo",
                "unique_id": "5",
            },
        ],
        title="Kitchen remote",
        unique_id="a1b2c3:42",
        version=1,
    )
    add_to_manager(hass, entry)

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        return lambda: None

    added: list[Any] = []

    def record_add(
        entities: list[Any],
        update_before_add: bool = False,
        *,
        config_subentry_id: str | None = None,
    ) -> None:
        del update_before_add, config_subentry_id
        added.extend(entities)

    async def forward(_entry: Any, _platforms: list[Any]) -> None:
        await cover_module.async_setup_entry(
            hass,
            entry,
            cast("AddConfigEntryEntitiesCallback", record_add),
        )

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    assert await async_setup_entry(hass, entry)
    assert [entity._config.name for entity in added] == ["Solo"]
    await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_cleared_device_area_survives_reload(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user's explicit no-area choice is never re-assigned on reload."""
    from homeassistant.helpers import device_registry as dr

    async def subscribe(
        _hass: HomeAssistant,
        _topic: str,
        _callback: Callable[[ReceiveMessage], None],
        qos: int,
    ) -> Callable[[], None]:
        assert qos == 1
        return lambda: None

    async def forward(_entry: ConfigEntry[RemoteRuntime], _platforms: list[Any]) -> None:
        return

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    entry = config_entry("one")
    add_to_manager(hass, entry)
    await async_setup_entry(hass, entry)

    registry = dr.async_get(hass)
    parent = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert parent is not None
    assert parent.area_id == "living_room"
    registry.async_update_device(parent.id, area_id=None)

    integration_module._ensure_remote_device(hass, entry)
    parent_after = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert parent_after is not None
    assert parent_after.area_id is None

    await async_unload_entry(hass, entry)
