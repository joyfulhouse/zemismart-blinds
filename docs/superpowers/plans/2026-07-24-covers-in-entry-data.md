# Covers Into Entry Data (Retire Subentries) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution model for this plan (owner-directed):** each task is dispatched to Codex
> gpt-5.6-sol at xhigh effort with the attested spec
> (`docs/superpowers/specs/2026-07-24-covers-in-entry-data-design.md`) as the normative
> source; the coordinator (Claude) reviews the diff, runs the gates, and commits. Codex
> must not run git mutations.

**Goal:** Covers become rows in the remote entry's data (stable `cover_id` == old
subentry_id), subentries are removed by a staged crash-safe migration, and per-cover
management moves to the reconfigure menu — so the integrations page renders one device row
per entry.

**Architecture:** `RemoteConfig` carries the verbatim stored cover rows plus parsed
`CoverConfig`s; the cover platform and coordinator key everything by `cover_id` (identical
values to today's subentry_ids after migration); `async_migrate_entry` stages, cleans up
resumably, and commits VERSION 2 with an idempotent repair sweep at every v2 setup.

**Tech Stack:** Python 3.14, `uv`, Home Assistant 2026.7.2 (dev pin bumped from 2026.5.4),
pytest + mypy --strict + ruff, no suppressions of any kind.

## Global Constraints

- Spec is normative: `docs/superpowers/specs/2026-07-24-covers-in-entry-data-design.md`
  (Codex-attested SOUND AS WRITTEN). Deviations require coordinator sign-off.
- Gates after every task: `uv run ruff check --fix . && uv run ruff format .`,
  `uv run mypy --strict`, `uv run pytest`. Baseline 731 tests; only tests that construct
  subentries directly may change, converting to data covers.
- No `# noqa`, `# type: ignore`, or ruff config suppressions — fix root causes.
- Entity identity is sacred: entity `unique_id` == `cover_id` == old subentry_id. No task
  may change how an existing identity is derived.
- `tests/test_state_sync.py` stays byte-identical (standing guardrail).
- Strings: every new step needs `strings.json` + `translations/en.json` entries; cover
  name labels say "Cover name", not "Device name".

---

### Task 0: Dev-environment parity — HA 2026.7.2

**Files:**
- Modify: `pyproject.toml` (homeassistant dev pin `2026.5.4` → `2026.7.2`)
- Modify: `uv.lock` (via `uv sync`)

**Interfaces:** none — environment only.

- [ ] **Step 1:** `uv add --dev homeassistant==2026.7.2` (or edit the pin + `uv sync`).
- [ ] **Step 2:** Run all gates. Expected: 731 passed, mypy clean, ruff clean. Any
  breakage from the HA bump is fixed root-cause in this task (record what changed).
- [ ] **Step 3:** Verify the APIs the migration needs exist in the installed source:
  `grep -n "config_subentry_id: str | None | UndefinedType" .venv/lib/python3.14/site-packages/homeassistant/helpers/entity_registry.py`
  (expect a hit inside `async_update_entity`) and
  `grep -n "def async_clear_config_subentry" .venv/lib/python3.14/site-packages/homeassistant/helpers/device_registry.py`.
- [ ] **Step 4:** Commit `chore: bump dev HA pin to 2026.7.2 (prod parity)`.

---

### Task 1: Data model — cover rows in `RemoteConfig`

**Files:**
- Modify: `custom_components/zemismart_blinds/models.py` (CoverConfig ~294, RemoteConfig ~353)
- Modify: `custom_components/zemismart_blinds/const.py` (add `CONF_COVERS: Final = "covers"`,
  `CONF_COVER_ID: Final = "cover_id"`)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `CoverConfig` gains field `cover_id: str` (frozen dataclass field, first
  position after `name` is NOT required — append it; validation: non-empty stripped
  string). `CoverConfig.from_stored(cover_id: str, data: Mapping[str, object]) ->
  CoverConfig` replaces `from_subentry` (same parsing of name/channels/travel; the old
  name may remain as a thin alias ONLY if migration code in Task 3 wants it — otherwise
  delete it in Task 2 when the last caller goes).
- Produces: `RemoteConfig` gains `cover_rows: tuple[dict[str, object], ...]` — the stored
  rows verbatim (each a dict containing at least `cover_id`; unknown keys preserved) — and
  a derived `covers: tuple[CoverConfig, ...]` built in `__post_init__`.
  `RemoteConfig.from_entry` reads `data["covers"]` (default `()` — migration guarantees
  presence on v2 entries, but absence must not crash construction of a bare remote).
  `RemoteConfig.as_dict()` emits `CONF_COVERS: [dict(row) for row in cover_rows]`
  verbatim. Validation in `__post_init__`: duplicate `cover_id`s → `ValueError`;
  a row that fails `CoverConfig.from_stored` → `ValueError` naming the cover_id.
- Produces: `RemoteConfig.replace_cover_row(cover_id, merged_row)`,
  `RemoteConfig.add_cover_row(row)`, `RemoteConfig.remove_cover_row(cover_id)` are NOT
  added — flows manipulate plain dicts and rebuild via `from_entry`; YAGNI.

- [ ] **Step 1:** Write failing tests in `tests/test_models.py`:
  - `test_remote_config_round_trips_cover_rows_verbatim` — build entry data with two
    cover rows, one carrying an unknown key `"calibration_blob": "xyz"`; assert
    `RemoteConfig.from_entry(data).as_dict()["covers"]` equals the input rows exactly
    (order and unknown key included).
  - `test_remote_config_rejects_duplicate_cover_ids` — two rows, same cover_id →
    `pytest.raises(ValueError)` with the id in the message.
  - `test_remote_config_rejects_malformed_cover_row` — row missing channels →
    `ValueError` naming the cover_id.
  - `test_cover_config_requires_cover_id` — empty/whitespace cover_id → `ValueError`.
- [ ] **Step 2:** Run them; expected: FAIL (attribute/keyword errors).
- [ ] **Step 3:** Implement per the Interfaces block.
- [ ] **Step 4:** Full gates green (existing CoverConfig construction sites in tests gain
  cover_ids — mechanical).
- [ ] **Step 5:** Commit `feat(model): cover rows live in RemoteConfig with stable cover_ids`.

---

### Task 2: Platform + coordinator key on `cover_id`

**Files:**
- Modify: `custom_components/zemismart_blinds/cover.py` (async_setup_entry ~107–162;
  unique_id sites 204, 1123)
- Modify: `custom_components/zemismart_blinds/coordinator.py` (rename subentry_id →
  cover_id throughout; pure rename, values identical)
- Test: `tests/test_cover.py`, `tests/test_models.py` (coordinator cases live where they
  live today)

**Interfaces:**
- Consumes: `runtime.remote.covers: tuple[CoverConfig, ...]` (Task 1), each with
  `.cover_id`.
- Produces: `cover.py::async_setup_entry` iterates `runtime.remote.covers` — no
  `entry.subentries`, no `config_subentry_id=` argument to `async_add_entities`, no
  `CoverConfig.from_subentry`. Entity `unique_id` = `cover.cover_id` (same attribute
  positions as today's `subentry_id`). Coordinator public methods keep their shapes with
  the parameter renamed (`register_leaf(cover_id, entity)` etc.).

- [ ] **Step 1:** Write/convert failing tests: an entry whose DATA carries two covers
  (leaf + aggregate) produces entities with `unique_id == cover_id`, attached to the one
  remote device, and aggregate membership identical to today's subentry-built equivalent.
  Existing tests that build subentries convert to data covers (keep their assertions).
- [ ] **Step 2:** Run; expected: FAIL (setup still reads subentries).
- [ ] **Step 3:** Implement; delete `CoverConfig.from_subentry` if Task 3 does not need it
  (Task 3 builds rows from subentry `data` dicts directly, so delete it).
- [ ] **Step 4:** Full gates green.
- [ ] **Step 5:** Commit `feat(cover)!: entities build from entry-data covers keyed by cover_id`.

---

### Task 3: Staged migration + v2 setup repair

**Files:**
- Modify: `custom_components/zemismart_blinds/__init__.py` (add `async_migrate_entry`;
  repair sweep inside `async_setup_entry`; `ZemismartBlindsConfigFlow.VERSION` moves to 2
  in `config_flow.py`)
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: `CONF_COVERS`/`CONF_COVER_ID` (Task 1).
- Produces: `async def async_migrate_entry(hass, entry) -> bool` implementing spec
  Phases 0/A/B/C exactly:

```python
async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fold cover subentries into entry data (spec 2026-07-24, staged phases)."""
    if entry.version != 1:
        return True
    # Phase 0: legacy per-blind reference entries pass through byte-for-byte.
    if CONF_CHANNELS in entry.data:
        hass.config_entries.async_update_entry(entry, version=2)
        return True
    # Phase A: stage the covers list while subentries are still intact.
    if CONF_COVERS not in entry.data:
        covers = [
            {CONF_COVER_ID: subentry_id, CONF_NAME: subentry.title, **subentry.data}
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type == "cover"
        ]
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_COVERS: covers}
        )
    # Phase B: resumable cleanup driven by the STAGED list, never live subentries.
    ent_reg = er.async_get(hass)
    for row in entry.data[CONF_COVERS]:
        cover_id = row[CONF_COVER_ID]
        for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            if reg_entry.config_subentry_id == cover_id:
                ent_reg.async_update_entity(reg_entry.entity_id, config_subentry_id=None)
        if cover_id in entry.subentries:
            hass.config_entries.async_remove_subentry(entry, cover_id)
    # Phase C: commit.
    if entry.subentries:
        msg = f"unstaged subentries survived migration of {entry.entry_id}"
        raise ValueError(msg)  # fail-safe: externally corrupted staged data
    hass.config_entries.async_update_entry(entry, version=2)
    return True
```

  (Exact helper names verified against HA 2026.7.2; Codex adjusts only if the installed
  source disagrees, and reports it.)
- Produces: a repair sweep called from `async_setup_entry` for v2 non-legacy entries,
  idempotent: re-home any entity row of this entry with non-None `config_subentry_id`;
  for each device of this entry, collect non-None ids from
  `device.config_entries_subentries.get(entry.entry_id, set())` and call
  `dev_reg.async_clear_config_subentry(entry.entry_id, stale_id)`. Never
  `async_remove_subentry` here (spec: dirty-v2 entries may not hold the subentry).

- [ ] **Step 1:** Write failing tests:
  - `test_migration_folds_subentries_into_data_preserving_identity` — v1 entry, two cover
    subentries (one with an extra unknown data key), entity rows registered with
    unique_id == subentry_id and config_subentry_id set, one row disabled; after setup:
    data covers match (order, titles→names, unknown key), entity rows keep entity_id +
    unique_id + registry id, config_subentry_id is None, no subentries, VERSION 2.
  - `test_migration_is_idempotent_on_v2` — second setup changes nothing.
  - `test_migration_resumes_after_partial_cleanup` — craft the crash state: covers staged
    in data, ONE subentry already removed, its entity row already re-homed; re-run
    migration; converges to the same end state with the full covers list.
  - `test_migration_passes_legacy_entries_through_untouched` — CONF_CHANNELS entry:
    VERSION 2, data byte-identical, no covers key, setup still raises the existing
    ConfigEntryError.
  - `test_v2_setup_repair_converges_registry_skew` — v2 entry whose entity row still has
    a config_subentry_id and whose device still holds a stale subentry association;
    after setup both are cleared.
- [ ] **Step 2:** Run; expected: FAIL (no async_migrate_entry).
- [ ] **Step 3:** Implement (migration + VERSION 2 + repair sweep).
- [ ] **Step 4:** Full gates green.
- [ ] **Step 5:** Commit `feat(migration)!: staged v1→v2 covers migration with idempotent repair`.

---

### Task 4: Flows — wizard writes data covers; reconfigure manages them

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow.py` (delete `CoverSubentryFlow`
  ~626–697 and `async_get_supported_subentry_types` ~705–714; rework
  `async_step_finish` ~1131; extend the reconfigure menu ~728; add `cover_add`,
  `cover_pick_edit`, `cover_edit`, `cover_pick_remove`, `cover_remove_confirm` steps)
- Modify: `custom_components/zemismart_blinds/strings.json`,
  `custom_components/zemismart_blinds/translations/en.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `RemoteConfig.from_entry` round-trip (Task 1); `er.async_get_entity_id`
  ("cover", DOMAIN, cover_id) + `er.async_remove` for remove-cover.
- Produces: `async_step_finish` creates the entry with
  `data={**remote.as_dict(), CONF_COVERS: [...]}` (fresh `cover_id` =
  `homeassistant.util.ulid.ulid_now()` per cover) and NO `subentries` argument.
  Reconfigure menu options: `["reconfigure_learn", "reconfigure_edit", "cover_add",
  "cover_pick_edit", "cover_pick_remove"]`. Pickers are select fields whose option VALUE
  is `cover_id` and label is the cover name (duplicate names stay unambiguous). Edit
  merges the validated fields into a COPY of the stored row (unknown keys survive);
  remove refuses the last cover and any leaf an aggregate's channel set depends on
  (reuse `laminar_conflict`/`_sibling_channel_sets` machinery against the data rows),
  then deletes the registry row (missing row tolerated) and writes the data without it.
  All terminal steps use the non-reloading update helper so `_async_entry_updated`
  schedules exactly one reload.

- [ ] **Step 1:** Write failing tests:
  - `test_wizard_creates_entry_with_data_covers_and_no_subentries` (fresh distinct
    cover_ids; ULID shape).
  - `test_reconfigure_menu_adds_a_cover` (validated against existing channels).
  - `test_cover_edit_merges_and_preserves_unknown_keys_and_cover_id`.
  - `test_cover_remove_deletes_the_registry_row` (enabled + disabled + missing-row cases,
    parametrized).
  - `test_cover_remove_refuses_last_cover_and_aggregate_dependency`.
  - `test_pickers_disambiguate_duplicate_names_by_cover_id`.
  - `test_remote_settings_reconfigure_round_trips_covers_verbatim`.
- [ ] **Step 2:** Run; expected: FAIL.
- [ ] **Step 3:** Implement flows + strings ("Cover name" labels; learn_setup description
  reworded to the remote, not "this blind"; new step titles/descriptions/menu options and
  abort/error strings in BOTH strings.json and translations/en.json).
- [ ] **Step 4:** Full gates green.
- [ ] **Step 5:** Commit `feat(flow)!: reconfigure-menu cover management replaces subentry flows`.

---

### Task 5: Release gate

**Files:**
- Modify: `custom_components/zemismart_blinds/manifest.json` (`0.4.0` → `0.5.0`)
- Modify: `custom_components/zemismart_blinds/__init__.py` (docstring of
  `_async_entry_updated` drops its subentry clause)

- [ ] **Step 1:** Sweep: `grep -rn "subentr" custom_components/` — remaining hits must be
  migration/repair code and its comments only.
- [ ] **Step 2:** Full gates; record final test count (expect ≥ baseline + ~20 new).
- [ ] **Step 3:** Commit `chore: v0.5.0`; push branch `feat/covers-in-entry-data`; open PR
  (CI = lint/type, tests, hassfest, validate must all pass).

---

### Task 6: Production rollout (coordinator-executed, spec §Rollout)

- [ ] **Step 1:** Merge PR on green CI; cut GH release v0.5.0.
- [ ] **Step 2:** `ha core stop`; backup component + `core.config_entries` +
  `core.entity_registry` + `core.device_registry` to
  `/config/rollbacks/zemismart-consumer/<ts>/` (stores must be snapshotted stopped).
- [ ] **Step 3:** Deploy v0.5.0 from the tag; `ha core start`.
- [ ] **Step 4:** Verify: registry diff — zero entity deletions, identity fields
  byte-stable; entries at VERSION 2 with covers in data and zero subentries; integrations
  page renders entry → single device (screenshot-level check via entry/device/entity
  counts); one probe command through an audible bridge pair (air stats plan normally).
- [ ] **Step 5:** Report to owner with before/after evidence.

## Self-Review (done at authoring)

- Spec coverage: data model → T1; platform/coordinator → T2; migration phases 0/A/B/C +
  assert + repair → T3; flows incl. registry deletion, pickers, merges, refusals, strings
  → T4; version/dev-pin → T0/T5; rollout/backup → T6. Rollback path documented in spec.
- No placeholders; migration code shown; test names with concrete assertions.
- Type consistency: `cover_id: str` everywhere; `CONF_COVERS`/`CONF_COVER_ID` defined in
  T1 and consumed in T3/T4; `from_stored` introduced T1, `from_subentry` deleted T2.
