"""Tests for privacy-bounded config-entry diagnostics."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from custom_components.zemismart_blinds.const import DOMAIN
from custom_components.zemismart_blinds.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.zemismart_blinds.models import (
    BridgeRegistry,
    DomainRuntime,
    ZemismartHub,
)
from tests.test_init import add_to_manager, config_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def _quiet_publish(_topic: str, _payload: str) -> None:
    return


def _hub(hass: HomeAssistant) -> ZemismartHub:
    """Install a hub with one discovered bridge as the domain runtime."""
    registry = BridgeRegistry()
    registry.update_info("bridge-a", {"area": "living_room", "listen": True, "v": 2})
    registry.update_availability("bridge-a", "online")
    hub = ZemismartHub(registry, _quiet_publish)
    hass.data[DOMAIN] = DomainRuntime(hub=hub, unsubscribers=[])
    return hub


@pytest.mark.asyncio
async def test_diagnostics_reports_entry_bridges_covers_and_hub(
    hass: HomeAssistant,
) -> None:
    """The dump answers the questions a bug report about this model raises."""
    from homeassistant.helpers import entity_registry as er

    hub = _hub(hass)
    entry = config_entry("diagnostics")
    add_to_manager(hass, entry)
    registry = er.async_get(hass)
    row = registry.async_get_or_create("cover", DOMAIN, "cover-one", config_entry=entry)
    hass.states.async_set(
        row.entity_id,
        "open",
        {
            "current_position": 80,
            "remote": "a1b2c3:42",
            "channels": [1],
            "role": "leaf",
            "position_confidence": "assumed",
            "position_suspect": False,
            "last_bridge": "bridge-a",
            "motion_direction": 0,
            "unverified_anchor_bridge": None,
        },
    )

    try:
        result = await async_get_config_entry_diagnostics(hass, entry)

        assert set(result) == {"entry", "bridges", "covers", "hub", "air_arbitration"}
        assert result["bridges"] == [
            {
                "bridge_id": "bridge-a",
                "area_id": "living_room",
                "online": True,
                "availability_seen": True,
                "is_default": False,
                "boot": None,
                "listen": True,
                "contract_v": 2,
            }
        ]
        covers = result["covers"]
        assert isinstance(covers, list)
        assert len(covers) == 1
        cover = covers[0]
        assert cover["entity_id"] == row.entity_id
        assert cover["state"] == "open"
        assert cover["current_position"] == 80
        assert cover["position_confidence"] == "assumed"
        assert cover["last_bridge"] == "bridge-a"
        hub_section = result["hub"]
        assert isinstance(hub_section, dict)
        assert hub_section["has_pending_disarms"] is False
        air = result["air_arbitration"]
        assert isinstance(air, dict)
        assert air["mode"] == "enforce"
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_diagnostics_never_discloses_the_rf_identity(hass: HomeAssistant) -> None:
    """No prefix, remote id or calibration base survives into the dump.

    This repository is public and diagnostics downloads get pasted into issue
    reports; the RF identity is what lets anyone drive the motors.
    """
    from homeassistant.helpers import entity_registry as er

    hub = _hub(hass)
    entry = config_entry("diagnostics-privacy")
    add_to_manager(hass, entry)
    registry = er.async_get(hass)
    row = registry.async_get_or_create("cover", DOMAIN, "cover-two", config_entry=entry)
    hass.states.async_set(
        row.entity_id,
        "open",
        {"current_position": 10, "remote": "a1b2c3:42", "channels": [1], "role": "leaf"},
    )

    try:
        result = await async_get_config_entry_diagnostics(hass, entry)
        serialized = json.dumps(result, default=str)

        # Every secret in the entry, including via the covers' remote_key.
        for secret in ("a1b2c3", "f42a", "bcf2", "dc12", "a1b2c3:42"):
            assert secret not in serialized

        # Absence of the literal is NOT enough, and asserting only that is what
        # let a reversible encoding through review: the first version of this
        # dump labelled remotes `remote-<sha256(remote_key)[:12]>`, which passes
        # the loop above and was recovered by exhaustive search in about seven
        # seconds. The identity is 32 bits at most.
        #
        # So brute-force the label the way an attacker would, over a space that
        # contains the real identity, and require that it does NOT fall out.
        covers_section = result["covers"]
        assert isinstance(covers_section, list)
        label = str(covers_section[0]["remote"])

        def _derivations(prefix: int, remote_id: int) -> set[str]:
            """Every cheap deterministic encoding of one candidate identity."""
            key = f"{prefix:06x}:{remote_id:02x}"
            digests = {
                algorithm(candidate.encode()).hexdigest()
                for algorithm in (hashlib.md5, hashlib.sha1, hashlib.sha256)
                for candidate in (key, key.upper(), f"{prefix:06x}{remote_id:02x}")
            }
            return {digest[:length] for digest in digests for length in (8, 12, 16, 32, 64)} | {
                key,
                f"{prefix:06x}",
                f"{remote_id:02x}",
            }

        real = _derivations(0xA1B2C3, 0x42)
        assert not any(derived and derived in label for derived in real), (
            f"the label {label!r} is derivable from the identity it is meant to hide"
        )

        # The label still has to do its job: two covers of ONE remote must share
        # it, or the dump stops showing which covers move together.
        sibling = registry.async_get_or_create("cover", DOMAIN, "cover-three", config_entry=entry)
        hass.states.async_set(
            sibling.entity_id,
            "open",
            {"current_position": 30, "remote": "a1b2c3:42", "channels": [2], "role": "leaf"},
        )
        paired = await async_get_config_entry_diagnostics(hass, entry)
        paired_covers = paired["covers"]
        assert isinstance(paired_covers, list)
        labels = {str(cover["remote"]) for cover in paired_covers if cover.get("remote")}
        assert len(labels) == 1, "covers of one remote must share one dump-local label"

        entry_section = result["entry"]
        assert isinstance(entry_section, dict)
        data = entry_section["data"]
        assert isinstance(data, dict)
        assert data["prefix"] == "**REDACTED**"
        assert data["remote_id"] == "**REDACTED**"
        assert data["base_up"] == "**REDACTED**"
        # Non-identifying entry fields survive: they are half the triage value.
        assert data["repeats"] == 5

        covers = result["covers"]
        assert isinstance(covers, list)
        # Still correlatable: the pseudonym is stable across covers of a remote.
        assert str(covers[0]["remote"]).startswith("remote-")
    finally:
        hub.close()
