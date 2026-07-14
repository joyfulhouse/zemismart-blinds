"""Tests for bridge resolution and MQTT command construction."""

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.zemismart_blinds.codec import (
    CommandBases,
    decode_b0,
    encode_b0,
    make_payload,
)
from custom_components.zemismart_blinds.models import (
    BlindConfig,
    BridgeRegistry,
    CommandAck,
    CommandAckTimeoutError,
    CommandRejectedError,
    CommandStartedTimeoutError,
    NoOnlineBridgeError,
    RemoteIdentity,
    ZemismartHub,
)
from tests.synthetic import (
    SYNTHETIC_REMOTES,
    TEST_BASES,
    TEST_PREFIX,
    TEST_REMOTE_ID,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# A second synthetic remote used to prove that remote identity partitions
# coalescing batches and command targets.
_name, OTHER_PREFIX, OTHER_REMOTE_ID, OTHER_BASES, _payload = SYNTHETIC_REMOTES[1]


def blind_config(*, area_id: str = "living_room") -> BlindConfig:
    """Return a representative two-channel group configuration."""
    return BlindConfig(
        name="Living Room Left",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1, 2),
        travel_up=14.0,
        travel_down=13.0,
        area_id=area_id,
        repeats=5,
    )


def config_with_window(config: BlindConfig, window_ms: int) -> BlindConfig:
    """Return a config loaded with one persisted coalescing option."""
    values = config.as_dict()
    values["coalesce_window_ms"] = window_ms
    return BlindConfig.from_mapping(values)


def accepted(body: Mapping[str, Any]) -> dict[str, object]:
    """Return the firmware's command-ID-only admission status."""
    return {"status": "accepted", "command_id": body["command_id"]}


def started(body: Mapping[str, Any]) -> dict[str, object]:
    """Return the firmware's command-ID-only first-RF-dispatch status."""
    return {"status": "started", "command_id": body["command_id"]}


def accept_and_start(hub: ZemismartHub, bridge_id: str, body: Mapping[str, Any]) -> None:
    """Complete both correlated firmware lifecycle statuses."""
    assert hub.handle_status(bridge_id, accepted(body))
    assert hub.handle_status(bridge_id, started(body))


def test_blind_config_round_trip_and_normalization() -> None:
    """Stored config-entry values normalize to typed, sorted channel data."""
    config = BlindConfig.from_mapping(
        {
            "name": "Living Room Left",
            "prefix": "a1b2c3",
            "remote_id": "42",
            "channels": [2, 1],
            "travel_up": 14,
            "travel_down": 13.5,
            "area_id": "living_room",
            "repeats": 6,
            "base_up": "f42a",
            "base_down": "bcf2",
            "base_stop": "dc12",
            "base_trailer": "dd05",
        }
    )

    assert config == BlindConfig(
        name="Living Room Left",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1, 2),
        travel_up=14.0,
        travel_down=13.5,
        area_id="living_room",
        repeats=6,
    )
    assert config.coalesce_window_ms == 150
    assert config.as_dict() == {
        "name": "Living Room Left",
        "prefix": "a1b2c3",
        "remote_id": "42",
        "channels": [1, 2],
        "travel_up": 14.0,
        "travel_down": 13.5,
        "area_id": "living_room",
        "repeats": 6,
        "coalesce_window_ms": 150,
        "base_up": "f42a",
        "base_down": "bcf2",
        "base_stop": "dc12",
        "base_trailer": "dd05",
    }


def test_stored_bases_load_and_drive_frames_without_a_trailer() -> None:
    """Stored action bases (no trailer) load and drive the correct UP frame."""
    config = BlindConfig.from_mapping(
        {
            "name": "Bedroom 1",
            "prefix": "123456",
            "remote_id": "0d",
            "channels": [1],
            "travel_up": 30,
            "travel_down": 30,
            "area_id": "bedroom",
            "repeats": 5,
            "base_up": "f449",
            "base_down": "bd11",
            "base_stop": "dd31",
        }
    )

    assert config.remote.bases == CommandBases(0xF449, 0xBD11, 0xDD31)
    assert decode_b0(ZemismartHub._frame(config, "UP"))["cmd"] == 0xF453
    assert "trailer_raw" not in ZemismartHub._command_body(
        config,
        "UP",
        stop_after_ms=None,
    )


