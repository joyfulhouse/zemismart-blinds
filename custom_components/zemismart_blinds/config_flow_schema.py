"""Schema builders and cover-row validation for Zemismart Blinds."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
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
    DEFAULT_COALESCE_WINDOW_MS,
    DEFAULT_REPEATS,
)
from .models import BridgeRegistry, CoverConfig, laminar_conflict, parse_channels

if TYPE_CHECKING:
    from homeassistant import config_entries

__all__ = [
    "MEASURE_REQUESTED",
    "_AUTOMATIC_BRIDGE",
    "_cover_display_values",
    "_cover_picker_schema",
    "_cover_removal_refusal",
    "_cover_schema",
    "_entry_cover_rows",
    "_find_cover_row",
    "_flatten_details",
    "_float_value",
    "_int_value",
    "_learn_setup_schema",
    "_manual_schema",
    "_measure_confirm_schema",
    "_measure_setup_schema",
    "_reconfigure_edit_schema",
    "_remote_settings_schema",
    "_row_cover_id",
    "_sibling_channel_sets",
    "_validate_cover_input",
]

_COERCION_ERRORS: Final = (TypeError, ValueError)
# Sentinel error key: blank travel on a measurable cover is a routing signal,
# not a validation failure. Callers translate it to travel_required when no
# calibrated physical identity exists to measure against.
MEASURE_REQUESTED: Final = "measure_requested"
_ADVANCED_SECTION = "advanced"
_AUTOMATIC_BRIDGE = "automatic"
_PENDING_COVER_ID = "pending"
# One travel-seconds field, shared by every form that offers one, so a typed
# time and a measured one are never accepted on different terms.
_TRAVEL_SELECTOR: Final = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0.1,
        max=600,
        step=0.1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="s",
    )
)


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


def _flatten_details(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the UI-only collapsed section into the persisted field shape."""
    advanced = user_input.get(_ADVANCED_SECTION)
    if not isinstance(advanced, Mapping):
        msg = "advanced settings are required"
        raise ValueError(msg)
    return {
        **{key: value for key, value in user_input.items() if key != _ADVANCED_SECTION},
        **advanced,
    }


def _manual_schema(suggested: Mapping[str, object] | None = None) -> vol.Schema:
    """Build the Advanced manual identity/calibration form."""
    values = suggested or {}
    button = str(values.get(CONF_CALIBRATION_BUTTON, "UP"))
    if button not in {"UP", "DOWN", "STOP"}:
        button = "UP"
    return vol.Schema(
        {
            vol.Required(
                CONF_PREFIX,
                default=str(values.get(CONF_PREFIX, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_REMOTE_ID,
                default=str(values.get(CONF_REMOTE_ID, "")),
            ): selector.TextSelector(),
            vol.Required(CONF_CALIBRATION_BUTTON, default=button): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["UP", "DOWN", "STOP"])
            ),
            vol.Optional(
                CONF_CALIBRATION_BASE,
                default=str(values.get(CONF_CALIBRATION_BASE, "")),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_CALIBRATION_FRAME,
                default=str(values.get(CONF_CALIBRATION_FRAME, "")),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_BASE_TRAILER,
                default=str(values.get(CONF_BASE_TRAILER, "")),
            ): selector.TextSelector(),
        }
    )


def _remote_settings_schema(suggested: Mapping[str, object] | None) -> vol.Schema:
    """Build the remote name/area/transport form."""
    values = suggested or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME,
                default=str(values.get(CONF_NAME, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_AREA_ID,
                default=str(values.get(CONF_AREA_ID, "")),
            ): selector.AreaSelector(),
            vol.Required(_ADVANCED_SECTION): section(
                vol.Schema(
                    {
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
                ),
                {"collapsed": True},
            ),
        }
    )


