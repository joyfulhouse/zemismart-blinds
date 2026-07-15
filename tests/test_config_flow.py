"""Tests for per-remote calibration in the add/edit flow."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant import config_entries, loader
from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.data_entry_flow import FlowResultType

import custom_components.zemismart_blinds as integration_module
import custom_components.zemismart_blinds.config_flow as config_flow_module
from custom_components.zemismart_blinds.codec import (
    CommandBases,
    derive_bases_from_base,
    encode_b0,
    make_payload,
)
from custom_components.zemismart_blinds.config_flow import _config_from_input
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
    CONF_KNOWN_REMOTE,
    CONF_NAME,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    CONF_TRAVEL_DOWN,
    CONF_TRAVEL_UP,
    DOMAIN,
    MANUAL_REMOTE,
    MQTT_AVAILABILITY_TOPIC,
    MQTT_INFO_TOPIC,
    MQTT_ROOT,
)
from custom_components.zemismart_blinds.models import BlindConfig, RemoteIdentity
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


def details_input(
    *,
    name: str | None = "Test Shade",
    channels: str = "1,2",
    area_id: str = "living_room",
    travel_up: float = 15,
    travel_down: float = 16,
    repeats: int = 5,
    coalesce_window_ms: int = 150,
) -> dict[str, Any]:
    """Return the shared details form shape, including its collapsed section."""
    values: dict[str, Any] = {
        CONF_CHANNELS: channels,
        CONF_TRAVEL_UP: travel_up,
        CONF_TRAVEL_DOWN: travel_down,
        CONF_AREA_ID: area_id,
        ADVANCED_SECTION: {
            CONF_REPEATS: repeats,
            CONF_COALESCE_WINDOW_MS: coalesce_window_ms,
        },
    }
    if name is not None:
        values[CONF_NAME] = name
    return values


def real_entry(
    entry_id: str,
    config: BlindConfig,
    *,
    options: dict[str, object] | None = None,
) -> ConfigEntry[Any]:
    """Build one real config entry around backward-compatible stored data."""
    return ConfigEntry(
        data=config.as_dict(),
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=1,
        options=options or {},
        source=config_entries.SOURCE_USER,
        subentries_data=None,
        title=config.name,
        unique_id=f"{config.remote_key}:{'-'.join(map(str, config.channels))}",
        version=1,
    )


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


def current_flow(hass: HomeAssistant, flow_id: str) -> ConfigFlowResult:
    """Return the current public flow result."""
    return hass.config_entries.flow.async_get(flow_id)


def manual_input(**overrides: object) -> dict[str, Any]:
    """Return representative manual flow input with one explicit UP base."""
    values: dict[str, Any] = {
        CONF_NAME: "Test Shade",
        CONF_KNOWN_REMOTE: MANUAL_REMOTE,
        CONF_PREFIX: "a1b2c3",
        CONF_REMOTE_ID: "42",
        CONF_CHANNELS: "1",
        CONF_TRAVEL_UP: 15,
        CONF_TRAVEL_DOWN: 15,
        CONF_AREA_ID: "living_room",
        CONF_REPEATS: 5,
        CONF_CALIBRATION_BUTTON: "UP",
        CONF_CALIBRATION_BASE: "f42a",
        CONF_CALIBRATION_FRAME: "",
    }
    values.update(overrides)
    return values


def test_manual_flow_derives_action_bases_from_one_direct_base() -> None:
    """A labeled per-remote base is enough to persist all three action bases."""
    config = _config_from_input(manual_input(), {})

    assert config.remote.bases == CommandBases(0xF42A, 0xBCF2, 0xDC12)


def test_manual_flow_accepts_direct_base_with_opcode_carry() -> None:
    """A base that generates a channel-1 f5 command still completes correctly."""
    config = _config_from_input(
        manual_input(
            **{
                CONF_PREFIX: "0ff1ce",
                CONF_REMOTE_ID: "10",
                CONF_CALIBRATION_BASE: "f52f",
            }
        ),
        {},
    )

    assert config.remote.bases == CommandBases(0xF52F, 0xBCF7, 0xDD17)


def test_manual_unknown_remote_requires_a_calibration_source() -> None:
    """New arbitrary identities cannot enter without a calibration source."""
    with pytest.raises(ValueError, match="calibration"):
        _config_from_input(
            manual_input(
                **{
                    CONF_CALIBRATION_BASE: "",
                    CONF_CALIBRATION_FRAME: "",
                }
            ),
            {},
        )


def test_manual_flow_derives_bases_from_captured_reference() -> None:
    """A labeled B0 reference supplies identity, channels, and command calibration."""
    config = _config_from_input(
        manual_input(
            **{
                CONF_PREFIX: f"{REF_PREFIX:06x}",
                CONF_REMOTE_ID: f"{REF_REMOTE_ID:02x}",
                CONF_CALIBRATION_BASE: "",
                CONF_CALIBRATION_FRAME: REFERENCE_FRAME,
            }
        ),
        {},
    )

    assert config.remote.bases == CommandBases(REF_BASES.up, REF_BASES.down, REF_BASES.stop)


def test_manual_flow_rejects_ambiguous_or_wrong_identity_reference() -> None:
    """A calibration source must be singular and belong to the entered remote."""
    with pytest.raises(ValueError, match="either"):
        _config_from_input(
            manual_input(**{CONF_CALIBRATION_FRAME: REFERENCE_FRAME}),
            {},
        )
    with pytest.raises(ValueError, match="identity"):
        _config_from_input(
            manual_input(
                **{
                    CONF_CALIBRATION_BASE: "",
                    CONF_CALIBRATION_FRAME: REFERENCE_FRAME,
                }
            ),
            {},
        )


def test_known_remote_reuse_keeps_its_calibration() -> None:
    """Selecting an existing remote reuses its bases without manual calibration fields."""
    remote = RemoteIdentity(0x7E55AA, 0xE5, CommandBases(0xF38F, 0xBC57, 0xDB77))
    config = _config_from_input(
        manual_input(
            **{
                CONF_KNOWN_REMOTE: remote.key,
                CONF_CALIBRATION_BASE: "",
                CONF_CALIBRATION_FRAME: "",
            }
        ),
        {remote.key: (remote, "Bedroom")},
    )

    assert config.remote == remote


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
async def test_learn_happy_path_creates_backward_compatible_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid B1 press drives the wizard through confirmation and persistence."""
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
    assert result["menu_options"] == ["learn_details", "learn_retry", "advanced"]
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
        {"next_step_id": "learn_details"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "learn_details"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        details_input(name=None, travel_up=17, travel_down=18),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room shade"
    expected = BlindConfig(
        name="Living room shade",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        channels=(1, 2),
        travel_up=17,
        travel_down=18,
        area_id="living_room",
        repeats=5,
        coalesce_window_ms=150,
    )
    assert result["data"] == expected.as_dict()
    assert result["result"].unique_id == "a1b2c3:42:1-2"
    assert CONF_BRIDGE not in result["data"]
    assert CONF_CALIBRATION_FRAME not in result["data"]

    flow_type, reuse_flow_id = result["next_flow"]
    assert flow_type is config_entries.FlowType.CONFIG_FLOW
    assert current_flow(hass, reuse_flow_id)["step_id"] == "reuse"
    reuse = await hass.config_entries.flow.async_configure(reuse_flow_id)
    assert reuse["type"] is FlowResultType.FORM
    assert reuse["step_id"] == "reuse"
    reuse_schema = reuse["data_schema"]
    assert reuse_schema is not None
    assert reuse_schema({})[CONF_KNOWN_REMOTE] == expected.remote_key

    continuation = await hass.config_entries.flow.async_configure(
        reuse_flow_id,
        {CONF_KNOWN_REMOTE: expected.remote_key},
    )
    assert continuation["type"] is FlowResultType.FORM
    assert continuation["step_id"] == "advanced_details"
    hass.config_entries.flow.async_abort(reuse_flow_id)


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


@pytest.mark.parametrize("path", ("reuse", "manual", "virtual"))
@pytest.mark.asyncio
async def test_advanced_paths_create_backward_compatible_entries(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """Known, manual, and virtual identities all retain the existing data contract."""
    prepare_config_flow(hass, monkeypatch)
    seed = BlindConfig(
        name="Existing shade",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        channels=(1,),
        travel_up=10,
        travel_down=11,
        area_id="living_room",
        repeats=5,
        coalesce_window_ms=150,
    )
    await hass.config_entries.async_add(real_entry("seed", seed))
    virtual_bases = derive_bases_from_base("UP", 0xF42A, 0x56)
    monkeypatch.setattr(
        integration_module,
        "new_virtual_remote_identity",
        lambda _hass: (0x5C1234, 0x56, virtual_bases),
    )
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "advanced"},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["reuse", "manual", "virtual"]
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": path},
    )

    if path == "reuse":
        assert result["step_id"] == "reuse"
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_KNOWN_REMOTE: seed.remote_key},
        )
        expected_remote = seed.remote
    elif path == "manual":
        assert result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_PREFIX: "123456",
                CONF_REMOTE_ID: "0d",
                CONF_CALIBRATION_BUTTON: "UP",
                CONF_CALIBRATION_BASE: "f449",
                CONF_CALIBRATION_FRAME: "",
                CONF_BASE_TRAILER: "",
            },
        )
        expected_remote = RemoteIdentity(0x123456, 0x0D, CommandBases(0xF449, 0xBD11, 0xDD31))
    else:
        expected_remote = RemoteIdentity(0x5C1234, 0x56, virtual_bases)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "advanced_details"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        details_input(name=f"{path.title()} shade", channels="3"),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    configured = BlindConfig.from_mapping(result["data"])
    assert configured.remote == expected_remote
    assert configured.channels == (3,)
    assert set(result["data"]) == set(configured.as_dict())