def test_explicit_remote_bases_round_trip() -> None:
    """Stored calibration is part of a remote identity and survives serialization."""
    values: dict[str, object] = {
        "name": "New Remote",
        "prefix": "123456",
        "remote_id": "0d",
        "channels": [1],
        "travel_up": 15,
        "travel_down": 15,
        "area_id": "living_room",
        "repeats": 5,
        "base_up": "f449",
        "base_down": "bd11",
        "base_stop": "dd31",
    }

    config = BlindConfig.from_mapping(values)

    assert config.remote.bases == CommandBases(0xF449, 0xBD11, 0xDD31)
    assert BlindConfig.from_mapping(config.as_dict()) == config


def test_unknown_uncalibrated_mapping_fails_loud() -> None:
    """An unknown remote without stored bases is rejected, never silently defaulted.

    Silently defaulting a different remote to some other remote's base would
    emit wrong codes; requiring calibration surfaces the problem instead.
    """
    with pytest.raises(ValueError, match="remote calibration is required"):
        BlindConfig.from_mapping(
            {
                "name": "Legacy Unknown",
                "prefix": "123456",
                "remote_id": "0d",
                "channels": [1],
                "travel_up": 15,
                "travel_down": 15,
                "area_id": "living_room",
                "repeats": 5,
            }
        )


def test_blind_config_accepts_zero_coalescing_window_and_rejects_negative() -> None:
    """Each entry can disable coalescing, but a negative window is invalid."""
    disabled = config_with_window(blind_config(), 0)

    assert disabled.coalesce_window_ms == 0
    with pytest.raises(ValueError, match="coalesce_window_ms"):
        config_with_window(blind_config(), -1)


def test_bridge_selection_prefers_online_same_area() -> None:
    """An online in-area beacon wins even when another beacon is the default."""
    registry = BridgeRegistry()
    registry.update_info("bridge-b", {"area": "bedroom", "default": True})
    registry.update_availability("bridge-b", "online")
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "online")

    assert registry.resolve("living_room").bridge_id == "bridge-a"


def test_bridge_selection_falls_back_to_default_then_any_online() -> None:
    """Resolution uses the configured default, then a deterministic online fallback."""
    registry = BridgeRegistry()
    registry.update_info("z-last", {"area": "other"})
    registry.update_availability("z-last", "online")
    registry.update_info("default", {"area": "other", "default": True})
    registry.update_availability("default", "online")

    assert registry.resolve("missing").bridge_id == "default"

    registry.update_availability("default", "offline")
    assert registry.resolve("missing").bridge_id == "z-last"


def test_bridge_selection_never_returns_offline_bridge() -> None:
    """An offline in-area/default bridge cannot be selected for transmission."""
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room", "default": True})
    registry.update_availability("bridge-a", "offline")

    with pytest.raises(NoOnlineBridgeError, match="online"):
        registry.resolve("living_room")


