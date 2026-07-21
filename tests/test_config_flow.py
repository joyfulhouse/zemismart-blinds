"""Tests for per-remote calibration in the add/edit flow."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import pytest
from homeassistant import config_entries, loader
from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.data_entry_flow import FlowResultType

import custom_components.zemismart_blinds.config_flow as config_flow_module
from custom_components.zemismart_blinds.codec import (
    derive_bases_from_base,
    encode_b0,
    make_payload,
)
from custom_components.zemismart_blinds.const import (
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
    CONF_NAME,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    CONF_TRAVEL_DOWN,
    CONF_TRAVEL_UP,
    DOMAIN,
    MQTT_AVAILABILITY_TOPIC,
    MQTT_INFO_TOPIC,
    MQTT_ROOT,
)
from custom_components.zemismart_blinds.models import (
    BlindConfig,
    CoverConfig,
    RemoteConfig,
    RemoteIdentity,
)
from tests.synthetic import (
    SYNTHETIC_REMOTES,
    TEST_ACTION_BASES,
    TEST_BASES,
    TEST_CH12_DOWN_B0,
    TEST_CH12_UP_B0,
    TEST_PREFIX,
    TEST_REMOTE_ID,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
    from homeassistant.core import HomeAssistant

    type MessageCallback = Callable[
        [ReceiveMessage],
        Coroutine[Any, Any, None] | None,
    ]

# A synthetic remote used as the "captured reference" calibration source. Its
# channel-1 UP frame is generated with the hardware-validated codec, so the
# flow's decode/derive path is exercised without any real capture material.
_name, REF_PREFIX, REF_REMOTE_ID, REF_BASES, _payload = SYNTHETIC_REMOTES[1]
REFERENCE_FRAME = encode_b0(make_payload(REF_PREFIX, REF_REMOTE_ID, (1,), "UP", bases=REF_BASES))
SECOND_REMOTE_UP_B0 = encode_b0(
    make_payload(REF_PREFIX, REF_REMOTE_ID, (1, 2), "UP", bases=REF_BASES)
)
ADVANCED_SECTION = "advanced"


def b0_to_b1(frame: str) -> str:
    """Convert a stored byte-exact B0 vector into its Portisch B1 capture form."""
    body = frame[6:-2]
    return f"AAB1{body[:2]}{body[4:]}3855"


REFERENCE_UP_B1 = b0_to_b1(TEST_CH12_UP_B0)
REFERENCE_DOWN_B1 = b0_to_b1(TEST_CH12_DOWN_B0)
REFERENCE_TRAILER_B1 = b0_to_b1(
    encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "TRAILER", bases=TEST_BASES))
)


@dataclass
class Subscription:
    """One MQTT subscription, retained after cleanup for late-frame tests."""

    topic: str
    callback: MessageCallback
    active: bool = True
    ready: bool = False
    unsubscribe_count: int = 0


class FakeMqtt:
    """Flow-local MQTT transport with retained bridge discovery messages."""

    def __init__(self, bridges: dict[str, dict[str, object]] | None = None) -> None:
        """Initialize transport state and deterministic retained metadata."""
        self.bridges = (
            bridges
            if bridges is not None
            else {
                "bridge-a": {"area_id": "living_room"},
                "bridge-b": {
                    "area_id": "bedroom",
                    "default": True,
                },
            }
        )
        self.subscriptions: list[Subscription] = []
        self.subscribe_done_callbacks: dict[tuple[str, int], list[Callable[[], None]]] = {}
        self.activation_gates: dict[str, asyncio.Event] = {}
        self.published: list[tuple[str, dict[str, object]]] = []
        self.changed = asyncio.Event()

    def async_on_subscribe_done(
        self,
        hass: HomeAssistant,
        topic: str,
        qos: int,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        """Track broker readiness independently from local registration."""
        key = (topic, qos)
        callbacks = self.subscribe_done_callbacks.setdefault(key, [])
        callbacks.append(callback)
        if any(
            subscription.topic == topic and subscription.ready
            for subscription in self.subscriptions
        ):
            hass.loop.call_soon(callback)

        def unsubscribe() -> None:
            callbacks.remove(callback)
            if not callbacks:
                del self.subscribe_done_callbacks[key]

        return unsubscribe

    async def async_subscribe(
        self,
        _hass: HomeAssistant,
        topic: str,
        callback: MessageCallback,
        qos: int = 0,
        encoding: str | None = "utf-8",
    ) -> Callable[[], None]:
        """Register locally, then acknowledge and deliver retained state later."""
        del encoding
        assert qos == 1
        subscription = Subscription(topic, callback)
        self.subscriptions.append(subscription)
        _hass.async_create_task(
            self._activate(subscription, qos),
            "fake MQTT subscription activation",
        )

        def unsubscribe() -> None:
            assert subscription.active
            subscription.active = False
            subscription.unsubscribe_count += 1
            self.changed.set()

        return unsubscribe

    async def async_publish(
        self,
        _hass: HomeAssistant,
        topic: str,
        payload: str | bytes | int | float | None,
        qos: int | None = 0,
        retain: bool | None = False,
        encoding: str | None = "utf-8",
    ) -> None:
        """Record one JSON command publication."""
        del encoding
        assert qos == 1
        assert retain is False
        assert isinstance(payload, str)
        decoded: object = json.loads(payload)
        assert isinstance(decoded, dict)
        if decoded.get("action") == "sniff" and decoded.get("seconds") != 0:
            bridge = topic.split("/")[1]
            assert any(
                subscription.topic == f"{MQTT_ROOT}/{bridge}/rx"
                and subscription.active
                and subscription.ready
                for subscription in self.subscriptions
            )
        self.published.append((topic, {str(key): value for key, value in decoded.items()}))
        self.changed.set()

    async def emit(
        self,
        subscription: Subscription,
        topic: str,
        payload: str,
        *,
        retain: bool = False,
    ) -> None:
        """Deliver a live or deliberately late message to one callback."""
        await self._deliver(subscription, topic, payload, retain=retain)

    async def wait_for_publications(self, count: int) -> None:
        """Wait without polling until at least ``count`` commands were published."""
        while len(self.published) < count:
            await self.changed.wait()
            self.changed.clear()

    def rx_subscriptions(self) -> list[Subscription]:
        """Return exact bridge RX subscriptions in creation order."""
        return [
            subscription
            for subscription in self.subscriptions
            if subscription.topic.endswith("/rx")
        ]

    async def _activate(self, subscription: Subscription, qos: int) -> None:
        """Model a broker SUBACK occurring after local registration."""
        await asyncio.sleep(0)
        if gate := self.activation_gates.get(subscription.topic):
            await gate.wait()
        if not subscription.active:
            return
        subscription.ready = True
        for callback in tuple(self.subscribe_done_callbacks.get((subscription.topic, qos), ())):
            callback()
        await asyncio.sleep(0)
        if not subscription.active:
            return
        if subscription.topic == MQTT_AVAILABILITY_TOPIC:
            for bridge in self.bridges:
                await self._deliver(
                    subscription,
                    f"{MQTT_ROOT}/{bridge}/availability",
                    "online",
                    retain=True,
                )
        elif subscription.topic == MQTT_INFO_TOPIC:
            for bridge, info in self.bridges.items():
                await self._deliver(
                    subscription,
                    f"{MQTT_ROOT}/{bridge}/info",
                    json.dumps(info),
                    retain=True,
                )

    async def _deliver(
        self,
        subscription: Subscription,
        topic: str,
        payload: str,
        *,
        retain: bool,
    ) -> None:
        """Invoke one callback on the HA event loop."""
        result = subscription.callback(
            ReceiveMessage(
                topic=topic,
                payload=payload,
                qos=1,
                retain=retain,
                subscribed_topic=subscription.topic,
                timestamp=1.0,
            )
        )
        if inspect.isawaitable(result):
            await result
        await asyncio.sleep(0)


def prepare_config_flow(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> None:
    """Register the already-imported custom config flow with the test loader."""
    loader.async_setup(hass)
    hass.data[loader.DATA_COMPONENTS][f"{DOMAIN}.config_flow"] = config_flow_module
    if monkeypatch is None:
        return

    async def async_setup_entry(_entry_id: str) -> bool:
        return True

    monkeypatch.setattr(hass.config_entries, "async_setup", async_setup_entry)


def install_mqtt(monkeypatch: pytest.MonkeyPatch, fake: FakeMqtt) -> None:
    """Install the flow-local MQTT transport and broker-readiness boundary."""
    monkeypatch.setattr(mqtt, "async_subscribe", fake.async_subscribe)
    monkeypatch.setattr(mqtt, "async_on_subscribe_done", fake.async_on_subscribe_done)
    monkeypatch.setattr(mqtt, "async_publish", fake.async_publish)
    # Leave one bounded scheduling window for every retained info document;
    # a zero sleep can unsubscribe after only the first bridge callback yields.
    monkeypatch.setattr(config_flow_module, "_BRIDGE_DISCOVERY_SECONDS", 0.001, raising=False)


async def start_user_flow(hass: HomeAssistant) -> ConfigFlowResult:
    """Start a real user flow and return its menu result."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )


