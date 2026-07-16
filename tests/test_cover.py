"""Home Assistant fixture tests for RF-start-gated travel-time covers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from homeassistant.components.cover import ATTR_CURRENT_POSITION, ATTR_POSITION
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError

from custom_components.zemismart_blinds import cover as cover_module
from custom_components.zemismart_blinds.codec import encode_b0, make_payload
from custom_components.zemismart_blinds.cover import ZemismartCover
from custom_components.zemismart_blinds.models import (
    BlindConfig,
    BridgeRegistry,
    EntryRuntime,
    RemoteIdentity,
    ZemismartHub,
)
from custom_components.zemismart_blinds.state_sync import HeardEvent
from tests.synthetic import TEST_ACTION_BASES, TEST_PREFIX, TEST_REMOTE_ID

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import EntityPlatform


def cover_config(*, travel: float = 0.04) -> BlindConfig:
    """Return a fast representative cover configuration."""
    return BlindConfig(
        name="Living Room Left",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        channels=(1, 2),
        travel_up=travel,
        travel_down=travel,
        area_id="living_room",
        repeats=2,
    )


def online_registry(bridge_id: str = "bridge-a") -> BridgeRegistry:
    """Return one same-area online bridge."""
    registry = BridgeRegistry()
    registry.update_info(bridge_id, {"area": "living_room"})
    registry.update_availability(bridge_id, "online")
    return registry


def platform_stub() -> EntityPlatform:
    """Return the minimal typed platform surface required by entity teardown."""
    return cast(
        "EntityPlatform", SimpleNamespace(platform_name="zemismart_blinds", config_entry=None)
    )


async def attach_cover(
    hass: HomeAssistant,
    hub: ZemismartHub,
    *,
    config: BlindConfig | None = None,
    cover_type: type[ZemismartCover] = ZemismartCover,
    entry_id: str = "entry-1",
    entity_id: str = "cover.living_room_left",
) -> ZemismartCover:
    """Attach one entity to the real HA core without a platform wrapper."""
    entity = cover_type(entry_id, EntryRuntime(config or cover_config(), hub))
    entity.hass = hass
    entity.entity_id = entity_id
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()
    await entity.async_added_to_hass()
    return entity


def restored_cover_type(restored_state: State) -> type[ZemismartCover]:
    """Return a cover type that restores one supplied HA state."""

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    return RestoredCover


def stopped_unverified_anchor_state() -> State:
    """Return a stopped estimate whose restore-time anchor is questioned."""
    config = cover_config()
    return State(
        "cover.living_room_left",
        "open",
        {
            ATTR_CURRENT_POSITION: 80,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": 0,
            "unverified_anchor_bridge": "bridge-a",
        },
    )


def acknowledge(hub: ZemismartHub, bridge_id: str, body: Mapping[str, Any]) -> None:
    """Emit admission and first-RF-dispatch statuses for one command."""
    assert hub.handle_status(
        bridge_id,
        {
            "status": "accepted",
            "command_id": body["command_id"],
        },
    )
    assert hub.handle_status(
        bridge_id,
        {
            "status": "started",
            "command_id": body["command_id"],
        },
    )


def dispatch_heard_press(
    hub: ZemismartHub,
    config: BlindConfig,
    button: str,
    channels: tuple[int, ...],
    *,
    at: float,
) -> None:
    """Dispatch one synthetic decoded physical press to registered covers."""
    hub._dispatch_heard(
        HeardEvent(
            button=button,
            chans=frozenset(channels),
            remote_key=config.remote_key,
            heard_at=at,
            bridge_id="synthetic-rx-bridge",
        ),
    )


@pytest.mark.asyncio
async def test_coalesced_covers_share_started_ack_but_keep_own_travel_times(
    hass: HomeAssistant,
) -> None:
    """One group start gates each contributing cover's independent estimator."""
    bodies: list[dict[str, Any]] = []
    accepted = asyncio.Event()
    allow_start = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        bodies.append(body)
        bridge_id = topic.split("/")[1]
        assert hub.handle_status(
            bridge_id,
            {"status": "accepted", "command_id": body["command_id"]},
        )
        accepted.set()
        await allow_start.wait()
        assert hub.handle_status(
            bridge_id,
            {"status": "started", "command_id": body["command_id"]},
        )

    hub = ZemismartHub(online_registry(), publish)
    first = await attach_cover(
        hass,
        hub,
        config=BlindConfig(
            name="Living Room channel 1",
            remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
            channels=(1,),
            travel_up=1.0,
            travel_down=1.0,
            area_id="living_room",
            repeats=2,
            coalesce_window_ms=20,
        ),
    )
    second = await attach_cover(
        hass,
        hub,
        config=BlindConfig(
            name="Living Room channel 2",
            remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
            channels=(2,),
            travel_up=2.0,
            travel_down=2.0,
            area_id="living_room",
            repeats=2,
            coalesce_window_ms=20,
        ),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_2",
    )
    try:
        commands = asyncio.gather(first.async_open_cover(), second.async_open_cover())
        await accepted.wait()

        assert len(bodies) == 1
        assert bodies[0]["raw"] == encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        assert not first.is_opening
        assert not second.is_opening

        allow_start.set()
        await commands

        assert first.is_opening
        assert second.is_opening
        assert first.extra_state_attributes["motion_command_id"] == bodies[0]["command_id"]
        assert second.extra_state_attributes["motion_command_id"] == bodies[0]["command_id"]
        assert first._motion_duration == 2.0
        assert second._motion_duration == 3.0
    finally:
        await first.async_will_remove_from_hass()
        await second.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_new_cover_is_unknown_and_commits_motion_only_after_rf_start(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission changes no position; first RF dispatch starts full calibration."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    accepted = asyncio.Event()
    allow_start = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        bridge_id = topic.split("/")[1]
        assert hub.handle_status(
            bridge_id,
            {"status": "accepted", "command_id": body["command_id"]},
        )
        accepted.set()
        await allow_start.wait()
        assert hub.handle_status(
            bridge_id,
            {"status": "started", "command_id": body["command_id"]},
        )

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub)
    try:
        assert entity.current_cover_position is None
        assert entity.is_closed is None

        command = asyncio.create_task(entity.async_open_cover())
        await accepted.wait()
        assert entity.current_cover_position is None
        assert not entity.is_opening

        allow_start.set()
        await command
        assert entity.is_opening
        assert entity.current_cover_position is None

        await asyncio.sleep(0.06)
        assert entity.current_cover_position == 100
        assert not entity.is_opening
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_ack_timeout_marks_position_unknown_and_degraded(hass: HomeAssistant) -> None:
    """A command with ambiguous bridge receipt cannot preserve or anchor an estimate."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish, ack_timeout=0.001)
    entity = await attach_cover(hass, hub)
    entity._position = 50.0
    try:
        with pytest.raises(HomeAssistantError, match="acknowledgement"):
            await entity.async_open_cover()

        assert entity.current_cover_position is None
        assert entity.extra_state_attributes["degraded_bridge"] is True
        assert not entity.is_opening
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.parametrize("timeout_phase", ("ack", "started"))
@pytest.mark.asyncio
async def test_group_timeout_marks_and_notifies_member_covers_unknown(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    timeout_phase: str,
) -> None:
    """Ambiguous group receipt invalidates every registered addressed channel."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if timeout_phase == "started":
            body: dict[str, Any] = json.loads(payload)
            assert hub.handle_status(
                topic.split("/")[1],
                {"status": "accepted", "command_id": body["command_id"]},
            )

    hub = ZemismartHub(
        online_registry(),
        publish,
        ack_timeout=0.001,
        started_timeout=0.001,
    )
    group = await attach_cover(hass, hub)
    member_config = BlindConfig(
        name="Living Room channel 1",
        remote=group._config.remote,
        channels=(1,),
        travel_up=1.0,
        travel_down=1.0,
        area_id="living_room",
        repeats=2,
    )
    member = ZemismartCover("entry-2", EntryRuntime(member_config, hub))
    member.hass = hass
    member.entity_id = "cover.living_room_channel_1"
    member.platform = platform_stub()
    await member.async_internal_added_to_hass()
    await member.async_added_to_hass()
    group._position = 50.0
    member._position = 60.0
    writes: list[str] = []
    monkeypatch.setattr(
        ZemismartCover,
        "async_write_ha_state",
        lambda entity: writes.append(entity.entity_id),
    )
    try:
        with pytest.raises(HomeAssistantError, match="timed out"):
            await group.async_open_cover()

        assert group.current_cover_position is None
        assert member.current_cover_position is None
        assert set(writes) == {"cover.living_room_left", "cover.living_room_channel_1"}
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_publish_failure_preserves_prior_motion_tracking(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed replacement leaves the acknowledged prior motion model running."""
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    calls = 0
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        nonlocal calls
        calls += 1
        body: dict[str, Any] = json.loads(payload)
        if calls == 2:
            msg = "broker down"
            raise OSError(msg)
        acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.5))
    entity._position = 20.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 80})
        await asyncio.sleep(0.01)
        before = entity.current_cover_position

        with pytest.raises(HomeAssistantError, match="broker down"):
            await entity.async_stop_cover()

        assert entity.is_opening
        await asyncio.sleep(0.02)
        assert entity.current_cover_position is not None
        assert before is not None
        assert entity.current_cover_position > before
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_set_position_at_current_while_moving_sends_stop(hass: HomeAssistant) -> None:
    """An apparent no-op cannot silently freeze a motor that is still moving."""
    bodies: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        bodies.append(body)
        acknowledge(hub, topic.split("/")[1], body)

    config = cover_config(travel=1.0)
    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=config)
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 80})
        assert entity.is_opening
        current = entity.current_cover_position
        assert current is not None

        await entity.async_set_cover_position(**{ATTR_POSITION: current})

        assert not entity.is_opening
        assert bodies[-1].keys() == {"command_id", "target", "raw", "repeats"}
        assert bodies[-1]["raw"] == encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "STOP", bases=TEST_ACTION_BASES)
        )
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_cover_commands_are_serialized_until_each_start(hass: HomeAssistant) -> None:
    """Concurrent HA service calls cannot interleave one cover's commit snapshots."""
    bodies: list[dict[str, Any]] = []
    release_first = asyncio.Event()
    first_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        bodies.append(body)
        if len(bodies) == 1:
            first_published.set()
            await release_first.wait()
        acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub)
    entity._position = 50.0
    try:
        opening = asyncio.create_task(entity.async_open_cover())
        await first_published.wait()
        closing = asyncio.create_task(entity.async_close_cover())
        await asyncio.sleep(0)
        assert len(bodies) == 1

        release_first.set()
        await asyncio.gather(opening, closing)

        assert len(bodies) == 2
        assert entity.is_closing
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_group_motion_updates_each_member_channel_estimate(hass: HomeAssistant) -> None:
    """A group command advances the individual covers backed by its physical channels."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    member_config = BlindConfig(
        name="Living Room channel 1",
        remote=group._config.remote,
        channels=(1,),
        travel_up=1.0,
        travel_down=1.0,
        area_id="living_room",
        repeats=2,
    )
    member = ZemismartCover("entry-2", EntryRuntime(member_config, hub))
    member.hass = hass
    member.entity_id = "cover.living_room_channel_1"
    member.platform = platform_stub()
    await member.async_internal_added_to_hass()
    await member.async_added_to_hass()
    group._position = 20.0
    member._position = 50.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.02)

        assert group.current_cover_position is not None
        assert member.is_opening
        # The RF frame moves the member for the group's duration from the
        # member's OWN estimate: +40% of full travel on top of 50, capped 100.
        assert member._motion_target == pytest.approx(90.0, abs=0.01)
        assert member._motion_start_position == 50.0
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_group_motion_marks_unknown_member_unknown(hass: HomeAssistant) -> None:
    """A member with no estimate becomes unknown when its group moves partially."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    member_config = BlindConfig(
        name="Living Room channel 1",
        remote=group._config.remote,
        channels=(1,),
        travel_up=1.0,
        travel_down=1.0,
        area_id="living_room",
        repeats=2,
    )
    member = ZemismartCover("entry-2", EntryRuntime(member_config, hub))
    member.hass = hass
    member.entity_id = "cover.living_room_channel_1"
    member.platform = platform_stub()
    await member.async_internal_added_to_hass()
    await member.async_added_to_hass()
    group._position = 20.0
    member._position = None
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.02)

        # The member physically moved with the group but from an unknown
        # origin — only an unknown estimate is honest.
        assert member.current_cover_position is None
        assert not member.is_opening
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_full_calibration_uses_configured_travel_not_drifted_estimate(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A near-end estimate still needs one complete configured run plus margin to anchor."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.04))
    entity._position = 99.0
    try:
        await entity.async_open_cover()
        await asyncio.sleep(0.015)
        assert entity.is_opening

        await asyncio.sleep(0.05)
        assert entity.current_cover_position == 100
        assert not entity.is_opening
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_restart_recovers_started_motion(hass: HomeAssistant) -> None:
    """Complete persisted RF-start metadata resumes local travel tracking."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    await original.async_will_remove_from_hass()
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    restored = await attach_cover(
        hass,
        hub,
        config=cover_config(travel=1.0),
        cover_type=RestoredCover,
    )
    try:
        assert restored.is_opening
        assert restored.current_cover_position is not None
        assert (
            restored.extra_state_attributes["motion_command_id"] == attributes["motion_command_id"]
        )
        assert restored.extra_state_attributes["motion_bridge"] == "bridge-a"
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_incomplete_restart_motion_becomes_unknown(hass: HomeAssistant) -> None:
    """A direction without the correlated deadline/bridge/ID is not trustworthy recovery."""
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 50,
            "motion_direction": 1,
            "remote": cover_config().remote_key,
            "channels": list(cover_config().channels),
        },
    )

    class IncompleteCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    async def publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(online_registry(), publish),
        cover_type=IncompleteCover,
    )
    try:
        assert entity.current_cover_position is None
        assert entity.extra_state_attributes["degraded_bridge"] is True
    finally:
        await entity.async_will_remove_from_hass()


def test_current_position_getter_is_pure() -> None:
    """Reading state never integrates elapsed time or mutates the estimator."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    entity = ZemismartCover(
        "entry-1",
        EntryRuntime(cover_config(), ZemismartHub(online_registry(), publish)),
    )
    entity._position = 40.0
    entity._direction = 1
    entity._motion_start_position = 40.0
    entity._motion_target = 80.0
    entity._motion_started = 0.0
    entity._motion_duration = 10.0

    assert entity.current_cover_position == 40
    assert entity._position == 40.0


