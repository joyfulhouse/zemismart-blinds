# Fleet-Wide Capture Listening Implementation Plan

**Goal:** The measure and Learn sniff sessions listen on every online bridge
at once (the way `state_sync` already does), deduplicate the same physical
press heard by several bridges, and make "Automatic" the fleet-wide default
with the bridge picker as an explicit single-bridge override.

**Spec:** `docs/superpowers/specs/2026-08-07-fleet-wide-capture-listen-design.md`
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
- New `press_signature(prefix, remote_id, channels, button)` returning the
  same `FrameSignature` shape `state_sync` debounces on; `classify_frame`
  returns it alongside the button.
- `TravelRun` gains `recent: dict[FrameSignature, float]` and `_is_repeat`,
  a SLIDING window (each copy re-stamps) applied BEFORE `_open`/`_close`, so
  those two see only genuinely new presses. `offer_payload` gains a
  `bridge_id: str | None = None` parameter. No per-bridge stamp map: its
  precision was below measurement granularity.
- Tests: fleet-wide dedup, the spread-copy short-travel regression, a fast
  STOP not swallowed, signature separation, cross-bridge monotonic fallback
  (equal boots!), post-close STOP ignored.

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
  bridge, named bridge = single-bridge override. The `{bridge}` placeholder
  carries the joined listening set on the screens that describe LISTENING.
- `learn_confirm` is the exception and must not: "learned ... through
  {bridge}" is a claim about which bridge HEARD the remote, so it receives
  the bridges the captures actually arrived on. Feeding it the armed set
  would name bridges that heard nothing.
- New failure screens `learn_busy`, `learn_ambiguous`, `cover_measure_busy`.

### Task 5: Flow tests + mutation check

Files: `tests/test_config_flow.py`

- Update publication counts / subscription indexing in existing measure and
  Learn tests that used Automatic (FakeMqtt has two online bridges, so
  Automatic now arms both).
- New: Automatic arms all bridges; duplicate press across two bridges is one
  measurement; bridge-silent-mid-run completes via the other bridge;
  explicit override arms exactly one; Learn captures off the second bridge.
- Run each new test with the implementation reverted to confirm red.

### Task 6: Review round (added after the first tribunal pass)

Files: `travel_capture.py`, `config_flow.py`, `learn_session.py`, strings,
`tests/conftest.py`

- Dedup by signature ahead of the run (Task 1 above, rewritten).
- Learn first-capture trust boundary: settle, then refuse to adopt when a
  second remote was heard; withdraw the measure screen's one-click adopt when
  several foreign remotes were heard.
- Claim conflicts reported as `bridge_busy` rather than as silence on air.
- Bounded arm fan-out; bounded re-arm retry on a narrow `HomeAssistantError`.
- Teardown assertion that every test released its bridge claims.

### Task 7: Second review round

Files: `travel_capture.py`, `config_flow.py`, `learn_session.py`, strings,
`tests/test_travel_capture.py`, `tests/test_config_flow.py`

Each round-6 fix had a second code path it did not cover:

- The trust boundary now also gates the unrecognised-opcode TIMEOUT path, and
  its settle sleeps only the remainder of the window the winner has not
  already outlasted.
- `TravelRun._open` refuses to re-anchor on the opening signature with no
  window at all; the repeat filter no longer gates a press that would OPEN a
  run. `_RECENT_CAP` deleted as unreachable.
- Competing presses are collected around the winner (settle window, inclusive)
  rather than across the whole 30 s listen, and keyed by the full
  `press_signature` rather than by remote id.
- The fleet claim is atomic: any held bridge refuses the whole claim as busy,
  naming the held bridges.
- The arm fan-out is a semaphore-bounded `gather(return_exceptions=True)`
  under one shared absolute deadline; a bridge that raises or misses it is
  skipped, and only subscribed bridges get a re-arm hold.
- The re-arm `except` goes back to broad, keeping the bounded retry.
- The mismatch screen diagnoses this device's own remote wherever it arrived,
  and `cover_measure_use_heard` re-makes the qualification itself.
- The claim-release teardown fixture moves from `tests/conftest.py` into
  `tests/test_config_flow.py` (write scope).

## Status

Implemented on `feat/fleet-wide-capture-listen`; see PR for gate output and
mutation-check evidence.
