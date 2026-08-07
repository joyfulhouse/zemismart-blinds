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
    CommandBases,
    derive_base,
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
    CONF_COVER_ID,
    CONF_COVERS,
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
REFERENCE_STOP_B1 = b0_to_b1(
    encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "STOP", bases=TEST_BASES))
)

# An R11-shaped remote: its calibration-normalised action commands carry the
# opcode bytes F3/BC/DB, which _ACTION_COMMAND_HIGH does not contain, while the
# low-byte offsets (-0x38 DOWN, -0x18 STOP from UP) hold exactly as they do on
# all eleven remotes surveyed in #26. Only UP and STOP fall outside the table,
# which is precisely why the pre-#26 wizard captured DOWN and then invented an
# UP command the motor ignored. Identity and low bytes are fabricated -- the
# real remote's are not committed to a public repository.
UNTABLED_PREFIX = 0xC0FFEE
UNTABLED_REMOTE_ID = 0x5A
UNTABLED_CALIBRATION_COMMANDS = {"UP": 0xF37A, "DOWN": 0xBC42, "STOP": 0xDB62}
UNTABLED_BASES = CommandBases(
    **{
        action.lower(): derive_base((1, 2, 3, 4, 5, 6), action, command, UNTABLED_REMOTE_ID)
        for action, command in UNTABLED_CALIBRATION_COMMANDS.items()
    }
)
UNTABLED_UP_B1, UNTABLED_DOWN_B1, UNTABLED_STOP_B1 = (
    b0_to_b1(
        encode_b0(
            make_payload(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1,), action, bases=UNTABLED_BASES)
        )
    )
    for action in ("UP", "DOWN", "STOP")
)
UNTABLED_FRAMES = (UNTABLED_UP_B1, UNTABLED_DOWN_B1, UNTABLED_STOP_B1)

# A DIFFERENT remote that is also untabled. Needed to test the identity guard in
# isolation: a tabled foreign frame is rejected earlier by the action-inference
# mismatch, so it never reaches the guard at all.
FOREIGN_UNTABLED_PREFIX = 0xC0FFEF
_FOREIGN_UNTABLED_UP_PAYLOAD = make_payload(
    FOREIGN_UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1,), "UP", bases=UNTABLED_BASES
)
FOREIGN_UNTABLED_UP_CMD = _FOREIGN_UNTABLED_UP_PAYLOAD & 0xFFFF
FOREIGN_UNTABLED_UP_B1 = b0_to_b1(encode_b0(_FOREIGN_UNTABLED_UP_PAYLOAD))
REFERENCE_TRAILER_B1 = b0_to_b1(
    encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "TRAILER", bases=TEST_BASES))
)
# Real field-captured bucket timings and the 65-pair single-0-read truncated
# trailer, re-keyed to the synthetic identity (the same fixture family as
# tests/test_codec.py REKEYED_FIELD_B1_CAPTURES). Decodes to the ALL-channel
# UP command 0xF42B. The Learn wizard decoded captures strictly until #27 and
# dropped this whole class of remote before any handler saw it.
TRUNCATED_TRAILER_UP_B1 = (
    "AAB10413EC026C012C143C381A192A192929292A1A192A1A19292A192A1A192929292A1A192A"
    "192929292A192A1A1929292929292A1A1A1A1A1A1A1A1A1A1A1A192A192929292A192A192A"
    "1A1955"
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


async def capture_learn_action(
    hass: HomeAssistant,
    fake: FakeMqtt,
    flow_id: str,
    frame: str,
    *,
    attempt: int,
) -> ConfigFlowResult:
    """Deliver one action frame to the current attempt and advance the wizard.

    ``attempt`` is 1-based: every attempt publishes exactly one sniff start and
    one sniff stop, and opens its own RX subscription.
    """
    rx = fake.rx_subscriptions()[attempt - 1]
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": frame, "t": attempt}),
    )
    await fake.wait_for_publications(attempt * 2)
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(flow_id)


async def learn_all_three_actions(
    hass: HomeAssistant,
    fake: FakeMqtt,
    flow_id: str,
    frames: tuple[str, str, str] = (REFERENCE_UP_B1, REFERENCE_DOWN_B1, REFERENCE_STOP_B1),
) -> ConfigFlowResult:
    """Walk one armed Learn flow through its UP, DOWN, and STOP captures."""
    result: ConfigFlowResult | None = None
    for index, frame in enumerate(frames, start=1):
        if index > 1:
            result = await hass.config_entries.flow.async_configure(
                flow_id,
                {"next_step_id": "learn_sniff"},
            )
            assert result["step_id"] == "learn_sniff"
        await fake.wait_for_publications(index * 2 - 1)
        result = await capture_learn_action(hass, fake, flow_id, frame, attempt=index)
    assert result is not None
    return result


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


