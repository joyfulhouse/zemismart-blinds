# Remote-Centric Redesign — Implementation Roadmap

> **For agentic workers:** This roadmap decomposes the redesign into four
> sequential plans. Implement them in order. Each plan produces working,
> testable software and a review checkpoint. Later plans are written **against
> the real code the previous phase produced**, not against speculative types —
> so plans 02–04 are authored only after their predecessor lands and is green.

**Spec:** `docs/superpowers/specs/2026-07-17-remote-centric-redesign-design.md`
(rev 3, committed `912f996`).

**Branch:** `feat/remote-centric-model`, worktree `.worktrees/remote-centric`,
rebased onto `main@d03ce0f` (state-sync baseline present).

**Implementer:** Codex GPT-5.6-sol, task-by-task TDD.

## Why phased, not one monolith

The redesign rewrites the config/device/entity model across `models.py`,
`config_flow.py`, `cover.py`, `__init__.py`, `strings.json`, and adds a
coordinator. The layers are tightly coupled: later-phase code consumes types
that earlier phases define. Writing exact code for Phase C before Phase A's
types exist would be guesswork. Phasing keeps every step's code real and lets a
human (and Codex) gate each layer before the next builds on it.

## Non-breaking strategy

Each phase keeps the suite green. New types are added alongside old ones; the
old single-entry path keeps loading the integration until the phase that
migrates its consumers removes it. `test_state_sync.py` must pass **unmodified**
at every checkpoint (spec guardrail).

## The four plans

### Plan 01 — Data-model foundation (pure Python, no HA) — DETAILED, ready

`docs/superpowers/plans/2026-07-17-remote-centric-01-data-model.md`

New pure types added to `models.py` without touching any consumer:
`Role`, `CoverConfig`, `RemoteConfig`, laminar channel-set validation, and role
& member derivation (`derive_role`, `member_covers`). Fully unit-tested in
`test_models.py`. Deliverable: `uv run pytest tests/test_models.py` green;
integration still loads via the untouched legacy path; `test_state_sync.py`
unmodified.

> **Scope correction (during execution):** the derived-`BlindConfig`
> runtime type (`BlindConfig.derive`, `role` field, optional aggregate-only
> travel, `is_aggregate`) was **moved out of Plan 01 into Plan 03**. Retyping
> `BlindConfig.travel_*` to `float | None` breaks `mypy --strict` on the
> unchanged `cover.py`, so that change must land with its `cover.py` consumer
> in Plan 03 to keep the package type-clean. `BlindConfig` is untouched in
> Plan 01.

### Plan 02a — Wizard & runtime shim — DETAILED, ready

`docs/superpowers/plans/2026-07-17-remote-centric-02a-wizard.md`

Rewrites the creation flow: learn/manual/virtual → remote settings → cover
loop → `async_create_entry(subentries=[...])` with final whole-list
laminar+uniqueness validation and the explicit duplicate-remote abort guard.
Deletes the options flow, reuse path, old reconfigure,
`_propagate_calibration`, `_cross_area_overlap`. Adds `RemoteRuntime` and the
dual-format loader in `__init__.py`/`cover.py` (remote entries load with zero
entities until Plan 03). Deliverable: wizard tests green, full suite green.
**Mid-branch gap (restored in 02b):** no reconfigure/options UI.

### Plan 02b — Subentry flows, entry reconfigure, strings

Registers `async_get_supported_subentry_types` → `{"cover": CoverSubentryFlow}`
(add/reconfigure with hidden-travel-key carry-forward and current-role
validation; native delete). New entry reconfigure (relearn with explicit
collision scan; edit settings incl. calibration fields). Rewrites
`strings.json`/`translations/en.json` for the new step topology
(`remote_settings`, `cover`, `cover_menu`, subentry section, new errors
`travel_required`/`overlapping_channels`/`duplicate_channels`/
`channel_conflict`). Interim: entry reconfigure uses
`async_update_reload_and_abort` until Plan 04 installs the update listener as
sole reload owner. Deliverable: `test_config_flow.py` green for subentry and
reconfigure paths.

> **Rev-4 simplification (user directive, applies from Plan 03):** legacy
> per-blind entries stop loading — `async_setup_entry` raises
> `ConfigEntryError` for old-shape data (`channels` present), keeping the
> entry+data as the manual-migration reference. The dual-format shim
> (`EntryRuntime` legacy path, `effective_values`, `_entry_config`, legacy
> entity setup) is deleted in Plan 03 alongside the cover.py rebuild. Panel
> FIX-2 (cross-format identity uniqueness) is REVERTED: inert legacy entries
> must not block onboarding their replacement remote.

### Plan 03 — Entities, coordinator, device topology

Adds the derived-`BlindConfig` runtime type first (`BlindConfig.derive`,
`role` field, optional aggregate-only travel, `is_aggregate` — moved here from
Plan 01 so it lands with its consumer). Then adds `coordinator.py` (per-entry:
cover index, role/membership map, press-ownership arbitration, forward/reverse
notification, batched recomputation). Migrates `cover.py`: leaf entity keeps
today's model but unique_id/device become subentry-based; new aggregate entity
class (state from
available members, single-frame open/close/stop + member model start/freeze,
concurrent typed set_position fan-out with STOP preemption). Parent-device-
first creation; per-subentry `async_add_entities(config_subentry_id=...)`;
area via `async_update_device` on first creation only. Deliverable:
`test_cover.py` green including aggregate math, fan-out, press ownership.

### Plan 04 — Lifecycle: reload owner, entry-scoped drain, relearn disarm

`__init__.py`: single reload owner via
`entry.async_on_unload(entry.add_update_listener(...))`; per-subentry entity
setup; entry-scoped queued-command drain on unload (owner token on
`_QueuedCommand`, awaited selective drain in the hub); relearn awaited
bridge-disarm of live timed commands before reload completes. Deliverable:
`test_init.py` lifecycle tests green; full suite green; `test_state_sync.py`
unmodified.

## Manual, no-code step (not a plan)

The deployment/runbook (spec §"Deployment runbook") is executed by Claude on the
live HA at rollout after Plan 04 merges. It is operational, not implementation.

## Execution order & checkpoints

```
Plan 01 → review → Plan 02 → review → Plan 03 → review → Plan 04 → review → rollout
```

After each plan is green and reviewed, author the next plan grounded in the
just-written code, then execute it.