def test_timed_position_command_contains_bridge_side_stop() -> None:
    """Partial TX carries correlation, target, trailer, and bridge-owned STOP data."""
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "online")
    published: list[tuple[str, Mapping[str, Any]]] = []
    status_times = iter((1_000.0, 1_010.0))

    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        assert hub.handle_status("bridge-a", bytearray(json.dumps(accepted(body)).encode()))
        assert hub.handle_status("bridge-a", bytearray(json.dumps(started(body)).encode()))

    config = blind_config()
    hub = ZemismartHub(
        registry,
        publish,
        ack_timeout=0.001,
        command_id_factory=lambda: "command-1",
        now=lambda: next(status_times),
    )
    ack = asyncio.run(
        hub.async_transmit(
            config,
            "DOWN",
            stop_after_ms=3_250,
        )
    )

    assert isinstance(ack, CommandAck)
    assert ack.bridge.bridge_id == "bridge-a"
    assert ack.command_id == "command-1"
    assert ack.acknowledged_at == 1_000.0
    assert ack.started_at == 1_010.0
    assert ack.deadline == 1_013.25
    assert published == [
        (
            "rf433/bridge-a/tx",
            {
                "command_id": "command-1",
                "target": "a1b2c3:42:1,2",
                "raw": encode_b0(
                    make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "DOWN", bases=TEST_BASES)
                ),
                "trailer_raw": encode_b0(
                    make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "TRAILER", bases=TEST_BASES)
                ),
                "repeats": 5,
                "stop_after_ms": 3_250,
                "stop_raw": encode_b0(
                    make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "STOP", bases=TEST_BASES)
                ),
            },
        )
    ]


def test_stop_command_has_no_delayed_stop() -> None:
    """Immediate STOP is standalone: no delayed STOP and no OEM TRAILER."""
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "online")
    payloads: list[dict[str, Any]] = []

    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        payloads.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: "stop-1")
    asyncio.run(hub.async_transmit(blind_config(), "STOP"))

    assert payloads[0].keys() == {"command_id", "target", "raw", "repeats"}


def test_matching_rejection_is_raised() -> None:
    """A correlated firmware rejection is an explicit failed command."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        hub.handle_status(
            "bridge-a",
            {
                "status": "rejected",
                "command_id": body["command_id"],
                "reason": "invalid stop_raw",
            },
        )

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: "bad-1")

    with pytest.raises(CommandRejectedError, match="invalid stop_raw"):
        asyncio.run(hub.async_transmit(blind_config(), "UP"))


def test_unmatched_and_malformed_statuses_cannot_acknowledge_a_command() -> None:
    """Wrong bridge/ID and malformed status JSON all end in an honest timeout."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert not hub.handle_status("bridge-a", "not-json")
        assert not hub.handle_status("other", body)
        assert not hub.handle_status(
            "bridge-a", {"status": "accepted", "command_id": f"{body['command_id']}-wrong"}
        )
        assert not hub.handle_status(
            "bridge-a", {"status": "started", "command_id": f"{body['command_id']}-wrong"}
        )

    hub = ZemismartHub(
        registry,
        publish,
        ack_timeout=0.001,
        command_id_factory=lambda: "timeout-1",
    )

    with pytest.raises(CommandAckTimeoutError, match="timeout-1"):
        asyncio.run(hub.async_transmit(blind_config(), "UP"))


def test_started_timeout_after_acceptance_is_reported_as_ambiguous() -> None:
    """Admission without first RF dispatch cannot start or preserve a position estimate."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status("bridge-a", accepted(body))

    hub = ZemismartHub(
        registry,
        publish,
        started_timeout=0.001,
        command_id_factory=lambda: "not-started",
    )

    with pytest.raises(CommandStartedTimeoutError, match="not-started"):
        asyncio.run(hub.async_transmit(blind_config(), "UP"))


def test_publish_failure_is_propagated() -> None:
    """Broker publish failure does not manufacture an acknowledgement."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")

    async def publish(_topic: str, _payload: str) -> None:
        msg = "broker unavailable"
        raise OSError(msg)

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: "publish-failure")

    with pytest.raises(OSError, match="broker unavailable"):
        asyncio.run(hub.async_transmit(blind_config(), "UP"))


