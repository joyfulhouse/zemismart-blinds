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


def online_registry() -> BridgeRegistry:
    """Return one same-area online bridge."""
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "online")
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
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 60})
        await asyncio.sleep(0.02)

        assert group.current_cover_position is not None
        assert member.current_cover_position == group.current_cover_position
        assert member.is_opening
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
        {ATTR_CURRENT_POSITION: 50, "motion_direction": 1},
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
