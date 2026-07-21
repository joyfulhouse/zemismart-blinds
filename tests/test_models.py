"""Tests for bridge resolution and MQTT command construction."""

import asyncio
import json
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

import pytest

from custom_components.zemismart_blinds import models as models_module
from custom_components.zemismart_blinds.codec import (
    CommandBases,
    decode_b0,
    encode_b0,
    make_payload,
)
from custom_components.zemismart_blinds.models import (
    BlindConfig,
    BridgeRegistry,
    Button,
    CommandAck,
    CommandAckTimeoutError,
    CommandRejectedError,
    CommandStartedTimeoutError,
    NoOnlineBridgeError,
    RemoteIdentity,
    TakeoverCoverState,
    ZemismartHub,
    parse_channels,
)
from custom_components.zemismart_blinds.state_sync import (
    BridgeClock,
    CommandLedger,
    HeardEvent,
    LedgerFrameSpec,
    StateSyncConsumer,
    frame_signature,
)
from tests.synthetic import (
    SYNTHETIC_REMOTES,
    TEST_BASES,
    TEST_PREFIX,
    TEST_REMOTE_ID,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from custom_components.zemismart_blinds.models import CoverConfig

# A second synthetic remote used to prove that remote identity partitions
# coalescing batches and command targets.
_name, OTHER_PREFIX, OTHER_REMOTE_ID, OTHER_BASES, _payload = SYNTHETIC_REMOTES[1]

_STATE_SYNC_BOOT: Final = 7
_STATE_SYNC_T: Final = 2_000
_STATE_SYNC_RECV_TIME: Final = 100.0
_LEDGER_STOP_AFTER_MS: Final = 3_250
_MILLISECONDS_PER_SECOND: Final = 1_000
_BRIDGE_STATE_CAP: Final = 256
_FORGED_BRIDGE_COUNT: Final = 300
_DISARM_RETRY_TEST_SECONDS: Final = 0.005
_DISARM_TEST_DEADLINE_SECONDS: Final = 1.0
_DISARM_SHORT_DEADLINE_SECONDS: Final = 0.05
_DISARM_LONG_DEADLINE_SECONDS: Final = 0.15
_DISARM_ACK_DELAY_SECONDS: Final = 0.1
_DISARM_TIMEOUT_LOWER_BOUND_SECONDS: Final = 0.12
_DISARM_PENDING_DEADLINE_SECONDS: Final = 0.08
_DISARM_BACKOFF_SCHEDULE: Final = (0.25, 0.5, 1.0, 2.0, 4.0, 5.0, 5.0)
_TAKEOVER_GENERIC_DEADLINE_SECONDS: Final = 0.04
_TAKEOVER_OWNED_DEADLINE_SECONDS: Final = 0.16
_TAKEOVER_AFTER_GENERIC_SECONDS: Final = 0.07
_TAKEOVER_TIMEOUT_LOWER_BOUND_SECONDS: Final = 0.13
_DISPLACED_STOP_AFTER_MS: Final = 120_000
_DISPLACED_AT: Final = 140.0
_DISPLACED_FLUSH_AT: Final = 140.1
_DISPLACED_ORIGINAL_STOP_AT: Final = 220.0
_DISPLACED_ORIGINAL_STOP_T: Final = 81_900
_SEEDED_BRIDGE_T: Final = 10_000
_STARTED_STATUS_T: Final = 10_250
_STARTED_STATUS_AGE_MS: Final = 250
_STARTED_DELIVERY_TIME: Final = 102.0
_CLAMPED_DELIVERY_TIME: Final = 200.0
_CLAMPED_AGE_MS: Final = 20_000
_CLAMPED_STATUS_T: Final = 70_000
_REPLAY_AGE_MS: Final = 600_000
_REPLAY_STATUS_T: Final = 610_000
_REPLAY_DELIVERY_TIME: Final = 700.0


def test_role_is_str_enum() -> None:
    from custom_components.zemismart_blinds.models import Role

    assert Role.LEAF.value == "leaf"
    assert Role.AGGREGATE.value == "aggregate"


def test_cover_config_normalizes_and_exposes_channel_key() -> None:
    from custom_components.zemismart_blinds.models import CoverConfig

    cover = CoverConfig(
        name="  Kitchen sink  ", channels=(3, 1, 2), travel_up=12.0, travel_down=10.0
    )
    assert cover.name == "Kitchen sink"
    assert cover.channels == (1, 2, 3)
    assert cover.channel_key == "1-2-3"
    assert cover.has_travel is True


def test_cover_config_allows_missing_travel_times() -> None:
    from custom_components.zemismart_blinds.models import CoverConfig

    cover = CoverConfig(name="All shades", channels=(1, 2, 3, 4, 5, 6))
    assert cover.travel_up is None
    assert cover.travel_down is None
    assert cover.has_travel is False


def test_cover_config_rejects_partial_travel_times() -> None:
    from custom_components.zemismart_blinds.models import CoverConfig

    with pytest.raises(ValueError, match="together"):
        CoverConfig(name="x", channels=(1,), travel_up=12.0)


def test_cover_config_rejects_empty_name_and_bad_travel() -> None:
    from custom_components.zemismart_blinds.models import CoverConfig

    with pytest.raises(ValueError, match="name"):
        CoverConfig(name="   ", channels=(1,), travel_up=5.0, travel_down=5.0)
    with pytest.raises(ValueError):
        CoverConfig(name="x", channels=(1,), travel_up=0.0, travel_down=5.0)
    with pytest.raises(ValueError):
        CoverConfig(name="x", channels=(1,), travel_up=5.0, travel_down=999_999.0)


def test_cover_config_roundtrips_through_mapping() -> None:
    from custom_components.zemismart_blinds.models import CoverConfig

    cover = CoverConfig(name="Counter", channels=(4,), travel_up=8.5, travel_down=9.5)
    restored = CoverConfig.from_subentry(cover.as_dict())
    assert restored == cover

    aggregate = CoverConfig(name="All", channels=(1, 2, 3))
    restored_aggregate = CoverConfig.from_subentry(aggregate.as_dict())
    assert restored_aggregate == aggregate
    assert restored_aggregate.travel_up is None


def _remote_identity() -> RemoteIdentity:
    from custom_components.zemismart_blinds.models import RemoteIdentity

    return RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)


def test_remote_config_key_and_defaults() -> None:
    from custom_components.zemismart_blinds.models import RemoteConfig

    remote = RemoteConfig(
        name=" Kitchen remote ",
        remote=_remote_identity(),
        area_id=" kitchen ",
        repeats=5,
    )
    assert remote.name == "Kitchen remote"
    assert remote.area_id == "kitchen"
    assert remote.key == f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    assert remote.coalesce_window_ms == 150


def test_remote_config_validates_bounds_and_calibration() -> None:
    from custom_components.zemismart_blinds.models import RemoteConfig, RemoteIdentity

    with pytest.raises(ValueError, match="calibration"):
        RemoteConfig(
            name="x",
            remote=RemoteIdentity(0x000001, 0x02),  # no bases, none pre-seeded
            area_id="a",
            repeats=5,
        )
    with pytest.raises(ValueError, match="repeats"):
        RemoteConfig(name="x", remote=_remote_identity(), area_id="a", repeats=0)
    with pytest.raises(ValueError, match="coalesce"):
        RemoteConfig(
            name="x",
            remote=_remote_identity(),
            area_id="a",
            repeats=5,
            coalesce_window_ms=99_999,
        )
    with pytest.raises(ValueError, match="area"):
        RemoteConfig(name="x", remote=_remote_identity(), area_id="  ", repeats=5)


def test_remote_config_roundtrips_through_mapping() -> None:
    from custom_components.zemismart_blinds.models import RemoteConfig

    remote = RemoteConfig(
        name="Kitchen remote",
        remote=_remote_identity(),
        area_id="kitchen",
        repeats=7,
        coalesce_window_ms=200,
    )
    restored = RemoteConfig.from_entry(remote.as_dict())
    assert restored == remote
    assert restored.remote.bases == TEST_BASES


def test_laminar_conflict_accepts_disjoint_and_nested() -> None:
    from custom_components.zemismart_blinds.models import laminar_conflict

    existing = [(1, 2, 3), (4,), (5,)]
    assert laminar_conflict((6,), existing) is None  # disjoint
    assert laminar_conflict((1, 2, 3, 4, 5, 6), existing) is None  # strict superset of all
    assert laminar_conflict((1,), existing) is None  # strict subset of (1,2,3)


def test_laminar_conflict_rejects_partial_overlap() -> None:
    from custom_components.zemismart_blinds.models import laminar_conflict

    existing = [(1, 2, 3)]
    assert laminar_conflict((2, 3, 4), existing) == "overlapping_channels"
    assert laminar_conflict((3, 4), existing) == "overlapping_channels"


def test_laminar_conflict_rejects_duplicate() -> None:
    from custom_components.zemismart_blinds.models import laminar_conflict

    assert laminar_conflict((1, 2), [(2, 1)]) == "duplicate_channels"


def test_laminar_conflict_normalizes_before_comparing() -> None:
    from custom_components.zemismart_blinds.models import laminar_conflict

    # order/dupes must not matter; nested still passes
    assert laminar_conflict((3, 1), [(1, 2, 3), (1, 3)]) == "duplicate_channels"


def _kitchen_covers() -> list[CoverConfig]:
    from custom_components.zemismart_blinds.models import CoverConfig

    slider = CoverConfig(name="Slider", channels=(1, 2, 3), travel_up=12.0, travel_down=12.0)
    counter = CoverConfig(name="Counter", channels=(4,), travel_up=8.0, travel_down=8.0)
    sink = CoverConfig(name="Sink", channels=(5,), travel_up=9.0, travel_down=9.0)
    allshades = CoverConfig(name="All", channels=(1, 2, 3, 4, 5, 6))
    return [slider, counter, sink, allshades]


def test_derive_role_leaf_and_aggregate() -> None:
    from custom_components.zemismart_blinds.models import Role, derive_role

    covers = _kitchen_covers()
    by_key = {c.channel_key: c for c in covers}
    assert derive_role(by_key["1-2-3"], covers) == Role.LEAF
    assert derive_role(by_key["4"], covers) == Role.LEAF
    assert derive_role(by_key["1-2-3-4-5-6"], covers) == Role.AGGREGATE


def test_member_covers_are_leaves_only() -> None:
    from custom_components.zemismart_blinds.models import member_covers

    covers = _kitchen_covers()
    by_key = {c.channel_key: c for c in covers}
    members = member_covers(by_key["1-2-3-4-5-6"], covers)
    keys = [m.channel_key for m in members]
    # slider (1-2-3), counter (4), sink (5) are leaves inside; nested aggregates excluded.
    assert keys == ["1-2-3", "4", "5"]


def test_member_covers_excludes_nested_aggregates() -> None:
    from custom_components.zemismart_blinds.models import CoverConfig, member_covers

    leaf1 = CoverConfig(name="1", channels=(1,), travel_up=5.0, travel_down=5.0)
    inner = CoverConfig(name="inner", channels=(1, 2))  # aggregate over leaf1
    leaf2 = CoverConfig(name="2", channels=(2,), travel_up=5.0, travel_down=5.0)
    outer = CoverConfig(name="outer", channels=(1, 2, 3))  # aggregate
    leaf3 = CoverConfig(name="3", channels=(3,), travel_up=5.0, travel_down=5.0)
    covers = [leaf1, inner, leaf2, outer, leaf3]
    members = member_covers(outer, covers)
    assert [m.channel_key for m in members] == ["1", "2", "3"]  # inner (1-2) excluded


def test_member_covers_empty_for_leaf() -> None:
    from custom_components.zemismart_blinds.models import member_covers

    covers = _kitchen_covers()
    by_key = {c.channel_key: c for c in covers}
    assert member_covers(by_key["4"], covers) == ()


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


def test_info_only_bridge_keeps_state_sync_metadata() -> None:
    """Contract metadata alone is meaningful retained bridge state."""
    registry = BridgeRegistry()

    registry.update_info("bridge-a", {"boot": 3, "listen": False, "v": 2})

    (bridge,) = registry.bridges
    assert bridge.boot == 3
    assert bridge.listen is False
    assert bridge.contract_v == 2


def test_availability_flip_preserves_state_sync_metadata() -> None:
    """Availability updates do not discard retained contract metadata."""
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"boot": 3, "listen": False, "v": 2})

    registry.update_availability("bridge-a", "online")
    registry.update_availability("bridge-a", "offline")

    (bridge,) = registry.bridges
    assert not bridge.online
    assert bridge.boot == 3
    assert bridge.listen is False
    assert bridge.contract_v == 2


