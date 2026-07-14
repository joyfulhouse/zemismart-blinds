"""Config and options flows for adding one Zemismart blind/group at a time."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .codec import (
    CommandBases,
    decode_reference_b0,
    derive_bases,
    derive_bases_from_base,
)
from .const import (
    CONF_AREA_ID,
    CONF_BASE_TRAILER,
    CONF_BASE_UP,
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
    DEFAULT_REPEATS,
    DEFAULT_TRAVEL_DOWN,
    DEFAULT_TRAVEL_UP,
    DOMAIN,
    MANUAL_REMOTE,
)
from .models import BlindConfig, RemoteIdentity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigFlowResult


def _parse_channels(value: object) -> tuple[int, ...]:
    """Parse ``1`` or a group such as ``{1,2,3}`` from the flow."""
    if not isinstance(value, str):
        msg = "channels must be text"
        raise ValueError(msg)
    try:
        channels = tuple(
            sorted(
                int(part.strip()) for part in value.strip().strip("{}").split(",") if part.strip()
            )
        )
    except ValueError as exc:
        msg = "channels must be comma-separated integers"
        raise ValueError(msg) from exc
    if not channels or any(channel < 1 or channel > 16 for channel in channels):
        msg = "channels must be in the range 1..16"
        raise ValueError(msg)
    if len(channels) != len(set(channels)):
        msg = "channels must be unique"
        raise ValueError(msg)
    return channels


def _parse_hex(value: object, bits: int) -> int:
    """Parse a fixed-width hexadecimal flow field."""
    if not isinstance(value, str):
        msg = "hex value must be text"
        raise ValueError(msg)
    normalized = value.strip().lower().removeprefix("0x")
    try:
        parsed = int(normalized, 16)
    except ValueError as exc:
        msg = "invalid hexadecimal value"
        raise ValueError(msg) from exc
    if not 0 <= parsed < (1 << bits):
        msg = f"hex value must fit in {bits} bits"
        raise ValueError(msg)
    return parsed


def _float_value(value: object, fallback: float) -> float:
    """Convert a persisted selector value to a display float."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def _int_value(value: object, fallback: int) -> int:
    """Convert a persisted selector value to a display integer."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def _effective_values(entry: config_entries.ConfigEntry[Any]) -> dict[str, object]:
    """Merge an existing entry's mutable options over initial data."""
    values: dict[str, object] = dict(entry.data)
    values.update(entry.options)
    return values


def _known_remotes(
    entries: list[config_entries.ConfigEntry[Any]],
) -> dict[str, tuple[RemoteIdentity, str]]:
    """Collect unique remote identities for the reuse dropdown."""
    remotes: dict[str, tuple[RemoteIdentity, str]] = {}
    for entry in entries:
        try:
            config = BlindConfig.from_mapping(_effective_values(entry))
        except TypeError, ValueError:
            continue
        remotes.setdefault(config.remote_key, (config.remote, entry.title))
    return remotes


def _remote_options(
    remotes: Mapping[str, tuple[RemoteIdentity, str]],
) -> list[selector.SelectOptionDict]:
    """Build select options with a manual-entry path first."""
    options: list[selector.SelectOptionDict] = [
        {"value": MANUAL_REMOTE, "label": "Enter a remote manually"}
    ]
    options.extend(
        {
            "value": key,
            "label": (f"{label} — prefix 0x{remote.prefix:06x}, id 0x{remote.remote_id:02x}"),
        }
        for key, (remote, label) in sorted(remotes.items())
    )
    return options


