"""Configuration models for Zemismart Blinds."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, cast

from .calibrations import KNOWN_CALIBRATIONS
from .codec import CommandBases, validate_channels
from .const import (
    CONF_AREA_ID,
    CONF_BASE_DOWN,
    CONF_BASE_STOP,
    CONF_BASE_TRAILER,
    CONF_BASE_UP,
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
)

_COERCION_ERRORS: Final = (TypeError, ValueError)

MIN_REPEATS: Final = 1
MAX_REPEATS: Final = 20
# Firmware caps stop_after_ms at 3,600,000 (one hour); a travel calibration
# above this could produce partial moves the bridge rejects.
MAX_TRAVEL_SECONDS: Final = 3600
# Matches the config-flow selector's maximum.
MAX_COALESCE_WINDOW_MS: Final = 2000


def parse_hex(value: object, field: str, bits: int) -> int:
    """Parse a fixed-width unsigned field from an integer or hex text.

    Shared by config-entry loading and the config flow so stored values and
    user input go through exactly one width-validated parser.
    """
    if isinstance(value, bool):
        msg = f"{field} must be hexadecimal"
        raise ValueError(msg)
    if isinstance(value, str):
        normalized = value.strip().lower().removeprefix("0x")
        try:
            value = int(normalized, 16)
        except ValueError as exc:
            msg = f"{field} must be hexadecimal"
            raise ValueError(msg) from exc
    if not isinstance(value, int):
        msg = f"{field} must be hexadecimal"
        raise ValueError(msg)
    if not 0 <= value < (1 << bits):
        msg = f"{field} must fit in {bits} bits"
        raise ValueError(msg)
    return value


def parse_channels(value: object) -> tuple[int, ...]:
    """Parse ``1`` or a group such as ``{1,2,3}`` from text or an iterable.

    Shared by config-entry loading and the config flow: one parser defines the
    accepted channel syntax and the 1..16 uniqueness rules everywhere.
    """
    if isinstance(value, str):
        try:
            channels: Iterable[int] = tuple(
                int(part.strip()) for part in value.strip().strip("{}").split(",") if part.strip()
            )
        except ValueError as exc:
            msg = "channels must be comma-separated integers"
            raise ValueError(msg) from exc
    elif isinstance(value, Iterable):
        channels = tuple(whole_number(channel, "channels") for channel in value)
    else:
        msg = "channels must be text or an iterable of integers"
        raise ValueError(msg)
    return tuple(sorted(validate_channels(channels)))


def whole_number(value: object, field: str) -> int:
    """Reject fractional numeric values instead of silently truncating them.

    Shared by the config flow, stored-channel parsing, and the send_raw
    service: HA selectors and service schemas do not enforce integrality, so
    a backend-valid 1.9 would otherwise be stored or transmitted as 1.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        msg = f"{field} must be a whole number"
        raise ValueError(msg)
    try:
        number = float(value)
    except ValueError as exc:
        msg = f"{field} must be a whole number"
        raise ValueError(msg) from exc
    if not number.is_integer():
        msg = f"{field} must be a whole number"
        raise ValueError(msg)
    return int(number)


def _required(mapping: Mapping[str, object], key: str) -> object:
    """Get a required stored configuration value with a useful error."""
    try:
        return mapping[key]
    except KeyError as exc:
        msg = f"missing required config value: {key}"
        raise ValueError(msg) from exc