def _reconfigure_edit_schema(suggested: Mapping[str, object]) -> vol.Schema:
    """Extend remote settings with editable command calibration bases."""
    return _remote_settings_schema(suggested).extend(
        {
            vol.Required(
                CONF_BASE_UP,
                default=str(suggested.get(CONF_BASE_UP, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_BASE_DOWN,
                default=str(suggested.get(CONF_BASE_DOWN, "")),
            ): selector.TextSelector(),
            vol.Required(
                CONF_BASE_STOP,
                default=str(suggested.get(CONF_BASE_STOP, "")),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_BASE_TRAILER,
                default=str(suggested.get(CONF_BASE_TRAILER, "")),
            ): selector.TextSelector(),
        }
    )


def _cover_schema(suggested: Mapping[str, object] | None) -> vol.Schema:
    """Build one wizard cover form: name, channels, optional travel times."""
    values = suggested or {}
    fields: dict[vol.Marker, object] = {
        vol.Required(CONF_NAME, default=str(values.get(CONF_NAME, ""))): selector.TextSelector(),
        vol.Required(
            CONF_CHANNELS,
            default=str(values.get(CONF_CHANNELS, "")),
        ): selector.TextSelector(),
        # Travel fields carry NO defaults, ever: a default harvested from a
        # previous (failed) submission would silently backfill an omitted
        # field on the next attempt and defeat the travel_required check.
        vol.Optional(CONF_TRAVEL_UP): _TRAVEL_SELECTOR,
        vol.Optional(CONF_TRAVEL_DOWN): _TRAVEL_SELECTOR,
    }
    return vol.Schema(fields)


def _cover_display_values(
    cover_id: str,
    data: Mapping[str, object],
    title: str,
) -> dict[str, object]:
    """Convert stored cover data to values suitable for form suggestions."""
    try:
        cover = CoverConfig.from_stored(cover_id, data)
    except _COERCION_ERRORS:
        suggested: dict[str, object] = {CONF_NAME: title}
        if (raw_channels := data.get(CONF_CHANNELS)) is not None:
            if isinstance(raw_channels, str):
                channels_text = raw_channels
            elif isinstance(raw_channels, Iterable):
                channels_text = ",".join(str(channel) for channel in raw_channels)
            else:
                channels_text = str(raw_channels)
            suggested[CONF_CHANNELS] = channels_text
        return suggested

    suggested = {
        CONF_NAME: cover.name,
        CONF_CHANNELS: ",".join(str(channel) for channel in cover.channels),
    }
    for key in (CONF_TRAVEL_UP, CONF_TRAVEL_DOWN):
        if (stored := data.get(key)) not in (None, ""):
            suggested[key] = stored
    return suggested


def _validate_cover_input(
    user_input: Mapping[str, Any],
    existing: list[tuple[int, ...]],
) -> tuple[CoverConfig | None, dict[str, str]]:
    """Validate one wizard cover form against the covers collected so far."""
    try:
        channels = parse_channels(user_input.get(CONF_CHANNELS, ""))
    except ValueError:
        return None, {CONF_CHANNELS: "invalid_config"}
    conflict = laminar_conflict(channels, existing)
    if conflict is not None:
        return None, {CONF_CHANNELS: conflict}
    born_aggregate = any(
        frozenset(sibling_channels) < frozenset(channels) for sibling_channels in existing
    )
    raw_up = user_input.get(CONF_TRAVEL_UP)
    raw_down = user_input.get(CONF_TRAVEL_DOWN)
    if not born_aggregate and (raw_up is None or raw_down is None):
        # Blank travel is a request to measure, not a mistake. The caller
        # routes to the capture flow; only a caller that cannot measure (no
        # calibrated physical identity) renders this as travel_required.
        return None, {"base": MEASURE_REQUESTED}
    try:
        cover = CoverConfig(
            name=str(user_input.get(CONF_NAME, "")),
            channels=channels,
            travel_up=float(raw_up) if raw_up is not None else None,
            travel_down=float(raw_down) if raw_down is not None else None,
            cover_id=_PENDING_COVER_ID,
        )
    except _COERCION_ERRORS:
        return None, {"base": "invalid_config"}
    return cover, {}


def _bridge_selector(
    registry: BridgeRegistry,
    requested_bridge: str,
) -> tuple[str, selector.SelectSelector]:
    """Build one online-bridge selector shared by every bridge-picking form."""
    online = [bridge for bridge in registry.bridges if bridge.online]
    bridge_ids = {bridge.bridge_id for bridge in online}
    if requested_bridge != _AUTOMATIC_BRIDGE and requested_bridge not in bridge_ids:
        requested_bridge = _AUTOMATIC_BRIDGE
    options: list[selector.SelectOptionDict] = [{"value": _AUTOMATIC_BRIDGE, "label": "Automatic"}]
    for bridge in online:
        label = bridge.bridge_id
        if bridge.area_id:
            label = f"{label} — {bridge.area_id}"
        options.append({"value": bridge.bridge_id, "label": label})
    return requested_bridge, selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            translation_key="bridge",
        )
    )


def _learn_setup_schema(
    registry: BridgeRegistry,
    suggested: Mapping[str, object] | None,
) -> vol.Schema:
    """Build name/area/bridge fields from one discovery snapshot."""
    values = suggested or {}
    area_id = str(values.get(CONF_AREA_ID, ""))
    requested_bridge, bridge_field = _bridge_selector(
        registry,
        str(values.get(CONF_BRIDGE, _AUTOMATIC_BRIDGE)),
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME,
                default=str(values.get(CONF_NAME, "")),
            ): selector.TextSelector(),
            vol.Required(CONF_AREA_ID, default=area_id): selector.AreaSelector(),
            vol.Required(CONF_BRIDGE, default=requested_bridge): bridge_field,
        }
    )