@pytest.mark.asyncio
async def test_displaced_timed_motion_freezes_at_current_estimate(hass: HomeAssistant) -> None:
    """A displaced timed move freezes: its fail-safe STOP is flushed on air."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 0.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 60})
        assert entity._motion_timed
        command_id = entity._motion_command_id
        assert command_id is not None

        assert hub.handle_status("bridge-a", {"status": "displaced", "command_id": command_id})

        assert entity._direction == 0
        position = entity.current_cover_position
        assert position is not None
        assert position < 60
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_displaced_full_travel_rides_to_endpoint(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A displaced full travel keeps its model: the motor runs to its limit."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.05))
    entity._position = 50.0
    try:
        await entity.async_open_cover()
        assert not entity._motion_timed
        command_id = entity._motion_command_id
        assert command_id is not None

        assert hub.handle_status("bridge-a", {"status": "displaced", "command_id": command_id})

        # The model keeps running to its endpoint on the motor's own limit.
        assert entity._direction == 1
        await asyncio.sleep(0.15)
        assert entity.current_cover_position == 100
    finally:
        await entity.async_will_remove_from_hass()


def member_config(*, channel: int = 1, travel: float = 1.0) -> BlindConfig:
    """Return one single-channel member configuration on the shared remote."""
    return BlindConfig(
        name=f"Living Room channel {channel}",
        remote=cover_config().remote,
        channels=(channel,),
        travel_up=travel,
        travel_down=travel,
        area_id="living_room",
        repeats=2,
    )


def channel_group_config(
    channels: tuple[int, ...],
    *,
    travel: float = 5.0,
) -> BlindConfig:
    """Return a synthetic group configuration for an arbitrary channel set."""
    return BlindConfig(
        name=f"Living Room channels {channels}",
        remote=cover_config().remote,
        channels=channels,
        travel_up=travel,
        travel_down=travel,
        area_id="living_room",
        repeats=2,
    )


async def attach_channel_groups(
    hass: HomeAssistant,
    hub: ZemismartHub,
    channels_in_order: tuple[tuple[int, ...], tuple[int, ...]],
) -> dict[tuple[int, ...], ZemismartCover]:
    """Attach two groups in an explicit RX-listener registration order."""
    covers: dict[tuple[int, ...], ZemismartCover] = {}
    for index, channels in enumerate(channels_in_order, start=1):
        covers[channels] = await attach_cover(
            hass,
            hub,
            config=channel_group_config(channels),
            entry_id=f"group-entry-{index}",
            entity_id=f"cover.synthetic_group_{index}",
        )
    return covers


@pytest.mark.asyncio
async def test_heard_up_starts_exact_cover_without_routing_or_publish(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A physical UP mirrors travel without pretending the hearing bridge transmitted."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    published: list[tuple[str, str]] = []

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.04))
    entity._position = 20.0
    entity._last_bridge = "prior-synthetic-bridge"
    entity._degraded = False
    try:
        before = entity.current_cover_position
        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )

        assert entity.is_opening
        assert published == []
        assert entity.extra_state_attributes["last_bridge"] == "prior-synthetic-bridge"
        assert entity.extra_state_attributes["degraded_bridge"] is False

        await asyncio.sleep(0.02)
        assert before is not None
        assert entity.current_cover_position is not None
        assert entity.current_cover_position > before
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_heard_up_supersedes_timed_down_awaiting_started(
    hass: HomeAssistant,
) -> None:
    """A delayed commanded ack cannot overwrite a newer physical press."""
    published: list[dict[str, Any]] = []
    admitted = asyncio.Event()
    disarm_published = asyncio.Event()
    allow_started = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        bridge_id = topic.split("/")[1]
        if topic.endswith("/cmd"):
            assert hub.handle_status(
                bridge_id,
                {"status": "disarmed", "command_id": body["command_id"]},
            )
            disarm_published.set()
            return
        assert hub.handle_status(
            bridge_id,
            {"status": "accepted", "command_id": body["command_id"]},
        )
        admitted.set()
        await allow_started.wait()
        assert not hub.handle_status(
            bridge_id,
            {"status": "started", "command_id": body["command_id"]},
        )

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 80.0
    command = asyncio.create_task(
        entity.async_set_cover_position(**{ATTR_POSITION: 20}),
    )
    try:
        await admitted.wait()
        assert entity._intent_generation == 0

        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        assert entity._intent_generation == 1
        assert entity.is_opening
        assert entity._motion_command_id is None

        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)
        allow_started.set()
        await command

        assert len(published) == 2
        assert published[1] == {
            "action": "disarm",
            "command_id": published[0]["command_id"],
        }
        assert entity.is_opening
        assert entity._motion_target == 100.0
        assert entity._motion_command_id is None
    finally:
        if not command.done():
            command.cancel()
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_opposite_press_disarms_started_full_move(
    hass: HomeAssistant,
) -> None:
    """A physical reversal aborts an untimed move's remaining action repeats."""
    published: list[tuple[str, dict[str, Any]]] = []
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "full-down",
    )
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 80.0
    try:
        await entity.async_close_cover()
        assert not entity._motion_timed
        assert entity._motion_command_id == "full-down"

        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)

        assert [item for item in published if item[0].endswith("/cmd")] == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": "full-down"},
            )
        ]
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "full-down"},
        )
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_group_move_models_members_once_without_double_invalidation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact group owns its member propagation for one heard-event batch."""
    published: list[tuple[str, str]] = []

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    first = await attach_cover(
        hass,
        hub,
        config=member_config(channel=1, travel=5.0),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    second = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2, travel=5.0),
        entry_id="entry-3",
        entity_id="cover.living_room_channel_2",
    )
    group._position = 20.0
    first._position = 30.0
    second._position = 40.0
    writes: list[str] = []
    monkeypatch.setattr(
        ZemismartCover,
        "async_write_ha_state",
        lambda entity: writes.append(entity.entity_id),
    )
    try:
        dispatch_heard_press(
            hub,
            group._config,
            "UP",
            group._config.channels,
            at=cover_module.WALL_CLOCK(),
        )

        assert group.is_opening
        assert first.is_opening
        assert second.is_opening
        assert sorted(writes) == sorted(
            [
                "cover.living_room_left",
                "cover.living_room_channel_1",
                "cover.living_room_channel_2",
            ],
        )
        assert published == []
    finally:
        await group.async_will_remove_from_hass()
        await first.async_will_remove_from_hass()
        await second.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_partially_addressed_heard_press_marks_group_unknown(
    hass: HomeAssistant,
) -> None:
    """A one-channel press cannot preserve a two-channel aggregate estimate."""
    published: list[tuple[str, str]] = []

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    group._position = 50.0
    try:
        dispatch_heard_press(
            hub,
            group._config,
            "UP",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )

        assert group.current_cover_position is None
        assert not group.is_opening
        assert group.extra_state_attributes["degraded_bridge"] is True
        assert published == []
    finally:
        await group.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_heard_group_prepare_disarms_member_timed_command_first(
    hass: HomeAssistant,
) -> None:
    """A group-first callback cannot erase its member's timed disarm snapshot."""
    published: list[tuple[str, dict[str, Any]]] = []
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "command-c",
    )
    group = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    member = await attach_cover(
        hass,
        hub,
        config=member_config(channel=1, travel=5.0),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    member._position = 20.0
    try:
        await member.async_set_cover_position(**{ATTR_POSITION: 60})
        assert member._motion_timed

        dispatch_heard_press(
            hub,
            group._config,
            "UP",
            group._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)

        assert [item for item in published if item[0].endswith("/cmd")] == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": "command-c"},
            )
        ]
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "command-c"},
        )
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_partial_heard_press_disarms_timed_command_before_unknown(
    hass: HomeAssistant,
) -> None:
    """A partially driven timed cover disarms its stale fail-safe STOP."""
    published: list[tuple[str, dict[str, Any]]] = []
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "command-d",
    )
    cover = await attach_cover(
        hass,
        hub,
        config=channel_group_config((1, 3)),
    )
    cover._position = 20.0
    try:
        await cover.async_set_cover_position(**{ATTR_POSITION: 60})
        assert cover._motion_timed

        dispatch_heard_press(
            hub,
            cover._config,
            "UP",
            (1, 2),
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)

        assert cover.current_cover_position is None
        assert [item for item in published if item[0].endswith("/cmd")] == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": "command-d"},
            )
        ]
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "command-d"},
        )
    finally:
        await cover.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_partial_heard_takeover_invalidates_unpressed_bound_member(
    hass: HomeAssistant,
) -> None:
    """A whole-command abort makes an unpressed bound member honestly unknown."""
    published: list[tuple[str, dict[str, Any]]] = []
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "group-timed",
    )
    group = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    first = await attach_cover(
        hass,
        hub,
        config=member_config(channel=1, travel=5.0),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    second = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2, travel=5.0),
        entry_id="entry-3",
        entity_id="cover.living_room_channel_2",
    )
    unbound = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2, travel=5.0),
        entry_id="entry-4",
        entity_id="cover.unbound_channel_2",
    )
    for cover in (group, first, second, unbound):
        cover._position = 20.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 40})
        assert second._motion_command_id == "group-timed"
        unbound._position = 65.0
        unbound._clear_motion()
        unbound._degraded = False

        dispatch_heard_press(
            hub,
            group._config,
            "UP",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)

        assert first.is_opening
        assert group.current_cover_position is None
        assert second.current_cover_position is None
        assert unbound.current_cover_position == 65
        assert unbound.extra_state_attributes["degraded_bridge"] is False
        assert [item for item in published if item[0].endswith("/cmd")] == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": "group-timed"},
            )
        ]
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "group-timed"},
        )
    finally:
        await group.async_will_remove_from_hass()
        await first.async_will_remove_from_hass()
        await second.async_will_remove_from_hass()
        await unbound.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_partial_heard_stop_preserves_confirmed_timed_group_stop(
    hass: HomeAssistant,
) -> None:
    """A partial physical STOP keeps the confirmed group's scheduled STOP."""
    published: list[tuple[str, dict[str, Any]]] = []
    barrier_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        elif body["command_id"] == "publish-barrier":
            assert hub.handle_status(
                "bridge-a",
                {"status": "disarmed", "command_id": "publish-barrier"},
            )
            barrier_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "command-stop",
    )
    cover = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    cover._position = 20.0
    try:
        await cover.async_set_cover_position(**{ATTR_POSITION: 60})
        assert cover._motion_timed

        dispatch_heard_press(
            hub,
            cover._config,
            "STOP",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        hub.request_disarm(
            "bridge-a",
            "publish-barrier",
            hub._now() + 1.0,
            lambda: None,
        )
        await asyncio.wait_for(barrier_published.wait(), timeout=1.0)

        assert cover.current_cover_position is None
        assert [item for item in published if item[0].endswith("/cmd")] == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": "publish-barrier"},
            )
        ]
    finally:
        await cover.async_will_remove_from_hass()
        hub.close()