@pytest.mark.asyncio
async def test_global_queue_serializes_different_targets_until_rf_start() -> None:
    """Admission alone cannot release the next command before actual RF dispatch."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    first_published = asyncio.Event()
    second_published = asyncio.Event()
    ids = iter(("target-a", "target-b"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        (first_published if len(published) == 1 else second_published).set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    first = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
    second = asyncio.create_task(hub.async_transmit(replace(blind_config(), channels=(3,)), "DOWN"))
    await first_published.wait()
    await asyncio.sleep(0)
    assert len(published) == 1

    assert hub.handle_status("bridge-a", accepted(published[0]))
    await asyncio.sleep(0)
    assert len(published) == 1
    assert hub.handle_status("bridge-a", started(published[0]))
    await second_published.wait()
    assert [body["target"] for body in published] == ["a1b2c3:42:1,2", "a1b2c3:42:3"]
    accept_and_start(hub, "bridge-a", published[1])
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_worker_resolves_bridge_when_queued_command_is_popped() -> None:
    """A queued command uses an online bridge selected immediately before publish."""
    registry = BridgeRegistry()
    registry.update_info("old", {"area": "living_room"})
    registry.update_availability("old", "online")
    published: list[tuple[str, dict[str, Any]]] = []
    publish_events = [asyncio.Event(), asyncio.Event()]
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, json.loads(payload)))
        publish_events[len(published) - 1].set()

    hub = ZemismartHub(registry, publish)
    first = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
    await publish_events[0].wait()
    second = asyncio.create_task(hub.async_transmit(blind_config(), "DOWN"))
    await asyncio.sleep(0)

    registry.update_availability("old", "offline")
    registry.update_info("new", {"area": "living_room"})
    registry.update_availability("new", "online")
    first_body = published[0][1]
    accept_and_start(hub, "old", first_body)

    await publish_events[1].wait()
    assert published[1][0] == "rf433/new/tx"
    second_body = published[1][1]
    accept_and_start(hub, "new", second_body)
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_stop_fast_lane_bypasses_unrelated_inflight_command() -> None:
    """STOP supersedes queued overlapping movement and skips the global lane.

    An in-flight command for unrelated channels must not delay a safety STOP:
    the STOP publishes immediately while the blocker is still awaiting its
    acknowledgement, and the queued overlapping movement resolves superseded.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    publish_events = [asyncio.Event() for _ in range(3)]
    ids = iter(("blocker", "stop", "unrelated"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        publish_events[len(published) - 1].set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    blocker_config = replace(blind_config(), channels=(3,))
    unrelated_config = replace(blind_config(), channels=(4,))
    blocker = asyncio.create_task(hub.async_transmit(blocker_config, "UP"))
    await publish_events[0].wait()

    superseded = asyncio.create_task(hub.async_transmit(blind_config(), "DOWN"))
    unrelated = asyncio.create_task(hub.async_transmit(unrelated_config, "UP"))
    await asyncio.sleep(0)
    stop = asyncio.create_task(hub.async_transmit(blind_config(), "STOP"))
    await asyncio.sleep(0)

    assert await superseded == "superseded"
    # The STOP does not wait behind the unacknowledged blocker.
    await publish_events[1].wait()
    assert published[1]["command_id"] == "stop"
    assert published[1]["raw"] == encode_b0(
        make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "STOP", bases=TEST_BASES)
    )
    accept_and_start(hub, "bridge-a", published[1])
    await stop
    # The queued unrelated movement still waits for the blocker to finish.
    assert len(published) == 2
    accept_and_start(hub, "bridge-a", published[0])
    await publish_events[2].wait()
    assert published[2]["target"] == unrelated_config.target_key
    accept_and_start(hub, "bridge-a", published[2])
    await asyncio.gather(blocker, unrelated)


@pytest.mark.asyncio
async def test_stop_overlapping_inflight_command_stays_ordered() -> None:
    """A STOP for the in-flight command's own channels queues behind it."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    publish_events = [asyncio.Event() for _ in range(2)]
    ids = iter(("move", "stop"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        publish_events[len(published) - 1].set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    move = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
    await publish_events[0].wait()

    stop = asyncio.create_task(hub.async_transmit(blind_config(), "STOP"))
    await asyncio.sleep(0)
    # Publishing order must be preserved for the same channels: the STOP may
    # not reach the bridge before the movement it is stopping.
    assert len(published) == 1
    accept_and_start(hub, "bridge-a", published[0])
    await publish_events[1].wait()
    assert published[1]["command_id"] == "stop"
    accept_and_start(hub, "bridge-a", published[1])
    await asyncio.gather(move, stop)


@pytest.mark.asyncio
async def test_simultaneous_same_remote_movements_publish_one_union_frame() -> None:
    """All same-direction individual futures resolve from one union-frame acknowledgement."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    configs = [
        config_with_window(replace(blind_config(), channels=(channel,)), 20)
        for channel in (1, 2, 4, 6)
    ]

    results = await asyncio.gather(*(hub.async_transmit(config, "DOWN") for config in configs))

    assert len(published) == 1
    assert published[0]["target"] == "a1b2c3:42:1,2,4,6"
    assert decode_b0(str(published[0]["raw"]))["chans"] == [1, 2, 4, 6]
    assert all(isinstance(result, CommandAck) for result in results)
    assert all(result is results[0] for result in results)