def _measure_confirm_schema(measured: Mapping[str, int]) -> vol.Schema:
    """Build the confirm form, pre-filled with the rounded measurements."""
    return vol.Schema(
        {
            vol.Required(CONF_TRAVEL_UP, default=measured["UP"]): _TRAVEL_SELECTOR,
            vol.Required(CONF_TRAVEL_DOWN, default=measured["DOWN"]): _TRAVEL_SELECTOR,
        }
    )


def _measure_setup_schema(
    registry: BridgeRegistry,
    suggested: Mapping[str, object] | None,
) -> vol.Schema:
    """Build the bridge picker for one travel measurement."""
    values = suggested or {}
    requested_bridge, bridge_field = _bridge_selector(
        registry,
        str(values.get(CONF_BRIDGE, _AUTOMATIC_BRIDGE)),
    )
    return vol.Schema(
        {
            vol.Required(CONF_BRIDGE, default=requested_bridge): bridge_field,
        }
    )


def _sibling_channel_sets(
    entry: config_entries.ConfigEntry,
    *,
    exclude_cover_id: str | None = None,
) -> list[tuple[int, ...]]:
    """Load every sibling channel set, failing closed on unreadable channels."""
    channel_sets: list[tuple[int, ...]] = []
    for row in _entry_cover_rows(entry):
        cover_id = _row_cover_id(row)
        if exclude_cover_id is not None and cover_id == exclude_cover_id:
            continue
        try:
            channels = CoverConfig.from_stored(cover_id, row).channels
        except _COERCION_ERRORS:
            try:
                channels = parse_channels(row.get(CONF_CHANNELS, ""))
            except _COERCION_ERRORS as err:
                msg = f"invalid sibling cover channels: {cover_id}"
                raise ValueError(msg) from err
        channel_sets.append(channels)
    return channel_sets


def _entry_cover_rows(
    entry: config_entries.ConfigEntry,
) -> list[dict[str, object]]:
    """Copy stored cover rows without dropping unknown keys."""
    raw_rows = entry.data.get(CONF_COVERS)
    if not isinstance(raw_rows, list | tuple):
        msg = "covers must be a list"
        raise ValueError(msg)
    rows: list[dict[str, object]] = []
    for index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, Mapping) or not all(isinstance(key, str) for key in raw_row):
            msg = f"invalid cover row {index}"
            raise ValueError(msg)
        rows.append({str(key): value for key, value in raw_row.items()})
    return rows


def _row_cover_id(row: Mapping[str, object]) -> str:
    """Return one non-empty stored cover identity."""
    cover_id = row.get(CONF_COVER_ID)
    if not isinstance(cover_id, str) or not cover_id.strip():
        msg = "invalid cover_id"
        raise ValueError(msg)
    return cover_id


def _find_cover_row(
    rows: list[dict[str, object]],
    cover_id: str,
) -> tuple[int, dict[str, object]]:
    """Find one stored row by stable identity."""
    for index, row in enumerate(rows):
        if _row_cover_id(row) == cover_id:
            return index, row
    msg = f"unknown cover_id: {cover_id}"
    raise ValueError(msg)


def _cover_picker_schema(rows: list[dict[str, object]]) -> vol.Schema:
    """Build an identity-keyed picker with unambiguous duplicate names."""
    names = [str(row.get(CONF_NAME, "")).strip() for row in rows]
    duplicate_names = {name for name in names if names.count(name) > 1}
    options: list[selector.SelectOptionDict] = []
    for row, name in zip(rows, names, strict=True):
        cover_id = _row_cover_id(row)
        label = name or cover_id
        if name in duplicate_names:
            label = f"{label} — {cover_id}"
        options.append({"value": cover_id, "label": label})
    return vol.Schema(
        {
            vol.Required(CONF_COVER_ID): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            )
        }
    )


def _cover_removal_refusal(
    entry: config_entries.ConfigEntry,
    cover_id: str,
) -> str | None:
    """Return why a stored cover cannot safely be removed."""
    rows = _entry_cover_rows(entry)
    if len(rows) <= 1:
        return "last_cover"
    _index, selected_row = _find_cover_row(rows, cover_id)
    selected = CoverConfig.from_stored(cover_id, selected_row)
    siblings = _sibling_channel_sets(entry, exclude_cover_id=cover_id)
    selected_channels = frozenset(selected.channels)
    is_leaf = not any(frozenset(channels) < selected_channels for channels in siblings)
    if is_leaf and any(selected_channels < frozenset(channels) for channels in siblings):
        return "aggregate_dependency"
    return None
