# Plan 02b — Subentry Flows, Entry Reconfigure, Strings

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **Git deviation:** never run `git add`/`git commit` (linked-worktree metadata is outside the sandbox); the controller commits after review.

**Goal:** Complete the config layer: per-cover subentry add/reconfigure flows,
the new entry-level reconfigure (relearn + edit settings), and the
strings/translations rewrite for the new step topology.

**Architecture:** A `CoverSubentryFlow` (registered via
`async_get_supported_subentry_types`) reuses `_cover_schema`/`_validate_cover_input`
but validates against the entry's live sibling subentries and carries hidden
travel keys forward on reconfigure. The main flow gains
`async_step_reconfigure` (menu) → `reconfigure_learn` (reuses the sniff
machinery; `learn_confirm` routes to `reconfigure_apply` when
`source == SOURCE_RECONFIGURE`) and `reconfigure_edit` (settings +
calibration-base correction). Entry reconfigure ends with
`async_update_reload_and_abort` — interim until Plan 04 makes the update
listener the sole reload owner. Strings/translations are rewritten to match.

**Implementer routing (user directive):** Tasks 1–2 → Codex GPT-5.6-sol,
reasoning effort **xhigh**. Task 3 (mechanical strings rewrite) → `agent`
CLI (Grok 4.5), output reviewed by the controller before commit.

## Global Constraints

- `uv` only; never pip. No `# noqa` / `# type: ignore`.
- `tests/test_state_sync.py` byte-for-byte unchanged.
- Files in scope: `custom_components/zemismart_blinds/{config_flow.py,strings.json,translations/en.json}`, `tests/test_config_flow.py`. Nothing else.
- Landed API (verified in HA 2026.5.4, use exactly):
  - `ConfigFlow.async_get_supported_subentry_types(cls, config_entry) -> dict[str, type[ConfigSubentryFlow]]` (classmethod).
  - `ConfigSubentryFlow`: `async_create_entry(data=..., title=..., unique_id=...)`,
    `async_update_and_abort(entry, subentry, *, data=..., title=..., unique_id=...)`,
    `_get_entry()`, `_get_reconfigure_subentry()`; add flow starts at
    `async_step_user`, reconfigure at `async_step_reconfigure`
    (`init_step = context["source"]`).
  - Subentry flow test API:
    `hass.config_entries.subentries.async_init((entry_id, "cover"), context={"source": config_entries.SOURCE_USER})`
    and, for reconfigure,
    `context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": <id>}`;
    `hass.config_entries.subentries.async_configure(flow_id, input)`.
  - Entry reconfigure test API: `hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})`.
- Landed flow surfaces to REUSE (do not duplicate): `_cover_schema`,
  `_validate_cover_input`, `_manual_schema`, `_remote_settings_schema`,
  `_flatten_details`, learn machinery (`learn_setup`/`learn_sniff`/
  `learn_confirm`/`learn_retry`/`learn_timeout`), `_remote_identity_from_capture`,
  `CoverConfig`, `RemoteConfig`, `laminar_conflict`, `parse_hex`, `whole_number`.
- Baseline: 620 tests green at `f888574`.

---

## Task 1 (Codex xhigh): `CoverSubentryFlow` + registration

**Files:** `custom_components/zemismart_blinds/config_flow.py`, `tests/test_config_flow.py`

**Interfaces produced:**
- `class CoverSubentryFlow(config_entries.ConfigSubentryFlow)` with
  `async_step_user` (add) and `async_step_reconfigure`.
- On `ZemismartBlindsConfigFlow`:

```python
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: config_entries.ConfigEntry,
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Expose per-cover subentry management on remote entries."""
        del config_entry
        return {"cover": CoverSubentryFlow}
```

- Shared helper (module level):

```python
def _sibling_covers(
    entry: config_entries.ConfigEntry,
    *,
    exclude_subentry_id: str | None = None,
) -> list[CoverConfig]:
    """Parse every cover subentry of one entry except the excluded one."""
    covers: list[CoverConfig] = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != "cover":
            continue
        if exclude_subentry_id is not None and subentry.subentry_id == exclude_subentry_id:
            continue
        with suppress(TypeError, ValueError):
            covers.append(CoverConfig.from_subentry(subentry.data))
    return covers
```