async def start_reconfigure_flow(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> ConfigFlowResult:
    """Start the reconfigure menu for a stored remote entry."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )


def stored_cover_rows(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Return the mutable-shape cover rows stored on one test entry."""
    rows = entry.data[CONF_COVERS]
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
    return cast("list[dict[str, Any]]", rows)


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


def test_validate_cover_input_blank_travel_requests_measurement() -> None:
    """Blank travel on a born leaf is the measurement-request sentinel.

    Callers route it to the capture flow, and only a caller that cannot
    measure (no calibrated physical identity) renders it as travel_required.
    """
    cover, errors = config_flow_module._validate_cover_input(
        {CONF_NAME: "Sink", CONF_CHANNELS: "5"},
        [],
    )
    assert cover is None
    assert errors == {"base": config_flow_module.MEASURE_REQUESTED}


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
    """Remote and cover-management copy stays exact in both English JSON files."""
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
    assert "config_subentries" not in strings
    assert strings["config"]["step"]["reconfigure"]["menu_options"] == {
        "reconfigure_learn": "Relearn from remote",
        "reconfigure_edit": "Edit remote settings",
        "cover_add": "Add cover",
        "cover_pick_edit": "Edit cover",
        "cover_pick_remove": "Remove cover",
    }
    assert strings["config"]["step"]["cover"]["data"][CONF_NAME] == "Cover name"
    assert strings["config"]["step"]["cover_add"]["data"][CONF_NAME] == "Cover name"
    assert "this blind" not in strings["config"]["step"]["learn_setup"]["description"]


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
async def test_legacy_entry_cannot_reconfigure(
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
async def test_wizard_creates_entry_with_data_covers_and_no_subentries(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard writes fresh entry-data cover IDs without config subentries."""
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

    result = await capture_learn_action(hass, fake, flow_id, REFERENCE_UP_B1, attempt=1)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "learn_next"
    # Deliberately no `learn_derive`: after a SUCCESSFUL capture, extrapolating
    # the remaining bases from the one just measured is the #26 defect, not a
    # convenience. It is offered only from `learn_timeout`, once a capture
    # attempt has actually failed.
    assert result["menu_options"] == ["learn_sniff", "learn_recapture"]
    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }
    assert rx.unsubscribe_count == 1
    assert fake.published[1] == (
        "rf433/bridge-a/cmd",
        {"action": "sniff", "seconds": 0},
    )

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn_sniff"},
    )
    assert result["step_id"] == "learn_sniff"
    await fake.wait_for_publications(3)
    result = await capture_learn_action(hass, fake, flow_id, REFERENCE_DOWN_B1, attempt=2)
    assert result["step_id"] == "learn_next"
    assert result["description_placeholders"] == {
        "captured": "DOWN",
        "measured": "UP, DOWN",
        "action": "STOP",
    }

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn_sniff"},
    )
    await fake.wait_for_publications(5)
    result = await capture_learn_action(hass, fake, flow_id, REFERENCE_STOP_B1, attempt=3)
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
        # All three bases came from a real press, so nothing was extrapolated
        # from the codec's action opcode table (#26).
        "measured": "UP, DOWN, STOP",
        "derived": "none",
        "name": "Living room shade",
        "bridge": "bridge-a",
    }
    assert REFERENCE_UP_B1 not in placeholders.values()

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
    # Blank travel on this calibrated wizard now routes to measurement
    # rather than erroring in place; that path has its own tests.
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
    cover_rows = result["data"][CONF_COVERS]
    assert isinstance(cover_rows, list)
    assert [
        {key: value for key, value in row.items() if key != CONF_COVER_ID} for row in cover_rows
    ] == [
        {
            CONF_NAME: "Slider",
            CONF_CHANNELS: [1, 2],
            CONF_TRAVEL_UP: 12.0,
            CONF_TRAVEL_DOWN: 12.0,
        },
        {
            CONF_NAME: "Kitchen shades",
            CONF_CHANNELS: [1, 2, 3],
            CONF_TRAVEL_UP: "",
            CONF_TRAVEL_DOWN: "",
        },
    ]
    cover_ids = [row[CONF_COVER_ID] for row in cover_rows]
    assert len(set(cover_ids)) == 2
    assert all(isinstance(cover_id, str) and len(cover_id) == 26 for cover_id in cover_ids)
    assert result["data"] == {**expected_remote.as_dict(), CONF_COVERS: cover_rows}
    entry = result["result"]
    assert entry.unique_id == "a1b2c3:42"
    assert not entry.subentries
    slider = CoverConfig.from_stored(cover_ids[0], cover_rows[0])
    assert slider.channels == (1, 2)
    assert slider.travel_up == 12.0
    aggregate = CoverConfig.from_stored(cover_ids[1], cover_rows[1])
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
    result = await learn_all_three_actions(hass, fake, flow_id)
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
        json.dumps({"frame": REFERENCE_UP_B1, "t": 5}),
    )
    await fake.wait_for_publications(4)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_next"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["captured"] == "UP"
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
    assert second["step_id"] == "learn_next"
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
async def test_reconfigure_menu_adds_a_cover(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconfigure menu validates and appends a data-backed cover."""
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
    original_id = stored_cover_rows(entry)[0][CONF_COVER_ID]
    result = await start_reconfigure_flow(hass, entry)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "reconfigure_learn",
        "reconfigure_edit",
        "cover_add",
        "cover_pick_edit",
        "cover_pick_remove",
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_add"},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Overlap",
            CONF_CHANNELS: "3,4",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNELS: "overlapping_channels"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Sink",
            CONF_CHANNELS: "5",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cover_added"
    rows = stored_cover_rows(entry)
    assert [row[CONF_NAME] for row in rows] == ["Slider", "Sink"]
    added_id = rows[1][CONF_COVER_ID]
    assert isinstance(added_id, str)
    assert len(added_id) == 26
    assert added_id != original_id
    assert not entry.subentries


@pytest.mark.asyncio
async def test_cover_add_rejects_duplicate_channels(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new data row cannot duplicate an existing cover's channel set."""
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
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_add"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Duplicate",
            CONF_CHANNELS: "3,2,1",
            CONF_TRAVEL_UP: 9,
            CONF_TRAVEL_DOWN: 9,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNELS: "duplicate_channels"}


@pytest.mark.asyncio
async def test_cover_add_fails_closed_for_unparseable_sibling_channels(
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
    row = stored_cover_rows(entry)[0]
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_COVERS: [
                {
                    **row,
                    CONF_CHANNELS: "not-a-channel",
                }
            ],
        },
    )
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_add"},
    )
    result = await hass.config_entries.flow.async_configure(
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
async def test_cover_add_validates_channels_from_malformed_travel_sibling(
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
    row = stored_cover_rows(entry)[0]
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_COVERS: [
                {
                    **row,
                    CONF_TRAVEL_UP: "garbage",
                    CONF_TRAVEL_DOWN: "garbage",
                }
            ],
        },
    )
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_add"},
    )
    result = await hass.config_entries.flow.async_configure(
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
async def test_cover_edit_prefills_display_values(
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
    slider = stored_cover_rows(entry)[0]
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_pick_edit"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COVER_ID: slider[CONF_COVER_ID]},
    )
    assert result["step_id"] == "cover_edit_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_edit"},
    )
    schema = result["data_schema"]
    assert schema is not None
    suggested = schema_suggested_values(schema)
    assert suggested[CONF_CHANNELS] == "1,2,3"
    assert suggested[CONF_TRAVEL_UP] == 12.0
    assert suggested[CONF_TRAVEL_DOWN] == 13.0

    suggested[CONF_NAME] = "Renamed slider"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        schema(suggested),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cover_updated"
    updated = stored_cover_rows(entry)[0]
    restored = CoverConfig.from_stored(str(updated[CONF_COVER_ID]), updated)
    assert updated[CONF_NAME] == "Renamed slider"
    assert restored.channels == (1, 2, 3)
    assert restored.travel_up == 12.0
    assert restored.travel_down == 13.0