async def advance_to_learn_setup(hass: HomeAssistant, flow_id: str) -> ConfigFlowResult:
    """Choose Learn and return the discovered setup form."""
    return await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn"},
    )


async def create_remote_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    covers: list[dict[str, Any]],
    *,
    prefix: str = "a1b2c3",
    remote_id: str = "42",
    base: str = "f42a",
    name: str = "Kitchen remote",
) -> ConfigEntry:
    """Drive the manual wizard to a real remote entry with the given covers."""
    prepare_config_flow(hass, monkeypatch)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "advanced"})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_PREFIX: prefix,
            CONF_REMOTE_ID: remote_id,
            CONF_CALIBRATION_BUTTON: "UP",
            CONF_CALIBRATION_BASE: base,
            CONF_CALIBRATION_FRAME: "",
            CONF_BASE_TRAILER: "",
        },
    )
    await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: name,
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {
                CONF_REPEATS: 5,
                CONF_COALESCE_WINDOW_MS: 150,
            },
        },
    )
    for index, cover in enumerate(covers):
        await hass.config_entries.flow.async_configure(flow_id, cover)
        if index < len(covers) - 1:
            await hass.config_entries.flow.async_configure(
                flow_id,
                {"next_step_id": "cover"},
            )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "finish"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result["result"]


