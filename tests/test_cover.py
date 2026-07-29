"""Home Assistant fixture tests for RF-start-gated travel-time covers."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import traceback
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, cast

import pytest
from homeassistant.components.cover import ATTR_CURRENT_POSITION, ATTR_POSITION, CoverEntity
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import EntityPlatformState
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.zemismart_blinds import cover as cover_module
from custom_components.zemismart_blinds import models as models_module
from custom_components.zemismart_blinds.codec import encode_b0, make_payload
from custom_components.zemismart_blinds.const import POSITION_UPDATE_INTERVAL_SECONDS
from custom_components.zemismart_blinds.coordinator import RemoteCoordinator
from custom_components.zemismart_blinds.cover import ZemismartCover
from custom_components.zemismart_blinds.models import (
    BlindConfig,
    BridgeRegistry,
    RemoteConfig,
    RemoteIdentity,
    RemoteRuntime,
    Role,
    ZemismartHub,
)
from custom_components.zemismart_blinds.state_sync import HeardEvent, LedgerFrameSpec
from tests.clocks import SteppableClocks
from tests.synthetic import TEST_ACTION_BASES, TEST_PREFIX, TEST_REMOTE_ID

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import (
        AddConfigEntryEntitiesCallback,
        EntityPlatform,
    )

_GENERIC_DISARM_DEADLINE_SECONDS: Final = 0.02
_LATE_LISTENER_DISARM_DEADLINE_SECONDS: Final = 0.2
_TIMED_COMMAND_STOP_AFTER_MS: Final = 5_000
_COMPLETED_COMMAND_ADVANCE_SECONDS: Final = 8.0
_UNTIMED_ACTION_WINDOW_ADVANCE_SECONDS: Final = 5.0
_TEST_REMOTE_KEY: Final = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"


@pytest.mark.parametrize(
    "first_module",
    ["cover_aggregate", "cover"],
)
def test_cover_modules_import_in_either_order(first_module: str) -> None:
    """Either public cover module can be the integration's first import."""
    second_module = "cover" if first_module == "cover_aggregate" else "cover_aggregate"
    package = "custom_components.zemismart_blinds"
    script = (
        f"from {package} import {first_module}; "
        f"from {package} import {second_module}; "
        f"assert cover.ZemismartAggregateCover is "
        f"cover_aggregate.ZemismartAggregateCover"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


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
    entity = cover_type(entry_id, "remote-entry", config or cover_config(), hub)
    entity.hass = hass
    entity.entity_id = entity_id
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()
    await entity.async_added_to_hass()
    # Production reaches this via EntityPlatform. Without it every
    # async_write_ha_state() on the entity is silently discarded before the
    # state machine, so anything observing states -- hass.states.get, the
    # coordinator's state-change subscription -- sees nothing and the test
    # quietly asserts less than it appears to.
    entity._platform_state = EntityPlatformState.ADDED
    entity.async_write_ha_state()
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
            heard_at_monotonic=(cover_module.MONOTONIC_CLOCK() - (cover_module.WALL_CLOCK() - at)),
            bridge_id="synthetic-rx-bridge",
        ),
    )


def patch_cover_clocks(
    monkeypatch: pytest.MonkeyPatch,
    clocks: SteppableClocks,
) -> None:
    """Keep cover and transport code on the same separated clock pair."""
    monkeypatch.setattr(cover_module, "WALL_CLOCK", clocks.wall_now)
    monkeypatch.setattr(cover_module, "MONOTONIC_CLOCK", clocks.monotonic_now)


@pytest.mark.parametrize("wall_step", [-3_600.0, 3_600.0], ids=["backward", "forward"])
@pytest.mark.asyncio
async def test_wall_step_does_not_change_live_partial_move_deadline(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    wall_step: float,
) -> None:
    """A partial move completes after physical elapsed time, not adjusted wall time."""
    # Deliberately far apart: started equal, a monotonic read swapped for a wall
    # one is invisible because the two values coincide. A realistic unix-epoch
    # wall clock against a small uptime-style monotonic clock makes any
    # confusion between the two axes structural rather than a coincidence.
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 10_000.0)
    real_sleep = asyncio.sleep

    async def elapse(seconds: float) -> None:
        if seconds > 0:
            assert entity.position_confidence != "anchored"
        clocks.advance(seconds)
        await real_sleep(0)

    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        **clocks.as_kwargs(),
    )
    entity = await attach_cover(hass, hub, config=cover_config(travel=1.0))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 80})
        expected_elapsed = entity._motion_duration
        entity._cancel_motion_task()
        clocks.step_wall(wall_step)
        monkeypatch.setattr(asyncio, "sleep", elapse)
        token = object()
        entity._motion_token = token
        started_at_monotonic = clocks.monotonic

        await entity._async_track_motion(token)

        assert clocks.monotonic - started_at_monotonic == pytest.approx(expected_elapsed)
        assert entity.current_cover_position == 80
        assert not entity.is_opening
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.parametrize("wall_step", [-3_600.0, 3_600.0], ids=["backward", "forward"])
@pytest.mark.asyncio
async def test_wall_step_does_not_false_anchor_live_full_travel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    wall_step: float,
) -> None:
    """A fractional full travel cannot become anchored after a wall-clock step."""
    # Deliberately far apart: started equal, a monotonic read swapped for a wall
    # one is invisible because the two values coincide. A realistic unix-epoch
    # wall clock against a small uptime-style monotonic clock makes any
    # confusion between the two axes structural rather than a coincidence.
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 10_000.0)
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.1)
    real_sleep = asyncio.sleep

    async def elapse(seconds: float) -> None:
        if seconds > 0:
            assert entity.position_confidence != "anchored"
        clocks.advance(seconds)
        await real_sleep(0)

    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        **clocks.as_kwargs(),
    )
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.2))
    entity._position = 50.0
    try:
        await entity.async_open_cover()
        expected_elapsed = entity._motion_duration
        entity._cancel_motion_task()
        clocks.step_wall(wall_step)
        monkeypatch.setattr(asyncio, "sleep", elapse)
        token = object()
        entity._motion_token = token
        started_at_monotonic = clocks.monotonic

        await entity._async_track_motion(token)

        elapsed = clocks.monotonic - started_at_monotonic
        assert elapsed == pytest.approx(expected_elapsed)
        assert entity.current_cover_position == 100
        assert entity.position_confidence == "anchored"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.parametrize("wall_step", [-2.0, 2.0], ids=["backward", "forward"])