@pytest.mark.asyncio
async def test_cancelled_movement_is_excluded_from_a_live_union_batch() -> None:
    """Canceling one unpublished future cannot move its channel with live siblings."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    first_config = config_with_window(replace(blind_config(), channels=(1,)), 20)
    second_config = config_with_window(replace(blind_config(), channels=(2,)), 20)
    first = asyncio.create_task(hub.async_transmit(first_config, "UP"))
    second = asyncio.create_task(hub.async_transmit(second_config, "UP"))
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert isinstance(await second, CommandAck)

    assert [body["target"] for body in published] == ["a1b2c3:42:2"]


@pytest.mark.asyncio
async def test_cancelling_waiting_head_wakes_incompatible_live_command() -> None:
    """A canceled long-window head cannot stall a different-direction batch."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    long_window = config_with_window(replace(blind_config(), channels=(1,)), 500)
    short_window = config_with_window(replace(blind_config(), channels=(2,)), 20)
    first = asyncio.create_task(hub.async_transmit(long_window, "UP"))
    second = asyncio.create_task(hub.async_transmit(short_window, "DOWN"))
    await asyncio.sleep(0.01)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    try:
        result = await asyncio.wait_for(second, timeout=0.1)
    finally:
        hub.close()

    assert isinstance(result, CommandAck)
    assert [body["target"] for body in published] == ["a1b2c3:42:2"]


@pytest.mark.asyncio
async def test_batch_flushes_at_earliest_contributing_window() -> None:
    """A short-window sibling bounds latency even when the oldest window is longer."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    long_window = config_with_window(replace(blind_config(), channels=(1,)), 500)
    short_window = config_with_window(replace(blind_config(), channels=(2,)), 20)
    first = asyncio.create_task(hub.async_transmit(long_window, "UP"))
    second = asyncio.create_task(hub.async_transmit(short_window, "UP"))
    try:
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=0.2)
    finally:
        hub.close()

    assert all(isinstance(result, CommandAck) for result in results)
    assert [body["target"] for body in published] == ["a1b2c3:42:1,2"]


@pytest.mark.asyncio
async def test_opposite_directions_on_same_remote_publish_two_frames() -> None:
    """The coalescing key keeps UP and DOWN batches separate."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    up_configs = [
        config_with_window(replace(blind_config(), channels=(channel,)), 20) for channel in (1, 2)
    ]
    down_configs = [
        config_with_window(replace(blind_config(), channels=(channel,)), 20) for channel in (3, 4)
    ]

    await asyncio.gather(
        *(hub.async_transmit(config, "UP") for config in up_configs),
        *(hub.async_transmit(config, "DOWN") for config in down_configs),
    )

    assert len(published) == 2
    assert {str(body["raw"]) for body in published} == {
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_BASES)),
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (3, 4), "DOWN", bases=TEST_BASES)),
    }