def current_flow(hass: HomeAssistant, flow_id: str) -> ConfigFlowResult:
    """Return the current public flow result."""
    return hass.config_entries.flow.async_get(flow_id)


def schema_suggested_values(schema: Any) -> dict[str, object]:
    """Serialize suggested values from a Home Assistant form schema."""
    return {
        str(marker.schema): marker.description["suggested_value"]
        for marker in schema.schema
        if marker.description and "suggested_value" in marker.description
    }


def manual_input(**overrides: object) -> dict[str, Any]:
    """Return representative manual identity input with one explicit UP base."""
    values: dict[str, Any] = {
        CONF_PREFIX: "a1b2c3",
        CONF_REMOTE_ID: "42",
        CONF_CALIBRATION_BUTTON: "UP",
        CONF_CALIBRATION_BASE: "f42a",
        CONF_CALIBRATION_FRAME: "",
    }
    values.update(overrides)
    return values


def stored_config_entry(
    data: dict[str, object],
    *,
    entry_id: str,
    title: str,
    unique_id: str,
) -> ConfigEntry:
    """Build a real stored entry without driving the config flow."""
    return config_entries.ConfigEntry(
        data=data,
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options={},
        source=config_entries.SOURCE_USER,
        subentries_data=None,
        title=title,
        unique_id=unique_id,
        version=1,
    )


def test_manual_identity_derives_action_bases_from_one_direct_base() -> None:
    """A labeled per-remote base is enough to derive all three action bases."""
    identity = config_flow_module._remote_identity_from_manual(manual_input())
    assert identity.prefix == TEST_PREFIX
    assert identity.remote_id == TEST_REMOTE_ID
    assert identity.bases == derive_bases_from_base("UP", 0xF42A, TEST_REMOTE_ID)


def test_manual_identity_requires_a_calibration_source() -> None:
    """An unknown remote with neither base nor reference is rejected."""
    with pytest.raises(ValueError, match="calibration"):
        config_flow_module._remote_identity_from_manual(
            manual_input(calibration_base="", prefix="000001", remote_id="02")
        )


def test_manual_identity_derives_bases_from_captured_reference() -> None:
    """A captured reference frame for the same identity calibrates the remote."""
    identity = config_flow_module._remote_identity_from_manual(
        manual_input(
            prefix=f"{REF_PREFIX:06x}",
            remote_id=f"{REF_REMOTE_ID:02x}",
            calibration_base="",
            calibration_frame=REFERENCE_FRAME,
        )
    )
    assert identity.bases is not None
    assert identity.bases.up == REF_BASES.up


def test_manual_identity_rejects_wrong_identity_reference() -> None:
    """A reference captured from a different remote must not calibrate this one."""
    with pytest.raises(ValueError, match="identity"):
        config_flow_module._remote_identity_from_manual(
            manual_input(calibration_base="", calibration_frame=REFERENCE_FRAME)
        )
    with pytest.raises(ValueError, match="not both"):
        config_flow_module._remote_identity_from_manual(
            manual_input(calibration_frame=REFERENCE_FRAME)
        )


def test_validate_cover_input_travel_required_for_born_leaf() -> None:
    """A cover that contains no collected cover must supply both travel times."""
    cover, errors = config_flow_module._validate_cover_input(
        {CONF_NAME: "Sink", CONF_CHANNELS: "5"},
        [],
    )
    assert cover is None
    assert errors == {"base": "travel_required"}


def test_validate_cover_input_laminar_errors() -> None:
    """Duplicates and partial overlaps map to channel-field form errors."""
    collected: list[tuple[int, ...]] = [(1, 2, 3)]
    _cover, errors = config_flow_module._validate_cover_input(
        {
            CONF_NAME: "X",
            CONF_CHANNELS: "2,3,4",
            CONF_TRAVEL_UP: 5,
            CONF_TRAVEL_DOWN: 5,
        },
        collected,
    )
    assert errors == {CONF_CHANNELS: "overlapping_channels"}
    _cover, errors = config_flow_module._validate_cover_input(
        {
            CONF_NAME: "X",
            CONF_CHANNELS: "3,2,1",
            CONF_TRAVEL_UP: 5,
            CONF_TRAVEL_DOWN: 5,
        },
        collected,
    )
    assert errors == {CONF_CHANNELS: "duplicate_channels"}


def test_validate_cover_input_born_aggregate_travel_optional() -> None:
    """Strictly containing a collected cover lifts the travel requirement."""
    collected: list[tuple[int, ...]] = [(1, 2, 3), (4,)]
    cover, errors = config_flow_module._validate_cover_input(
        {CONF_NAME: "Kitchen shades", CONF_CHANNELS: "1,2,3,4,5,6"},
        collected,
    )
    assert errors == {}
    assert cover is not None
    assert cover.channel_key == "1-2-3-4-5-6"
    assert cover.travel_up is None