@pytest.mark.asyncio
async def test_wall_step_restore_projects_remaining_duration_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    wall_step: float,
) -> None:
    """Restore maps the persisted wall pair once onto the new monotonic epoch."""
    config = cover_config(travel=10.0)
    persisted_started = 1_000.0
    persisted_deadline = 1_010.0
    clocks = SteppableClocks(
        wall=1_004.0 + wall_step,
        monotonic=50.0,
    )
    patch_cover_clocks(monkeypatch, clocks)
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 20,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": 1,
            "motion_target": 80,
            "motion_started": persisted_started,
            "motion_deadline": persisted_deadline,
            "motion_start_position": 20,
            "motion_bridge": "bridge-a",
            "motion_command_id": "restored-command",
            "motion_timed": True,
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(online_registry(), quiet_publish),
        config=config,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        remaining = persisted_deadline - clocks.wall
        assert entity._motion_deadline_monotonic == pytest.approx(clocks.monotonic + remaining)
        assert entity.extra_state_attributes["motion_started"] == persisted_started
        assert entity.extra_state_attributes["motion_deadline"] == persisted_deadline
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_restore_completion_is_suspect_but_in_flight_restore_is_assumed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a restore completion inferred from wall time is suspect."""
    config = cover_config(travel=10.0)
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)

    def moving_state(*, deadline: float, command_id: str) -> State:
        return State(
            "cover.living_room_left",
            "opening",
            {
                ATTR_CURRENT_POSITION: 44,
                "remote": config.remote_key,
                "channels": list(config.channels),
                "motion_direction": 1,
                "motion_target": 80,
                "motion_started": clocks.wall - 4.0,
                "motion_deadline": deadline,
                "motion_start_position": 20,
                "motion_bridge": "bridge-a",
                "motion_command_id": command_id,
                "motion_timed": True,
            },
        )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    in_flight_hub = ZemismartHub(online_registry(), quiet_publish)
    in_flight = await attach_cover(
        hass,
        in_flight_hub,
        config=config,
        cover_type=restored_cover_type(
            moving_state(
                deadline=clocks.wall + 6.0,
                command_id="in-flight-restore",
            )
        ),
    )
    try:
        assert in_flight.is_opening
        assert in_flight.position_confidence == "assumed"
    finally:
        await in_flight.async_will_remove_from_hass()
        in_flight_hub.close()

    completed_hub = ZemismartHub(online_registry(), quiet_publish)
    completed = await attach_cover(
        hass,
        completed_hub,
        config=config,
        cover_type=restored_cover_type(
            moving_state(
                deadline=clocks.wall - 1.0,
                command_id="completed-restore",
            )
        ),
    )
    try:
        assert not completed.is_opening
        assert completed.current_cover_position == 80
        assert completed.extra_state_attributes["position_confidence"] == "suspect"
    finally:
        await completed.async_will_remove_from_hass()
        completed_hub.close()


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
        with pytest.raises(HomeAssistantError) as timeout:
            await entity.async_open_cover()
        # Translated at the boundary; the transport's own detail is the
        # placeholder, so the assertion still pins what the user is told.
        assert timeout.value.translation_key == "command_timeout"
        # No placeholder: the message is fully translated, and embedding
        # str(exc) is what left English model text in a localized string (#37).
        assert not timeout.value.translation_placeholders

        assert entity.current_cover_position is None
        assert entity.extra_state_attributes["degraded_bridge"] is True
        assert not entity.is_opening
    finally:
        await entity.async_will_remove_from_hass()


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

        with pytest.raises(HomeAssistantError) as publish_failure:
            await entity.async_stop_cover()
        # An OSError from the broker is a transport failure and says so in its
        # own message. It deliberately carries NO placeholder: interpolating
        # str(exc) put untranslated English ("broker down") inside an otherwise
        # localized string (#37). The detail goes to the log instead.
        assert publish_failure.value.translation_key == "transport_failed"
        assert not publish_failure.value.translation_placeholders

        assert entity.is_opening
        await asyncio.sleep(0.02)
        assert entity.current_cover_position is not None
        assert before is not None
        assert entity.current_cover_position > before
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_cancelled_leaf_transmit_marks_position_unknown(hass: HomeAssistant) -> None:
    """A transmit cancelled after publication invalidates the estimate (#28).

    `CancelledError` derives from `BaseException`, so it used to pass through
    both of `_async_transmit`'s handlers and unwind without touching the model
    -- while the published frame reached the air and moved the blind. The cover
    kept a confident, specific, wrong position. Drives the ENTITY, which the
    existing hub-lifecycle cancellation tests do not.
    """
    hub: ZemismartHub
    published = asyncio.Event()
    bodies: list[dict[str, Any]] = []

    async def publish(topic: str, payload: str) -> None:
        if not topic.endswith("/tx"):
            return
        # Published -- the frame is on air -- but deliberately never
        # acknowledged, so the entity is still awaiting `started`.
        bodies.append(json.loads(payload))
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 50.0
    try:
        closing = asyncio.create_task(entity.async_close_cover())
        await asyncio.wait_for(published.wait(), timeout=1.0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert entity.current_cover_position is None
        assert entity.position_confidence == "unknown"

        # A `started` arriving after the cancellation must not resurrect it:
        # nobody modelled the travel, so there is nothing to resume.
        acknowledge(hub, "bridge-a", bodies[0])
        await hass.async_block_till_done()
        assert entity.current_cover_position is None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_set_position_at_current_while_moving_sends_stop(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An apparent no-op cannot silently freeze a motor that is still moving."""
    bodies: list[dict[str, Any]] = []
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        bodies.append(body)
        if len(bodies) == 2:
            # Make the old post-STOP re-evaluation deterministically see a
            # different estimate. The requested position was the value HA
            # reported at service entry, so this must remain a STOP-only no-op.
            clocks.advance(0.02)
        acknowledge(hub, topic.split("/")[1], body)

    config = cover_config(travel=1.0)
    hub = ZemismartHub(
        online_registry(),
        publish,
        **clocks.as_kwargs(),
    )
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
        hub.close()


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
    member = ZemismartCover("entry-2", "remote-entry", member_config, hub)
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
        "remote-entry",
        cover_config(),
        ZemismartHub(online_registry(), publish),
    )
    entity._position = 40.0
    entity._direction = 1
    entity._motion_start_position = 40.0
    entity._motion_target = 80.0
    entity._motion_started_monotonic = 0.0
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
async def test_takeover_invalidation_does_not_supersede_own_awaited_full_move(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated takeover invalidation cannot discard a started own move."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.005)
    admitted = asyncio.Event()
    allow_started = asyncio.Event()
    command_ids = iter(("older-group-up", "own-channel-down"))
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/cmd"):
            return
        bridge_id = topic.split("/")[1]
        if body["command_id"] == "older-group-up":
            acknowledge(hub, bridge_id, body)
            return
        assert hub.handle_status(
            bridge_id,
            {"status": "accepted", "command_id": body["command_id"]},
        )
        admitted.set()
        await allow_started.wait()
        assert hub.handle_status(
            bridge_id,
            {"status": "started", "command_id": body["command_id"]},
        )

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: next(command_ids),
    )
    pressed = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    commanded = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2, travel=0.04),
        entry_id="entry-2",
        entity_id="cover.own_channel_2",
    )
    pressed._position = 50.0
    commanded._position = 80.0
    command: asyncio.Task[None] | None = None
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        command = asyncio.create_task(commanded.async_close_cover())
        await admitted.wait()

        dispatch_heard_press(
            hub,
            pressed._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        assert commanded.current_cover_position is None
        assert not commanded.is_closing

        allow_started.set()
        await command

        assert commanded.is_closing
        assert commanded._motion_command_id == "own-channel-down"
        assert commanded._motion_target == 0.0
        motion_task = commanded._motion_task
        assert motion_task is not None
        await asyncio.wait_for(asyncio.shield(motion_task), timeout=1.0)
        assert commanded.current_cover_position == 0
    finally:
        if command is not None and not command.done():
            command.cancel()
        await commanded.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
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
        hub._start_disarm_request(
            "bridge-a",
            "publish-barrier",
            hub._now() + 1.0,
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
        started_at_monotonic=cover_module.MONOTONIC_CLOCK(),
        deadline=None,
        deadline_monotonic=None,
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
        old_deadline = entity._motion_deadline_monotonic
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

        await asyncio.sleep(max(0.0, old_deadline - cover_module.MONOTONIC_CLOCK()) + 0.02)

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
        old_deadline = entity._motion_deadline_monotonic
        assert command_id is not None

        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)
        await asyncio.sleep(max(0.0, old_deadline - cover_module.MONOTONIC_CLOCK()) + 0.02)

        assert [item for item in published if item[0].endswith("/cmd")]
        assert entity.current_cover_position is None
        assert not entity.is_opening
        assert entity.extra_state_attributes["degraded_bridge"] is True
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_confirmed_stop_takeover_disarm_timeout_marks_mirror_unknown(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost generic disarm invalidates a mirror after a commanded STOP."""
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _GENERIC_DISARM_DEADLINE_SECONDS,
    )
    published: list[tuple[str, dict[str, Any]]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "confirmed-stop",
    )
    entity = await attach_cover(hass, hub, config=member_config(travel=5.0))
    entity._position = 50.0
    try:
        assert await entity._async_stop()
        assert entity._motion_command_id is None

        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "confirmed-stop")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(task, timeout=1.0)

        assert [item for item in published if item[0].endswith("/cmd")] == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": "confirmed-stop"},
            )
        ]
        assert entity.current_cover_position is None
        assert not entity.is_opening
        assert entity.extra_state_attributes["degraded_bridge"] is True
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_second_press_merges_timeout_hook_into_live_disarm_request(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later press on a live disarm request keeps its failure consequence.

    The dedup-skip must merge the new pressed-cover hooks into the existing
    request instead of discarding them: with the disarm lost, BOTH pressed
    covers must end unknown, not only the first.
    """
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _GENERIC_DISARM_DEADLINE_SECONDS,
    )
    published: list[tuple[str, dict[str, Any]]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "group-stop",
    )
    group = await attach_cover(hass, hub, config=channel_group_config((1, 2)))
    member_one = await attach_cover(hass, hub, config=member_config(channel=1))
    member_two = await attach_cover(hass, hub, config=member_config(channel=2))
    group._position = 50.0
    member_one._position = 50.0
    member_two._position = 50.0
    try:
        assert await group._async_stop()
        assert group._motion_command_id is None

        dispatch_heard_press(hub, group._config, "UP", (1,), at=cover_module.WALL_CLOCK())
        dispatch_heard_press(hub, group._config, "UP", (2,), at=cover_module.WALL_CLOCK())
        assert member_one.is_opening
        assert member_two.is_opening
        request = hub._disarm_requests[("bridge-a", "group-stop")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(task, timeout=1.0)

        disarms = [item for item in published if item[0].endswith("/cmd")]
        assert len(disarms) == 1
        assert member_one.current_cover_position is None
        assert member_two.current_cover_position is None
    finally:
        await member_two.async_will_remove_from_hass()
        await member_one.async_will_remove_from_hass()
        await group.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_generic_timeout_hook_does_not_fan_out_to_unthreatened_members(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost generic disarm invalidates only the covers the command threatens.

    The listener hook must be self-only: the group's whole-command fan-out
    would re-invalidate a member the hub's intersect-both filter deliberately
    excluded (its channel is untouched by the un-disarmed command).
    """
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _GENERIC_DISARM_DEADLINE_SECONDS,
    )
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "channel-one-stop",
    )
    member_one = await attach_cover(hass, hub, config=member_config(channel=1))
    member_two = await attach_cover(hass, hub, config=member_config(channel=2))
    group = await attach_cover(hass, hub, config=channel_group_config((1, 2)))
    member_one._position = 50.0
    member_two._position = 50.0
    group._position = 50.0
    try:
        assert await member_one._async_stop()
        assert member_one._motion_command_id is None

        dispatch_heard_press(hub, group._config, "UP", (1, 2), at=cover_module.WALL_CLOCK())
        assert member_two.is_opening
        request = hub._disarm_requests[("bridge-a", "channel-one-stop")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(task, timeout=1.0)

        # The command only ever addressed channel 1: covers on it go unknown,
        # while member 2's mirrored motion is never threatened and survives.
        assert member_one.current_cover_position is None
        assert group.current_cover_position is None
        assert member_two.is_opening
    finally:
        await group.async_will_remove_from_hass()
        await member_two.async_will_remove_from_hass()
        await member_one.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_lost_disarm_invalidates_unmodeled_covers_outside_the_press(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost disarm marks every cover on the command's channels unknown.

    A raw send is never modeled by any cover, so its covers hold no motion
    command; a press on a subset must still leave the OUTSIDE-the-press cover
    honestly unknown when the disarm is lost — the aborted command latched
    its motor and the un-disarmed frames may keep driving it.
    """
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _GENERIC_DISARM_DEADLINE_SECONDS,
    )
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "raw-group-up",
    )
    member_one = await attach_cover(hass, hub, config=member_config(channel=1))
    member_two = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_2",
    )
    member_one._position = 50.0
    member_two._position = 50.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        assert member_two._motion_command_id is None

        dispatch_heard_press(
            hub,
            member_one._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        assert member_one.is_closing
        request = hub._disarm_requests[("bridge-a", "raw-group-up")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(task, timeout=1.0)

        assert member_one.current_cover_position is None
        assert member_two.current_cover_position is None
    finally:
        await member_two.async_will_remove_from_hass()
        await member_one.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_completed_timed_command_is_not_disarmed_on_physical_takeover(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command retained only for echoes cannot threaten idle members."""
    published: list[tuple[str, dict[str, Any]]] = []
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "completed-timed-group",
        **clocks.as_kwargs(),
    )
    pressed = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    unpressed = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2, travel=5.0),
        entry_id="entry-2",
        entity_id="cover.completed_channel_2",
    )
    pressed._position = 50.0
    unpressed._position = 60.0
    try:
        await hub.async_transmit(
            channel_group_config((1, 2)),
            "UP",
            stop_after_ms=_TIMED_COMMAND_STOP_AFTER_MS,
        )
        clocks.advance(_COMPLETED_COMMAND_ADVANCE_SECONDS)

        dispatch_heard_press(
            hub,
            pressed._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert pressed.is_closing
        assert unpressed.current_cover_position == 60
        assert [item for item in published if item[0].endswith("/cmd")] == []
    finally:
        await unpressed.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_completed_untimed_cover_command_is_not_disarmed_on_physical_takeover(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover-owned takeover ignores a command whose RF window has ended."""
    published: list[tuple[str, dict[str, Any]]] = []
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "untimed-member-up",
        **clocks.as_kwargs(),
    )
    entity = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    entity._position = 50.0
    try:
        await entity.async_open_cover()
        assert entity.is_opening
        assert entity._motion_command_id == "untimed-member-up"
        clocks.advance(_UNTIMED_ACTION_WINDOW_ADVANCE_SECONDS)

        dispatch_heard_press(
            hub,
            entity._config,
            "DOWN",
            (1,),
            at=clocks.wall,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert [item for item in published if item[0].endswith("/cmd")] == []
        assert entity.is_closing
        assert entity.current_cover_position is not None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_raw_takeover_invalidates_partially_contained_unpressed_aggregate(
    hass: HomeAssistant,
) -> None:
    """A successful takeover invalidates every unpressed command overlap."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "aggregate-group-up",
    )
    pressed = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    aggregate = await attach_cover(
        hass,
        hub,
        config=channel_group_config((2, 3)),
        entry_id="entry-2",
        entity_id="cover.channels_2_3",
    )
    outside = await attach_cover(
        hass,
        hub,
        config=member_config(channel=4, travel=5.0),
        entry_id="entry-3",
        entity_id="cover.channel_4",
    )
    pressed._position = 50.0
    aggregate._position = 60.0
    outside._position = 70.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)

        dispatch_heard_press(
            hub,
            pressed._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )

        assert pressed.is_closing
        assert aggregate.current_cover_position is None
        assert outside.current_cover_position == 70
    finally:
        await outside.async_will_remove_from_hass()
        await aggregate.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_takeover_timeout_invalidates_listener_attached_after_request(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disarm consequence discovers threatened listeners at fire time."""
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _LATE_LISTENER_DISARM_DEADLINE_SECONDS,
    )
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "late-listener-group-up",
    )
    pressed = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    late: ZemismartCover | None = None
    pressed._position = 50.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        dispatch_heard_press(
            hub,
            pressed._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "late-listener-group-up")]
        task = request.task
        assert task is not None

        late = await attach_cover(
            hass,
            hub,
            config=member_config(channel=2, travel=5.0),
            entry_id="entry-2",
            entity_id="cover.late_channel_2",
        )
        late._position = 60.0
        await asyncio.wait_for(task, timeout=1.0)

        assert late.current_cover_position is None
    finally:
        if late is not None:
            await late.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_takeover_disarm_ack_invalidates_listener_attached_after_request(
    hass: HomeAssistant,
) -> None:
    """Successful disarm rechecks unpressed listeners added during the drain."""
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "late-ack-group-up",
    )
    pressed = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    late_config = member_config(channel=2, travel=5.0)
    restored_state = State(
        "cover.late_ack_channel_2",
        "open",
        {
            ATTR_CURRENT_POSITION: 60,
            "remote": late_config.remote_key,
            "channels": list(late_config.channels),
            "motion_direction": 0,
        },
    )
    late: ZemismartCover | None = None
    pressed._position = 50.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        dispatch_heard_press(
            hub,
            pressed._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "late-ack-group-up")]
        task = request.task
        assert task is not None

        late = await attach_cover(
            hass,
            hub,
            config=late_config,
            cover_type=restored_cover_type(restored_state),
            entry_id="entry-2",
            entity_id="cover.late_ack_channel_2",
        )
        assert late.current_cover_position == 60
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "late-ack-group-up"},
        )
        await asyncio.wait_for(task, timeout=1.0)

        assert late.current_cover_position is None
        assert pressed.is_closing
        assert pressed.current_cover_position is not None
    finally:
        if late is not None:
            await late.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_displaced_timed_command_fires_takeover_consequence_immediately(
    hass: HomeAssistant,
) -> None:
    """Flushed fail-safe STOPs immediately invalidate the pressed mirror."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "timed-group-up",
    )
    entity = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    entity._position = 50.0
    try:
        await hub.async_transmit(
            channel_group_config((1, 2)),
            "UP",
            stop_after_ms=_TIMED_COMMAND_STOP_AFTER_MS,
        )
        dispatch_heard_press(
            hub,
            entity._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        assert entity.is_closing
        request = hub._disarm_requests[("bridge-a", "timed-group-up")]
        task = request.task
        assert task is not None

        assert hub.handle_status(
            "bridge-a",
            {"status": "displaced", "command_id": "timed-group-up"},
        )

        assert entity.current_cover_position is None
        assert not entity.is_closing
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_stop_survives_displaced_timed_takeover_consequence(
    hass: HomeAssistant,
) -> None:
    """A flushed STOP cannot invalidate a mirror already halted by heard STOP."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "timed-member-up",
    )
    entity = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    entity._position = 50.0
    try:
        await hub.async_transmit(
            member_config(channel=1, travel=5.0),
            "UP",
            stop_after_ms=_TIMED_COMMAND_STOP_AFTER_MS,
        )
        dispatch_heard_press(
            hub,
            entity._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        assert entity.is_closing
        request = hub._disarm_requests[("bridge-a", "timed-member-up")]
        task = request.task
        assert task is not None

        dispatch_heard_press(
            hub,
            entity._config,
            "STOP",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        stopped_position = entity.current_cover_position
        assert stopped_position is not None
        assert not entity.is_closing

        assert hub.handle_status(
            "bridge-a",
            {"status": "displaced", "command_id": "timed-member-up"},
        )
        await asyncio.wait_for(task, timeout=1.0)

        assert entity.current_cover_position == stopped_position
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_displaced_raw_command_settles_takeover_disarm_timeout(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Displacement settles the obsolete disarm without erasing a new mirror."""
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _GENERIC_DISARM_DEADLINE_SECONDS,
    )
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "raw-group-up",
    )
    entity = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    entity._position = 50.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)

        dispatch_heard_press(
            hub,
            entity._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        assert entity.is_closing
        request = hub._disarm_requests[("bridge-a", "raw-group-up")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)

        assert hub.handle_status(
            "bridge-a",
            {"status": "displaced", "command_id": "raw-group-up"},
        )
        await asyncio.wait_for(task, timeout=1.0)
        await asyncio.sleep(_GENERIC_DISARM_DEADLINE_SECONDS)

        assert request.waiter.done() and not request.waiter.cancelled()
        assert entity.is_closing
        assert entity.current_cover_position is not None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_takeover_timeout_preserves_newer_commanded_model(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic timeout cannot erase a newer commanded model."""
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _GENERIC_DISARM_DEADLINE_SECONDS,
    )
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "raw-group-up",
    )
    mirroring = await attach_cover(hass, hub, config=member_config(channel=1))
    commanded = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_2",
    )
    mirroring._position = 40.0
    mirroring._direction = -1
    commanded._position = 65.0
    commanded._direction = 1
    commanded._motion_command_id = "newer-command"
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        dispatch_heard_press(
            hub,
            mirroring._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "raw-group-up")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(task, timeout=1.0)

        assert mirroring.current_cover_position is None
        assert commanded.current_cover_position == 65
        assert commanded._motion_command_id == "newer-command"
    finally:
        await commanded.async_will_remove_from_hass()
        await mirroring.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_confirmed_group_stop_timeout_preserves_unpressed_idle_member(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost confirmed STOP disarm threatens only the pressed mirror."""
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _GENERIC_DISARM_DEADLINE_SECONDS,
    )
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "confirmed-group-stop",
    )
    group = await attach_cover(hass, hub, config=channel_group_config((1, 2)))
    pressed = await attach_cover(hass, hub, config=member_config(channel=1))
    unpressed = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_2",
    )
    group._position = 50.0
    pressed._position = 40.0
    unpressed._position = 60.0
    try:
        assert await group._async_stop()
        assert pressed._motion_command_id is None
        assert unpressed._motion_command_id is None

        dispatch_heard_press(
            hub,
            group._config,
            "UP",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "confirmed-group-stop")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(task, timeout=1.0)

        assert pressed.current_cover_position is None
        assert unpressed.current_cover_position == 60
    finally:
        await unpressed.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
        await group.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_raw_takeover_disarm_ack_invalidates_unpressed_idle_member(
    hass: HomeAssistant,
) -> None:
    """A successful raw-movement abort invalidates its idle unpressed cover."""
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "raw-group-up",
    )
    pressed = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    unpressed = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2, travel=5.0),
        entry_id="entry-2",
        entity_id="cover.living_room_channel_2",
    )
    pressed._position = 50.0
    unpressed._position = 60.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        assert pressed._motion_command_id is None
        assert unpressed._motion_command_id is None

        dispatch_heard_press(
            hub,
            pressed._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "raw-group-up")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "raw-group-up"},
        )
        await asyncio.wait_for(task, timeout=1.0)

        assert pressed.is_closing
        assert unpressed.current_cover_position is None
    finally:
        await unpressed.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_confirmed_stop_takeover_disarm_ack_keeps_mirrored_motion(
    hass: HomeAssistant,
) -> None:
    """A timely generic disarm ack preserves the mirrored physical UP."""
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "confirmed-stop",
    )
    entity = await attach_cover(hass, hub, config=member_config(travel=5.0))
    entity._position = 50.0
    try:
        assert await entity._async_stop()
        assert entity._motion_command_id is None

        dispatch_heard_press(
            hub,
            entity._config,
            "UP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "confirmed-stop")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "confirmed-stop"},
        )
        await asyncio.wait_for(task, timeout=1.0)

        assert entity.is_opening
        assert entity._motion_target == 100.0
        assert entity.current_cover_position is not None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.parametrize("outcome", ["timed_out", "displaced"])
@pytest.mark.asyncio
async def test_heard_stop_preserves_cover_owned_takeover_resolution(
    hass: HomeAssistant,
    outcome: str,
) -> None:
    """A heard STOP stays authoritative when an owned disarm resolves."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "owned-timed-up",
    )
    entity = await attach_cover(hass, hub, config=member_config(travel=0.4))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 75})
        command_id = entity._motion_command_id
        assert command_id == "owned-timed-up"

        dispatch_heard_press(
            hub,
            entity._config,
            "DOWN",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", command_id)]
        task = request.task
        assert task is not None

        dispatch_heard_press(
            hub,
            entity._config,
            "STOP",
            entity._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        stopped_position = entity.current_cover_position
        assert stopped_position is not None
        assert not entity.is_closing

        if outcome == "displaced":
            assert hub.handle_status(
                "bridge-a",
                {"status": "displaced", "command_id": command_id},
            )
        await asyncio.wait_for(task, timeout=1.0)

        assert entity.current_cover_position == stopped_position
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_group_stop_preserves_members_after_takeover_timeout(
    hass: HomeAssistant,
) -> None:
    """A full-group heard STOP protects every frozen member from timeout."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "owned-timed-group-up",
    )
    group = await attach_cover(hass, hub, config=channel_group_config((1, 2), travel=0.4))
    member_one = await attach_cover(hass, hub, config=member_config(channel=1, travel=0.4))
    member_two = await attach_cover(
        hass,
        hub,
        config=member_config(channel=2, travel=0.4),
        entry_id="entry-2",
        entity_id="cover.group_stop_channel_2",
    )
    group._position = 50.0
    member_one._position = 40.0
    member_two._position = 60.0
    try:
        await group.async_set_cover_position(**{ATTR_POSITION: 75})
        command_id = group._motion_command_id
        assert command_id == "owned-timed-group-up"

        dispatch_heard_press(
            hub,
            group._config,
            "DOWN",
            group._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", command_id)]
        task = request.task
        assert task is not None

        dispatch_heard_press(
            hub,
            group._config,
            "STOP",
            group._config.channels,
            at=cover_module.WALL_CLOCK(),
        )
        stopped_positions = (
            member_one.current_cover_position,
            member_two.current_cover_position,
        )
        assert all(position is not None for position in stopped_positions)

        await asyncio.wait_for(task, timeout=1.0)

        assert member_one.current_cover_position == stopped_positions[0]
        assert member_two.current_cover_position == stopped_positions[1]
    finally:
        await member_two.async_will_remove_from_hass()
        await member_one.async_will_remove_from_hass()
        await group.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_merged_takeover_disarm_uses_cumulative_pressed_channels(
    hass: HomeAssistant,
) -> None:
    """A later press joins the request without an earlier snapshot erasing it."""
    disarm_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)
        else:
            disarm_published.set()

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "cumulative-group-up",
    )
    member_one = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    member_two: ZemismartCover | None = None
    member_one._position = 50.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        dispatch_heard_press(
            hub,
            member_one._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )

        member_two_config = member_config(channel=2, travel=5.0)
        restored_state = State(
            "cover.cumulative_channel_2",
            "open",
            {
                ATTR_CURRENT_POSITION: 60,
                "remote": member_two_config.remote_key,
                "channels": list(member_two_config.channels),
                "motion_direction": 0,
            },
        )
        member_two = await attach_cover(
            hass,
            hub,
            config=member_two_config,
            cover_type=restored_cover_type(restored_state),
            entry_id="entry-2",
            entity_id="cover.cumulative_channel_2",
        )
        dispatch_heard_press(
            hub,
            member_one._config,
            "DOWN",
            (1, 2),
            at=cover_module.WALL_CLOCK(),
        )
        assert member_one.is_closing
        assert member_two.is_closing
        assert member_two.current_cover_position is not None

        request = hub._disarm_requests[("bridge-a", "cumulative-group-up")]
        task = request.task
        assert task is not None
        await asyncio.wait_for(disarm_published.wait(), timeout=1.0)
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "cumulative-group-up"},
        )
        await asyncio.wait_for(task, timeout=1.0)

        assert member_one.is_closing
        assert member_two.is_closing
        assert member_two.current_cover_position is not None
    finally:
        if member_two is not None:
            await member_two.async_will_remove_from_hass()
        await member_one.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_displaced_takeover_invalidates_late_unpressed_listener(
    hass: HomeAssistant,
) -> None:
    """Displacement evaluates an unpressed listener attached during disarm."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "displaced-late-group-up",
    )
    pressed = await attach_cover(hass, hub, config=member_config(channel=1, travel=5.0))
    late: ZemismartCover | None = None
    pressed._position = 50.0
    try:
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await hub.async_send_raw("bridge-a", raw_frame, 2)
        dispatch_heard_press(
            hub,
            pressed._config,
            "DOWN",
            (1,),
            at=cover_module.WALL_CLOCK(),
        )
        request = hub._disarm_requests[("bridge-a", "displaced-late-group-up")]
        task = request.task
        assert task is not None

        late_config = member_config(channel=2, travel=5.0)
        restored_state = State(
            "cover.displaced_late_channel_2",
            "open",
            {
                ATTR_CURRENT_POSITION: 60,
                "remote": late_config.remote_key,
                "channels": list(late_config.channels),
                "motion_direction": 0,
            },
        )
        late = await attach_cover(
            hass,
            hub,
            config=late_config,
            cover_type=restored_cover_type(restored_state),
            entry_id="entry-2",
            entity_id="cover.displaced_late_channel_2",
        )
        assert late.current_cover_position == 60

        assert hub.handle_status(
            "bridge-a",
            {"status": "displaced", "command_id": "displaced-late-group-up"},
        )
        await asyncio.wait_for(task, timeout=1.0)

        assert late.current_cover_position is None
    finally:
        if late is not None:
            await late.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
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
async def test_takeover_timeout_during_restore_wins_over_stopped_position(
    hass: HomeAssistant,
) -> None:
    """A consequence delivered during restore cannot be clobbered by cache."""
    config = member_config(channel=1)
    restored_state = State(
        "cover.living_room_left",
        "open",
        {
            ATTR_CURRENT_POSITION: 60,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": 0,
        },
    )

    class InvalidatedDuringRestore(ZemismartCover):
        async def async_get_last_state(self) -> State:
            self._invalidate_for_takeover()
            return restored_state

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)
    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=InvalidatedDuringRestore,
    )
    try:
        assert entity.current_cover_position is None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_press_during_restore_supersedes_cached_motion(
    hass: HomeAssistant,
) -> None:
    """A live heard UP cannot be clobbered by an older cached DOWN."""
    config = member_config(channel=1, travel=5.0)
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "closing",
        {
            ATTR_CURRENT_POSITION: 60,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": -1,
            "motion_target": 0,
            "motion_started": now - 1.0,
            "motion_deadline": now + 10.0,
            "motion_start_position": 80,
            "motion_bridge": "bridge-a",
            "motion_command_id": "cached-down",
            "motion_timed": False,
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)

    class HeardUpDuringRestore(ZemismartCover):
        async def async_get_last_state(self) -> State:
            dispatch_heard_press(hub, config, "UP", (1,), at=cover_module.WALL_CLOCK())
            return restored_state

    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=HeardUpDuringRestore,
    )
    try:
        assert entity.is_opening
        assert not entity.is_closing
        assert entity._motion_target == 100.0
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_group_motion_during_restore_supersedes_cached_motion(
    hass: HomeAssistant,
) -> None:
    """A live group command cannot be clobbered by an older cached motion.

    Members register with the coordinator BEFORE their restore await, so an
    aggregate can drive a member while its persisted state is still loading.
    """
    config = member_config(channel=1, travel=5.0)
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "closing",
        {
            ATTR_CURRENT_POSITION: 60,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "motion_direction": -1,
            "motion_target": 0,
            "motion_started": now - 1.0,
            "motion_deadline": now + 10.0,
            "motion_start_position": 80,
            "motion_bridge": "bridge-a",
            "motion_command_id": "cached-down",
            "motion_timed": False,
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)

    class GroupDrivenDuringRestore(ZemismartCover):
        async def async_get_last_state(self) -> State:
            self._start_member_motion(
                cover_module._MotionStart(
                    source="commanded",
                    started_at=cover_module.WALL_CLOCK(),
                    started_at_monotonic=cover_module.MONOTONIC_CLOCK(),
                    deadline=None,
                    deadline_monotonic=None,
                    bridge_id="bridge-a",
                    command_id="group-up",
                ),
                ack=None,
                direction=1,
                duration=0.0,
                group_target=100.0,
            )
            return restored_state

    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=GroupDrivenDuringRestore,
    )
    try:
        assert entity.is_opening
        assert not entity.is_closing
        assert entity._motion_target == 100.0
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_later_heard_press_overtakes_restore_invalidation(
    hass: HomeAssistant,
) -> None:
    """A heard UP after live invalidation remains the chronological winner."""
    pressed_config = member_config(channel=1, travel=5.0)
    restored_config = member_config(channel=2, travel=5.0)
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.restore_order_channel_2",
        "closing",
        {
            ATTR_CURRENT_POSITION: 60,
            "remote": restored_config.remote_key,
            "channels": list(restored_config.channels),
            "motion_direction": -1,
            "motion_target": 0,
            "motion_started": now - 1.0,
            "motion_deadline": now + 10.0,
            "motion_start_position": 80,
            "motion_bridge": "bridge-a",
            "motion_command_id": "cached-down",
            "motion_timed": False,
        },
    )
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        command_id_factory=lambda: "restore-order-group-up",
    )
    pressed = await attach_cover(hass, hub, config=pressed_config)
    pressed._position = 50.0
    raw_frame = encode_b0(
        make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
    )
    await hub.async_send_raw("bridge-a", raw_frame, 2)

    class InvalidatedThenHeardUp(ZemismartCover):
        async def async_get_last_state(self) -> State:
            dispatch_heard_press(
                hub,
                pressed_config,
                "DOWN",
                (1,),
                at=cover_module.WALL_CLOCK(),
            )
            dispatch_heard_press(
                hub,
                restored_config,
                "UP",
                (2,),
                at=cover_module.WALL_CLOCK(),
            )
            return restored_state

    entity = await attach_cover(
        hass,
        hub,
        config=restored_config,
        cover_type=InvalidatedThenHeardUp,
        entry_id="entry-2",
        entity_id="cover.restore_order_channel_2",
    )
    try:
        assert entity.is_opening
        assert not entity.is_closing
        assert entity._motion_target == 100.0
    finally:
        await entity.async_will_remove_from_hass()
        await pressed.async_will_remove_from_hass()
        hub.close()


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

        assert member._motion_deadline_monotonic == pytest.approx(
            member._motion_started_monotonic + member._motion_duration
        )
        assert member._motion_deadline_monotonic < group._motion_deadline_monotonic
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
async def test_group_member_clamped_to_its_limit_re_anchors(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reaching a hard limit settles the anchor even when the GROUP did not.

    ``absolute_anchor`` records the group's intent, so a member whose own
    travel clamps to its endpoint while the group aims somewhere in between
    was left questioned despite physically resting against its limit switch.
    Re-anchoring keys on where the motion actually ENDED.
    """

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    # An empty registry: bridge-a is never seen online, so the questioned
    # anchor survives the restore and only reaching a limit can settle it.
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    travel = 0.02
    hub = ZemismartHub(BridgeRegistry(), quiet_publish)
    config = cover_config(travel=travel)
    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=restored_cover_type(stopped_unverified_anchor_state()),
    )
    try:
        assert entity.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"
        entity._position = 20.0
        # The group aims at 50 -- NOT an endpoint, so absolute_anchor is False --
        # but this member's own travel overshoots and clamps to 0.
        entity._start_member_motion(
            cover_module._MotionStart(
                source="commanded",
                started_at=cover_module.WALL_CLOCK(),
                started_at_monotonic=cover_module.MONOTONIC_CLOCK(),
                deadline=None,
                deadline_monotonic=None,
                bridge_id="bridge-b",
                command_id="group-down",
            ),
            ack=None,
            direction=-1,
            duration=travel,
            group_target=50.0,
        )
        assert entity._motion_target == 0.0
        assert entity._motion_absolute_anchor is False
        await asyncio.sleep(travel + 0.01 + 0.05)

        assert entity.current_cover_position == 0
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
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
async def test_restored_state_from_the_other_role_is_ignored(
    hass: HomeAssistant,
) -> None:
    """A cover_id that changed role must not inherit the old role's position.

    The sibling half of this guard -- remote and channels -- is pinned by
    test_restored_state_from_different_hardware_is_ignored. The role half was
    pinned by nothing: making the comparison always-true left all 860 tests
    green, because every restore fixture supplies a role that matches by
    construction.

    It is reachable and it is the worst failure mode this integration has. An
    aggregate publishes `role` precisely so a restore can discriminate, and the
    position it publishes is DERIVED from its members. Flip that cover_id to a
    leaf -- a topology edit in options -- and without this check that
    member-derived number is reinstated as the new leaf's own dead-reckoned
    estimate: a confident, specific position for hardware it never measured.
    """
    restored_state = State(
        "cover.living_room_left",
        "open",
        {
            ATTR_CURRENT_POSITION: 70,
            # Same hardware, so the remote/channels half of the guard passes
            # and only the role half can reject this.
            "remote": cover_config().remote_key,
            "channels": list(cover_config().channels),
            "role": Role.AGGREGATE.value,
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
async def test_restarted_absolute_motion_keeps_unverified_anchor_revocable(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted full travel keeps its unwitnessed origin revocable."""
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.001)
    registry = online_registry("bridge-b")
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        registry,
        publish,
        **clocks.as_kwargs(),
    )
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
    restored_hub = ZemismartHub(
        restored_registry,
        quiet_publish,
        **clocks.as_kwargs(),
    )
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

        clocks.advance(1.0)
        await asyncio.sleep(0.02)

        assert restored.current_cover_position == 100
        assert restored.extra_state_attributes["motion_absolute_anchor"] is False
        assert restored.extra_state_attributes["unverified_anchor_bridge"] == "bridge-a"
        assert restored.position_confidence == "suspect"

        restored_registry.update_availability("bridge-a", "offline")
        restored_hub.notify_bridge_change()
        assert restored.current_cover_position is None
        assert restored.position_confidence == "unknown"
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_recovered_travel_completion_keeps_suspect_and_earns_no_anchor(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart gap cannot be promoted to an observed hard-limit travel."""
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.001)
    config = cover_config(travel=10.0)
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 40,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "role": config.role.value,
            "motion_direction": 1,
            "motion_target": 100,
            "motion_started": clocks.wall - 5.0,
            "motion_deadline": clocks.wall + 5.0,
            "motion_start_position": 0,
            "motion_bridge": "bridge-a",
            "motion_command_id": "recovered-open",
            "motion_timed": False,
            "motion_absolute_anchor": True,
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish, **clocks.as_kwargs())
    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert entity.is_opening

        clocks.advance(5.0)
        await asyncio.sleep(0.02)

        assert entity.current_cover_position == 100
        assert entity._position_anchored is False
        assert entity._suspect is True
        assert entity.position_confidence == "suspect"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_recovered_absolute_completion_applies_deferred_offline_evidence(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered endpoint cannot settle an origin invalidated while moving."""
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    monkeypatch.setattr(cover_module, "POSITION_UPDATE_INTERVAL_SECONDS", 0.001)
    config = cover_config(travel=10.0)
    restored_state = State(
        "cover.living_room_left",
        "opening",
        {
            ATTR_CURRENT_POSITION: 40,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "role": config.role.value,
            "motion_direction": 1,
            "motion_target": 100,
            "motion_started": clocks.wall - 5.0,
            "motion_deadline": clocks.wall + 5.0,
            "motion_start_position": 0,
            "motion_bridge": "bridge-b",
            "motion_command_id": "recovered-open",
            "motion_timed": False,
            "motion_absolute_anchor": True,
            "unverified_anchor_bridge": "bridge-a",
        },
    )

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    registry = online_registry("bridge-b")
    hub = ZemismartHub(registry, quiet_publish, **clocks.as_kwargs())
    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert entity.is_opening

        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()
        assert entity.is_opening
        assert entity.extra_state_attributes["unverified_anchor_offline"] is True

        clocks.advance(5.0)
        await asyncio.sleep(0.02)

        assert entity.current_cover_position is None
        assert entity.position_confidence == "unknown"
        assert entity.extra_state_attributes["unverified_anchor_bridge"] is None
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


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
        assert entity.position_confidence == "suspect"
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_restore_does_not_persist_suspect_without_position(
    hass: HomeAssistant,
) -> None:
    """Late offline reconciliation discards both estimate and its doubt."""
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
    registry = BridgeRegistry()

    class LateOfflineRestoredCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            return restored_state

        def _bridge_seen_online(self, bridge_id: str) -> bool:
            registry.update_availability(bridge_id, "offline")
            return False

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    entity = await attach_cover(
        hass,
        ZemismartHub(registry, quiet_publish),
        cover_type=LateOfflineRestoredCover,
    )
    try:
        assert entity.current_cover_position is None
        assert entity.position_confidence == "unknown"
        assert entity.extra_state_attributes["position_suspect"] is False
    finally:
        await entity.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_expired_absolute_restore_keeps_unverified_anchor_revocable(
    hass: HomeAssistant,
) -> None:
    """A full travel completed during downtime stays suspect and revocable."""
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
        assert attributes["unverified_anchor_bridge"] == "bridge-a"
        assert entity.position_confidence == "suspect"

        registry.update_availability("bridge-a", "offline")
        hub.notify_bridge_change()
        assert entity.current_cover_position is None
        assert entity.position_confidence == "unknown"
    finally:
        await entity.async_will_remove_from_hass()


# --- Aggregate covers (coordinator-derived state, single-frame RF) ---


def aggregate_family(
    hass: HomeAssistant,
    hub: ZemismartHub,
    *,
    travel: float = 1.0,
) -> tuple[RemoteCoordinator, BlindConfig, BlindConfig, BlindConfig]:
    """Return a coordinator plus configs for leaves {1},{2} and aggregate {1,2}."""
    from custom_components.zemismart_blinds.models import CoverConfig

    del hub  # the family shares the caller's hub; nothing to derive from it
    covers = {
        "sub-1": CoverConfig(
            name="Channel 1",
            channels=(1,),
            travel_up=travel,
            travel_down=travel,
            cover_id="sub-1",
        ),
        "sub-2": CoverConfig(
            name="Channel 2",
            channels=(2,),
            travel_up=travel,
            travel_down=travel,
            cover_id="sub-2",
        ),
        "sub-agg": CoverConfig(name="Both", channels=(1, 2), cover_id="sub-agg"),
    }
    remote = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES)
    coordinator = RemoteCoordinator(hass, covers, "remote-entry", remote.key)
    leaf_one = BlindConfig(
        name="Channel 1",
        remote=remote,
        channels=(1,),
        travel_up=travel,
        travel_down=travel,
        area_id="living_room",
        repeats=2,
    )
    leaf_two = BlindConfig(
        name="Channel 2",
        remote=remote,
        channels=(2,),
        travel_up=travel,
        travel_down=travel,
        area_id="living_room",
        repeats=2,
    )
    from custom_components.zemismart_blinds.models import Role as _Role

    aggregate = BlindConfig(
        name="Both",
        remote=remote,
        channels=(1, 2),
        travel_up=None,
        travel_down=None,
        area_id="living_room",
        repeats=2,
        role=_Role.AGGREGATE,
    )
    return coordinator, leaf_one, leaf_two, aggregate