@pytest.mark.parametrize(("boot", "contract_v"), [("3", "2"), (True, False)])
def test_bridge_info_rejects_non_integer_contract_metadata(
    boot: object,
    contract_v: object,
) -> None:
    """Strings and booleans are not strict integer contract metadata."""
    registry = BridgeRegistry()

    registry.update_info(
        "bridge-a",
        {"area": "living_room", "boot": boot, "listen": "false", "v": contract_v},
    )

    (bridge,) = registry.bridges
    assert bridge.boot is None
    assert bridge.listen is None
    assert bridge.contract_v is None


def test_info_tombstone_clears_state_sync_metadata() -> None:
    """A complete retained-info withdrawal clears every metadata field."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    registry.update_info("bridge-a", {"boot": 3, "listen": True, "v": 2})

    registry.update_info("bridge-a", {})

    (bridge,) = registry.bridges
    assert bridge.online
    assert bridge.boot is None
    assert bridge.listen is None
    assert bridge.contract_v is None


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
    clock = {"now": 0.0}

    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        clock["now"] = 1_000.0
        assert hub.handle_status("bridge-a", bytearray(json.dumps(accepted(body)).encode()))
        clock["now"] = 1_010.0
        assert hub.handle_status("bridge-a", bytearray(json.dumps(started(body)).encode()))

    config = blind_config()
    hub = ZemismartHub(
        registry,
        publish,
        ack_timeout=0.001,
        command_id_factory=lambda: "command-1",
        now=lambda: clock["now"],
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


def test_handle_status_drops_unhashable_status_value() -> None:
    """A JSON list status is malformed input, not a callback exception."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)

    assert not hub.handle_status(
        "bridge-a",
        {"status": [], "command_id": "command-1"},
    )


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


def test_started_status_feeds_bridge_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A correlated started status seeds clock conversion from its t/boot pair."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    observed: list[tuple[int, int, float]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status(
            "bridge-a",
            {
                "status": "started",
                "command_id": body["command_id"],
                "t": _STATE_SYNC_T,
                "boot": _STATE_SYNC_BOOT,
            },
        )

    def observe(boot: int, t: int, recv_time: float) -> None:
        observed.append((boot, t, recv_time))

    hub = ZemismartHub(registry, publish, now=lambda: _STATE_SYNC_RECV_TIME)
    bridge_clock = hub._resolve_bridge_clock("bridge-a")
    monkeypatch.setattr(bridge_clock, "observe", observe)

    result = asyncio.run(hub.async_transmit(blind_config(), "UP"))

    assert isinstance(result, CommandAck)
    assert observed == [(_STATE_SYNC_BOOT, _STATE_SYNC_T, _STATE_SYNC_RECV_TIME)]


@pytest.mark.asyncio
async def test_rf_start_records_commanded_start_for_press_staleness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every correlated start records its remote channels and projected time."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    recorded: list[tuple[str, frozenset[int], float]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        accept_and_start(hub, "bridge-a", json.loads(payload))

    hub = ZemismartHub(registry, publish, now=lambda: _STATE_SYNC_RECV_TIME)
    monkeypatch.setattr(
        hub._state_sync,
        "record_commanded_start",
        lambda remote_key, channels, started_at: recorded.append(
            (remote_key, channels, started_at)
        ),
    )
    config = blind_config()

    result = await hub.async_transmit(config, "UP")

    assert isinstance(result, CommandAck)
    assert recorded == [
        (
            config.remote.key,
            frozenset(config.channels),
            _STATE_SYNC_RECV_TIME,
        )
    ]
    hub.close()


@pytest.mark.parametrize(
    ("raw_button", "expected_stamp_count", "expected_buttons"),
    [
        pytest.param("TRAILER", 0, ("DOWN",), id="non-movement-dispatched"),
        pytest.param("UP", 1, (), id="movement-dropped"),
    ],
)
@pytest.mark.asyncio
async def test_raw_command_stamps_start_only_for_movement_frames(
    raw_button: Button,
    expected_stamp_count: int,
    expected_buttons: tuple[str, ...],
) -> None:
    """Only a movement raw frame may outrank an older physical press."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    events: list[HeardEvent] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        accept_and_start(hub, "bridge-a", json.loads(payload))

    hub = ZemismartHub(registry, publish, now=lambda: _STATE_SYNC_RECV_TIME)
    channels = frozenset({1})
    config = blind_config()
    raw = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            channels,
            raw_button,
            bases=TEST_BASES,
        )
    )
    hub.register_rx_listener(config.remote.key, channels, events.append)
    try:
        await hub.async_send_raw("bridge-a", raw, 1)

        assert len(hub._state_sync._commanded_starts) == expected_stamp_count

        hub._state_sync._dispatch_press(
            (config.remote.key, channels, "DOWN"),
            _STATE_SYNC_RECV_TIME - 1.0,
            "bridge-b",
            _STATE_SYNC_RECV_TIME + 1.0,
        )

        assert tuple(event.button for event in events) == expected_buttons
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_started_stamp_rejects_older_press_in_same_callback_batch() -> None:
    """A started callback stamps before a following older broker capture."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    events: list[HeardEvent] = []
    hub: ZemismartHub
    config = blind_config()
    physical_up = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            config.channels,
            "UP",
            bases=TEST_BASES,
        )
    )

    async def publish(topic: str, payload: str) -> None:
        if not topic.endswith("/tx"):
            return
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status(
            "bridge-a",
            {
                "status": "started",
                "command_id": body["command_id"],
                "age_ms": _STARTED_STATUS_AGE_MS,
            },
        )
        # Same synchronous broker callback batch: the transmit awaiter has
        # not resumed when this older, overlapping physical capture arrives.
        hub.handle_rx(
            "bridge-b",
            {
                "frame": physical_up,
                "t": 0,
                "boot": _STATE_SYNC_BOOT,
            },
        )

    hub = ZemismartHub(registry, publish, now=lambda: _STATE_SYNC_RECV_TIME)
    hub._resolve_bridge_clock("bridge-b").observe(
        _STATE_SYNC_BOOT,
        _SEEDED_BRIDGE_T,
        _STATE_SYNC_RECV_TIME,
    )
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), events.append)

    result = await hub.async_transmit(config, "DOWN")

    assert isinstance(result, CommandAck)
    assert events == []
    hub.close()


def test_disarmed_status_routes_to_separate_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disarmed status bypasses command-start pending state and routes directly."""
    routed: list[tuple[str, str]] = []

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)

    def on_disarmed(bridge_id: str, command_id: str) -> None:
        routed.append((bridge_id, command_id))

    monkeypatch.setattr(hub, "on_disarmed", on_disarmed)

    assert hub.handle_status(
        "bridge-a",
        {"status": "disarmed", "command_id": "command-a"},
    )
    assert routed == [("bridge-a", "command-a")]
    assert not hub.handle_status(
        "bridge-a",
        {"status": "unknown", "command_id": "command-a"},
    )


def test_handle_rx_dispatches_to_channel_intersecting_listeners() -> None:
    """A decoded press reaches every listener whose channels intersect it.

    The hub dispatches to any matching listener that shares a channel with the
    press (contained OR partially overlapping); the cover-side callback then
    decides whether to mirror the move or mark itself unknown (design §6.A).
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish, now=lambda: _STATE_SYNC_RECV_TIME)
    member_events: list[HeardEvent] = []
    group_events: list[HeardEvent] = []
    partial_events: list[HeardEvent] = []
    disjoint_events: list[HeardEvent] = []
    remote_key = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    hub.register_rx_listener(remote_key, frozenset({1}), member_events.append)
    hub.register_rx_listener(remote_key, frozenset({1, 2}), group_events.append)
    hub.register_rx_listener(remote_key, frozenset({1, 3}), partial_events.append)
    hub.register_rx_listener(remote_key, frozenset({4}), disjoint_events.append)
    frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1, 2),
            "UP",
            bases=TEST_BASES,
        )
    )

    hub.handle_rx(
        "bridge-a",
        {"frame": frame, "t": _STATE_SYNC_T, "boot": _STATE_SYNC_BOOT},
    )

    expected = HeardEvent(
        button="UP",
        chans=frozenset({1, 2}),
        remote_key=remote_key,
        heard_at=_STATE_SYNC_RECV_TIME,
        bridge_id="bridge-a",
    )
    # Contained (member, group) AND partial-overlap ({1, 3} shares channel 1)
    # all receive the event; a disjoint listener ({4}) does not.
    assert member_events == [expected]
    assert group_events == [expected]
    assert partial_events == [expected]
    assert disjoint_events == []


def test_dispatch_heard_supersedes_only_matched_configured_channels() -> None:
    """Unmatched identities and unconfigured channels cannot grow generations."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    foreign_key = f"{OTHER_PREFIX:06x}:{OTHER_REMOTE_ID:02x}"
    hub._dispatch_heard(
        HeardEvent(
            button="UP",
            chans=frozenset({1}),
            remote_key=foreign_key,
            heard_at=_STATE_SYNC_RECV_TIME,
            bridge_id="bridge-a",
        )
    )
    assert hub._publish_seq == {}

    events: list[HeardEvent] = []
    remote_key = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    hub.register_rx_listener(remote_key, frozenset({1, 2}), events.append)
    event = HeardEvent(
        button="DOWN",
        chans=frozenset({1, 2, 9}),
        remote_key=remote_key,
        heard_at=_STATE_SYNC_RECV_TIME,
        bridge_id="bridge-a",
    )
    hub._dispatch_heard(event)

    assert events == [event]
    assert hub._publish_seq == {
        (remote_key, 1): 1,
        (remote_key, 2): 1,
    }


def test_dispatch_heard_gathers_all_listener_state_before_callbacks() -> None:
    """Every matched listener exposes live state before callbacks mutate it."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    calls: list[str] = []
    remote_key = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"

    def takeover_state(name: str) -> TakeoverCoverState:
        calls.append(f"{name} state")
        return TakeoverCoverState(None, None, None, None, False)

    hub.register_rx_listener(
        remote_key,
        frozenset({1, 2}),
        lambda _event: calls.append("group callback"),
        takeover_state=lambda: takeover_state("group"),
    )
    hub.register_rx_listener(
        remote_key,
        frozenset({1}),
        lambda _event: calls.append("member callback"),
        takeover_state=lambda: takeover_state("member"),
    )

    hub._dispatch_heard(
        HeardEvent(
            button="UP",
            chans=frozenset({1, 2}),
            remote_key=remote_key,
            heard_at=_STATE_SYNC_RECV_TIME,
            bridge_id="bridge-a",
        )
    )

    assert calls == [
        "group state",
        "member state",
        "group callback",
        "member callback",
    ]


def test_handle_rx_maintains_independent_bridge_clocks() -> None:
    """Alternating bridge boots cannot replace one another's correlation."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    clock = {"now": 100.0}
    hub = ZemismartHub(BridgeRegistry(), publish, now=lambda: clock["now"])
    frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1,),
            "UP",
            bases=TEST_BASES,
        )
    )

    hub.handle_rx(
        "bridge-a",
        {"frame": frame, "t": 1_000, "boot": _STATE_SYNC_BOOT},
    )
    clock["now"] = 100.1
    hub.handle_rx(
        "bridge-b",
        {"frame": frame, "t": 9_000, "boot": _STATE_SYNC_BOOT + 1},
    )
    clock["now"] = 101.0
    hub.handle_rx(
        "bridge-a",
        {"frame": frame, "t": 2_000, "boot": _STATE_SYNC_BOOT},
    )
    clock["now"] = 101.1
    hub.handle_rx(
        "bridge-b",
        {"frame": frame, "t": 10_000, "boot": _STATE_SYNC_BOOT + 1},
    )

    assert hub._bridge_clocks["bridge-a"].to_ha_time(
        _STATE_SYNC_BOOT,
        2_500,
        101.5,
    ) == pytest.approx(101.5)
    assert hub._bridge_clocks["bridge-b"].to_ha_time(
        _STATE_SYNC_BOOT + 1,
        10_500,
        101.6,
    ) == pytest.approx(101.6)


def test_bridge_clock_resolver_evicts_least_recently_observed() -> None:
    """Clock correlation stays bounded while retaining a recently used bridge."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    for index in range(models_module._BRIDGE_CLOCK_CAP):
        hub._resolve_bridge_clock(f"bridge-{index}")
    hub._resolve_bridge_clock("bridge-0")

    hub._resolve_bridge_clock("bridge-new")

    assert len(hub._bridge_clocks) == models_module._BRIDGE_CLOCK_CAP
    assert "bridge-0" in hub._bridge_clocks
    assert "bridge-1" not in hub._bridge_clocks


