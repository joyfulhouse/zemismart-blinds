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

- `learn_setup.data_description.bridge` and `cover_measure_setup.description`
  are rewritten: Automatic = every online bridge, named bridge = single-bridge
  override.
- `progress.sniffing` / `progress.measuring` are NOT edited (corrected after
  review round 3, which caught this task claiming an edit the diff does not
  contain). What changes there is the VALUE of `{bridge}`, which now carries the
  joined listening set. Both strings already place the placeholder after "on"
  -- "Capturing on {bridge}", "Listening on {bridge}" -- so they read correctly
  for one bridge and for a comma-joined list alike, and so do the other three
  `{bridge}` screens (`learn_confirm`, `learn_busy`, `cover_measure_busy`).
  A string of the form "the bridge {bridge}" would have needed rewording; none
  exists.
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

### Task 8: Third review round

Files: `travel_capture.py`, `config_flow.py`, `learn_session.py`,
`bridge_registry.py`, `tests/test_travel_capture.py`,
`tests/test_config_flow.py`, spec

Each round-7 guard protected one instance of something there were several of:

- `TravelRun` remembers EVERY signature that has anchored the run, not just the
  current one, and clears them on close. A run restarted on UP was otherwise
  re-anchored by a lagging copy of the DOWN burst it replaced — wrong direction
  and wrong length.
- The learn sniff window is armed for the capture timeout PLUS the settle
  (`_LEARN_SNIFF_WINDOW_SECONDS`); the learn path arms each bridge once, so a
  settle past the window listened on bridges that had stopped sniffing.
- `_stamp_candidate` prunes presses that can no longer compete before applying
  `_LEARN_CANDIDATE_CAP`, and a press it still had no room for sets
  `overflowed`, which refuses the capture. The cap bounds naming only.
- Mismatch rows dedup by signature, not by raw frame: bridges report their own
  pulse widths, so one press was filling `_HEARD_CAP`.
- The arm fan-out's shared deadline wraps the semaphore acquisition too.
- Candidate signatures use the button the frame IS (untabled falls back to the
  solicited action); what COMPETES is compared on remote and channel set, so a
  remote's other button does not veto its own capture.
- The post-close STOP test is replaced with one that exercises the gate it was
  written for: a stale STOP copy arriving in a re-opened run.
- Not changed, but now locked by test: the held candidate is already first-wins;
  `_async_subscribe_ready` already unsubscribes on cancellation before
  readiness; `online_bridge_ids` already returned sorted order (now promised
  locally rather than inherited from `bridges`).

Round-3 addendum, all documentation (no code, no strings):

- Task 4 above corrected: the progress strings were never edited, and did not
  need to be — every `{bridge}` string reads "…on {bridge}", which takes a
  comma-joined list unchanged.
- The design's dedup section gains a **residual-risk** subsection: opening
  presses get unconditional signature protection while STOP closes rely on the
  sliding window, so a STOP copy lagging more than the burst window can close a
  re-opened run. Accepted, because honouring a late STOP is the safe default;
  narrowed by `MIN_MEASURED_SECONDS` and by the screens that print the seconds
  before anything is saved.
- The arm fan-out's semaphore is KEPT; Bounds records why (the fan-out is as
  wide as the 256-entry discovery snapshot, not as the house's bridge count).

## Status

Implemented on `feat/fleet-wide-capture-listen`; see PR for gate output and
mutation-check evidence.