@pytest.mark.asyncio
async def test_cover_edit_merges_and_preserves_unknown_keys_and_cover_id(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover edits merge validated fields without replacing identity or hidden data."""
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
        ],
    )
    stored = stored_cover_rows(entry)[0]
    cover_id = stored[CONF_COVER_ID]
    unknown = {"calibration_epoch": 4, "vendor": {"offset": 7}}
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_COVERS: [{**stored, **unknown}],
        },
    )
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_pick_edit"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COVER_ID: cover_id},
    )
    assert result["step_id"] == "cover_edit_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_edit"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Renamed slider",
            CONF_CHANNELS: "1,2,3",
            CONF_TRAVEL_UP: 12,
            CONF_TRAVEL_DOWN: 12,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cover_updated"
    updated = stored_cover_rows(entry)[0]
    assert updated[CONF_COVER_ID] == cover_id
    assert updated[CONF_NAME] == "Renamed slider"
    assert updated["calibration_epoch"] == 4
    assert updated["vendor"] == {"offset": 7}
    assert updated[CONF_TRAVEL_UP] == 12.0
    assert updated[CONF_TRAVEL_DOWN] == 12.0


@pytest.mark.asyncio
async def test_cover_edit_to_leaf_with_blank_travel_routes_to_measurement(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconfigured leaf with blank travel is asked which bridge to measure on."""
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
    aggregate = next(row for row in stored_cover_rows(entry) if row[CONF_CHANNELS] == [1, 2, 3, 4])
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_pick_edit"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COVER_ID: aggregate[CONF_COVER_ID]},
    )
    assert result["step_id"] == "cover_edit_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_edit"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Solo", CONF_CHANNELS: "6"},
    )
    # Becoming a leaf with blank travel is now a measurement request. This
    # test environment has no reachable bridge (flow-local MQTT discovery
    # fails), so the flow lands back on the edit form explaining that typed
    # times are the only option -- not on the old travel_required error.
    assert result["step_id"] == "cover_edit"
    assert result["errors"] == {"base": "measure_no_bridge"}


@pytest.mark.parametrize("registry_state", ("enabled", "disabled", "missing"))
@pytest.mark.asyncio
async def test_cover_remove_deletes_the_registry_row(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    registry_state: str,
) -> None:
    """Removing a cover explicitly deletes any enabled or disabled registry row."""
    from homeassistant.helpers import entity_registry as er

    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1",
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
    removed = stored_cover_rows(entry)[0]
    cover_id = str(removed[CONF_COVER_ID])
    registry = er.async_get(hass)
    if registry_state != "missing":
        registry.async_get_or_create(
            "cover",
            DOMAIN,
            cover_id,
            config_entry=entry,
            disabled_by=(er.RegistryEntryDisabler.USER if registry_state == "disabled" else None),
        )

    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_pick_remove"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COVER_ID: cover_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cover_remove_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cover_removed"
    assert [row[CONF_NAME] for row in stored_cover_rows(entry)] == ["Sink"]
    assert registry.async_get_entity_id("cover", DOMAIN, cover_id) is None