async def attach_family(
    hass: HomeAssistant,
    hub: ZemismartHub,
    *,
    travel: float = 1.0,
) -> tuple[ZemismartCover, ZemismartCover, cover_module.ZemismartAggregateCover]:
    """Attach two leaves and their aggregate wired through one coordinator."""
    coordinator, leaf_one_config, leaf_two_config, aggregate_config = aggregate_family(
        hass, hub, travel=travel
    )
    leaf_one = ZemismartCover("sub-1", "remote-entry", leaf_one_config, hub, coordinator)
    leaf_two = ZemismartCover("sub-2", "remote-entry", leaf_two_config, hub, coordinator)
    aggregate = cover_module.ZemismartAggregateCover(
        "sub-agg", "remote-entry", aggregate_config, hub, coordinator
    )
    for entity, entity_id in (
        (leaf_one, "cover.channel_1"),
        (leaf_two, "cover.channel_2"),
        (aggregate, "cover.both"),
    ):
        entity.hass = hass
        entity.entity_id = entity_id
        entity.platform = platform_stub()
        await entity.async_internal_added_to_hass()
        await entity.async_added_to_hass()
        entity._platform_state = EntityPlatformState.ADDED
        entity.async_write_ha_state()
    return leaf_one, leaf_two, aggregate


async def attach_family_entity(
    hass: HomeAssistant,
    entity: ZemismartCover | cover_module.ZemismartAggregateCover,
    entity_id: str,
) -> None:
    """Attach one preconstructed family entity with real HA lifecycle state."""
    entity.hass = hass
    entity.entity_id = entity_id
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()
    await entity.async_added_to_hass()
    entity._platform_state = EntityPlatformState.ADDED
    entity.async_write_ha_state()


async def detach_family(*entities: Any) -> None:
    """Tear the family down in reverse order."""
    for entity in reversed(entities):
        await entity.async_will_remove_from_hass()


def restored_leaf_state(
    config: BlindConfig,
    *,
    entity_id: str = "cover.channel_2",
    position: int = 40,
) -> State:
    """Return restorable state tied to one leaf's current hardware identity."""
    return State(
        entity_id,
        "open",
        {
            ATTR_CURRENT_POSITION: position,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "role": Role.LEAF.value,
        },
    )


def topology_cover(
    name: str,
    cover_id: str,
    channels: tuple[int, ...],
) -> models_module.CoverConfig:
    """Return one calibrated leaf for tombstone topology tests."""
    return models_module.CoverConfig(
        name=name,
        channels=channels,
        travel_up=1.0,
        travel_down=1.0,
        cover_id=cover_id,
    )


def tombstone_coordinator(
    hass: HomeAssistant,
    *covers: models_module.CoverConfig,
    remote_key: str = _TEST_REMOTE_KEY,
) -> RemoteCoordinator:
    """Attach current tombstone topology for one remote entry."""
    return RemoteCoordinator(
        hass,
        {cover.cover_id: cover for cover in covers},
        "remote-entry",
        remote_key,
    )


@pytest.mark.asyncio
async def test_aggregate_command_yields_to_press_heard_during_transmit(
    hass: HomeAssistant,
) -> None:
    """A press heard while the group frame is queued wins for that member."""
    hub: ZemismartHub
    entered = asyncio.Event()
    release = asyncio.Event()

    async def publish(topic: str, payload: str) -> None:
        if not topic.endswith("/tx"):
            return
        entered.set()
        await release.wait()
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    try:
        leaf_one._position = 50.0
        leaf_two._position = 50.0
        opening = asyncio.create_task(aggregate.async_open_cover())
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        dispatch_heard_press(hub, leaf_one._config, "DOWN", (1,), at=cover_module.WALL_CLOCK())
        release.set()
        await asyncio.wait_for(opening, timeout=1.0)

        # The pressed member keeps its newer heard model; the other member
        # follows the group command.
        assert leaf_one.is_closing
        assert not leaf_one.is_opening
        assert leaf_two.is_opening
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_state_derives_from_members(hass: HomeAssistant) -> None:
    """Position is the member mean; closed only when every member is closed.

    An unknown member takes the whole aggregate position to None (#32): the
    mean of the remaining members is a confident number describing only part of
    the hardware. `is_closed` keeps its own, weaker rule -- one member known
    open is enough to know the group is not closed.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        leaf_one._position = 100.0
        leaf_two._position = 0.0
        assert aggregate.current_cover_position == 50
        assert aggregate.is_closed is False

        leaf_one._position = 0.0
        assert aggregate.current_cover_position == 0
        assert aggregate.is_closed is True

        leaf_one._position = None
        assert aggregate.current_cover_position is None  # one unknown: no honest mean
        assert aggregate.is_closed is None  # none open, one unknown

        leaf_two._position = 60.0
        assert aggregate.is_closed is False  # any open wins over unknown
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


@pytest.mark.asyncio
async def test_aggregate_open_drives_each_member_model(hass: HomeAssistant) -> None:
    """One full-set frame starts every member's own absolute-anchor travel."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    leaf_one._position = 20.0
    leaf_two._position = 80.0
    try:
        await aggregate.async_open_cover()
        assert leaf_one.is_opening
        assert leaf_two.is_opening
        assert leaf_one._motion_target == 100.0
        assert leaf_two._motion_target == 100.0
        assert leaf_one._motion_absolute_anchor is True
        assert aggregate.is_opening  # derived
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


@pytest.mark.asyncio
async def test_aggregate_stop_freezes_members_at_ack(hass: HomeAssistant) -> None:
    """A displaced or clean aggregate STOP freezes every member's model."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    leaf_one._position = 20.0
    leaf_two._position = 20.0
    try:
        await aggregate.async_open_cover()
        assert leaf_one.is_opening
        await aggregate.async_stop_cover()
        assert not leaf_one.is_opening
        assert not leaf_two.is_opening
        assert leaf_one.current_cover_position is not None
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


@pytest.mark.asyncio
async def test_aggregate_set_position_fans_out_with_member_timing(
    hass: HomeAssistant,
) -> None:
    """Position commands delegate to each member's own timed positioning."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    leaf_one._position = 20.0
    leaf_two._position = 80.0
    try:
        await aggregate.async_set_cover_position(**{ATTR_POSITION: 60})
        assert leaf_one._motion_target == 60.0
        assert leaf_two._motion_target == 60.0
        assert leaf_one.is_opening  # 20 -> 60
        assert leaf_two.is_closing  # 80 -> 60
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


@pytest.mark.asyncio
async def test_aggregate_set_position_preflights_before_any_frame(
    hass: HomeAssistant,
) -> None:
    """One unpositionable member aborts the call before ANY frame goes on air.

    The old fan-out transmitted to the healthy members first and only then hit
    the unknown one, so the service reported failure after half the group had
    physically moved -- and a retry would then move those members again from
    their new positions (#32). Asserted on the transmit mock, not just on the
    exception.
    """
    hub: ZemismartHub
    tx_frames: list[str] = []

    async def publish(topic: str, payload: str) -> None:
        body = json.loads(payload)
        if topic.endswith("/tx"):
            tx_frames.append(body["raw"])
        acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    leaf_one._position = 20.0
    leaf_two._position = None  # unknown: set_position must reject this member
    try:
        with pytest.raises(HomeAssistantError) as delegation:
            await aggregate.async_set_cover_position(**{ATTR_POSITION: 60})
        assert tx_frames == []  # nothing reached the air
        assert leaf_one._motion_target is None  # the healthy member did NOT move
        assert not leaf_one.is_opening
        # Preflight refuses BEFORE the fan-out, so this is the unknown-position
        # rejection, not the post-hoc delegation failure it used to be (#32),
        # and it is translated rather than raw English (#37).
        assert delegation.value.translation_key == "member_position_unknown"
        assert "Channel 2" in (delegation.value.translation_placeholders or {})["members"]
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