def test_heard_press_from_a_different_remote_reaches_no_listener() -> None:
    """A press from a remote we do not manage never moves our covers."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish, now=lambda: _STATE_SYNC_RECV_TIME)
    events: list[HeardEvent] = []
    remote_key = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    hub.register_rx_listener(remote_key, frozenset({1}), events.append)
    foreign_frame = encode_b0(
        make_payload(OTHER_PREFIX, OTHER_REMOTE_ID, (1,), "UP", bases=OTHER_BASES)
    )

    hub.handle_rx(
        "bridge-a",
        {"frame": foreign_frame, "t": _STATE_SYNC_T, "boot": _STATE_SYNC_BOOT},
    )

    assert events == []


def test_identical_identity_press_is_mirrored_accepted_residual_risk() -> None:
    """A remote with our exact identity is indistinguishable and IS mirrored.

    Documents the accepted trust-and-mirror residual (design §14 / finding 13):
    RF carries no provenance, so a neighbour's remote sharing our 24-bit prefix
    and 8-bit id decodes identically and cannot be told apart from ours. This is
    a known, accepted limitation, not a bug — the assertion guards the behaviour.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish, now=lambda: _STATE_SYNC_RECV_TIME)
    events: list[HeardEvent] = []
    remote_key = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    hub.register_rx_listener(remote_key, frozenset({1}), events.append)
    # A press with our exact prefix/id from ANOTHER physical remote ("bridge-c"
    # heard it) is byte-identical to our own remote's frame.
    identical_frame = encode_b0(
        make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "UP", bases=TEST_BASES)
    )

    hub.handle_rx(
        "bridge-c",
        {"frame": identical_frame, "t": _STATE_SYNC_T, "boot": _STATE_SYNC_BOOT},
    )

    assert events == [
        HeardEvent(
            button="UP",
            chans=frozenset({1}),
            remote_key=remote_key,
            heard_at=_STATE_SYNC_RECV_TIME,
            bridge_id="bridge-c",
        )
    ]


@pytest.mark.asyncio
async def test_pending_command_holds_peer_echo_until_started_confirmation() -> None:
    """A peer capture before STARTED is held, then classified as our echo."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    enqueued = asyncio.Event()
    clock = {"now": _STATE_SYNC_RECV_TIME}

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))
        enqueued.set()

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "ledger-confirmed",
        now=lambda: clock["now"],
    )
    events: list[HeardEvent] = []
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), events.append)
    transmit = asyncio.create_task(
        hub.async_transmit(
            config,
            "DOWN",
            stop_after_ms=_LEDGER_STOP_AFTER_MS,
        )
    )
    try:
        await enqueued.wait()
        body = published[0]
        hub.handle_rx(
            "bridge-b",
            {
                "frame": body["raw"],
                "t": _STATE_SYNC_T,
                "boot": _STATE_SYNC_BOOT,
            },
        )

        assert events == []
        assert len(hub._state_sync._holds) == 1
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status("bridge-a", started(body))
        assert isinstance(await transmit, CommandAck)

        assert events == []
        assert not hub._state_sync._holds
        assert "ledger-confirmed" in hub._recent_emission_proofs

        clock["now"] += _LEDGER_STOP_AFTER_MS / _MILLISECONDS_PER_SECOND
        hub.handle_rx(
            "bridge-b",
            {
                "frame": body["stop_raw"],
                "t": _STATE_SYNC_T + _LEDGER_STOP_AFTER_MS,
                "boot": _STATE_SYNC_BOOT,
            },
        )
        assert events == []
    finally:
        if not transmit.done():
            transmit.cancel()
        hub.close()


@pytest.mark.asyncio
async def test_heard_stop_disarms_published_unstarted_command() -> None:
    """A physical STOP outranks an admitted command before its RF start."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[tuple[str, dict[str, Any]]] = []
    tx_enqueued = asyncio.Event()
    disarm_enqueued = asyncio.Event()

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, json.loads(payload)))
        if topic.endswith("/tx"):
            tx_enqueued.set()
        else:
            disarm_enqueued.set()

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "published-unstarted",
    )
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), lambda _event: None)
    transmit = asyncio.create_task(hub.async_transmit(config, "DOWN"))
    try:
        await tx_enqueued.wait()
        tx_body = published[0][1]
        assert hub.handle_status("bridge-a", accepted(tx_body))

        hub._dispatch_heard(
            HeardEvent(
                button="STOP",
                chans=frozenset(config.channels),
                remote_key=config.remote.key,
                heard_at=_STATE_SYNC_RECV_TIME,
                bridge_id="bridge-b",
            )
        )
        await asyncio.wait_for(disarm_enqueued.wait(), timeout=_DISARM_TEST_DEADLINE_SECONDS)

        assert [item for item in published if item[0].endswith("/cmd")] == [
            (
                "rf433/bridge-a/cmd",
                {"action": "disarm", "command_id": "published-unstarted"},
            )
        ]
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": "published-unstarted"},
        )
        assert await transmit == "superseded"
    finally:
        if not transmit.done():
            transmit.cancel()
        hub.close()


@pytest.mark.asyncio
async def test_heard_press_disarms_confirmed_stop_command() -> None:
    """A physical press aborts remaining RF work after STOP was confirmed."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[tuple[str, dict[str, Any]]] = []
    disarm_enqueued = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            accept_and_start(hub, "bridge-a", body)
        else:
            disarm_enqueued.set()

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "confirmed-stop",
    )
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), lambda _event: None)
    result = await hub.async_transmit(config, "STOP")
    assert isinstance(result, CommandAck)

    hub._dispatch_heard(
        HeardEvent(
            button="UP",
            chans=frozenset(config.channels),
            remote_key=config.remote.key,
            heard_at=_STATE_SYNC_RECV_TIME,
            bridge_id="bridge-b",
        )
    )
    await asyncio.wait_for(disarm_enqueued.wait(), timeout=_DISARM_TEST_DEADLINE_SECONDS)

    assert [item for item in published if item[0].endswith("/cmd")] == [
        (
            "rf433/bridge-a/cmd",
            {"action": "disarm", "command_id": "confirmed-stop"},
        )
    ]
    assert hub.handle_status(
        "bridge-a",
        {"status": "disarmed", "command_id": "confirmed-stop"},
    )
    hub.close()


@pytest.mark.asyncio
async def test_heard_press_does_not_disarm_displaced_confirmed_command() -> None:
    """Takeover leaves a displaced command's STOP drain entry untouched."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[tuple[str, dict[str, Any]]] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if topic.endswith("/tx"):
            accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "displaced-stop",
    )
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), lambda _event: None)
    result = await hub.async_transmit(config, "STOP")
    assert isinstance(result, CommandAck)
    assert hub.handle_status(
        "bridge-a",
        {"status": "displaced", "command_id": "displaced-stop"},
    )

    hub._dispatch_heard(
        HeardEvent(
            button="UP",
            chans=frozenset(config.channels),
            remote_key=config.remote.key,
            heard_at=_STATE_SYNC_RECV_TIME,
            bridge_id="bridge-b",
        )
    )
    await asyncio.sleep(0)

    assert [item for item in published if item[0].endswith("/cmd")] == []
    assert hub._disarm_requests == {}
    hub.close()


@pytest.mark.parametrize("terminal_status", ("rejected", "displaced"))
@pytest.mark.asyncio
async def test_unconfirmed_command_retires_ledger_and_releases_peer_hold(
    terminal_status: str,
) -> None:
    """A command that never starts releases a held peer capture as a press."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    enqueued = asyncio.Event()

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))
        enqueued.set()

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: f"ledger-{terminal_status}",
        now=lambda: _STATE_SYNC_RECV_TIME,
    )
    events: list[HeardEvent] = []
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), events.append)
    transmit = asyncio.create_task(hub.async_transmit(config, "DOWN"))
    try:
        await enqueued.wait()
        body = published[0]
        hub.handle_rx(
            "bridge-b",
            {
                "frame": body["raw"],
                "t": _STATE_SYNC_T,
                "boot": _STATE_SYNC_BOOT,
            },
        )
        assert events == []
        assert len(hub._state_sync._holds) == 1

        assert hub.handle_status(
            "bridge-a",
            {
                "status": terminal_status,
                "command_id": body["command_id"],
            },
        )
        if terminal_status == "rejected":
            with pytest.raises(CommandRejectedError):
                await transmit
        else:
            assert await transmit == "superseded"

        assert not hub._state_sync._holds
        assert len(events) == 1
        assert events[0].button == "DOWN"
        assert events[0].chans == frozenset(config.channels)
    finally:
        if not transmit.done():
            transmit.cancel()
        hub.close()


def test_handle_rx_bounds_forged_bridge_ids() -> None:
    """Unknown RX topic ids cannot allocate more than the bridge registry cap."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish, now=lambda: _STATE_SYNC_RECV_TIME)
    frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1,),
            "UP",
            bases=TEST_BASES,
        )
    )
    payload = {"frame": frame, "t": _STATE_SYNC_T, "boot": _STATE_SYNC_BOOT}

    for index in range(_FORGED_BRIDGE_COUNT):
        hub.handle_rx(f"forged-{index}", payload)

    assert len(hub._rx_bridge_ids) == _BRIDGE_STATE_CAP
    assert len(hub._bridge_clocks) == models_module._BRIDGE_CLOCK_CAP
    assert f"forged-{_FORGED_BRIDGE_COUNT - 1}" not in hub._rx_bridge_ids


