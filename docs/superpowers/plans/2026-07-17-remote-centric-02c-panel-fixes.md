# Plan 02c — Four-Way Panel Review Fixes

> Fix round from the phase-gate adversarial review of Plans 01+02a+02b
> (panel: Fable 5, Codex GPT-5.6-sol ultra, Gemini 3.1 Pro, Grok 4.5).
> Amended for spec rev 4: FIX-2 dropped (legacy entries become inert).
> Implementer: Codex GPT-5.6-sol xhigh. Controller commits.
> Files in scope: `custom_components/zemismart_blinds/{config_flow.py,strings.json,translations/en.json}`,
> `tests/test_config_flow.py`. `tests/test_state_sync.py` untouched.

Each fix lands with its test (TDD where practical). Baseline: 627 green.

## FIX-1 (High): Gate legacy entries out of remote-only management

- `async_get_supported_subentry_types`: return `{}` when `CONF_CHANNELS in config_entry.data` (legacy), else `{"cover": CoverSubentryFlow}`.
- `async_step_reconfigure`: first line — if `CONF_CHANNELS in self._get_reconfigure_entry().data`: `return self.async_abort(reason="legacy_not_supported")`.
- strings: add `config.abort.legacy_not_supported`: "This entry uses the old per-blind format. Delete it and add its remote again instead of reconfiguring." (both files).
- Tests: legacy-format entry (build `ConfigEntry` with `BlindConfig(...).as_dict()` data as existing tests do) — (a) reconfigure init returns ABORT `legacy_not_supported`; (b) `ZemismartBlindsConfigFlow.async_get_supported_subentry_types(entry)` == `{}` for it and == `{"cover": CoverSubentryFlow}` for a remote entry.

## FIX-2: DROPPED (spec rev 4)

Rev 4 makes legacy entries inert (`ConfigEntryError` at setup, Plan 03), so a
legacy entry holding the same RF identity is harmless — and blocking it would
break the migration order (onboard the replacement remote FIRST, delete the
legacy entries after). Uniqueness stays remote-format-only via the existing
unique_id guards. Do not implement any cross-format identity scan.

## FIX-3 (High): Source-aware learn-failure menus

- `async_step_learn_timeout` and `async_step_learn_unavailable`: when
  `self.source == config_entries.SOURCE_RECONFIGURE`, drop `"advanced"` from
  `menu_options` (timeout → `["learn_retry"]`, unavailable → `["learn_setup"]`).
- Test: reconfigure → relearn with `FakeMqtt(bridges={})` (no online bridges) →
  `learn_unavailable` menu shows no `advanced`. (Timeout path: same assertion via
  the existing timeout machinery if cheap; otherwise the unavailable test suffices —
  both share the gating helper. Implement the gate as one small helper used by both.)

## FIX-4 (Medium): Subentry reconfigure prefill and redisplay

- In `CoverSubentryFlow.async_step_reconfigure`, build display values instead of
  passing raw storage: on first display, `suggested = {CONF_NAME: <stored name>,
  CONF_CHANNELS: ",".join(str(c) for c in CoverConfig.from_subentry(subentry.data).channels),
  CONF_TRAVEL_UP/DOWN: stored values when non-empty}`; if stored data is unparseable,
  fall back to `{CONF_NAME: subentry.title}` with best-effort channels text.
  On error redisplay, `suggested = user_input` (typed values preserved).
- Apply the suggested mapping with `self.add_suggested_values_to_schema(_cover_schema(None), suggested)`
  so travel values PREFILL the UI without becoming schema defaults (clearing a
  suggested optional field still omits the key on submit — the travel_required
  check and carry-forward semantics keep working; schema({}) in tests still
  omits travel).
- Wizard `async_step_cover` error redisplay: same `add_suggested_values_to_schema`
  treatment for `user_input` so typed travel is not dropped (first display keeps
  the existing capture-channel prefill behavior).
