# Plan 02a — Remote Wizard & Runtime Shim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Git deviation:** do NOT run `git add`/`git commit` (linked-worktree git metadata is outside the sandbox); the controller commits after review. Skip only the git lines of each commit step.

**Goal:** Replace the one-blind-per-entry config flow with the remote-centric
wizard: learn/manual/virtual → remote settings → repeating cover loop →
one entry with cover subentries — plus a dual-format runtime shim so both old
(legacy) and new (remote) entries load and the suite stays green.

**Architecture:** `config_flow.py` keeps the entire learn/sniff capture
machinery verbatim and swaps everything after `learn_confirm` (and after the
manual/virtual identity forms) for two new stages: `remote_settings` (builds a
`RemoteConfig`, sets the entry unique_id guard) and a `cover` loop (collects
`CoverConfig`s under laminar validation), finishing with
`async_create_entry(..., subentries=[...])`. The legacy per-blind paths
(reuse, options flow, old reconfigure, cross-area guard, calibration
propagation) are deleted. `__init__.py`/`cover.py` gain a format switch: legacy
entries (with `channels` in data) keep today's behavior; remote entries load a
new `RemoteRuntime` and create no entities yet (Plan 03 adds them).

**Tech Stack:** Python 3.13+/3.14, `uv`, pytest/pytest-asyncio, Home Assistant
2026.5.4 (`ConfigSubentryData`, `async_create_entry(subentries=...)` verified
present), voluptuous + HA selectors.

## Global Constraints

- **Package manager:** `uv` only; never pip. `uv run pytest`, `uv run ruff check --fix`, `uv run ruff format`, `uv run mypy --strict`.
- **No linter suppressions** (`# noqa`, `# type: ignore`): fix root cause.
- **State-sync guardrail:** `tests/test_state_sync.py` byte-for-byte unchanged.
- **Files in scope:** `custom_components/zemismart_blinds/{models.py,__init__.py,cover.py,config_flow.py}` and `tests/{test_models.py,test_init.py,test_config_flow.py}`. Nothing else (strings.json is Plan 02b).
- **Landed Plan-01 types (use, do not re-implement):** `Role`, `CoverConfig`
  (`.channel_key`, `.has_travel`, `.from_subentry`, `.as_dict`), `RemoteConfig`
  (`.key`, `.from_entry`, `.as_dict`), `laminar_conflict(new, existing) ->
  "duplicate_channels" | "overlapping_channels" | None`, `derive_role`,
  `member_covers`. `BlindConfig` is UNCHANGED (no `role` field yet — Plan 03).
- **Entry formats:** legacy entry data contains `CONF_CHANNELS`; remote entry
  data does not (it is exactly `RemoteConfig.as_dict()`). This is the format
  discriminator everywhere.
- **Subentry shape:** `ConfigSubentryData(data=cover.as_dict(), subentry_type="cover", title=cover.name, unique_id=cover.channel_key)`.
- **Mid-branch feature gap (accepted):** entry reconfigure and the options flow
  are DELETED in this plan and re-introduced (reconfigure only, new semantics)
  in Plan 02b. Subentry add/reconfigure UI flows are also Plan 02b.
- **Wizard travel-time rule:** a cover whose channels strictly contain an
  already-collected cover's is *born aggregate*: travel fields optional
  (stored if supplied — preserved-unused per spec). Otherwise *born leaf*:
  both travel fields required (`travel_required` error).

---

## Task 1: `RemoteRuntime` (models.py)

**Files:**
- Modify: `custom_components/zemismart_blinds/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `RemoteRuntime(remote: RemoteConfig, hub: ZemismartHub)` —
  mutable `@dataclass(slots=True)` placed next to `EntryRuntime` at the bottom
  of `models.py`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_models.py`):

```python
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
```

- [ ] **Step 2:** `uv run pytest tests/test_models.py -k remote_runtime -v` → FAIL (ImportError).

- [ ] **Step 3:** Add after `EntryRuntime` in `models.py`:

```python
@dataclass(slots=True)
class RemoteRuntime:
    """Runtime data owned by one remote-centric config entry."""

    remote: RemoteConfig
    hub: ZemismartHub
```

- [ ] **Step 4:** Same pytest command → PASS.

- [ ] **Step 5:** `uv run ruff check --fix` + `format` on both files; `uv run mypy --strict custom_components/zemismart_blinds/models.py`. (Controller commits: `feat(models): add RemoteRuntime for remote-centric entries`.)

---

## Task 2: Dual-format entry loading (`__init__.py`, `cover.py`)