def _number_scalar(value: object, field: str, kind: str) -> int | float | str:
    """Reject booleans/containers before numeric config coercion."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        msg = f"{field} must be {kind}"
        raise ValueError(msg)
    return value


def _as_float(value: object, field: str) -> float:
    """Coerce a stored JSON scalar to float without accepting arbitrary objects."""
    try:
        return float(_number_scalar(value, field, "numeric"))
    except ValueError as exc:
        msg = f"{field} must be numeric"
        raise ValueError(msg) from exc


def _bases_from_mapping(values: Mapping[str, object]) -> CommandBases | None:
    """Read one remote's calibration out of stored config values.

    All three bases are configured together or not at all: a partial set is
    rejected rather than half-filled, because a missing base would silently
    fall back to another remote's calibration and emit the wrong code.
    Returns None when none are stored, which leaves RemoteIdentity free to
    consult KNOWN_CALIBRATIONS.
    """
    configured = [key in values for key in (CONF_BASE_UP, CONF_BASE_DOWN, CONF_BASE_STOP)]
    if any(configured) and not all(configured):
        msg = "base_up, base_down, and base_stop must be configured together"
        raise ValueError(msg)
    if not all(configured):
        return None
    return CommandBases(
        up=parse_hex(_required(values, CONF_BASE_UP), CONF_BASE_UP, 16),
        down=parse_hex(_required(values, CONF_BASE_DOWN), CONF_BASE_DOWN, 16),
        stop=parse_hex(_required(values, CONF_BASE_STOP), CONF_BASE_STOP, 16),
        trailer=(
            parse_hex(values[CONF_BASE_TRAILER], CONF_BASE_TRAILER, 16)
            if values.get(CONF_BASE_TRAILER) not in (None, "")
            else None
        ),
    )


def _bases_as_dict(bases: CommandBases) -> dict[str, object]:
    """Return the JSON-safe storage form of one remote's calibration.

    The trailer is always emitted, empty when absent: options merge OVER entry
    data, so removing a trailer must store an explicit empty marker — an
    omitted key would let the stale data-layer trailer keep winning.
    """
    return {
        CONF_BASE_UP: f"{bases.up:04x}",
        CONF_BASE_DOWN: f"{bases.down:04x}",
        CONF_BASE_STOP: f"{bases.stop:04x}",
        CONF_BASE_TRAILER: f"{bases.trailer:04x}" if bases.trailer is not None else "",
    }


def _validate_pacing(repeats: int, coalesce_window_ms: int) -> None:
    """Validate the two transmit-pacing knobs shared by every stored config."""
    if not MIN_REPEATS <= repeats <= MAX_REPEATS:
        msg = f"repeats must be in the range {MIN_REPEATS}..{MAX_REPEATS}"
        raise ValueError(msg)
    if (
        isinstance(coalesce_window_ms, bool)
        or not isinstance(coalesce_window_ms, int)
        or not 0 <= coalesce_window_ms <= MAX_COALESCE_WINDOW_MS
    ):
        # The upper bound matches the config-flow selector: a hand-edited
        # giant window would silently delay every movement command.
        msg = f"coalesce_window_ms must be an integer in 0..{MAX_COALESCE_WINDOW_MS}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RemoteIdentity:
    """The 32-bit identity shared by every channel of one remote."""

    prefix: int
    remote_id: int
    bases: CommandBases | None = None

    def __post_init__(self) -> None:
        """Validate protocol field widths."""
        if not 0 <= self.prefix <= 0xFFFFFF:
            msg = "prefix must be an unsigned 24-bit integer"
            raise ValueError(msg)
        if not 0 <= self.remote_id <= 0xFF:
            msg = "remote_id must be an unsigned 8-bit integer"
            raise ValueError(msg)
        if self.bases is None:
            object.__setattr__(
                self,
                "bases",
                KNOWN_CALIBRATIONS.get((self.prefix, self.remote_id)),
            )

    @property
    def key(self) -> str:
        """Return a stable config-flow/dropdown identity."""
        return f"{self.prefix:06x}:{self.remote_id:02x}"

    def target_key(self, channels: Iterable[int]) -> str:
        """Return the canonical bridge-agnostic key for one channel set."""
        normalized = tuple(sorted(validate_channels(channels)))
        channel_key = ",".join(str(channel) for channel in normalized)
        return f"{self.key}:{channel_key}"


class Role(StrEnum):
    """Whether a cover addresses its channels directly or aggregates others."""

    LEAF = "leaf"
    AGGREGATE = "aggregate"


def _optional_travel(value: object, field: str) -> float | None:
    """Coerce an optional stored travel value; empty/None means unset."""
    if value is None or value == "":
        return None
    return _as_float(value, field)


@dataclass(frozen=True, slots=True)
class CoverConfig:
    """One stored cover: a stable identity and optional travel timing."""

    name: str
    channels: tuple[int, ...]
    travel_up: float | None = None
    travel_down: float | None = None
    cover_id: str = field(kw_only=True)

    def __post_init__(self) -> None:
        """Normalize and validate at the cover-storage boundary."""
        name = self.name.strip()
        cover_id = self.cover_id.strip()
        channels = tuple(sorted(validate_channels(self.channels)))
        if not name:
            msg = "cover name must not be empty"
            raise ValueError(msg)
        if not cover_id:
            msg = "cover_id must not be empty"
            raise ValueError(msg)
        if (self.travel_up is None) != (self.travel_down is None):
            msg = "travel_up and travel_down must be set together"
            raise ValueError(msg)
        for value in (self.travel_up, self.travel_down):
            if value is not None and not (math.isfinite(value) and 0 < value <= MAX_TRAVEL_SECONDS):
                msg = f"travel times must be finite, >0, at most {MAX_TRAVEL_SECONDS}"
                raise ValueError(msg)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "cover_id", cover_id)
        object.__setattr__(self, "channels", channels)

    @property
    def channel_key(self) -> str:
        """Return the canonical channel-set validation key, e.g. ``1-2-3``."""
        return "-".join(str(channel) for channel in self.channels)

    @property
    def has_travel(self) -> bool:
        """Return whether this cover carries a full position model."""
        return self.travel_up is not None

    @classmethod
    def from_stored(cls, cover_id: str, data: Mapping[str, object]) -> CoverConfig:
        """Build one cover from a stored entry-data row."""
        return cls(
            name=str(_required(data, CONF_NAME)),
            channels=parse_channels(_required(data, CONF_CHANNELS)),
            travel_up=_optional_travel(data.get(CONF_TRAVEL_UP), CONF_TRAVEL_UP),
            travel_down=_optional_travel(data.get(CONF_TRAVEL_DOWN), CONF_TRAVEL_DOWN),
            cover_id=cover_id,
        )

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe cover fields, excluding stable row identity."""
        values: dict[str, object] = {
            CONF_NAME: self.name,
            CONF_CHANNELS: list(self.channels),
        }
        # Always emitted, empty when absent: an omitted key on reconfigure must
        # clear a previously stored travel time rather than let it persist.
        values[CONF_TRAVEL_UP] = self.travel_up if self.travel_up is not None else ""
        values[CONF_TRAVEL_DOWN] = self.travel_down if self.travel_down is not None else ""
        return values


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """One remote config entry: identity, calibration, routing, transport."""

    name: str
    remote: RemoteIdentity
    area_id: str
    repeats: int
    coalesce_window_ms: int = DEFAULT_COALESCE_WINDOW_MS
    cover_rows: tuple[dict[str, object], ...] = ()
    covers: tuple[CoverConfig, ...] = field(init=False)

    def __post_init__(self) -> None:
        """Normalize and validate at the entry-storage boundary."""
        name = self.name.strip()
        area_id = self.area_id.strip()
        cover_rows = tuple(dict(row) for row in self.cover_rows)
        covers: list[CoverConfig] = []
        seen_cover_ids: set[str] = set()
        for row in cover_rows:
            raw_cover_id = row.get(CONF_COVER_ID)
            cover_id = raw_cover_id if isinstance(raw_cover_id, str) else ""
            normalized_cover_id = cover_id.strip()
            if normalized_cover_id in seen_cover_ids:
                msg = f"duplicate cover_id: {normalized_cover_id}"
                raise ValueError(msg)
            try:
                cover = CoverConfig.from_stored(cover_id, row)
            except _COERCION_ERRORS as err:
                row_id = normalized_cover_id or repr(raw_cover_id)
                msg = f"invalid cover row {row_id}: {err}"
                raise ValueError(msg) from err
            seen_cover_ids.add(cover.cover_id)
            covers.append(cover)
        if not name:
            msg = "remote name must not be empty"
            raise ValueError(msg)
        if not area_id:
            msg = "area_id must not be empty"
            raise ValueError(msg)
        if self.remote.bases is None:
            msg = "remote calibration is required"
            raise ValueError(msg)
        _validate_pacing(self.repeats, self.coalesce_window_ms)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "area_id", area_id)
        object.__setattr__(self, "cover_rows", cover_rows)
        object.__setattr__(self, "covers", tuple(covers))

    @property
    def key(self) -> str:
        """Return the remote-identity key used as the entry unique_id."""
        return self.remote.key

    @classmethod
    def from_entry(cls, data: Mapping[str, object]) -> RemoteConfig:
        """Build one remote from HA config-entry data."""
        prefix = parse_hex(_required(data, CONF_PREFIX), CONF_PREFIX, 24)
        remote_id = parse_hex(_required(data, CONF_REMOTE_ID), CONF_REMOTE_ID, 8)
        remote = RemoteIdentity(
            prefix=prefix,
            remote_id=remote_id,
            bases=_bases_from_mapping(data),
        )
        raw_cover_rows = data.get(CONF_COVERS, ())
        if not isinstance(raw_cover_rows, list | tuple):
            msg = "covers must be a list"
            raise ValueError(msg)
        cover_rows: list[dict[str, object]] = []
        for index, raw_row in enumerate(raw_cover_rows):
            if not isinstance(raw_row, Mapping):
                msg = f"cover row {index} must be a mapping"
                raise ValueError(msg)
            if not all(isinstance(key, str) for key in raw_row):
                msg = f"cover row {index} keys must be strings"
                raise ValueError(msg)
            cover_rows.append(dict(cast("Mapping[str, object]", raw_row)))
        return cls(
            name=str(_required(data, CONF_NAME)),
            remote=remote,
            area_id=str(_required(data, CONF_AREA_ID)),
            repeats=whole_number(_required(data, CONF_REPEATS), CONF_REPEATS),
            coalesce_window_ms=whole_number(
                data.get(CONF_COALESCE_WINDOW_MS, DEFAULT_COALESCE_WINDOW_MS),
                CONF_COALESCE_WINDOW_MS,
            ),
            cover_rows=tuple(cover_rows),
        )

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe config-entry storage values."""
        assert self.remote.bases is not None
        values: dict[str, object] = {
            CONF_NAME: self.name,
            CONF_PREFIX: f"{self.remote.prefix:06x}",
            CONF_REMOTE_ID: f"{self.remote.remote_id:02x}",
            CONF_AREA_ID: self.area_id,
            CONF_REPEATS: self.repeats,
            CONF_COALESCE_WINDOW_MS: self.coalesce_window_ms,
            **_bases_as_dict(self.remote.bases),
        }
        values[CONF_COVERS] = [dict(row) for row in self.cover_rows]
        return values


def laminar_conflict(
    new_channels: Iterable[int],
    existing: Iterable[Iterable[int]],
) -> str | None:
    """Return a conflict key if ``new_channels`` is not laminar with ``existing``.

    A laminar family admits only disjoint or strictly nested sets. Returns
    ``"duplicate_channels"`` on an equal set, ``"overlapping_channels"`` on a
    partial overlap (intersecting but neither strict subset nor superset), or
    ``None`` when the addition keeps the family laminar.
    """
    new_set = frozenset(new_channels)
    for other in existing:
        other_set = frozenset(other)
        if new_set == other_set:
            return "duplicate_channels"
        if new_set & other_set and not (new_set < other_set or other_set < new_set):
            return "overlapping_channels"
    return None


def derive_role(cover: CoverConfig, siblings: Iterable[CoverConfig]) -> Role:
    """Return AGGREGATE iff a sibling's channels strictly subset ``cover``'s."""
    own = frozenset(cover.channels)
    for sibling in siblings:
        if frozenset(sibling.channels) < own:
            return Role.AGGREGATE
    return Role.LEAF


def member_covers(
    cover: CoverConfig,
    siblings: Iterable[CoverConfig],
) -> tuple[CoverConfig, ...]:
    """Return the leaf covers strictly inside ``cover``, sorted by channel key.

    A sibling is a leaf when no other sibling strictly subsets it; only leaves
    are members, so each physical channel is represented at most once and
    nested aggregates are never traversed.
    """
    covers = list(siblings)
    own = frozenset(cover.channels)
    members = [
        candidate
        for candidate in covers
        if frozenset(candidate.channels) < own and derive_role(candidate, covers) is Role.LEAF
    ]
    return tuple(sorted(members, key=lambda candidate: candidate.channel_key))


@dataclass(frozen=True, slots=True)
class BlindConfig:
    """Persisted configuration for exactly one blind or group device."""

    name: str
    remote: RemoteIdentity
    channels: tuple[int, ...]
    travel_up: float | None
    travel_down: float | None
    area_id: str
    repeats: int
    coalesce_window_ms: int = DEFAULT_COALESCE_WINDOW_MS
    role: Role = Role.LEAF

    def __post_init__(self) -> None:
        """Normalize and validate values at the config-entry boundary."""
        name = self.name.strip()
        area_id = self.area_id.strip()
        channels = tuple(sorted(validate_channels(self.channels)))
        if not name:
            msg = "name must not be empty"
            raise ValueError(msg)
        if not area_id:
            msg = "area_id must not be empty"
            raise ValueError(msg)
        if self.remote.bases is None:
            msg = "remote calibration is required"
            raise ValueError(msg)
        if self.role is Role.LEAF and (self.travel_up is None or self.travel_down is None):
            msg = "leaf covers require travel_up and travel_down"
            raise ValueError(msg)
        if not all(
            math.isfinite(value) and 0 < value <= MAX_TRAVEL_SECONDS
            for value in (self.travel_up, self.travel_down)
            if value is not None
        ):
            # NaN slips through plain comparisons (nan <= 0 is False) and
            # would leave the position model "moving" forever. The upper
            # bound keeps every derivable partial-move stop_after_ms inside
            # the firmware's accepted 1-hour range.
            msg = f"travel times must be finite, greater than zero, at most {MAX_TRAVEL_SECONDS}"
            raise ValueError(msg)
        _validate_pacing(self.repeats, self.coalesce_window_ms)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "area_id", area_id)
        object.__setattr__(self, "channels", channels)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> BlindConfig:
        """Build a typed config from Home Assistant entry data/options."""
        channels = parse_channels(_required(values, CONF_CHANNELS))
        prefix = parse_hex(_required(values, CONF_PREFIX), CONF_PREFIX, 24)
        remote_id = parse_hex(_required(values, CONF_REMOTE_ID), CONF_REMOTE_ID, 8)

        # RemoteIdentity.__post_init__ fills bases from KNOWN_CALIBRATIONS when
        # none are stored (the table ships empty; deployments may pre-seed it).
        # A remote with no stored bases and no pre-seeded calibration stays
        # None and is rejected by BlindConfig.__post_init__ ("remote
        # calibration is required") — never silently defaulted, which would
        # emit wrong codes for a different remote.
        remote = RemoteIdentity(
            prefix=prefix,
            remote_id=remote_id,
            bases=_bases_from_mapping(values),
        )

        return cls(
            name=str(_required(values, CONF_NAME)),
            remote=remote,
            channels=channels,
            travel_up=_as_float(_required(values, CONF_TRAVEL_UP), CONF_TRAVEL_UP),
            travel_down=_as_float(_required(values, CONF_TRAVEL_DOWN), CONF_TRAVEL_DOWN),
            area_id=str(_required(values, CONF_AREA_ID)),
            repeats=whole_number(_required(values, CONF_REPEATS), CONF_REPEATS),
            coalesce_window_ms=whole_number(
                values.get(CONF_COALESCE_WINDOW_MS, DEFAULT_COALESCE_WINDOW_MS),
                CONF_COALESCE_WINDOW_MS,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe config-entry storage values."""
        assert self.remote.bases is not None
        values: dict[str, object] = {
            CONF_NAME: self.name,
            CONF_PREFIX: f"{self.remote.prefix:06x}",
            CONF_REMOTE_ID: f"{self.remote.remote_id:02x}",
            CONF_CHANNELS: list(self.channels),
            CONF_TRAVEL_UP: self.travel_up,
            CONF_TRAVEL_DOWN: self.travel_down,
            CONF_AREA_ID: self.area_id,
            CONF_REPEATS: self.repeats,
            CONF_COALESCE_WINDOW_MS: self.coalesce_window_ms,
            **_bases_as_dict(self.remote.bases),
        }
        return values

    @property
    def is_group(self) -> bool:
        """Return whether this device addresses more than one motor channel."""
        return len(self.channels) > 1

    @property
    def is_aggregate(self) -> bool:
        """Return whether this cover aggregates member covers' state."""
        return self.role is Role.AGGREGATE

    @classmethod
    def derive(
        cls,
        remote: RemoteConfig,
        cover: CoverConfig,
        role: Role,
    ) -> BlindConfig:
        """Build the runtime config for one cover from its remote and data row."""
        return cls(
            name=cover.name,
            remote=remote.remote,
            channels=cover.channels,
            travel_up=cover.travel_up,
            travel_down=cover.travel_down,
            area_id=remote.area_id,
            repeats=remote.repeats,
            coalesce_window_ms=remote.coalesce_window_ms,
            role=role,
        )

    @property
    def remote_key(self) -> str:
        """Return the shared remote identity key."""
        return self.remote.key

    @property
    def target_key(self) -> str:
        """Return the bridge scheduler's canonical RF target key."""
        return self.remote.target_key(self.channels)