**Behavior:**
- **Add (`async_step_user`)**: show `_cover_schema(user_input or {})`; on
  submit run `_validate_cover_input(user_input, _sibling_covers(self._get_entry()))`;
  on success `return self.async_create_entry(data=cover.as_dict(), title=cover.name, unique_id=cover.channel_key)`.
- **Reconfigure (`async_step_reconfigure`)**: prefill the form from
  `self._get_reconfigure_subentry().data`. On submit, validate against
  `_sibling_covers(entry, exclude_subentry_id=subentry.subentry_id)`.
  **Hidden-travel carry-forward:** before validation, if the submitted input
  omits `CONF_TRAVEL_UP`/`CONF_TRAVEL_DOWN` and the stored subentry data has
  non-empty values for them, merge the stored values into the input — a
  reconfigure that hides (born-aggregate) or simply leaves travel untouched
  must never erase stored calibration; `_validate_cover_input`'s
  travel-required check then applies to the MERGED input, so
  reconfigure-to-leaf passes iff travel is supplied now or already stored.
  On success `return self.async_update_and_abort(self._get_entry(), self._get_reconfigure_subentry(), data=cover.as_dict(), title=cover.name, unique_id=cover.channel_key)`.
- Delete needs no code (native HA).

**Tests (add to `tests/test_config_flow.py`):** helper first —

```python
async def create_remote_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    covers: list[dict[str, Any]],
) -> ConfigEntry:
    """Drive the manual wizard to a real remote entry with the given covers."""
    prepare_config_flow(hass, monkeypatch)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "advanced"})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    await hass.config_entries.flow.async_configure(
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
    await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Kitchen remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {CONF_REPEATS: 5, CONF_COALESCE_WINDOW_MS: 150},
        },
    )
    for cover in covers:
        await hass.config_entries.flow.async_configure(flow_id, cover)
        await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "cover"})
    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result["result"]
```

(NOTE the loop above ends every cover with a `next_step_id: cover` navigation,
which leaves the flow showing an empty cover form before `finish`; check the
landed `async_step_finish` guard `if remote is None or not covers` — the
collected list is non-empty so finish works. If the menu navigation instead
errors because the current step is the form, adjust to navigate
`cover_menu → finish` only after the LAST cover: drive
`{"next_step_id": "cover"}` between covers, nothing after the last.)

Then four tests:

```python
async def test_subentry_add_creates_cover(hass, monkeypatch) -> None:
    entry = await create_remote_entry(
        hass, monkeypatch,
        [{CONF_NAME: "Slider", CONF_CHANNELS: "1,2,3", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12}],
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Sink", CONF_CHANNELS: "5", CONF_TRAVEL_UP: 9, CONF_TRAVEL_DOWN: 9},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentries = {s.unique_id: s for s in entry.subentries.values()}
    assert "5" in subentries
    assert subentries["5"].title == "Sink"


async def test_subentry_add_rejects_partial_overlap_and_duplicate(hass, monkeypatch) -> None:
    entry = await create_remote_entry(
        hass, monkeypatch,
        [{CONF_NAME: "Slider", CONF_CHANNELS: "1,2,3", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12}],
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Bad", CONF_CHANNELS: "3,4", CONF_TRAVEL_UP: 9, CONF_TRAVEL_DOWN: 9},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CHANNELS: "overlapping_channels"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Dup", CONF_CHANNELS: "1,2,3", CONF_TRAVEL_UP: 9, CONF_TRAVEL_DOWN: 9},
    )
    assert result["errors"] == {CONF_CHANNELS: "duplicate_channels"}


async def test_subentry_reconfigure_carries_hidden_travel_forward(hass, monkeypatch) -> None:
    entry = await create_remote_entry(
        hass, monkeypatch,
        [
            {CONF_NAME: "Slider", CONF_CHANNELS: "1,2,3", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12},
            {CONF_NAME: "Sink", CONF_CHANNELS: "5", CONF_TRAVEL_UP: 9, CONF_TRAVEL_DOWN: 9},
        ],
    )
    sink = next(s for s in entry.subentries.values() if s.unique_id == "5")
    # Rechannel the sink to strictly contain the slider: born aggregate, travel
    # fields omitted — stored 9/9 must survive in data.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": sink.subentry_id,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Kitchen shades", CONF_CHANNELS: "1,2,3,5"},
    )
    assert result["type"] is FlowResultType.ABORT
    updated = next(s for s in entry.subentries.values() if s.subentry_id == sink.subentry_id)
    assert updated.unique_id == "1-2-3-5"
    assert updated.title == "Kitchen shades"
    restored = CoverConfig.from_subentry(updated.data)
    assert restored.travel_up == 9.0  # carried forward, not erased


async def test_subentry_reconfigure_to_leaf_requires_travel(hass, monkeypatch) -> None:
    entry = await create_remote_entry(
        hass, monkeypatch,
        [
            {CONF_NAME: "Slider", CONF_CHANNELS: "1,2,3", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12},
            {CONF_NAME: "All", CONF_CHANNELS: "1,2,3,4"},  # aggregate, no travel stored
        ],
    )
    aggregate = next(s for s in entry.subentries.values() if s.unique_id == "1-2-3-4")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "cover"),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": aggregate.subentry_id,
        },
    )
    # Rechannel to a disjoint set (a leaf) without supplying travel: rejected.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Solo", CONF_CHANNELS: "6"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "travel_required"}
```