@pytest.mark.asyncio
async def test_simultaneous_movements_on_different_remotes_publish_one_frame_each() -> None:
    """Remote identity partitions simultaneous coalescing batches."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    configs = (
        config_with_window(replace(blind_config(), channels=(1,)), 20),
        config_with_window(replace(blind_config(), channels=(2,)), 20),
        config_with_window(
            replace(
                blind_config(),
                remote=RemoteIdentity(OTHER_PREFIX, OTHER_REMOTE_ID, OTHER_BASES),
                channels=(3,),
            ),
            20,
        ),
        config_with_window(
            replace(
                blind_config(),
                remote=RemoteIdentity(OTHER_PREFIX, OTHER_REMOTE_ID, OTHER_BASES),
                channels=(5,),
            ),
            20,
        ),
    )

    await asyncio.gather(*(hub.async_transmit(config, "UP") for config in configs))

    assert len(published) == 2
    assert {body["target"] for body in published} == {
        "a1b2c3:42:1,2",
        "123456:0d:3,5",
    }


@pytest.mark.asyncio
async def test_stop_during_window_is_immediate_and_supersedes_queued_movement() -> None:
    """STOP interrupts the coalescing wait without publishing the superseded movement."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    stop_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        if body["raw"] == encode_b0(
            make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "STOP", bases=TEST_BASES)
        ):
            stop_published.set()
        accept_and_start(hub, "bridge-a", body)

    config = config_with_window(replace(blind_config(), channels=(1,)), 150)
    sibling_config = config_with_window(replace(blind_config(), channels=(2,)), 150)
    hub = ZemismartHub(registry, publish)
    movement = asyncio.create_task(hub.async_transmit(config, "DOWN"))
    sibling = asyncio.create_task(hub.async_transmit(sibling_config, "DOWN"))
    await asyncio.sleep(0)
    assert published == []

    stop = asyncio.create_task(hub.async_transmit(config, "STOP"))
    await asyncio.wait_for(stop_published.wait(), timeout=0.1)

    assert await movement == "superseded"
    assert isinstance(await stop, CommandAck)
    assert isinstance(await sibling, CommandAck)
    assert [body["raw"] for body in published] == [
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "STOP", bases=TEST_BASES)),
        encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (2,), "DOWN", bases=TEST_BASES)),
    ]


@pytest.mark.asyncio
async def test_zero_window_disables_coalescing() -> None:
    """A per-config zero window preserves one frame per individual command."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    configs = [
        config_with_window(replace(blind_config(), channels=(channel,)), 0) for channel in (1, 2, 3)
    ]

    await asyncio.gather(*(hub.async_transmit(config, "UP") for config in configs))

    assert [body["target"] for body in published] == [
        "a1b2c3:42:1",
        "a1b2c3:42:2",
        "a1b2c3:42:3",
    ]


@pytest.mark.asyncio
async def test_command_after_window_closes_starts_a_new_batch() -> None:
    """A sibling enqueued after the first deadline cannot join the prior union frame."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    first_published = asyncio.Event()
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        first_published.set()
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    first_config = config_with_window(replace(blind_config(), channels=(1,)), 20)
    second_config = config_with_window(replace(blind_config(), channels=(2,)), 20)
    first = asyncio.create_task(hub.async_transmit(first_config, "UP"))
    await first_published.wait()
    second = asyncio.create_task(hub.async_transmit(second_config, "UP"))

    await asyncio.gather(first, second)

    assert [body["target"] for body in published] == [
        "a1b2c3:42:1",
        "a1b2c3:42:2",
    ]


@pytest.mark.asyncio
async def test_explicit_group_movement_is_not_delayed_or_coalesced() -> None:
    """An explicit group remains its existing immediate single-frame command."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published = asyncio.Event()
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.set()
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    command = asyncio.create_task(
        hub.async_transmit(config_with_window(blind_config(), 150), "DOWN")
    )

    await asyncio.wait_for(published.wait(), timeout=0.1)
    assert isinstance(await command, CommandAck)


@pytest.mark.parametrize(
    "raw",
    (
        "AAB0GG55",
        "AAB0010055",
        "AAB005010000011155",
    ),
)
def test_send_raw_rejects_malformed_b0_before_publish(raw: str) -> None:
    """Debug TX applies the same strict B0 parser before MQTT sees any bytes."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[str] = []

    async def publish(_topic: str, payload: str) -> None:
        published.append(payload)

    hub = ZemismartHub(registry, publish)

    with pytest.raises(ValueError, match=r"hex|length|bucket|frame"):
        asyncio.run(hub.async_send_raw("bridge-a", raw, 1))

    assert published == []