def test_remote_centric_flow_copy_is_complete_and_synchronized() -> None:
    """Remote and subentry copy stays exact in both English JSON files."""
    integration_dir = Path(__file__).parents[1] / "custom_components" / DOMAIN
    strings_bytes = (integration_dir / "strings.json").read_bytes()
    translations_bytes = (integration_dir / "translations" / "en.json").read_bytes()
    assert strings_bytes == translations_bytes
    strings = json.loads(strings_bytes)

    assert strings["config"]["step"]["user"] == {
        "title": "Add a Zemismart remote",
        "description": (
            "Learn the remote automatically, or use an Advanced setup method. "
            "You will add its covers (blinds and groups) next."
        ),
        "menu_options": {
            "learn": "Learn from remote",
            "advanced": "Advanced",
        },
    }
    assert strings["config"]["error"]["already_configured"] == (
        "This remote is already configured by another entry."
    )
    assert strings["config"]["abort"]["legacy_not_supported"] == (
        "This entry uses the old per-blind format. Delete it and add its remote "
        "again instead of reconfiguring."
    )
    assert strings["config_subentries"]["cover"]["abort"] == {
        "reconfigure_successful": "The cover was reconfigured successfully.",
        "already_configured": ("Another cover of this remote already uses exactly these channels."),
    }


@pytest.mark.asyncio
async def test_user_starts_with_learn_and_advanced_menu(hass: Any) -> None:
    """The guided Learn path is the first choice, with fallbacks behind Advanced."""
    prepare_config_flow(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["learn", "advanced"]


@pytest.mark.asyncio
async def test_legacy_entry_cannot_reconfigure_or_manage_subentries(
    hass: HomeAssistant,
) -> None:
    """Legacy per-blind entries stay outside remote-only management flows."""
    prepare_config_flow(hass)
    legacy = BlindConfig(
        name="Legacy blind",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        channels=(1, 2),
        travel_up=12.0,
        travel_down=12.0,
        area_id="kitchen",
        repeats=5,
    )
    legacy_entry = stored_config_entry(
        legacy.as_dict(),
        entry_id="legacy-entry",
        title=legacy.name,
        unique_id=f"{legacy.remote.key}:1-2",
    )
    remote = RemoteConfig(
        name="Remote",
        remote=legacy.remote,
        area_id="kitchen",
        repeats=5,
    )
    remote_entry = stored_config_entry(
        remote.as_dict(),
        entry_id="remote-entry",
        title=remote.name,
        unique_id=remote.key,
    )

    assert (
        config_flow_module.ZemismartBlindsConfigFlow.async_get_supported_subentry_types(
            legacy_entry
        )
        == {}
    )
    assert config_flow_module.ZemismartBlindsConfigFlow.async_get_supported_subentry_types(
        remote_entry
    ) == {"cover": config_flow_module.CoverSubentryFlow}

    await hass.config_entries.async_add(legacy_entry)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": legacy_entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "legacy_not_supported"


@pytest.mark.asyncio
async def test_learn_wizard_creates_remote_entry_with_cover_subentries(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard captures a remote, then collects covers into subentries."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]

    result = await advance_to_learn_setup(hass, flow_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "learn_setup"
    discovery = fake.subscriptions[:2]
    assert {subscription.topic for subscription in discovery} == {
        MQTT_AVAILABILITY_TOPIC,
        MQTT_INFO_TOPIC,
    }
    assert all(not subscription.active for subscription in discovery)
    assert all(subscription.ready for subscription in discovery)
    assert all(subscription.unsubscribe_count == 1 for subscription in discovery)
    assert fake.subscribe_done_callbacks == {}
    setup_schema = result["data_schema"]
    assert setup_schema is not None
    setup_values = setup_schema(
        {
            CONF_NAME: "Living room shade",
            CONF_AREA_ID: "living_room",
        }
    )
    assert setup_values[CONF_BRIDGE] == config_flow_module._AUTOMATIC_BRIDGE

    result = await hass.config_entries.flow.async_configure(flow_id, setup_values)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "learn_sniff"
    assert result["progress_action"] == "sniffing"
    await fake.wait_for_publications(1)
    rx = fake.rx_subscriptions()[0]
    assert rx.ready
    assert fake.published[0] == (
        "rf433/bridge-a/cmd",
        {"action": "sniff", "seconds": 30},
    )

    await fake.emit(
        rx,
        "rf433/bridge-b/rx",
        json.dumps({"frame": REFERENCE_DOWN_B1, "t": 1}),
    )
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_DOWN_B1, "t": 2}),
        retain=True,
    )
    await fake.emit(rx, "rf433/bridge-a/rx", "not-json")
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_TRAILER_B1, "t": 2}),
    )
    assert current_flow(hass, flow_id)["step_id"] == "learn_sniff"
    assert len(fake.published) == 1

    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 3}),
    )
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "learn_confirm"
    assert result["menu_options"] == [
        "remote_settings",
        "learn_retry",
        "advanced",
    ]
    placeholders = result["description_placeholders"]
    assert placeholders == {
        "prefix": "0xa1b2c3",
        "remote_id": "0x42",
        "channels": "1,2",
        "button": "UP",
        "name": "Living room shade",
        "bridge": "bridge-a",
    }
    assert REFERENCE_UP_B1 not in placeholders.values()
    assert rx.unsubscribe_count == 1
    assert fake.published[1] == (
        "rf433/bridge-a/cmd",
        {"action": "sniff", "seconds": 0},
    )

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "remote_settings"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "remote_settings"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kitchen remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {
                CONF_REPEATS: 5,
                CONF_COALESCE_WINDOW_MS: 150,
            },
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cover"
    schema = result["data_schema"]
    assert schema is not None
    assert (
        schema({CONF_NAME: "Slider", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12})[CONF_CHANNELS]
        == "1,2"
    )

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Slider",
            CONF_CHANNELS: "1,2",
            CONF_TRAVEL_UP: 12,
            CONF_TRAVEL_DOWN: 12,
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "cover_menu"
    assert result["menu_options"] == ["cover", "finish"]

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "cover"},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Bad",
            CONF_CHANNELS: "2,3",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNELS: "overlapping_channels"}
    error_schema = result["data_schema"]
    assert error_schema is not None
    error_suggestions = schema_suggested_values(error_schema)
    assert error_suggestions[CONF_TRAVEL_UP] == 9
    assert error_suggestions[CONF_TRAVEL_DOWN] == 9
    assert CONF_TRAVEL_UP not in error_schema({})
    assert CONF_TRAVEL_DOWN not in error_schema({})
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Sink", CONF_CHANNELS: "5"},
    )
    assert result["errors"] == {"base": "travel_required"}
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Kitchen shades", CONF_CHANNELS: "1,2,3"},
    )
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "finish"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen remote"
    expected_remote = RemoteConfig(
        name="Kitchen remote",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        area_id="kitchen",
        repeats=5,
        coalesce_window_ms=150,
    )
    assert result["data"] == expected_remote.as_dict()
    entry = result["result"]
    assert entry.unique_id == "a1b2c3:42"
    subentries = list(entry.subentries.values())
    assert [(s.subentry_type, s.title, s.unique_id) for s in subentries] == [
        ("cover", "Slider", "1-2"),
        ("cover", "Kitchen shades", "1-2-3"),
    ]
    slider = CoverConfig.from_subentry(subentries[0].data)
    assert slider.channels == (1, 2)
    assert slider.travel_up == 12.0
    aggregate = CoverConfig.from_subentry(subentries[1].data)
    assert aggregate.travel_up is None