**TDD order:** write the helper + first test → run
`uv run pytest tests/test_config_flow.py -k subentry_add_creates -v` → FAIL
(`UnknownHandler`) → implement registration + flow → PASS → remaining tests
one at a time. Then `uv run ruff check --fix` + `format` on both files and
`uv run mypy --strict custom_components/zemismart_blinds/`.

---

## Task 2 (Codex xhigh): Entry reconfigure — relearn + edit settings

**Files:** `custom_components/zemismart_blinds/config_flow.py`, `tests/test_config_flow.py`

**Interfaces produced (on `ZemismartBlindsConfigFlow`):**
- `async_step_reconfigure` — menu `["reconfigure_learn", "reconfigure_edit"]`.
- `async_step_reconfigure_learn` — seeds `self._learn_suggested` with
  `CONF_NAME`/`CONF_AREA_ID` from the entry's data, resets
  `self._learn_registry = None`, delegates to `async_step_learn_setup`
  (capture routing only; the stored name/area/settings are what persist).
- In `async_step_learn_confirm`: when `self.source == config_entries.SOURCE_RECONFIGURE`,
  menu is `["reconfigure_apply", "learn_retry"]` instead of
  `["remote_settings", "learn_retry", "advanced"]`.
- `async_step_reconfigure_apply` — requires `self._identity` (set by
  `learn_confirm`); builds the updated remote:

```python
        entry = self._get_reconfigure_entry()
        current = RemoteConfig.from_entry(entry.data)
        updated = RemoteConfig(
            name=current.name,
            remote=self._identity,
            area_id=current.area_id,
            repeats=current.repeats,
            coalesce_window_ms=current.coalesce_window_ms,
        )
        if any(
            other.entry_id != entry.entry_id and other.unique_id == updated.key
            for other in self.hass.config_entries.async_entries(DOMAIN)
        ):
            return self.async_abort(reason="already_configured")
        return self.async_update_reload_and_abort(
            entry,
            title=updated.name,
            unique_id=updated.key,
            data=updated.as_dict(),
        )
```

- `async_step_reconfigure_edit` — form: `CONF_NAME` (Text), `CONF_AREA_ID`
  (Area), calibration texts `CONF_BASE_UP`/`CONF_BASE_DOWN`/`CONF_BASE_STOP`
  (required Text, prefilled 4-hex) and `CONF_BASE_TRAILER` (optional Text,
  prefilled or empty), collapsed `_ADVANCED_SECTION` with
  `CONF_REPEATS`/`CONF_COALESCE_WINDOW_MS` — all prefilled from
  `RemoteConfig.from_entry(entry.data)`. `CONF_PREFIX`/`CONF_REMOTE_ID` are
  NOT on the form (identity is relearn-only). On submit: parse bases with
  `parse_hex(..., 16)` (trailer only when non-empty), build
  `RemoteIdentity(prefix=current.remote.prefix, remote_id=current.remote.remote_id, bases=CommandBases(...))`,
  then `RemoteConfig(...)`; errors → `{"base": "invalid_config"}`. Success:
  `async_update_reload_and_abort(entry, title=updated.name, data=updated.as_dict())`
  (unique_id unchanged — same identity). Import `CONF_BASE_UP`,
  `CONF_BASE_DOWN`, `CONF_BASE_STOP` into config_flow's `.const` import
  (CONF_BASE_TRAILER already imported).