def test_close_clears_state_sync_registries_and_stops_callbacks() -> None:
    """Closing the hub clears bounded RX/proof state and disables later dispatch."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish, now=lambda: _STATE_SYNC_RECV_TIME)
    events: list[HeardEvent] = []
    remote_key = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    hub.register_rx_listener(remote_key, frozenset({1}), events.append)
    bridge_clock = hub._resolve_bridge_clock("bridge-a")
    bridge_clock.observe(_STATE_SYNC_BOOT, _STATE_SYNC_T, _STATE_SYNC_RECV_TIME)
    for index in range(_FORGED_BRIDGE_COUNT):
        hub._record_emission_proof(f"command-{index}")

    assert len(hub._recent_emission_proofs) == _BRIDGE_STATE_CAP
    hub.close()

    assert not hub._rx_listeners
    assert not hub._rx_bridge_ids
    assert not hub._bridge_clocks
    assert not bridge_clock.can_project(_STATE_SYNC_BOOT)
    assert not hub._recent_emission_proofs
    frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1,),
            "DOWN",
            bases=TEST_BASES,
        )
    )
    hub.handle_rx(
        "bridge-a",
        {"frame": frame, "t": _STATE_SYNC_T, "boot": _STATE_SYNC_BOOT},
    )
    assert events == []


@pytest.mark.asyncio
async def test_disarm_retries_are_deduped_by_bridge_and_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate takeover requests share one retry task and one waiter."""
    monkeypatch.setattr(
        models_module,
        "_DISARM_RETRY_SECONDS",
        _DISARM_RETRY_TEST_SECONDS,
    )
    published: list[tuple[str, dict[str, Any]]] = []
    acknowledged = asyncio.Event()
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append((topic, body))
        if len(published) == 3:
            hub.on_disarmed("bridge-a", "timed-command")
            acknowledged.set()

    hub = ZemismartHub(BridgeRegistry(), publish)
    deadline = hub._now() + _DISARM_TEST_DEADLINE_SECONDS
    request = hub._start_disarm_request("bridge-a", "timed-command", deadline)
    joined = hub._start_disarm_request("bridge-a", "timed-command", deadline)
    assert joined is request
    try:
        await asyncio.wait_for(acknowledged.wait(), timeout=_DISARM_TEST_DEADLINE_SECONDS)
        await asyncio.sleep(_DISARM_RETRY_TEST_SECONDS)

        assert (
            published
            == [
                (
                    "rf433/bridge-a/cmd",
                    {"action": "disarm", "command_id": "timed-command"},
                ),
            ]
            * 3
        )
        assert request.waiter.done() and not request.waiter.cancelled()
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_joined_disarm_extends_deadline_for_a_late_ack() -> None:
    """A shared waiter accepts an ack after the first requester's deadline."""

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    now = hub._now()
    request = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        now + _DISARM_SHORT_DEADLINE_SECONDS,
    )
    joined = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        now + _DISARM_LONG_DEADLINE_SECONDS,
    )
    assert joined is request
    task = request.task
    assert task is not None
    try:
        await asyncio.sleep(_DISARM_ACK_DELAY_SECONDS)
        hub.on_disarmed("bridge-a", "timed-command")
        await asyncio.wait_for(task, timeout=_DISARM_TEST_DEADLINE_SECONDS)

        assert request.waiter.done() and not request.waiter.cancelled()
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_joined_disarm_times_out_at_widest_deadline() -> None:
    """One deduped request times out at the maximum shared deadline."""
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    now = hub._now()
    request = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        now + _DISARM_SHORT_DEADLINE_SECONDS,
    )
    joined = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        now + _DISARM_LONG_DEADLINE_SECONDS,
    )
    assert joined is request
    task = request.task
    assert task is not None
    try:
        await asyncio.wait_for(task, timeout=_DISARM_TEST_DEADLINE_SECONDS)

        assert loop.time() - started_at >= _DISARM_TIMEOUT_LOWER_BOUND_SECONDS
        assert request.waiter.cancelled()
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_later_cover_owned_press_widens_live_ledger_disarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cover safety horizon widens a generic request through real dispatch."""
    monkeypatch.setattr(
        models_module,
        "_PRESTART_DISARM_DEADLINE_SECONDS",
        _TAKEOVER_GENERIC_DEADLINE_SECONDS,
    )
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    remote_key = f"{TEST_PREFIX:06x}:{TEST_REMOTE_ID:02x}"
    state = TakeoverCoverState(None, None, None, None, False)
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if topic.endswith("/tx"):
            accept_and_start(hub, "bridge-a", json.loads(payload))

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "ledger-command",
    )
    hub.register_rx_listener(
        remote_key,
        frozenset({1}),
        lambda _event: None,
        takeover_state=lambda: state,
    )
    raw_frame = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", bases=TEST_BASES))
    await hub.async_send_raw("bridge-a", raw_frame, 2)
    event = HeardEvent(
        button="DOWN",
        chans=frozenset({1}),
        remote_key=remote_key,
        heard_at=hub._now(),
        bridge_id="synthetic-rx-bridge",
    )
    started_at = asyncio.get_running_loop().time()
    hub._dispatch_heard(event)
    request = hub._disarm_requests[("bridge-a", "ledger-command")]
    generic_deadline = request.deadline
    generic_loop_deadline = request.loop_deadline
    owned_deadline = hub._now() + _TAKEOVER_OWNED_DEADLINE_SECONDS
    state = TakeoverCoverState(
        "bridge-a",
        "ledger-command",
        "UP",
        owned_deadline,
        False,
    )
    task = request.task
    assert task is not None
    try:
        hub._dispatch_heard(event)

        assert hub._disarm_requests[("bridge-a", "ledger-command")] is request
        assert request.deadline == pytest.approx(owned_deadline, abs=0.001, rel=0)
        assert request.deadline > generic_deadline
        assert request.loop_deadline > generic_loop_deadline
        await asyncio.sleep(_TAKEOVER_AFTER_GENERIC_SECONDS)
        assert not task.done()
        await asyncio.wait_for(task, timeout=_DISARM_TEST_DEADLINE_SECONDS)

        assert (
            asyncio.get_running_loop().time() - started_at >= _TAKEOVER_TIMEOUT_LOWER_BOUND_SECONDS
        )
        assert request.waiter.cancelled()
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_disarm_does_not_republish_while_puback_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unacknowledged QoS-1 publish serves every retry cycle."""
    monkeypatch.setattr(
        models_module,
        "_DISARM_RETRY_SECONDS",
        _DISARM_RETRY_TEST_SECONDS,
    )
    published: list[str] = []
    puback = asyncio.Event()

    async def publish(_topic: str, payload: str) -> None:
        published.append(payload)
        await puback.wait()

    hub = ZemismartHub(BridgeRegistry(), publish)
    request = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        hub._now() + _DISARM_PENDING_DEADLINE_SECONDS,
    )
    task = request.task
    assert task is not None
    try:
        await asyncio.wait_for(task, timeout=_DISARM_TEST_DEADLINE_SECONDS)

        assert len(published) == 1
        assert request.waiter.cancelled()
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_disarm_retry_backoff_doubles_and_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acked retries follow the bounded exponential backoff schedule."""
    published: list[str] = []
    retry_schedule: list[float] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        published.append(payload)

    async def record_retry(_request: Any, retry_seconds: float) -> None:
        retry_schedule.append(retry_seconds)
        if len(retry_schedule) == len(_DISARM_BACKOFF_SCHEDULE):
            hub.on_disarmed("bridge-a", "timed-command")
        await asyncio.sleep(0)

    hub = ZemismartHub(BridgeRegistry(), publish)
    monkeypatch.setattr(hub, "_wait_for_disarm_retry", record_retry)
    request = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        hub._now() + _DISARM_TEST_DEADLINE_SECONDS,
    )
    task = request.task
    assert task is not None
    try:
        await asyncio.wait_for(task, timeout=_DISARM_TEST_DEADLINE_SECONDS)

        assert retry_schedule == list(_DISARM_BACKOFF_SCHEDULE)
        assert len(published) == len(_DISARM_BACKOFF_SCHEDULE)
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_close_cancels_disarm_task_and_waiter_before_publish() -> None:
    """Final unload cancels a scheduled disarm before it can publish later."""
    published: list[tuple[str, str]] = []

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, payload))

    hub = ZemismartHub(BridgeRegistry(), publish)
    request = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        hub._now() + _DISARM_TEST_DEADLINE_SECONDS,
    )
    task = request.task
    assert task is not None

    hub.close()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert request.waiter.cancelled()
    assert not hub._disarm_requests
    assert published == []


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
async def test_enqueue_racing_close_on_contended_lock_is_superseded() -> None:
    """A command reaching _queue_ready after close() supersedes; no worker resurrection."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))

    hub = ZemismartHub(registry, publish)
    # Hold _queue_ready so the transmit blocks on a CONTENDED acquire (the entry
    # _closed check only covers the uncontended fast path), then close() while
    # it waits. On acquiring the lock it must re-check _closed and supersede
    # rather than queue the command and resurrect the worker via _ensure_worker.
    async with hub._queue_ready:
        pending = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
        await asyncio.sleep(0.01)
        hub.close()
    result = await asyncio.wait_for(pending, timeout=0.1)
    assert result == "superseded"
    assert published == []
    assert hub._worker_task is None


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
async def test_execute_drains_started_exception_after_prestart_disarm() -> None:
    """Admission displacement cannot orphan a later started exception."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    captured_pending: list[Any] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        if not topic.endswith("/tx"):
            return
        body: dict[str, Any] = json.loads(payload)
        command_id = str(body["command_id"])
        hub._start_disarm_request(
            "bridge-a",
            command_id,
            hub._now() + _DISARM_TEST_DEADLINE_SECONDS,
        )
        assert hub.handle_status(
            "bridge-a",
            {"status": "disarmed", "command_id": command_id},
        )
        assert hub.handle_status(
            "bridge-a",
            {"status": "displaced", "command_id": command_id},
        )

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "double-resolve",
    )
    original_register_pending = hub._register_pending

    def capture_pending(
        bridge: Any,
        command_id: str,
        remote_key: str | None,
        channels: frozenset[int],
    ) -> Any:
        pending = original_register_pending(bridge, command_id, remote_key, channels)
        captured_pending.append(pending)
        return pending

    hub._register_pending = capture_pending  # type: ignore[method-assign]

    result = await hub.async_transmit(blind_config(), "UP")

    assert result == "superseded"
    pending = captured_pending[0]
    assert pending.started.done()
    assert not pending.started.cancelled()
    assert not pending.started._log_traceback
    hub.close()


@pytest.mark.asyncio
async def test_displaced_status_rewindows_confirmed_stop_echoes() -> None:
    """Flushed STOPs are echoes, while a STOP at the freed deadline is physical."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    events: list[HeardEvent] = []
    clock = {"now": _STATE_SYNC_RECV_TIME}
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "displaced-confirmed",
        now=lambda: clock["now"],
    )
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), events.append)
    result = await hub.async_transmit(
        config,
        "DOWN",
        stop_after_ms=_DISPLACED_STOP_AFTER_MS,
    )
    assert isinstance(result, CommandAck)
    body = published[0]

    clock["now"] = _DISPLACED_AT
    assert hub.handle_status(
        "bridge-a",
        {"status": "displaced", "command_id": "displaced-confirmed"},
    )
    clock["now"] = _DISPLACED_FLUSH_AT
    hub.handle_rx(
        "bridge-b",
        {
            "frame": body["stop_raw"],
            "t": _STATE_SYNC_T,
            "boot": _STATE_SYNC_BOOT,
        },
    )
    assert events == []

    clock["now"] = _DISPLACED_ORIGINAL_STOP_AT
    hub.handle_rx(
        "bridge-b",
        {
            "frame": body["stop_raw"],
            "t": _DISPLACED_ORIGINAL_STOP_T,
            "boot": _STATE_SYNC_BOOT,
        },
    )
    assert [event.button for event in events] == ["STOP"]
    hub.close()


@pytest.mark.asyncio
async def test_disarm_ack_keeps_displaced_stop_drain_suppressed() -> None:
    """A disarm ack preserves echo suppression for a displaced flushed STOP."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    events: list[HeardEvent] = []
    clock = {"now": _STATE_SYNC_RECV_TIME}
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if topic.endswith("/tx"):
            published.append(body)
            accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "displaced-disarmed",
        now=lambda: clock["now"],
    )
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), events.append)
    result = await hub.async_transmit(
        config,
        "DOWN",
        stop_after_ms=_DISPLACED_STOP_AFTER_MS,
    )
    assert isinstance(result, CommandAck)
    body = published[0]

    clock["now"] = _DISPLACED_AT
    assert hub.handle_status(
        "bridge-a",
        {"status": "displaced", "command_id": "displaced-disarmed"},
    )
    hub._start_disarm_request(
        "bridge-a",
        "displaced-disarmed",
        _DISPLACED_AT + _DISARM_TEST_DEADLINE_SECONDS,
    )
    assert hub.handle_status(
        "bridge-a",
        {"status": "disarmed", "command_id": "displaced-disarmed"},
    )

    clock["now"] = _DISPLACED_FLUSH_AT
    hub.handle_rx(
        "bridge-b",
        {
            "frame": body["stop_raw"],
            "t": _STATE_SYNC_T,
            "boot": _STATE_SYNC_BOOT,
        },
    )

    assert events == []
    hub.close()


@pytest.mark.asyncio
async def test_started_then_displaced_broker_batch_still_rewindows_stops() -> None:
    """A displaced arriving before the started awaiter resumes keeps the drain.

    One broker callback batch can resolve the started future AND deliver the
    displaced status before _async_execute resumes to confirm the ledger.
    displace() must not retire the still-pending entry (the flushed STOPs
    would dispatch as physical presses), and the awaiter's later confirm must
    not resurrect the original-deadline windows over the drain re-window.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    events: list[HeardEvent] = []
    clock = {"now": _STATE_SYNC_RECV_TIME}
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)
        # Same callback batch: the started future is resolved but its awaiter
        # has not resumed yet when the displacement lands.
        assert hub.handle_status(
            "bridge-a",
            {"status": "displaced", "command_id": body["command_id"]},
        )

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "displaced-race",
        now=lambda: clock["now"],
    )
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), events.append)
    result = await hub.async_transmit(
        config,
        "DOWN",
        stop_after_ms=_DISPLACED_STOP_AFTER_MS,
    )
    assert isinstance(result, CommandAck)
    body = published[0]

    clock["now"] = _STATE_SYNC_RECV_TIME + 0.1
    hub.handle_rx(
        "bridge-b",
        {
            "frame": body["stop_raw"],
            "t": _STATE_SYNC_T,
            "boot": _STATE_SYNC_BOOT,
        },
    )
    assert events == []

    clock["now"] = _DISPLACED_ORIGINAL_STOP_AT
    hub.handle_rx(
        "bridge-b",
        {
            "frame": body["stop_raw"],
            "t": _DISPLACED_ORIGINAL_STOP_T,
            "boot": _STATE_SYNC_BOOT,
        },
    )
    assert [event.button for event in events] == ["STOP"]
    hub.close()