async def attach_weighted_family(
    hass: HomeAssistant,
    hub: ZemismartHub,
    *,
    travel: float = 1.0,
) -> tuple[ZemismartCover, ZemismartCover, cover_module.ZemismartAggregateCover]:
    """Attach leaves of UNEQUAL channel cardinality ({1,2} and {3}) plus their group."""
    from custom_components.zemismart_blinds.models import CoverConfig
    from custom_components.zemismart_blinds.models import Role as _Role

    covers = {
        "sub-wide": CoverConfig(
            name="Pair",
            channels=(1, 2),
            travel_up=travel,
            travel_down=travel,
            cover_id="sub-wide",
        ),
        "sub-solo": CoverConfig(
            name="Single",
            channels=(3,),
            travel_up=travel,
            travel_down=travel,
            cover_id="sub-solo",
        ),
        "sub-agg": CoverConfig(name="All three", channels=(1, 2, 3), cover_id="sub-agg"),
    }
    remote = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES)
    coordinator = RemoteCoordinator(hass, covers, "remote-entry", remote.key)
    wide_config = BlindConfig(
        name="Pair",
        remote=remote,
        channels=(1, 2),
        travel_up=travel,
        travel_down=travel,
        area_id="living_room",
        repeats=2,
    )
    solo_config = BlindConfig(
        name="Single",
        remote=remote,
        channels=(3,),
        travel_up=travel,
        travel_down=travel,
        area_id="living_room",
        repeats=2,
    )
    aggregate_config = BlindConfig(
        name="All three",
        remote=remote,
        channels=(1, 2, 3),
        travel_up=None,
        travel_down=None,
        area_id="living_room",
        repeats=2,
        role=_Role.AGGREGATE,
    )
    wide = ZemismartCover("sub-wide", "remote-entry", wide_config, hub, coordinator)
    solo = ZemismartCover("sub-solo", "remote-entry", solo_config, hub, coordinator)
    aggregate = cover_module.ZemismartAggregateCover(
        "sub-agg", "remote-entry", aggregate_config, hub, coordinator
    )
    for entity, entity_id in (
        (wide, "cover.pair"),
        (solo, "cover.single"),
        (aggregate, "cover.all_three"),
    ):
        entity.hass = hass
        entity.entity_id = entity_id
        entity.platform = platform_stub()
        await entity.async_internal_added_to_hass()
        await entity.async_added_to_hass()
    return wide, solo, aggregate