@pytest.mark.parametrize(
    ("covers", "target_index", "reason"),
    [
        (
            [
                {
                    CONF_NAME: "Only",
                    CONF_CHANNELS: "1",
                    CONF_TRAVEL_UP: 12,
                    CONF_TRAVEL_DOWN: 12,
                }
            ],
            0,
            "last_cover",
        ),
        (
            [
                {
                    CONF_NAME: "Leaf",
                    CONF_CHANNELS: "1",
                    CONF_TRAVEL_UP: 12,
                    CONF_TRAVEL_DOWN: 12,
                },
                {
                    CONF_NAME: "All",
                    CONF_CHANNELS: "1,2",
                },
            ],
            0,
            "aggregate_dependency",
        ),
    ],
)
@pytest.mark.asyncio
async def test_cover_remove_refuses_last_cover_and_aggregate_dependency(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    covers: list[dict[str, Any]],
    target_index: int,
    reason: str,
) -> None:
    """Removal refuses the last cover and leaves required aggregate members intact."""
    entry = await create_remote_entry(hass, monkeypatch, covers)
    before = [dict(row) for row in stored_cover_rows(entry)]
    cover_id = before[target_index][CONF_COVER_ID]

    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cover_pick_remove"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_COVER_ID: cover_id},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert stored_cover_rows(entry) == before


@pytest.mark.asyncio
async def test_pickers_disambiguate_duplicate_names_by_cover_id(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edit and remove picker values and duplicate labels expose stable cover IDs."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Shade",
                CONF_CHANNELS: "1",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 12,
            },
            {
                CONF_NAME: "Shade",
                CONF_CHANNELS: "5",
                CONF_TRAVEL_UP: 9,
                CONF_TRAVEL_DOWN: 9,
            },
        ],
    )
    cover_ids = [str(row[CONF_COVER_ID]) for row in stored_cover_rows(entry)]

    for step_id in ("cover_pick_edit", "cover_pick_remove"):
        result = await start_reconfigure_flow(hass, entry)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"next_step_id": step_id},
        )
        schema = result["data_schema"]
        assert schema is not None
        select = schema.schema[CONF_COVER_ID]
        options = select.config["options"]
        assert [option["value"] for option in options] == cover_ids
        labels = [option["label"] for option in options]
        assert len(set(labels)) == 2
        assert all(cover_id in label for cover_id, label in zip(cover_ids, labels, strict=True))
        hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_remote_settings_reconfigure_round_trips_covers_verbatim(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing remote settings round-trips every cover row and unknown key verbatim."""
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
    cover_rows = [
        {
            **stored_cover_rows(entry)[0],
            "future_calibration": {"curve": [1, 3, 5]},
            "opaque_flag": True,
        },
    ]
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_COVERS: cover_rows},
    )
    result = await start_reconfigure_flow(hass, entry)
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
    assert stored_cover_rows(entry) == cover_rows
    assert not entry.subentries


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
    original_covers = [dict(row) for row in stored_cover_rows(entry)]
    # A relearn is the one flow that changes an EXISTING entry's remote
    # identity, and the remote device is keyed by that identity. The device_id
    # is what automations target, so it must survive -- along with any area the
    # user set on the device page.
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    original_key = RemoteConfig.from_entry(entry.data).key
    # Model the deployed registry: _ensure_remote_device has already keyed this
    # remote's device by its identity, and the user set an area on it.
    device_before = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, original_key)},
        name="Kitchen remote",
    )
    registry.async_update_device(device_before.id, area_id="user_override")
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
    assert result["step_id"] == "learn_next"
    # Only UP was captured; the remaining bases come from the extrapolation
    # fallback, which the confirmation step must name as calculated (#26).
    result = await derive_from_timeout(hass, monkeypatch, flow_id)
    assert result["step_id"] == "learn_confirm"
    assert result["menu_options"] == ["reconfigure_apply", "learn_retry"]
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["measured"] == "UP"
    assert placeholders["derived"] == "DOWN, STOP"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "reconfigure_apply"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # The extrapolated bases must still match what one labeled UP reference
    # has always produced, so the 10 remotes in #26's table keep enrolling.
    assert RemoteConfig.from_entry(entry.data).remote.bases == REF_BASES
    updated = RemoteConfig.from_entry(entry.data)
    assert updated.key == f"{REF_PREFIX:06x}:{REF_REMOTE_ID:02x}"
    assert entry.unique_id == updated.key
    assert updated.name == "Renamed remote"
    assert updated.area_id == "pantry"
    assert entry.title == "Renamed remote"
    assert stored_cover_rows(entry) == original_covers
    assert not entry.subentries
    # Re-identified in place: same registry row, same device_id, same area.
    device_after = registry.async_get_device(identifiers={(DOMAIN, updated.key)})
    assert device_after is not None
    assert device_after.id == device_before.id
    assert device_after.area_id == "user_override"
    # The retired identity no longer resolves, so nothing is left to prune.
    assert registry.async_get_device(identifiers={(DOMAIN, original_key)}) is None


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
    assert result["step_id"] == "learn_next"
    result = await derive_from_timeout(hass, monkeypatch, flow_id)
    assert result["step_id"] == "learn_confirm"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "reconfigure_apply"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(entry.data) == original_data
    assert entry.unique_id == "a1b2c3:42"


def test_untabled_remote_fixture_is_outside_the_action_opcode_table() -> None:
    """Pin the premise of #26: only DOWN of this remote is recognisable by opcode."""
    from custom_components.zemismart_blinds.codec import infer_action_button

    inferred = {
        action: infer_action_button(
            (1,),
            make_payload(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1,), action, bases=UNTABLED_BASES)
            & 0xFFFF,
        )
        for action in ("UP", "DOWN", "STOP")
    }
    assert inferred == {"UP": None, "DOWN": "DOWN", "STOP": None}
    # And the derivation the wizard used to perform from that single DOWN
    # capture reconstructs the WRONG UP and STOP -- the live-proven defect.
    from custom_components.zemismart_blinds.codec import derive_bases_from_base

    invented = derive_bases_from_base("DOWN", UNTABLED_BASES.down, UNTABLED_REMOTE_ID)
    assert invented.down == UNTABLED_BASES.down
    assert invented.up != UNTABLED_BASES.up
    assert invented.stop != UNTABLED_BASES.stop