@pytest.mark.asyncio
async def test_started_projection_clamped_to_delivery_is_rejected() -> None:
    """A clamped projection never anchors a delayed delivery at NOW.

    With a small age_ms the corroboration tolerance alone would accept a
    projection that to_ha_time clamped to recv_time; the exact-recv guard
    must reject it and keep the recv - age baseline anchor.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    clock = {"now": _STATE_SYNC_RECV_TIME}
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status(
            "bridge-a",
            {
                "status": "started",
                "command_id": body["command_id"],
                "age_ms": _CLAMPED_AGE_MS,
                "t": _CLAMPED_STATUS_T,
                "boot": _STATE_SYNC_BOOT,
            },
        )

    hub = ZemismartHub(registry, publish, now=lambda: clock["now"])
    seed_frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1,),
            "UP",
            bases=TEST_BASES,
        )
    )
    hub.handle_rx(
        "bridge-a",
        {"frame": seed_frame, "t": _SEEDED_BRIDGE_T, "boot": _STATE_SYNC_BOOT},
    )
    clock["now"] = _CLAMPED_DELIVERY_TIME

    ack = await hub.async_transmit(blind_config(), "DOWN")

    assert isinstance(ack, CommandAck)
    assert ack.started_at == pytest.approx(
        _CLAMPED_DELIVERY_TIME - _CLAMPED_AGE_MS / _MILLISECONDS_PER_SECOND
    )


@pytest.mark.asyncio
async def test_disarm_after_resolved_waiter_starts_fresh_request() -> None:
    """A takeover after a completed disarm gets its own live request.

    Joining a request whose waiter already resolved would strand the new
    requester: the finished task never retries or reaches the new deadline.
    """

    async def publish(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publish)
    now = hub._now()

    old_request = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        now + _DISARM_LONG_DEADLINE_SECONDS,
    )
    # Resolve the first request and re-request in the SAME loop step, before
    # the old task's finally block can clear its slot.
    hub.on_disarmed("bridge-a", "timed-command")
    new_request = hub._start_disarm_request(
        "bridge-a",
        "timed-command",
        hub._now() + _DISARM_SHORT_DEADLINE_SECONDS,
    )
    try:
        assert new_request is not old_request
        task = new_request.task
        assert task is not None
        await asyncio.wait_for(task, timeout=_DISARM_TEST_DEADLINE_SECONDS)
        assert new_request.waiter.cancelled()
    finally:
        hub.close()


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


@pytest.mark.asyncio
async def test_coalescing_never_merges_across_an_overlapping_command() -> None:
    """A sibling behind an overlapping intervening command keeps its order.

    UP ch1, DOWN group {2,3}, UP ch2 within one window: merging UP ch2 into
    the leading UP would emit UP {1,2} BEFORE the older DOWN {2,3}, letting
    the older DOWN win channel 2 on air. The merge is barred and the three
    commands publish in arrival order.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    up_one = config_with_window(replace(blind_config(), channels=(1,)), 20)
    down_group = replace(blind_config(), channels=(2, 3))
    up_two = config_with_window(replace(blind_config(), channels=(2,)), 20)
    first = asyncio.create_task(hub.async_transmit(up_one, "UP"))
    await asyncio.sleep(0)
    second = asyncio.create_task(hub.async_transmit(down_group, "DOWN"))
    await asyncio.sleep(0)
    third = asyncio.create_task(hub.async_transmit(up_two, "UP"))
    await asyncio.gather(first, second, third)

    assert [body["target"] for body in published] == [
        "a1b2c3:42:1",
        "a1b2c3:42:2,3",
        "a1b2c3:42:2",
    ]


@pytest.mark.asyncio
async def test_followup_commands_stick_to_the_motion_bridge() -> None:
    """Consecutive commands for one remote route through one bridge.

    A timed movement starts through the only online bridge; a same-area
    bridge (which resolve() would now prefer) then announces. The follow-up
    STOP must go to the original bridge -- it holds the scheduler state and
    the armed fail-safe STOP -- and affinity breaks only when that bridge
    reports offline.
    """
    registry = BridgeRegistry()
    registry.update_info("bridge-b", {"area": "somewhere_else"})
    registry.update_availability("bridge-b", "online")
    topics: list[str] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        topics.append(topic)
        accept_and_start(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(registry, publish)
    await hub.async_transmit(blind_config(), "UP", stop_after_ms=5000)
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "online")
    await hub.async_transmit(blind_config(), "STOP")

    assert topics == ["rf433/bridge-b/tx", "rf433/bridge-b/tx"]

    registry.update_availability("bridge-b", "offline")
    await hub.async_transmit(blind_config(), "STOP")
    assert topics[-1] == "rf433/bridge-a/tx"


@pytest.mark.asyncio
async def test_displaced_raw_command_raises_a_command_error() -> None:
    """A raw frame displaced pre-start surfaces as a command failure.

    A second controller sharing the bridge can displace the raw frame in its
    pre-start window; the service caller gets a reportable error, never an
    AssertionError.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published_event = asyncio.Event()
    ids = iter(("raw-1",))
    hub: ZemismartHub

    async def publish(_topic: str, _payload: str) -> None:
        published_event.set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    frame = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "UP", bases=TEST_BASES))
    task = asyncio.create_task(hub.async_send_raw("bridge-a", frame, 3))
    await published_event.wait()
    assert hub.handle_status("bridge-a", {"status": "displaced", "command_id": "raw-1"})

    with pytest.raises(CommandRejectedError, match="displaced"):
        await task


def test_registry_distinguishes_undiscovered_from_known_offline() -> None:
    """Only an explicitly reported offline availability counts as offline."""
    registry = BridgeRegistry()
    assert not registry.is_known_offline("bridge-a")
    registry.update_info("bridge-a", {"area": "living_room"})
    assert not registry.is_known_offline("bridge-a")  # info only, no availability
    registry.update_availability("bridge-a", "offline")
    assert registry.is_known_offline("bridge-a")
    registry.update_availability("bridge-a", "online")
    assert not registry.is_known_offline("bridge-a")


@pytest.mark.asyncio
async def test_affinity_is_partitioned_by_area() -> None:
    """Affinity never routes a command through another area's bridge.

    Channels of one remote can live in rooms served by different bridges;
    consecutive commands in different areas each use their own area's bridge.
    """
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room"})
    registry.update_availability("bridge-a", "online")
    registry.update_info("bridge-b", {"area": "bedroom"})
    registry.update_availability("bridge-b", "online")
    topics: list[str] = []
    hub: ZemismartHub

    async def publish(topic: str, payload: str) -> None:
        topics.append(topic)
        accept_and_start(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(registry, publish)
    living = replace(blind_config(), channels=(1,))
    bedroom = replace(blind_config(area_id="bedroom"), channels=(2,))
    await hub.async_transmit(living, "UP")
    await hub.async_transmit(bedroom, "UP")

    assert topics == ["rf433/bridge-a/tx", "rf433/bridge-b/tx"]


@pytest.mark.asyncio
async def test_replayed_started_age_anchors_the_original_rf_start() -> None:
    """A started status carrying age_ms back-dates the model's start time."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status(
            "bridge-a",
            {"status": "started", "command_id": body["command_id"], "age_ms": 5_000},
        )

    clock = {"now": 100.0}
    hub = ZemismartHub(registry, publish, now=lambda: clock["now"])
    ack = await hub.async_transmit(blind_config(), "UP")

    assert isinstance(ack, CommandAck)
    assert ack.started_at == pytest.approx(95.0)


@pytest.mark.asyncio
async def test_started_status_projects_bridge_handoff_before_delivery() -> None:
    """A seeded bridge clock removes network delay from STARTED handoff time."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    clock = {"now": 100.0}
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status(
            "bridge-a",
            {
                "status": "started",
                "command_id": body["command_id"],
                "age_ms": _STARTED_STATUS_AGE_MS,
                "t": _STARTED_STATUS_T,
                "boot": _STATE_SYNC_BOOT,
            },
        )

    hub = ZemismartHub(registry, publish, now=lambda: clock["now"])
    seed_frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1,),
            "UP",
            bases=TEST_BASES,
        )
    )
    hub.handle_rx(
        "bridge-a",
        {"frame": seed_frame, "t": _SEEDED_BRIDGE_T, "boot": _STATE_SYNC_BOOT},
    )
    clock["now"] = _STARTED_DELIVERY_TIME

    ack = await hub.async_transmit(blind_config(), "DOWN")

    assert isinstance(ack, CommandAck)
    assert ack.started_at == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_started_status_without_seed_falls_back_to_delivery_age() -> None:
    """An unseeded bridge uses receive time minus the reported bridge age."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status(
            "bridge-a",
            {
                "status": "started",
                "command_id": body["command_id"],
                "age_ms": _STARTED_STATUS_AGE_MS,
                "t": _STARTED_STATUS_T,
                "boot": _STATE_SYNC_BOOT,
            },
        )

    hub = ZemismartHub(
        registry,
        publish,
        now=lambda: _STARTED_DELIVERY_TIME,
    )

    ack = await hub.async_transmit(blind_config(), "DOWN")

    assert isinstance(ack, CommandAck)
    assert ack.started_at == pytest.approx(
        _STARTED_DELIVERY_TIME - _STARTED_STATUS_AGE_MS / _MILLISECONDS_PER_SECOND
    )


@pytest.mark.asyncio
async def test_replayed_started_with_large_age_keeps_age_anchor() -> None:
    """A replay older than the projection clamp still back-dates by age_ms.

    to_ha_time collapses any projection older than 30 s to receive time; a
    QoS-1 replayed STARTED can be legitimately minutes old, so the clamped
    projection must be rejected in favor of the recv - age baseline anchor
    instead of anchoring the model at delivery time.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    clock = {"now": 100.0}
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        assert hub.handle_status("bridge-a", accepted(body))
        assert hub.handle_status(
            "bridge-a",
            {
                "status": "started",
                "command_id": body["command_id"],
                "age_ms": _REPLAY_AGE_MS,
                "t": _REPLAY_STATUS_T,
                "boot": _STATE_SYNC_BOOT,
            },
        )

    hub = ZemismartHub(registry, publish, now=lambda: clock["now"])
    seed_frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1,),
            "UP",
            bases=TEST_BASES,
        )
    )
    hub.handle_rx(
        "bridge-a",
        {"frame": seed_frame, "t": _SEEDED_BRIDGE_T, "boot": _STATE_SYNC_BOOT},
    )
    clock["now"] = _REPLAY_DELIVERY_TIME

    ack = await hub.async_transmit(blind_config(), "DOWN")

    assert isinstance(ack, CommandAck)
    assert ack.started_at == pytest.approx(
        _REPLAY_DELIVERY_TIME - _REPLAY_AGE_MS / _MILLISECONDS_PER_SECOND
    )


@pytest.mark.asyncio
async def test_stop_queues_behind_an_overlapping_queued_raw_frame() -> None:
    """A fast-lane STOP never jumps an earlier overlapping raw debug frame.

    Publishing the STOP first would let the raw frame displace it on the
    bridge and re-drive the just-stopped motor.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    publish_events = [asyncio.Event() for _ in range(3)]
    ids = iter(("blocker", "raw-1", "stop-1"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        publish_events[len(published) - 1].set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    blocker_config = replace(blind_config(), channels=(3,))
    blocker = asyncio.create_task(hub.async_transmit(blocker_config, "UP"))
    await publish_events[0].wait()

    frame = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "UP", bases=TEST_BASES))
    raw = asyncio.create_task(hub.async_send_raw("bridge-a", frame, 3))
    await asyncio.sleep(0)
    stop = asyncio.create_task(hub.async_transmit(replace(blind_config(), channels=(1,)), "STOP"))
    await asyncio.sleep(0)
    # The STOP overlaps the queued raw frame: both wait for the blocker in
    # request order instead of the STOP taking the fast lane.
    assert len(published) == 1

    accept_and_start(hub, "bridge-a", published[0])
    await blocker
    await publish_events[1].wait()
    assert published[1]["command_id"] == "raw-1"
    accept_and_start(hub, "bridge-a", published[1])
    await raw
    await publish_events[2].wait()
    assert published[2]["command_id"] == "stop-1"
    accept_and_start(hub, "bridge-a", published[2])
    await stop


def test_travel_time_is_bounded_by_the_firmware_stop_cap() -> None:
    """A travel calibration above one hour cannot produce rejected partial moves."""
    with pytest.raises(ValueError, match="travel"):
        replace(blind_config(), travel_up=3601.0)


def test_as_dict_always_emits_the_trailer_marker() -> None:
    """A trailer-less calibration stores an explicit empty base_trailer.

    Options merge over entry data: without the marker, a stale data-layer
    trailer base could never be removed through the options flow.
    """
    trailerless = replace(
        blind_config(),
        remote=RemoteIdentity(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            CommandBases(up=TEST_BASES.up, down=TEST_BASES.down, stop=TEST_BASES.stop),
        ),
    )
    values = trailerless.as_dict()
    assert values["base_trailer"] == ""
    restored = BlindConfig.from_mapping(values)
    assert restored.remote.bases is not None
    assert restored.remote.bases.trailer is None


