# Fleet-Wide Capture Listening Implementation Plan

**Goal:** The measure and Learn sniff sessions listen on every online bridge
at once (the way `state_sync` already does), deduplicate the same physical
press heard by several bridges, and make "Automatic" the fleet-wide default
with the bridge picker as an explicit single-bridge override.

**Spec:** `docs/claude/specs/2026-08-07-fleet-wide-capture-listen-design.md`
**Issue:** #57

## Global Constraints

- `uv` only, never pip. Gate for every task:
  `uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest`.
- Never disable a linter rule (`# noqa` / `# type: ignore` forbidden).
- `except (A, B):` at py314 is rewritten by `ruff format` — use named
  exception tuples or two blocks.
- Test frames are synthesized through the codec, never pasted house captures.
- Behavior-preserving outside the capture/learn flows: cover-control TX and
  `state_sync` untouched.
- Mutation check: every new/changed test shown red with the fix reverted.

## Tasks

### Task 1: Bridge-aware timing in the pure machine

Files: `travel_capture.py`, `tests/test_travel_capture.py`

- `TimedPress` gains `bridge_id: str | None = None`.
- `interval_seconds` uses the bridge clock only when both presses carry the
  same `bridge_id` (both `None` counts as same); otherwise monotonic.
- `TravelRun` gains `starts: dict[str, TimedPress]` per-bridge stamps of the
  current press; `offer_payload` gains a `bridge_id: str | None = None`
  parameter. `_open` absorbs same-direction copies inside the burst window
  while stamping each new bridge once; a restart clears the stamps. `_close`
  prefers the STOP bridge's own stamp; else the earliest start (which then
  falls to monotonic via the `bridge_id` mismatch).
- Tests: two-bridge dedup, cross-bridge monotonic fallback (equal boots!),
  same-bridge preference, restart clears stamps, post-close STOP ignored.

### Task 2: Fleet-wide measure session

Files: `config_flow.py`

- New `_SniffChannel` dataclass (bridge_id, owner_key, command_topic,
  unsubscribe, holder). `_MeasureSession` replaces its four one-bridge
  fields with `channels: list[_SniffChannel]`.
- `_PendingMeasure.bridge: str | None` → `bridges: tuple[str, ...] = ()`.
- `_handle_travel_message` gains `bridge_id`, passed to `offer_payload`;
  own-emission guard stays ahead of the run on every bridge.
- `_async_hold_sniff_open` tolerates publish failure per iteration.
- `_async_measure_arm` claims/subscribes/holds per bridge, skipping owned
  bridges, failing only when none claim. `_async_measure_session_close`
  tears down every channel with the existing stop-task/release discipline.
- `cover_measure_setup`: Automatic → `BridgeRegistry.online_bridge_ids()`
  (new method); explicit → that bridge. Delete `_measure_area_id`.

### Task 3: Fleet-wide Learn sniff

Files: `config_flow.py`, `bridge_registry.py`

- `_learn_bridge: str | None` → `_learn_bridges: tuple[str, ...] = ()`.
- `learn_setup` resolves Automatic to the online set and stores the raw
  picker value in `_learn_suggested`.
- `_async_capture` claims, subscribes, and arms every learn bridge; the
  finally block stops and releases each.

### Task 4: Strings

Files: `strings.json`, `translations/en.json`

- `learn_setup.data_description.bridge`, `cover_measure_setup.description`,
  `progress.sniffing` / `progress.measuring`: Automatic = every online
  bridge, named bridge = single-bridge override. `{bridge}` placeholder now
  carries the joined listening set.

### Task 5: Flow tests + mutation check

Files: `tests/test_config_flow.py`

- Update publication counts / subscription indexing in existing measure and
  Learn tests that used Automatic (FakeMqtt has two online bridges, so
  Automatic now arms both).
- New: Automatic arms all bridges; duplicate press across two bridges is one
  measurement; bridge-silent-mid-run completes via the other bridge;
  explicit override arms exactly one; Learn captures off the second bridge.
- Run each new test with the implementation reverted to confirm red.

## Status

Implemented on `feat/fleet-wide-capture-listen`; see PR for gate output and
mutation-check evidence.
