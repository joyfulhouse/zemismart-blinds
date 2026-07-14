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
    CONF_BASE_DOWN,
    CONF_BASE_STOP,
    CONF_BASE_TRAILER,
    CONF_BASE_UP,
    CONF_CALIBRATION_BASE,
    CONF_CALIBRATION_BUTTON,
    CONF_CALIBRATION_FRAME,
    CONF_CHANNELS,
    CONF_COALESCE_WINDOW_MS,
    CONF_KNOWN_REMOTE,
    CONF_NAME,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    CONF_TRAVEL_DOWN,
    CONF_TRAVEL_UP,
    DEFAULT_COALESCE_WINDOW_MS,
    DEFAULT_REPEATS,
    DEFAULT_TRAVEL_DOWN,
    DEFAULT_TRAVEL_UP,
    DOMAIN,
    MANUAL_REMOTE,
    VIRTUAL_REMOTE,
)
from .models import BlindConfig, RemoteIdentity, parse_channels, parse_hex

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant


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


def known_remotes(
    entries: list[config_entries.ConfigEntry[Any]],
) -> dict[str, tuple[RemoteIdentity, str]]:
    """Collect unique calibrated remote identities from existing entries."""
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
    *,
    include_virtual: bool,
) -> list[selector.SelectOptionDict]:
    """Build select options with the manual and virtual paths first."""
    options: list[selector.SelectOptionDict] = [
        {"value": MANUAL_REMOTE, "label": "Enter a remote manually"}
    ]
    if include_virtual:
        options.append(
            {
                "value": VIRTUAL_REMOTE,
                "label": "Allocate a new virtual remote (pair the motor to it afterwards)",
            }
        )
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
    *,
    include_virtual: bool = True,
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
                selector.SelectSelectorConfig(
                    options=_remote_options(remotes, include_virtual=include_virtual)
                )
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
            vol.Required(
                CONF_COALESCE_WINDOW_MS,
                default=_int_value(
                    values.get(CONF_COALESCE_WINDOW_MS),
                    DEFAULT_COALESCE_WINDOW_MS,
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=2000,
                    step=10,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="ms",
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
        prefix = parse_hex(user_input.get(CONF_PREFIX), CONF_PREFIX, 24)
        remote_id = parse_hex(user_input.get(CONF_REMOTE_ID), CONF_REMOTE_ID, 8)
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
                parse_hex(raw_base, CONF_CALIBRATION_BASE, 16),
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
                trailer=parse_hex(raw_trailer, CONF_BASE_TRAILER, 16),
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
        channels=parse_channels(user_input[CONF_CHANNELS]),
        travel_up=float(user_input[CONF_TRAVEL_UP]),
        travel_down=float(user_input[CONF_TRAVEL_DOWN]),
        area_id=str(user_input[CONF_AREA_ID]),
        repeats=int(user_input[CONF_REPEATS]),
        coalesce_window_ms=int(user_input.get(CONF_COALESCE_WINDOW_MS, DEFAULT_COALESCE_WINDOW_MS)),
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


def _materialize_virtual_remote(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Rewrite a virtual-remote selection as a fully calibrated manual entry."""
    from . import new_virtual_remote_identity

    prefix, remote_id, bases = new_virtual_remote_identity(hass)
    return {
        **user_input,
        CONF_KNOWN_REMOTE: MANUAL_REMOTE,
        CONF_PREFIX: f"{prefix:06x}",
        CONF_REMOTE_ID: f"{remote_id:02x}",
        CONF_CALIBRATION_BUTTON: "UP",
        CONF_CALIBRATION_BASE: f"{bases.up:04x}",
        CONF_CALIBRATION_FRAME: "",
        CONF_BASE_TRAILER: "",
    }


def _propagate_calibration(
    hass: HomeAssistant,
    config: BlindConfig,
    exclude_entry_id: str | None,
) -> None:
    """Copy a manually recalibrated identity to every sibling entry.

    One physical remote has exactly one calibration; entries sharing its
    identity must never transmit conflicting command bases.
    """
    assert config.remote.bases is not None
    base_keys = {CONF_BASE_UP, CONF_BASE_DOWN, CONF_BASE_STOP, CONF_BASE_TRAILER}
    base_values = {key: value for key, value in config.as_dict().items() if key in base_keys}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id:
            continue
        try:
            sibling = BlindConfig.from_mapping(_effective_values(entry))
        except TypeError, ValueError:
            continue
        if sibling.remote_key != config.remote_key or sibling.remote.bases == config.remote.bases:
            continue

        def rewritten(values: Mapping[str, object]) -> dict[str, object]:
            # Drop stale base keys first: a recalibration without a trailer
            # must also remove a sibling's previous trailer base.
            return {
                **{key: value for key, value in values.items() if key not in base_keys},
                **base_values,
            }

        options = rewritten(entry.options) if entry.options else entry.options
        hass.config_entries.async_update_entry(entry, data=rewritten(entry.data), options=options)
        hass.config_entries.async_schedule_reload(entry.entry_id)


class ZemismartBlindsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add exactly one blind or group device per config entry."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual, virtual, or reused-remote device setup."""
        remotes = known_remotes(self._async_current_entries())
        errors: dict[str, str] = {}
        if user_input is not None:
            if str(user_input.get(CONF_KNOWN_REMOTE)) == VIRTUAL_REMOTE:
                user_input = _materialize_virtual_remote(self.hass, user_input)
            try:
                config = _config_from_input(user_input, remotes)
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(_unique_id(config))
                self._abort_if_unique_id_configured()
                _propagate_calibration(self.hass, config, None)
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
        remotes = known_remotes(entries)
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
                    _propagate_calibration(self.hass, config, self.config_entry.entry_id)
                    return self.async_create_entry(title=config.name, data=config.as_dict())

        suggested = user_input if user_input is not None else _suggested_for(current)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(remotes, suggested, include_virtual=False),
            errors=errors,
        )
