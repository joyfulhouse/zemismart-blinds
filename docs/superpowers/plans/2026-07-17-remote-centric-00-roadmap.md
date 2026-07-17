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
`Role`, `CoverConfig`, `RemoteConfig`, laminar channel-set validation, role &
member derivation, and `BlindConfig.derive(...)` with an explicit `role` and
optional (aggregate-only) travel times. Fully unit-tested in `test_models.py`.
Deliverable: `uv run pytest tests/test_models.py` green; integration still
loads via the untouched legacy path; `test_state_sync.py` unmodified.

### Plan 02 — Config flow: wizard, subentry flows, entry reconfigure

Rewrites `config_flow.py` onto the new types and `strings.json`/`translations`
for the new steps. Registers `async_get_supported_subentry_types` →
`{"cover": CoverSubentryFlow}`. Wizard (learn/manual/virtual → remote settings
→ cover loop → `async_create_entry(subentries=[...])` with final whole-list
laminar+uniqueness validation). Subentry add/reconfigure/delete
(`async_update_and_abort`, hidden-travel-key carry-forward, current-role
validation). Entry reconfigure (relearn with collision scan; edit settings).
Deletes the options flow, `_propagate_calibration`, `_cross_area_overlap`,
reuse path. Deliverable: `test_config_flow.py` green for entry+subentry
creation and all rejection paths.

### Plan 03 — Entities, coordinator, device topology

Adds `coordinator.py` (per-entry: cover index, role/membership map,
press-ownership arbitration, forward/reverse notification, batched
recomputation). Migrates `cover.py`: leaf entity keeps today's model but
unique_id/device become subentry-based; new aggregate entity class (state from
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