@pytest.mark.parametrize(
    "channels_in_order",
    [
        ((1, 2, 3), (1, 2)),
        ((1, 2), (1, 2, 3)),
    ],
    ids=["larger-first", "smaller-first"],
)
@pytest.mark.asyncio
async def test_heard_up_preserves_every_fully_contained_group(
    hass: HomeAssistant,
    channels_in_order: tuple[tuple[int, ...], tuple[int, ...]],
) -> None:
    """Contained covers in one heard batch cannot invalidate one another."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    covers = await attach_channel_groups(hass, hub, channels_in_order)
    for index, cover in enumerate(covers.values(), start=2):
        cover._position = float(index * 10)
    try:
        dispatch_heard_press(
            hub,
            covers[(1, 2, 3)]._config,
            "UP",
            (1, 2, 3),
            at=cover_module.WALL_CLOCK(),
        )

        assert all(cover.is_opening for cover in covers.values())
        assert all(cover.current_cover_position is not None for cover in covers.values())
    finally:
        for cover in covers.values():
            await cover.async_will_remove_from_hass()
        hub.close()


@pytest.mark.parametrize(
    "channels_in_order",
    [
        ((1, 2, 3), (1, 2)),
        ((1, 2), (1, 2, 3)),
    ],
    ids=["larger-first", "smaller-first"],
)
@pytest.mark.asyncio
async def test_heard_stop_freezes_every_fully_contained_group(
    hass: HomeAssistant,
    channels_in_order: tuple[tuple[int, ...], tuple[int, ...]],
) -> None:
    """Each contained cover freezes its own estimate without batch invalidation."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    covers = await attach_channel_groups(hass, hub, channels_in_order)
    started_at = cover_module.WALL_CLOCK()
    motion = cover_module._MotionStart(
        source="heard",
        started_at=started_at,
        deadline=None,
        bridge_id=None,
        command_id=None,
    )
    for index, cover in enumerate(covers.values(), start=2):
        cover._position = float(index * 10)
        cover._commit_motion(
            motion,
            direction=1,
            target=100.0,
            duration=5.0,
            absolute_anchor=True,
        )
    try:
        dispatch_heard_press(
            hub,
            covers[(1, 2, 3)]._config,
            "STOP",
            (1, 2, 3),
            at=started_at + 0.01,
        )

        assert all(not cover.is_opening for cover in covers.values())
        assert all(cover.current_cover_position is not None for cover in covers.values())
    finally:
        for cover in covers.values():
            await cover.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_stop_reconciles_offline_unverified_anchor(
    hass: HomeAssistant,
) -> None:
    """A heard STOP revokes the origin exposed by stopping exempt full travel."""
    registry = online_registry("bridge-b")
    published: list[tuple[str, str]] = []

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, payload))

    hub = ZemismartHub(registry, publish)
    entity = await attach_cover(
        hass,
        hub,
        config=cover_config(travel=5.0),
        cover_type=restored_cover_type(stopped_unverified_anchor_state()),
    )
    heard_at = cover_module.WALL_CLOCK()
    try:
        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=heard_at,
        )
        assert entity.is_opening

        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()
        assert entity.is_opening
        assert entity.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"

        dispatch_heard_press(
            hub,
            entity._config,
            "STOP",
            entity._config.channels,
            at=heard_at + 0.25,
        )

        assert entity.current_cover_position is None
        assert not entity.is_opening
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
        assert published == []
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_emission_proof_upgrades_only_the_exact_command_anchor(
    hass: HomeAssistant,
) -> None:
    """Proof for C cannot clear another cover's restored command-D marker."""
    first_config = member_config(channel=1)
    second_config = member_config(channel=2)

    def restored_state(
        config: BlindConfig,
        *,
        bridge_id: str,
        command_id: str,
    ) -> State:
        return State(
            "cover.synthetic",
            "open",
            {
                ATTR_CURRENT_POSITION: 80,
                "remote": config.remote_key,
                "channels": list(config.channels),
                "motion_direction": 0,
                "unverified_anchor_bridge": bridge_id,
                "unverified_anchor_command_id": command_id,
            },
        )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    registry = BridgeRegistry()
    hub = ZemismartHub(registry, quiet_publish)
    first = await attach_cover(
        hass,
        hub,
        config=first_config,
        cover_type=restored_cover_type(
            restored_state(
                first_config,
                bridge_id="bridge-c",
                command_id="command-c",
            )
        ),
    )
    second = await attach_cover(
        hass,
        hub,
        config=second_config,
        cover_type=restored_cover_type(
            restored_state(
                second_config,
                bridge_id="bridge-d",
                command_id="command-d",
            )
        ),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_2",
    )
    try:
        hub._record_emission_proof("command-c")

        assert first.extra_state_attributes["unverified_anchor_bridge"] is None
        assert first.extra_state_attributes["unverified_anchor_command_id"] is None
        assert second.extra_state_attributes["unverified_anchor_bridge"] == "bridge-d"
        assert second.extra_state_attributes["unverified_anchor_command_id"] == "command-d"

        registry.update_availability("bridge-d", "offline")
        hub.notify_bridge_change()

        assert first.current_cover_position == 80
        assert second.current_cover_position is None
    finally:
        await first.async_will_remove_from_hass()
        await second.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_emission_proof_before_restore_marker_commit_is_replayed(
    hass: HomeAssistant,
) -> None:
    """Bounded proof memory upgrades a marker committed after the peer echo."""
    config = cover_config()
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 50,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": 1,
            "motion_target": 80,
            "motion_started": now - 2.0,
            "motion_deadline": now - 1.0,
            "motion_start_position": 50,
            "motion_bridge": "bridge-a",
            "motion_command_id": "command-c",
            "motion_timed": True,
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), quiet_publish)
    hub._record_emission_proof("command-c")
    entity = await attach_cover(
        hass,
        hub,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert hub.was_emission_proven("command-c")
        assert entity.current_cover_position == 80
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
        assert entity.extra_state_attributes["unverified_anchor_command_id"] is None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_up_disarm_ack_keeps_mirrored_motion(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An acknowledged takeover disarms the old STOP and keeps modeling UP."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    published: list[tuple[str, dict[str, Any]]] = []
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.4))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 75})
        command_id = entity._motion_command_id
        old_deadline = entity._motion_deadline
        assert command_id is not None
        assert entity._motion_timed

        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)

        disarms = [item for item in published if item[0].endswith("/cmd")]
        assert disarms == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": command_id},
            )
        ]
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": command_id},
        )

        await asyncio.sleep(max(0.0, old_deadline - cover_module.WALL_CLOCK()) + 0.02)

        assert entity.is_opening
        assert entity._motion_target == 100.0
        assert entity.current_cover_position is not None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_up_disarm_timeout_marks_mirrored_motion_unknown(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a disarm ack, the old STOP deadline invalidates the mirror."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    published: list[tuple[str, dict[str, Any]]] = []
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.4))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 75})
        command_id = entity._motion_command_id
        old_deadline = entity._motion_deadline
        assert command_id is not None

        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)
        await asyncio.sleep(max(0.0, old_deadline - cover_module.WALL_CLOCK()) + 0.02)

        assert [item for item in published if item[0].endswith("/cmd")]
        assert entity.current_cover_position is None
        assert not entity.is_opening
        assert entity.extra_state_attributes["degraded_bridge"] is True
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_transmitted_stop_still_publishes_records_ack_and_freezes(
    hass: HomeAssistant,
) -> None:
    """Extracting the freeze helper preserves the ordinary transmitted STOP path."""
    bodies: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        bodies.append(body)
        acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 20.0
    try:
        await entity.async_open_cover()
        stopped = await entity._async_stop()

        assert stopped is True
        assert len(bodies) == 2
        assert bodies[-1]["raw"] == encode_b0(
            make_payload(
                TEST_PREFIX,
                TEST_REMOTE_ID,
                entity._config.channels,
                "STOP",
                bases=TEST_ACTION_BASES,
            ),
        )
        assert entity.current_cover_position is not None
        assert not entity.is_opening
        assert entity.extra_state_attributes["last_bridge"] == "bridge-a"
        assert entity.extra_state_attributes["degraded_bridge"] is False
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_raced_group_displacement_stops_members_and_reconciles_overlaps(
    hass: HomeAssistant,
) -> None:
    """A timed group command displaced after STARTED still stops every channel model."""
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        bridge_id = topic.split("/")[1]
        acknowledge(hub, bridge_id, body)
        if len(published) == 2:
            assert hub.handle_status(
                bridge_id,
                {"status": "displaced", "command_id": body["command_id"]},
            )

    hub = ZemismartHub(online_registry(), publish)
    member = await attach_cover(hass, hub, config=member_config(travel=5.0))
    member._position = 20.0
    await member.async_open_cover()
    assert member.is_opening

    group = await attach_cover(
        hass,
        hub,
        config=cover_config(travel=5.0),
        entry_id="entry-2",
        entity_id="cover.living_room_group",
    )
    overlap_config = BlindConfig(
        name="Living Room overlap",
        remote=cover_config().remote,
        channels=(2, 3),
        travel_up=5.0,
        travel_down=5.0,
        area_id="living_room",
        repeats=2,
    )
    overlap = await attach_cover(
        hass,
        hub,
        config=overlap_config,
        entry_id="entry-3",
        entity_id="cover.living_room_overlap",
    )
    group._position = 20.0
    overlap._position = 40.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})

        assert not group.is_opening
        assert not member.is_opening
        assert overlap.current_cover_position is None
    finally:
        await member.async_will_remove_from_hass()
        await group.async_will_remove_from_hass()
        await overlap.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_displaced_user_stop_still_freezes_group_members(hass: HomeAssistant) -> None:
    """A user STOP displaced after STARTED still freezes the group and its members.

    A full-travel group member is untimed, so the timed-only _on_displaced never
    freezes it; before the round-14 fix _async_stop returned on displacement
    BEFORE the freeze, leaving a non-re-driven member tracking a stale full travel.
    """
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        bridge_id = topic.split("/")[1]
        acknowledge(hub, bridge_id, body)
        # Displace the STOP (the 2nd published frame) right after it starts.
        if len(published) == 2:
            hub.handle_status(
                bridge_id,
                {"status": "displaced", "command_id": body["command_id"]},
            )

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    member = await attach_cover(
        hass,
        hub,
        config=member_config(),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    group._position = 20.0
    member._position = 20.0
    try:
        await group.async_open_cover()
        assert group.is_opening
        assert member.is_opening

        await group.async_stop_cover()

        assert not group.is_opening
        assert not member.is_opening
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_member_stop_marks_moving_containing_group_unknown(hass: HomeAssistant) -> None:
    """Stopping one member channel invalidates its moving group's aggregate.

    The STOP frame halts only that channel's motor; the group's remaining
    members keep moving, so the group estimate no longer describes anything
    physical. The stopped member itself freezes at its own estimate.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    member = await attach_cover(
        hass,
        hub,
        config=member_config(),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    group._position = 20.0
    member._position = 50.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.02)
        assert group.is_opening

        await member.async_stop_cover()

        assert group.current_cover_position is None
        assert not group.is_opening
        assert member.current_cover_position is not None
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_overlapping_group_movement_marks_other_group_unknown(
    hass: HomeAssistant,
) -> None:
    """Driving group {2,3} while group {1,2} moves invalidates {1,2}.

    Channel 2 physically follows the newer frame, so the older group's
    aggregate estimate no longer describes its members.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group_a = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    config_b = BlindConfig(
        name="Living Room Right",
        remote=cover_config().remote,
        channels=(2, 3),
        travel_up=1.0,
        travel_down=1.0,
        area_id="living_room",
        repeats=2,
    )
    group_b = await attach_cover(
        hass,
        hub,
        config=config_b,
        entry_id="entry-3",
        entity_id="cover.living_room_right",
    )
    group_a._position = 20.0
    try:
        await group_a.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.02)
        assert group_a.is_opening
        # group_a's own start already invalidated the idle overlapping group.
        assert group_b.current_cover_position is None

        await group_b.async_open_cover()
        await asyncio.sleep(0.02)

        assert group_a.current_cover_position is None
        assert not group_a.is_opening
        assert group_b.is_opening
    finally:
        await group_a.async_will_remove_from_hass()
        await group_b.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_group_endpoint_models_member_over_its_own_calibration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group full travel models each member over the member's OWN travel time.

    A fast member reaches its limit switch long before a slow group duration;
    inheriting the group's duration would report it moving long after it
    physically stopped (and let a follow-up set_position compute from a stale
    mid-travel estimate).
    """
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    member = await attach_cover(
        hass,
        hub,
        config=member_config(travel=0.1),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    group._position = 50.0
    member._position = 50.0
    try:
        await group.async_open_cover()
        await asyncio.sleep(0.02)

        assert member._motion_duration == pytest.approx(0.11)
        assert group._motion_duration == pytest.approx(1.01)
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_restored_timed_motion_with_offline_bridge_becomes_unknown(
    hass: HomeAssistant,
) -> None:
    """A restored timed motion whose bridge is known offline is not trusted.

    The bridge holding the armed fail-safe STOP keeps it in RAM only; if it
    is offline when HA comes back, the STOP may be lost and the motor may
    have run to its limit. Only unknown is honest.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=10.0))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    assert attributes["motion_timed"] is True
    await original.async_will_remove_from_hass()
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    offline_registry = BridgeRegistry()
    offline_registry.update_info("bridge-a", {"area": "living_room"})
    offline_registry.update_availability("bridge-a", "offline")
    restored = await attach_cover(
        hass,
        ZemismartHub(offline_registry, quiet_publish),
        config=cover_config(travel=10.0),
        cover_type=RestoredCover,
    )
    try:
        assert restored.current_cover_position is None
        assert not restored.is_opening
        assert restored.extra_state_attributes["degraded_bridge"] is True
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_restored_timed_motion_with_undiscovered_bridge_keeps_tracking(
    hass: HomeAssistant,
) -> None:
    """A motion bridge that merely has not announced yet is not offline.

    During startup, retained availability arrives in arbitrary order; only an
    EXPLICIT offline report for the motion's own bridge invalidates restored
    tracking.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=10.0))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    await original.async_will_remove_from_hass()
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    partial_registry = BridgeRegistry()
    partial_registry.update_info("bridge-z", {"area": "other_room"})
    partial_registry.update_availability("bridge-z", "online")
    restored = await attach_cover(
        hass,
        ZemismartHub(partial_registry, quiet_publish),
        config=cover_config(travel=10.0),
        cover_type=RestoredCover,
    )
    try:
        assert restored.is_opening
        assert restored.current_cover_position is not None
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.parametrize("deadline_offset", [-1.0, 10.0])
@pytest.mark.asyncio
async def test_restore_rejects_recently_displaced_timed_motion(
    hass: HomeAssistant,
    deadline_offset: float,
) -> None:
    """A displaced status received while last state loads cannot be lost."""
    config = cover_config(travel=10.0)
    now = cover_module.WALL_CLOCK()
    command_id = "restored-displaced-command"
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 50,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": 1,
            "motion_target": 80,
            "motion_started": now - 1.0,
            "motion_deadline": now + deadline_offset,
            "motion_start_position": 50,
            "motion_bridge": "bridge-a",
            "motion_command_id": command_id,
            "motion_timed": True,
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)

    class DisplacedDuringRestore(ZemismartCover):
        async def async_get_last_state(self) -> State:
            assert hub.handle_status(
                "bridge-a",
                {"status": "displaced", "command_id": command_id},
            )
            return restored_state

    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=DisplacedDuringRestore,
    )
    try:
        assert entity.current_cover_position is None
        assert not entity.is_opening
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_clamped_member_models_only_its_physical_distance(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member clamped at an endpoint stops modeling at its limit switch.

    Member at 90 in a group moving +40%: the member physically arrives at 100
    after 10% of its own travel, not after the group's full frame duration.
    """
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    member = await attach_cover(
        hass,
        hub,
        config=member_config(travel=1.0),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    group._position = 20.0
    member._position = 90.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.02)

        assert member._motion_target == 100.0
        assert member._motion_duration == pytest.approx(0.11, abs=0.001)
        assert group._motion_duration == pytest.approx(0.4, abs=0.01)
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_single_channel_timeout_invalidates_containing_group(
    hass: HomeAssistant,
) -> None:
    """An ambiguous single-channel timeout may have moved a group member.

    The frame MAY have reached RF, so a containing group's aggregate estimate
    can no longer be trusted either.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish, ack_timeout=0.001)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    single = await attach_cover(
        hass,
        hub,
        config=member_config(),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    group._position = 40.0
    single._position = 40.0
    try:
        with pytest.raises(HomeAssistantError, match="acknowledgement"):
            await single.async_open_cover()

        assert single.current_cover_position is None
        assert group.current_cover_position is None
        assert group.extra_state_attributes["degraded_bridge"] is True
    finally:
        await group.async_will_remove_from_hass()
        await single.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_clamped_member_deadline_matches_its_own_duration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clamped member's model ENDS at its own arrival, not the group deadline.

    The duration rescale is meaningless if the group's later bridge-armed
    deadline still drives the member's completion: the member would report
    opening long after its limit switch.
    """
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    member = await attach_cover(
        hass,
        hub,
        config=member_config(travel=1.0),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    group._position = 20.0
    member._position = 90.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.02)

        assert member._motion_deadline == pytest.approx(
            member._motion_started + member._motion_duration
        )
        assert member._motion_deadline < group._motion_deadline
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_member_already_at_endpoint_never_blips_during_group_travel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member sitting at 100 stays at 100 while its group runs a full open."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    group = await attach_cover(hass, hub, config=cover_config(travel=0.5))
    member = await attach_cover(
        hass,
        hub,
        config=member_config(travel=0.5),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_1",
    )
    group._position = 50.0
    member._position = 100.0
    try:
        await group.async_open_cover()
        await asyncio.sleep(0.02)

        assert member.current_cover_position == 100
    finally:
        await group.async_will_remove_from_hass()
        await member.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_restore_anchor_is_revoked_by_late_offline_availability(
    hass: HomeAssistant,
) -> None:
    """A late retained offline report invalidates a trusting expired anchor.

    On a cold restart the registry is empty when the entity restores; the
    expired timed motion anchors at its target, but the anchor is remembered
    as unverified. When the motion bridge's retained availability finally
    arrives saying offline, the STOP may never have fired: only unknown is
    honest. An online report instead confirms the anchor.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    assert attributes["motion_timed"] is True
    await original.async_will_remove_from_hass()
    await asyncio.sleep(0.05)  # let the persisted deadline expire
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    empty_registry = BridgeRegistry()
    restored_hub = ZemismartHub(empty_registry, quiet_publish)
    restored = await attach_cover(
        hass,
        restored_hub,
        config=cover_config(travel=0.02),
        cover_type=RestoredCover,
    )
    try:
        # Anchored at target while the bridge is merely undiscovered.
        assert restored.current_cover_position == 80

        empty_registry.update_availability("bridge-a", "offline")
        restored_hub.notify_bridge_change()

        assert restored.current_cover_position is None
        assert restored.extra_state_attributes["degraded_bridge"] is True
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_restore_anchor_is_confirmed_by_late_online_availability(
    hass: HomeAssistant,
) -> None:
    """An online report clears the unverified marker and keeps the anchor."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    await original.async_will_remove_from_hass()
    await asyncio.sleep(0.05)
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    empty_registry = BridgeRegistry()
    restored_hub = ZemismartHub(empty_registry, quiet_publish)
    restored = await attach_cover(
        hass,
        restored_hub,
        config=cover_config(travel=0.02),
        cover_type=RestoredCover,
    )
    try:
        empty_registry.update_availability("bridge-a", "online")
        restored_hub.notify_bridge_change()

        assert restored.current_cover_position == 80
        # A second, later offline drop no longer questions the anchor.
        empty_registry.update_availability("bridge-a", "offline")
        restored_hub.notify_bridge_change()
        assert restored.current_cover_position == 80
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_set_position_aborts_when_its_preparatory_stop_is_superseded(
    hass: HomeAssistant,
) -> None:
    """A superseded pre-move STOP means a newer command owns the channels.

    Continuing would publish the OLDER set_position movement over the newer
    overlapping command; the multi-frame operation must abort instead.
    """
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        bridge_id = topic.split("/")[1]
        if len(published) == 2:
            # The preparatory STOP is displaced by a newer overlapping
            # command from elsewhere (bridge latest-command-wins).
            assert hub.handle_status(
                bridge_id, {"status": "displaced", "command_id": body["command_id"]}
            )
        else:
            acknowledge(hub, bridge_id, body)

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 80})
        assert entity.is_opening

        await entity.async_set_cover_position(**{ATTR_POSITION: 20})

        # Only the first movement and the superseded STOP were published --
        # no third (DOWN) frame carrying the stale older intent.
        assert len(published) == 2
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_set_position_aborts_when_started_preparatory_stop_is_displaced(
    hass: HomeAssistant,
) -> None:
    """A STARTED-then-displaced STOP cannot authorize the stale final movement."""
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        bridge_id = topic.split("/")[1]
        acknowledge(hub, bridge_id, body)
        if len(published) == 2:
            assert hub.handle_status(
                bridge_id,
                {"status": "displaced", "command_id": body["command_id"]},
            )

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 80})
        assert entity.is_opening

        await entity.async_set_cover_position(**{ATTR_POSITION: 20})

        assert len(published) == 2
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_partial_move_keeps_the_unverified_anchor_revocable(
    hass: HomeAssistant,
) -> None:
    """A relative move still derives from the questioned restore anchor.

    Only an absolute endpoint travel settles the anchor; after a partial
    move, a late offline report from the anchor bridge must still invalidate
    the estimate chain built on it.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    await original.async_will_remove_from_hass()
    await asyncio.sleep(0.05)
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    restored_hub_publishes: list[str] = []
    empty_registry = BridgeRegistry()
    restored_hub: ZemismartHub

    async def restored_publish(topic: str, payload: str) -> None:
        restored_hub_publishes.append(topic)
        acknowledge(restored_hub, topic.split("/")[1], json.loads(payload))

    restored_hub = ZemismartHub(empty_registry, restored_publish)
    restored = await attach_cover(
        hass,
        restored_hub,
        config=cover_config(travel=0.02),
        cover_type=RestoredCover,
    )
    try:
        assert restored.current_cover_position == 80
        # A second bridge comes online and serves a PARTIAL move.
        empty_registry.update_info("bridge-b", {"area": "living_room"})
        empty_registry.update_availability("bridge-b", "online")
        restored_hub.notify_bridge_change()
        await restored.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.05)
        assert restored.current_cover_position == 60

        # The anchor bridge's late offline report still revokes the chain.
        empty_registry.update_availability("bridge-a", "offline")
        restored_hub.notify_bridge_change()
        assert restored.current_cover_position is None
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_restored_state_from_different_hardware_is_ignored(
    hass: HomeAssistant,
) -> None:
    """Re-pointing an entry at other hardware discards the old estimate."""
    restored_state = State(
        "cover.living_room_left",
        "closed",
        {
            ATTR_CURRENT_POSITION: 40,
            "remote": "beef01:07",
            "channels": [5],
        },
    )

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    async def publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(online_registry(), publish),
        cover_type=RestoredCover,
    )
    try:
        assert entity.current_cover_position is None
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_full_travel_interrupted_before_completion_keeps_anchor_revocable(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full travel STOPped before it completes does not settle the anchor.

    Only a full travel that runs its whole configured duration reaches the
    hard limit and settles a questioned restore anchor. If it is interrupted
    early, the position still derives from the questioned origin, so a late
    offline report from the anchor bridge must still revoke it.
    """
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    await original.async_will_remove_from_hass()
    await asyncio.sleep(0.05)  # let the persisted deadline expire
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    empty_registry = BridgeRegistry()
    restored_hub: ZemismartHub

    async def restored_publish(topic: str, payload: str) -> None:
        acknowledge(restored_hub, topic.split("/")[1], json.loads(payload))

    restored_hub = ZemismartHub(empty_registry, restored_publish)
    restored = await attach_cover(
        hass,
        restored_hub,
        config=cover_config(travel=5.0),
        cover_type=RestoredCover,
    )
    try:
        assert restored.current_cover_position == 80  # anchored, unverified
        # The anchor bridge is bridge-a (served the original move). A
        # DIFFERENT bridge comes online to serve the OPEN, so bridge-a stays
        # undiscovered — its later offline report is what tests the fix.
        empty_registry.update_info("bridge-b", {"area": "living_room"})
        empty_registry.update_availability("bridge-b", "online")
        restored_hub.notify_bridge_change()
        assert restored.current_cover_position == 80  # bridge-b online != anchor confirmed

        # Begin a full OPEN (5s travel) and STOP it almost immediately.
        open_task = asyncio.create_task(restored.async_open_cover())
        await asyncio.sleep(0.02)
        assert restored.is_opening
        await restored.async_stop_cover()
        await open_task

        # The full travel never completed, so the anchor is still in doubt:
        # the anchor bridge going offline must invalidate the estimate.
        empty_registry.update_availability("bridge-a", "offline")
        restored_hub.notify_bridge_change()
        assert restored.current_cover_position is None
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_unverified_anchor_survives_a_second_restart(hass: HomeAssistant) -> None:
    """The questioned-anchor marker persists so repeated restarts stay honest.

    After the first restart anchors an expired timed motion (clearing the
    motion, so direction is 0), a second restart before availability arrives
    must not silently promote that target to trusted: the marker is restored
    from state and a late offline report still revokes it.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    first_attrs = dict(original.extra_state_attributes)
    first_attrs[ATTR_CURRENT_POSITION] = original.current_cover_position
    await original.async_will_remove_from_hass()
    await asyncio.sleep(0.05)  # expire the deadline

    holder: dict[str, State] = {"state": State("cover.living_room_left", "open", first_attrs)}

    class Restored(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return holder["state"]

    async def quiet(_topic: str, _payload: str) -> None:
        return

    # First restart: empty registry -> anchors at 80, marks it unverified,
    # clears the motion (direction 0).
    first = await attach_cover(
        hass,
        ZemismartHub(BridgeRegistry(), quiet),
        config=cover_config(travel=0.02),
        cover_type=Restored,
    )
    assert first.current_cover_position == 80
    assert first.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"
    second_attrs = dict(first.extra_state_attributes)
    second_attrs[ATTR_CURRENT_POSITION] = first.current_cover_position
    await first.async_will_remove_from_hass()

    # Second restart from the first restart's persisted (direction-0) state.
    holder["state"] = State("cover.living_room_left", "open", second_attrs)
    reg2 = BridgeRegistry()
    hub2 = ZemismartHub(reg2, quiet)
    second = await attach_cover(hass, hub2, config=cover_config(travel=0.02), cover_type=Restored)
    try:
        assert second.current_cover_position == 80  # restored, still unverified
        reg2.update_availability("bridge-a", "offline")
        hub2.notify_bridge_change()
        assert second.current_cover_position is None  # marker survived -> revoked
    finally:
        await second.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_offline_anchor_does_not_cancel_a_running_motion(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late offline for the OLD anchor must not cancel a live full travel.

    While a full OPEN commanded through another bridge is running, the
    original (undiscovered) anchor bridge reporting offline must leave the
    active motion alone — it reaches the hard limit and anchors at 100,
    rather than being cancelled to unknown forever.
    """
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    original = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    original._position = 50.0
    await original.async_set_cover_position(**{ATTR_POSITION: 80})
    attributes = dict(original.extra_state_attributes)
    attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
    await original.async_will_remove_from_hass()
    await asyncio.sleep(0.05)
    restored_state = State("cover.living_room_left", "opening", attributes)

    class RestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

    empty_registry = BridgeRegistry()
    restored_hub: ZemismartHub

    async def restored_publish(topic: str, payload: str) -> None:
        acknowledge(restored_hub, topic.split("/")[1], json.loads(payload))

    restored_hub = ZemismartHub(empty_registry, restored_publish)
    restored = await attach_cover(
        hass, restored_hub, config=cover_config(travel=0.05), cover_type=RestoredCover
    )
    try:
        assert restored.current_cover_position == 80
        empty_registry.update_info("bridge-b", {"area": "living_room"})
        empty_registry.update_availability("bridge-b", "online")
        restored_hub.notify_bridge_change()

        await restored.async_open_cover()  # full travel through bridge-b
        assert restored.is_opening
        # The anchor bridge reports offline WHILE the OPEN is running.
        empty_registry.update_availability("bridge-a", "offline")
        restored_hub.notify_bridge_change()
        assert restored.is_opening  # not cancelled

        await asyncio.sleep(0.1)  # let the full travel complete
        assert restored.current_cover_position == 100
        assert not restored.is_opening
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_offline_anchor_cancels_running_relative_motion(
    hass: HomeAssistant,
) -> None:
    """A live partial move still depends on its questioned origin."""
    registry = online_registry("bridge-b")
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(registry, publish)
    entity = await attach_cover(
        hass,
        hub,
        config=cover_config(travel=5.0),
        cover_type=restored_cover_type(stopped_unverified_anchor_state()),
    )
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 60})
        assert entity.is_closing

        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()

        assert entity.current_cover_position is None
        assert not entity.is_closing
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_relative_group_motion_revalidates_member_absolute_anchor(
    hass: HomeAssistant,
) -> None:
    """A member leaving exempt full travel cannot reuse an offline origin."""
    member_state = State(
        "cover.living_room_channel_1",
        "open",
        {
            ATTR_CURRENT_POSITION: 80,
            "remote": cover_config().remote_key,
            "channels": [1],
            "motion_direction": 0,
            "unverified_anchor_bridge": "bridge-a",
        },
    )
    registry = online_registry("bridge-b")
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(registry, publish)
    member = await attach_cover(
        hass,
        hub,
        config=member_config(travel=5.0),
        cover_type=restored_cover_type(member_state),
    )
    await member.async_open_cover()
    registry.update_availability("bridge-a", "offline")
    hub.notify_bridge_change()
    assert member.is_opening

    group = await attach_cover(
        hass,
        hub,
        config=cover_config(travel=5.0),
        entry_id="entry-2",
        entity_id="cover.living_room_group",
    )
    group._position = 20.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})

        assert member.current_cover_position is None
        assert not member.is_opening
    finally:
        await member.async_will_remove_from_hass()
        await group.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_offline_anchor_remains_revocable_when_absolute_motion_is_stopped(
    hass: HomeAssistant,
) -> None:
    """An offline report is deferred only while a full travel stays live."""
    registry = online_registry("bridge-b")
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(registry, publish)
    entity = await attach_cover(
        hass,
        hub,
        config=cover_config(travel=5.0),
        cover_type=restored_cover_type(stopped_unverified_anchor_state()),
    )
    try:
        await entity.async_open_cover()
        assert entity.is_opening

        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()

        assert entity.is_opening
        assert entity.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"

        await entity.async_stop_cover()

        assert entity.current_cover_position is None
        assert not entity.is_opening
        assert entity.extra_state_attributes["degraded_bridge"] is True
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_deferred_offline_anchor_survives_reconnect_until_absolute_finishes(
    hass: HomeAssistant,
) -> None:
    """Reconnect cannot erase offline evidence deferred by a full travel."""
    registry = online_registry("bridge-b")
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(registry, publish)
    entity = await attach_cover(
        hass,
        hub,
        config=cover_config(travel=5.0),
        cover_type=restored_cover_type(stopped_unverified_anchor_state()),
    )
    try:
        await entity.async_open_cover()
        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()
        assert entity.is_opening

        registry.update_availability("bridge-a", "online")
        hub.notify_bridge_change()
        assert entity.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"

        await entity.async_stop_cover()

        assert entity.current_cover_position is None
        assert not entity.is_opening
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_timed_motion_is_unknown_when_ack_bridge_is_already_offline(
    hass: HomeAssistant,
) -> None:
    """A raced offline LWT prevents a timed model from being committed."""
    registry = online_registry()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        bridge_id = topic.split("/")[1]
        acknowledge(hub, bridge_id, json.loads(payload))
        registry.update_availability(bridge_id, "offline")
        hub.notify_bridge_change()

    hub = ZemismartHub(registry, publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 80})

        assert entity.current_cover_position is None
        assert not entity.is_opening
        assert entity.extra_state_attributes["last_bridge"] == "bridge-a"
        assert entity.extra_state_attributes["motion_target"] is None
        assert entity.extra_state_attributes["motion_timed"] is False
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_stopped_unverified_anchor_reconciles_preexisting_offline_bridge(
    hass: HomeAssistant,
) -> None:
    """Restore observes an offline LWT that arrived before last state."""
    registry = online_registry()
    registry.update_availability("bridge-a", "offline")

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(registry, quiet_publish),
        cover_type=restored_cover_type(stopped_unverified_anchor_state()),
    )
    try:
        assert entity.current_cover_position is None
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_stopped_unverified_anchor_reconciles_preexisting_online_bridge(
    hass: HomeAssistant,
) -> None:
    """Restore confirms a questioned anchor from existing online state."""

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(online_registry(), quiet_publish),
        cover_type=restored_cover_type(stopped_unverified_anchor_state()),
    )
    try:
        assert entity.current_cover_position == 80
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_relative_restore_reconciles_persisted_offline_anchor_first(
    hass: HomeAssistant,
) -> None:
    """An expired relative target cannot replace an already-invalid origin."""
    config = cover_config()
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "closing",
        {
            ATTR_CURRENT_POSITION: 80,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": -1,
            "motion_target": 60,
            "motion_started": now - 2.0,
            "motion_deadline": now - 1.0,
            "motion_start_position": 80,
            "motion_bridge": "bridge-b",
            "motion_command_id": "relative-command",
            "motion_timed": True,
            "unverified_anchor_bridge": "bridge-a",
        },
    )
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "offline")

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(registry, quiet_publish),
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert entity.current_cover_position is None
        assert entity.extra_state_attributes["motion_direction"] == 0
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_relative_restore_preserves_existing_anchor_dependency(
    hass: HomeAssistant,
) -> None:
    """A second unverified bridge cannot replace the questioned origin bridge."""
    config = cover_config()
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "closing",
        {
            ATTR_CURRENT_POSITION: 80,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": -1,
            "motion_target": 60,
            "motion_started": now - 2.0,
            "motion_deadline": now - 1.0,
            "motion_start_position": 80,
            "motion_bridge": "bridge-b",
            "motion_command_id": "relative-command",
            "motion_timed": True,
            "unverified_anchor_bridge": "bridge-a",
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    registry = BridgeRegistry()
    hub = ZemismartHub(registry, quiet_publish)
    entity = await attach_cover(
        hass,
        hub,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert entity.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"

        registry.update_availability("bridge-b", "online")
        hub.notify_bridge_change()
        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()

        assert entity.current_cover_position is None
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_restarted_absolute_motion_settles_unverified_anchor(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted full travel retains its hard-limit anchoring semantics."""
    clock = [1_000.0]
    monkeypatch.setattr(cover_module, "WALL_CLOCK", lambda: clock[0])
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.001)
    registry = online_registry("bridge-b")
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(registry, publish, now=lambda: clock[0])
    original = await attach_cover(hass, hub, config=cover_config(travel=0.2))
    original._position = 80.0
    original._unverified_anchor_bridge = "bridge-a"
    try:
        await original.async_open_cover()
        attributes = dict(original.extra_state_attributes)
        attributes[ATTR_CURRENT_POSITION] = original.current_cover_position
        assert attributes["motion_absolute_anchor"] is True
    finally:
        await original.async_will_remove_from_hass()

    restored_state = State("cover.living_room_left", "opening", attributes)

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    restored_registry = online_registry("bridge-b")
    restored_hub = ZemismartHub(restored_registry, quiet_publish, now=lambda: clock[0])
    restored = await attach_cover(
        hass,
        restored_hub,
        config=cover_config(travel=0.2),
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert restored.is_opening
        assert restored.extra_state_attributes["motion_absolute_anchor"] is True
        assert restored.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"

        clock[0] = 1_001.0
        await asyncio.sleep(0.02)

        assert restored.current_cover_position == 100
        assert restored.extra_state_attributes["motion_absolute_anchor"] is False
        assert restored.extra_state_attributes["unverified_anchor_bridge"] is None

        restored_registry.update_availability("bridge-a", "offline")
        restored_hub.notify_bridge_change()
        assert restored.current_cover_position == 100
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_timed_restore_has_coherent_unverified_anchor_state(
    hass: HomeAssistant,
) -> None:
    """An expired partial restore is stopped, questioned, and non-absolute."""
    config = cover_config()
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 50,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": 1,
            "motion_target": 80,
            "motion_started": now - 2.0,
            "motion_deadline": now - 1.0,
            "motion_start_position": 50,
            "motion_bridge": "bridge-a",
            "motion_command_id": "timed-command",
            "motion_timed": True,
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(BridgeRegistry(), quiet_publish),
        cover_type=restored_cover_type(restored_state),
    )
    try:
        attributes = entity.extra_state_attributes
        assert entity.current_cover_position == 80
        assert attributes["motion_direction"] == 0
        assert attributes["motion_target"] is None
        assert attributes["motion_absolute_anchor"] is False
        assert attributes["unverified_anchor_bridge"] == "bridge-a"
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_absolute_restore_settles_unverified_anchor(
    hass: HomeAssistant,
) -> None:
    """A full travel completed during downtime replaces the old anchor."""
    config = cover_config()
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 80,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": 1,
            "motion_target": 100,
            "motion_started": now - 2.0,
            "motion_deadline": now - 1.0,
            "motion_start_position": 80,
            "motion_bridge": "bridge-b",
            "motion_command_id": "absolute-command",
            "motion_timed": False,
            "motion_absolute_anchor": True,
            "unverified_anchor_bridge": "bridge-a",
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    registry = online_registry("bridge-b")
    hub = ZemismartHub(registry, quiet_publish)
    entity = await attach_cover(
        hass,
        hub,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        attributes = entity.extra_state_attributes
        assert entity.current_cover_position == 100
        assert attributes["motion_direction"] == 0
        assert attributes["motion_absolute_anchor"] is False
        assert attributes["unverified_anchor_bridge"] is None

        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()
        assert entity.current_cover_position == 100
    finally:
        await entity.async_will_remove_from_hass()