@pytest.mark.asyncio
async def test_learn_allows_explicit_online_bridge_override(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named online bridge overrides automatic area-based selection."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Override shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-b",
        },
    )

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    assert fake.published[0] == (
        "rf433/bridge-b/cmd",
        {"action": "sniff", "seconds": 30},
    )
    hass.config_entries.flow.async_abort(flow_id)
    await fake.wait_for_publications(2)


@pytest.mark.asyncio
async def test_advanced_setup_clears_learned_cover_channel_prefill(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving Learn for Advanced cannot leak captured channels into Manual."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Learned shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    rx = fake.rx_subscriptions()[0]
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 3}),
    )
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_confirm"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "advanced"},
    )
    assert result["step_id"] == "advanced"
    await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "manual"},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        manual_input(base_trailer=""),
    )
    assert result["step_id"] == "remote_settings"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Manual remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {
                CONF_REPEATS: 5,
                CONF_COALESCE_WINDOW_MS: 150,
            },
        },
    )

    assert result["step_id"] == "cover"
    schema = result["data_schema"]
    assert schema is not None
    assert (
        schema(
            {
                CONF_NAME: "Manual cover",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        )[CONF_CHANNELS]
        == ""
    )


@pytest.mark.asyncio
async def test_learn_timeout_retry_ignores_stale_session(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry owns a fresh task/session and cannot accept the prior callback's frame."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 0.001, raising=False)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Retry shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_timeout"
    assert result["menu_options"] == ["learn_retry", "advanced"]
    stale = fake.rx_subscriptions()[0]
    assert stale.unsubscribe_count == 1

    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 30.0, raising=False)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn_retry"},
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(3)
    current = fake.rx_subscriptions()[1]
    assert current is not stale

    await fake.emit(
        stale,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 4}),
    )
    await asyncio.sleep(0)
    assert current_flow(hass, flow_id)["step_id"] == "learn_sniff"
    await fake.emit(
        current,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_DOWN_B1, "t": 5}),
    )
    await fake.wait_for_publications(4)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_confirm"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["button"] == "DOWN"
    assert current.unsubscribe_count == 1
    assert fake.published[2][1] == {
        "action": "sniff",
        "seconds": 30,
    }
    assert fake.published[3][1] == {
        "action": "sniff",
        "seconds": 0,
    }
    hass.config_entries.flow.async_abort(flow_id)


@pytest.mark.asyncio
async def test_learn_subscription_readiness_uses_the_same_timeout_budget(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing RX SUBACK cannot extend the advertised capture deadline."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    rx_topic = "rf433/bridge-a/rx"
    readiness_gate = asyncio.Event()
    fake.activation_gates[rx_topic] = readiness_gate
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 0.01, raising=False)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Delayed subscription shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    assert fake.published == [
        (
            "rf433/bridge-a/cmd",
            {"action": "sniff", "seconds": 0},
        )
    ]
    rx = fake.rx_subscriptions()[0]
    assert not rx.active
    assert rx.unsubscribe_count == 1
    readiness_gate.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_timeout"