**Files:**
- Modify: `custom_components/zemismart_blinds/__init__.py`
- Modify: `custom_components/zemismart_blinds/cover.py`
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: `RemoteConfig.from_entry`, `RemoteRuntime`, `CONF_CHANNELS`.
- Produces: `type ZemismartConfigEntry = ConfigEntry[EntryRuntime | RemoteRuntime]`;
  remote-format entries set up successfully with zero cover entities.

- [ ] **Step 1: Write the failing test.** Open `tests/test_init.py` and copy its
existing setup pattern: find the test that sets up a legacy entry end-to-end
(it builds a `ConfigEntry` via a helper or `real_entry`-style constructor,
adds it with `await hass.config_entries.async_add(entry)`, and stubs MQTT).
Add alongside it, reusing that file's existing fixtures/helpers verbatim
(imports of `RemoteConfig`, `RemoteRuntime`, `RemoteIdentity`, and the
synthetic remote constants as the file already does for `BlindConfig`):

```python
async def test_remote_format_entry_sets_up_with_no_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote-centric entry loads the shared runtime and adds no covers yet."""
    from types import MappingProxyType

    from homeassistant import config_entries as ha_config_entries
    from homeassistant.config_entries import ConfigEntry

    from custom_components.zemismart_blinds.models import (
        RemoteConfig,
        RemoteIdentity,
        RemoteRuntime,
    )

    remote = RemoteConfig(
        name="Kitchen remote",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        area_id="kitchen",
        repeats=5,
    )
    entry = ConfigEntry(
        data=remote.as_dict(),
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id="remote-entry-1",
        minor_version=1,
        options={},
        source=ha_config_entries.SOURCE_USER,
        subentries_data=None,
        title=remote.name,
        unique_id=remote.key,
        version=1,
    )
    # Reuse this test file's existing MQTT stubbing + platform-forward pattern
    # exactly as the legacy setup test does before calling async_setup_entry.
    await add_and_setup(hass, monkeypatch, entry)  # the file's existing helper/pattern

    runtime = entry.runtime_data
    assert isinstance(runtime, RemoteRuntime)
    assert runtime.remote == remote
    assert [
        state for state in hass.states.async_all("cover")
    ] == []
```

If `tests/test_init.py` has no single `add_and_setup` helper, inline the same
sequence its legacy setup test uses (MQTT stub install → `await
hass.config_entries.async_add(entry)` or direct `async_setup_entry` call →
`await hass.async_block_till_done()`); mirror it exactly rather than inventing
a new pattern.

- [ ] **Step 2:** `uv run pytest tests/test_init.py -k remote_format -v` → FAIL
(legacy loader raises on missing `channels`, or `RemoteRuntime` assert fails).

- [ ] **Step 3: Implement the format switch.**

In `__init__.py`:

1. Extend the models import: `from .models import (BlindConfig, BridgeRegistry, DomainRuntime, EntryRuntime, RemoteConfig, RemoteRuntime, ZemismartHub)`; add `CONF_CHANNELS` to the `.const` import.
2. Change the alias: `type ZemismartConfigEntry = ConfigEntry[EntryRuntime | RemoteRuntime]`.
3. In `async_setup_entry`, replace

```python
    config = _entry_config(entry)
```

with

```python
    legacy_config = None if CONF_CHANNELS not in entry.data else _entry_config(entry)
```

4. Inside the lifecycle-lock block, replace

```python
                entry.runtime_data = EntryRuntime(config=config, hub=runtime.hub)
```

with

```python
                if legacy_config is None:
                    entry.runtime_data = RemoteRuntime(
                        remote=RemoteConfig.from_entry(entry.data),
                        hub=runtime.hub,
                    )
                else:
                    entry.runtime_data = EntryRuntime(config=legacy_config, hub=runtime.hub)
```

5. Guard the area assignment:

```python
                await hass.config_entries.async_forward_entry_setups(entry, [Platform.COVER])
                if legacy_config is not None:
                    await _async_assign_device_area(hass, entry, legacy_config)
```