**Tests:**

```python
async def test_reconfigure_edit_updates_settings_and_keeps_identity(hass, monkeypatch) -> None:
    entry = await create_remote_entry(
        hass, monkeypatch,
        [{CONF_NAME: "Slider", CONF_CHANNELS: "1,2,3", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12}],
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_edit"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Kitchen remote",
            CONF_AREA_ID: "pantry",
            CONF_BASE_UP: "f42a",
            CONF_BASE_DOWN: "bcf2",
            CONF_BASE_STOP: "dc12",
            CONF_BASE_TRAILER: "dd05",
            ADVANCED_SECTION: {CONF_REPEATS: 8, CONF_COALESCE_WINDOW_MS: 0},
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = RemoteConfig.from_entry(entry.data)
    assert updated.area_id == "pantry"
    assert updated.repeats == 8
    assert updated.key == "a1b2c3:42"
    assert updated.remote.bases is not None
    assert updated.remote.bases.trailer == 0xDD05
    # subentries untouched
    assert [s.unique_id for s in entry.subentries.values()] == ["1-2-3"]


async def test_reconfigure_relearn_applies_new_identity_and_collides(hass, monkeypatch) -> None:
    entry = await create_remote_entry(
        hass, monkeypatch,
        [{CONF_NAME: "Slider", CONF_CHANNELS: "1,2", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 12}],
    )
    fake = FakeMqtt()
    install_mqtt(monkeypatch, fake)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_learn"}
    )
    assert result["step_id"] == "learn_setup"
    flow_id = result["flow_id"]
    # name/area prefilled from the entry; drive the standard sniff walk
    schema = result["data_schema"]
    assert schema is not None
    setup_values = schema({})
    assert setup_values[CONF_NAME] == "Kitchen remote"
    result = await hass.config_entries.flow.async_configure(flow_id, setup_values)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await fake.wait_for_publications(1)
    rx = fake.rx_subscriptions()[0]
    # Emit on the subscription's own topic: the entry's area ("kitchen")
    # matches no fake bridge, so automatic selection routes to the default
    # bridge — hardcoding a bridge id here would miss the capture handler's
    # exact-topic check.
    await fake.emit(
        rx,
        rx.topic,
        json.dumps({"frame": b0_to_b1(SECOND_REMOTE_UP_B0), "t": 3}),
    )
    await fake.wait_for_publications(2)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "learn_confirm"
    assert result["menu_options"] == ["reconfigure_apply", "learn_retry"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "reconfigure_apply"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = RemoteConfig.from_entry(entry.data)
    assert updated.key == f"{REF_PREFIX:06x}:{REF_REMOTE_ID:02x}"
    assert entry.unique_id == updated.key
    assert updated.name == "Kitchen remote"          # preserved
    assert [s.unique_id for s in entry.subentries.values()] == ["1-2"]  # untouched
```

For `SECOND_REMOTE_UP_B0`, add near `REFERENCE_FRAME`:

```python
SECOND_REMOTE_UP_B0 = encode_b0(
    make_payload(REF_PREFIX, REF_REMOTE_ID, (1, 2), "UP", bases=REF_BASES)
)
```

The capture decodes to prefix `REF_PREFIX`/id `REF_REMOTE_ID` — a different
identity than the entry's `a1b2c3:42`, so apply succeeds. For the collision
variant, add a second entry with that identity first via `create_remote_entry`
— but `create_remote_entry` hardcodes `a1b2c3:42`; parameterize the helper's
prefix/remote_id/base (defaults preserved) so a second entry with
`f"{REF_PREFIX:06x}"`/`f"{REF_REMOTE_ID:02x}"`/`f"{REF_BASES.up:04x}"` can be
created; then relearning the FIRST entry to that identity must return
`FlowResultType.ABORT` with reason `already_configured` from
`reconfigure_apply`, and `entry.data` must be UNCHANGED.
Write that as a third test `test_reconfigure_relearn_collision_aborts`.

**TDD order:** edit test → FAIL (`reconfigure` step unknown) → implement menu +
edit → PASS → relearn test → FAIL → implement learn routing + apply → PASS →
collision test → PASS. Then ruff + package strict mypy + full
`uv run pytest -q`.

