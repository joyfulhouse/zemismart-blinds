# Issue #29 Monotonic Timing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every process-local timing decision immune to wall-clock steps while preserving the wall timestamps persisted by `RestoreEntity`.

**Architecture:** The hub stamps command starts and received RF events in both wall and monotonic domains. Explicitly named monotonic fields feed the ledger, state-sync classifier, takeover disarms, and cover motion model; wall companions feed only persisted state. Restore is the deliberate boundary: it interprets the persisted wall pair once and immediately projects the remaining duration onto a fresh monotonic deadline.

**Tech Stack:** Python 3.13, Home Assistant custom integration APIs, asyncio, pytest, uv, ruff, mypy strict.

## Global Constraints

- `_ATTR_MOTION_STARTED` and `_ATTR_MOTION_DEADLINE` remain wall-clock values and their public output is unchanged.
- No live comparison mixes wall and monotonic timestamps.
- `disarm_deadline_monotonic` remains monotonic from cover producer through hub and ledger consumers.
- `BridgeClock` keeps its 30-second plausibility clamp on the monotonic projection axis.
- Use `time.monotonic()` for live timing and never `asyncio.get_event_loop()`.
- Use only `uv`; never silence lint or type errors.
- Run `uv run ruff check --fix .`, `uv run ruff format .`, `uv run mypy --strict .`, and `uv run pytest -q`.
- Commit the finished work on `harden/29-monotonic`.

---

### Task 1: Dual-domain timing contracts

**Files:**
- Modify: `custom_components/zemismart_blinds/models.py`
- Modify: `custom_components/zemismart_blinds/state_sync.py`
- Test: `tests/test_models.py`
- Test: `tests/test_state_sync.py`

**Interfaces:**
- Consumes: injected `now: Clock` for wall timestamps and `monotonic_now: Clock` for live timestamps.
- Produces: `CommandAck.started_at`, `CommandAck.deadline`, `CommandAck.started_at_monotonic`, `CommandAck.deadline_monotonic`; `HeardEvent.heard_at` and `HeardEvent.heard_at_monotonic`; monotonic `CommandLedger` and `StateSyncConsumer` inputs.

- [ ] **Step 1: Write the failing confirmed-echo test**

Add a parameterized hub-level test with wall and monotonic clocks starting together. Confirm one command, step only wall time by `-3600` and `+3600`, receive the command's own frame while monotonic advances normally, and assert no `HeardEvent` is dispatched.

- [ ] **Step 2: Run the test to verify RED**

Run: `uv run pytest -q tests/test_models.py -k wall_step_confirmed_echo`

Expected: both parameter cases fail because the ledger window and RX projection follow the stepped wall clock.

- [ ] **Step 3: Add explicit dual stamps and monotonic live classification**

Introduce private status/start carriers holding both domains. Keep wall fields on public events and acknowledgements, add explicitly suffixed monotonic fields, move `BridgeClock`, all ledger windows, state-sync cache TTLs, holds, debounce, commanded starts, displaced/emission memories, and takeover queries to `monotonic_now`.

- [ ] **Step 4: Run focused model/state-sync tests**

Run: `uv run pytest -q tests/test_models.py tests/test_state_sync.py`

Expected: all focused tests pass after fixtures and expected event values supply both timestamps.

### Task 2: Monotonic cover motion with unchanged persistence

**Files:**
- Modify: `custom_components/zemismart_blinds/cover.py`
- Test: `tests/test_cover.py`

**Interfaces:**
- Consumes: dual-domain `CommandAck` and `HeardEvent`.
- Produces: `_motion_started_monotonic` and `_motion_deadline_monotonic` for live integration; `_motion_started_wall` and `_motion_deadline_wall` for `extra_state_attributes`.

- [ ] **Step 1: Write failing live partial/full wall-step tests**

Add two tests parameterized by `-3600` and `+3600`. Start a real acknowledged partial move and a real acknowledged full travel, step only `WALL_CLOCK`, and assert completion follows elapsed monotonic duration. The full-travel test must additionally assert `position_confidence` stays unanchored before physical completion and becomes `anchored` only afterward.