In `cover.py`, add `RemoteRuntime` to the models import and change
`async_setup_entry` to:

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[EntryRuntime | RemoteRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create cover entities for one legacy blind/group entry."""
    del hass
    runtime = entry.runtime_data
    if isinstance(runtime, RemoteRuntime):
        # Remote-centric entries grow per-subentry entities in Plan 03.
        return
    async_add_entities([ZemismartCover(entry.entry_id, runtime)])
```

- [ ] **Step 4:** `uv run pytest tests/test_init.py -v` → new test PASSES and every existing test still passes.

- [ ] **Step 5:** ruff + `uv run mypy --strict custom_components/zemismart_blinds/`. (Controller commit: `feat(init): load remote-format entries via RemoteRuntime shim`.)

---

## Task 3: Identity helpers + `remote_settings` step (config_flow.py)

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow.py`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Produces:
  - `_remote_identity_from_manual(user_input: Mapping[str, Any]) -> RemoteIdentity`
    (raises `ValueError`) — the manual branch of today's `_config_from_input`,
    identity/calibration only.
  - `_remote_identity_from_capture(capture: _LearnCapture) -> RemoteIdentity`.
  - `_remote_settings_schema(suggested: Mapping[str, object] | None) -> vol.Schema`
    — fields `CONF_NAME` (Text), `CONF_AREA_ID` (Area), collapsed
    `_ADVANCED_SECTION` with `CONF_REPEATS`/`CONF_COALESCE_WINDOW_MS`
    (reuse `_int_value` defaults exactly like today's `_details_schema`
    advanced section).
  - Flow state: `_identity: RemoteIdentity | None = None`,
    `_remote: RemoteConfig | None = None`,
    `_covers: list[CoverConfig] | None = None`.
  - `async_step_remote_settings` — validates + builds `RemoteConfig`; on
    non-reconfigure sources runs `await self.async_set_unique_id(remote.key)`
    then `self._abort_if_unique_id_configured()`; stores `self._remote`,
    initializes `self._covers = []`, then `return await self.async_step_cover()`.

- [ ] **Step 1: Write the failing tests.** Replace the four `_config_from_input`
unit tests at the top of the test file (`test_manual_flow_derives_action_bases_from_one_direct_base`,
`test_manual_flow_accepts_direct_base_with_opcode_carry`,
`test_manual_unknown_remote_requires_a_calibration_source`,
`test_manual_flow_derives_bases_from_captured_reference`,
`test_manual_flow_rejects_ambiguous_or_wrong_identity_reference`) with
identity-helper equivalents, and delete
`test_known_remote_reuse_keeps_its_calibration`. Keep `manual_input()` but
strip its non-identity keys:

```python
def manual_input(**overrides: object) -> dict[str, Any]:
    """Return representative manual identity input with one explicit UP base."""
    values: dict[str, Any] = {
        CONF_PREFIX: "a1b2c3",
        CONF_REMOTE_ID: "42",
        CONF_CALIBRATION_BUTTON: "UP",
        CONF_CALIBRATION_BASE: "f42a",
        CONF_CALIBRATION_FRAME: "",
    }
    values.update(overrides)
    return values


def test_manual_identity_derives_action_bases_from_one_direct_base() -> None:
    """A labeled per-remote base is enough to derive all three action bases."""
    identity = config_flow_module._remote_identity_from_manual(manual_input())
    assert identity.prefix == TEST_PREFIX
    assert identity.remote_id == TEST_REMOTE_ID
    assert identity.bases == derive_bases_from_base("UP", 0xF42A, TEST_REMOTE_ID)


def test_manual_identity_requires_a_calibration_source() -> None:
    """An unknown remote with neither base nor reference is rejected."""
    with pytest.raises(ValueError, match="calibration"):
        config_flow_module._remote_identity_from_manual(
            manual_input(calibration_base="", prefix="000001", remote_id="02")
        )


def test_manual_identity_derives_bases_from_captured_reference() -> None:
    """A captured reference frame for the same identity calibrates the remote."""
    identity = config_flow_module._remote_identity_from_manual(
        manual_input(
            prefix=f"{REF_PREFIX:06x}",
            remote_id=f"{REF_REMOTE_ID:02x}",
            calibration_base="",
            calibration_frame=REFERENCE_FRAME,
        )
    )
    assert identity.bases is not None
    assert identity.bases.up == REF_BASES.up


def test_manual_identity_rejects_wrong_identity_reference() -> None:
    """A reference captured from a different remote must not calibrate this one."""
    with pytest.raises(ValueError, match="identity"):
        config_flow_module._remote_identity_from_manual(
            manual_input(calibration_base="", calibration_frame=REFERENCE_FRAME)
        )
    with pytest.raises(ValueError, match="not both"):
        config_flow_module._remote_identity_from_manual(
            manual_input(calibration_frame=REFERENCE_FRAME)
        )
```

(`calibration_base=`/`calibration_frame=` etc. keyword names in `overrides`
must be the CONF constant values — use `**{CONF_CALIBRATION_BASE: ""}` style if
the literal names differ; CONF values are `"calibration_base"`,
`"calibration_frame"`, `"prefix"`, `"remote_id"`, so plain keywords work.)

- [ ] **Step 2:** `uv run pytest tests/test_config_flow.py -k manual_identity -v` → FAIL (`AttributeError: _remote_identity_from_manual`).

- [ ] **Step 3: Implement.** In `config_flow.py`:

1. Imports: add `CoverConfig`, `RemoteConfig`, `laminar_conflict` to the
   `.models` import; add `ConfigSubentryData` to the `homeassistant.config_entries`
   surface (`from homeassistant.config_entries import ConfigSubentryData` —
   keep the existing `from homeassistant import config_entries` module import
   style for everything else).
2. Extract from `_config_from_input` (then DELETE `_config_from_input` in Task 5):

```python
def _remote_identity_from_manual(user_input: Mapping[str, Any]) -> RemoteIdentity:
    """Validate manual identity input into a calibrated RemoteIdentity."""
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
    identity = RemoteIdentity(prefix=prefix, remote_id=remote_id, bases=bases)
    if identity.bases is None:
        msg = "remote calibration is required"
        raise ValueError(msg)
    return identity


def _remote_identity_from_capture(capture: _LearnCapture) -> RemoteIdentity:
    """Derive the calibrated identity from one accepted sniff capture."""
    return RemoteIdentity(
        prefix=capture.prefix,
        remote_id=capture.remote_id,
        bases=derive_bases(
            capture.channels,
            capture.button,
            capture.command,
            capture.remote_id,
        ),
    )
```

3. Schema + step (class body gains the three state fields listed in
   Interfaces, following the existing class-attribute-default style):

```python
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
                                min=1, max=20, step=1,
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
                                min=0, max=2000, step=10,
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
```

and on the flow class:

```python
    async def async_step_remote_settings(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Name the remote, choose its area, and confirm transport settings."""
        if self._identity is None:
            return await self.async_step_user()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                flattened = _flatten_details(user_input)
                remote = RemoteConfig(
                    name=str(flattened.get(CONF_NAME, "")),
                    remote=self._identity,
                    area_id=str(flattened.get(CONF_AREA_ID, "")),
                    repeats=whole_number(flattened.get(CONF_REPEATS), CONF_REPEATS),
                    coalesce_window_ms=whole_number(
                        flattened.get(CONF_COALESCE_WINDOW_MS, DEFAULT_COALESCE_WINDOW_MS),
                        CONF_COALESCE_WINDOW_MS,
                    ),
                )
            except TypeError, ValueError:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(remote.key)
                self._abort_if_unique_id_configured()
                self._remote = remote
                self._covers = []
                return await self.async_step_cover()
        suggested: Mapping[str, object] | None = self._learn_suggested
        if user_input is not None:
            with suppress(TypeError, ValueError):
                suggested = _flatten_details(user_input)
        return self.async_show_form(
            step_id="remote_settings",
            data_schema=_remote_settings_schema(suggested),
            errors=errors,
        )
```

`whole_number` is already imported from `.models`.

- [ ] **Step 4:** `uv run pytest tests/test_config_flow.py -k manual_identity -v` → PASS. (Flow-walk coverage of `remote_settings` arrives with Tasks 4–5's wizard tests; full-file collection may still fail on not-yet-updated tests — that is expected until Task 6.)

- [ ] **Step 5:** ruff both files. **No mypy gate here**: `async_step_remote_settings` forward-references `async_step_cover`, which Task 4 introduces — Tasks 3+4 are one type-checkable unit and the strict-mypy gate for `config_flow.py` runs at the end of Task 4. (Controller commit after Task 4.)

---

## Task 4: Cover loop — schema, validation, steps (config_flow.py)

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow.py`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Produces:
  - `_cover_schema(suggested: Mapping[str, object] | None) -> vol.Schema` —
    `CONF_NAME` (Text, required), `CONF_CHANNELS` (Text, required),
    `CONF_TRAVEL_UP`/`CONF_TRAVEL_DOWN` (`vol.Optional`, NumberSelector
    0.1–600 s box, like today's `_details_schema` travel fields but optional
    and defaultless).
  - `_validate_cover_input(user_input, collected: list[CoverConfig]) -> tuple[CoverConfig | None, dict[str, str]]`
    — error keys: `CONF_CHANNELS: "invalid_config" | "duplicate_channels" | "overlapping_channels"`,
    `"base": "invalid_config" | "travel_required"`.
  - `async_step_cover`, `async_step_cover_menu`, `async_step_finish` on the flow.

- [ ] **Step 1: Write the failing unit tests** (append to test file):

```python
def test_validate_cover_input_travel_required_for_born_leaf() -> None:
    """A cover that contains no collected cover must supply both travel times."""
    cover, errors = config_flow_module._validate_cover_input(
        {CONF_NAME: "Sink", CONF_CHANNELS: "5"},
        [],
    )
    assert cover is None
    assert errors == {"base": "travel_required"}


def test_validate_cover_input_laminar_errors() -> None:
    """Duplicates and partial overlaps map to channel-field form errors."""
    from custom_components.zemismart_blinds.models import CoverConfig

    collected = [CoverConfig(name="Slider", channels=(1, 2, 3), travel_up=12.0, travel_down=12.0)]
    _cover, errors = config_flow_module._validate_cover_input(
        {CONF_NAME: "X", CONF_CHANNELS: "2,3,4", CONF_TRAVEL_UP: 5, CONF_TRAVEL_DOWN: 5},
        collected,
    )
    assert errors == {CONF_CHANNELS: "overlapping_channels"}
    _cover, errors = config_flow_module._validate_cover_input(
        {CONF_NAME: "X", CONF_CHANNELS: "3,2,1", CONF_TRAVEL_UP: 5, CONF_TRAVEL_DOWN: 5},
        collected,
    )
    assert errors == {CONF_CHANNELS: "duplicate_channels"}


def test_validate_cover_input_born_aggregate_travel_optional() -> None:
    """Strictly containing a collected cover lifts the travel requirement."""
    from custom_components.zemismart_blinds.models import CoverConfig

    collected = [
        CoverConfig(name="Slider", channels=(1, 2, 3), travel_up=12.0, travel_down=12.0),
        CoverConfig(name="Counter", channels=(4,), travel_up=8.0, travel_down=8.0),
    ]
    cover, errors = config_flow_module._validate_cover_input(
        {CONF_NAME: "Kitchen shades", CONF_CHANNELS: "1,2,3,4,5,6"},
        collected,
    )
    assert errors == {}
    assert cover is not None
    assert cover.channel_key == "1-2-3-4-5-6"
    assert cover.travel_up is None
```

- [ ] **Step 2:** `uv run pytest tests/test_config_flow.py -k validate_cover_input -v` → FAIL (AttributeError).

- [ ] **Step 3: Implement** in `config_flow.py`:

```python
def _cover_schema(suggested: Mapping[str, object] | None) -> vol.Schema:
    """Build one wizard cover form: name, channels, optional travel times."""
    values = suggested or {}
    travel_selector = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0.1, max=600, step=0.1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )
    fields: dict[vol.Marker, object] = {
        vol.Required(CONF_NAME, default=str(values.get(CONF_NAME, ""))): selector.TextSelector(),
        vol.Required(
            CONF_CHANNELS,
            default=str(values.get(CONF_CHANNELS, "")),
        ): selector.TextSelector(),
    }
    for key in (CONF_TRAVEL_UP, CONF_TRAVEL_DOWN):
        raw = values.get(key)
        marker = (
            vol.Optional(key, default=float(cast("float", raw)))
            if isinstance(raw, int | float) and not isinstance(raw, bool)
            else vol.Optional(key)
        )
        fields[marker] = travel_selector
    return vol.Schema(fields)


def _validate_cover_input(
    user_input: Mapping[str, Any],
    collected: list[CoverConfig],
) -> tuple[CoverConfig | None, dict[str, str]]:
    """Validate one wizard cover form against the covers collected so far."""
    try:
        channels = parse_channels(user_input.get(CONF_CHANNELS, ""))
    except ValueError:
        return None, {CONF_CHANNELS: "invalid_config"}
    conflict = laminar_conflict(channels, [cover.channels for cover in collected])
    if conflict is not None:
        return None, {CONF_CHANNELS: conflict}
    born_aggregate = any(
        frozenset(cover.channels) < frozenset(channels) for cover in collected
    )
    raw_up = user_input.get(CONF_TRAVEL_UP)
    raw_down = user_input.get(CONF_TRAVEL_DOWN)
    if not born_aggregate and (raw_up is None or raw_down is None):
        return None, {"base": "travel_required"}
    try:
        cover = CoverConfig(
            name=str(user_input.get(CONF_NAME, "")),
            channels=channels,
            travel_up=float(raw_up) if raw_up is not None else None,
            travel_down=float(raw_down) if raw_down is not None else None,
        )
    except TypeError, ValueError:
        return None, {"base": "invalid_config"}
    return cover, {}
```

(`cast` is imported from `typing` — add it to the existing `typing` import.)

Flow steps:

```python
    async def async_step_cover(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect one cover: name, channels, and leaf travel times."""
        if self._remote is None or self._covers is None:
            return await self.async_step_user()
        errors: dict[str, str] = {}
        if user_input is not None:
            cover, errors = _validate_cover_input(user_input, self._covers)
            if cover is not None:
                self._covers.append(cover)
                return await self.async_step_cover_menu()
        suggested: dict[str, object] = {}
        if not self._covers and self._capture is not None:
            suggested[CONF_CHANNELS] = ",".join(map(str, self._capture.channels))
        if user_input is not None:
            suggested = dict(user_input)
        return self.async_show_form(
            step_id="cover",
            data_schema=_cover_schema(suggested),
            errors=errors,
            description_placeholders={"count": str(len(self._covers))},
        )

    async def async_step_cover_menu(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer another cover or finishing the remote."""
        del user_input
        return self.async_show_menu(
            step_id="cover_menu",
            menu_options=["cover", "finish"],
            description_placeholders={"count": str(len(self._covers or []))},
        )

    async def async_step_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create the remote entry with every collected cover subentry."""
        del user_input
        remote = self._remote
        covers = self._covers
        if remote is None or not covers:
            return await self.async_step_user()
        # Final whole-list backstop: HA does not validate subentry unique_ids
        # at initial entry creation, and flow-state replay could bypass the
        # per-iteration checks.
        for index, cover in enumerate(covers):
            others = [c.channels for i, c in enumerate(covers) if i != index]
            if laminar_conflict(cover.channels, others) is not None:
                return self.async_abort(reason="channel_conflict")
        await self.async_set_unique_id(remote.key)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=remote.name,
            data=remote.as_dict(),
            subentries=[
                ConfigSubentryData(
                    data=cover.as_dict(),
                    subentry_type="cover",
                    title=cover.name,
                    unique_id=cover.channel_key,
                )
                for cover in covers
            ],
        )
```

Note the finish backstop flags ANY conflict — a strict-nesting pair triggers
`laminar_conflict(...) is None`, so nesting passes; only duplicates/partial
overlaps abort. (Verify with the Step 1 unit tests plus the wizard test in
Task 5.)

- [ ] **Step 4:** `uv run pytest tests/test_config_flow.py -k validate_cover_input -v` → PASS.

- [ ] **Step 5:** ruff; mypy `--strict` on `config_flow.py`. (Controller commit: `feat(flow): wizard cover loop with laminar validation`.)

---

## Task 5: Wire the paths; delete the legacy flow surfaces

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow.py`
- Modify: `custom_components/zemismart_blinds/__init__.py`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Learn: `learn_confirm` menu becomes `["remote_settings", "learn_retry", "advanced"]`;
  choosing `remote_settings` requires `self._identity` set from the capture.
- Advanced menu becomes `["manual", "virtual"]`.
- Deleted from `config_flow.py`: `async_step_learn_details`,
  `async_step_advanced_details`, `async_step_reuse`, `async_step_reconfigure`,
  `async_step_reconfigure_learn`, `async_step_reconfigure_edit`,
  `_async_finish_config`, `async_on_create_entry`, `_offer_reuse_continuation`,
  `_reconfigure_config`, `_reuse_selected`, `_advanced_identity`,
  `_details_schema`, `_config_from_input`, `_suggested_for`,
  `_cross_area_overlap`, `_unique_id`, `_materialize_virtual_remote`,
  `_propagate_calibration`, `known_remotes`, `_known_remote_options`,
  `_reuse_schema`, `ZemismartBlindsOptionsFlow`, `async_get_options_flow`,
  and the flow-continuation `data` handling in `async_step_user`.
  KEEP: `effective_values` (legacy `__init__` loader), `_flatten_details`,
  `_float_value`, `_int_value`, all learn/sniff machinery.
- `__init__.py`: `_known_remote_pairs` no longer imports `known_remotes`.

- [ ] **Step 1: Write the failing wizard tests.** REPLACE
`test_learn_happy_path_creates_backward_compatible_entry` with the following
(same fixture/harness usage; the sniff/capture segment — everything up to and
including the `learn_confirm` assertions — is IDENTICAL to the old test except
the menu list; copy it, then continue as shown). Also REPLACE
`test_advanced_paths_create_backward_compatible_entries` with the manual test
below, and ADD the duplicate-abort test:

```python
async def test_learn_wizard_creates_remote_entry_with_cover_subentries(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard captures a remote, then collects covers into subentries."""
    # ... identical harness + learn walk as the old happy-path test, with ONE
    # change at learn_confirm:
    #   assert result["menu_options"] == ["remote_settings", "learn_retry", "advanced"]
    # then continue:

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "remote_settings"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "remote_settings"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kitchen remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {CONF_REPEATS: 5, CONF_COALESCE_WINDOW_MS: 150},
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cover"
    schema = result["data_schema"]
    assert schema is not None
    # captured channels are prefilled for the first cover
    assert schema({CONF_NAME: "Slider", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12})[
        CONF_CHANNELS
    ] == "1,2"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Slider", CONF_CHANNELS: "1,2", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "cover_menu"
    assert result["menu_options"] == ["cover", "finish"]

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "cover"},
    )
    # partial overlap rejected
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Bad", CONF_CHANNELS: "2,3", CONF_TRAVEL_UP: 9, CONF_TRAVEL_DOWN: 9},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNELS: "overlapping_channels"}
    # born-leaf without travel rejected
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Sink", CONF_CHANNELS: "5"},
    )
    assert result["errors"] == {"base": "travel_required"}
    # aggregate strictly containing the slider needs no travel
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Kitchen shades", CONF_CHANNELS: "1,2,3"},
    )
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"next_step_id": "finish"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen remote"
    expected_remote = RemoteConfig(
        name="Kitchen remote",
        remote=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_ACTION_BASES),
        area_id="kitchen",
        repeats=5,
        coalesce_window_ms=150,
    )
    assert result["data"] == expected_remote.as_dict()
    entry = result["result"]
    assert entry.unique_id == "a1b2c3:42"
    subentries = list(entry.subentries.values())
    assert [(s.subentry_type, s.title, s.unique_id) for s in subentries] == [
        ("cover", "Slider", "1-2"),
        ("cover", "Kitchen shades", "1-2-3"),
    ]
    slider = CoverConfig.from_subentry(subentries[0].data)
    assert slider.channels == (1, 2)
    assert slider.travel_up == 12.0
    aggregate = CoverConfig.from_subentry(subentries[1].data)
    assert aggregate.travel_up is None


async def test_manual_wizard_and_duplicate_remote_abort(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual identity enters the same wizard; a second identical remote aborts."""
    prepare_config_flow(hass, monkeypatch)

    async def run_manual_to_settings() -> tuple[str, ConfigFlowResult]:
        result = await start_user_flow(hass)
        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "advanced"}
        )
        assert result["menu_options"] == ["manual", "virtual"]
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "manual"}
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_PREFIX: "a1b2c3",
                CONF_REMOTE_ID: "42",
                CONF_CALIBRATION_BUTTON: "UP",
                CONF_CALIBRATION_BASE: "f42a",
                CONF_CALIBRATION_FRAME: "",
                CONF_BASE_TRAILER: "",
            },
        )
        assert result["step_id"] == "remote_settings"
        return flow_id, result

    flow_id, _ = await run_manual_to_settings()
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kitchen remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {CONF_REPEATS: 5, CONF_COALESCE_WINDOW_MS: 150},
        },
    )
    assert result["step_id"] == "cover"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Sink", CONF_CHANNELS: "5", CONF_TRAVEL_UP: 9, CONF_TRAVEL_DOWN: 9},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "a1b2c3:42"

    flow_id, _ = await run_manual_to_settings()
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Duplicate remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {CONF_REPEATS: 5, CONF_COALESCE_WINDOW_MS: 150},
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
```

Add the needed imports at the top of the test file: `RemoteConfig`,
`CoverConfig` from the models module (extend the existing import), and
`CONF_BASE_TRAILER` if not present.

- [ ] **Step 2:** `uv run pytest tests/test_config_flow.py -k "wizard" -v` → FAIL
(learn_confirm still menus to learn_details; manual still routes to
advanced_details).

- [ ] **Step 3: Implement wiring + deletions.**

1. `async_step_user`: body becomes exactly

```python
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer guided learning before the Advanced fallback paths."""
        del user_input
        return self.async_show_menu(step_id="user", menu_options=["learn", "advanced"])
```

2. `async_step_learn_confirm`: change `menu_options=["learn_details", "learn_retry", "advanced"]` to `menu_options=["remote_settings", "learn_retry", "advanced"]`, and immediately before returning the menu set `self._identity = _remote_identity_from_capture(capture)` (wrap in `try/except ValueError: return await self.async_step_learn_timeout()` — an underivable capture is equivalent to no capture).
3. `async_step_advanced`: `menu_options=["manual", "virtual"]`.
4. `async_step_manual`: on submit, `try: self._identity = _remote_identity_from_manual(user_input)` / `except ValueError: errors["base"] = "invalid_config"` else `return await self.async_step_remote_settings()`. Form remains `_manual_schema(user_input)`.
5. `async_step_virtual`:

```python
    async def async_step_virtual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Allocate a calibrated virtual identity before the wizard."""
        del user_input
        from . import new_virtual_remote_identity

        prefix, remote_id, bases = new_virtual_remote_identity(self.hass)
        self._identity = RemoteIdentity(prefix=prefix, remote_id=remote_id, bases=bases)
        return await self.async_step_remote_settings()