def _schema(
    remotes: Mapping[str, tuple[RemoteIdentity, str]],
    suggested: Mapping[str, object] | None = None,
) -> vol.Schema:
    """Build the dynamic add/edit schema."""
    values = suggested or {}
    prefix = str(values.get(CONF_PREFIX, ""))
    remote_id = str(values.get(CONF_REMOTE_ID, ""))
    calibration_base = str(values.get(CONF_CALIBRATION_BASE, values.get(CONF_BASE_UP, "")))
    calibration_frame = str(values.get(CONF_CALIBRATION_FRAME, ""))
    calibration_button = str(values.get(CONF_CALIBRATION_BUTTON, "UP"))
    if calibration_button not in {"UP", "DOWN", "STOP"}:
        calibration_button = "UP"
    trailer_base = str(values.get(CONF_BASE_TRAILER, ""))
    known_default = str(values.get(CONF_KNOWN_REMOTE, MANUAL_REMOTE))
    if known_default != MANUAL_REMOTE and known_default not in remotes:
        known_default = MANUAL_REMOTE
    raw_channels = values.get(CONF_CHANNELS, "1")
    if isinstance(raw_channels, list | tuple | set):
        raw_channels = ",".join(str(channel) for channel in raw_channels)
    elif not isinstance(raw_channels, str):
        raw_channels = "1"

    return vol.Schema(
        {
            vol.Required(
                CONF_NAME,
                default=str(values.get(CONF_NAME, "")),
            ): selector.TextSelector(),
            vol.Required(CONF_KNOWN_REMOTE, default=known_default): selector.SelectSelector(
                selector.SelectSelectorConfig(options=_remote_options(remotes))
            ),
            vol.Optional(CONF_PREFIX, default=prefix): selector.TextSelector(),
            vol.Optional(CONF_REMOTE_ID, default=remote_id): selector.TextSelector(),
            vol.Required(
                CONF_CALIBRATION_BUTTON,
                default=calibration_button,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["UP", "DOWN", "STOP"])
            ),
            vol.Optional(
                CONF_CALIBRATION_BASE,
                default=calibration_base,
            ): selector.TextSelector(),
            vol.Optional(
                CONF_CALIBRATION_FRAME,
                default=calibration_frame,
            ): selector.TextSelector(),
            vol.Optional(CONF_BASE_TRAILER, default=trailer_base): selector.TextSelector(),
            vol.Required(CONF_CHANNELS, default=raw_channels): selector.TextSelector(),
            vol.Required(
                CONF_TRAVEL_UP,
                default=_float_value(
                    values.get(CONF_TRAVEL_UP),
                    DEFAULT_TRAVEL_UP,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=600,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_TRAVEL_DOWN,
                default=_float_value(
                    values.get(CONF_TRAVEL_DOWN),
                    DEFAULT_TRAVEL_DOWN,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.1,
                    max=600,
                    step=0.1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(CONF_AREA_ID, default=str(values.get(CONF_AREA_ID, ""))): (
                selector.AreaSelector()
            ),
            vol.Required(
                CONF_REPEATS,
                default=_int_value(values.get(CONF_REPEATS), DEFAULT_REPEATS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=20,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _config_from_input(
    user_input: Mapping[str, Any],
    remotes: Mapping[str, tuple[RemoteIdentity, str]],
) -> BlindConfig:
    """Validate flow input and resolve either manual or reused remote identity."""
    selection = str(user_input[CONF_KNOWN_REMOTE])
    if selection == MANUAL_REMOTE:
        prefix = _parse_hex(user_input.get(CONF_PREFIX), 24)
        remote_id = _parse_hex(user_input.get(CONF_REMOTE_ID), 8)
        calibration_button = str(user_input.get(CONF_CALIBRATION_BUTTON, "UP"))
        raw_base = str(user_input.get(CONF_CALIBRATION_BASE, "")).strip()
        raw_frame = str(user_input.get(CONF_CALIBRATION_FRAME, "")).strip()
        if raw_base and raw_frame:
            msg = "provide either a command base or a captured reference, not both"
            raise ValueError(msg)
        bases: CommandBases | None = None
        if raw_base:
            bases = derive_bases_from_base(
                calibration_button,
                _parse_hex(raw_base, 16),
                remote_id,
            )
        elif raw_frame:
            decoded = decode_reference_b0(raw_frame)
            if decoded["prefix"] != prefix or decoded["remote_id"] != remote_id:
                msg = "captured reference identity does not match the entered remote"
                raise ValueError(msg)
            bases = derive_bases(
                decoded["chans"],
                calibration_button,
                decoded["cmd"],
                remote_id,
            )
        raw_trailer = str(user_input.get(CONF_BASE_TRAILER, "")).strip()
        if raw_trailer:
            if bases is None:
                bases = RemoteIdentity(prefix, remote_id).bases
            if bases is None:
                msg = "action calibration is required before a trailer base"
                raise ValueError(msg)
            bases = CommandBases(
                up=bases.up,
                down=bases.down,
                stop=bases.stop,
                trailer=_parse_hex(raw_trailer, 16),
            )
        remote = RemoteIdentity(prefix=prefix, remote_id=remote_id, bases=bases)
    else:
        try:
            remote = remotes[selection][0]
        except KeyError as exc:
            msg = "selected known remote no longer exists"
            raise ValueError(msg) from exc
    if remote.bases is None:
        msg = "remote calibration is required"
        raise ValueError(msg)
    return BlindConfig(
        name=str(user_input[CONF_NAME]),
        remote=remote,
        channels=_parse_channels(user_input[CONF_CHANNELS]),
        travel_up=float(user_input[CONF_TRAVEL_UP]),
        travel_down=float(user_input[CONF_TRAVEL_DOWN]),
        area_id=str(user_input[CONF_AREA_ID]),
        repeats=int(user_input[CONF_REPEATS]),
    )


def _suggested_for(config: BlindConfig) -> dict[str, object]:
    """Convert typed storage into edit-flow display values."""
    values = config.as_dict()
    values[CONF_KNOWN_REMOTE] = config.remote_key
    values[CONF_CHANNELS] = ",".join(str(channel) for channel in config.channels)
    values[CONF_CALIBRATION_BUTTON] = "UP"
    values[CONF_CALIBRATION_BASE] = values[CONF_BASE_UP]
    values[CONF_CALIBRATION_FRAME] = ""
    return values


def _unique_id(config: BlindConfig) -> str:
    """Return the identity of one remote channel set."""
    return f"{config.remote_key}:{'-'.join(map(str, config.channels))}"


class ZemismartBlindsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add exactly one blind or group device per config entry."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual/reused-remote device setup."""
        remotes = _known_remotes(self._async_current_entries())
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = _config_from_input(user_input, remotes)
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(_unique_id(config))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=config.name, data=config.as_dict())

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(remotes, user_input),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry[Any],
    ) -> ZemismartBlindsOptionsFlow:
        """Return the edit flow for one existing device."""
        return ZemismartBlindsOptionsFlow()


class ZemismartBlindsOptionsFlow(config_entries.OptionsFlowWithReload):
    """Edit one blind/group and reload its entity."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit timing, area, channels, or remote identity."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        remotes = _known_remotes(entries)
        current = BlindConfig.from_mapping(_effective_values(self.config_entry))
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = _config_from_input(user_input, remotes)
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                new_unique_id = _unique_id(config)
                if any(
                    entry.entry_id != self.config_entry.entry_id
                    and entry.unique_id == new_unique_id
                    for entry in entries
                ):
                    errors["base"] = "already_configured"
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        title=config.name,
                        unique_id=new_unique_id,
                    )
                    return self.async_create_entry(title=config.name, data=config.as_dict())

        suggested = user_input if user_input is not None else _suggested_for(current)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(remotes, suggested),
            errors=errors,
        )