- [ ] **Step 2: Run the tests to verify RED**

Run: `uv run pytest -q tests/test_cover.py -k 'wall_step and (partial or full)'`

Expected: a forward step completes early and a backward step misses the real completion deadline.

- [ ] **Step 3: Convert every cover live call site**

Add `MONOTONIC_CLOCK = time.monotonic`; make `_estimated_position`, `_sync_position`, `_interrupt_motion`, `_apply_stop`, `_async_track_motion`, member motion, displacement handling, partial-position snapshots, and both takeover implementations consume explicitly named monotonic values. Derive the persisted wall deadline independently from the wall start and duration so emitted attributes retain their existing values.

- [ ] **Step 4: Run all cover tests**

Run: `uv run pytest -q tests/test_cover.py`

Expected: all cover tests pass after direct private-field assertions and synthetic `HeardEvent` helpers are updated to the explicit clock domain.

### Task 3: Restore boundary conversion

**Files:**
- Modify: `custom_components/zemismart_blinds/cover.py`
- Test: `tests/test_cover.py`

**Interfaces:**
- Consumes: persisted wall `motion_started` and `motion_deadline`, current `WALL_CLOCK()`, and current `MONOTONIC_CLOCK()`.
- Produces: one monotonic start/deadline pair plus the unchanged persisted wall pair.

- [ ] **Step 1: Write the failing restore projection test**

Persist a live motion, simulate restart with distinct wall and monotonic epochs, step wall by `-2` and `+2` seconds before restore, and assert the restored monotonic deadline is exactly `MONOTONIC_CLOCK() + (persisted_deadline - WALL_CLOCK())`; assert emitted wall attributes are unchanged.

- [ ] **Step 2: Run the test to verify RED**

Run: `uv run pytest -q tests/test_cover.py -k wall_step_restore`

Expected: the old wall-domain live fields do not contain the required monotonic projection.

- [ ] **Step 3: Implement the single sanctioned boundary**

Validate the persisted wall pair, determine expiry on the wall axis, compute remaining wall duration once, then map that duration to the current monotonic instant. Map a valid persisted origin onto the same monotonic axis for progress integration. Add a WHY comment explaining that reboot resets monotonic time and no later live comparison may return to the wall pair.

- [ ] **Step 4: Run restore and cover tests**

Run: `uv run pytest -q tests/test_cover.py`

Expected: all restore and live cover behavior passes.

### Task 4: Negative controls, gates, and commit

**Files:**
- Verify: `custom_components/zemismart_blinds/cover.py`
- Verify: `custom_components/zemismart_blinds/models.py`
- Verify: `custom_components/zemismart_blinds/state_sync.py`
- Verify: `tests/test_cover.py`
- Verify: `tests/test_models.py`
- Verify: `tests/test_state_sync.py`

**Interfaces:**
- Consumes: the four regression tests and their specific production changes.
- Produces: recorded failing negative-control output, clean quality gates, and one branch commit.

- [ ] **Step 1: Negative-control live cover timing**

Temporarily route the cover live clock back to wall time. Run the partial and full wall-step tests separately and record that both forward/backward cases fail. Restore the monotonic clock production code.

- [ ] **Step 2: Negative-control echo classification**

Temporarily confirm ledger windows with the wall start instead of `started_at_monotonic`. Run the confirmed-echo wall-step test and record that both direction cases dispatch the echo as a press. Restore the monotonic confirmation.

- [ ] **Step 3: Negative-control restore conversion**

Temporarily assign the persisted wall deadline directly as the live deadline instead of projecting remaining duration onto `MONOTONIC_CLOCK()`. Run the restore test and record that both direction cases fail. Restore the boundary conversion.

- [ ] **Step 4: Run all required gates**

Run:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy --strict .
uv run pytest -q
```

Expected: every command exits zero and the full suite reports no failures.

- [ ] **Step 5: Inspect and commit**

Run `git diff --check`, inspect `git diff` and `git status --short`, then commit only issue #29 files with a message such as `fix: use monotonic time for live blind state`.