@pytest.mark.asyncio
async def test_learn_abort_cleans_capture_and_ignores_late_frame(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a progress flow unsubscribes and best-effort stops its sniff session."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Abort shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    stale = fake.rx_subscriptions()[0]

    hass.config_entries.flow.async_abort(flow_id)
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    assert stale.unsubscribe_count == 1
    assert fake.published == [
        (
            "rf433/bridge-a/cmd",
            {"action": "sniff", "seconds": 30},
        ),
        (
            "rf433/bridge-a/cmd",
            {"action": "sniff", "seconds": 0},
        ),
    ]
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    await fake.emit(
        stale,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 6}),
    )


@pytest.mark.asyncio
async def test_learn_serializes_concurrent_sniffs_on_one_bridge(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One flow cannot consume or stop another flow's bridge capture window."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    first = await start_user_flow(hass)
    second = await start_user_flow(hass)
    first_id = first["flow_id"]
    second_id = second["flow_id"]
    await advance_to_learn_setup(hass, first_id)
    await advance_to_learn_setup(hass, second_id)

    first = await hass.config_entries.flow.async_configure(
        first_id,
        {
            CONF_NAME: "First shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert first["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)

    second = await hass.config_entries.flow.async_configure(
        second_id,
        {
            CONF_NAME: "Second shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert second["type"] is FlowResultType.SHOW_PROGRESS
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second = await hass.config_entries.flow.async_configure(second_id)
    assert second["step_id"] == "learn_timeout"
    assert len(fake.rx_subscriptions()) == 1
    assert len(fake.published) == 1

    hass.config_entries.flow.async_abort(first_id)
    await fake.wait_for_publications(2)
    await asyncio.sleep(0)
    second = await hass.config_entries.flow.async_configure(
        second_id,
        {"next_step_id": "learn_retry"},
    )
    assert second["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(3)
    current = fake.rx_subscriptions()[1]
    await fake.emit(
        current,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 7}),
    )
    await fake.wait_for_publications(4)
    second = await hass.config_entries.flow.async_configure(second_id)
    assert second["step_id"] == "learn_confirm"
    hass.config_entries.flow.async_abort(second_id)


@pytest.mark.asyncio
async def test_learn_without_online_bridges_offers_advanced(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-install discovery failure remains recoverable through Advanced setup."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt(bridges={})
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)

    result = await advance_to_learn_setup(hass, result["flow_id"])

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "learn_unavailable"
    assert result["menu_options"] == ["learn_setup", "advanced"]


@pytest.mark.asyncio
async def test_reconfigure_learn_without_online_bridges_hides_advanced(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relearn failures cannot escape into new-entry Advanced setup paths."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    fake = FakeMqtt(bridges={})
    install_mqtt(monkeypatch, fake)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "reconfigure_learn"},
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "learn_unavailable"
    assert result["menu_options"] == ["learn_setup"]


@pytest.mark.asyncio
async def test_learn_without_mqtt_offers_advanced(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable first-install MQTT client fails closed but keeps fallbacks usable."""
    prepare_config_flow(hass, monkeypatch)

    async def mqtt_unavailable(_hass: HomeAssistant) -> bool:
        return False

    monkeypatch.setattr(mqtt, "async_wait_for_mqtt_client", mqtt_unavailable)
    result = await start_user_flow(hass)

    result = await advance_to_learn_setup(hass, result["flow_id"])

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "learn_unavailable"
    assert result["menu_options"] == ["learn_setup", "advanced"]


@pytest.mark.asyncio
async def test_manual_wizard_and_duplicate_remote_abort(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual identity enters the same wizard; a second identical remote aborts."""
    prepare_config_flow(hass, monkeypatch)

    async def run_manual_to_settings() -> tuple[str, ConfigFlowResult]:
        result = await start_user_flow(hass)
        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {"next_step_id": "advanced"},
        )
        assert result["menu_options"] == ["manual", "virtual"]
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {"next_step_id": "manual"},
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_PREFIX: "a1b2c3",
                CONF_REMOTE_ID: "42",
                CONF_CALIBRATION_BUTTON: "UP",
                CONF_CALIBRATION_BASE: "f42a",
                CONF_CALIBRATION_FRAME: "",
                CONF_BASE_TRAILER: "",
            },
        )
        assert result["step_id"] == "remote_settings"
        return flow_id, result

    flow_id, _ = await run_manual_to_settings()
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kitchen remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {
                CONF_REPEATS: 5,
                CONF_COALESCE_WINDOW_MS: 150,
            },
        },
    )
    assert result["step_id"] == "cover"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Sink",
            CONF_CHANNELS: "5",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "finish"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "a1b2c3:42"

    flow_id, _ = await run_manual_to_settings()
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Duplicate remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {
                CONF_REPEATS: 5,
                CONF_COALESCE_WINDOW_MS: 150,
            },
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_subentry_add_creates_cover(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote entry exposes a flow that adds one cover subentry."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2,3",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Sink",
            CONF_CHANNELS: "5",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = {subentry.unique_id: subentry for subentry in entry.subentries.values()}
    assert "5" in subentries
    assert subentries["5"].title == "Sink"


@pytest.mark.asyncio
async def test_subentry_add_rejects_partial_overlap_and_duplicate(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new cover must remain laminar with existing sibling covers."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2,3",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Bad",
            CONF_CHANNELS: "3,4",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNELS: "overlapping_channels"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Dup",
            CONF_CHANNELS: "1,2,3",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    assert result["errors"] == {CONF_CHANNELS: "duplicate_channels"}


@pytest.mark.asyncio
async def test_subentry_add_fails_closed_for_unparseable_sibling_channels(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable sibling blocks cover mutations instead of disappearing."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    sibling = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry,
        sibling,
        data={CONF_NAME: "x", CONF_CHANNELS: "not-a-channel"},
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={"source": config_entries.SOURCE_USER},
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Sink",
            CONF_CHANNELS: "5",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}


@pytest.mark.asyncio
async def test_subentry_add_validates_channels_from_malformed_travel_sibling(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parseable sibling channels still participate when its travel is corrupt."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    sibling = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry,
        sibling,
        data={
            CONF_NAME: "x",
            CONF_CHANNELS: "1,2",
            CONF_TRAVEL_UP: "garbage",
            CONF_TRAVEL_DOWN: "garbage",
        },
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={"source": config_entries.SOURCE_USER},
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Overlap",
            CONF_CHANNELS: "2,3",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNELS: "overlapping_channels"}


@pytest.mark.asyncio
async def test_subentry_reconfigure_prefills_display_values(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfigure suggestions display storage values in form-friendly shapes."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2,3",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 13,
            }
        ],
    )
    slider = next(iter(entry.subentries.values()))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": slider.subentry_id,
        },
    )
    schema = result["data_schema"]
    assert schema is not None
    suggested = schema_suggested_values(schema)
    assert suggested[CONF_CHANNELS] == "1,2,3"
    assert suggested[CONF_TRAVEL_UP] == 12.0
    assert suggested[CONF_TRAVEL_DOWN] == 13.0

    suggested[CONF_NAME] = "Renamed slider"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        schema(suggested),
    )

    assert result["type"] is FlowResultType.ABORT
    updated = next(iter(entry.subentries.values()))
    restored = CoverConfig.from_subentry(updated.data)
    assert updated.title == "Renamed slider"
    assert restored.channels == (1, 2, 3)
    assert restored.travel_up == 12.0
    assert restored.travel_down == 13.0