@pytest.mark.asyncio
async def test_reconfigure_relearn_reloads_and_clears_stale_options(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relearning replaces data, clears option precedence, and reloads the same entry."""
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    old = BlindConfig(
        name="Old shade",
        remote=RemoteIdentity(0x123456, 0x0D, CommandBases(0xF449, 0xBD11, 0xDD31)),
        channels=(1,),
        travel_up=10,
        travel_down=11,
        area_id="living_room",
        repeats=4,
        coalesce_window_ms=100,
    )
    stale_options = {
        CONF_BASE_UP: "1111",
        CONF_BASE_DOWN: "2222",
        CONF_BASE_STOP: "3333",
        CONF_BASE_TRAILER: "4444",
        CONF_TRAVEL_UP: 99,
    }
    entry = real_entry("reconfigure-me", old, options=stale_options)
    await hass.config_entries.async_add(entry)
    reloads: list[str] = []
    monkeypatch.setattr(
        hass.config_entries,
        "async_schedule_reload",
        lambda entry_id: reloads.append(entry_id),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    flow_id = result["flow_id"]
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    assert result["menu_options"] == ["reconfigure_learn", "reconfigure_edit"]

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "reconfigure_learn"},
    )
    assert result["step_id"] == "learn_setup"
    setup_schema = result["data_schema"]
    assert setup_schema is not None
    setup_values = setup_schema(
        {
            CONF_NAME: "Relearned shade",
            CONF_AREA_ID: "living_room",
        }
    )
    assert setup_values[CONF_BRIDGE] == config_flow_module._AUTOMATIC_BRIDGE
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        setup_values,
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    assert fake.published[0][0] == "rf433/bridge-a/cmd"
    await fake.emit(
        fake.rx_subscriptions()[0],
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 7}),
    )
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_confirm"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn_details"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "learn_details"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        details_input(name=None, travel_up=20, travel_down=21, repeats=6),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Relearned shade"
    assert entry.unique_id == "a1b2c3:42:1-2"
    expected = BlindConfig(
        name="Relearned shade",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        channels=(1, 2),
        travel_up=20,
        travel_down=21,
        area_id="living_room",
        repeats=6,
        coalesce_window_ms=150,
    )
    assert entry.data == expected.as_dict()
    assert entry.options == {}
    assert reloads == [entry.entry_id]


@pytest.mark.asyncio
async def test_reconfigure_edit_keeps_remote_and_clears_options(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings-only reconfigure retains calibration while removing option shadowing."""
    prepare_config_flow(hass, monkeypatch)
    original = BlindConfig(
        name="Original shade",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        channels=(1,),
        travel_up=10,
        travel_down=11,
        area_id="living_room",
        repeats=5,
        coalesce_window_ms=150,
    )
    sibling = real_entry(
        "edit-sibling",
        BlindConfig(
            name="Sibling shade",
            remote=original.remote,
            channels=(3,),
            travel_up=10,
            travel_down=11,
            area_id="living_room",
            repeats=5,
            coalesce_window_ms=150,
        ),
    )
    await hass.config_entries.async_add(sibling)
    effective_bases = derive_bases_from_base("UP", 0xF43A, TEST_REMOTE_ID)
    entry = real_entry(
        "edit-me",
        original,
        options={
            CONF_BASE_UP: f"{effective_bases.up:04x}",
            CONF_BASE_DOWN: f"{effective_bases.down:04x}",
            CONF_BASE_STOP: f"{effective_bases.stop:04x}",
            CONF_TRAVEL_UP: 99,
            CONF_COALESCE_WINDOW_MS: 600,
        },
    )
    await hass.config_entries.async_add(entry)
    reloads: list[str] = []
    monkeypatch.setattr(
        hass.config_entries,
        "async_schedule_reload",
        lambda entry_id: reloads.append(entry_id),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "reconfigure_edit"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_edit"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        details_input(
            name="Edited shade",
            channels="1,2",
            area_id="bedroom",
            travel_up=22,
            travel_down=23,
            repeats=7,
            coalesce_window_ms=250,
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    edited = BlindConfig.from_mapping(entry.data)
    assert edited.remote == RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, effective_bases)
    assert edited.name == "Edited shade"
    assert edited.channels == (1, 2)
    assert edited.area_id == "bedroom"
    assert edited.travel_up == 22
    assert edited.coalesce_window_ms == 250
    assert entry.options == {}
    assert BlindConfig.from_mapping(sibling.data).remote == edited.remote
    assert reloads == [sibling.entry_id, entry.entry_id]


@pytest.mark.asyncio
async def test_options_flow_still_edits_travel_and_area(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy options entry point remains usable for timing and area edits."""
    prepare_config_flow(hass, monkeypatch)
    original = BlindConfig(
        name="Options shade",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        channels=(1,),
        travel_up=10,
        travel_down=11,
        area_id="living_room",
        repeats=5,
        coalesce_window_ms=150,
    )
    entry = real_entry("options-me", original)
    await hass.config_entries.async_add(entry)
    reloads: list[str] = []
    monkeypatch.setattr(
        hass.config_entries,
        "async_schedule_reload",
        lambda entry_id: reloads.append(entry_id),
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        details_input(
            name="Options shade",
            channels="1",
            area_id="bedroom",
            travel_up=30,
            travel_down=31,
            repeats=5,
            coalesce_window_ms=150,
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    effective = BlindConfig.from_mapping({**entry.data, **entry.options})
    assert effective.remote == original.remote
    assert effective.area_id == "bedroom"
    assert effective.travel_up == 30
    assert effective.travel_down == 31
    assert reloads == [entry.entry_id]
