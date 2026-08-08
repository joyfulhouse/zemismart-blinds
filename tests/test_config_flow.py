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
    DEFAULT_SNIFF_WINDOW_SECONDS,
    DOMAIN,
    MAX_SNIFF_WINDOW_SECONDS,
    MQTT_AVAILABILITY_TOPIC,
    MQTT_INFO_TOPIC,
    MQTT_ROOT,
    TRAVEL_BURST_WINDOW_SECONDS,
)
from custom_components.zemismart_blinds.models import (
    BlindConfig,
    CoverConfig,
    RemoteConfig,
    RemoteIdentity,
)
from custom_components.zemismart_blinds.travel_capture import (
    _HEARD_CAP,
    TravelRun,
    press_signature,
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
    from collections.abc import Callable, Coroutine, Iterator

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

# Bound on every "drive the flow until X" helper below. Long enough that a
# loaded machine never trips it, short enough that a flow which never reaches
# X fails its own assertion instead of waiting out the flow's 30s timeouts.
_FLOW_WAIT_TIMEOUT_SECONDS = 10.0

# What one learn bridge is told to sniff for. Longer than the capture window by
# the settle, because the learn path arms each bridge once and the settle must
# fall inside a window they are still sniffing (#57) -- taken from the module so
# the tests cannot drift from the constant that has to satisfy that.
LEARN_SNIFF_START = {
    "action": "sniff",
    "seconds": config_flow_module._LEARN_SNIFF_WINDOW_SECONDS,
}


def b0_to_b1(frame: str) -> str:
    """Convert a stored byte-exact B0 vector into its Portisch B1 capture form."""
    body = frame[6:-2]
    return f"AAB1{body[:2]}{body[4:]}3855"


REFERENCE_UP_B1 = b0_to_b1(TEST_CH12_UP_B0)
REFERENCE_DOWN_B1 = b0_to_b1(TEST_CH12_DOWN_B0)
REFERENCE_STOP_B1 = b0_to_b1(
    encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "STOP", bases=TEST_BASES))
)
# The same second remote's DOWN. Recognised by the opcode table, so while the
# wizard is soliciting UP it neither resolves the attempt nor gets held as the
# fallback -- it only lands in the recently-heard set. That makes it the one
# rival ONLY a winner's look-back can notice (#57).
SECOND_REMOTE_DOWN_B1 = b0_to_b1(
    encode_b0(make_payload(REF_PREFIX, REF_REMOTE_ID, (1, 2), "DOWN", bases=REF_BASES))
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


@pytest.fixture(autouse=True)
def capture_owners_are_released() -> Iterator[None]:
    """Fail the test that leaks a bridge claim, not the one that inherits it.

    ``_CAPTURE_OWNERS`` is module state keyed by ``id(hass)``, and CPython
    reuses the addresses of the short-lived HomeAssistant objects these tests
    build. A leaked claim therefore lands on some LATER test, which now sees
    an outright "the bridges are busy" refusal -- a failure arbitrarily far
    from its cause.

    Asserting rather than quietly clearing, deliberately: every capture path
    releases its claim in a `finally`, so a leak is a production bug in that
    discipline and the suite should say so. The reset still runs either way,
    so one leak cannot cascade through the rest of the session.

    Local to this module rather than `conftest.py`: the capture flows that
    claim bridges are tested here, and a suite-wide autouse fixture would
    charge every unrelated test for an invariant it cannot break.
    """
    config_flow_module._CAPTURE_OWNERS.clear()
    try:
        yield
        assert not config_flow_module._CAPTURE_OWNERS, (
            f"the test left bridge claims behind: {config_flow_module._CAPTURE_OWNERS}"
        )
    finally:
        config_flow_module._CAPTURE_OWNERS.clear()


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
    # The first learn capture settles for a real 1.5s in production to see any
    # competing remote. Tests deliver every frame before awaiting, so a token
    # window is enough here -- the ambiguity tests set their own.
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", 0.001, raising=False)


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


def active_rx(fake: FakeMqtt, bridge: str = "bridge-a") -> Subscription:
    """Return the newest ACTIVE RX subscription for one bridge.

    Fleet-wide listening opens one RX subscription per online bridge, so a
    positional index no longer names a bridge; the topic does.
    """
    matches = [
        subscription
        for subscription in fake.rx_subscriptions()
        if subscription.topic == f"{MQTT_ROOT}/{bridge}/rx" and subscription.active
    ]
    return matches[-1]


def sniff_starts(fake: FakeMqtt, bridge: str) -> int:
    """Count the sniff windows opened on one bridge so far."""
    topic = f"{MQTT_ROOT}/{bridge}/cmd"
    return sum(
        1
        for published_topic, payload in fake.published
        if published_topic == topic and payload.get("seconds") != 0
    )


def sniff_windows(fake: FakeMqtt, bridge: str) -> list[int]:
    """Return the window, in seconds, of each sniff START published to one bridge.

    Read off the published command rather than the module constant: what a bridge
    actually sniffs for is what the payload says, and the whole point of the
    window covering the settle is that it reaches the hardware (#57).
    """
    topic = f"{MQTT_ROOT}/{bridge}/cmd"
    return [
        seconds
        for published_topic, payload in fake.published
        if published_topic == topic
        and isinstance(seconds := payload.get("seconds"), int)
        and seconds
    ]


async def wait_for_sniff_starts(
    fake: FakeMqtt,
    count: int,
    bridge: str = "bridge-a",
) -> None:
    """Wait until ``count`` sniff starts were published to one bridge.

    Counting per bridge keeps callers independent of how many bridges a
    session armed, which varies between Automatic and an explicit pick.

    Bounded deliberately: a regression that never arms a bridge would leave
    ``changed`` set for the last time already, so an unbounded wait would hang
    the suite instead of failing the test that caught the regression.
    """
    async with asyncio.timeout(_FLOW_WAIT_TIMEOUT_SECONDS):
        while sniff_starts(fake, bridge) < count:
            await fake.changed.wait()
            fake.changed.clear()


async def advance_to_step(hass: HomeAssistant, flow_id: str, step_id: str) -> ConfigFlowResult:
    """Drive ONE flow until it reaches ``step_id``.

    A measurement moves between two SHOW_PROGRESS steps, so "left progress"
    is not the signal -- the step is. Waiting on ONE flow is also what a test
    with a second flow deliberately left mid-capture needs: blocking on every
    task would wait out that flow's whole window. Bounded, so a run that never
    closes fails the assertion it was written for instead of waiting out the
    flow's own 30-second timeouts.
    """
    async with asyncio.timeout(_FLOW_WAIT_TIMEOUT_SECONDS):
        while True:
            result = await hass.config_entries.flow.async_configure(flow_id)
            if result.get("step_id") == step_id:
                return result
            await asyncio.sleep(0)


def pin_receive_clock(monkeypatch: pytest.MonkeyPatch, *ticks: float) -> None:
    """Pin the monotonic clock the travel handler stamps received frames with.

    Cross-bridge runs are timed on the receive clock rather than either
    bridge's own, and a test emits its whole run inside a millisecond, so the
    fallback needs a real interval to measure. The last tick repeats forever.
    """
    from types import SimpleNamespace

    remaining = list(ticks)

    def monotonic() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    # `config_flow` reads exactly one thing from `time`: the receive stamp.
    monkeypatch.setattr(config_flow_module, "time", SimpleNamespace(monotonic=monotonic))


async def capture_learn_action(
    hass: HomeAssistant,
    fake: FakeMqtt,
    flow_id: str,
    frame: str,
    *,
    attempt: int,
) -> ConfigFlowResult:
    """Deliver one action frame on bridge-a and advance the wizard.

    ``attempt`` is 1-based: it counts bridge-a's sniff starts to wait for, and
    stamps the frame's ``t`` so repeated captures stay distinguishable.
    """
    await wait_for_sniff_starts(fake, attempt)
    await fake.emit(
        active_rx(fake),
        f"{MQTT_ROOT}/bridge-a/rx",
        json.dumps({"frame": frame, "t": attempt}),
    )
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

    # Fleet listening made "the bridge" plural, but a user who picked ONE bridge
    # as an override still sees these screens -- and they are the users who are
    # diagnosing something, so copy describing a fleet they opted out of is
    # exactly the wrong thing to tell them (#57). The failure screens therefore
    # say what is true in BOTH modes, and that has to be locked or it regresses
    # to "every online bridge" the next time someone edits nearby.
    for step in ("learn_busy", "cover_measure_busy"):
        description = strings["config"]["step"][step]["description"]
        assert "every bridge it listens on" in description
        assert "every online bridge" not in description, (
            f"{step} must not describe a fleet to a user who picked one bridge"
        )
    ambiguous = strings["config"]["step"]["learn_ambiguous"]["description"]
    assert "within range of the listening bridges" in ambiguous
    assert "every online bridge" not in ambiguous
    # The picker and its setup screens DO describe the modes, so they keep saying
    # what Automatic does -- the phrase is only wrong where the mode is unknown.
    assert (
        "every online bridge"
        in strings["config"]["step"]["learn_setup"]["data_description"][CONF_BRIDGE]
    )


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
    await wait_for_sniff_starts(fake, 1)
    await wait_for_sniff_starts(fake, 1, "bridge-b")
    # Automatic is fleet-wide (#57): every online bridge is subscribed and
    # armed, not just the one matching the selected area.
    assert {subscription.topic for subscription in fake.rx_subscriptions()} == {
        "rf433/bridge-a/rx",
        "rf433/bridge-b/rx",
    }
    rx = active_rx(fake)
    assert rx.ready
    assert ("rf433/bridge-a/cmd", LEARN_SNIFF_START) in fake.published
    assert ("rf433/bridge-b/cmd", LEARN_SNIFF_START) in fake.published

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
    assert all(payload.get("seconds") != 0 for _topic, payload in fake.published), (
        "nothing may stop the sniff windows before a capture lands"
    )

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
    # The capture stops and releases EVERY armed bridge, not just the heard one.
    assert ("rf433/bridge-a/cmd", {"action": "sniff", "seconds": 0}) in fake.published
    assert ("rf433/bridge-b/cmd", {"action": "sniff", "seconds": 0}) in fake.published

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn_sniff"},
    )
    assert result["step_id"] == "learn_sniff"
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
        # Both bridges were ARMED, but only bridge-a delivered the presses.
        # Naming the armed set here would tell the user this remote was heard
        # through a bridge that never heard it (#57).
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
    assert fake.published[0] == ("rf433/bridge-b/cmd", LEARN_SNIFF_START)
    assert sniff_starts(fake, "bridge-a") == 0, "an explicit pick never widens to the fleet"
    hass.config_entries.flow.async_abort(flow_id)
    await fake.wait_for_publications(2)