@pytest.mark.asyncio
async def test_aggregate_position_weights_members_by_channel_count(
    hass: HomeAssistant,
) -> None:
    """A member is a motor set, not a vote (#32).

    A leaf covering {1,2} and one covering {3} are three motors; the group must
    report the mean of the MOTORS, not the midpoint of the two leaves.
    """

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)
    wide, solo, aggregate = await attach_weighted_family(hass, hub)
    try:
        wide._position = 100.0
        solo._position = 0.0
        # Unweighted this reads 50; two of the three motors are fully open.
        assert aggregate.current_cover_position == 67

        wide._position = 0.0
        solo._position = 100.0
        assert aggregate.current_cover_position == 33

        # And one unknown member still takes the whole group to None.
        solo._position = None
        assert aggregate.current_cover_position is None
    finally:
        await detach_family(wide, solo, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_recompute_batches_into_one_write(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several member writes in one iteration flush the aggregate once."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    writes: list[int] = []
    original_write = aggregate.async_write_ha_state

    def counting_write() -> None:
        writes.append(1)
        original_write()

    monkeypatch.setattr(aggregate, "async_write_ha_state", counting_write)
    try:
        leaf_one._position = 10.0
        leaf_two._position = 30.0
        leaf_one.async_write_ha_state()
        leaf_two.async_write_ha_state()
        assert writes == []  # deferred to the scheduled flush
        await hass.async_block_till_done()
        assert len(writes) == 1
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


@pytest.mark.asyncio
async def test_partial_press_invalidates_only_intersected_leaf(hass: HomeAssistant) -> None:
    """A press covering part of a leaf invalidates it; disjoint leaves model on."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    remote = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES)
    wide = await attach_cover(
        hass,
        hub,
        config=BlindConfig(
            name="Wide",
            remote=remote,
            channels=(1, 2),
            travel_up=1.0,
            travel_down=1.0,
            area_id="living_room",
            repeats=2,
        ),
        entry_id="sub-wide",
        entity_id="cover.wide",
    )
    solo = await attach_cover(
        hass,
        hub,
        config=member_config(channel=3, travel=1.0),
        entry_id="sub-solo",
        entity_id="cover.solo",
    )
    wide._position = 50.0
    solo._position = 50.0
    try:
        dispatch_heard_press(hub, wide._config, "UP", (1,), at=cover_module.WALL_CLOCK())
        assert wide.current_cover_position is None  # partial: only unknown is honest
        assert solo.current_cover_position == 50  # disjoint: untouched

        dispatch_heard_press(hub, solo._config, "UP", (3,), at=cover_module.WALL_CLOCK())
        assert solo.is_opening  # full coverage: models the press
    finally:
        await solo.async_will_remove_from_hass()
        await wide.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_aggregate_open_cancels_inflight_fanout(hass: HomeAssistant) -> None:
    """A full-set command preempts pending member position delegations."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    leaf_one._position = 20.0
    leaf_two._position = 20.0
    blocked = asyncio.Event()

    async def never_done() -> None:
        await blocked.wait()

    pending = hass.async_create_task(never_done(), "stub fan-out")
    aggregate._fanout_tasks.add(pending)
    try:
        await aggregate.async_open_cover()
        assert pending.cancelled()
        assert not aggregate._fanout_tasks
    finally:
        blocked.set()
        await detach_family(leaf_one, leaf_two, aggregate)


@pytest.mark.asyncio
async def test_cancelled_fanout_marks_every_member_unknown(hass: HomeAssistant) -> None:
    """Cancelling a fan-out mid-flight invalidates every member it published (#28).

    `_cancel_fanout()` runs from `async_will_remove_from_hass`, so an entry
    reload or options change during a group position move cancels every
    member's transmit after its frame is already on air.

    The queue is globally serialized, so exactly one member's frame is
    published while the other is still queued behind it -- and both go unknown:
    the entity cannot tell which side of publication its cancellation landed
    on, and the pessimistic answer is the only safe one.
    """
    hub: ZemismartHub
    published = asyncio.Event()
    bodies: list[dict[str, Any]] = []

    async def publish(topic: str, payload: str) -> None:
        if not topic.endswith("/tx"):
            return
        # On air, never acknowledged: the fan-out is awaiting `started`.
        bodies.append(json.loads(payload))
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    leaf_one._position = 20.0
    leaf_two._position = 80.0
    try:
        fanout = asyncio.create_task(aggregate.async_set_cover_position(**{ATTR_POSITION: 60}))
        await asyncio.wait_for(published.wait(), timeout=1.0)
        aggregate._cancel_fanout()
        with pytest.raises(asyncio.CancelledError):
            await fanout

        assert leaf_one.current_cover_position is None
        assert leaf_two.current_cover_position is None
        assert aggregate.current_cover_position is None

        for body in bodies:
            acknowledge(hub, "bridge-a", body)
        await hass.async_block_till_done()
        assert aggregate.current_cover_position is None
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_cancelled_group_frame_marks_every_member_unknown(hass: HomeAssistant) -> None:
    """A cancelled group frame moved every channel, so every member is unknown (#28).

    The aggregate owns no position model, and the members cannot learn of the
    loss themselves -- the command was never theirs.
    """
    hub: ZemismartHub
    published = asyncio.Event()

    async def publish(topic: str, _payload: str) -> None:
        if topic.endswith("/tx"):
            published.set()

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    leaf_one._position = 20.0
    leaf_two._position = 80.0
    try:
        closing = asyncio.create_task(aggregate.async_close_cover())
        await asyncio.wait_for(published.wait(), timeout=1.0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert leaf_one.current_cover_position is None
        assert leaf_two.current_cover_position is None
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_fanout_reraises_unexpected_member_exception(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected member exception fails the service call with its traceback (#33).

    `delegate()` catches only `HomeAssistantError` and the gather results were
    discarded, so a `TypeError` or `AttributeError` in a member's command path
    produced no traceback, no service failure, and a group where some blinds
    moved and some did not.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=5.0)
    leaf_one._position = 20.0
    leaf_two._position = 80.0

    async def broken_member_command(_target: int) -> None:
        msg = "member command path is broken"
        raise ValueError(msg)

    monkeypatch.setattr(leaf_two, "async_set_member_position", broken_member_command)
    try:
        with pytest.raises(ValueError, match="member command path is broken") as caught:
            await aggregate.async_set_cover_position(**{ATTR_POSITION: 60})

        # Re-raised, not wrapped: the raising frame is still in the traceback.
        frames = traceback.format_tb(caught.value.__traceback__)
        assert any("broken_member_command" in frame for frame in frames)
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_takeover_state_expires_and_tracks_heard_stop(
    hass: HomeAssistant,
) -> None:
    """Retired command ids stop feeding takeover; heard STOPs are flagged."""
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        await aggregate.async_open_cover()
        state = aggregate._takeover_state()
        assert state.command_id is not None

        aggregate._last_command_at_monotonic = cover_module.MONOTONIC_CLOCK() - 3600.0
        expired = aggregate._takeover_state()
        assert expired.command_id is None
        assert expired.disarm_deadline_monotonic is None

        dispatch_heard_press(
            hub,
            aggregate._config,
            "STOP",
            (1, 2),
            at=cover_module.WALL_CLOCK(),
        )
        assert aggregate._takeover_state().stopped_by_heard is True
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


def test_legacy_cover_clock_patch_reaches_aggregate(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aggregate reads its clock through the legacy cover module."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    coordinator, _leaf_one, _leaf_two, config = aggregate_family(hass, hub)
    aggregate = cover_module.ZemismartAggregateCover(
        "sub-agg",
        "remote-entry",
        config,
        hub,
        coordinator,
    )
    aggregate._last_command_bridge = "bridge-a"
    aggregate._last_command_id = "command-a"
    aggregate._last_command_button = "UP"
    aggregate._last_command_at_monotonic = 100.0
    try:
        # Both readings run under an explicit patch: the first sits far past
        # the command stamp (expired), the second right next to it (armed).
        # An aggregate holding a by-value clock import would ignore the
        # second patch and keep returning the real-clock answer, so the seam
        # is still what decides the outcome — without assuming anything
        # about the host's actual monotonic value (a freshly booted CI
        # runner sits near zero, which broke the unpatched baseline).
        monkeypatch.setattr(cover_module, "MONOTONIC_CLOCK", lambda: 1_000_000.0)
        expired = aggregate._takeover_state()
        monkeypatch.setattr(cover_module, "MONOTONIC_CLOCK", lambda: 105.0)
        patched = aggregate._takeover_state()

        assert expired.command_id is None
        assert patched.command_id == "command-a"
        assert patched.disarm_deadline_monotonic == 115.0
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_covers_go_unavailable_when_ha_loses_the_broker(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retained bridge beacons must not keep covers available with MQTT down."""
    from homeassistant.components import mqtt

    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        # The bridge's retained availability still says online...
        assert all(bridge.online for bridge in hub.registry.bridges)
        assert leaf_one.available is True
        assert aggregate.available is True

        # ...but with HA's own client down, nothing can reach the air.
        monkeypatch.setattr(mqtt, "is_connected", lambda _hass: False)

        assert leaf_one.available is False
        assert leaf_two.available is False
        assert aggregate.available is False
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)


@pytest.mark.asyncio
async def test_broker_drop_rerenders_cover_availability(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection-status subscription must be wired and released."""
    from homeassistant.components import mqtt

    subscribers: list[Callable[[bool], None]] = []
    unsubscribed: list[Callable[[bool], None]] = []

    def fake_subscribe(
        _hass: object,
        callback_fn: Callable[[bool], None],
    ) -> Callable[[], None]:
        subscribers.append(callback_fn)
        return lambda: unsubscribed.append(callback_fn)

    monkeypatch.setattr(mqtt, "async_subscribe_connection_status", fake_subscribe)

    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        # Every entity registered a connection-status callback.
        assert len(subscribers) == 3

        writes: list[str] = []
        for entity in (leaf_one, leaf_two, aggregate):
            monkeypatch.setattr(
                entity,
                "async_write_ha_state",
                lambda entity=entity: writes.append(entity.entity_id),
            )
        # A broker drop must push new state, not wait for an unrelated event.
        for notify in subscribers:
            notify(False)
        assert len(writes) == 3
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)

    # ...and every callback is released on teardown.
    assert len(unsubscribed) == 3


# #21: a bridge holding several targets dispatches them round-robin, one slot
# each, so a command's own repeats are spread across the OTHER targets' slots
# too -- not sent back-to-back the way a solo train would be. Past four
# concurrent same-bridge targets, our own later repeats used to fall outside
# the (contiguous-assuming) confirmed window and dispatch as a physical
# press. `_on_heard_press` (cover.py:475-491) has two distinct consequences
# once that happens: it ALWAYS bumps `_intent_generation` (dropping the
# in-flight commanded motion's ownership), then either re-anchors travel at
# the wrong instant (a cover fully covered by the phantom press) or marks the
# cover unknown (a cover only partially covered). The two tests below drive a
# REAL command through the hub -- so `record_commanded_start` and the
# ledger's confirmed window come from production code, not a synthetic
# stand-in -- and feed the phantom echo through `hub.handle_rx` with a
# genuinely encoded frame, exactly as a peer bridge would report it.
_ROUND_ROBIN_BRIDGE_ID: Final = "bridge-office"
_ROUND_ROBIN_TARGET_COUNT: Final = 7
_ROUND_ROBIN_TRAIN_REPEATS: Final = 3  # the production default
_ROUND_ROBIN_TRAIN_MS: Final = 3_000
# Measured on air in #21 at this concurrency and repeat count: an own repeat
# heard at +8.1 s, well outside the un-stretched 3.75 s window but inside the
# round-robin-stretched one this fix computes: (repeats - 1) * (targets - 1)
# * 1 s + 3.75 s = 2 * 6 * 1 + 3.75 = 15.75 s.
_ROUND_ROBIN_LATE_OWN_REPEAT_SECONDS: Final = 8.1


def _register_round_robin_peers(
    hub: ZemismartHub,
    remote_key: str,
    handoff: float,
    *,
    count: int = _ROUND_ROBIN_TARGET_COUNT - 1,
    first_channel: int = 20,
) -> None:
    """Confirm same-bridge peer commands so a real command picks up concurrency.

    `CommandLedger._round_robin_concurrency` counts confirmed same-bridge
    entries purely from `bridge_id`/`handoff`/`frames` -- exactly the state a
    real peer command leaves behind -- so registering peers straight into the
    ledger alongside one genuinely commanded cover reproduces the concurrency
    a busy bridge produces without standing up seven full entities.
    """
    for index in range(count):
        channels = (first_channel + index,)
        hub._ledger.register_pending(
            f"peer-{index}",
            _ROUND_ROBIN_BRIDGE_ID,
            channels,
            "DOWN",
            [
                LedgerFrameSpec(
                    (remote_key, frozenset(channels), "DOWN"),
                    offset_ms=0,
                    airtime_ms=_ROUND_ROBIN_TRAIN_MS,
                ),
            ],
        )
        hub._ledger.confirm(f"peer-{index}", handoff)


@pytest.mark.asyncio
async def test_round_robin_burst_does_not_re_anchor_a_fully_covered_cover(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-bridge burst's own late repeat must not re-anchor travel (#21).

    Reproduces the FIRST `_on_heard_press` consequence shape: a fully
    covered cover re-anchors travel at `heard_at` -- the wrong instant, since
    the frame is our own late repeat, not a physical press -- producing a
    wrong final position while the motor runs on. `_intent_generation` and
    `_motion_command_id` are checked directly because `_on_heard_press`
    changes BOTH unconditionally the moment it runs at all; equality after
    the phantom frame is direct proof the callback never ran.
    """
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(_ROUND_ROBIN_BRIDGE_ID),
        publish,
        **clocks.as_kwargs(),
    )
    entity = await attach_cover(
        hass,
        hub,
        config=BlindConfig(
            name="Office channel 3",
            remote=cover_config().remote,
            channels=(3,),
            travel_up=100.0,
            travel_down=100.0,
            area_id="living_room",
            repeats=_ROUND_ROBIN_TRAIN_REPEATS,
        ),
    )
    try:
        await entity.async_close_cover()
        assert entity.is_closing
        real_command_id = entity._motion_command_id
        assert real_command_id is not None
        handoff = clocks.monotonic
        generation_before = entity._intent_generation

        _register_round_robin_peers(hub, entity._config.remote_key, handoff)

        clocks.advance(_ROUND_ROBIN_LATE_OWN_REPEAT_SECONDS)
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (3,), "DOWN", bases=TEST_ACTION_BASES)
        )
        hub.handle_rx("bridge-peer", {"frame": raw_frame, "t": 0, "boot": 1})

        assert entity._intent_generation == generation_before
        assert entity._motion_command_id == real_command_id
        assert entity.is_closing
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_round_robin_burst_does_not_mark_a_partially_covered_cover_unknown(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-bridge burst's own late repeat must not mark a wider cover unknown (#21).

    Reproduces the SECOND `_on_heard_press` consequence shape: a press whose
    channels only partially cover a multi-channel cover takes the
    `_mark_unknown()` branch instead -- the exposure the team flagged for
    aggregates, where a leaf's own command only intersects a wider cover
    spanning it plus another channel. `group` is a plain two-channel
    `ZemismartCover` (role defaults to LEAF), matching the existing
    `test_partially_addressed_heard_press_marks_group_unknown` config shape;
    only channel 2 of its (2, 3) is ever driven by a real command, exactly
    like an aggregate whose displayed state spans more channels than any one
    contributing leaf command addresses.
    """
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(_ROUND_ROBIN_BRIDGE_ID),
        publish,
        **clocks.as_kwargs(),
    )
    group = await attach_cover(
        hass,
        hub,
        config=BlindConfig(
            name="Office channels 2+3",
            remote=cover_config().remote,
            channels=(2, 3),
            travel_up=100.0,
            travel_down=100.0,
            area_id="living_room",
            repeats=_ROUND_ROBIN_TRAIN_REPEATS,
        ),
    )
    leaf = await attach_cover(
        hass,
        hub,
        config=BlindConfig(
            name="Office channel 2",
            remote=cover_config().remote,
            channels=(2,),
            travel_up=100.0,
            travel_down=100.0,
            area_id="living_room",
            repeats=_ROUND_ROBIN_TRAIN_REPEATS,
        ),
        entry_id="entry-2",
        entity_id="cover.office_channel_2",
    )
    group._position = 50.0
    try:
        await leaf.async_close_cover()
        assert leaf.is_closing
        handoff = clocks.monotonic
        group_generation_before = group._intent_generation

        _register_round_robin_peers(hub, leaf._config.remote_key, handoff)

        clocks.advance(_ROUND_ROBIN_LATE_OWN_REPEAT_SECONDS)
        raw_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (2,), "DOWN", bases=TEST_ACTION_BASES)
        )
        hub.handle_rx("bridge-peer", {"frame": raw_frame, "t": 0, "boot": 1})

        assert group._intent_generation == group_generation_before
        assert group.current_cover_position == 50
        assert not group._degraded
    finally:
        await leaf.async_will_remove_from_hass()
        await group.async_will_remove_from_hass()
        hub.close()


# --- Explicit reanchor recovery service + position_confidence (issue #23) ---


@pytest.mark.asyncio
async def test_reanchor_service_drives_leaf_to_endpoint_from_unknown(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reanchor recovers a cover whose position is unknown -- its whole point.

    A full travel needs no prior estimate, so the service works from the exact
    state (`unknown`) it exists to repair, and the endpoint completion anchors
    the estimate through the normal outcome-based path.
    """
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    try:
        assert entity.current_cover_position is None
        assert entity.position_confidence == "unknown"

        await entity.async_reanchor("close")
        assert entity.is_closing
        await asyncio.sleep(0.02 + 0.01 + 0.06)

        assert entity.current_cover_position == 0
        assert entity.position_confidence == "anchored"
        assert entity._suspect is False
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_completed_endpoint_travel_publishes_anchored_not_verified(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published attribute reads `anchored`; `verified` is gone (#31).

    A completed timer against a hard limit is the strongest claim this
    integration can make, and it is not motor confirmation -- the vocabulary
    must not say otherwise. Asserted on the ATTRIBUTE an automation reads, not
    only on the property behind it.
    """
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=0.02))
    try:
        await entity.async_close_cover()
        await asyncio.sleep(0.02 + 0.01 + 0.06)

        assert entity.extra_state_attributes["position_confidence"] == "anchored"
        assert cover_module.CONFIDENCE_ANCHORED == "anchored"
        assert not hasattr(cover_module, "CONFIDENCE_VERIFIED")
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_reanchor_on_aggregate_is_one_group_frame(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reanchor on an aggregate re-anchors it exactly like a full open on it."""
    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub: ZemismartHub
    tx_frames: list[str] = []

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            body = json.loads(payload)
            tx_frames.append(body["raw"])
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=0.02)
    try:
        await aggregate.async_reanchor("open")
        # ONE frame addressed to the whole channel set -- not one per member.
        assert len(tx_frames) == 1
        assert tx_frames[0] == encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_ACTION_BASES)
        )
        await asyncio.sleep(0.02 + 0.01 + 0.06)

        assert leaf_one.current_cover_position == 100
        assert leaf_two.current_cover_position == 100
        assert aggregate.current_cover_position == 100
        assert aggregate.position_confidence == "anchored"
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


async def _commanded_untimed_full_close(
    hass: HomeAssistant,
    clocks: SteppableClocks,
) -> tuple[ZemismartCover, ZemismartHub]:
    """Attach a leaf at 100 and start a commanded untimed full close on it.

    Frozen-clock: the completion task never fires, so the caller controls
    exactly where in the travel a heard STOP lands.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        online_registry(),
        publish,
        **clocks.as_kwargs(),
    )
    entity = await attach_cover(hass, hub, config=cover_config(travel=10.0))
    entity._position = 100.0
    await entity.async_close_cover()
    assert entity.is_closing
    assert not entity._motion_timed
    assert entity._motion_target == 0.0
    return entity, hub


@pytest.mark.asyncio
async def test_heard_stop_early_in_untimed_full_travel_marks_suspect(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heard STOP early in an untimed full close leaves the estimate suspect."""
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    entity, hub = await _commanded_untimed_full_close(hass, clocks)
    try:
        started = entity._motion_started_monotonic
        duration = entity._motion_duration
        # STOP heard near the START of travel: the blind is still near open.
        clocks.set_monotonic(started + 0.1 * duration)
        dispatch_heard_press(hub, entity._config, "STOP", (1, 2), at=clocks.wall)

        assert entity._suspect is True
        assert entity.position_confidence == "suspect"
        position = entity.current_cover_position
        assert position is not None
        assert position > 50
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_heard_stop_late_in_untimed_full_travel_marks_suspect(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same doubt when the STOP is heard LATE -- a different timing shape.

    A fix that only tried one STOP position would miss the opposite ground
    truth here: heard late, a phantom STOP means the motor already ran nearly
    to its limit while the model froze near it.
    """
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    entity, hub = await _commanded_untimed_full_close(hass, clocks)
    try:
        started = entity._motion_started_monotonic
        duration = entity._motion_duration
        # STOP heard near the END of travel: the blind is nearly closed.
        clocks.set_monotonic(started + 0.9 * duration)
        dispatch_heard_press(hub, entity._config, "STOP", (1, 2), at=clocks.wall)

        assert entity._suspect is True
        assert entity.position_confidence == "suspect"
        position = entity.current_cover_position
        assert position is not None
        assert position < 50
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_timed_move_interrupted_by_heard_stop_is_not_suspect(
    hass: HomeAssistant,
) -> None:
    """A TIMED move cut short by a heard STOP is not suspect -- it stops near here.

    Only an untimed full travel runs to the motor's own limit; a timed partial
    move was going to stop near the freeze anyway, so the ground truths do not
    diverge and the estimate stays merely `assumed`.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=5.0))
    entity._position = 50.0
    try:
        await entity.async_set_cover_position(**{ATTR_POSITION: 30})
        assert entity._motion_timed

        dispatch_heard_press(hub, entity._config, "STOP", (1, 2), at=cover_module.WALL_CLOCK())

        assert entity._suspect is False
        assert entity.position_confidence == "assumed"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_suspect_survives_a_restart(hass: HomeAssistant) -> None:
    """The doubt must outlive a restart, just as the wrong estimate itself did."""

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    config = cover_config()
    restored_state = State(
        "cover.living_room_left",
        "open",
        {
            ATTR_CURRENT_POSITION: 45,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "role": config.role.value,
            "motion_direction": 0,
            "position_suspect": True,
        },
    )
    hub = ZemismartHub(online_registry(), quiet_publish)
    entity = await attach_cover(
        hass,
        hub,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert entity.current_cover_position == 45
        assert entity._suspect is True
        assert entity.position_confidence == "suspect"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_downtime_completion_earns_no_anchor_and_keeps_suspect(
    hass: HomeAssistant,
) -> None:
    """A travel that finished while HA was down settles nothing (review).

    Nobody was listening while it ran: any press in that gap, real or phantom,
    was invisible. The position lands on the target -- that part is unchanged
    -- but it must not earn `anchored`, and a restored suspect must stay.
    """

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    config = cover_config()
    now = cover_module.WALL_CLOCK()
    restored_state = State(
        "cover.living_room_left",
        "closing",
        {
            ATTR_CURRENT_POSITION: 45,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "role": config.role.value,
            "motion_direction": -1,
            "motion_target": 0.0,
            "motion_started": now - 60.0,
            "motion_deadline": now - 30.0,
            "motion_start_position": 45,
            "motion_bridge": "bridge-a",
            "motion_command_id": "downtime-close",
            "motion_timed": False,
            "motion_absolute_anchor": True,
            "position_suspect": True,
        },
    )
    hub = ZemismartHub(online_registry(), quiet_publish)
    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        # Position completes to the target as before...
        assert entity.current_cover_position == 0
        # ...but an unobserved completion earns no anchor and keeps the doubt.
        assert entity._position_anchored is False
        assert entity._suspect is True
        assert entity.position_confidence == "suspect"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_anchored_deliberately_does_not_survive_a_restart(
    hass: HomeAssistant,
) -> None:
    """A restart downgrades anchored to assumed, by design (review).

    During HA's downtime no RX listener runs, so a physical press in that gap
    is invisible -- restoring `anchored` verbatim would overclaim across
    exactly the window in which the integration was blind. A cover re-earns it
    with its next observed completed travel.
    """

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    config = cover_config()
    restored_state = State(
        "cover.living_room_left",
        "closed",
        {
            ATTR_CURRENT_POSITION: 0,
            "remote": config.remote_key,
            "channels": list(config.channels),
            "role": config.role.value,
            "motion_direction": 0,
            "position_confidence": "anchored",
            "position_suspect": False,
        },
    )
    hub = ZemismartHub(online_registry(), quiet_publish)
    entity = await attach_cover(
        hass,
        hub,
        config=config,
        cover_type=restored_cover_type(restored_state),
    )
    try:
        assert entity.current_cover_position == 0
        assert entity.position_confidence == "assumed"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_suspect_cleared_by_a_completed_endpoint_travel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed hard-limit travel (a reanchor) settles the suspect doubt."""
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    entity, hub = await _commanded_untimed_full_close(hass, clocks)
    try:
        started = entity._motion_started_monotonic
        duration = entity._motion_duration
        clocks.set_monotonic(started + 0.5 * duration)
        dispatch_heard_press(hub, entity._config, "STOP", (1, 2), at=clocks.wall)
        assert entity._suspect is True
        assert entity.position_confidence == "suspect"

        # Reanchor: a fresh full close that runs to the limit clears the doubt.
        await entity.async_reanchor("close")
        assert entity.is_closing
        clocks.set_monotonic(entity._motion_deadline_monotonic + 1.0)
        await asyncio.sleep(0.4)

        assert entity.current_cover_position == 0
        assert entity._suspect is False
        assert entity.position_confidence == "anchored"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_confidence_requires_a_position_before_member_ordering(
    hass: HomeAssistant,
) -> None:
    """Confidence qualifies an estimate before ordering member confidence.

    No aggregate position leaves nothing for `suspect` to qualify, so `unknown`
    is the only honest label. Once every member contributes a position, the
    conservative member order still applies and `suspect` outranks `assumed`.
    """

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=1.0)
    try:
        # All members anchored -> anchored.
        leaf_one._position, leaf_one._position_anchored = 0.0, True
        leaf_two._position, leaf_two._position_anchored = 0.0, True
        assert aggregate.position_confidence == "anchored"

        # One merely assumed drags it down to assumed.
        leaf_two._position_anchored, leaf_two._position = False, 50.0
        assert aggregate.position_confidence == "assumed"

        # With a group position, suspect outranks assumed: leaf_one is assumed,
        # leaf_two suspect, and suspect must win (order matters).
        leaf_one._position_anchored, leaf_one._position = False, 20.0
        leaf_two._suspect = True
        assert aggregate.current_cover_position == 35
        assert aggregate.position_confidence == "suspect"

        # An unknown member withholds the GROUP position. Even though its
        # sibling has a surviving suspect estimate, there is no aggregate
        # estimate for that doubt to qualify, so the group is unknown.
        leaf_one._position, leaf_one._position_anchored = 0.0, True
        leaf_one._suspect = True
        leaf_two._position, leaf_two._suspect = None, False
        assert leaf_two.position_confidence == "unknown"
        assert aggregate.current_cover_position is None
        assert aggregate.position_confidence == "unknown"

        # Restore the missing estimate and suspect surfaces again: it is now
        # qualifying the aggregate's derived position rather than nothing.
        leaf_two._position = 50.0
        assert aggregate.current_cover_position == 25
        assert aggregate.position_confidence == "suspect"

        # No member has a position -> still unknown.
        leaf_one._position, leaf_two._position = None, None
        assert aggregate.position_confidence == "unknown"
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_heard_stop_through_handle_rx_marks_suspect(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident end to end: a genuine STOP, decoded from a real frame and
    delivered through the production RX path while a commanded untimed full
    close runs, leaves the estimate suspect."""
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            acknowledge(hub, topic.split("/")[1], body)

    hub = ZemismartHub(
        online_registry(),
        publish,
        **clocks.as_kwargs(),
    )
    entity = await attach_cover(
        hass,
        hub,
        config=BlindConfig(
            name="Office channel 3",
            remote=cover_config().remote,
            channels=(3,),
            travel_up=100.0,
            travel_down=100.0,
            area_id="living_room",
            repeats=2,
        ),
        entity_id="cover.office_channel_3",
    )
    entity._position = 100.0
    try:
        await entity.async_close_cover()
        assert entity.is_closing
        assert not entity._motion_timed

        # A real STOP heard AFTER our RF started: record_commanded_start (set by
        # the hub on `started`) cannot dismiss it as our own late echo, and its
        # STOP signature matches no armed window, so it is dispatched as a press.
        clocks.advance(2.0)
        stop_frame = encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (3,), "STOP", bases=TEST_ACTION_BASES)
        )
        hub.handle_rx("bridge-peer", {"frame": stop_frame, "t": 0, "boot": 1})

        assert entity._stopped_by_heard is True
        assert entity._suspect is True
        assert entity.position_confidence == "suspect"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_reanchor_entity_service_registers_on_the_platform(
    hass: HomeAssistant,
) -> None:
    """Platform setup wires `reanchor` to the entity method under a live context.

    Locks the positive branch of the context guard in ``async_setup_entry``: it
    must register the entity service (not silently skip it) whenever HA drives
    platform setup with the platform context set.
    """
    from homeassistant.helpers import entity_platform

    registered: list[tuple[str, str]] = []

    class RecordingPlatform:
        def async_register_entity_service(self, name: str, schema: object, func: str) -> None:
            registered.append((name, func))

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)
    runtime = RemoteRuntime(
        remote=RemoteConfig(
            name="Remote",
            remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
            area_id="living_room",
            repeats=2,
        ),
        hub=hub,
    )
    entry = cast(
        "Any",
        SimpleNamespace(
            runtime_data=runtime,
            entry_id="entry-1",
            async_on_unload=lambda _cb: None,
        ),
    )

    token = entity_platform.current_platform.set(cast("Any", RecordingPlatform()))
    try:
        await cover_module.async_setup_entry(hass, entry, lambda *_a, **_k: None)
    finally:
        entity_platform.current_platform.reset(token)
        hub.close()

    assert ("reanchor", "async_reanchor") in registered


@pytest.mark.asyncio
async def test_platform_setup_adds_every_entity_in_one_batch(
    hass: HomeAssistant,
) -> None:
    """All of a remote's covers reach HA through a single add call.

    One call per cover schedules one add pass per cover; a sixteen-cover remote
    paid sixteen of them at every reload.
    """
    from homeassistant.helpers import entity_platform

    added: list[list[object]] = []

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)
    runtime = RemoteRuntime(
        remote=RemoteConfig(
            name="Remote",
            remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
            area_id="living_room",
            repeats=2,
            cover_rows=(
                {
                    "cover_id": "sub-1",
                    "name": "Channel 1",
                    "channels": [1],
                    "travel_up": 4.0,
                    "travel_down": 4.0,
                },
                {
                    "cover_id": "sub-2",
                    "name": "Channel 2",
                    "channels": [2],
                    "travel_up": 4.0,
                    "travel_down": 4.0,
                },
                {"cover_id": "sub-agg", "name": "Both", "channels": [1, 2]},
            ),
        ),
        hub=hub,
    )
    entry = cast(
        "Any",
        SimpleNamespace(
            runtime_data=runtime,
            entry_id="entry-1",
            async_on_unload=lambda _cb: None,
        ),
    )

    def record_add(
        entities: Iterable[Any],
        update_before_add: bool = False,
        **kwargs: Any,
    ) -> None:
        del update_before_add, kwargs
        added.append(list(entities))

    token = entity_platform.current_platform.set(None)
    try:
        await cover_module.async_setup_entry(
            hass,
            entry,
            cast("AddConfigEntryEntitiesCallback", record_add),
        )
    finally:
        entity_platform.current_platform.reset(token)
        hub.close()

    assert len(added) == 1
    assert len(added[0]) == 3


@pytest.mark.asyncio
async def test_failed_add_releases_every_registration(hass: HomeAssistant) -> None:
    """A restore that raises leaves nothing registered on the hub.

    HA logs an entity-add failure without calling
    async_will_remove_from_hass(), so registrations acquired before the restore
    await used to stay on the hub for the lifetime of the entry.
    """

    class ExplodingCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            msg = "restore store unavailable"
            raise RuntimeError(msg)

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    config = cover_config()
    coordinator = RemoteCoordinator(
        hass,
        {"sub-1": models_module.CoverConfig(name="Channel 1", channels=(1,), cover_id="sub-1")},
        "remote-entry",
        _TEST_REMOTE_KEY,
    )
    entity = ExplodingCover("sub-1", "remote-entry", config, hub, coordinator)
    entity.hass = hass
    entity.entity_id = "cover.living_room_left"
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()

    try:
        with pytest.raises(RuntimeError, match="restore store unavailable"):
            await entity.async_added_to_hass()
        # Exactly what EntityPlatform does when add_to_platform_finish raises.
        entity.add_to_platform_abort()

        assert hub.displaced_listeners == []
        assert hub.emission_proof_listeners == []
        assert hub.bridge_listeners == []
        assert hub._rx_listeners == []
        assert coordinator._leaf_entities == {}
        assert coordinator._entity_cover_ids == {}
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_failed_aggregate_add_releases_every_registration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aggregate whose add fails leaves no RX listener or coordinator row."""
    from homeassistant.components import mqtt

    def exploding_subscribe(_hass: HomeAssistant, _callback: Any) -> Callable[[], None]:
        msg = "mqtt dispatcher unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        mqtt,
        "async_subscribe_connection_status",
        exploding_subscribe,
        raising=False,
    )

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    coordinator, _leaf_one, _leaf_two, aggregate_config = aggregate_family(hass, hub)
    entity = cover_module.ZemismartAggregateCover(
        "sub-agg",
        "remote-entry",
        aggregate_config,
        hub,
        coordinator,
    )
    entity.hass = hass
    entity.entity_id = "cover.living_room_group"
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()

    try:
        with pytest.raises(RuntimeError, match="mqtt dispatcher unavailable"):
            await entity.async_added_to_hass()
        entity.add_to_platform_abort()

        assert hub._rx_listeners == []
        assert coordinator._aggregate_entities == {}
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_coordinator_only_subscribes_to_its_own_leaf_entities(
    hass: HomeAssistant,
) -> None:
    """Unrelated state changes never reach the coordinator.

    One unfiltered EVENT_STATE_CHANGED listener per config entry meant every
    state change anywhere in the instance was dispatched into every
    coordinator; the subscription now names the registered leaf entity ids.
    """
    delivered: list[str] = []

    class RecordingCoordinator(RemoteCoordinator):
        def _on_state_changed(self, event: Any) -> None:
            delivered.append(event.data["entity_id"])
            super()._on_state_changed(event)

    coordinator = RecordingCoordinator(
        hass,
        {
            "sub-1": models_module.CoverConfig(name="Channel 1", channels=(1,), cover_id="sub-1"),
            "sub-2": models_module.CoverConfig(name="Channel 2", channels=(2,), cover_id="sub-2"),
            "sub-agg": models_module.CoverConfig(name="Both", channels=(1, 2), cover_id="sub-agg"),
        },
        "remote-entry",
        _TEST_REMOTE_KEY,
    )
    seen: list[str] = []

    class RecordingAggregate:
        def async_write_ha_state(self) -> None:
            seen.append("flush")

    leaf = cast(
        "Any",
        SimpleNamespace(entity_id="cover.leaf_one", async_write_ha_state=lambda: None),
    )
    coordinator.register_aggregate("sub-agg", cast("Any", RecordingAggregate()))

    try:
        # No leaf registered yet: nothing to listen to, so nothing is installed.
        assert coordinator._unsub_state_changed is None

        coordinator.register_leaf("sub-1", leaf)
        assert coordinator._unsub_state_changed is not None
        # Registration itself marks the container dirty; let that flush land
        # before measuring what the subscription delivers.
        await hass.async_block_till_done()
        seen.clear()
        delivered.clear()

        hass.states.async_set("light.unrelated", "on")
        await hass.async_block_till_done()
        # Not merely filtered out in Python: never dispatched here at all.
        assert delivered == []
        assert seen == []

        hass.states.async_set("cover.leaf_one", "open")
        await hass.async_block_till_done()
        assert delivered == ["cover.leaf_one"]
        assert seen == ["flush"]

        # Unregistering the last leaf drops the subscription entirely.
        coordinator.unregister_leaf("sub-1")
        assert coordinator._unsub_state_changed is None
        # Unregistration re-derives the container too; flush that first.
        await hass.async_block_till_done()
        seen.clear()
        hass.states.async_set("cover.leaf_one", "closed")
        await hass.async_block_till_done()
        assert seen == []
    finally:
        coordinator.detach()


@pytest.mark.asyncio
async def test_motion_internals_are_excluded_from_the_recorder(hass: HomeAssistant) -> None:
    """Motion/anchor internals are unrecorded but stay in the state machine.

    Restore reads the state machine, not the recorder, so excluding them costs
    nothing at restart while dropping twelve of seventeen attributes from every
    travel-rate history row.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub)
    # attach_cover stops short of the platform's own add bookkeeping, and HA
    # drops state writes from an entity that is not ADDED.
    try:
        entity._position = 40.0
        entity.async_write_ha_state()
        state = hass.states.get("cover.living_room_left")
        assert state is not None
        assert state.state_info is not None
        unrecorded = state.state_info["unrecorded_attributes"]

        assert unrecorded == {
            "motion_started",
            "motion_deadline",
            "motion_start_position",
            "motion_bridge",
            "motion_command_id",
            "motion_timed",
            "motion_absolute_anchor",
            "motion_direction",
            "motion_target",
            "unverified_anchor_bridge",
            "unverified_anchor_command_id",
            "unverified_anchor_offline",
        }
        # The signals a user or an automation looks back at stay recorded.
        assert unrecorded.isdisjoint(
            {
                "channels",
                "remote",
                "role",
                "position_confidence",
                "last_bridge",
                "degraded_bridge",
                "position_suspect",
            }
        )
        # Excluded from history only: restore still sees every one of them.
        assert set(state.attributes) >= unrecorded
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_intermediate_progress_writes_are_throttled(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A travel integrates at 0.25 s but reaches the state machine at ~1 s.

    The tick rate is what makes the position smooth; the recorder gained
    nothing from 4 Hz history of a dead-reckoned estimate (~120 rows of 17
    attributes per 30 s travel, per cover).
    """
    clocks = SteppableClocks()
    patch_cover_clocks(monkeypatch, clocks)

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        # Patching the GLOBAL asyncio.sleep means every sleeper in the
        # process comes through here — HA internals included. Only the travel
        # loop's own pacing sleeps (min(interval, remaining), always in
        # (0, POSITION_UPDATE_INTERVAL_SECONDS]) may advance the shared fake
        # clock; letting unrelated sleepers advance it made the throttle
        # window count depend on runner scheduling and flake on slow CI.
        if 0 < seconds <= POSITION_UPDATE_INTERVAL_SECONDS:
            clocks.advance(seconds)
        await real_sleep(0)

    # cover.py calls asyncio.sleep through the module, so patching it here is
    # what the travel loop actually sees.
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=10.0))
    ticks: list[float] = []
    writes: list[float] = []

    original_sync = entity._sync_position

    def counting_sync(at: float | None = None) -> None:
        ticks.append(clocks.monotonic)
        original_sync(at)

    monkeypatch.setattr(entity, "_sync_position", counting_sync)

    unsub = async_track_state_change_event(
        hass,
        ["cover.living_room_left"],
        lambda _event: writes.append(clocks.monotonic),
    )
    try:
        entity._position = 0.0
        entity._direction = 1
        entity._motion_started_monotonic = clocks.monotonic
        entity._motion_start_position = 0.0
        entity._motion_target = 100.0
        entity._motion_duration = 10.0
        entity._motion_deadline_monotonic = clocks.monotonic + 10.0
        entity._create_motion_task("throttle test")
        task = entity._motion_task
        assert task is not None
        await task
        # The write counter is a bus listener: state-changed events are
        # dispatched via the event loop, not synchronously from
        # async_write_ha_state, so on a slow runner the tail of the events
        # is still queued when the task completes. Drain before counting.
        await hass.async_block_till_done()

        # 10 s of travel at the 0.25 s integration interval.
        assert len(ticks) == 40
        # ~1 write/second of travel plus the completion write, not 41.
        assert 10 <= len(writes) <= 12
        assert entity.current_cover_position == 100

        # The count alone does NOT pin the completion write: ten throttled
        # progress writes already satisfy the range above, so deleting the
        # settle write left this green while HA stayed stuck on `opening` for
        # good. Assert the FINAL state separately -- that is the one write whose
        # absence a user actually sees.
        final = hass.states.get("cover.living_room_left")
        assert final is not None
        assert final.state == "open"
        assert final.attributes["current_position"] == 100
        assert final.attributes["motion_direction"] == 0
        assert final.attributes["motion_target"] is None
        assert final.attributes["motion_deadline"] is None
    finally:
        unsub()
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_programmer_defect_in_the_command_path_keeps_its_traceback(
    hass: HomeAssistant,
) -> None:
    """A TypeError is not a bridge failure and must not be reported as one.

    `except Exception` turned every defect into a bland service failure with no
    traceback and a `degraded` flag misattributing the cause to the bridge.
    """

    async def publish(_topic: str, _payload: str) -> None:
        # Stands in for a defect anywhere below the transmit call.
        raise TypeError("'NoneType' object is not subscriptable")

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub)
    try:
        with pytest.raises(TypeError):
            await entity.async_open_cover()

        assert entity.extra_state_attributes["degraded_bridge"] is False
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_unknown_position_partial_move_is_a_validation_error(
    hass: HomeAssistant,
) -> None:
    """Asking for a partial move with no estimate is bad input, not a failure."""

    async def publish(topic: str, payload: str) -> None:
        acknowledge(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub)
    try:
        entity._position = None
        with pytest.raises(ServiceValidationError) as unknown:
            await entity.async_set_cover_position(**{ATTR_POSITION: 60})
        assert unknown.value.translation_key == "position_unknown"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_with_an_unrepresented_channel_reports_nothing(
    hass: HomeAssistant,
) -> None:
    """A group whose members do not cover all its channels has no position.

    The laminar topology permits it: `members_of` returns the live configured
    leaves strictly inside the aggregate, and nothing requires their union to
    equal the aggregate's own channels. `async_setup_entry` also skips any cover
    whose config fails to derive, so a member can go missing at runtime.

    Before #32 the group averaged the members it had and published a confident
    number -- possibly `anchored` -- for hardware it had no model of.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    # Leaves {1,2} and {3} under a group of {1,2,3,4}: channel 4 has no model.
    wide, solo, aggregate = await attach_weighted_family(hass, hub)
    aggregate._config = replace(aggregate._config, channels=(1, 2, 3, 4))
    try:
        wide._position = 50.0
        solo._position = 50.0
        wide.async_write_ha_state()
        solo.async_write_ha_state()
        await hass.async_block_till_done()

        assert aggregate.current_cover_position is None
        # `unknown`, not merely "not anchored": there is no position at all in
        # this state, and an automation gating on `!= 'unknown'` must not be
        # told otherwise.
        assert aggregate.position_confidence == "unknown"

        with pytest.raises(HomeAssistantError) as incomplete:
            await aggregate.async_set_cover_position(**{ATTR_POSITION: 60})
        assert incomplete.value.translation_key == "aggregate_incomplete"
        assert "4" in (incomplete.value.translation_placeholders or {})["missing"]
        # and the members it DID have were not moved
        assert wide._motion_target is None
        assert solo._motion_target is None
    finally:
        await detach_family(wide, solo, aggregate)


@pytest.mark.asyncio
async def test_programmer_defect_in_an_aggregate_command_keeps_its_traceback(
    hass: HomeAssistant,
) -> None:
    """The aggregate's transmit must not launder a defect either.

    The leaf-side version of this test left the aggregate's own handler
    unpinned: reverting `ZemismartAggregateCover._async_transmit` to
    `except Exception` kept the suite green, so the narrowing there was
    protected by nothing (#37).
    """

    async def publish(_topic: str, _payload: str) -> None:
        # Stands in for a defect anywhere below the transmit call.
        raise TypeError("'NoneType' object is not subscriptable")

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        with pytest.raises(TypeError):
            await aggregate.async_open_cover()
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_timeout_invalidates_every_member(hass: HomeAssistant) -> None:
    """A group command that never reports `started` leaves no confident member.

    The leaf invalidates itself on a timeout; the aggregate has to do it for its
    members, because the command was never any member's own. Without it
    `_async_move_full` never starts member tracking, `async_stop_cover` never
    freezes it, and members keep integrating -- possibly through a STOP that did
    fire -- while still reporting `anchored`.
    """

    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        # ACCEPTED but never started: this is the CommandStartedTimeoutError
        # path specifically. Acknowledging neither only ever reaches
        # CommandAckTimeoutError, so removing StartedTimeout from the handler
        # would have left this test green.
        body = json.loads(payload)
        hub.handle_status(
            topic.split("/")[1],
            {"status": "accepted", "command_id": body["command_id"]},
        )

    hub = ZemismartHub(online_registry(), publish, ack_timeout=0.5, started_timeout=0.01)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        leaf_one._position = 40.0
        leaf_one._position_anchored = True
        leaf_two._position = 40.0
        leaf_two._position_anchored = True

        with pytest.raises(HomeAssistantError) as timeout:
            await aggregate.async_open_cover()
        assert timeout.value.translation_key == "command_timeout"
        assert not timeout.value.translation_placeholders

        for member in (leaf_one, leaf_two):
            assert member.current_cover_position is None
            assert member.position_confidence == "unknown"
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_timeout_records_position_tombstones(
    hass: HomeAssistant,
) -> None:
    """A timeout persists a marker for an addressed leaf that is not live."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(
        online_registry(),
        publish,
        ack_timeout=0.01,
        started_timeout=0.01,
    )
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        aggregate._coordinator.unregister_leaf("sub-2")
        with pytest.raises(HomeAssistantError):
            await aggregate.async_open_cover()

        coordinator = aggregate._coordinator
        assert not coordinator.has_position_invalidation("remote-entry", "sub-1")
        assert coordinator.has_position_invalidation("remote-entry", "sub-2")
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_incomplete_aggregate_is_never_reported_closed(hass: HomeAssistant) -> None:
    """`closed` is the entity's PRIMARY state and needs full channel coverage.

    position and confidence already refuse an incomplete group, but `is_closed`
    did not -- so an aggregate over {1,2,3,4} whose live members cover {1,2,3}
    published itself to HA as `closed` with channel 4 entirely unmodelled. The
    other incomplete-group test uses mid-travel positions and never reaches this
    state.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    wide, solo, aggregate = await attach_weighted_family(hass, hub)
    aggregate._config = replace(aggregate._config, channels=(1, 2, 3, 4))
    try:
        wide._position = 0.0
        solo._position = 0.0
        assert wide.is_closed is True
        assert solo.is_closed is True

        assert aggregate.is_closed is None, "an unmodelled channel cannot be called closed"

        # One member open still makes the GROUP open, coverage or not: that
        # answer does not depend on the channels we have no model for.
        solo._position = 60.0
        assert aggregate.is_closed is False
    finally:
        await detach_family(wide, solo, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_queue_full_surfaces_as_a_translated_error(hass: HomeAssistant) -> None:
    """The outstanding-work cap must not leak a raw RuntimeError to the user.

    `CommandQueueFullError` derives from RuntimeError, and the cover boundaries
    listed only the other defined transport failures -- so once the cap was
    reached the next movement surfaced an untranslated traceback rather than a
    service error, despite the exception's own docstring claiming otherwise
    (#34).
    """
    from custom_components.zemismart_blinds import models as models_module

    async def publish(_topic: str, _payload: str) -> None:
        # Never acknowledged: everything stays outstanding.
        return

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub)
    raw = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (7,), "UP", bases=TEST_ACTION_BASES))
    floods = [
        asyncio.create_task(hub.async_send_raw("bridge-a", raw, 1))
        for _ in range(models_module._MAX_OUTSTANDING_COMMANDS)
    ]
    try:
        for _ in range(5):
            await asyncio.sleep(0)

        with pytest.raises(HomeAssistantError) as full:
            await entity.async_open_cover()
        assert not isinstance(full.value, asyncio.CancelledError)
        assert full.value.translation_key == "queue_full"
        assert entity.extra_state_attributes["degraded_bridge"] is True
    finally:
        for task in floods:
            task.cancel()
        await asyncio.gather(*floods, return_exceptions=True)
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_queue_full_is_translated_at_the_aggregate_boundary_too(
    hass: HomeAssistant,
) -> None:
    """Both cover boundaries translate the cap error, not just the leaf.

    The leaf-only version left the aggregate's tuple unpinned: removing
    CommandQueueFullError from it kept the suite green while a saturated queue
    surfaced a raw RuntimeError from any group command (#34).
    """
    from custom_components.zemismart_blinds import models as models_module

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    raw = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (7,), "UP", bases=TEST_ACTION_BASES))
    floods = [
        asyncio.create_task(hub.async_send_raw("bridge-a", raw, 1))
        for _ in range(models_module._MAX_OUTSTANDING_COMMANDS)
    ]
    try:
        for _ in range(5):
            await asyncio.sleep(0)

        with pytest.raises(HomeAssistantError) as full:
            await aggregate.async_open_cover()
        assert full.value.translation_key == "queue_full"
    finally:
        for task in floods:
            task.cancel()
        await asyncio.gather(*floods, return_exceptions=True)
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_no_online_bridge_is_translated_at_both_boundaries(
    hass: HomeAssistant,
) -> None:
    """Losing every bridge must surface a translated error, not a RuntimeError.

    `queue_full` was pinned at both boundaries but the other four defined
    transport failures were not, and two of them were pinned NOWHERE: deleting
    `NoOnlineBridgeError` and `CommandRejectedError` from the caught set left
    the whole suite green while a remote with no reachable bridge raised a raw
    RuntimeError straight through the service layer.
    """

    async def publish(_topic: str, _payload: str) -> None:
        raise AssertionError("nothing may be published without an online bridge")

    # Same-area bridge that has gone offline: the registry knows it, so this is
    # the real deployment shape (a bridge that dropped) rather than an empty
    # registry that never had one.
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "offline")

    hub = ZemismartHub(registry, publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        with pytest.raises(HomeAssistantError) as leaf_failure:
            await leaf_one.async_open_cover()
        assert leaf_failure.value.translation_key == "no_bridge_online"
        assert leaf_one.extra_state_attributes["degraded_bridge"] is True

        with pytest.raises(HomeAssistantError) as aggregate_failure:
            await aggregate.async_open_cover()
        assert aggregate_failure.value.translation_key == "no_bridge_online"
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", list(cover_module._TRANSPORT_FAILURE_KEYS))
async def test_every_defined_transport_failure_is_translated_at_both_boundaries(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    """Every type in the mapping must be caught and translated by BOTH covers.

    Parametrized over the mapping itself, so a type added there is pinned the
    moment it is added rather than whenever someone remembers to write a test.

    This drives the failure in through the hub rather than through a real
    broker, which is deliberately weaker than the neighbouring tests that lose
    a bridge or take a NACK for real -- those stay. What this one adds is
    TOTALITY, and totality is what was missing: dropping ValueError from the
    caught set left all 853 tests green.

    ValueError has no KNOWN route from a cover command today -- async_transmit
    rejects a non-positive stop_after_ms, but the cover clamps it to at least 1
    before calling (an earlier version of this docstring claimed that route was
    live; it is not). It stays pinned anyway, because the mapping is what both
    boundaries derive their caught set from: an entry that is mapped but
    untested is one refactor away from being mapped but uncaught, and the
    failure would be a raw RuntimeError reaching a user through the service
    layer.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    async def failing_transmit(*_args: Any, **_kwargs: Any) -> None:
        raise failure_type("synthetic transport failure")

    expected = cover_module._TRANSPORT_FAILURE_KEYS[failure_type]
    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    monkeypatch.setattr(hub, "async_transmit", failing_transmit)
    try:
        for entity in (leaf_one, aggregate):
            with pytest.raises(HomeAssistantError) as failure:
                await entity.async_open_cover()
            assert failure.value.translation_key == expected, (
                f"{type(entity).__name__} did not translate {failure_type.__name__}"
            )
        # Uniform across all five at the leaf, because they share one handler:
        # every defined transport failure also means the bridge is degraded.
        # The aggregate owns no such flag -- its members carry the state.
        assert leaf_one.extra_state_attributes["degraded_bridge"] is True
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_bridge_rejection_is_translated_at_both_boundaries(
    hass: HomeAssistant,
) -> None:
    """A bridge NACK must reach the user as its own translated message.

    The other half of the gap above: `CommandRejectedError` was caught by both
    transmit paths but asserted by neither, so the rejection path could stop
    being translated without a single test noticing.
    """
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status(
            topic.split("/")[1],
            {
                "status": "rejected",
                "command_id": body["command_id"],
                "reason": "unsupported frame",
            },
        )

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        with pytest.raises(HomeAssistantError) as leaf_failure:
            await leaf_one.async_open_cover()
        assert leaf_failure.value.translation_key == "command_rejected"
        assert leaf_one.extra_state_attributes["degraded_bridge"] is True

        with pytest.raises(HomeAssistantError) as aggregate_failure:
            await aggregate.async_open_cover()
        assert aggregate_failure.value.translation_key == "command_rejected"
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_cancellation_survives_a_restore_still_in_flight(hass: HomeAssistant) -> None:
    """A pending restore must not put a confident position back over a cancel.

    This is the exact round-one defect the `_restore_epoch` bumps were added
    for, and none of the existing cancellation tests could see it: they all
    await fully completed attachment, so the restore has already run and there
    is nothing left to race. Deleting both `_restore_epoch += 1` statements left
    every one of them green.

    Leaves register their listeners BEFORE awaiting restored state, so a command
    can be issued, published and cancelled while `_async_restore_state` is still
    suspended -- and when it resumes, its guard compares the epoch it captured
    on entry.
    """
    release = asyncio.Event()
    restored = State(
        "cover.living_room_left",
        "open",
        {
            ATTR_CURRENT_POSITION: 70,
            "remote": f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}",
            "channels": [1, 2],
            "role": "leaf",
        },
    )

    class SlowRestoreCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            await release.wait()
            return restored

    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        # Published, never acknowledged: the caller is cancelled mid-lifecycle,
        # which is exactly when the frame may already be on air.
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    entity = SlowRestoreCover("cover-slow", "entry-slow", cover_config(), hub)
    entity.hass = hass
    entity.entity_id = "cover.living_room_left"
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()
    adding = hass.async_create_task(entity.async_added_to_hass())
    try:
        for _ in range(3):
            await asyncio.sleep(0)
        assert not adding.done(), "the restore must still be suspended"

        # A REAL command, published and then cancelled, while the restore is
        # still suspended. Calling invalidate_for_cancelled_command() directly
        # would pin only the helper's own bump and leave the leaf's
        # CancelledError handler unpinned.
        moving = hass.async_create_task(entity.async_open_cover())
        await published.wait()
        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving
        assert entity.current_cover_position is None

        release.set()
        await adding
        await hass.async_block_till_done()

        assert entity.current_cover_position is None, (
            "the pending restore overwrote an invalidation it is older than"
        )
        assert entity.position_confidence == "unknown"
    finally:
        release.set()
        with suppress(Exception):
            await adding
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_cancellation_survives_a_members_pending_restore(
    hass: HomeAssistant,
) -> None:
    """The helper's own epoch bump is pinned, not just the leaf handler's.

    Rewriting the leaf race test to drive a real cancelled command pinned
    `cover.py`'s CancelledError bump but removed the only exercise of
    `invalidate_for_cancelled_command()`, unpinning the bump inside it -- a gap
    opened by closing another one.

    The aggregate is the caller that matters here: its frame addresses the whole
    channel set, so a member that never issued the command is invalidated
    through the helper, and a member whose restore is still suspended would
    otherwise put its cached confident position straight back.
    """
    release = asyncio.Event()
    published = asyncio.Event()
    restored = State(
        "cover.member_slow",
        "open",
        {
            ATTR_CURRENT_POSITION: 80,
            "remote": f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}",
            "channels": [1],
            "role": "leaf",
        },
    )

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        # leaf_one's restore is still in flight when the group frame is cancelled.
        leaf_one._restore_epoch = 0
        guard = (leaf_one._intent_generation, leaf_one._restore_epoch)
        pending = hass.async_create_task(leaf_one._async_restore_state(guard))

        async def slow_last_state() -> State:
            await release.wait()
            return restored

        leaf_one.async_get_last_state = slow_last_state  # type: ignore[method-assign]
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        pending = hass.async_create_task(leaf_one._async_restore_state(guard))
        for _ in range(3):
            await asyncio.sleep(0)

        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()
        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        assert leaf_one.current_cover_position is None

        release.set()
        await pending
        await hass.async_block_till_done()

        assert leaf_one.current_cover_position is None, (
            "a member's pending restore overwrote an invalidation it is older than"
        )
    finally:
        release.set()
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_cover_registration_supplies_bases_to_the_hub(hass: HomeAssistant) -> None:
    """The bases reach the hub through cover.py's OWN registration call.

    The RX regressions in test_models.py call `register_rx_listener(bases=...)`
    by hand, so deleting `bases=self._config.remote.bases` from both cover
    registration sites left the whole suite green -- inert production wiring
    behind a passing test, which is the exact defect class this round began with
    (#30).
    """
    from tests.synthetic import UNTABLED_BASES, UNTABLED_PREFIX, UNTABLED_REMOTE_ID

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    config = replace(
        cover_config(),
        remote=RemoteIdentity(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, UNTABLED_BASES),
        channels=(1,),
    )
    entity = await attach_cover(hass, hub, config=config)
    entity._position = 50.0
    try:
        frame = encode_b0(
            make_payload(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1,), "UP", bases=UNTABLED_BASES)
        )
        hub.handle_rx("bridge-a", {"frame": frame, "t": 1_000, "boot": 7})

        # Observed through the entity's own model, not a patched callback: the
        # listener the hub holds was bound at registration, so replacing the
        # method afterwards would test nothing. Dispatch is synchronous all the
        # way from handle_rx into _start_heard_motion, so no waiting is needed
        # -- and waiting would only run the travel to completion.
        assert entity.is_opening, (
            "an untabled remote's press only classifies if the entity handed its bases over"
        )
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_cancellation_still_invalidates_after_a_heard_stop_with_no_motion(
    hass: HomeAssistant,
) -> None:
    """A STOP that stopped nothing does not earn the position it leaves behind.

    The companion test covers a heard DOWN, which installs a replacement model
    and rightly survives the cancellation. A heard STOP arriving before the
    command's result has committed any motion is different: `_apply_stop` finds
    nothing tracked and simply preserves the OLD estimate, which predates the
    frame we already published. The blind may have moved.

    Keying the skip on `_intent_generation` -- which every intersecting press
    bumps, usable model or not -- stranded exactly that unearned position.
    """
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=10.0))
    entity._position = 50.0
    try:
        moving = hass.async_create_task(entity.async_open_cover())
        await published.wait()
        assert entity._direction == 0, "no motion is tracked yet: the ack has not landed"

        entity._on_heard_press(
            HeardEvent(
                button="STOP",
                chans=frozenset(entity._config.channels),
                remote_key=entity._config.remote_key,
                heard_at=cover_module.WALL_CLOCK(),
                heard_at_monotonic=cover_module.MONOTONIC_CLOCK(),
                bridge_id="bridge-a",
            )
        )
        assert entity.current_cover_position == 50, "the STOP left the old estimate in place"

        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        assert entity.current_cover_position is None, (
            "a STOP that stopped nothing cannot save a position from invalidation"
        )
        assert entity.position_confidence == "unknown"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_cancellation_invalidates_even_when_a_press_was_heard(
    hass: HomeAssistant,
) -> None:
    """Cancellation after publication is unknown, press or no press.

    Two rejected attempts are pinned here, because the tempting behaviour is
    wrong in a way that only shows up on the RF timeline.

    Cancellation invalidates after ANY intervening press. Sparing a cover whose
    model a press has just replaced looks right -- the press seems newer than the
    cancelled command -- but it is not, because cancellation says nothing about
    RF ORDERING. The hub deliberately keeps an
    already-published command alive after its caller is cancelled, so the bridge
    may first-dispatch it AFTER the press was heard -- with no cover task left to
    observe the result. The blind then moves under a command nobody is tracking
    while the cover confidently reports what the press installed.

    Waiting on the publisher, as this test does, is entry to the MQTT publish --
    NOT the `started` status that is this project's proof of first dispatch. No
    generation or revision counter available here can express the ordering that
    would make preservation safe.
    """
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    entity = await attach_cover(hass, hub, config=cover_config(travel=10.0))
    entity._position = 50.0
    try:
        moving = hass.async_create_task(entity.async_open_cover())
        await published.wait()

        entity._on_heard_press(
            HeardEvent(
                button="DOWN",
                chans=frozenset(entity._config.channels),
                remote_key=entity._config.remote_key,
                heard_at=cover_module.WALL_CLOCK(),
                heard_at_monotonic=cover_module.MONOTONIC_CLOCK(),
                bridge_id="bridge-a",
            )
        )
        assert entity.is_closing, "the press installed a model of its own"
        # ...which is nonetheless discarded below. There is no "the press wins"
        # case: see the companion STOP test for the other half of the rule.

        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        assert entity.current_cover_position is None, (
            "the published UP may still dispatch after the press; only unknown is honest"
        )
        assert entity.position_confidence == "unknown"
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_cancellation_invalidates_a_member_that_joined_late(
    hass: HomeAssistant,
) -> None:
    """A member registered DURING the group transmit is invalidated too.

    Snapshotting members before the await missed them. A leaf registers with the
    coordinator before awaiting its restore, and entities are added in stored
    order -- so an automation can drive an already-added aggregate while a later
    leaf is still registering. The group frame addresses that leaf's channel, so
    leaving it out of the invalidation lets its pending restore publish a stale
    specific position for hardware the frame just moved.
    """
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        leaf_one._position = 40.0
        leaf_two._position = 40.0
        # leaf_two is absent when the frame goes out, and rejoins mid-flight.
        aggregate._coordinator.unregister_leaf(leaf_two._cover_id)

        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()
        aggregate._coordinator.register_leaf(leaf_two._cover_id, leaf_two)

        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        assert leaf_two.current_cover_position is None, (
            "a member that joined during the transmit was left with a stale position"
        )
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_timeout_survives_a_restore_still_in_flight(hass: HomeAssistant) -> None:
    """A command that times out cannot be undone by a pending restore either.

    HA inserts an entity into the entity-service mapping BEFORE awaiting
    `async_added_to_hass`, so an available cover can be commanded while its own
    restore is still suspended. The cancellation path bumps `_restore_epoch` for
    exactly this reason; the timeout path marked unknown without it, so the
    restore resumed, passed its guard, and reinstalled the cached position --
    reporting a specific estimate after a command that may have reached the air.
    """
    release = asyncio.Event()
    published = asyncio.Event()
    restored = State(
        "cover.living_room_left",
        "open",
        {
            ATTR_CURRENT_POSITION: 70,
            "remote": f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}",
            "channels": [1, 2],
            "role": "leaf",
        },
    )

    class SlowRestoreCover(ZemismartCover):
        async def async_get_last_state(self) -> State:
            await release.wait()
            return restored

    async def publish(_topic: str, _payload: str) -> None:
        # Published, never acknowledged: this is the ack-timeout path.
        published.set()

    hub = ZemismartHub(online_registry(), publish, ack_timeout=0.01, started_timeout=0.01)
    entity = SlowRestoreCover("cover-timeout", "entry-timeout", cover_config(), hub)
    entity.hass = hass
    entity.entity_id = "cover.living_room_left"
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()
    adding = hass.async_create_task(entity.async_added_to_hass())
    try:
        for _ in range(3):
            await asyncio.sleep(0)
        assert not adding.done(), "the restore must still be suspended"

        with pytest.raises(HomeAssistantError):
            await entity.async_open_cover()
        assert published.is_set()
        assert entity.current_cover_position is None

        release.set()
        await adding
        await hass.async_block_till_done()

        assert entity.current_cover_position is None, (
            "the pending restore reinstated a position after a command timed out"
        )
    finally:
        release.set()
        with suppress(Exception):
            await adding
        await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_cancellation_invalidates_a_member_that_left_mid_flight(
    hass: HomeAssistant,
) -> None:
    """A member that deregistered during the group transmit is invalidated too.

    The mirror of the late-joining case. Iterating only CURRENT members at
    failure time misses a leaf that unloaded while the frame was awaiting
    `accepted`/`started`: it is gone from `_members()`, but the frame that
    addressed its channels is already on air, so its known position survives and
    a later reload can restore it as though nothing happened.
    """
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    try:
        leaf_one._position = 40.0
        leaf_two._position = 40.0

        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()
        # leaf_two unloads while the group frame is in flight.
        aggregate._coordinator.unregister_leaf(leaf_two._cover_id)
        assert leaf_two not in aggregate._members()

        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        assert leaf_two.current_cover_position is None, (
            "a member that left during the transmit kept a position the frame may have moved"
        )
        assert leaf_one.current_cover_position is None
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_cancellation_tombstones_a_configured_leaf_that_was_never_live(
    hass: HomeAssistant,
) -> None:
    """A configured leaf absent from HA cannot later restore pre-frame certainty."""
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    coordinator, leaf_one_config, leaf_two_config, aggregate_config = aggregate_family(hass, hub)
    leaf_one = ZemismartCover("sub-1", "remote-entry", leaf_one_config, hub, coordinator)
    aggregate = cover_module.ZemismartAggregateCover(
        "sub-agg", "remote-entry", aggregate_config, hub, coordinator
    )
    late_leaf: ZemismartCover | None = None
    await attach_family_entity(hass, leaf_one, "cover.channel_1")
    await attach_family_entity(hass, aggregate, "cover.both")
    try:
        leaf_one._position = 40.0
        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()
        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        # A platform reload rebuilds the coordinator; only the hass.data store
        # spans that replacement.
        replacement_coordinator, _, _, _ = aggregate_family(hass, hub)
        restored = restored_leaf_state(leaf_two_config)
        late_leaf = restored_cover_type(restored)(
            "sub-2",
            "remote-entry",
            leaf_two_config,
            hub,
            replacement_coordinator,
        )
        await attach_family_entity(hass, late_leaf, "cover.channel_2")

        assert late_leaf.current_cover_position is None
        assert late_leaf.position_confidence == "unknown"
    finally:
        if late_leaf is not None:
            await late_leaf.async_will_remove_from_hass()
        await detach_family(leaf_one, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_tombstone_recorded_during_restore_await_is_honoured(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old coordinator can tombstone a new coordinator's suspended restore."""
    entered_restore = asyncio.Event()
    release_restore = asyncio.Event()

    class SlowRestoreCover(ZemismartCover):
        """Expose a distinct restore getter for this race."""

    async def slow_last_state(_entity: SlowRestoreCover) -> State:
        entered_restore.set()
        await release_restore.wait()
        return restored

    monkeypatch.setattr(SlowRestoreCover, "async_get_last_state", slow_last_state)

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    old_coordinator, _, leaf_two_config, _ = aggregate_family(hass, hub)
    replacement_coordinator, _, _, _ = aggregate_family(hass, hub)
    restored = restored_leaf_state(leaf_two_config)
    replacement = SlowRestoreCover(
        "sub-2",
        "remote-entry",
        leaf_two_config,
        hub,
        replacement_coordinator,
    )
    replacement.hass = hass
    replacement.entity_id = "cover.channel_2"
    replacement.platform = platform_stub()
    await replacement.async_internal_added_to_hass()
    adding = hass.async_create_task(replacement.async_added_to_hass())
    try:
        await entered_restore.wait()
        old_coordinator.record_position_invalidations("remote-entry", (2,))
        assert replacement_coordinator.has_position_invalidation(
            "remote-entry",
            "sub-2",
        )

        release_restore.set()
        await adding

        assert replacement.current_cover_position is None
        assert replacement.position_confidence == "unknown"
        assert replacement_coordinator.has_position_invalidation(
            "remote-entry",
            "sub-2",
        )

        replacement._platform_state = EntityPlatformState.ADDED
        replacement.async_write_ha_state()

        assert not replacement_coordinator.has_position_invalidation(
            "remote-entry",
            "sub-2",
        )
    finally:
        release_restore.set()
        with suppress(Exception):
            await adding
        await replacement.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_late_stale_coordinator_tombstone_invalidates_live_replacement(
    hass: HomeAssistant,
) -> None:
    """A late failure reaches the replacement that already finished restore."""

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)
    stale_coordinator, _, leaf_two_config, _ = aggregate_family(hass, hub)
    current_coordinator, _, _, _ = aggregate_family(hass, hub)
    restored = restored_leaf_state(leaf_two_config)
    replacement = restored_cover_type(restored)(
        "sub-2",
        "remote-entry",
        leaf_two_config,
        hub,
        current_coordinator,
    )
    await attach_family_entity(hass, replacement, "cover.channel_2")
    try:
        assert replacement.current_cover_position == 40
        assert replacement.position_confidence == "assumed"

        stale_coordinator.record_position_invalidations("remote-entry", (2,))

        written = hass.states.get("cover.channel_2")
        assert replacement.current_cover_position is None
        assert replacement.position_confidence == "unknown"
        assert written is not None
        assert written.state == "unknown"
        assert not current_coordinator.has_position_invalidation(
            "remote-entry",
            "sub-2",
        )
    finally:
        await replacement.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_live_invalidation_write_retires_marker_before_later_restore(
    hass: HomeAssistant,
) -> None:
    """A successful live unknown write cannot poison newer earned history."""

    async def quiet_publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), quiet_publish)
    coordinator, _, leaf_two_config, _ = aggregate_family(hass, hub)
    restored = restored_leaf_state(leaf_two_config)
    entity = restored_cover_type(restored)(
        "sub-2",
        "remote-entry",
        leaf_two_config,
        hub,
        coordinator,
    )
    await attach_family_entity(hass, entity, "cover.channel_2")
    replacement: ZemismartCover | None = None
    try:
        coordinator.unregister_leaf("sub-2")
        coordinator.record_position_invalidations("remote-entry", (2,))
        assert coordinator.has_position_invalidation("remote-entry", "sub-2")
        coordinator.register_leaf("sub-2", entity)

        entity.invalidate_for_cancelled_command()

        written_unknown = hass.states.get("cover.channel_2")
        assert written_unknown is not None
        assert written_unknown.state == "unknown"
        assert not coordinator.has_position_invalidation("remote-entry", "sub-2")

        entity._position = 65.0
        entity.async_write_ha_state()
        persisted = hass.states.get("cover.channel_2")
        assert persisted is not None
        assert persisted.attributes[ATTR_CURRENT_POSITION] == 65

        await entity.async_will_remove_from_hass()
        replacement = restored_cover_type(persisted)(
            "sub-2",
            "remote-entry",
            leaf_two_config,
            hub,
            coordinator,
        )
        await attach_family_entity(hass, replacement, "cover.channel_2")

        assert replacement.current_cover_position == 65
        assert replacement.position_confidence == "assumed"
    finally:
        if replacement is not None:
            await replacement.async_will_remove_from_hass()
        else:
            await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_failure_invalidates_each_current_member_once(
    hass: HomeAssistant,
) -> None:
    """Shared dispatch and issued-member fallback must not double-invalidate."""
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    epochs = {
        leaf_one: leaf_one._restore_epoch,
        leaf_two: leaf_two._restore_epoch,
    }
    try:
        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()
        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        assert leaf_one._restore_epoch == epochs[leaf_one] + 1
        assert leaf_two._restore_epoch == epochs[leaf_two] + 1
        assert leaf_one.current_cover_position is None
        assert leaf_two.current_cover_position is None
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_failure_isolates_member_write_and_runs_fallback(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failed live write cannot block later dispatch or issued-member fallback."""
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    remote = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES)
    channels = (1, 2, 3, 4)
    covers = {
        cover_id: models_module.CoverConfig(
            name=f"Channel {channel}",
            channels=(channel,),
            travel_up=1.0,
            travel_down=1.0,
            cover_id=cover_id,
        )
        for channel, cover_id in zip(
            channels,
            ("sub-1", "sub-2", "sub-3", "sub-4"),
            strict=True,
        )
    }
    covers["sub-agg"] = models_module.CoverConfig(
        name="All four",
        channels=channels,
        cover_id="sub-agg",
    )
    coordinator = RemoteCoordinator(hass, covers, "remote-entry", remote.key)

    def leaf_config(channel: int) -> BlindConfig:
        return BlindConfig(
            name=f"Channel {channel}",
            remote=remote,
            channels=(channel,),
            travel_up=1.0,
            travel_down=1.0,
            area_id="living_room",
            repeats=2,
        )

    hub = ZemismartHub(online_registry(), publish)
    leaves = tuple(
        ZemismartCover(
            f"sub-{channel}",
            "remote-entry",
            leaf_config(channel),
            hub,
            coordinator,
        )
        for channel in channels
    )
    aggregate = cover_module.ZemismartAggregateCover(
        "sub-agg",
        "remote-entry",
        BlindConfig(
            name="All four",
            remote=remote,
            channels=channels,
            travel_up=None,
            travel_down=None,
            area_id="living_room",
            repeats=2,
            role=Role.AGGREGATE,
        ),
        hub,
        coordinator,
    )
    for entity, entity_id in (
        (leaves[0], "cover.channel_1"),
        (leaves[1], "cover.channel_2"),
        (leaves[2], "cover.channel_3"),
        (leaves[3], "cover.channel_4"),
        (aggregate, "cover.all_four"),
    ):
        await attach_family_entity(hass, entity, entity_id)

    original_write = CoverEntity._async_write_ha_state
    attempted_leaf_writes: list[str] = []
    failed_cover_ids: set[str] = set()
    raising_leaves = (leaves[0], leaves[2])

    def fail_first_leaf_write(entity: CoverEntity) -> None:
        if entity in leaves:
            attempted_leaf_writes.append(entity._cover_id)
        if entity in raising_leaves and entity._cover_id not in failed_cover_ids:
            failed_cover_ids.add(entity._cover_id)
            msg = "member invalidation write failed"
            raise RuntimeError(msg)
        original_write(entity)

    monkeypatch.setattr(CoverEntity, "_async_write_ha_state", fail_first_leaf_write)
    for leaf in leaves:
        leaf._position = 40.0
        leaf._position_anchored = True

    try:
        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()
        coordinator.unregister_leaf("sub-3")
        coordinator.unregister_leaf("sub-4")

        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        assert failed_cover_ids == {"sub-1", "sub-3"}
        assert attempted_leaf_writes == ["sub-1", "sub-2", "sub-3", "sub-4"]
        assert all(leaf.current_cover_position is None for leaf in leaves)
        assert coordinator.has_position_invalidation("remote-entry", "sub-1")
        assert not coordinator.has_position_invalidation("remote-entry", "sub-2")
        assert coordinator.has_position_invalidation("remote-entry", "sub-3")
        assert not coordinator.has_position_invalidation("remote-entry", "sub-4")
    finally:
        await detach_family(*leaves, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_failed_add_keeps_consumed_tombstone_for_retry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after restore invalidation cannot expose stale state on retry."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    subscribe_calls = 0

    def fail_first_subscription(
        _hass: HomeAssistant,
        _entity: Any,
    ) -> Callable[[], None]:
        nonlocal subscribe_calls
        subscribe_calls += 1
        if subscribe_calls == 1:
            msg = "reachability subscription failed"
            raise RuntimeError(msg)
        return lambda: None

    monkeypatch.setattr(
        cover_module,
        "_subscribe_rf_reachability",
        fail_first_subscription,
    )
    hub = ZemismartHub(online_registry(), publish)
    coordinator, _, leaf_two_config, _ = aggregate_family(hass, hub)
    coordinator.record_position_invalidations("remote-entry", (2,))
    restored = restored_leaf_state(leaf_two_config)
    cover_type = restored_cover_type(restored)
    failed = cover_type(
        "sub-2",
        "remote-entry",
        leaf_two_config,
        hub,
        coordinator,
    )
    failed.hass = hass
    failed.entity_id = "cover.channel_2"
    failed.platform = platform_stub()
    await failed.async_internal_added_to_hass()
    retry: ZemismartCover | None = None
    try:
        with pytest.raises(RuntimeError, match="reachability subscription failed"):
            await failed.async_added_to_hass()
        failed.add_to_platform_abort()

        assert coordinator.has_position_invalidation("remote-entry", "sub-2")

        retry = cover_type(
            "sub-2",
            "remote-entry",
            leaf_two_config,
            hub,
            coordinator,
        )
        await attach_family_entity(hass, retry, "cover.channel_2")

        assert retry.current_cover_position is None
        assert retry.position_confidence == "unknown"
        assert not coordinator.has_position_invalidation("remote-entry", "sub-2")
    finally:
        if retry is not None:
            await retry.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_initial_state_write_failure_keeps_consumed_tombstone_for_retry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-add state write is the tombstone consumption boundary."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    coordinator, _, leaf_two_config, _ = aggregate_family(hass, hub)
    coordinator.record_position_invalidations("remote-entry", (2,))
    restored = restored_leaf_state(leaf_two_config)
    cover_type = restored_cover_type(restored)
    failed = cover_type(
        "sub-2",
        "remote-entry",
        leaf_two_config,
        hub,
        coordinator,
    )
    failed.hass = hass
    failed.entity_id = "cover.channel_2"
    failed.platform = platform_stub()
    await failed.async_internal_added_to_hass()
    await failed.async_added_to_hass()

    original_write = CoverEntity._async_write_ha_state
    failed_writes = 0

    def fail_first_post_add_write(entity: CoverEntity) -> None:
        nonlocal failed_writes
        if entity is failed and failed_writes == 0:
            failed_writes += 1
            msg = "initial state write failed"
            raise RuntimeError(msg)
        original_write(entity)

    monkeypatch.setattr(CoverEntity, "_async_write_ha_state", fail_first_post_add_write)
    retry: ZemismartCover | None = None
    try:
        failed._platform_state = EntityPlatformState.ADDED
        with pytest.raises(RuntimeError, match="initial state write failed"):
            failed.async_write_ha_state()
        failed.add_to_platform_abort()

        assert coordinator.has_position_invalidation("remote-entry", "sub-2")

        retry = cover_type(
            "sub-2",
            "remote-entry",
            leaf_two_config,
            hub,
            coordinator,
        )
        await attach_family_entity(hass, retry, "cover.channel_2")

        assert retry.current_cover_position is None
        assert retry.position_confidence == "unknown"
        assert not coordinator.has_position_invalidation("remote-entry", "sub-2")
    finally:
        if retry is not None:
            await retry.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_successful_initial_state_write_clears_consumed_tombstone_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful unknown write clears once and cannot poison honest state."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    coordinator, _, leaf_two_config, _ = aggregate_family(hass, hub)
    coordinator.record_position_invalidations("remote-entry", (2,))
    restored = restored_leaf_state(leaf_two_config)
    clear_calls = 0
    original_clear = coordinator.clear_position_invalidation

    def count_clear(remote_entry_id: str, cover_id: str, generation: int) -> None:
        nonlocal clear_calls
        clear_calls += 1
        original_clear(remote_entry_id, cover_id, generation)

    monkeypatch.setattr(coordinator, "clear_position_invalidation", count_clear)
    cover_type = restored_cover_type(restored)
    entity = cover_type(
        "sub-2",
        "remote-entry",
        leaf_two_config,
        hub,
        coordinator,
    )
    entity.hass = hass
    entity.entity_id = "cover.channel_2"
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()
    replacement: ZemismartCover | None = None
    try:
        await entity.async_added_to_hass()

        assert coordinator.has_position_invalidation("remote-entry", "sub-2")
        assert clear_calls == 0

        entity._platform_state = EntityPlatformState.ADDED
        entity.async_write_ha_state()

        written = hass.states.get("cover.channel_2")
        assert written is not None
        assert written.state == "unknown"
        assert not coordinator.has_position_invalidation("remote-entry", "sub-2")
        assert clear_calls == 1

        entity._position = 65.0
        entity.async_write_ha_state()
        persisted = hass.states.get("cover.channel_2")
        assert persisted is not None
        assert persisted.attributes[ATTR_CURRENT_POSITION] == 65
        assert clear_calls == 1

        await entity.async_will_remove_from_hass()
        replacement = restored_cover_type(persisted)(
            "sub-2",
            "remote-entry",
            leaf_two_config,
            hub,
            coordinator,
        )
        await attach_family_entity(hass, replacement, "cover.channel_2")

        assert replacement.current_cover_position == 65
        assert replacement.position_confidence == "assumed"
        assert clear_calls == 1
    finally:
        if replacement is not None:
            await replacement.async_will_remove_from_hass()
        else:
            await entity.async_will_remove_from_hass()
        hub.close()


@pytest.mark.asyncio
async def test_rebuilt_coordinator_prunes_deleted_cover_tombstone(
    hass: HomeAssistant,
) -> None:
    """Attaching a changed topology drops markers for deleted cover IDs."""
    retained = topology_cover("Retained", "sub-2", (2,))
    deleted = topology_cover("Deleted", "sub-1", (1,))
    coordinator = tombstone_coordinator(hass, deleted, retained)
    coordinator.record_position_invalidations("remote-entry", (1, 2))
    assert coordinator.has_position_invalidation("remote-entry", "sub-1")
    assert coordinator.has_position_invalidation("remote-entry", "sub-2")

    replacement = tombstone_coordinator(hass, retained)

    assert not replacement.has_position_invalidation("remote-entry", "sub-1")
    assert replacement.has_position_invalidation("remote-entry", "sub-2")


@pytest.mark.asyncio
async def test_stale_coordinator_cannot_reinsert_deleted_cover_tombstone(
    hass: HomeAssistant,
) -> None:
    """A late old-topology failure is filtered by the current leaf IDs."""
    deleted = topology_cover("Deleted", "sub-1", (1,))
    retained = topology_cover("Retained", "sub-2", (2,))
    stale = tombstone_coordinator(hass, deleted, retained)
    stale.record_position_invalidations("remote-entry", (2,))
    current = tombstone_coordinator(hass, retained)
    assert current.has_position_invalidation("remote-entry", "sub-2")
    assert not current.has_position_invalidation("remote-entry", "sub-1")

    stale.record_position_invalidations("remote-entry", (1,))

    assert current.has_position_invalidation("remote-entry", "sub-2")
    assert not current.has_position_invalidation("remote-entry", "sub-1")


@pytest.mark.asyncio
async def test_rebuilt_coordinator_prunes_leaf_to_aggregate_tombstone(
    hass: HomeAssistant,
) -> None:
    """A stable ID that is now an aggregate cannot retain a leaf marker."""
    former_leaf = topology_cover("Former leaf", "sub-outer", (1, 2))
    stale = tombstone_coordinator(hass, former_leaf)
    stale.record_position_invalidations("remote-entry", (1, 2))
    assert stale.has_position_invalidation("remote-entry", "sub-outer")

    inner_leaf = topology_cover("New inner leaf", "sub-inner", (1,))
    current = tombstone_coordinator(hass, former_leaf, inner_leaf)

    assert current.roles["sub-outer"] is Role.AGGREGATE
    assert not current.has_position_invalidation("remote-entry", "sub-outer")


@pytest.mark.asyncio
async def test_failure_tombstones_only_addressed_leaf_channels(
    hass: HomeAssistant,
) -> None:
    """A configured leaf outside the failed frame's channels stays restorable."""
    coordinator = tombstone_coordinator(
        hass,
        topology_cover("Addressed", "sub-1", (1,)),
        topology_cover("Unaddressed", "sub-2", (2,)),
    )

    coordinator.record_position_invalidations("remote-entry", (1,))

    assert coordinator.has_position_invalidation("remote-entry", "sub-1")
    assert not coordinator.has_position_invalidation("remote-entry", "sub-2")


@pytest.mark.asyncio
async def test_stale_coordinator_cannot_tombstone_a_retained_remapped_cover(
    hass: HomeAssistant,
) -> None:
    """A stale failed frame is filtered through the current channel mapping."""
    channel_one = topology_cover("Retained", "sub-retained", (1,))
    stale = tombstone_coordinator(hass, channel_one)
    channel_two = replace(channel_one, channels=(2,))
    current = tombstone_coordinator(hass, channel_two)

    stale.record_position_invalidations("remote-entry", (1,))

    assert not current.has_position_invalidation("remote-entry", "sub-retained")

    current.record_position_invalidations("remote-entry", (2,))

    assert current.has_position_invalidation("remote-entry", "sub-retained")


@pytest.mark.asyncio
async def test_stale_remote_cannot_tombstone_a_retargeted_cover(
    hass: HomeAssistant,
) -> None:
    """A stale failed frame from the old RF remote cannot cross a reload."""
    cover = topology_cover("Retained", "sub-retained", (1,))
    stale = tombstone_coordinator(hass, cover)
    current = tombstone_coordinator(hass, cover, remote_key="ffffff:ff")

    stale.record_position_invalidations("remote-entry", (1,))

    assert not current.has_position_invalidation("remote-entry", "sub-retained")

    current.record_position_invalidations("remote-entry", (1,))

    assert current.has_position_invalidation("remote-entry", "sub-retained")


@pytest.mark.asyncio
async def test_aggregate_cancellation_tombstone_survives_real_member_removal(
    hass: HomeAssistant,
) -> None:
    """A replacement cannot restore state cached before a real async_remove."""
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub)
    for entity in (leaf_one, leaf_two, aggregate):
        entity._platform_state = EntityPlatformState.ADDED
        entity.async_write_ha_state()
    restored = restored_leaf_state(leaf_two._config)
    replacement: ZemismartCover | None = None
    try:
        leaf_one._position = 40.0
        leaf_two._position = 40.0
        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()

        await leaf_two.async_remove(force_remove=True)
        assert leaf_two not in aggregate._members()

        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        replacement = restored_cover_type(restored)(
            "sub-2",
            "remote-entry",
            leaf_two._config,
            hub,
            aggregate._coordinator,
        )
        await attach_family_entity(hass, replacement, "cover.channel_2")

        assert replacement.current_cover_position is None
        assert replacement.position_confidence == "unknown"
    finally:
        if replacement is not None:
            await replacement.async_will_remove_from_hass()
        await detach_family(leaf_one, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_aggregate_cancellation_tombstones_a_join_then_leave_member(
    hass: HomeAssistant,
) -> None:
    """A leaf present only within the await is covered by configured topology."""
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    hub = ZemismartHub(online_registry(), publish)
    coordinator, leaf_one_config, leaf_two_config, aggregate_config = aggregate_family(hass, hub)
    leaf_one = ZemismartCover("sub-1", "remote-entry", leaf_one_config, hub, coordinator)
    aggregate = cover_module.ZemismartAggregateCover(
        "sub-agg", "remote-entry", aggregate_config, hub, coordinator
    )
    restored = restored_leaf_state(leaf_two_config)
    transient: ZemismartCover | None = None
    replacement: ZemismartCover | None = None
    await attach_family_entity(hass, leaf_one, "cover.channel_1")
    await attach_family_entity(hass, aggregate, "cover.both")
    try:
        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()

        transient = restored_cover_type(restored)(
            "sub-2",
            "remote-entry",
            leaf_two_config,
            hub,
            coordinator,
        )
        await attach_family_entity(hass, transient, "cover.channel_2")
        assert transient in aggregate._members()
        await transient.async_remove(force_remove=True)
        assert transient not in aggregate._members()

        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        replacement = restored_cover_type(restored)(
            "sub-2",
            "remote-entry",
            leaf_two_config,
            hub,
            coordinator,
        )
        await attach_family_entity(hass, replacement, "cover.channel_2")

        assert replacement.current_cover_position is None
        assert replacement.position_confidence == "unknown"
    finally:
        if replacement is not None:
            await replacement.async_will_remove_from_hass()
        await detach_family(leaf_one, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_genuine_anchor_clears_a_tombstone_only_after_its_state_write(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed hard-limit travel retires its marker at the write boundary."""
    published = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        published.set()

    monkeypatch.setattr(cover_module, "FULL_TRAVEL_MARGIN_SECONDS", 0.01)
    hub = ZemismartHub(online_registry(), publish)
    leaf_one, leaf_two, aggregate = await attach_family(hass, hub, travel=0.01)
    try:
        moving = hass.async_create_task(aggregate.async_open_cover())
        await published.wait()
        moving.cancel()
        with suppress(asyncio.CancelledError):
            await moving

        coordinator = aggregate._coordinator
        assert not coordinator.has_position_invalidation("remote-entry", "sub-2")
        coordinator.unregister_leaf("sub-1")
        coordinator.unregister_leaf("sub-2")
        coordinator.record_position_invalidations("remote-entry", (1, 2))
        coordinator.register_leaf("sub-1", leaf_one)
        coordinator.register_leaf("sub-2", leaf_two)
        assert coordinator.has_position_invalidation("remote-entry", "sub-2")
        clear_calls = 0
        original_clear = coordinator.clear_position_invalidation

        def count_clear(remote_entry_id: str, cover_id: str, generation: int) -> None:
            nonlocal clear_calls
            clear_calls += 1
            original_clear(remote_entry_id, cover_id, generation)

        monkeypatch.setattr(coordinator, "clear_position_invalidation", count_clear)
        original_write = CoverEntity._async_write_ha_state
        failed_writes = 0

        def fail_first_anchor_write(entity: CoverEntity) -> None:
            nonlocal failed_writes
            if (
                entity is leaf_two
                and leaf_two.position_confidence == "anchored"
                and failed_writes == 0
            ):
                failed_writes += 1
                msg = "anchor state write failed"
                raise RuntimeError(msg)
            original_write(entity)

        monkeypatch.setattr(CoverEntity, "_async_write_ha_state", fail_first_anchor_write)

        started = cover_module.MONOTONIC_CLOCK()
        leaf_two._start_member_motion(
            cover_module._MotionStart(
                source="commanded",
                started_at=cover_module.WALL_CLOCK(),
                started_at_monotonic=started,
                deadline=None,
                deadline_monotonic=None,
                bridge_id="bridge-a",
                command_id="genuine-anchor",
            ),
            ack=None,
            direction=1,
            duration=0.0,
            group_target=100.0,
        )
        motion_task = leaf_two._motion_task
        assert motion_task is not None
        with pytest.raises(RuntimeError, match="anchor state write failed"):
            await motion_task

        assert leaf_two.current_cover_position == 100
        assert leaf_two.position_confidence == "anchored"
        assert coordinator.has_position_invalidation("remote-entry", "sub-2")
        assert leaf_two._restore_position_invalidation_generation is not None
        assert clear_calls == 0

        leaf_two.async_write_ha_state()

        assert not coordinator.has_position_invalidation("remote-entry", "sub-2")
        assert leaf_two._restore_position_invalidation_generation is None
        assert clear_calls == 1

        leaf_two.async_write_ha_state()

        assert clear_calls == 1
        assert coordinator.has_position_invalidation("remote-entry", "sub-1")
    finally:
        await detach_family(leaf_one, leaf_two, aggregate)
        hub.close()


@pytest.mark.asyncio
async def test_anchor_pending_clear_cannot_retire_a_newer_tombstone(
    hass: HomeAssistant,
) -> None:
    """A pending clear owns only the marker generation that it consumed."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(online_registry(), publish)
    coordinator, _, leaf_two_config, _ = aggregate_family(hass, hub)
    coordinator.record_position_invalidations("remote-entry", (2,))
    restored = restored_leaf_state(leaf_two_config)
    entity = restored_cover_type(restored)(
        "sub-2",
        "remote-entry",
        leaf_two_config,
        hub,
        coordinator,
    )
    entity.hass = hass
    entity.entity_id = "cover.channel_2"
    entity.platform = platform_stub()
    await entity.async_internal_added_to_hass()
    await entity.async_added_to_hass()
    try:
        assert entity._restore_position_invalidation_generation is not None
        assert coordinator.has_position_invalidation("remote-entry", "sub-2")

        entity._position = 100.0
        entity._anchor_if_at_limit()
        coordinator.unregister_leaf("sub-2")
        coordinator.record_position_invalidations("remote-entry", (2,))

        entity._platform_state = EntityPlatformState.ADDED
        entity._position = 90.0
        entity.async_write_ha_state()

        assert entity._restore_position_invalidation_generation is None
        assert coordinator.has_position_invalidation("remote-entry", "sub-2")
    finally:
        await entity.async_will_remove_from_hass()
        hub.close()
