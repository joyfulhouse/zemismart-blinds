"""Tests for per-remote calibration in the add/edit flow."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.zemismart_blinds.codec import (
    CommandBases,
    encode_b0,
    make_payload,
)
from custom_components.zemismart_blinds.config_flow import _config_from_input
from custom_components.zemismart_blinds.const import (
    CONF_AREA_ID,
    CONF_CALIBRATION_BASE,
    CONF_CALIBRATION_BUTTON,
    CONF_CALIBRATION_FRAME,
    CONF_CHANNELS,
    CONF_KNOWN_REMOTE,
    CONF_NAME,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    CONF_TRAVEL_DOWN,
    CONF_TRAVEL_UP,
    MANUAL_REMOTE,
)
from custom_components.zemismart_blinds.models import RemoteIdentity
from tests.synthetic import SYNTHETIC_REMOTES

# A synthetic remote used as the "captured reference" calibration source. Its
# channel-1 UP frame is generated with the hardware-validated codec, so the
# flow's decode/derive path is exercised without any real capture material.
_name, REF_PREFIX, REF_REMOTE_ID, REF_BASES, _payload = SYNTHETIC_REMOTES[1]
REFERENCE_FRAME = encode_b0(make_payload(REF_PREFIX, REF_REMOTE_ID, (1,), "UP", bases=REF_BASES))


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
