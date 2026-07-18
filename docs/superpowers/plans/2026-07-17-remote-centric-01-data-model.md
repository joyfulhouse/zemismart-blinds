# Plan 01 — Data-Model Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the pure typed foundation of the remote-centric model —
`Role`, `CoverConfig`, `RemoteConfig`, laminar channel-set validation, role &
member derivation, and a derived `BlindConfig` — to `models.py` without
touching any consumer, so the integration keeps loading via the legacy path and
the whole suite stays green.

**Architecture:** New frozen dataclasses and free functions live in
`models.py` alongside the existing `RemoteIdentity`/`BlindConfig`.
`RemoteConfig` wraps a `RemoteIdentity` plus the per-remote routing/transport
fields (area, repeats, coalesce). `CoverConfig` holds one cover subentry's
name/channels/optional-travel. Roles and membership are **derived** from the
set of a remote's `CoverConfig`s, never stored. `BlindConfig` gains an explicit
`role` and optional travel times and a `derive(remote, cover, role)`
constructor; its legacy `from_mapping`/`as_dict` stay untouched so existing
consumers keep working until Plan 03 migrates them.

**Tech Stack:** Python 3.13+ (repo runs on CPython 3.14), `uv` for all Python
ops, `pytest`/`pytest-asyncio`, frozen `@dataclass(slots=True)`, `enum.StrEnum`,
`voluptuous` (not used in this phase). Home Assistant `2026.5.4` is a dependency
but this phase imports nothing from it.

## Global Constraints

- **Package manager:** `uv` only. Never `pip`. Commands: `uv run pytest ...`,
  `uv run ruff check --fix`, `uv run ruff format`, `uv run mypy --strict .`.
- **No linter suppressions:** never add `# noqa`, `# type: ignore`. Fix root
  cause.
- **State-sync guardrail:** `tests/test_state_sync.py` MUST pass unmodified at
  the end of this plan. Do not edit it.
- **No consumer edits:** this plan edits only `custom_components/zemismart_blinds/models.py`
  and `tests/test_models.py`. Do NOT edit `config_flow.py`, `cover.py`,
  `__init__.py`, `const.py`, or `strings.json` in this plan.
- **Channel rules (existing):** channels are integers `1..16`, unique, stored
  sorted; reuse `validate_channels` from `.codec` and `parse_channels` from
  `models.py` — do not reimplement channel parsing.
- **Travel bound (existing):** travel seconds are finite, `> 0`, and
  `<= MAX_TRAVEL_SECONDS` (3600), already defined in `models.py`.
- **Repeats/coalesce bounds (existing):** `MIN_REPEATS..MAX_REPEATS` (1..20)
  and `0..MAX_COALESCE_WINDOW_MS` (2000), already defined in `models.py`.
- **Channel key format:** normalized sorted channels joined by `-`, e.g.
  `1-2-3` (this is the subentry unique_id in later phases). Distinct from
  `RemoteIdentity.target_key`'s comma form — do not change `target_key`.

---

## Reference: current `models.py` shapes this plan builds on

- `RemoteIdentity(prefix: int, remote_id: int, bases: CommandBases | None = None)`
  — frozen; `.key -> "{prefix:06x}:{remote_id:02x}"`;
  `.target_key(channels) -> "{key}:{c,c,c}"`; `__post_init__` fills `bases`
  from `KNOWN_CALIBRATIONS` when `None`.
- `BlindConfig(name, remote: RemoteIdentity, channels: tuple[int,...],
  travel_up: float, travel_down: float, area_id: str, repeats: int,
  coalesce_window_ms: int = DEFAULT_COALESCE_WINDOW_MS)` — frozen;
  `.from_mapping`, `.as_dict`, `.is_group` (`len(channels) > 1`),
  `.remote_key`, `.target_key`.
- Free helpers: `parse_hex(value, field, bits)`, `parse_channels(value)`,
  `whole_number(value, field)`, `_required(mapping, key)`,
  `_as_float(value, field)`.
- Module constants already present: `MIN_REPEATS`, `MAX_REPEATS`,
  `MAX_TRAVEL_SECONDS`, `MAX_COALESCE_WINDOW_MS`, `DEFAULT_COALESCE_WINDOW_MS`.
- `CommandBases` imported from `.codec`; `validate_channels` from `.codec`.
- CONF_* constants in `const.py`: `CONF_NAME`, `CONF_PREFIX`, `CONF_REMOTE_ID`,
  `CONF_BASE_UP/DOWN/STOP/TRAILER`, `CONF_CHANNELS`, `CONF_TRAVEL_UP/DOWN`,
  `CONF_AREA_ID`, `CONF_REPEATS`, `CONF_COALESCE_WINDOW_MS`.