@pytest.mark.asyncio
async def test_learn_enrols_a_remote_outside_the_action_opcode_table(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every action is measured, so an unrecognised opcode still enrols (#26).

    Before this, the wizard's capture handler dropped any frame whose opcode
    byte was missing from _ACTION_COMMAND_HIGH -- silently, with no log and no
    error -- so UP and STOP presses on this remote produced a capture timeout.
    Having accepted only DOWN it then invented UP and STOP from the same table,
    which for this shape of remote yields commands the motor refuses.
    """
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    # An unrecognised opcode is only resolved when the window closes without a
    # recognised frame for the prompted action, so shorten the window rather
    # than waiting the real 30 s twice.
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 0.2, raising=False)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Study shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    result = await learn_all_three_actions(hass, fake, flow_id, UNTABLED_FRAMES)
    assert result["step_id"] == "learn_confirm"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["prefix"] == f"0x{UNTABLED_PREFIX:06x}"
    assert placeholders["measured"] == "UP, DOWN, STOP"
    assert placeholders["derived"] == "none"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "remote_settings"},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Study remote",
            CONF_AREA_ID: "living_room",
            ADVANCED_SECTION: {CONF_REPEATS: 3, CONF_COALESCE_WINDOW_MS: 0},
        },
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Study blind",
            CONF_CHANNELS: "1",
            CONF_TRAVEL_UP: 10,
            CONF_TRAVEL_DOWN: 10,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "finish"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Every base is the one the remote actually transmits, not a table lookup.
    assert RemoteConfig.from_entry(result["data"]).remote.bases == UNTABLED_BASES


@pytest.mark.asyncio
async def test_learn_recapture_rearms_the_action_just_measured(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recapture re-arms UP, not the DOWN the wizard has already moved on to.

    learn_next advances the prompt before it is shown, so reusing learn_retry
    here would discard nothing and silently arm the next button instead.
    """
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Hall shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    result = await capture_learn_action(hass, fake, flow_id, REFERENCE_UP_B1, attempt=1)
    assert result["step_id"] == "learn_next"
    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn_recapture"},
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    rearmed = result["description_placeholders"]
    assert rearmed is not None
    assert rearmed["action"] == "UP"
    await fake.wait_for_publications(3)
    # A DOWN press is refused while UP is armed, so the discarded UP really is
    # being asked for again rather than the prompt having moved on.
    rx = fake.rx_subscriptions()[1]
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_DOWN_B1, "t": 9}),
    )
    await asyncio.sleep(0)
    assert current_flow(hass, flow_id)["step_id"] == "learn_sniff"

    result = await capture_learn_action(hass, fake, flow_id, REFERENCE_UP_B1, attempt=2)
    assert result["step_id"] == "learn_next"
    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }
    hass.config_entries.flow.async_abort(flow_id)