@pytest.mark.asyncio
async def test_stop_publishes_while_overlapping_movement_awaits_ack() -> None:
    """A STOP behind an already-published overlapping movement goes out now.

    Only PUBLICATION order must match request order. Waiting for the
    movement's full acknowledgement lifecycle would park the safety STOP for
    up to the 30-second started timeout while the blind keeps moving.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    publish_events = [asyncio.Event() for _ in range(2)]
    ids = iter(("move-1", "stop-1"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        publish_events[len(published) - 1].set()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    move = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
    await publish_events[0].wait()

    stop = asyncio.create_task(hub.async_transmit(replace(blind_config(), channels=(1,)), "STOP"))
    # The movement is published but entirely unacknowledged; the STOP must
    # still reach the broker (in order, after the movement).
    await publish_events[1].wait()
    assert [body["command_id"] for body in published] == ["move-1", "stop-1"]

    accept_and_start(hub, "bridge-a", published[0])
    accept_and_start(hub, "bridge-a", published[1])
    await move
    await stop


@pytest.mark.asyncio
async def test_movement_publishes_after_an_earlier_unpublished_fast_stop() -> None:
    """Per-channel request order holds across the fast lane and the worker.

    STOP ch1, STOP {1,2} chained behind it, then UP ch2 must reach the broker
    in that order (paho preserves publish-call order), or the older STOP would
    displace the newest intent on the bridge. Crucially, the later commands
    enqueue in order WITHOUT waiting for STOP ch1's QoS-1 acknowledgment: the
    publish-order barrier releases on enqueue, not on PUBACK, so a safety STOP
    is never stranded behind an earlier command's broker ack.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    all_enqueued = asyncio.Event()
    release_first_ack = asyncio.Event()
    ids = iter(("stop-1", "stop-12", "up-2"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        # Model real HA/paho: the message is enqueued (ordered broker receipt)
        # synchronously, then the QoS-1 PUBACK is awaited. Withhold only
        # STOP ch1's PUBACK.
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        if len(published) == 3:
            all_enqueued.set()
        if body["command_id"] == "stop-1":
            await release_first_ack.wait()

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    stop_one = asyncio.create_task(
        hub.async_transmit(replace(blind_config(), channels=(1,)), "STOP")
    )
    await asyncio.sleep(0)
    stop_group = asyncio.create_task(hub.async_transmit(blind_config(), "STOP"))
    await asyncio.sleep(0)
    up_two = asyncio.create_task(hub.async_transmit(replace(blind_config(), channels=(2,)), "UP"))

    # All three enqueue in request order even though STOP ch1's PUBACK is
    # still withheld — the barrier did not wait on the acknowledgment.
    await all_enqueued.wait()
    assert [body["command_id"] for body in published] == ["stop-1", "stop-12", "up-2"]

    release_first_ack.set()
    for body in published:
        accept_and_start(hub, "bridge-a", body)
    await asyncio.gather(stop_one, stop_group, up_two)


@pytest.mark.asyncio
async def test_cancelled_chained_stop_is_never_transmitted() -> None:
    """A chained STOP whose caller cancelled must not still reach RF.

    Without the post-barrier recheck, the resolved group STOP would publish
    anyway and unexpectedly halt the second channel.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    release_first = asyncio.Event()
    ids = iter(("stop-1", "stop-12"))
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        if body["command_id"] == "stop-1":
            await release_first.wait()
        published.append(body)

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: next(ids))
    stop_one = asyncio.create_task(
        hub.async_transmit(replace(blind_config(), channels=(1,)), "STOP")
    )
    await asyncio.sleep(0)
    stop_group = asyncio.create_task(hub.async_transmit(blind_config(), "STOP"))
    await asyncio.sleep(0)

    stop_group.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_group
    release_first.set()
    await asyncio.sleep(0.01)

    assert [body["command_id"] for body in published] == ["stop-1"]
    accept_and_start(hub, "bridge-a", published[0])
    await stop_one


def test_empty_availability_payload_is_not_an_offline_report() -> None:
    """A retained-topic deletion clears knowledge instead of reporting offline."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    registry.update_availability("bridge-a", "")
    assert not registry.is_known_offline("bridge-a")
    registry.update_availability("bridge-a", "offline")
    assert registry.is_known_offline("bridge-a")


def test_bridge_registry_prunes_tombstones_and_bounds_wildcard_discovery() -> None:
    """Withdrawn and forged wildcard IDs cannot grow the registry forever."""
    registry = BridgeRegistry()
    registry.update_availability("availability-tombstone", "")
    registry.update_info("info-tombstone", {})
    assert registry.bridges == ()

    for index in range(300):
        registry.update_availability(f"bridge-{index}", "online")

    assert len(registry.bridges) == 256


def test_coalesce_window_is_bounded() -> None:
    """A hand-edited giant window cannot delay every command."""
    with pytest.raises(ValueError, match="coalesce_window_ms"):
        config_with_window(blind_config(), 2001)


def test_parse_channels_rejects_fractional_values() -> None:
    """Stored fractional channels are rejected, never silently truncated."""
    with pytest.raises(ValueError, match="whole number"):
        parse_channels([2.9])


@pytest.mark.asyncio
async def test_stale_overlap_token_supersedes_the_movement() -> None:
    """A multi-frame operation aborts when its channels moved on since.

    The caller snapshots the per-channel publish state between its STOP and
    its movement; any overlapping publication in between means a newer
    intent owns the channels and the stale movement resolves superseded.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    config = blind_config()
    token = hub.overlap_token(config)
    # An overlapping command publishes after the snapshot.
    await hub.async_transmit(replace(config, channels=(2,)), "UP")

    result = await hub.async_transmit(config, "DOWN", overlap_token=token)
    assert result == "superseded"
    assert len(published) == 1  # the stale movement never reached the broker

    # A fresh token publishes normally.
    fresh = await hub.async_transmit(config, "DOWN", overlap_token=hub.overlap_token(config))
    assert isinstance(fresh, CommandAck)
    assert len(published) == 2


@pytest.mark.asyncio
async def test_heard_press_invalidates_overlap_token_before_publish() -> None:
    """A physical press makes a pre-press set-position movement stale."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish, now=lambda: _STATE_SYNC_RECV_TIME)
    config = blind_config()
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), lambda _event: None)
    token = hub.overlap_token(config)
    physical_up = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            config.channels,
            "UP",
            bases=TEST_BASES,
        )
    )

    hub.handle_rx(
        "bridge-b",
        {
            "frame": physical_up,
            "t": _STATE_SYNC_T,
            "boot": _STATE_SYNC_BOOT,
        },
    )
    result = await hub.async_transmit(
        config,
        "DOWN",
        stop_after_ms=_LEDGER_STOP_AFTER_MS,
        overlap_token=token,
    )

    assert result == "superseded"
    assert published == []
    hub.close()


@pytest.mark.asyncio
async def test_overlap_token_is_rechecked_after_waiting_for_publish_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A physical press during lock contention prevents the stale timed RF."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    rebuilt = asyncio.Event()
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    original_rebuild = hub._rebuild_from_live_contributors

    def record_rebuild(command: Any) -> None:
        original_rebuild(command)
        rebuilt.set()

    monkeypatch.setattr(hub, "_rebuild_from_live_contributors", record_rebuild)
    config = blind_config()
    token = hub.overlap_token(config)
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), lambda _event: None)

    async with hub._publish_lock:
        transmit = asyncio.create_task(
            hub.async_transmit(
                config,
                "DOWN",
                stop_after_ms=_LEDGER_STOP_AFTER_MS,
                overlap_token=token,
            )
        )
        await rebuilt.wait()
        hub._dispatch_heard(
            HeardEvent(
                button="UP",
                chans=frozenset(config.channels),
                remote_key=config.remote.key,
                heard_at=_STATE_SYNC_RECV_TIME,
                bridge_id="bridge-b",
            )
        )

    assert await transmit == "superseded"
    assert published == []
    hub.close()


@pytest.mark.asyncio
async def test_scheduled_heard_press_supersedes_full_move_before_publisher_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready press callback wins the final no-await check before paho enqueue."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    config = blind_config()
    event = HeardEvent(
        button="UP",
        chans=frozenset(config.channels),
        remote_key=config.remote.key,
        heard_at=_STATE_SYNC_RECV_TIME,
        bridge_id="bridge-b",
    )
    hub.register_rx_listener(config.remote.key, frozenset(config.channels), lambda _event: None)
    original_register_pending = hub._register_pending

    def register_and_schedule_press(
        bridge: Any,
        command_id: str,
        remote_key: str | None,
        channels: frozenset[int],
    ) -> Any:
        pending = original_register_pending(bridge, command_id, remote_key, channels)
        asyncio.get_running_loop().call_soon(hub._dispatch_heard, event)
        return pending

    monkeypatch.setattr(hub, "_register_pending", register_and_schedule_press)

    result = await hub.async_transmit(config, "DOWN")

    assert result == "superseded"
    assert published == []
    hub.close()


@pytest.mark.asyncio
async def test_scheduled_cancellation_prevents_publisher_wrapper_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cancelled after the outer guard never publishes or registers."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    captured_commands: list[Any] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "cancelled-before-wrapper",
    )
    original_register_pending = hub._register_pending

    def register_and_schedule_cancel(
        bridge: Any,
        command_id: str,
        remote_key: str | None,
        channels: frozenset[int],
    ) -> Any:
        pending = original_register_pending(bridge, command_id, remote_key, channels)
        command = hub._inflight
        assert command is not None
        captured_commands.append(command)
        asyncio.get_running_loop().call_soon(command.futures[0].cancel)
        return pending

    monkeypatch.setattr(hub, "_register_pending", register_and_schedule_cancel)
    transmit = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
    try:
        with pytest.raises(asyncio.CancelledError):
            await transmit
        command = captured_commands[0]
        assert command.published is not None
        await asyncio.wait_for(
            command.published.wait(),
            timeout=_DISARM_TEST_DEADLINE_SECONDS,
        )

        assert published == []
        assert "cancelled-before-wrapper" not in hub._ledger._entries
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_cancelled_contributor_channel_is_dropped_from_the_batch() -> None:
    """A coalesced batch rebuilds from LIVE contributors before publishing.

    The cancelled caller's channel must not move with the surviving batch.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    first_config = config_with_window(replace(blind_config(), channels=(1,)), 30)
    second_config = config_with_window(replace(blind_config(), channels=(2,)), 30)
    first = asyncio.create_task(hub.async_transmit(first_config, "UP"))
    second = asyncio.create_task(hub.async_transmit(second_config, "UP"))
    await asyncio.sleep(0)

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert isinstance(await first, CommandAck)

    assert len(published) == 1
    assert published[0]["target"] == "a1b2c3:42:1"


@pytest.mark.asyncio
async def test_cancelled_contributor_is_rechecked_after_publish_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation while waiting to publish removes that caller's channel."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    rebuilt = asyncio.Event()
    original_rebuild = hub._rebuild_from_live_contributors

    def record_rebuild(command: Any) -> None:
        original_rebuild(command)
        rebuilt.set()

    monkeypatch.setattr(hub, "_rebuild_from_live_contributors", record_rebuild)
    first_config = config_with_window(replace(blind_config(), channels=(1,)), 10)
    second_config = config_with_window(replace(blind_config(), channels=(2,)), 10)

    async with hub._publish_lock:
        first = asyncio.create_task(hub.async_transmit(first_config, "UP"))
        second = asyncio.create_task(hub.async_transmit(second_config, "UP"))
        await rebuilt.wait()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    assert isinstance(await first, CommandAck)
    assert len(published) == 1
    assert published[0]["target"] == "a1b2c3:42:1"


@pytest.mark.asyncio
async def test_ledger_registers_the_final_under_lock_coalesced_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled contributor cannot leave a stale union echo envelope."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    events: list[HeardEvent] = []
    enqueued = asyncio.Event()
    rebuilt = asyncio.Event()
    prelock_channels: list[frozenset[int]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))
        enqueued.set()

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "ledger-final-frame",
        now=lambda: _STATE_SYNC_RECV_TIME,
    )
    original_rebuild = hub._rebuild_from_live_contributors

    def record_rebuild(command: Any) -> None:
        original_rebuild(command)
        prelock_channels.append(command.channels)
        rebuilt.set()

    monkeypatch.setattr(hub, "_rebuild_from_live_contributors", record_rebuild)
    first_config = config_with_window(replace(blind_config(), channels=(1,)), 10)
    second_config = config_with_window(replace(blind_config(), channels=(2,)), 10)
    hub.register_rx_listener(
        first_config.remote.key,
        frozenset({1, 2}),
        events.append,
    )

    async with hub._publish_lock:
        first = asyncio.create_task(hub.async_transmit(first_config, "UP"))
        second = asyncio.create_task(hub.async_transmit(second_config, "UP"))
        await rebuilt.wait()
        assert prelock_channels[0] == frozenset({1, 2})
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    await enqueued.wait()
    entry = hub._ledger._entries["ledger-final-frame"]
    assert entry.channels == (1,)
    assert {frame.signature[1] for frame in entry.frames} == {frozenset({1})}
    original_union_frame = encode_b0(
        make_payload(
            TEST_PREFIX,
            TEST_REMOTE_ID,
            (1, 2),
            "UP",
            bases=TEST_BASES,
        )
    )
    hub.handle_rx(
        "bridge-b",
        {
            "frame": original_union_frame,
            "t": _STATE_SYNC_T,
            "boot": _STATE_SYNC_BOOT,
        },
    )
    accept_and_start(hub, "bridge-a", published[0])

    assert isinstance(await first, CommandAck)
    assert [event.chans for event in events] == [frozenset({1, 2})]
    hub.close()


@pytest.mark.asyncio
async def test_started_stamp_uses_final_under_lock_coalesced_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled contributor cannot leave a stale commanded-start channel."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    events: list[HeardEvent] = []
    enqueued = asyncio.Event()
    rebuilt = asyncio.Event()

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))
        enqueued.set()

    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "final-started-channels",
        now=lambda: _STATE_SYNC_RECV_TIME,
    )
    original_rebuild = hub._rebuild_from_live_contributors

    def record_rebuild(command: Any) -> None:
        original_rebuild(command)
        rebuilt.set()

    monkeypatch.setattr(hub, "_rebuild_from_live_contributors", record_rebuild)
    first_config = config_with_window(replace(blind_config(), channels=(1,)), 10)
    second_config = config_with_window(replace(blind_config(), channels=(2,)), 10)
    hub.register_rx_listener(first_config.remote.key, frozenset({1, 2}), events.append)

    async with hub._publish_lock:
        first = asyncio.create_task(hub.async_transmit(first_config, "UP"))
        second = asyncio.create_task(hub.async_transmit(second_config, "UP"))
        await rebuilt.wait()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    await enqueued.wait()
    accept_and_start(hub, "bridge-a", published[0])
    assert isinstance(await first, CommandAck)
    older_at = _STATE_SYNC_RECV_TIME - 1.0
    seen_at = _STATE_SYNC_RECV_TIME + 1.0
    for channels in (frozenset({2}), frozenset({1})):
        hub._state_sync._dispatch_press(
            (first_config.remote.key, channels, "DOWN"),
            older_at,
            "bridge-b",
            seen_at,
        )

    assert [event.chans for event in events] == [frozenset({2})]
    hub.close()


@pytest.mark.asyncio
async def test_timed_partial_moves_never_coalesce() -> None:
    """Two simultaneous timed partial moves publish independent frames.

    Merging them would force one stop_after_ms on both and, before that,
    made the overlap-token check compare the head's per-channel sequence
    against the merged union — silently superseding the whole batch. Timed
    moves are excluded from coalescing entirely.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    one = config_with_window(replace(blind_config(), channels=(1,)), 30)
    two = config_with_window(replace(blind_config(), channels=(2,)), 30)
    results = await asyncio.gather(
        hub.async_transmit(one, "UP", stop_after_ms=500),
        hub.async_transmit(two, "UP", stop_after_ms=500),
    )

    assert all(isinstance(result, CommandAck) for result in results)
    assert [body["target"] for body in published] == ["a1b2c3:42:1", "a1b2c3:42:2"]
    assert all("stop_after_ms" in body for body in published)