@pytest.mark.asyncio
async def test_subentry_reconfigure_carries_hidden_travel_forward(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfiguring into an aggregate retains stored travel calibration."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2,3",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            },
            {
                CONF_NAME: "Sink",
                CONF_CHANNELS: "5",
                CONF_TRAVEL_UP: 9,
                CONF_TRAVEL_DOWN: 9,
            },
        ],
    )
    sink = next(subentry for subentry in entry.subentries.values() if subentry.unique_id == "5")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": sink.subentry_id,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Kitchen shades", CONF_CHANNELS: "1,2,3,5"},
    )
    assert result["type"] is FlowResultType.ABORT
    updated = next(
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_id == sink.subentry_id
    )
    assert updated.unique_id == "1-2-3-5"
    assert updated.title == "Kitchen shades"
    restored = CoverConfig.from_subentry(updated.data)
    assert restored.travel_up == 9.0


@pytest.mark.asyncio
async def test_subentry_reconfigure_to_leaf_requires_travel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconfigured leaf needs submitted or previously stored travel times."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2,3",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            },
            {CONF_NAME: "All", CONF_CHANNELS: "1,2,3,4"},
        ],
    )
    aggregate = next(
        subentry for subentry in entry.subentries.values() if subentry.unique_id == "1-2-3-4"
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": aggregate.subentry_id,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Solo", CONF_CHANNELS: "6"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "travel_required"}


@pytest.mark.asyncio
async def test_reconfigure_edit_updates_settings_and_keeps_identity(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing settings updates mutable fields without replacing identity or covers."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2,3",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "reconfigure_edit"},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Kitchen remote",
            CONF_AREA_ID: "pantry",
            CONF_BASE_UP: "f42a",
            CONF_BASE_DOWN: "bcf2",
            CONF_BASE_STOP: "dc12",
            CONF_BASE_TRAILER: "dd05",
            ADVANCED_SECTION: {
                CONF_REPEATS: 8,
                CONF_COALESCE_WINDOW_MS: 0,
            },
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = RemoteConfig.from_entry(entry.data)
    assert updated.area_id == "pantry"
    assert updated.repeats == 8
    assert updated.key == "a1b2c3:42"
    assert updated.remote.bases is not None
    assert updated.remote.bases.trailer == 0xDD05
    assert [subentry.unique_id for subentry in entry.subentries.values()] == ["1-2-3"]