@pytest.mark.asyncio
async def test_learn_accepts_a_truncated_trailer_capture(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote that truncates its OEM trailer on air still enrols through Learn.

    The codec and state-sync layers already tolerated this structure; the Learn
    wizard used the strict decoder and rejected it before any handler ran, so
    the user saw a capture timeout on a remote transmitting perfectly (#27).
    """
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Living room shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE,
        },
    )
    assert result["step_id"] == "learn_sniff"
    await fake.wait_for_publications(1)
    rx = fake.rx_subscriptions()[0]

    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": TRUNCATED_TRAILER_UP_B1, "t": 1}),
    )
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "learn_next"
    result = await derive_from_timeout(hass, monkeypatch, flow_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "learn_confirm"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["prefix"] == "0xa1b2c3"
    assert placeholders["remote_id"] == "0x42"
    assert placeholders["channels"] == "1,2,3,4,5,6"


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
    registry.update_info("bridge-a", {"area": "living_room", "boot": 7})
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


def _deliver_sniff_frame(
    hass: HomeAssistant,
    flow: Any,
    session_id: str,
    attempt: Any,
    frame: str,
) -> None:
    """Push one RX payload straight into the wizard's capture handler."""
    from types import SimpleNamespace

    config_flow_module._handle_sniff_message(
        flow,
        session_id,
        "rf433/bridge-a/rx",
        attempt,
        cast(
            "ReceiveMessage",
            SimpleNamespace(
                topic="rf433/bridge-a/rx",
                payload=json.dumps({"frame": frame}),
                retain=False,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_sniff_handler_holds_an_unrecognised_opcode_but_prefers_the_action(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised opcode reaches the handler and is held, not dropped (#26).

    It must not resolve the attempt outright, though: the OEM TRAILER burst
    that follows UP and DOWN also decodes structurally, so a frame that really
    does carry the prompted action's opcode still wins.
    """
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: False)
    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    session_id = "session-untabled"
    flow._sniff_session_id = session_id

    attempt = config_flow_module._SniffAttempt(
        action="UP",
        measured={},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, attempt, UNTABLED_UP_B1)
    assert not attempt.future.done()
    assert attempt.unrecognized is not None
    assert attempt.unrecognized.button == "UP"
    assert attempt.unrecognized.inferred_button is None
    assert attempt.unrecognized.base == UNTABLED_BASES.up

    # A recognised UP for the SAME prompt still ends the window immediately.
    recognised = config_flow_module._SniffAttempt(
        action="UP",
        measured={},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, recognised, REFERENCE_TRAILER_B1)
    assert recognised.unrecognized is not None
    _deliver_sniff_frame(hass, flow, session_id, recognised, REFERENCE_UP_B1)
    assert recognised.future.done()
    assert recognised.future.result().inferred_button == "UP"
    attempt.future.cancel()


@pytest.mark.asyncio
async def test_sniff_handler_rejects_a_foreign_remote_and_a_repeated_base(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later actions are pinned to the identity and bases already measured.

    With inference demoted to a hint, these are what keep a stray neighbouring
    frame — or the previous button's lingering repeats — out of the next
    action's slot.
    """
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: False)
    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    session_id = "session-guards"
    flow._sniff_session_id = session_id

    measured_up = config_flow_module._SniffAttempt(
        action="UP",
        measured={},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, measured_up, REFERENCE_UP_B1)
    assert measured_up.future.done()
    already = {"UP": measured_up.future.result()}

    # A different remote's frame cannot fill this remote's slot. It has to be an
    # UNTABLED foreign frame to test the identity guard at all: a tabled one is
    # dropped earlier by the action-inference mismatch, so the guard could be
    # deleted outright with this test still passing.
    foreign = config_flow_module._SniffAttempt(
        action="DOWN",
        measured=already,
        future=hass.loop.create_future(),
    )
    from custom_components.zemismart_blinds.codec import infer_action_button as _infer

    assert _infer((1,), FOREIGN_UNTABLED_UP_CMD) is None, (
        "the foreign fixture must be untabled or this test proves nothing"
    )
    _deliver_sniff_frame(hass, flow, session_id, foreign, FOREIGN_UNTABLED_UP_B1)
    assert not foreign.future.done()
    assert foreign.unrecognized is None

    # Nor can a lingering repeat of the UP burst we already measured.
    repeat = config_flow_module._SniffAttempt(
        action="DOWN",
        measured=already,
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, repeat, REFERENCE_UP_B1)
    assert not repeat.future.done()
    assert repeat.unrecognized is None

    # The genuine DOWN press is still accepted.
    _deliver_sniff_frame(hass, flow, session_id, repeat, REFERENCE_DOWN_B1)
    assert repeat.future.done()
    foreign.future.cancel()


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

    def deliver(frame: str = TEST_CH12_UP_B0) -> config_flow_module._SniffAttempt:
        attempt = config_flow_module._SniffAttempt(
            action="UP",
            measured={},
            future=hass.loop.create_future(),
        )
        config_flow_module._handle_sniff_message(
            flow,
            session_id,
            topic,
            attempt,
            cast(
                "ReceiveMessage",
                SimpleNamespace(
                    topic=topic,
                    payload=json.dumps({"frame": frame}),
                    retain=False,
                ),
            ),
        )
        return attempt

    # Classified as our own echo -> the capture is dropped.
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: True)
    echo = deliver()
    assert not echo.future.done(), "our own transmission must not be learned"
    assert echo.unrecognized is None

    # Classified as foreign -> it is a real remote press and gets captured.
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: False)
    press = deliver()
    assert press.future.done()
    assert press.future.result().button == "UP"
    echo.future.cancel()


@pytest.mark.asyncio
async def test_untabled_lingering_repeat_never_becomes_the_next_action(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An UP repeat heard during the DOWN window is not stored as DOWN.

    Only reachable on an UNTABLED remote, which is why the tabled version of
    this test passed while the guard was broken. For a tabled remote the frame
    is dropped further down by `inferred is not None`; for an untabled one
    `inferred` is None, so `_capture_belongs_to_this_action` is the only thing
    standing between a lingering repeat and the measured DOWN base.

    A review round claimed the guard was broken here, on the theory that
    `derive_base` folds the solicited action into the base and so never matches
    across the UP -> DOWN boundary. It does not: the function validates its
    button argument but recovers the base as
    `(cmd - remote_id + group_offset(chans))`, which is action-independent.
    Confirmed by this test passing with the guard's comparison switched between
    base and raw command. It is kept as characterization, not regression: the
    guard genuinely had no untabled coverage before, which is how a false
    finding survived long enough to be acted on.
    """
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: False)
    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    session_id = "session-untabled-repeat"
    flow._sniff_session_id = session_id

    up_attempt = config_flow_module._SniffAttempt(
        action="UP",
        measured={},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, up_attempt, UNTABLED_UP_B1)
    # Untabled: inference cannot resolve it, so it is held rather than accepted.
    measured_up = (
        up_attempt.future.result() if up_attempt.future.done() else up_attempt.unrecognized
    )
    assert measured_up is not None
    assert measured_up.inferred_button is None, (
        "fixture must be untabled for this test to mean anything"
    )

    repeat = config_flow_module._SniffAttempt(
        action="DOWN",
        measured={"UP": measured_up},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, repeat, UNTABLED_UP_B1)
    assert not repeat.future.done()
    assert repeat.unrecognized is None, "the UP repeat must not become the DOWN fallback"

    # The genuine DOWN press still lands.
    genuine = config_flow_module._SniffAttempt(
        action="DOWN",
        measured={"UP": measured_up},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, genuine, UNTABLED_DOWN_B1)
    landed = genuine.future.result() if genuine.future.done() else genuine.unrecognized
    assert landed is not None
    assert landed.command != measured_up.command


async def derive_from_timeout(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    flow_id: str,
) -> ConfigFlowResult:
    """Reach `learn_derive` the only way production offers it: a failed capture.

    Derivation is deliberately NOT on the `learn_next` success menu (#26) --
    extrapolating from a button we just measured is what stored an invented base
    that the motor ignored. It is reachable only after a capture attempt has
    demonstrably failed, so a test that wants it has to fail one.
    """
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 0.001, raising=False)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "learn_sniff"}
    )
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_timeout", result["step_id"]
    return await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "learn_derive"})


def _capture_for(
    action: str, prefix: int, remote_id: int, bases: CommandBases
) -> config_flow_module._LearnCapture:
    """Build one measured capture the way _handle_sniff_message would."""
    from custom_components.zemismart_blinds.codec import infer_action_button

    payload = make_payload(prefix, remote_id, (1,), action, bases=bases)
    command = payload & 0xFFFF
    return config_flow_module._LearnCapture(
        frame=b0_to_b1(encode_b0(payload)),
        prefix=prefix,
        remote_id=remote_id,
        channels=(1,),
        command=command,
        button=action,
        inferred_button=infer_action_button((1,), command),
        base=derive_base((1,), action, command, remote_id),
    )


def test_derivation_falls_back_to_a_later_usable_capture() -> None:
    """Derivation tries every measured capture, not just the first.

    `derive_bases` only works from a reference whose own opcode is inside
    `_ACTION_COMMAND_HIGH`, and captures are kept in the order the wizard asked
    for buttons. A user whose UP is untabled but whose DOWN is not was therefore
    refused the fallback they had explicitly selected -- the flow bounced back to
    the timeout menu -- even though the measured DOWN could derive the missing
    STOP perfectly well.
    """
    untabled_up = _capture_for("UP", UNTABLED_PREFIX, UNTABLED_REMOTE_ID, UNTABLED_BASES)
    tabled_down = _capture_for("DOWN", TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    assert untabled_up.inferred_button is None, "the UP fixture must be untabled"
    assert tabled_down.inferred_button == "DOWN", "the DOWN fixture must be tabled"

    # UP first, exactly as the wizard collects them; STOP never captured.
    identity, derived = config_flow_module._remote_identity_from_captures(
        {"UP": untabled_up, "DOWN": tabled_down}
    )

    assert derived == ("STOP",), "only the uncaptured button may be derived"
    assert identity.bases is not None
    # Measured values are kept verbatim; only STOP comes from the fallback.
    assert identity.bases.up == untabled_up.base
    assert identity.bases.down == tabled_down.base


async def start_learned_flow_at_cover_step(
    hass: HomeAssistant,
    fake: FakeMqtt,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Walk one Learn wizard to the cover form and return its flow id.

    Extracted from the Learn happy-path tests: arm on bridge-a, capture all
    three actions, accept the calibration, and submit remote settings.
    """
    prepare_config_flow(hass, monkeypatch)
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Sunroom remote",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await learn_all_three_actions(hass, fake, flow_id)
    assert result["step_id"] == "learn_confirm"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "remote_settings"},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Sunroom remote",
            CONF_AREA_ID: "living_room",
            ADVANCED_SECTION: {
                CONF_REPEATS: 5,
                CONF_COALESCE_WINDOW_MS: 150,
            },
        },
    )
    assert result["step_id"] == "cover"
    return flow_id


def _travel_rx_frame(button: str) -> str:
    """Synthesize one bridge capture of the calibrated test remote."""
    return b0_to_b1(
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), button, bases=TEST_BASES))
    )