@pytest.mark.asyncio
async def test_untimed_full_travels_still_coalesce() -> None:
    """The coalescing optimization still merges simultaneous open/close."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []
    hub: ZemismartHub

    async def publish(_topic: str, payload: str) -> None:
        body: dict[str, Any] = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    hub = ZemismartHub(registry, publish)
    one = config_with_window(replace(blind_config(), channels=(1,)), 30)
    two = config_with_window(replace(blind_config(), channels=(2,)), 30)
    await asyncio.gather(
        hub.async_transmit(one, "DOWN"),
        hub.async_transmit(two, "DOWN"),
    )

    assert len(published) == 1
    assert published[0]["target"] == "a1b2c3:42:1,2"


@pytest.mark.asyncio
async def test_closed_hub_rejects_new_commands() -> None:
    """A command that arrives after close() cannot resurrect the worker.

    A caller blocked on its entity command lock during teardown must resolve
    as superseded rather than publishing on the torn-down hub.
    """
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    published: list[dict[str, Any]] = []

    async def publish(_topic: str, payload: str) -> None:
        published.append(json.loads(payload))

    hub = ZemismartHub(registry, publish)
    hub.close()
    result = await hub.async_transmit(blind_config(), "UP")
    assert result == "superseded"
    assert published == []


@pytest.mark.asyncio
async def test_publish_transport_error_pops_pending() -> None:
    """An immediate publish failure never leaks its pending-status entry."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")

    async def publish(_topic: str, _payload: str) -> None:
        msg = "broker unavailable"
        raise OSError(msg)

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: "leak-check")
    with pytest.raises(OSError, match="broker unavailable"):
        await hub.async_transmit(blind_config(), "UP")
    assert hub._pending == {}


@pytest.mark.asyncio
async def test_close_cancels_background_publish_tasks() -> None:
    """close() cancels a still-pending background publish (no orphan)."""
    registry = BridgeRegistry()
    registry.update_availability("bridge-a", "online")
    release = asyncio.Event()
    enqueued = asyncio.Event()

    async def publish(_topic: str, _payload: str) -> None:
        enqueued.set()  # "enqueue" happens synchronously
        await release.wait()  # withhold the PUBACK

    hub = ZemismartHub(registry, publish, command_id_factory=lambda: "bg-1")
    task = asyncio.create_task(hub.async_transmit(blind_config(), "UP"))
    await enqueued.wait()
    background = set(hub._publish_tasks)
    assert background  # a background publish is in flight
    hub.close()
    await asyncio.sleep(0)
    assert all(publish_task.cancelled() for publish_task in background)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()


def test_remote_runtime_carries_remote_and_hub() -> None:
    from custom_components.zemismart_blinds.models import (
        BridgeRegistry,
        RemoteConfig,
        RemoteRuntime,
        ZemismartHub,
    )

    async def publisher(_topic: str, _payload: str) -> None:
        return None

    hub = ZemismartHub(BridgeRegistry(), publisher)
    remote = RemoteConfig(
        name="Kitchen remote",
        remote=_remote_identity(),
        area_id="kitchen",
        repeats=5,
    )
    runtime = RemoteRuntime(remote=remote, hub=hub)
    assert runtime.remote is remote
    assert runtime.hub is hub


def test_blindconfig_defaults_to_leaf_role() -> None:
    from custom_components.zemismart_blinds.models import BlindConfig, Role

    config = BlindConfig(
        name="Sink",
        remote=_remote_identity(),
        channels=(5,),
        travel_up=9.0,
        travel_down=9.0,
        area_id="kitchen",
        repeats=5,
    )
    assert config.role is Role.LEAF
    assert config.is_aggregate is False


def test_blindconfig_leaf_still_requires_travel() -> None:
    from custom_components.zemismart_blinds.models import BlindConfig, Role

    with pytest.raises(ValueError, match="travel"):
        BlindConfig(
            name="Sink",
            remote=_remote_identity(),
            channels=(5,),
            travel_up=None,
            travel_down=None,
            area_id="kitchen",
            repeats=5,
            role=Role.LEAF,
        )


def test_blindconfig_aggregate_allows_no_travel() -> None:
    from custom_components.zemismart_blinds.models import BlindConfig, Role

    config = BlindConfig(
        name="All",
        remote=_remote_identity(),
        channels=(1, 2, 3, 4, 5, 6),
        travel_up=None,
        travel_down=None,
        area_id="kitchen",
        repeats=5,
        role=Role.AGGREGATE,
    )
    assert config.is_aggregate is True
    assert config.travel_up is None


def test_blindconfig_derive_from_remote_and_cover() -> None:
    from custom_components.zemismart_blinds.models import (
        BlindConfig,
        CoverConfig,
        RemoteConfig,
        Role,
    )

    remote = RemoteConfig(
        name="Kitchen remote",
        remote=_remote_identity(),
        area_id="kitchen",
        repeats=7,
        coalesce_window_ms=200,
    )
    leaf = CoverConfig(name="Sink", channels=(5,), travel_up=9.0, travel_down=9.0)
    derived = BlindConfig.derive(remote, leaf, Role.LEAF)
    assert derived.name == "Sink"
    assert derived.channels == (5,)
    assert derived.area_id == "kitchen"
    assert derived.repeats == 7
    assert derived.coalesce_window_ms == 200
    assert derived.travel_up == 9.0
    assert derived.role is Role.LEAF
    assert derived.remote.key == remote.key

    aggregate_cover = CoverConfig(name="All", channels=(1, 2, 3, 4, 5, 6))
    aggregate = BlindConfig.derive(remote, aggregate_cover, Role.AGGREGATE)
    assert aggregate.is_aggregate is True
    assert aggregate.travel_up is None


def test_blindconfig_from_mapping_stays_leaf() -> None:
    from custom_components.zemismart_blinds.models import BlindConfig, Role

    config = BlindConfig(
        name="Sink",
        remote=_remote_identity(),
        channels=(5,),
        travel_up=9.0,
        travel_down=9.0,
        area_id="kitchen",
        repeats=5,
    )
    restored = BlindConfig.from_mapping(config.as_dict())
    assert restored.role is Role.LEAF
    assert restored.travel_up == 9.0


@pytest.mark.asyncio
async def test_drain_owner_supersedes_only_that_entrys_queued_commands() -> None:
    """Unloading an entry drains its queued commands; others stay queued."""
    release = asyncio.Event()
    first_published = asyncio.Event()
    published: list[str] = []

    async def publisher(topic: str, payload: str) -> None:
        del topic
        published.append(payload)
        first_published.set()
        await release.wait()

    hub = ZemismartHub(BridgeRegistry(), publisher)
    hub.registry.update_info("bridge-a", {"area": "living_room"})
    hub.registry.update_availability("bridge-a", "online")
    config = BlindConfig(
        name="Blind",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1,),
        travel_up=10.0,
        travel_down=10.0,
        area_id="living_room",
        repeats=2,
        coalesce_window_ms=0,
    )
    other_remote = RemoteIdentity(OTHER_PREFIX, OTHER_REMOTE_ID, OTHER_BASES)
    other_config = BlindConfig(
        name="Other",
        remote=other_remote,
        channels=(1,),
        travel_up=10.0,
        travel_down=10.0,
        area_id="living_room",
        repeats=2,
        coalesce_window_ms=0,
    )
    # First command occupies the worker inside the (blocked) publisher.
    first = asyncio.create_task(hub.async_transmit(config, "UP", owner="entry-a"))
    await asyncio.wait_for(first_published.wait(), timeout=1.0)
    # Two queued commands with different owners.
    drained = asyncio.create_task(hub.async_transmit(config, "DOWN", owner="entry-a"))
    kept = asyncio.create_task(hub.async_transmit(other_config, "UP", owner="entry-b"))
    await asyncio.sleep(0)

    hub.drain_owner("entry-a")
    assert await asyncio.wait_for(drained, timeout=1.0) == "superseded"
    assert not kept.done()

    release.set()
    first.cancel()
    kept.cancel()
    for task in (first, kept):
        with pytest.raises(asyncio.CancelledError):
            await task
    hub.close()


@pytest.mark.asyncio
async def test_disarm_remote_awaits_acknowledged_bridge_disarms() -> None:
    """Relearn disarms every live ledger command of the remote, bounded."""
    control: list[dict[str, Any]] = []
    disarm_seen = asyncio.Event()

    async def publisher(topic: str, payload: str) -> None:
        if topic.endswith("/cmd"):
            body: dict[str, Any] = json.loads(payload)
            control.append(body)
            disarm_seen.set()

    hub = ZemismartHub(BridgeRegistry(), publisher)
    remote = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    frame = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "UP", bases=TEST_BASES))
    signature = frame_signature(frame)
    assert signature is not None
    hub._ledger.register_pending(
        "cmd-live",
        "bridge-a",
        (1,),
        "UP",
        [LedgerFrameSpec(signature=signature, offset_ms=0, airtime_ms=100)],
    )

    # No live commands for a different remote: returns without publishing.
    await hub.async_disarm_remote("ffffff:01", deadline_seconds=0.2)
    assert control == []

    disarm_task = asyncio.create_task(hub.async_disarm_remote(remote.key, deadline_seconds=1.0))
    await asyncio.wait_for(disarm_seen.wait(), timeout=1.0)
    assert control[0] == {"action": "disarm", "command_id": "cmd-live"}
    hub.on_disarmed("bridge-a", "cmd-live")
    await asyncio.wait_for(disarm_task, timeout=1.0)
    hub.close()


@pytest.mark.asyncio
async def test_disarm_idle_callback_fires_once_when_last_request_resolves() -> None:
    """Pending disarms are reported, and draining fires the one-shot callback."""

    async def publisher(_topic: str, _payload: str) -> None:
        return

    hub = ZemismartHub(BridgeRegistry(), publisher)
    assert hub.has_pending_disarms is False

    request = hub._start_disarm_request("bridge-a", "cmd-live", hub._now() + 30.0)
    assert hub.has_pending_disarms is True

    fired: list[int] = []
    hub.set_disarm_idle_callback(lambda: fired.append(1))
    hub.on_disarmed("bridge-a", "cmd-live")
    assert request.task is not None
    await asyncio.wait_for(request.task, timeout=1.0)
    assert fired == [1]
    assert hub.has_pending_disarms is False

    # One-shot: a later drain must not re-fire the consumed callback.
    second = hub._start_disarm_request("bridge-a", "cmd-live-2", hub._now() + 30.0)
    hub.on_disarmed("bridge-a", "cmd-live-2")
    assert second.task is not None
    await asyncio.wait_for(second.task, timeout=1.0)
    assert fired == [1]
    hub.close()