@pytest.mark.asyncio
async def test_reconfigure_relearn_applies_new_identity_and_collides(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relearning applies a captured identity while preserving entry metadata."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "reconfigure_learn"},
    )
    assert result["step_id"] == "learn_setup"
    flow_id = result["flow_id"]
    schema = result["data_schema"]
    assert schema is not None
    setup_values = schema({})
    assert setup_values[CONF_NAME] == "Kitchen remote"
    setup_values[CONF_NAME] = "Renamed remote"
    setup_values[CONF_AREA_ID] = "pantry"
    result = await hass.config_entries.flow.async_configure(flow_id, setup_values)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    rx = fake.rx_subscriptions()[0]
    # Emit on the subscription's own topic: the edited area ("pantry")
    # matches no fake bridge, so automatic selection routes to the default
    # bridge — hardcoding a bridge id here would miss the capture handler's
    # exact-topic check.
    await fake.emit(
        rx,
        rx.topic,
        json.dumps({"frame": b0_to_b1(SECOND_REMOTE_UP_B0), "t": 3}),
    )
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_confirm"
    assert result["menu_options"] == ["reconfigure_apply", "learn_retry"]
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "reconfigure_apply"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = RemoteConfig.from_entry(entry.data)
    assert updated.key == f"{REF_PREFIX:06x}:{REF_REMOTE_ID:02x}"
    assert entry.unique_id == updated.key
    assert updated.name == "Renamed remote"
    assert updated.area_id == "pantry"
    assert entry.title == "Renamed remote"
    assert [subentry.unique_id for subentry in entry.subentries.values()] == ["1-2"]


@pytest.mark.asyncio
async def test_reconfigure_relearn_collision_aborts(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relearning an identity another entry already owns aborts unchanged."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            }
        ],
    )
    await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Other blind",
                CONF_CHANNELS: "1",
                CONF_TRAVEL_UP: 10,
                CONF_TRAVEL_DOWN: 10,
            }
        ],
        prefix=f"{REF_PREFIX:06x}",
        remote_id=f"{REF_REMOTE_ID:02x}",
        base=f"{REF_BASES.up:04x}",
        name="Bedroom remote",
    )
    original_data = dict(entry.data)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "reconfigure_learn"},
    )
    flow_id = result["flow_id"]
    schema = result["data_schema"]
    assert schema is not None
    result = await hass.config_entries.flow.async_configure(flow_id, schema({}))
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    rx = fake.rx_subscriptions()[0]
    await fake.emit(
        rx,
        rx.topic,
        json.dumps({"frame": b0_to_b1(SECOND_REMOTE_UP_B0), "t": 3}),
    )
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_confirm"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "reconfigure_apply"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(entry.data) == original_data
    assert entry.unique_id == "a1b2c3:42"


@pytest.mark.asyncio
async def test_learn_ignores_our_own_transmission_echo(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command we are transmitting must not be learned as a remote press."""
    from types import SimpleNamespace

    from custom_components.zemismart_blinds.models import (
        BridgeRegistry,
        RemoteRuntime,
        ZemismartHub,
    )

    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "online")

    async def publish(topic: str, payload: str) -> None:
        """Complete both firmware lifecycle statuses for the published command."""
        body = json.loads(payload)
        bridge_id = topic.split("/")[1]
        for status in ("accepted", "started"):
            hub.handle_status(
                bridge_id,
                bytearray(
                    json.dumps({"status": status, "command_id": body["command_id"]}).encode()
                ),
            )

    hub = ZemismartHub(registry, publish)
    remote = RemoteConfig(
        name="Living Room",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        area_id="living_room",
        repeats=2,
        coalesce_window_ms=0,
    )
    entry = SimpleNamespace(runtime_data=RemoteRuntime(remote=remote, hub=hub))
    monkeypatch.setattr(
        hass.config_entries,
        "async_entries",
        lambda _domain: [entry],
    )
    blind = BlindConfig(
        name="Living Room",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1, 2),
        travel_up=14.0,
        travel_down=13.0,
        area_id="living_room",
        repeats=2,
    )

    # Before we transmit, an identical capture is a genuine remote press.
    assert config_flow_module._is_own_emission(hass, TEST_CH12_UP_B0) is False

    # Transmitting it makes the very same capture our own echo off the bridge.
    await hub.async_transmit(blind, "UP", stop_after_ms=None)

    assert config_flow_module._is_own_emission(hass, TEST_CH12_UP_B0) is True


@pytest.mark.asyncio
async def test_sniff_handler_skips_our_echo_but_accepts_a_real_press(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard HANDLER itself must consult _is_own_emission, not just exist."""
    from types import SimpleNamespace

    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    session_id = "session-echo"
    flow._sniff_session_id = session_id
    topic = "rf433/rf433-bridge-office/rx"

    def deliver(future: asyncio.Future[Any]) -> None:
        config_flow_module._handle_sniff_message(
            flow,
            session_id,
            topic,
            future,
            cast(
                "ReceiveMessage",
                SimpleNamespace(
                    topic=topic,
                    payload=json.dumps({"frame": TEST_CH12_UP_B0}),
                    retain=False,
                ),
            ),
        )

    # Classified as our own echo -> the capture is dropped.
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: True)
    echo_future: asyncio.Future[Any] = hass.loop.create_future()
    deliver(echo_future)
    assert not echo_future.done(), "our own transmission must not be learned"

    # Classified as foreign -> it is a real remote press and gets captured.
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: False)
    press_future: asyncio.Future[Any] = hass.loop.create_future()
    deliver(press_future)
    assert press_future.done()
    assert press_future.result().button == "UP"
    echo_future.cancel()