async def measure_one_direction(
    hass: HomeAssistant,
    fake: FakeMqtt,
    flow_id: str,
    direction: str,
    *,
    start_millis: int,
    stop_millis: int,
) -> ConfigFlowResult:
    """Drive one run through both progress phases: press, then STOP.

    Asserts the phase transition the field test asked for: hearing the
    direction press advances the spinner to a screen that names the heard
    direction and waits for the STOP.
    """
    rx = fake.rx_subscriptions()[-1]
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": _travel_rx_frame(direction), "t": start_millis, "boot": 7}),
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "cover_measure_stop"
    assert result["progress_action"] == "measuring_stop"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["direction"] == direction
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": _travel_rx_frame("STOP"), "t": stop_millis, "boot": 7}),
    )
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(flow_id)


@pytest.mark.asyncio
async def test_blank_travel_measures_both_directions(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cover submitted with no travel times is measured from the remote."""
    fake = FakeMqtt()
    flow_id = await start_learned_flow_at_cover_step(hass, fake, monkeypatch)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Sunroom shade", CONF_CHANNELS: "1,2"},
    )
    assert result["step_id"] == "cover_measure_setup"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "measuring"
    await fake.wait_for_publications(7)

    result = await measure_one_direction(
        hass,
        fake,
        flow_id,
        "DOWN",
        start_millis=1_000,
        stop_millis=15_310,
    )
    assert result["step_id"] == "cover_measure_next"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["measured"] == "DOWN"
    assert placeholders["stored"] == "15"
    assert placeholders["wanted"] == "UP"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "cover_measure_run"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(9)
    result = await measure_one_direction(
        hass,
        fake,
        flow_id,
        "UP",
        start_millis=20_000,
        stop_millis=36_080,
    )
    assert result["step_id"] == "cover_measure_confirm"
    schema = result["data_schema"]
    assert schema is not None
    defaults = schema({})
    assert defaults[CONF_TRAVEL_DOWN] == 15
    assert defaults[CONF_TRAVEL_UP] == 17

    result = await hass.config_entries.flow.async_configure(flow_id, dict(defaults))
    assert result["step_id"] == "cover_menu"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "finish"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    row = stored_cover_rows(result["result"])[0]
    restored = CoverConfig.from_stored(str(row[CONF_COVER_ID]), row)
    assert restored.travel_down == 15.0
    assert restored.travel_up == 17.0


@pytest.mark.asyncio
async def test_no_press_reaches_the_timeout_menu(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hearing nothing at all is reported as its own failure, not a bad run."""
    monkeypatch.setattr(config_flow_module, "TRAVEL_ARM_TIMEOUT_SECONDS", 0.001)
    fake = FakeMqtt()
    flow_id = await start_learned_flow_at_cover_step(hass, fake, monkeypatch)
    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Sunroom shade", CONF_CHANNELS: "1,2"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE}
    )
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "cover_measure_timeout"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["reason"] == "no_press"