- Tests: (a) reconfigure a cover and submit the schema-processed defaults with
  ONLY the name changed (channels untouched from prefill) — succeeds, channels
  unchanged, travel carried; (b) the suggested description of the first-display
  schema contains the comma-form channels (assert via schema serialization:
  iterate `schema.schema` markers and check `marker.description["suggested_value"]`).

## FIX-5 (Medium): Laminar validation must not skip malformed siblings

- Change `_validate_cover_input`'s second parameter to
  `existing: list[tuple[int, ...]]` (channel sets). Wizard call sites pass
  `[c.channels for c in self._covers]`; born_aggregate check uses the same list.
- Replace `_sibling_covers` usage in validation with a new
  `_sibling_channel_sets(entry, *, exclude_subentry_id=None) -> list[tuple[int, ...]]`:
  for each cover subentry, try `CoverConfig.from_subentry(...).channels`; on
  failure, fall back to `parse_channels(subentry.data.get(CONF_CHANNELS, ""))`;
  if even that fails, **fail closed**: raise `ValueError` — the flow maps it to
  a form error `{"base": "invalid_config"}` (a corrupted sibling must block
  mutations, not silently vanish from validation). Keep `_sibling_covers` only
  if still used elsewhere; otherwise delete it.
- Tests: entry with a manually corrupted subentry (`hass.config_entries.async_update_subentry(entry, sub, data={CONF_NAME: "x", CONF_CHANNELS: "not-a-channel"})`
  — use the real API or construct the entry with such subentry data): subentry
  add of any set → form error `invalid_config` (fail closed). And: a sibling
  whose channels parse but whose travel is malformed still participates in
  laminar rejection (add `{2,3}` against stored `{1,2}` with travel garbage →
  `overlapping_channels`).

## FIX-6 (Low): Clear stale capture when leaving Learn for Advanced

- In `async_step_advanced` (entered from learn_confirm or failure menus): set
  `self._capture = None` and `self._sniff_session_id = None`.
- Test: drive learn to `learn_confirm` (capture channels `1,2`), choose
  `advanced` → `manual`, submit identity, reach `remote_settings` → cover step;
  assert the first cover form's channels default is "" (no stale `1,2` prefill).

## FIX-7 (Low): Subentry abort strings

- strings + translations: add under `config_subentries.cover`:
  `"abort": {"reconfigure_successful": "The cover was reconfigured successfully.", "already_configured": "Another cover of this remote already uses exactly these channels."}`.

## FIX-8 (Low): Copy refresh

- `config.step.user.title` → "Add a Zemismart remote"; description → "Learn the
  remote automatically, or use an Advanced setup method. You will add its
  covers (blinds and groups) next."
- `config.error.already_configured` → "This remote is already configured by
  another entry."

## FIX-9 (Medium): Relearn honors edited name/area

- `async_step_reconfigure_apply`: when `self._learn_name`/`self._learn_area_id`
  are set (the relearn setup form collected them), use them for `updated`'s
  `name`/`area_id` instead of `current`'s. Transport settings still come from
  `current`.
- Update `test_reconfigure_relearn_applies_new_identity_and_collides`: it
  submits `schema({})` (prefilled name/area) so assertions keep passing; add an
  edited-name variant assertion in the same test (change the submitted name to
  "Renamed remote" and assert `updated.name == "Renamed remote"` and entry
  title updated).

## Definition of done

- All new tests green; full `uv run pytest -q` green;
  `uv run mypy --strict custom_components/zemismart_blinds/` clean;
  `uv run ruff check custom_components/zemismart_blinds/ tests/` clean;
  strings/translations byte-identical valid JSON;
  `git diff --stat main -- tests/test_state_sync.py` empty.

## Deferred with roadmap note (not in this round)

- Positive format marker for the dual-format discriminator (Codex M#5):
  unreachable through shipped flows; revisit in Plan 04 if a `format` key is
  added to remote entries.
- Role-aware hiding of travel fields on the reconfigure form (Codex L#7):
  suggested-values prefill (FIX-4) addresses the practical confusion.
- Gemini's lifecycle blockers: Plan 04 scope (update listener, entry-scoped
  drain, relearn bridge-disarm).