---

## Task 3 (agent/Grok 4.5, controller-reviewed): strings + translations

**Files:** `custom_components/zemismart_blinds/strings.json`, `custom_components/zemismart_blinds/translations/en.json`

Rewrite both files (they must stay identical) for the new topology:

- **Delete**: the whole `"options"` section; steps `learn_details`,
  `advanced_details`, `reuse`, `reconfigure_edit`-old-shape; errors
  `no_known_remotes`, `cross_area_overlap` (both occurrences).
- **Keep** (unchanged copy): `user`, `learn_setup`, `learn_unavailable`,
  `learn_sniff`, `learn_timeout`, `manual`, `progress.sniffing`, `selector`,
  `services` sections.
- **Update**: `advanced.menu_options` → only `manual`/`virtual`.
  `learn_confirm.menu_options` → `remote_settings` ("Continue"),
  `learn_retry`, `advanced`, plus `reconfigure_apply` ("Apply new identity").
- **Add** config steps:
  - `remote_settings`: title "Name the remote", data `name` ("Remote name"),
    `area_id` ("Home Assistant area"), advanced section as in the old
    `learn_details` (repeats + coalesce descriptions verbatim).
  - `cover`: title "Add a cover", description "Cover {count} so far. Name this
    blind or group and enter its channels.", data `name`, `channels`,
    `travel_up`, `travel_down`; data_description for `channels` (comma list)
    and for travel fields: "Required for a blind; leave empty only for a group
    that contains covers you already added."
  - `cover_menu`: title "Cover added", description "{count} cover(s) configured.",
    menu_options `cover` ("Add another cover"), `finish` ("Finish").
  - `reconfigure`: title "Reconfigure remote", menu_options
    `reconfigure_learn` ("Relearn from remote"), `reconfigure_edit`
    ("Edit settings").
  - `reconfigure_edit`: title "Edit remote settings", data `name`, `area_id`,
    `base_up` ("UP base (16-bit hex)"), `base_down`, `base_stop`,
    `base_trailer` ("TRAILER base (optional)"), advanced section as above.
- **Errors** (config): keep `invalid_config`, `bridge_unavailable`,
  `already_configured`; add `travel_required` ("Enter both travel times — only
  a group containing existing covers can omit them."),
  `duplicate_channels` ("Another cover of this remote already uses exactly
  these channels."), `overlapping_channels` ("Channels may be fully inside,
  fully containing, or separate from another cover — never partially
  overlapping.").
- **Abort** (config): keep `already_configured`, `reconfigure_successful`; add
  `channel_conflict` ("The collected covers conflict; start over.").
- **Add** top-level `config_subentries` section:

```json
"config_subentries": {
  "cover": {
    "entry_type": "Cover",
    "initiate_flow": {
      "user": "Add cover",
      "reconfigure": "Reconfigure cover"
    },
    "step": {
      "user": {
        "title": "Add a cover",
        "data": { "name": "...", "channels": "...", "travel_up": "...", "travel_down": "..." },
        "data_description": { "...same as config cover step..." }
      },
      "reconfigure": {
        "title": "Reconfigure cover",
        "data": { "...same keys..." },
        "data_description": { "...same..." }
      }
    },
    "error": {
      "invalid_config": "...", "travel_required": "...",
      "duplicate_channels": "...", "overlapping_channels": "..."
    }
  }
}
```

(fill the "..." with the same strings as the config `cover` step / errors —
no placeholders may remain in the delivered file).

**Verification:** `python3 -c "import json; a=json.load(open('custom_components/zemismart_blinds/strings.json')); b=json.load(open('custom_components/zemismart_blinds/translations/en.json')); assert a==b; print('valid+identical')"`,
then full suite + ruff (no Python changes expected).

---

## Definition of done (Plan 02b)

- [ ] Subentry add/reconfigure flows registered and green (4 new tests).
- [ ] Entry reconfigure: edit settings, relearn apply, relearn collision (3 new tests).
- [ ] strings.json/translations rewritten, valid JSON, identical, no leftover
  `options`/reuse/cross-area strings, no placeholder text.
- [ ] `uv run pytest -q` green; `uv run mypy --strict custom_components/zemismart_blinds/` clean; `uv run ruff check` clean; `test_state_sync.py` untouched.