@pytest.mark.asyncio
async def test_drain_owner_covers_fast_lane_stops_behind_barriers() -> None:
    """A fast-lane STOP still waiting on publish barriers drains too."""
    release = asyncio.Event()
    first_published = asyncio.Event()

    async def publisher(topic: str, payload: str) -> None:
        del topic, payload
        first_published.set()
        await release.wait()

    hub = ZemismartHub(BridgeRegistry(), publisher)
    hub.registry.update_info("bridge-a", {"area": "living_room"})
    hub.registry.update_availability("bridge-a", "online")
    config = BlindConfig(
        name="Blind",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1,),
        travel_up=10.0,
        travel_down=10.0,
        area_id="living_room",
        repeats=2,
        coalesce_window_ms=0,
    )
    # Movement occupies the worker with its publish incomplete (blocked
    # publisher), so the following STOP's fast lane waits on its barrier.
    movement = asyncio.create_task(hub.async_transmit(config, "UP", owner="entry-a"))
    await asyncio.wait_for(first_published.wait(), timeout=1.0)
    stop = asyncio.create_task(hub.async_transmit(config, "STOP", owner="entry-a"))
    await asyncio.sleep(0)
    assert not stop.done()

    hub.drain_owner("entry-a")
    assert await asyncio.wait_for(stop, timeout=1.0) == "superseded"

    release.set()
    movement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await movement
    hub.close()


def test_dispatched_press_resets_overlapping_debounce_signatures() -> None:
    """A rapid physical UP → STOP → UP jog dispatches all three presses.

    The second UP lands inside the first UP's debounce window, but the
    intervening STOP dispatch proves the first press's repeat train ended —
    the stamp must not swallow the genuine re-press. Stale late copies stay
    dropped by the heard_at ordering guard, which this reset never touches.
    """
    dispatched: list[HeardEvent] = []
    now_value = [10.0]
    clocks: dict[str, BridgeClock] = {}
    consumer = StateSyncConsumer(
        ledger=CommandLedger(),
        clock_resolver=lambda bridge_id: clocks.setdefault(bridge_id, BridgeClock()),
        dispatch=dispatched.append,
        on_emission_proof=lambda _proof: None,
        now=lambda: now_value[0],
    )
    up = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "UP", bases=TEST_BASES))
    stop = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "STOP", bases=TEST_BASES))

    consumer.handle_rx("bridge-a", 7, 1_000, up, 10.0)
    now_value[0] = 10.5
    consumer.handle_rx("bridge-a", 7, 1_500, stop, 10.5)
    now_value[0] = 11.0
    consumer.handle_rx("bridge-a", 7, 2_000, up, 11.0)

    assert [event.button for event in dispatched] == ["UP", "STOP", "UP"]


def test_ledger_disarm_deadline_extends_to_window_end() -> None:
    """A confirmed command's disarm retries until its last window closes."""
    ledger = CommandLedger()
    frame = encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "UP", bases=TEST_BASES))
    signature = frame_signature(frame)
    assert signature is not None
    ledger.register_pending(
        "cmd-timed",
        "bridge-a",
        (1,),
        "UP",
        [
            LedgerFrameSpec(signature=signature, offset_ms=0, airtime_ms=100),
            LedgerFrameSpec(signature=signature, offset_ms=45_000, airtime_ms=100),
        ],
    )
    # Pending: only the fallback applies.
    assert ledger.disarm_deadline("cmd-timed", fallback=50.0) == 50.0
    ledger.confirm("cmd-timed", 100.0)
    # Confirmed: the far STOP frame's window end wins over the fallback.
    deadline = ledger.disarm_deadline("cmd-timed", fallback=110.0)
    assert deadline > 140.0
    # Unknown commands keep the fallback.
    assert ledger.disarm_deadline("cmd-unknown", fallback=7.0) == 7.0


def _online_registry(bridge_id: str = "bridge-a") -> BridgeRegistry:
    """Return one same-area online bridge."""
    registry = BridgeRegistry()
    registry.update_info(bridge_id, {"area": "living_room"})
    registry.update_availability(bridge_id, "online")
    return registry


async def _noop_publish(_topic: str, _payload: str) -> None:
    """Accept a publish without a transport or a lifecycle response."""


@pytest.mark.parametrize(
    ("repeats", "expected_ms"),
    [
        (None, 2_000),
        ("2", 2_000),
        (True, 2_000),
        (1, 2_000),
        (2, 2_000),
        (5, 5_000),
        (20, 20_000),
        (999, 20_000),
        (-3, 2_000),
    ],
)
def test_ledger_airtime_tracks_repeats_and_clamps(repeats: object, expected_ms: int) -> None:
    """The emission envelope covers the whole repeat train, never less than 2 s."""
    assert models_module._ledger_airtime_ms(repeats) == expected_ms


def test_own_late_repeat_is_not_mistaken_for_a_physical_press() -> None:
    """A high-repeats command's tail echo stays recognized as our own emission."""
    registry = _online_registry()
    published: list[tuple[str, dict[str, Any]]] = []
    clock = {"now": 1_000.0}

    async def publish(topic: str, payload: str) -> None:
        body = json.loads(payload)
        published.append((topic, body))
        accept_and_start(hub, "bridge-a", body)

    # 20 repeats keeps the bridge on air for roughly 12 s; the ledger's old
    # fixed 2 s envelope expired mid-train and reclassified our own remaining
    # copies as a physical remote takeover.
    config = replace(blind_config(), repeats=20)
    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "command-repeats",
        now=lambda: clock["now"],
    )
    asyncio.run(hub.async_transmit(config, "DOWN", stop_after_ms=None))
    action_raw = published[0][1]["raw"]
    assert isinstance(action_raw, str)

    # Still inside the envelope 10 s after handoff.
    clock["now"] = 1_010.0
    assert hub.frame_is_own_emission(action_raw) is True
    # Well past the whole train, a genuine press is no longer masked.
    clock["now"] = 1_100.0
    assert hub.frame_is_own_emission(action_raw) is False


def test_frame_is_own_emission_ignores_undecodable_and_foreign_frames() -> None:
    """Only frames this hub actually put on air count as its own emission."""
    hub = ZemismartHub(_online_registry(), _noop_publish)

    assert hub.frame_is_own_emission("not-a-frame") is False
    assert (
        hub.frame_is_own_emission(
            encode_b0(make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1,), "UP", bases=TEST_BASES))
        )
        is False
    )


def test_timed_move_envelope_stops_at_the_preempting_stop_deadline() -> None:
    """An armed timed STOP preempts action repeats, so the window must shrink."""
    registry = _online_registry()
    published: list[dict[str, Any]] = []
    clock = {"now": 1_000.0}

    async def publish(topic: str, payload: str) -> None:
        body = json.loads(payload)
        published.append(body)
        accept_and_start(hub, "bridge-a", body)

    # 20 repeats would nominally hold the UP/DOWN signature for 20 s, but the
    # firmware promotes to STOP at 1 s and stops sending the action frame.
    config = replace(blind_config(), repeats=20)
    hub = ZemismartHub(
        registry,
        publish,
        command_id_factory=lambda: "command-timed",
        now=lambda: clock["now"],
    )
    asyncio.run(hub.async_transmit(config, "DOWN", stop_after_ms=1_000))
    action_raw = published[0]["raw"]
    assert isinstance(action_raw, str)

    # Shortly after the deadline the action frame is still plausibly ours.
    clock["now"] = 1_001.5
    assert hub.frame_is_own_emission(action_raw) is True
    # Long after the preempting STOP, a same-direction press is a REAL press
    # and must not be swallowed as our own echo.
    clock["now"] = 1_010.0
    assert hub.frame_is_own_emission(action_raw) is False


def test_pending_command_is_not_proof_of_emission() -> None:
    """An accepted-but-never-started command must not mask a real press.

    The masking window is WHILE the command is pending, so the check has to
    happen mid-flight -- after the timeout the entry is retired anyway and the
    bug is invisible.
    """
    registry = _online_registry()
    verdict: dict[str, bool] = {}
    clock = {"now": 500.0}

    async def publish(topic: str, payload: str) -> None:
        # Acknowledge admission but NEVER report `started`: published and
        # pending, with no proof it ever keyed RF. Sample the classification
        # right here, inside the pending window.
        body = json.loads(payload)
        hub.handle_status("bridge-a", bytearray(json.dumps(accepted(body)).encode()))
        raw = body["raw"]
        assert isinstance(raw, str)
        verdict["pending_masks"] = hub.frame_is_own_emission(raw)

    hub = ZemismartHub(
        registry,
        publish,
        ack_timeout=0.05,
        started_timeout=0.05,
        command_id_factory=lambda: "command-pending",
        now=lambda: clock["now"],
    )
    with suppress(CommandStartedTimeoutError, CommandAckTimeoutError):
        asyncio.run(hub.async_transmit(blind_config(), "UP", stop_after_ms=None))

    assert verdict, "publish should have run"
    assert verdict["pending_masks"] is False


def _two_area_registry() -> BridgeRegistry:
    """Return two online bridges in DIFFERENT areas so routing really differs."""
    registry = BridgeRegistry()
    for bridge_id, area in (("bridge-a", "living_room"), ("bridge-b", "office")):
        registry.update_info(bridge_id, {"area": area})
        registry.update_availability(bridge_id, "online")
    return registry


def test_shadow_arbiter_observes_cross_bridge_without_delaying() -> None:
    """Two bridges keying the air together is the case worth measuring."""
    published: list[tuple[str, float]] = []
    clock = {"now": 1_000.0}

    async def publish(topic: str, payload: str) -> None:
        published.append((topic, clock["now"]))
        accept_and_start(hub, topic.split("/")[1], json.loads(payload))

    ids = iter(["cmd-1", "cmd-2"])
    hub = ZemismartHub(
        _two_area_registry(),
        publish,
        command_id_factory=lambda: next(ids),
        now=lambda: clock["now"],
    )

    async def scenario() -> None:
        await hub.async_transmit(blind_config(area_id="living_room"), "DOWN", stop_after_ms=None)
        clock["now"] = 1_000.2
        await hub.async_transmit(
            replace(blind_config(area_id="office"), channels=(3,)), "UP", stop_after_ms=None
        )

    asyncio.run(scenario())

    # Genuinely different bridges...
    assert [topic for topic, _ in published] == [
        "rf433/bridge-a/tx",
        "rf433/bridge-b/tx",
    ]
    # ...both published immediately: shadow mode delays nothing.
    assert [when for _, when in published] == [1_000.0, 1_000.2]

    stats = hub.air_shadow_stats()
    assert stats["planned"] == 2
    assert stats["would_wait"] == 1
    assert stats["waits_by_bridge"] == {"bridge-b": 1}


def test_shadow_arbiter_ignores_same_bridge_back_to_back_commands() -> None:
    """The bridge's own scheduler already serializes these -- not contention."""
    published: list[str] = []
    clock = {"now": 1_000.0}

    async def publish(topic: str, payload: str) -> None:
        published.append(topic)
        accept_and_start(hub, topic.split("/")[1], json.loads(payload))

    ids = iter(["cmd-1", "cmd-2"])
    hub = ZemismartHub(
        _two_area_registry(),
        publish,
        command_id_factory=lambda: next(ids),
        now=lambda: clock["now"],
    )
    config = blind_config(area_id="living_room")

    async def scenario() -> None:
        await hub.async_transmit(config, "DOWN", stop_after_ms=None)
        clock["now"] = 1_000.2
        await hub.async_transmit(replace(config, channels=(3,)), "UP", stop_after_ms=None)

    asyncio.run(scenario())

    assert published == ["rf433/bridge-a/tx", "rf433/bridge-a/tx"]
    stats = hub.air_shadow_stats()
    assert stats["planned"] == 2
    # Same bridge, back to back, INSIDE the first train: still not contention.
    assert stats["would_wait"] == 0
    assert stats["waits_by_bridge"] == {}


def test_shadow_arbiter_stays_off_for_a_single_bridge_install() -> None:
    """One online bridge must leave the hub's behavior entirely unchanged."""
    published: list[str] = []
    clock = {"now": 1.0}

    async def publish(topic: str, payload: str) -> None:
        published.append(topic)
        accept_and_start(hub, topic.split("/")[1], json.loads(payload))

    hub = ZemismartHub(
        _online_registry(),
        publish,
        command_id_factory=lambda: "cmd-solo",
        now=lambda: clock["now"],
    )
    asyncio.run(hub.async_transmit(blind_config(), "DOWN", stop_after_ms=None))

    assert published == ["rf433/bridge-a/tx"]
    stats = hub.air_shadow_stats()
    assert stats["disabled_single_bridge"] == 1
    assert stats["planned"] == 0