Run the whole suite once before starting to confirm a green baseline:

```bash
cd /Users/bryanli/Projects/joyfulhouse/homeassistant-dev/zemismart-blinds/.worktrees/remote-centric
uv run pytest -q
```

Expected: all pass.

---

## Task 1: `Role` enum and `CoverConfig`

**Files:**
- Modify: `custom_components/zemismart_blinds/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `parse_channels`, `whole_number`, `_as_float`, `_required`,
  `validate_channels`, `MAX_TRAVEL_SECONDS`, CONF_* from `const.py`.
- Produces:
  - `class Role(StrEnum)` with members `LEAF = "leaf"`, `AGGREGATE = "aggregate"`.
  - `CoverConfig(name: str, channels: tuple[int, ...], travel_up: float | None = None, travel_down: float | None = None)`
    — frozen; normalizes/validates in `__post_init__`; properties
    `.channel_key -> str`, `.has_travel -> bool`; classmethod
    `.from_subentry(data: Mapping[str, object]) -> CoverConfig`; method
    `.as_dict() -> dict[str, object]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_models.py` (import `CoverConfig`, `Role` from the models
module; `import pytest` already present):

```python
def test_role_is_str_enum() -> None:
    from custom_components.zemismart_blinds.models import Role

    assert Role.LEAF == "leaf"
    assert Role.AGGREGATE == "aggregate"