@pytest.mark.asyncio
async def test_displaced_status_resolves_pending_command_as_superseded() -> None:
    """A displaced command's caller resolves superseded instead of timing out.

    The bridge's latest-command-wins can displace a command between accepted
    and started; without pending resolution the caller would block for the
    full started timeout and wrongly invalidate the cover's position.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    publish_event = asyncio.Event()
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))
        publish_event.set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: "victim")
    transmit = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
    await publish_event.wait()

    assert hub.handle_status("bridge-a", accepted(published[0]))
    assert hub.handle_status(
        "bridge-a",
        json.dumps({"status": "displaced", "command_id": "victim"}),
    )

    assert await transmit == "superseded"
    assert hub.was_displaced("victim")


@pytest.mark.asyncio
async def test_second_overlapping_fast_lane_stop_queues_behind_first() -> None:
    """Concurrent overlapping STOPs never publish out of order.

    The second STOP must see the first fast-lane STOP as in flight and chain
    behind its completion inside the fast lane instead of racing it to the
    bridge.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    publish_events = [asyncio.Event() for _ in range(2)]
    ids = iter(("stop-1", "stop-2"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))
        publish_events[len(published) - 1].set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    first = asyncio.create_task(hub.async_transmit(blind_config(), "STOP"))
    await publish_events[0].wait()
    second = asyncio.create_task(hub.async_transmit(blind_config(), "STOP"))
    await asyncio.sleep(0)

    # The overlapping second STOP is NOT published while the first is in flight.
    assert len(published) == 1
    accept_and_start(hub, "bridge-a", published[0])
    await first
    await publish_events[1].wait()
    assert published[1]["command_id"] == "stop-2"
    accept_and_start(hub, "bridge-a", published[1])
    await second


@pytest.mark.asyncio
async def test_overlapping_fast_stop_chains_behind_fast_stop_not_inflight() -> None:
    """A chained fast STOP never waits behind an unrelated in-flight command.

    While an unrelated channel-3 movement is still awaiting acknowledgement,
    STOP {1} runs in the fast lane and STOP {1, 2} arrives. The second STOP
    chains behind the first inside the fast lane and publishes as soon as the
    first resolves -- dropping to the global queue would park a safety STOP
    behind the blocker's up-to-30-second acknowledgement wait, leaving
    channel 2 moving.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    publish_events = [asyncio.Event() for _ in range(3)]
    ids = iter(("blocker", "stop-1", "stop-12"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        publish_events[len(published) - 1].set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    blocker_config = replace(blind_config(), channels=(3,))
    single_config = replace(blind_config(), channels=(1,))
    blocker = asyncio.create_task(hub.async_transmit(blocker_config, "UP"))
    await publish_events[0].wait()

    first_stop = asyncio.create_task(hub.async_transmit(single_config, "STOP"))
    await publish_events[1].wait()
    assert published[1]["command_id"] == "stop-1"
    second_stop = asyncio.create_task(hub.async_transmit(blind_config(), "STOP"))
    await asyncio.sleep(0)
    # Chained behind the in-flight overlapping fast STOP: not yet published.
    assert len(published) == 2

    accept_and_start(hub, "bridge-a", published[1])
    await first_stop
    # Publishes while the unrelated blocker is STILL unacknowledged.
    await publish_events[2].wait()
    assert published[2]["command_id"] == "stop-12"
    accept_and_start(hub, "bridge-a", published[2])
    await second_stop

    accept_and_start(hub, "bridge-a", published[0])
    await blocker