```

6. Delete every name listed in Interfaces above, including the whole
   `ZemismartBlindsOptionsFlow` class. Remove now-unused imports (ruff will
   flag them).
7. `__init__.py`'s `_known_remote_pairs` becomes:

```python
def _known_remote_pairs(hass: HomeAssistant) -> set[tuple[int, int]]:
    """Return remote identities already stored in config entries."""
    from .models import parse_hex

    pairs: set[tuple[int, int]] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        try:
            pairs.add(
                (
                    parse_hex(entry.data.get(CONF_PREFIX), CONF_PREFIX, 24),
                    parse_hex(entry.data.get(CONF_REMOTE_ID), CONF_REMOTE_ID, 8),
                )
            )
        except ValueError:
            continue
    return pairs
```

- [ ] **Step 4:** `uv run pytest tests/test_config_flow.py -k "wizard or manual_identity or validate_cover_input" -v` → PASS. (Other tests in the file still reference deleted steps — Task 6.)

- [ ] **Step 5:** ruff; mypy `--strict` on the whole package. (Controller commit: `feat(flow)!: remote-centric wizard replaces per-blind flow`.)

---

## Task 6: Test-file migration and full-suite green

**Files:**
- Modify: `tests/test_config_flow.py`
- Verify: whole suite

- [ ] **Step 1: Delete obsolete tests** (they exercise deleted features):
  - `test_known_remote_reuse_keeps_its_calibration` (if not already removed in Task 3)
  - `test_reconfigure_relearn_reloads_and_clears_stale_options`
  - `test_reconfigure_edit_keeps_remote_and_clears_options`
  - `test_options_flow_still_edits_travel_and_area`
  - In `tests/test_init.py`:
    `test_flow_rejects_same_remote_channel_overlap_across_areas` (it imports
    the deleted `_cross_area_overlap`; the cross-area guard is intentionally
    gone — per-remote area replaces it). Check test_init.py for any other
    import of deleted config_flow names (`known_remotes`,
    `_propagate_calibration`, options flow) and delete/adapt those tests the
    same way.
  Also delete `details_input()` and `real_entry()` if nothing references them
  afterward (check with grep; `real_entry` may still be used by kept tests —
  keep it if so).

- [ ] **Step 2: Update the learn-machinery tests** for the new step topology.
  Mechanical rules:
  - Any assertion `result["menu_options"] == ["learn_details", "learn_retry", "advanced"]` → `["remote_settings", "learn_retry", "advanced"]`.
  - Any `{"next_step_id": "learn_details"}` navigation → `{"next_step_id": "remote_settings"}` followed by the remote_settings/cover submissions from the wizard test if the test needs to reach CREATE_ENTRY; tests that only exercise capture/timeout/retry/cleanup stop before that and need no more changes.
  - `test_learn_allows_explicit_online_bridge_override` walks to entry creation: after its capture confirm, replace the old details submission with the remote_settings + one-leaf-cover + finish sequence (copy from the wizard test, adjusting names).
  - The timeout/retry/session/subscription/abort/serialization/no-bridge/no-mqtt tests (`test_learn_timeout_retry_ignores_stale_session`, `test_learn_subscription_readiness_uses_the_same_timeout_budget`, `test_learn_abort_cleans_capture_and_ignores_late_frame`, `test_learn_serializes_concurrent_sniffs_on_one_bridge`, `test_learn_without_online_bridges_offers_advanced`, `test_learn_without_mqtt_offers_advanced`, `test_user_starts_with_learn_and_advanced_menu`) exercise machinery that did not move; update only step-name/menu assertions that changed (`user` menu still `["learn", "advanced"]` — unchanged).

- [ ] **Step 3:** `uv run pytest tests/test_config_flow.py -v` → ALL PASS.

- [ ] **Step 4: Full Definition-of-Done**

```bash
uv run pytest -q                                             # all green
uv run mypy --strict custom_components/zemismart_blinds/     # clean
uv run ruff check custom_components/zemismart_blinds/ tests/ # clean
git diff --stat main -- tests/test_state_sync.py             # empty
```

(Controller commit: `test(flow): migrate suite to remote-centric wizard`.)

---

## Definition of done (Plan 02a)

- [ ] Wizard end-to-end: learn → remote_settings → cover loop → entry +
  subentries (`test_learn_wizard_creates_remote_entry_with_cover_subentries`).
- [ ] Manual + virtual paths reach the same wizard; duplicate remote aborts
  `already_configured`.
- [ ] Laminar violations and missing leaf travel are rejected inside the loop.
- [ ] Remote-format entries load with zero entities; legacy entries unchanged.
- [ ] Options flow, reuse, old reconfigure, `_cross_area_overlap`,
  `_propagate_calibration` are gone.
- [ ] Full suite green; package `mypy --strict` clean; ruff clean;
  `test_state_sync.py` untouched.

Then author Plan 02b (subentry flows, entry reconfigure, strings) against this
landed code.