def test_cover_config_normalizes_and_exposes_channel_key() -> None:
    from custom_components.zemismart_blinds.models import CoverConfig

    cover = CoverConfig(name="  Kitchen sink  ", channels=(3, 1, 2), travel_up=12.0, travel_down=10.0)
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_models.py -k "role_is_str_enum or cover_config" -v
```

Expected: FAIL with `ImportError: cannot import name 'CoverConfig'` (or
`Role`).

- [ ] **Step 3: Implement `Role` and `CoverConfig`**

At the top of `models.py`, add `StrEnum` to the stdlib imports (there is an
existing `from enum import ...`? there is not — add a new import line near the
other stdlib imports):

```python
from enum import StrEnum
```

Add the CONF imports this task needs to the existing `from .const import (...)`
block (it already imports several; add any missing of these):
`CONF_CHANNELS`, `CONF_NAME`, `CONF_TRAVEL_DOWN`, `CONF_TRAVEL_UP`.

Add, immediately **before** the `@dataclass ... class BlindConfig` definition:

```python
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
    """One cover subentry: a named channel set with optional travel timing."""

    name: str
    channels: tuple[int, ...]
    travel_up: float | None = None
    travel_down: float | None = None

    def __post_init__(self) -> None:
        """Normalize and validate at the subentry-storage boundary."""
        name = self.name.strip()
        channels = tuple(sorted(validate_channels(self.channels)))
        if not name:
            msg = "cover name must not be empty"
            raise ValueError(msg)
        if (self.travel_up is None) != (self.travel_down is None):
            msg = "travel_up and travel_down must be set together"
            raise ValueError(msg)
        for value in (self.travel_up, self.travel_down):
            if value is not None and not (
                math.isfinite(value) and 0 < value <= MAX_TRAVEL_SECONDS
            ):
                msg = f"travel times must be finite, >0, at most {MAX_TRAVEL_SECONDS}"
                raise ValueError(msg)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "channels", channels)

    @property
    def channel_key(self) -> str:
        """Return the subentry-identity key, e.g. ``1-2-3``."""
        return "-".join(str(channel) for channel in self.channels)

    @property
    def has_travel(self) -> bool:
        """Return whether this cover carries a full position model."""
        return self.travel_up is not None

    @classmethod
    def from_subentry(cls, data: Mapping[str, object]) -> CoverConfig:
        """Build one cover from HA subentry data."""
        return cls(
            name=str(_required(data, CONF_NAME)),
            channels=parse_channels(_required(data, CONF_CHANNELS)),
            travel_up=_optional_travel(data.get(CONF_TRAVEL_UP), CONF_TRAVEL_UP),
            travel_down=_optional_travel(data.get(CONF_TRAVEL_DOWN), CONF_TRAVEL_DOWN),
        )

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe subentry storage values."""
        values: dict[str, object] = {
            CONF_NAME: self.name,
            CONF_CHANNELS: list(self.channels),
        }
        # Always emitted, empty when absent: an omitted key on reconfigure must
        # clear a previously stored travel time rather than let it persist.
        values[CONF_TRAVEL_UP] = self.travel_up if self.travel_up is not None else ""
        values[CONF_TRAVEL_DOWN] = self.travel_down if self.travel_down is not None else ""
        return values
```

`math` is already imported in `models.py` (used by `BlindConfig`). Confirm the
top imports include `from collections.abc import ... Mapping` (they do).

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_models.py -k "role_is_str_enum or cover_config" -v
```

Expected: PASS (6 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check --fix custom_components/zemismart_blinds/models.py tests/test_models.py
uv run ruff format custom_components/zemismart_blinds/models.py tests/test_models.py
uv run mypy --strict custom_components/zemismart_blinds/models.py
git add custom_components/zemismart_blinds/models.py tests/test_models.py
git commit -m "feat(models): add Role enum and CoverConfig subentry type"
```

---

## Task 2: `RemoteConfig`

**Files:**
- Modify: `custom_components/zemismart_blinds/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `RemoteIdentity`, `parse_hex`, `whole_number`, `_required`,
  `CommandBases`, CONF_* (`CONF_NAME`, `CONF_PREFIX`, `CONF_REMOTE_ID`,
  `CONF_BASE_UP`, `CONF_BASE_DOWN`, `CONF_BASE_STOP`, `CONF_BASE_TRAILER`,
  `CONF_AREA_ID`, `CONF_REPEATS`, `CONF_COALESCE_WINDOW_MS`),
  `MIN_REPEATS`, `MAX_REPEATS`, `MAX_COALESCE_WINDOW_MS`,
  `DEFAULT_COALESCE_WINDOW_MS`.
- Produces:
  - `RemoteConfig(name: str, remote: RemoteIdentity, area_id: str, repeats: int, coalesce_window_ms: int = DEFAULT_COALESCE_WINDOW_MS)`
    — frozen; property `.key -> str` (delegates to `remote.key`); classmethod
    `.from_entry(data: Mapping[str, object]) -> RemoteConfig`; method
    `.as_dict() -> dict[str, object]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_models.py` (uses `TEST_PREFIX`, `TEST_REMOTE_ID`,
`TEST_BASES` already imported; import `RemoteConfig`):

```python
def _remote_identity() -> "RemoteIdentity":
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_models.py -k "remote_config" -v
```

Expected: FAIL with `ImportError: cannot import name 'RemoteConfig'`.

- [ ] **Step 3: Implement `RemoteConfig`**

Add immediately after `CoverConfig` in `models.py`:

```python
@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """One remote config entry: identity, calibration, routing, transport."""

    name: str
    remote: RemoteIdentity
    area_id: str
    repeats: int
    coalesce_window_ms: int = DEFAULT_COALESCE_WINDOW_MS

    def __post_init__(self) -> None:
        """Normalize and validate at the entry-storage boundary."""
        name = self.name.strip()
        area_id = self.area_id.strip()
        if not name:
            msg = "remote name must not be empty"
            raise ValueError(msg)
        if not area_id:
            msg = "area_id must not be empty"
            raise ValueError(msg)
        if self.remote.bases is None:
            msg = "remote calibration is required"
            raise ValueError(msg)
        if not MIN_REPEATS <= self.repeats <= MAX_REPEATS:
            msg = f"repeats must be in the range {MIN_REPEATS}..{MAX_REPEATS}"
            raise ValueError(msg)
        if (
            isinstance(self.coalesce_window_ms, bool)
            or not isinstance(self.coalesce_window_ms, int)
            or not 0 <= self.coalesce_window_ms <= MAX_COALESCE_WINDOW_MS
        ):
            msg = f"coalesce_window_ms must be an integer in 0..{MAX_COALESCE_WINDOW_MS}"
            raise ValueError(msg)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "area_id", area_id)

    @property
    def key(self) -> str:
        """Return the remote-identity key used as the entry unique_id."""
        return self.remote.key

    @classmethod
    def from_entry(cls, data: Mapping[str, object]) -> RemoteConfig:
        """Build one remote from HA config-entry data."""
        prefix = parse_hex(_required(data, CONF_PREFIX), CONF_PREFIX, 24)
        remote_id = parse_hex(_required(data, CONF_REMOTE_ID), CONF_REMOTE_ID, 8)
        configured = [
            key in data for key in (CONF_BASE_UP, CONF_BASE_DOWN, CONF_BASE_STOP)
        ]
        if any(configured) and not all(configured):
            msg = "base_up, base_down, and base_stop must be configured together"
            raise ValueError(msg)
        bases = (
            CommandBases(
                up=parse_hex(_required(data, CONF_BASE_UP), CONF_BASE_UP, 16),
                down=parse_hex(_required(data, CONF_BASE_DOWN), CONF_BASE_DOWN, 16),
                stop=parse_hex(_required(data, CONF_BASE_STOP), CONF_BASE_STOP, 16),
                trailer=(
                    parse_hex(data[CONF_BASE_TRAILER], CONF_BASE_TRAILER, 16)
                    if data.get(CONF_BASE_TRAILER) not in (None, "")
                    else None
                ),
            )
            if all(configured)
            else None
        )
        remote = RemoteIdentity(prefix=prefix, remote_id=remote_id, bases=bases)
        return cls(
            name=str(_required(data, CONF_NAME)),
            remote=remote,
            area_id=str(_required(data, CONF_AREA_ID)),
            repeats=whole_number(_required(data, CONF_REPEATS), CONF_REPEATS),
            coalesce_window_ms=whole_number(
                data.get(CONF_COALESCE_WINDOW_MS, DEFAULT_COALESCE_WINDOW_MS),
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
            CONF_AREA_ID: self.area_id,
            CONF_REPEATS: self.repeats,
            CONF_COALESCE_WINDOW_MS: self.coalesce_window_ms,
            CONF_BASE_UP: f"{self.remote.bases.up:04x}",
            CONF_BASE_DOWN: f"{self.remote.bases.down:04x}",
            CONF_BASE_STOP: f"{self.remote.bases.stop:04x}",
        }
        values[CONF_BASE_TRAILER] = (
            f"{self.remote.bases.trailer:04x}"
            if self.remote.bases.trailer is not None
            else ""
        )
        return values
```

Add any missing CONF imports (`CONF_BASE_UP`, `CONF_BASE_DOWN`, `CONF_BASE_STOP`,
`CONF_BASE_TRAILER`, `CONF_PREFIX`, `CONF_REMOTE_ID`, `CONF_AREA_ID`,
`CONF_REPEATS`) to the existing `from .const import (...)` block — several are
already imported for `BlindConfig`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_models.py -k "remote_config" -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check --fix custom_components/zemismart_blinds/models.py tests/test_models.py
uv run ruff format custom_components/zemismart_blinds/models.py tests/test_models.py
uv run mypy --strict custom_components/zemismart_blinds/models.py
git add custom_components/zemismart_blinds/models.py tests/test_models.py
git commit -m "feat(models): add RemoteConfig entry type"
```

---

## Task 3: Laminar channel-set validation

**Files:**
- Modify: `custom_components/zemismart_blinds/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `def laminar_conflict(new_channels: Iterable[int], existing: Iterable[Iterable[int]]) -> str | None`
    — returns `"duplicate_channels"` if `new` equals any existing set,
    `"overlapping_channels"` if `new` partially overlaps any existing set
    (intersects but is neither a strict subset nor strict superset), else
    `None`. `Iterable` is already imported from `collections.abc`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_models.py` (import `laminar_conflict`):

```python
def test_laminar_conflict_accepts_disjoint_and_nested() -> None:
    from custom_components.zemismart_blinds.models import laminar_conflict

    existing = [(1, 2, 3), (4,), (5,)]
    assert laminar_conflict((6,), existing) is None          # disjoint
    assert laminar_conflict((1, 2, 3, 4, 5, 6), existing) is None  # strict superset of all
    assert laminar_conflict((1,), existing) is None          # strict subset of (1,2,3)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_models.py -k "laminar_conflict" -v
```

Expected: FAIL with `ImportError: cannot import name 'laminar_conflict'`.

- [ ] **Step 3: Implement `laminar_conflict`**

Add after `RemoteConfig` in `models.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_models.py -k "laminar_conflict" -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check --fix custom_components/zemismart_blinds/models.py tests/test_models.py
uv run ruff format custom_components/zemismart_blinds/models.py tests/test_models.py
uv run mypy --strict custom_components/zemismart_blinds/models.py
git add custom_components/zemismart_blinds/models.py tests/test_models.py
git commit -m "feat(models): add laminar channel-set validation"
```

---

## Task 4: Role and member derivation

**Files:**
- Modify: `custom_components/zemismart_blinds/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `CoverConfig`, `Role`.
- Produces:
  - `def derive_role(cover: CoverConfig, siblings: Iterable[CoverConfig]) -> Role`
    — `AGGREGATE` iff some sibling's channel set is a strict subset of
    `cover`'s; else `LEAF`. `siblings` may include `cover` itself (ignored by
    identity of channel set: a set is not a strict subset of itself).
  - `def member_covers(cover: CoverConfig, siblings: Iterable[CoverConfig]) -> tuple[CoverConfig, ...]`
    — the **leaf** covers whose channels are a strict subset of `cover`'s,
    where a sibling is a leaf iff no other sibling strictly subsets it. Returns
    them sorted by `channel_key`. Empty when `cover` is a leaf.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_models.py` (import `derive_role`, `member_covers`):

```python
def _kitchen_covers() -> "list[CoverConfig]":
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_models.py -k "derive_role or member_covers" -v
```

Expected: FAIL with `ImportError: cannot import name 'derive_role'`.

- [ ] **Step 3: Implement `derive_role` and `member_covers`**

Add after `laminar_conflict` in `models.py`:

```python
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
        if frozenset(candidate.channels) < own
        and derive_role(candidate, covers) is Role.LEAF
    ]
    return tuple(sorted(members, key=lambda candidate: candidate.channel_key))
```

Note: `member_covers` sorts by `channel_key` **string**; `"4"` sorts before
`"5"` and `"1-2-3"` before both because `-` (0x2D) precedes digits — the test
`["1-2-3", "4", "5"]` reflects this exact ordering. Keep the string sort; it is
stable and deterministic, which is all callers need.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_models.py -k "derive_role or member_covers" -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check --fix custom_components/zemismart_blinds/models.py tests/test_models.py
uv run ruff format custom_components/zemismart_blinds/models.py tests/test_models.py
uv run mypy --strict custom_components/zemismart_blinds/models.py
git add custom_components/zemismart_blinds/models.py tests/test_models.py
git commit -m "feat(models): derive cover roles and leaf membership"
```

---

## Task 5 (moved to Plan 03): Derived `BlindConfig` with explicit role

**Deferred — do NOT implement in Plan 01.** During Plan 01 execution, Codex
GPT-5.6-sol found that retyping `BlindConfig.travel_up/travel_down` to
`float | None` breaks `mypy --strict` on the **unchanged** `cover.py`, whose
leaf travel-time arithmetic requires a non-`None` `float`. Plan 01 forbids
consumer edits and requires whole-package strict mypy to stay clean, so the
`BlindConfig` change cannot land here.

Resolution: `BlindConfig.derive(remote, cover, role)`, the `role: Role` field,
optional (aggregate-only) travel, and `is_aggregate` move to **Plan 03**
(entities), where `cover.py` is migrated to the leaf/aggregate split in the
same phase — so the type change and its consumer land together and the package
stays green. The tests originally listed here move with it.

Plan 01 leaves the existing `BlindConfig` completely untouched. Its consumers
(`cover.py`, `config_flow.py`, `__init__.py`) keep loading the integration via
the legacy path.

---

## Definition of done (Plan 01)

- [ ] `uv run pytest -q` fully green.
- [ ] `uv run mypy --strict custom_components/zemismart_blinds/` clean
  (whole package — passes because `BlindConfig` is untouched).
- [ ] `uv run ruff check custom_components/zemismart_blinds/ tests/` clean.
- [ ] `tests/test_state_sync.py` unchanged (verify with `git diff --stat main -- tests/test_state_sync.py` shows no change).
- [ ] Only `models.py` and `tests/test_models.py` were modified.
- [ ] New public names exist and are importable: `Role`, `CoverConfig`,
  `RemoteConfig`, `laminar_conflict`, `derive_role`, `member_covers`.
  (`BlindConfig.derive`/`is_aggregate` arrive in Plan 03.)

Then author Plan 02 (config flow) grounded in these real types.

## Self-review notes

- **Spec coverage (this phase):** data-model section of the spec — remote
  fields, cover fields with optional travel, laminar rule, leaves-only
  membership — covered by Tasks 1–4. The derived-`BlindConfig`/role runtime
  type is deferred to Plan 03 (lands with its `cover.py` consumer to keep the
  package type-clean). Device/flow/lifecycle sections are Plans 02–04.
- **Type consistency:** `channel_key` uses `-`; `RemoteConfig.key` uses
  `RemoteIdentity.key` (`prefix:remote_id`); `laminar_conflict` returns the
  string keys `"duplicate_channels"`/`"overlapping_channels"` that Plan 02's
  flow will map to form errors. `derive_role`/`member_covers` accept
  `Iterable[CoverConfig]` and are pure.
- **No placeholders:** every code and test step in Tasks 1–4 is complete and
  runnable.