@pytest.mark.asyncio
async def test_remeasure_menu_updates_a_stored_cover(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edit menu's re-measure path writes new travel into the stored row."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2",
                CONF_TRAVEL_UP: 10,
                CONF_TRAVEL_DOWN: 10,
            }
        ],
    )
    slider = stored_cover_rows(entry)[0]
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_reconfigure_flow(hass, entry)
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "cover_pick_edit"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_COVER_ID: slider[CONF_COVER_ID]}
    )
    assert result["step_id"] == "cover_edit_menu"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "cover_measure_start"}
    )
    assert result["step_id"] == "cover_measure_setup"
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BRIDGE: "bridge-a"})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    result = await measure_one_direction(
        hass, fake, flow_id, "DOWN", start_millis=1_000, stop_millis=13_500
    )
    assert result["step_id"] == "cover_measure_next"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "cover_measure_run"}
    )
    await fake.wait_for_publications(3)
    result = await measure_one_direction(
        hass, fake, flow_id, "UP", start_millis=20_000, stop_millis=34_100
    )
    assert result["step_id"] == "cover_measure_confirm"
    schema = result["data_schema"]
    assert schema is not None
    result = await hass.config_entries.flow.async_configure(flow_id, dict(schema({})))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cover_updated"
    updated = stored_cover_rows(entry)[0]
    assert updated[CONF_COVER_ID] == slider[CONF_COVER_ID]
    restored = CoverConfig.from_stored(str(updated[CONF_COVER_ID]), updated)
    assert restored.travel_down == 13.0
    assert restored.travel_up == 15.0


@pytest.mark.asyncio
async def test_an_aggregate_cover_is_never_measured(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cover that aggregates its siblings carries no travel by design."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Left",
                CONF_CHANNELS: "1",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 13,
            }
        ],
    )
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cover_add"}
    )
    # Channels 1,2 strictly contain the stored leaf on channel 1, so this row
    # is born_aggregate: blank travel is correct there, not a measurement
    # request.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Both", CONF_CHANNELS: "1,2"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cover_added"
    added = next(row for row in stored_cover_rows(entry) if row[CONF_CHANNELS] == [1, 2])
    assert CoverConfig.from_stored(str(added[CONF_COVER_ID]), added).travel_up is None


@pytest.mark.asyncio
async def test_clearing_travel_on_edit_routes_to_measurement(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edit form's backfill must not resurrect the stored value.

    The form arrives pre-filled, so an empty field there is a deliberate
    clear. Restoring it made re-measurement unreachable from the one screen
    where a user with a wrong travel time actually goes.
    """
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [
            {
                CONF_NAME: "Slider",
                CONF_CHANNELS: "1,2",
                CONF_TRAVEL_UP: 12,
                CONF_TRAVEL_DOWN: 13,
            }
        ],
    )
    slider = stored_cover_rows(entry)[0]
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cover_pick_edit"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COVER_ID: slider[CONF_COVER_ID]}
    )
    assert result["step_id"] == "cover_edit_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cover_edit"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Slider", CONF_CHANNELS: "1,2"}
    )
    assert result["step_id"] == "cover_measure_setup"


@pytest.mark.asyncio
async def test_a_virtual_remote_still_refuses_blank_travel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthesized identity cannot be measured, so blank travel stays an error.

    Nothing physical transmits a virtual remote's identity, so every press
    would fail to match and the user would wait out the arming deadline to
    learn that. The wizard knows in memory that it allocated this identity.
    """
    prepare_config_flow(hass, monkeypatch)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "advanced"})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "virtual"})
    await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Virtual remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {CONF_REPEATS: 5, CONF_COALESCE_WINDOW_MS: 150},
        },
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Shade", CONF_CHANNELS: "1"}
    )
    assert result["step_id"] == "cover"
    assert result["errors"] == {"base": "travel_required"}