@pytest.mark.asyncio
async def test_learn_on_automatic_captures_off_whichever_bridge_heard_it(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Learn wizard gets the same fleet-wide treatment as measure (#57).

    ``living_room`` is bridge-a's area, so the pre-#57 Automatic resolved to
    bridge-a alone and a remote only bridge-b could hear was unlearnable. The
    press is delivered on bridge-b here for exactly that reason.
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
            CONF_NAME: "Kaelyn shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE,
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await wait_for_sniff_starts(fake, 1, "bridge-b")

    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 9}),
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)

    assert result["step_id"] == "learn_next"
    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }
    # Both armed bridges are stopped, not only the one that heard the press.
    assert ("rf433/bridge-a/cmd", {"action": "sniff", "seconds": 0}) in fake.published
    assert ("rf433/bridge-b/cmd", {"action": "sniff", "seconds": 0}) in fake.published

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


def sniff_channels(count: int) -> list[Any]:
    """Build ``count`` unclaimed sniff channels for the fan-out tests."""
    return [
        config_flow_module._SniffChannel(
            bridge_id=f"bridge-{index}",
            owner_key=(0, f"bridge-{index}"),
            command_topic=f"rf433/bridge-{index}/cmd",
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_the_arm_fanout_is_concurrent_and_loses_nobody(hass: HomeAssistant) -> None:
    """A bigger fleet arms concurrently, and every bridge is armed exactly once.

    Arming happens inside a fixed 5-second bootstrap budget, so it cannot be
    serial per bridge -- a house that grows a bridge would spend another SUBACK
    round trip of the budget. The shared deadline is what bounds this, not a
    concurrency limit: a limit in front of the deadline only decides WHICH
    bridges miss out when the broker is slow.
    """
    channels = sniff_channels(19)
    in_flight = 0
    peak = 0
    armed_order: list[str] = []

    async def arm(channel: Any) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Two suspension points, so a serial implementation cannot look
        # concurrent by accident.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        in_flight -= 1
        armed_order.append(channel.bridge_id)

    armed = await config_flow_module._async_arm_sniff_channels(
        channels,
        hass.loop.time() + 5.0,
        arm,
    )

    assert armed == channels, "every bridge armed, and the caller sees them all"
    assert sorted(armed_order) == sorted(channel.bridge_id for channel in channels)
    assert peak == len(channels), "all of them are in flight together"
    assert await config_flow_module._async_arm_sniff_channels([], hass.loop.time() + 5.0, arm) == []


@pytest.mark.asyncio
async def test_one_bridge_failing_to_arm_strands_no_sibling(hass: HomeAssistant) -> None:
    """One bridge raising must not leave its siblings running behind the caller.

    A fan-out that propagates the first exception leaves every sibling
    coroutine in flight. The caller has already gone to its teardown, which
    walks the channel list and finds nothing to release -- and the sibling then
    finishes, subscribing a topic nobody will unsubscribe and opening a sniff
    window on a bridge whose owner key is already released.

    A failed bridge is skipped rather than fatal: one misbehaving bridge must
    not cost the user the fleet.
    """
    channels = sniff_channels(3)
    finished: list[str] = []

    async def arm(channel: Any) -> None:
        if channel.bridge_id == "bridge-1":
            raise RuntimeError("this bridge is not answering")
        await asyncio.sleep(0)
        finished.append(channel.bridge_id)

    armed = await config_flow_module._async_arm_sniff_channels(
        channels,
        hass.loop.time() + 5.0,
        arm,
    )

    assert [channel.bridge_id for channel in armed] == ["bridge-0", "bridge-2"]
    assert finished == ["bridge-0", "bridge-2"], (
        "every sibling finished BEFORE the fan-out returned, so teardown can see them"
    )


@pytest.mark.asyncio
async def test_a_bridge_that_misses_the_arm_deadline_is_skipped_not_fatal(
    hass: HomeAssistant,
) -> None:
    """A slow bridge costs itself, not the whole listening session.

    The bootstrap budget is shared and absolute, so a bridge whose SUBACK
    never arrives cannot push the advertised capture window out -- and
    dropping the session because of it would hand the user "nothing was
    heard" for a fleet that was ready to listen.
    """
    channels = sniff_channels(2)

    async def arm(channel: Any) -> None:
        if channel.bridge_id == "bridge-1":
            await asyncio.Event().wait()

    armed = await config_flow_module._async_arm_sniff_channels(
        channels,
        hass.loop.time() + 0.05,
        arm,
    )

    assert [channel.bridge_id for channel in armed] == ["bridge-0"]


@pytest.mark.asyncio
async def test_a_cancelled_subscribe_leaves_no_live_subscription(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arm cancelled while waiting for the SUBACK must leak nothing.

    Missing the arm deadline is now an ordinary outcome -- a slow bridge is
    skipped rather than fatal -- so the cancellation lands on a task that has
    already registered its subscription and is waiting for the broker to
    acknowledge it. Nothing after that point is guaranteed to run, and the
    caller never receives the unsubscribe callable, so the teardown that walks
    the channel list cannot close it: one leaked subscription per slow bridge
    per attempt, each pinning the flow it belongs to.
    """
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    rx_topic = f"{MQTT_ROOT}/bridge-a/rx"
    suback = asyncio.Event()
    fake.activation_gates[rx_topic] = suback

    def ignore(_message: ReceiveMessage) -> None:
        return None

    subscribing = hass.async_create_task(
        config_flow_module._async_subscribe_ready(hass, rx_topic, ignore),
        f"{DOMAIN} test subscribe",
    )
    await asyncio.sleep(0)
    assert fake.rx_subscriptions(), "the subscription exists before the cancellation"

    subscribing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await subscribing

    assert all(not subscription.active for subscription in fake.rx_subscriptions()), (
        "the subscription nobody was handed must still be closed"
    )
    assert fake.subscribe_done_callbacks == {}, "no readiness watcher outlives the arm either"

    suback.set()
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_bridge_whose_rearm_keeps_failing_stops_being_retried(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hold survives a broker hiccup but gives up on a bridge that is gone.

    Retrying forever was silent: a bridge that dropped off mid-run kept being
    published to for the whole measurement with nothing said, and a broker
    refusing every publish looked exactly like a healthy hold.
    """
    from homeassistant.exceptions import HomeAssistantError

    monkeypatch.setattr(config_flow_module, "TRAVEL_REARM_INTERVAL_SECONDS", 0)
    attempts = 0
    fail_until = 2

    async def publish(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= fail_until or attempts > fail_until + 1:
            raise HomeAssistantError("broker is gone")

    monkeypatch.setattr(mqtt, "async_publish", publish)

    async with asyncio.timeout(_FLOW_WAIT_TIMEOUT_SECONDS):
        await config_flow_module._async_hold_sniff_open(hass, "rf433/bridge-a/cmd")

    limit = config_flow_module._SNIFF_REARM_FAILURE_LIMIT
    # Two failures, one success resetting the count, then `limit` in a row.
    assert attempts == fail_until + 1 + limit, (
        "a success must clear the failure count, and the hold must then stop"
    )


@pytest.mark.asyncio
async def test_two_remotes_pressed_at_once_are_never_silently_adopted(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first capture refuses to guess which remote the user meant (#57).

    Nothing calibrated exists yet on the first capture, so any Zemismart
    remote satisfies it -- and with the whole fleet listening that means any
    remote pressed anywhere in the house. Adopting one while another was
    heard would pin the wizard, and every later capture, to a remote the user
    never touched. No RSSI is reported by the bridges, so which press was
    nearer is not knowable here; refusing and asking is the honest answer.
    """
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", 0.2)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kaelyn shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE,
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await wait_for_sniff_starts(fake, 1)
    await wait_for_sniff_starts(fake, 1, "bridge-b")

    # The remote in the user's hand, and someone else's two rooms away.
    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 3}),
    )
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": b0_to_b1(SECOND_REMOTE_UP_B0), "t": 4}),
    )
    result = await advance_to_step(hass, flow_id, "learn_ambiguous")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["action"] == "UP"
    for prefix, remote_id in ((TEST_PREFIX, TEST_REMOTE_ID), (REF_PREFIX, REF_REMOTE_ID)):
        assert f"{prefix:06x}:{remote_id:02x} on channels 1,2" in placeholders["remotes"], (
            "both remotes are named so the user knows what happened"
        )
    assert "learn_retry" in result["menu_options"]

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_one_remote_heard_on_two_bridges_is_not_ambiguous(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the refusal above: copies are not competitors.

    Fleet listening delivers ONE press once per bridge that heard it. If the
    competing-remote check counted deliveries instead of remotes, every
    ordinary capture in a multi-bridge house would refuse itself.
    """
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", 0.2)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kaelyn shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE,
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await wait_for_sniff_starts(fake, 1)
    await wait_for_sniff_starts(fake, 1, "bridge-b")

    for bridge, stamp in (("bridge-a", 3), ("bridge-b", 4)):
        await fake.emit(
            active_rx(fake, bridge),
            f"rf433/{bridge}/rx",
            json.dumps({"frame": REFERENCE_UP_B1, "t": stamp}),
        )
    result = await advance_to_step(hass, flow_id, "learn_next")

    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


async def start_fleet_learn(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeMqtt,
    *,
    settle: float,
) -> str:
    """Arm one Learn wizard on Automatic with an explicit settle window."""
    prepare_config_flow(hass, monkeypatch)
    install_mqtt(monkeypatch, fake)
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", settle)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kaelyn shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE,
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await wait_for_sniff_starts(fake, 1)
    await wait_for_sniff_starts(fake, 1, "bridge-b")
    return flow_id


@pytest.mark.asyncio
async def test_an_unrecognised_opcode_is_held_to_the_same_trust_boundary(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window closing on a held capture adopts a remote just as hard (#57).

    An unrecognised opcode cannot end the window early -- nothing separates it
    from the OEM trailer burst until the window closes with no recognised
    frame -- so it is adopted on the TIMEOUT path instead. That path pins the
    wizard's remote exactly like a resolved capture, and an untabled remote's
    trailer burst from two rooms away is precisely what reaches it, so it owes
    the user the same refusal rather than a silent guess.
    """
    fake = FakeMqtt()
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 0.2, raising=False)
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    # Neither frame's opcode is in the action table, so neither can resolve
    # the attempt: the first is HELD and the window runs out.
    for bridge, frame in (
        ("bridge-a", UNTABLED_UP_B1),
        ("bridge-b", FOREIGN_UNTABLED_UP_B1),
    ):
        await fake.emit(
            active_rx(fake, bridge),
            f"rf433/{bridge}/rx",
            json.dumps({"frame": frame, "t": 3}),
        )
    result = await advance_to_step(hass, flow_id, "learn_ambiguous")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    for prefix in (UNTABLED_PREFIX, FOREIGN_UNTABLED_PREFIX):
        assert f"{prefix:06x}:{UNTABLED_REMOTE_ID:02x} on channels 1" in placeholders["remotes"]

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_press_outside_the_settle_window_does_not_veto_the_learn(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the refusal: only an OVERLAPPING press competes.

    Collecting competitors across the whole 30-second listen makes a
    legitimate learn impossible to complete in a house where anyone else
    touches a remote while the wizard is open -- every attempt refuses, and
    the screen's advice ("press again while nobody else is") cannot be
    complied with. One burst window around the winner is what "pressed at the
    same time" means; a press seconds earlier is ordinary house traffic.
    """
    fake = FakeMqtt()
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.02)

    # Untabled, so it cannot resolve the attempt -- it only competes.
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": FOREIGN_UNTABLED_UP_B1, "t": 3}),
    )
    await asyncio.sleep(0.05)
    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 4}),
    )
    result = await advance_to_step(hass, flow_id, "learn_next")

    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_one_remote_on_two_channel_selectors_is_two_presses(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Competitors are counted per PRESS, not per remote id (#57).

    The wizard stores the captured channel set as well as the identity -- it
    prefills the first cover with it -- so one remote heard on two selectors
    at once is two different answers to "which blind is this", and collapsing
    them by remote id would adopt whichever arrived first without a word.
    """
    fake = FakeMqtt()
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    other_selector = b0_to_b1(
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (3,), "UP", bases=TEST_BASES))
    )
    for bridge, frame in (("bridge-a", REFERENCE_UP_B1), ("bridge-b", other_selector)):
        await fake.emit(
            active_rx(fake, bridge),
            f"rf433/{bridge}/rx",
            json.dumps({"frame": frame, "t": 5}),
        )
    result = await advance_to_step(hass, flow_id, "learn_ambiguous")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    remote = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    assert f"{remote} on channels 1,2" in placeholders["remotes"]
    assert f"{remote} on channels 3" in placeholders["remotes"]

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_one_remotes_other_button_does_not_veto_its_own_learn(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the refusal: a remote agreeing with itself is no rival.

    The trust boundary decides WHICH remote and WHICH channel selector get
    adopted, and a second button from the very remote being adopted -- a stray
    DOWN while UP was asked for, or a trailer burst whose opcode byte the table
    happens to recognise -- is the same answer to "which blind is this". It is
    counted as its own press, and it must not refuse the capture it agrees with.
    """
    fake = FakeMqtt()
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    for bridge, frame in (("bridge-b", REFERENCE_DOWN_B1), ("bridge-a", REFERENCE_UP_B1)):
        await fake.emit(
            active_rx(fake, bridge),
            f"rf433/{bridge}/rx",
            json.dumps({"frame": frame, "t": 5}),
        )
    result = await advance_to_step(hass, flow_id, "learn_next")

    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_the_first_held_capture_is_the_remote_the_wizard_adopts(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A press heard LATER, outside the settle, cannot replace the held one.

    The held-candidate path is first-wins like the resolved one: whichever
    capture the wizard ends up judging is the capture it heard FIRST. Assigning
    the hold last-wins instead would let a remote pressed twenty seconds later
    -- far outside the window that decides ambiguity, so nothing would even flag
    it -- quietly become the remote the whole wizard is pinned to (#57).
    """
    fake = FakeMqtt()
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 0.2, raising=False)
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.02)

    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": UNTABLED_UP_B1, "t": 3}),
    )
    # Well outside the settle window, so it competes with nothing -- and the
    # capture already held has to survive it rather than be overwritten by it.
    await asyncio.sleep(0.05)
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": FOREIGN_UNTABLED_UP_B1, "t": 4}),
    )
    result = await advance_to_step(hass, flow_id, "learn_next")
    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }

    # Which remote it pinned is only visible in what it accepts next: the
    # identity gate the first capture set admits that remote's DOWN alone.
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "learn_sniff"},
    )
    assert result["step_id"] == "learn_sniff"
    result = await capture_learn_action(hass, fake, flow_id, UNTABLED_DOWN_B1, attempt=2)
    assert result["step_id"] == "learn_next"
    assert result["description_placeholders"] == {
        "captured": "DOWN",
        "measured": "UP, DOWN",
        "action": "STOP",
    }

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


def test_the_learn_sniff_window_covers_the_capture_window_and_its_settle() -> None:
    """The settle must fall inside a window the bridges are still sniffing.

    Nothing re-arms a learn bridge -- only the measure path has holder tasks --
    so a winner arriving in the capture window's final moment settles past the
    end of the armed window unless the window was armed longer than the window
    the user is given. Subscriptions staying live is not enough: a bridge that
    stopped sniffing reports no competitor, and silence reads as proof.
    """
    assert (
        config_flow_module._CAPTURE_TIMEOUT_SECONDS + config_flow_module._LEARN_SETTLE_SECONDS
        <= config_flow_module._LEARN_SNIFF_WINDOW_SECONDS
        <= MAX_SNIFF_WINDOW_SECONDS
    ), "the armed window must cover the capture window plus its settle, and the firmware cap"


@pytest.mark.asyncio
async def test_a_competitor_arriving_after_the_capture_window_still_refuses(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settle listens past the capture window, and is heard there.

    A capture held to the very end of the window is adopted on the timeout
    path, and its settle then runs entirely AFTER that window closed. A press
    arriving in that overhang is exactly the rival the refusal exists for, so
    the sniff must still be open when it lands.
    """
    fake = FakeMqtt()
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 0.05, raising=False)
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.3)

    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": UNTABLED_UP_B1, "t": 3}),
    )
    await asyncio.sleep(0.1)
    assert not [payload for _topic, payload in fake.published if payload.get("seconds") == 0], (
        "the capture window has closed and the sniff is still open: this is the settle"
    )
    # What the bridges were actually TOLD to sniff has to cover the production
    # capture window plus the settle that can follow a winner arriving in its
    # last moment. Read off the published command rather than the constant, and
    # measured against the production budget rather than this test's compressed
    # one -- otherwise the assertions below pass on a window that only looks
    # long enough because the timeout here is 50ms (#57).
    for bridge in ("bridge-a", "bridge-b"):
        armed = sniff_windows(fake, bridge)
        assert armed, f"{bridge} was never armed"
        assert min(armed) >= DEFAULT_SNIFF_WINDOW_SECONDS + TRAVEL_BURST_WINDOW_SECONDS, (
            "each bridge must be told to sniff past the end of the capture window, or in "
            "production this settle listens to bridges that have already stopped"
        )
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": FOREIGN_UNTABLED_UP_B1, "t": 4}),
    )
    result = await advance_to_step(hass, flow_id, "learn_ambiguous")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    for prefix in (UNTABLED_PREFIX, FOREIGN_UNTABLED_PREFIX):
        assert f"{prefix:06x}:{UNTABLED_REMOTE_ID:02x} on channels 1" in placeholders["remotes"]

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_rival_that_presses_again_later_still_refuses_the_capture(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole flow, for the erased-evidence case (#57).

    A held winner and a rival heard beside it, then the rival pressed again a
    second later -- ordinary behaviour for somebody using their own remote
    across the room. Its single timestamp moved out of the winner's settle
    window, the settle then saw nothing competing, and the wizard adopted a
    capture two remotes had claimed at the same moment.
    """
    fake = FakeMqtt()
    monkeypatch.setattr(config_flow_module, "_CAPTURE_TIMEOUT_SECONDS", 1.2, raising=False)
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    # Both untabled, so neither ends the window early: the first is HELD as the
    # winner and adopted when the window closes.
    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": UNTABLED_UP_B1, "t": 3}),
    )
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": FOREIGN_UNTABLED_UP_B1, "t": 4}),
    )
    await asyncio.sleep(0.5)
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": FOREIGN_UNTABLED_UP_B1, "t": 5}),
    )
    result = await advance_to_step(hass, flow_id, "learn_ambiguous")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    for prefix in (UNTABLED_PREFIX, FOREIGN_UNTABLED_PREFIX):
        assert f"{prefix:06x}:{UNTABLED_REMOTE_ID:02x} on channels 1" in placeholders["remotes"]

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_hung_sniff_stop_releases_the_bridge_instead_of_holding_it(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown is deadlined, and a claim outlives no publish that never settles.

    The stop fan-out runs in a `finally` on the flow's own task, once per bridge
    in a list as wide as the discovery snapshot allows. Undeadlined, one broker
    publish that never returns pins that task and every bridge's claim with it,
    so no later wizard can listen anywhere in the house until Home Assistant is
    restarted -- while a bridge that never receives its stop merely keeps
    sniffing until the window it was already given expires.
    """
    monkeypatch.setattr(config_flow_module, "_SNIFF_STOP_TIMEOUT_SECONDS", 0.05, raising=False)
    channels = sniff_channels(6)
    session_id = "stop-fanout"
    for channel in channels:
        config_flow_module._CAPTURE_OWNERS[channel.owner_key] = session_id

    async def publish(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(mqtt, "async_publish", publish)

    async with asyncio.timeout(_FLOW_WAIT_TIMEOUT_SECONDS):
        await config_flow_module._async_stop_sniff_channels(hass, session_id, channels)

    assert config_flow_module._CAPTURE_OWNERS == {}, (
        "the claim is given up with the publication: a fleet claimed by a session that "
        "is gone needs a restart to clear"
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_cancelled_teardown_still_deadlines_and_releases_the_fleet(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline cannot live on the caller's await, because the caller dies.

    Teardown runs in a `finally`, so it is itself liable to be cancelled -- and a
    timeout enforced by the awaiting task vanishes with it. That left the stop
    publications running with nobody to time them out, and a publication that
    never settles then held its bridge's claim for the life of the process (#57).
    Both the deadline and the release belong to an independent task.
    """
    monkeypatch.setattr(config_flow_module, "_SNIFF_STOP_TIMEOUT_SECONDS", 0.05, raising=False)
    channels = sniff_channels(3)
    session_id = "cancelled-teardown"
    for channel in channels:
        config_flow_module._CAPTURE_OWNERS[channel.owner_key] = session_id

    async def publish(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(mqtt, "async_publish", publish)

    tearing_down = hass.async_create_task(
        config_flow_module._async_stop_sniff_channels(hass, session_id, channels),
        f"{DOMAIN} test teardown",
    )
    await asyncio.sleep(0)
    tearing_down.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tearing_down

    assert config_flow_module._CAPTURE_OWNERS, "the publications have not been given up yet"
    # The deadline belongs to the cleanup task, which the cancellation above did
    # not touch, so waiting for that task is enough: it expires on its own and
    # releases every bridge on the way out.
    async with asyncio.timeout(_FLOW_WAIT_TIMEOUT_SECONDS):
        await hass.async_block_till_done()

    assert config_flow_module._CAPTURE_OWNERS == {}, (
        "a teardown that was cancelled still has to hand every bridge back"
    )


def _sniff_attempt(hass: HomeAssistant, action: str = "UP") -> Any:
    """Build one bare attempt for the ambiguity bookkeeping tests."""
    attempt = config_flow_module._SniffAttempt(
        action=action,
        measured={},
        future=hass.loop.create_future(),
    )
    attempt.future.cancel()
    return attempt


def _press(remote: str, channel: int = 1, button: str = "UP") -> Any:
    """Build one press signature the way the sniff handler keys them."""
    return (remote, frozenset({channel}), button)


def _stamp_winner(
    attempt: Any,
    signature: Any,
    heard_at: float,
    *,
    held: bool = False,
) -> Any:
    """Stamp one candidate winner exactly the way the sniff handler does.

    Record the press, THEN open its window, and store that window beside the
    capture it judges. The order is the point: the winner occupies a slot of the
    press bound before its own look-back reads the rest, which is the only reason
    that bound cannot decide a verdict.
    """
    config_flow_module._record_press(attempt, signature, heard_at)
    window = config_flow_module._open_contest_window(attempt, signature, heard_at)
    attempt.resolved_at = heard_at
    if held:
        attempt.unrecognized_window = window
    else:
        attempt.resolved_window = window
    return window


@pytest.mark.asyncio
async def test_a_press_heard_before_a_winner_is_judged_as_the_window_opens(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict is taken when the anchor exists, not re-derived afterwards.

    A window opening looks back exactly one settle window through what was
    recently heard. Deciding it here -- rather than storing a float per press and
    measuring it later -- is what removes the whole class of defect three review
    rounds found in that arithmetic (#57).

    Run at a press bound of 2, the smallest production allows, and with the
    winner recorded first as the handler records it: the winner takes one slot
    and the rival has to survive in the other. That interaction is what the
    earlier version of this test skipped by never recording the winner at all.
    """
    monkeypatch.setattr(config_flow_module, "_LEARN_CANDIDATE_CAP", 2)
    attempt = _sniff_attempt(hass)
    config_flow_module._record_press(attempt, _press("rival"), 10.0)
    config_flow_module._record_press(attempt, _press("stale"), 1.0)

    window = _stamp_winner(attempt, _press("winner"), 10.5)

    assert sorted(window.rivals) == ["rival on channels 1"], (
        "the press beside the winner competes; the one nine seconds earlier does not"
    )
    assert attempt.resolved_window is window, "the winner keeps the window that judged it"


@pytest.mark.asyncio
async def test_a_press_heard_after_a_winner_is_counted_as_it_arrives(
    hass: HomeAssistant,
) -> None:
    """The other half of the window needs no stored timestamp either."""
    attempt = _sniff_attempt(hass)
    window = _stamp_winner(attempt, _press("winner"), 10.0)
    assert window.rivals == set()

    config_flow_module._record_press(attempt, _press("rival"), 11.0)
    assert window.rivals == {"rival on channels 1"}

    config_flow_module._record_press(attempt, _press("late"), 20.0)
    assert window.rivals == {"rival on channels 1"}, (
        "a press a whole listen later did not overlap this winner"
    )


@pytest.mark.asyncio
async def test_the_winners_own_other_button_is_not_a_rival(hass: HomeAssistant) -> None:
    """Same remote, same selector: it agrees with the winner rather than rivalling it."""
    attempt = _sniff_attempt(hass)
    window = _stamp_winner(attempt, _press("winner", button="UP"), 10.0)

    config_flow_module._record_press(attempt, _press("winner", button="DOWN"), 10.2)
    assert window.rivals == set()

    config_flow_module._record_press(attempt, _press("winner", channel=3), 10.3)
    assert window.rivals == {"winner on channels 3"}, (
        "the same remote on ANOTHER selector is a different answer to which blind this is"
    )


@pytest.mark.asyncio
async def test_a_rivals_later_repress_cannot_erase_that_it_competed(
    hass: HomeAssistant,
) -> None:
    """Counting a rival when it arrives is what makes this unloseable.

    While the verdict was re-derived from one timestamp per press, the same
    remote pressed again seconds later moved its only timestamp out of the
    winner's window and erased the evidence. There is now nothing to move.
    """
    attempt = _sniff_attempt(hass)
    window = _stamp_winner(attempt, _press("winner"), 10.0)
    config_flow_module._record_press(attempt, _press("rival"), 10.1)
    assert window.rivals == {"rival on channels 1"}

    config_flow_module._record_press(attempt, _press("rival"), 25.0)

    assert window.rivals == {"rival on channels 1"}, (
        "the rival was judged against this winner when it arrived, once and for all"
    )


@pytest.mark.asyncio
async def test_a_superseded_anchor_never_decides_the_final_winner(
    hass: HomeAssistant,
) -> None:
    """The anchor MOVES, and each anchor is judged on its own (#57, round 5).

    A held unrecognised capture stamps an anchor that a recognised winner then
    supersedes. The reachable sequence: the held capture at t=1.0, a rival beside
    it at t=1.1, that rival pressed again at t=25.0, and a THIRD remote resolving
    the attempt at t=25.1. Pinning a press to whichever anchor happened to exist
    when its copy arrived let the rival be kept for its proximity to the
    ABANDONED anchor and then measured against the final one -- 24 seconds away,
    so 'unambiguous', with a second remote plainly on air.
    """
    attempt = _sniff_attempt(hass)
    held = _press("held")
    rival = _press("rival")
    recognised = _press("recognised")

    held_window = _stamp_winner(attempt, held, 1.0, held=True)
    config_flow_module._record_press(attempt, rival, 1.1)
    assert held_window.rivals == {"rival on channels 1"}, "the held anchor is contested"

    config_flow_module._record_press(attempt, rival, 25.0)
    resolved_window = _stamp_winner(attempt, recognised, 25.1)

    assert resolved_window.rivals == {"rival on channels 1"}, (
        "the FINAL winner had the rival on air 0.1s before it, whatever the old anchor saw"
    )
    assert attempt.unrecognized_window is held_window, "each candidate keeps its own verdict"


@pytest.mark.asyncio
async def test_an_abandoned_anchors_rival_does_not_veto_a_later_winner(
    hass: HomeAssistant,
) -> None:
    """The control for the transition above: refusals must stay compliable.

    If the rival is NOT pressed again, the final winner had nothing beside it and
    must be adopted -- otherwise one stray press early in a 30-second listen
    would refuse every capture for the rest of it, which is a refusal the user
    cannot act on.
    """
    attempt = _sniff_attempt(hass)
    held_window = _stamp_winner(attempt, _press("held"), 1.0, held=True)
    config_flow_module._record_press(attempt, _press("rival"), 1.1)

    resolved_window = _stamp_winner(attempt, _press("recognised"), 25.1)

    assert resolved_window.rivals == set()
    assert held_window.rivals == {"rival on channels 1"}, (
        "the abandoned window keeps its own verdict; it just is not the one read"
    )


def _held_capture(frame: str, prefix: int, remote_id: int) -> Any:
    """Build the capture a held (untabled) winner would be adopted from."""
    from custom_components.zemismart_blinds.codec import decode_rx_capture

    decoded = decode_rx_capture(frame)
    return config_flow_module._LearnCapture(
        frame=frame,
        prefix=prefix,
        remote_id=remote_id,
        channels=tuple(decoded["chans"]),
        command=decoded["cmd"],
        button="UP",
        inferred_button=None,
        base=derive_base(tuple(decoded["chans"]), "UP", decoded["cmd"], remote_id),
    )


def _settle_flow(hass: HomeAssistant) -> Any:
    """A bare flow object for driving the settle decision directly."""
    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    return flow


def _winner_pair(frame: str, prefix: int) -> tuple[Any, Any]:
    """Return one winner capture and the press signature the handler keys it on."""
    winner = _held_capture(frame, prefix, UNTABLED_REMOTE_ID)
    signature = press_signature(
        winner.prefix,
        winner.remote_id,
        winner.channels,
        "UP",
    )
    return winner, signature


@pytest.mark.asyncio
async def test_the_settle_judges_the_window_handed_to_it_not_the_newest(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adopted capture and the window that judged it cannot come apart.

    Both candidates can be open at once, and which one is adopted is settled by a
    race the bookkeeping cannot see: a recognised frame resolving the future in
    the same ready-batch as the capture timeout can leave the flow adopting the
    HELD capture. Reading "the newest window" then judged the recognised winner's
    window -- a clean one -- and lost the refusal the held capture had earned.
    Each capture now brings its own window, so the same attempt yields opposite
    verdicts depending only on which capture is being adopted.
    """
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", 0.01)
    flow = _settle_flow(hass)
    attempt = _sniff_attempt(hass)
    held, held_signature = _winner_pair(UNTABLED_UP_B1, UNTABLED_PREFIX)
    recognised, recognised_signature = _winner_pair(FOREIGN_UNTABLED_UP_B1, FOREIGN_UNTABLED_PREFIX)

    held_window = _stamp_winner(attempt, held_signature, 100.0, held=True)
    config_flow_module._record_press(attempt, _press("rival"), 100.005)
    resolved_window = _stamp_winner(attempt, recognised_signature, 100.5)

    assert held_window.rivals, "the held capture had a rival beside it"
    assert resolved_window.rivals == set(), "the recognised winner that replaced it did not"

    assert await flow._async_settle_first_capture(attempt, held, held_window) is False, (
        "adopting the HELD capture judges the HELD window, whatever opened after it"
    )
    assert await flow._async_settle_first_capture(attempt, recognised, resolved_window) is True


@pytest.mark.asyncio
async def test_a_capture_with_no_window_is_refused_rather_than_adopted(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unjudged is not the same as uncontested.

    A capture reaching the settle without a window is impossible today -- the
    window is assigned in the same breath as the anchor -- so this pins the
    direction that impossibility fails in. Adopting would make a missing window
    read as "nobody else was on air", which is the shape of every defect this
    subsystem has produced.
    """
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", 0.01)
    flow = _settle_flow(hass)
    attempt = _sniff_attempt(hass)
    winner, signature = _winner_pair(UNTABLED_UP_B1, UNTABLED_PREFIX)
    config_flow_module._record_press(attempt, signature, 100.0)
    attempt.resolved_at = 100.0

    assert await flow._async_settle_first_capture(attempt, winner, None) is False


@pytest.mark.asyncio
async def test_the_press_bound_leaves_room_for_a_rival(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The press bound has a floor of two, and nothing else enforces it.

    The winner occupies one slot before its own look-back reads the rest, so a
    bound of 1 evicts the only rival and the capture reads as unambiguous:
    record a rival, record the winner, and `recent` holds the winner alone.
    Production must therefore never drop below 2, and this is what says so.
    """
    assert config_flow_module._LEARN_CANDIDATE_CAP >= 2, (
        "a bound of 1 lets the winner evict the rival that would have refused it"
    )
    monkeypatch.setattr(config_flow_module, "_LEARN_CANDIDATE_CAP", 2)
    attempt = _sniff_attempt(hass)
    for index in range(6):
        config_flow_module._record_press(attempt, _press(f"rival-{index:02x}"), 10.0 + index * 0.01)

    window = _stamp_winner(attempt, _press("winner"), 10.06)

    assert len(attempt.recent) == 2, "the bound is saturated by the winner and one rival"
    assert window.rivals, "the smallest safe bound still names a rival"


@pytest.mark.asyncio
async def test_the_press_bound_drops_the_oldest_and_keeps_the_verdict(
    hass: HomeAssistant,
) -> None:
    """A bound on remembered presses must not be able to decide a verdict.

    Everything `recent` holds is inside one settle window, so the press the bound
    drops IS one the window about to open could have reached -- it is not safe by
    being out of reach, which is what an earlier comment here claimed. It is safe
    by arithmetic: the winner takes the newest slot, so a bound of N leaves N-1
    presses that can still be named, and any rival in window leaves the window
    non-empty.
    """
    attempt = _sniff_attempt(hass)
    cap = config_flow_module._LEARN_CANDIDATE_CAP
    for index in range(cap + 4):
        config_flow_module._record_press(attempt, _press(f"press-{index:02x}"), 10.0 + index * 0.01)

    assert len(attempt.recent) == cap, "the bound holds"
    remembered = {remote for remote, _channels, _button in attempt.recent}
    assert "press-00" not in remembered, "the oldest went first"
    assert f"press-{cap + 3:02x}" in remembered, "the newest is always kept"

    window = _stamp_winner(attempt, _press("winner"), 10.0 + (cap + 4) * 0.01)

    assert len(window.rivals) == cap - 1, "the winner takes one slot; every other is named"
    assert window.rivals, "the verdict survives the bound"


@pytest.mark.asyncio
async def test_more_rivals_than_can_be_named_still_refuse(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name bound shortens the SCREEN; the refusal is not negotiable.

    Driven through the settle rather than read off the window, so what is pinned
    is the decision the wizard acts on: refuse, and name as many of the rivals as
    a screen can carry.
    """
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", 0.01)
    flow = _settle_flow(hass)
    attempt = _sniff_attempt(hass)
    winner, signature = _winner_pair(UNTABLED_UP_B1, UNTABLED_PREFIX)
    cap = config_flow_module._LEARN_CANDIDATE_CAP
    window = _stamp_winner(attempt, signature, 100.0)
    for index in range(cap + 3):
        config_flow_module._record_press(attempt, _press(f"rival-{index:02x}"), 100.001)

    assert await flow._async_settle_first_capture(attempt, winner, window) is False
    assert len(flow._learn_candidates) == cap + 1, (
        "the winner, plus as many rivals as the screen can name"
    )
    assert flow._learn_candidates[0] == config_flow_module._press_name(signature)


@pytest.mark.asyncio
async def test_a_press_that_aged_out_is_never_a_rival(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Presses no window can reach are forgotten, so they cannot veto anything.

    Driven through the settle for the same reason: an unreachable press must
    produce an ADOPTION, not merely an empty rival set.
    """
    monkeypatch.setattr(config_flow_module, "_LEARN_SETTLE_SECONDS", 0.01)
    flow = _settle_flow(hass)
    attempt = _sniff_attempt(hass)
    winner, signature = _winner_pair(UNTABLED_UP_B1, UNTABLED_PREFIX)
    config_flow_module._record_press(attempt, _press("early"), 95.0)

    window = _stamp_winner(attempt, signature, 100.0)

    assert await flow._async_settle_first_capture(attempt, winner, window) is True
    assert flow._learn_candidates == (), "an adopted capture names nobody"
    assert set(attempt.recent) == {signature}, "the aged-out press was forgotten"


@pytest.mark.asyncio
async def test_the_listening_set_is_named_in_a_stable_order(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic resolves to the SORTED online set, not to discovery order.

    The set is joined into every screen that says which bridges are listening,
    and discovery order is whatever the retained beacons happened to arrive in
    -- so an unsorted set makes one unchanged fleet read as a different one
    between two visits to the same form.
    """
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt(
        bridges={
            "bridge-z": {"area_id": "attic"},
            "bridge-a": {"area_id": "living_room", "default": True},
        }
    )
    install_mqtt(monkeypatch, fake)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await advance_to_learn_setup(hass, flow_id)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Attic shade",
            CONF_AREA_ID: "attic",
            CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE,
        },
    )

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["bridge"] == "bridge-a, bridge-z"
    await wait_for_sniff_starts(fake, 1, "bridge-a")
    await wait_for_sniff_starts(fake, 1, "bridge-z")

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


def _learned_remote_name() -> str:
    """How the ambiguity screen names the remote these tests learn."""
    return f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x} on channels 1,2"


def _rival_remote_name() -> str:
    """How it names the second remote pressed alongside."""
    return f"{REF_PREFIX:06x}:{REF_REMOTE_ID:02x} on channels 1,2"


@pytest.mark.asyncio
async def test_a_rival_pressed_before_the_winner_refuses_the_whole_flow(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The winner's look-back, driven by real presses on a real fleet (#57).

    Everything else covering this half of the window asserts `window.rivals` on
    an attempt the test built, so deleting the look-back leaves those green while
    a real wizard silently adopts. Here two remotes are pressed on two bridges
    and the assertion is the SCREEN.

    The rival is a recognised DOWN while UP is solicited: it cannot resolve the
    attempt and is not held as the fallback, so it opens no window of its own.
    Only the winner looking back can notice it.
    """
    fake = FakeMqtt()
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": SECOND_REMOTE_DOWN_B1, "t": 3}),
    )
    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 4}),
    )
    result = await advance_to_step(hass, flow_id, "learn_ambiguous")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert _learned_remote_name() in placeholders["remotes"]
    assert _rival_remote_name() in placeholders["remotes"], (
        "the remote pressed just before the winner has to be named, not merely counted"
    )

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_rival_pressed_long_before_the_winner_is_adopted(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the look-back reaches one settle window, not the whole listen.

    Same two presses, spaced further apart than the settle. Without this the
    refusal above could be produced by a look-back that vetoes on anything ever
    heard -- a refusal the user cannot comply with in a house with two remotes.
    """
    fake = FakeMqtt()
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": SECOND_REMOTE_DOWN_B1, "t": 3}),
    )
    await asyncio.sleep(0.5)
    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 4}),
    )
    result = await advance_to_step(hass, flow_id, "learn_next")

    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_recognised_winner_that_supersedes_a_held_one_is_still_judged(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anchor transition, driven by real presses end to end (#57).

    An untabled press is HELD as the fallback and opens its own window; a
    recognised UP then supersedes it and must be judged by a window of its OWN.
    The rival is pressed twice -- once beside the held capture, once beside the
    recognised winner -- which is what the round-5 defect could not see: it kept
    the occurrence near the ABANDONED anchor and measured it against the final
    one, 24 seconds away, and adopted.
    """
    fake = FakeMqtt()
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": UNTABLED_UP_B1, "t": 3}),
    )
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": SECOND_REMOTE_DOWN_B1, "t": 4}),
    )
    # Long enough that the first copy of the rival is out of reach of anything
    # opening now: only the RE-press can put it beside the recognised winner.
    await asyncio.sleep(0.5)
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": SECOND_REMOTE_DOWN_B1, "t": 5}),
    )
    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 6}),
    )
    result = await advance_to_step(hass, flow_id, "learn_ambiguous")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert _learned_remote_name() in placeholders["remotes"]
    assert _rival_remote_name() in placeholders["remotes"]

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_recognised_winner_ignores_the_held_windows_own_rival(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the transition: the abandoned window does not veto.

    Identical to the case above without the re-press. The held capture's window
    is contested and the recognised winner's is not, and it is the winner's that
    decides -- otherwise a stray press early in the listen would refuse every
    capture for the rest of it.
    """
    fake = FakeMqtt()
    flow_id = await start_fleet_learn(hass, monkeypatch, fake, settle=0.2)

    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": UNTABLED_UP_B1, "t": 3}),
    )
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": SECOND_REMOTE_DOWN_B1, "t": 4}),
    )
    await asyncio.sleep(0.5)
    await fake.emit(
        active_rx(fake),
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_UP_B1, "t": 6}),
    )
    result = await advance_to_step(hass, flow_id, "learn_next")

    assert result["description_placeholders"] == {
        "captured": "UP",
        "measured": "UP",
        "action": "DOWN",
    }

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_measurement_reports_a_held_bridge_as_busy_not_silent(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim conflict on the measure path is its own failure, too.

    The remedy is to close the other window, which is nothing like the
    remedies the "nothing measured" screen recommends.
    """
    fake = FakeMqtt()
    flow_id = await start_learned_flow_at_cover_step(hass, fake, monkeypatch)

    # A second wizard takes bridge-b and holds it.
    holder = await start_user_flow(hass)
    holder_id = holder["flow_id"]
    await advance_to_learn_setup(hass, holder_id)
    result = await hass.config_entries.flow.async_configure(
        holder_id,
        {
            CONF_NAME: "Holder shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-b",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await wait_for_sniff_starts(fake, 1, "bridge-b")

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Sunroom shade", CONF_CHANNELS: "1,2"},
    )
    assert result["step_id"] == "cover_measure_setup"
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BRIDGE: "bridge-b"})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await advance_to_step(hass, flow_id, "cover_measure_busy")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["bridge"] == "bridge-b"
    assert "cover_measure_run" in result["menu_options"]

    hass.config_entries.flow.async_abort(flow_id)
    hass.config_entries.flow.async_abort(holder_id)


@pytest.mark.asyncio
async def test_a_fleet_sniff_is_refused_while_one_bridge_is_held(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet claim is all-or-nothing, and a partial claim is reported as busy.

    Claiming what is free and listening on the rest looks identical to a
    healthy fleet-wide session from the outside -- the progress screen still
    names every bridge -- while the bridge that could actually hear the remote
    may be precisely the excluded one. #57 exists because a session listened
    on too few bridges and reported the result as though it had listened on
    the right ones, so a subset is refused and named instead.
    """
    prepare_config_flow(hass, monkeypatch)
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    holder = await start_user_flow(hass)
    fleet = await start_user_flow(hass)
    holder_id = holder["flow_id"]
    fleet_id = fleet["flow_id"]
    await advance_to_learn_setup(hass, holder_id)
    await advance_to_learn_setup(hass, fleet_id)

    result = await hass.config_entries.flow.async_configure(
        holder_id,
        {
            CONF_NAME: "Holder shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: "bridge-a",
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await wait_for_sniff_starts(fake, 1, "bridge-a")

    result = await hass.config_entries.flow.async_configure(
        fleet_id,
        {
            CONF_NAME: "Fleet shade",
            CONF_AREA_ID: "living_room",
            CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE,
        },
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    # Not `async_block_till_done`: the holder's capture is deliberately still
    # pending here, and blocking on every task would wait out its whole window.
    result = await advance_to_step(hass, fleet_id, "learn_busy")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["bridge"] == "bridge-a", "the HELD bridge is named, not the whole fleet"
    assert sniff_starts(fake, "bridge-a") == 1, "the holder's bridge is not re-armed"
    assert sniff_starts(fake, "bridge-b") == 0, "no bridge listens for a session that was refused"
    assert "learn_retry" in result["menu_options"]

    hass.config_entries.flow.async_abort(fleet_id)
    hass.config_entries.flow.async_abort(holder_id)


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
    assert fake.published[2][1] == LEARN_SNIFF_START
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
        ("rf433/bridge-a/cmd", LEARN_SNIFF_START),
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
    """One flow cannot consume or stop another flow's bridge capture window.

    The blocked flow is told the bridge is BUSY, not that nothing was heard:
    with Automatic now claiming the whole fleet a conflict is far likelier,
    and "no press was detected" would send the user off testing a remote that
    was never the problem (#57).
    """
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
    assert second["step_id"] == "learn_busy", "a claim conflict is not silence on air"
    placeholders = second["description_placeholders"]
    assert placeholders is not None
    assert placeholders["bridge"] == "bridge-a", "the screen names the bridge that is held"
    assert len(fake.rx_subscriptions()) == 1
    assert len(fake.published) == 1

    hass.config_entries.flow.async_abort(first_id)
    await fake.wait_for_publications(2)
    # Wait for the first flow's TEARDOWN, not merely for its stop to be
    # published: the claim is released once teardown has accounted for every
    # stop, and a retry that arrives before that is refused as busy. Waiting on
    # the publication alone made the retry depend on which tick the release
    # happened to land in.
    await hass.async_block_till_done()
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
    # Driven to the step rather than polled once: the capture task publishes its
    # stop and then finishes accounting for it, so "the stop was published" is
    # not yet "the capture is done".
    second = await advance_to_step(hass, second_id, "learn_next")
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
        "bridge-a",
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
async def test_sniff_handler_keys_a_press_on_the_button_it_actually_is(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recognised press is counted as the button it IS, not the one requested.

    Keying every candidate on the solicited action collapsed one remote's DOWN
    into the UP it arrived beside, so two presses were tracked as one. Whether
    that remote is ambiguous is a separate question -- the settle asks it of the
    remote and channel set, which are identical here -- but the wizard cannot
    reason about either press while both share one key (#57).

    An UNTABLED opcode still falls back to the solicited action: it carries no
    opinion, and a remote's own untabled trailer burst must stay a copy of the
    press it trails rather than becoming a rival to it.
    """
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: False)
    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    session_id = "session-other-button"
    flow._sniff_session_id = session_id

    attempt = config_flow_module._SniffAttempt(
        action="UP",
        measured={},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, attempt, REFERENCE_DOWN_B1)
    _deliver_sniff_frame(hass, flow, session_id, attempt, REFERENCE_UP_B1)

    assert attempt.future.done(), "the requested action still resolves the attempt"
    assert sorted(button for _remote, _channels, button in attempt.recent) == ["DOWN", "UP"]
    assert attempt.resolved_window is not None
    assert attempt.resolved_window.rivals == set(), (
        "one remote's two buttons on one selector are the same answer, not rivals"
    )

    held = config_flow_module._SniffAttempt(
        action="UP",
        measured={},
        future=hass.loop.create_future(),
    )
    _deliver_sniff_frame(hass, flow, session_id, held, UNTABLED_UP_B1)
    assert [button for _remote, _channels, button in held.recent] == ["UP"], (
        "an untabled opcode is keyed by the action being solicited"
    )
    held.future.cancel()


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
            "rf433-bridge-office",
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
    rx = active_rx(fake)
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
    # 3 Learn sniffs on bridge-a preceded this; the measure arm is the 4th.
    await wait_for_sniff_starts(fake, 4)
    await wait_for_sniff_starts(fake, 1, "bridge-b")

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
    await wait_for_sniff_starts(fake, 5)
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


async def arm_travel_measurement(
    hass: HomeAssistant,
    fake: FakeMqtt,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge: str,
) -> ConfigFlowResult:
    """Learn a remote on bridge-a, then arm a blank-travel measurement.

    The Learn phase always pins bridge-a explicitly, so bridge-b's sniff
    starts count only the ones this measurement opened. Returns the progress
    result, whose placeholders name the bridges actually listening.
    """
    flow_id = await start_learned_flow_at_cover_step(hass, fake, monkeypatch)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Sunroom shade", CONF_CHANNELS: "1,2"},
    )
    assert result["step_id"] == "cover_measure_setup"
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BRIDGE: bridge})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "measuring"
    return result


@pytest.mark.asyncio
async def test_measure_on_automatic_listens_on_every_online_bridge(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automatic arms the whole fleet, not the device's area bridge (#57).

    The live taps behind #57: the one listening bridge heard 3 of 11 physical
    presses while three or four peers heard nearly every one. Automatic is
    only trustworthy if it means every online bridge.
    """
    fake = FakeMqtt()
    result = await arm_travel_measurement(
        hass,
        fake,
        monkeypatch,
        bridge=config_flow_module._AUTOMATIC_BRIDGE,
    )
    # bridge-a already carries the three Learn sniffs; the fourth is this arm.
    await wait_for_sniff_starts(fake, 4, "bridge-a")
    await wait_for_sniff_starts(fake, 1, "bridge-b")

    assert {rx.topic for rx in fake.rx_subscriptions() if rx.active} == {
        "rf433/bridge-a/rx",
        "rf433/bridge-b/rx",
    }
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["bridge"] == "bridge-a, bridge-b"

    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_an_explicit_bridge_pick_measures_on_that_bridge_alone(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picker stays an override: one named bridge listens, nothing else.

    Diagnosing what a single bridge can hear is the reason the picker
    survived Automatic going fleet-wide, so it must not quietly widen.
    """
    fake = FakeMqtt()
    result = await arm_travel_measurement(hass, fake, monkeypatch, bridge="bridge-b")
    await wait_for_sniff_starts(fake, 1, "bridge-b")

    # Automatic arms in sorted order, so a fleet-wide regression would have
    # published bridge-a's arm BEFORE the bridge-b arm just awaited.
    assert sniff_starts(fake, "bridge-a") == 3, "only the three Learn sniffs may be on bridge-a"
    assert {rx.topic for rx in fake.rx_subscriptions() if rx.active} == {"rf433/bridge-b/rx"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["bridge"] == "bridge-b"

    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_one_press_delivered_by_every_bridge_measures_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two bridges hearing one press yield ONE run, timed from the first copy.

    Each bridge reports the press on its own ``t`` -- deliberately far apart
    here, and sharing a ``boot`` number by coincidence, which is exactly the
    pair a cross-bridge subtraction would turn into garbage. The copies must
    be filtered by signature so only bridge-b's first delivery anchors the
    run; timing then falls to the receive clock the whole fleet shares.
    """
    fake = FakeMqtt()
    flow_id = (
        await arm_travel_measurement(
            hass,
            fake,
            monkeypatch,
            bridge=config_flow_module._AUTOMATIC_BRIDGE,
        )
    )["flow_id"]
    await wait_for_sniff_starts(fake, 4, "bridge-a")
    await wait_for_sniff_starts(fake, 1, "bridge-b")
    pin_receive_clock(monkeypatch, 100.0, 100.05, 114.5)

    down = _travel_rx_frame("DOWN")
    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": down, "t": 500_000, "boot": 7}),
    )
    await fake.emit(
        active_rx(fake, "bridge-a"),
        "rf433/bridge-a/rx",
        json.dumps({"frame": down, "t": 1_000, "boot": 7}),
    )
    result = await advance_to_step(hass, flow_id, "cover_measure_stop")
    assert result["step_id"] == "cover_measure_stop", "the second copy must not open a second run"

    await fake.emit(
        active_rx(fake, "bridge-a"),
        "rf433/bridge-a/rx",
        json.dumps({"frame": _travel_rx_frame("STOP"), "t": 15_310, "boot": 7}),
    )
    result = await advance_to_step(hass, flow_id, "cover_measure_next")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["measured"] == "DOWN"
    assert placeholders["stored"] == "15", (
        "14.5s of receive-clock separation from the FIRST copy, rounded up"
    )

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_a_stop_only_another_bridge_heard_still_closes_the_run(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Kaelyn case: the arming bridge never hears the STOP at all.

    bridge-a hears the direction press, bridge-b hears only the STOP. The two
    bridges' ``t`` counters share no epoch -- and their ``boot`` numbers can
    collide, as they deliberately do here -- so the run is bounded by the
    receive clock instead. Before #57 bridge-b was not even subscribed and
    the measurement ran on as though the user had never pressed STOP.
    """
    fake = FakeMqtt()
    flow_id = (
        await arm_travel_measurement(
            hass,
            fake,
            monkeypatch,
            bridge=config_flow_module._AUTOMATIC_BRIDGE,
        )
    )["flow_id"]
    await wait_for_sniff_starts(fake, 4, "bridge-a")
    await wait_for_sniff_starts(fake, 1, "bridge-b")
    pin_receive_clock(monkeypatch, 100.0, 114.5)

    await fake.emit(
        active_rx(fake, "bridge-a"),
        "rf433/bridge-a/rx",
        json.dumps({"frame": _travel_rx_frame("DOWN"), "t": 1_000, "boot": 7}),
    )
    await advance_to_step(hass, flow_id, "cover_measure_stop")

    await fake.emit(
        active_rx(fake, "bridge-b"),
        "rf433/bridge-b/rx",
        json.dumps({"frame": _travel_rx_frame("STOP"), "t": 900_000, "boot": 7}),
    )
    result = await advance_to_step(hass, flow_id, "cover_measure_next")

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["stored"] == "15", (
        "14.5s of receive-clock separation, not a cross-bridge t subtraction"
    )

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_the_travel_handler_skips_our_echo_on_every_bridge(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame we transmitted is our echo off ANY bridge, not a press.

    Fleet listening multiplies the echo: an automation driving this very
    cover mid-measurement comes back off every subscribed bridge at once, so
    the guard has to sit in the shared handler ahead of the run rather than
    on one bridge's path.
    """
    from types import SimpleNamespace

    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    session_id = "session-travel-echo"
    flow._sniff_session_id = session_id
    run = TravelRun(
        identity=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1, 2),
        wanted=frozenset({"DOWN"}),
    )
    armed = asyncio.Event()
    future: asyncio.Future[Any] = hass.loop.create_future()

    def deliver(bridge: str) -> None:
        topic = f"rf433/{bridge}/rx"
        config_flow_module._handle_travel_message(
            flow,
            session_id,
            topic,
            bridge,
            run,
            armed,
            future,
            cast(
                "ReceiveMessage",
                SimpleNamespace(
                    topic=topic,
                    payload=json.dumps({"frame": _travel_rx_frame("DOWN"), "t": 1_000}),
                    retain=False,
                ),
            ),
        )

    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: True)
    for bridge in ("bridge-a", "bridge-b"):
        deliver(bridge)
    assert run.started is None, "our own transmission opened a run"
    assert not armed.is_set()

    # Classified as foreign, the very same frame is a real press.
    monkeypatch.setattr(config_flow_module, "_is_own_emission", lambda _hass, _frame: False)
    deliver("bridge-b")
    assert run.started is not None
    assert run.started.bridge_id == "bridge-b", "the run is attributed to the bridge that heard it"
    assert armed.is_set()
    future.cancel()


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


def _foreign_rx_frame(button: str, channels: tuple[int, ...] = (1, 2)) -> str:
    """Synthesize a capture from the tabled reference remote (not the entry's)."""
    return b0_to_b1(
        encode_b0(make_payload(REF_PREFIX, REF_REMOTE_ID, channels, button, bases=REF_BASES))
    )


_, THIRD_PREFIX, THIRD_REMOTE_ID, THIRD_BASES, _third_payload = SYNTHETIC_REMOTES[2]


def _third_rx_frame(button: str, channels: tuple[int, ...] = (1, 2)) -> str:
    """Synthesize a capture from a SECOND remote that is not the entry's."""
    return b0_to_b1(
        encode_b0(make_payload(THIRD_PREFIX, THIRD_REMOTE_ID, channels, button, bases=THIRD_BASES))
    )


async def _start_remeasure_listening(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ConfigEntry, str, FakeMqtt]:
    """Reach an armed re-measure session on bridge-a for a stored cover."""
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
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "cover_pick_edit"})
    await hass.config_entries.flow.async_configure(flow_id, {CONF_COVER_ID: slider[CONF_COVER_ID]})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "cover_measure_start"})
    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_BRIDGE: "bridge-a"})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    return entry, flow_id, fake


@pytest.mark.asyncio
async def test_several_foreign_remotes_are_never_one_click_adopted(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adoption needs ONE candidate; the fleet can deliver a houseful (#57).

    The one-click identity swap rewrites the device's stored remote, so
    offering it while two different remotes were heard would let the first
    arrival win a decision the user never made.
    """
    monkeypatch.setattr(config_flow_module, "TRAVEL_ARM_TIMEOUT_SECONDS", 0.5)
    entry, flow_id, fake = await _start_remeasure_listening(hass, monkeypatch)
    rx = fake.rx_subscriptions()[-1]
    for frame, millis in (
        (_foreign_rx_frame("UP"), 1_000),
        (_third_rx_frame("UP"), 2_000),
    ):
        await fake.emit(
            rx,
            "rf433/bridge-a/rx",
            json.dumps({"frame": frame, "t": millis, "boot": 7}),
        )
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "cover_measure_mismatch"
    assert "cover_measure_use_heard" not in result["menu_options"], (
        "adopting one of two heard remotes would be a coin toss"
    )
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    for prefix, remote_id in ((REF_PREFIX, REF_REMOTE_ID), (THIRD_PREFIX, THIRD_REMOTE_ID)):
        assert f"{prefix:06x}:{remote_id:02x}" in placeholders["detail"]

    # The stored identity is untouched by a screen that refused to adopt.
    unchanged = RemoteConfig.from_entry(entry.data).remote
    assert (unchanged.prefix, unchanged.remote_id) == (TEST_PREFIX, TEST_REMOTE_ID)

    hass.config_entries.flow.async_abort(flow_id)


def heard_press(frame: str, prefix: int, remote_id: int) -> Any:
    """Decode one synthesized frame into the mismatch screen's heard record."""
    from custom_components.zemismart_blinds.codec import decode_rx_capture
    from custom_components.zemismart_blinds.travel_capture import HeardPress

    decoded = decode_rx_capture(frame)
    return HeardPress(
        frame=frame,
        prefix=prefix,
        remote_id=remote_id,
        channels=tuple(decoded["chans"]),
        command=decoded["cmd"],
        button="UP",
    )


def _adopt_guard_flow(hass: HomeAssistant) -> Any:
    """Build one flow parked where the identity swap would happen."""
    flow = config_flow_module.ZemismartBlindsConfigFlow()
    flow.hass = hass
    flow.flow_id = "adopt-guard"
    flow.handler = DOMAIN
    flow.context = {}
    flow._identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    flow._pending_measure = config_flow_module._PendingMeasure(
        origin="wizard",
        name="Slider",
        channels=(1, 2),
    )
    return flow


@pytest.mark.asyncio
async def test_the_adopt_step_refuses_what_its_menu_would_not_offer(hass: HomeAssistant) -> None:
    """The identity swap re-makes the refusal instead of trusting its caller.

    Driven at the step rather than through the flow deliberately: Home
    Assistant validates a menu choice against the options the step published,
    so this is defence in depth rather than a reachable bypass today. It is
    cheap and it keeps the rule where the damage is done -- the step rewrites
    the device's stored identity from ``heard[0]``, and every reason not to
    lives one screen away in code that only decides which buttons to draw.
    """
    flow = _adopt_guard_flow(hass)
    flow._measure_heard = (
        heard_press(_foreign_rx_frame("UP"), REF_PREFIX, REF_REMOTE_ID),
        heard_press(_third_rx_frame("UP"), THIRD_PREFIX, THIRD_REMOTE_ID),
    )

    result = await flow.async_step_cover_measure_use_heard()

    assert result["step_id"] == "cover_measure_mismatch", "the refusal is re-made, not bypassed"
    assert "cover_measure_use_heard" not in result["menu_options"]
    assert flow._identity == RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES), (
        "no identity was adopted"
    )


@pytest.mark.asyncio
async def test_a_capped_heard_list_cannot_prove_one_remote_and_is_refused(
    hass: HomeAssistant,
) -> None:
    """One-click adopt needs uniqueness the capped list cannot establish (#57).

    Everything the screen concludes -- one foreign remote, and this device's own
    never heard -- is read off a list with a cap. A stranger's remote worked
    across enough selector positions fills it alone, and the press dropped for
    room is then the user's own, so both conclusions hold of the list and
    neither holds of the air. This is where a wrong one overwrites a correct
    stored identity, so the overflow refuses rather than being outvoted by the
    presses that happened to fit.
    """
    flow = _adopt_guard_flow(hass)
    # One foreign remote, several selector positions: `_foreign_remotes` sees
    # exactly one identity and our own is nowhere in the list -- the shape that
    # offers the adopt.
    flow._measure_heard = tuple(
        heard_press(_foreign_rx_frame("UP"), REF_PREFIX, REF_REMOTE_ID) for _selector in range(3)
    )
    flow._measure_heard_overflowed = True

    result = await flow.async_step_cover_measure_mismatch()

    assert result["step_id"] == "cover_measure_mismatch"
    assert "cover_measure_use_heard" not in result["menu_options"], (
        "a capped list must not be offered as proof of which remote drives this shade"
    )
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "more remotes than can be listed" in placeholders["detail"]

    # And the step itself refuses, not merely the menu that reaches it.
    result = await flow.async_step_cover_measure_use_heard()

    assert result["step_id"] == "cover_measure_mismatch"
    assert flow._identity == RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES), (
        "no identity was adopted"
    )


@pytest.mark.asyncio
async def test_an_uncapped_heard_list_still_offers_the_adopt(hass: HomeAssistant) -> None:
    """The control: the refusal above must come from the overflow, not the shape.

    Same presses, same single foreign identity, nothing dropped -- this is the
    Kaelyn field case the one-click adopt exists for, and it has to keep working
    or the fix above would be a silent removal of the feature.
    """
    flow = _adopt_guard_flow(hass)
    flow._measure_heard = tuple(
        heard_press(_foreign_rx_frame("UP"), REF_PREFIX, REF_REMOTE_ID) for _selector in range(3)
    )

    result = await flow.async_step_cover_measure_mismatch()

    assert result["menu_options"][0] == "cover_measure_use_heard"


@pytest.mark.asyncio
async def test_a_real_capped_measurement_refuses_the_adopt_end_to_end(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The producer and the consumer of the overflow are wired together (#57).

    Everything else about the capped list is tested on either side of one
    assignment: the run sets ``heard_overflowed``, the screen refuses on
    ``_measure_heard_overflowed``. Neither notices if the line carrying the first
    into the second is deleted -- and then a real arm timeout can still offer to
    rewrite this device's identity off a list that overflowed. So this drives the
    whole path: one foreign remote worked across more selector positions than the
    list can hold, through a genuine measurement that times out, and asks the
    screen what it offers.
    """
    monkeypatch.setattr(config_flow_module, "TRAVEL_ARM_TIMEOUT_SECONDS", 0.5)
    entry, flow_id, fake = await _start_remeasure_listening(hass, monkeypatch)
    rx = fake.rx_subscriptions()[-1]
    # One remote, one distinct press per selector position: enough of them that
    # the cap has to turn the last one away.
    for selector in range(1, _HEARD_CAP + 2):
        await fake.emit(
            rx,
            "rf433/bridge-a/rx",
            json.dumps(
                {
                    "frame": _foreign_rx_frame("UP", channels=(selector,)),
                    "t": 1_000 + selector,
                    "boot": 7,
                }
            ),
        )
    await hass.async_block_till_done()

    result = await advance_to_step(hass, flow_id, "cover_measure_mismatch")

    assert "cover_measure_use_heard" not in result["menu_options"], (
        "the list overflowed, so 'one foreign remote and ours never heard' is unprovable"
    )
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "more remotes than can be listed" in placeholders["detail"]
    unchanged = RemoteConfig.from_entry(entry.data).remote
    assert (unchanged.prefix, unchanged.remote_id) == (TEST_PREFIX, TEST_REMOTE_ID)

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_aborting_at_the_arm_to_stop_handoff_releases_every_bridge(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow removed BETWEEN the two measurement phases must not keep the fleet.

    ``_async_measure_arm`` returning "armed" deliberately leaves the session open
    for the STOP phase to inherit, and Home Assistant advances to that phase from
    a task it schedules when the arm task completes -- suppressing ``UnknownFlow``
    if the flow has gone by the time that task runs. An abort processed in that
    gap therefore left a session NO task would ever close: the per-bridge holders
    keep re-arming their sniff windows, and every claim is held until Home
    Assistant restarts, so no later wizard can listen anywhere in the house (#57).

    Deterministic rather than raced: done-callbacks run in registration order, so
    Home Assistant's own scheduling callback runs first and the abort below lands
    before the task it created gets its turn.
    """
    _entry, flow_id, fake = await _start_remeasure_listening(hass, monkeypatch)
    flow = cast(
        "config_flow_module.ZemismartBlindsConfigFlow",
        hass.config_entries.flow._progress[flow_id],
    )
    arming = flow._measure_task
    assert arming is not None, "the arm phase is in flight"
    arming.add_done_callback(lambda _task: hass.config_entries.flow.async_abort(flow_id))

    await fake.emit(
        fake.rx_subscriptions()[-1],
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_DOWN_B1, "t": 1_000, "boot": 7}),
    )
    async with asyncio.timeout(_FLOW_WAIT_TIMEOUT_SECONDS):
        await hass.async_block_till_done()

    assert arming.result() == "armed", (
        "the fixture must reach the handoff: the arm phase hands a LIVE session on"
    )
    assert config_flow_module._CAPTURE_OWNERS == {}, (
        "the removed flow's session was closed, so no bridge is left claimed by it"
    )
    assert not [subscription for subscription in fake.rx_subscriptions() if subscription.active], (
        "and nothing is still subscribed on its behalf"
    )


@pytest.mark.asyncio
async def test_the_stop_phase_keeps_waiting_when_teardown_clears_the_session(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task that still owes a result outranks a session already cleared.

    ``_async_measure_finish`` sets ``_measure_session`` to None and THEN tears
    the session down, so between the two this step is re-entered with no session
    and a task still running -- and every poll re-enters it. Reading the missing
    session as an abandoned measurement threw away a run the user had just made
    and dropped the flow into the cover form, for no reason but a broker that
    took a moment to accept the stop publication. Bounding that teardown (#57)
    widened the window from "one tick" to "as long as the broker is slow".
    """
    _entry, flow_id, fake = await _start_remeasure_listening(hass, monkeypatch)
    await fake.emit(
        fake.rx_subscriptions()[-1],
        "rf433/bridge-a/rx",
        json.dumps({"frame": REFERENCE_DOWN_B1, "t": 1_000, "boot": 7}),
    )
    result = await advance_to_step(hass, flow_id, "cover_measure_stop")
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    flow = cast(
        "config_flow_module.ZemismartBlindsConfigFlow",
        hass.config_entries.flow._progress[flow_id],
    )
    assert flow._measure_task is not None and not flow._measure_task.done()
    session = flow._measure_session
    flow._measure_session = None

    result = await hass.config_entries.flow.async_configure(flow_id)

    assert result["type"] is FlowResultType.SHOW_PROGRESS, (
        "the run is still in flight; only the ABSENCE of a task means there is nothing to wait for"
    )
    assert result["step_id"] == "cover_measure_stop"

    # Hand the session back so teardown still releases bridge-a's claim.
    flow._measure_session = session
    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_the_own_remote_is_diagnosed_wherever_it_arrived(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which bridge delivered first must not decide what the screen says.

    Reading the diagnosis off ``heard[0]`` made it a race: a stranger's remote
    arriving first hid the fact that this device's OWN remote was heard on the
    wrong channels, and the screen then offered to replace a correct identity
    with the stranger's. The channel-selector diagnosis is exact -- the
    identity matched -- so it is taken wherever in the list it landed.
    """
    monkeypatch.setattr(config_flow_module, "TRAVEL_ARM_TIMEOUT_SECONDS", 0.5)
    entry, flow_id, fake = await _start_remeasure_listening(hass, monkeypatch)
    rx = fake.rx_subscriptions()[-1]
    own_wrong_channel = b0_to_b1(
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (3,), "UP", bases=TEST_BASES))
    )
    for frame, millis in ((_foreign_rx_frame("UP"), 1_000), (own_wrong_channel, 2_000)):
        await fake.emit(
            rx,
            "rf433/bridge-a/rx",
            json.dumps({"frame": frame, "t": millis, "boot": 7}),
        )
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(flow_id)

    assert result["step_id"] == "cover_measure_mismatch"
    assert result["menu_options"] == ["cover_measure_run", "cover_measure_manual"], (
        "the identity is right; replacing it with the remote that arrived first is not the fix"
    )
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "channels 3" in placeholders["detail"]
    assert "channels 1,2" in placeholders["detail"]
    unchanged = RemoteConfig.from_entry(entry.data).remote
    assert (unchanged.prefix, unchanged.remote_id) == (TEST_PREFIX, TEST_REMOTE_ID)

    hass.config_entries.flow.async_abort(flow_id)


@pytest.mark.asyncio
async def test_hearing_a_different_remote_offers_an_identity_update(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign remote heard during the arming window can be adopted.

    The Kaelyn field case: the stored identity does not match the remote in
    the user's hand, so nothing ever matches and the old flow spun for the
    full deadline. Now the timeout names the heard remote and one menu click
    replaces the entry's identity -- seeded from the presses actually heard,
    with only the untouched button derived.
    """
    monkeypatch.setattr(config_flow_module, "TRAVEL_ARM_TIMEOUT_SECONDS", 0.5)
    entry, flow_id, fake = await _start_remeasure_listening(hass, monkeypatch)
    rx = fake.rx_subscriptions()[-1]
    for button, millis in (("UP", 1_000), ("STOP", 3_000)):
        await fake.emit(
            rx,
            "rf433/bridge-a/rx",
            json.dumps({"frame": _foreign_rx_frame(button), "t": millis, "boot": 7}),
        )
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "cover_measure_mismatch"
    assert result["menu_options"] == [
        "cover_measure_use_heard",
        "cover_measure_run",
        "cover_measure_manual",
    ]
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert f"{REF_PREFIX:06x}:{REF_REMOTE_ID:02x}" in placeholders["detail"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "cover_measure_use_heard"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "cover_measure_run"

    adopted = RemoteConfig.from_entry(entry.data).remote
    assert (adopted.prefix, adopted.remote_id) == (REF_PREFIX, REF_REMOTE_ID)
    assert adopted.bases is not None
    # UP and STOP were measured from the heard frames; only DOWN is derived.
    assert adopted.bases.up == REF_BASES.up
    assert adopted.bases.stop == REF_BASES.stop
    assert adopted.bases.down == REF_BASES.down
    assert entry.unique_id == adopted.key

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_hearing_the_own_remote_on_other_channels_names_both_sets(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrong-channel-selector near-miss is explained, not offered as update.

    The identity is right, so replacing it would be wrong -- the fix is the
    remote's channel selector (or the cover's channels), and the menu says so
    without the adopt option.
    """
    monkeypatch.setattr(config_flow_module, "TRAVEL_ARM_TIMEOUT_SECONDS", 0.5)
    _entry, flow_id, fake = await _start_remeasure_listening(hass, monkeypatch)
    rx = fake.rx_subscriptions()[-1]
    own_wrong_channel = b0_to_b1(
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (3,), "UP", bases=TEST_BASES))
    )
    await fake.emit(
        rx,
        "rf433/bridge-a/rx",
        json.dumps({"frame": own_wrong_channel, "t": 1_000, "boot": 7}),
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "cover_measure_mismatch"
    assert result["menu_options"] == ["cover_measure_run", "cover_measure_manual"]
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "channels 3" in placeholders["detail"]
    assert "channels 1,2" in placeholders["detail"]

    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()
