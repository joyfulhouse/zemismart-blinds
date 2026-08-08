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
  (Superseded in Task 7: the filter no longer gates `_open` at all — a press
  with no run open cannot shorten one — and the anchor is protected by
  `anchored` instead.)
- Tests: fleet-wide dedup, the spread-copy short-travel regression, a fast
  STOP not swallowed, signature separation, cross-bridge monotonic fallback
  (equal boots!), post-close STOP ignored (superseded in Task 8: that test could
  not fail, and is now the stale-STOP-into-a-reopened-run case).

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
  (Superseded in Task 10: one independent task publishes every stop and
  releases every claim from its own `finally`, per-bridge stop tasks and their
  done-callbacks deleted. Task 8 also made the claim atomic, so no bridge is
  skipped.)
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
  (Both narrowed later: Task 7 widened the re-arm `except` back to broad, keeping
  the bounded retry, and Task 10 deleted the fan-out bound.)
- Teardown assertion that every test released its bridge claims.

### Task 7: Second review round

Files: `travel_capture.py`, `config_flow.py`, `learn_session.py`, strings,
`tests/test_travel_capture.py`, `tests/test_config_flow.py`

Each Task-6 fix had a second code path it did not cover:

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

Each Task-7 guard protected one instance of something there were several of:

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
  (Superseded in Task 10: `_stamp_candidate` is `_record_press`, and the
  overflow report is gone because no bound decides a verdict any more.)
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
  (Superseded in Task 10: deleted, along with the ordering hazard it carried.)

### Task 9: Fourth review round

Files: `travel_capture.py`, `config_flow.py`, `learn_session.py`,
`strings.json`, `translations/en.json`, both test modules, spec

Round three's lesson repeated one file away: a rule established for one consumer
of capped evidence was not carried to the other.

- `TravelRun.heard_overflowed` mirrors the learn cap's `overflowed_at`: a
  distinct rejected press dropped at `_HEARD_CAP` is reported, and the
  mismatch screen and the identity-swap step both refuse the one-click adopt on
  it. One foreign remote across enough selector positions fills the list while
  the user's own press is dropped — the shape that offered to overwrite a
  correct identity with a stranger's.
- `_decisive_occurrence` keeps the occurrence of a press NEAREST the stamped
  winner instead of the newest, so a rival pressed again later cannot erase that
  it competed. (Superseded in Task 10: deleted — a rival is counted when it
  arrives, so no occurrence has to be chosen.)
- `overflowed_at` is a list of timestamps aged by the same window as the
  candidates, replacing a flag that could only ever be set. (Superseded in
  Task 10: deleted with the rest of the settle-time arithmetic.)
- The stop fan-out is bounded by `_SNIFF_FANOUT_LIMIT` and deadlined by
  `_SNIFF_STOP_TIMEOUT_SECONDS`; at expiry it cancels and releases the claim
  itself. Cancellation of the teardown still leaves the publications running.
  (Superseded in Task 10: the bound is gone and the deadline moved into the
  independent task, because a deadline on the caller's await dies with the
  caller.)
- `cover_measure_stop` lets a RUNNING task outrank a cleared session — a latent
  one-tick race the bounded teardown widened into a visible one.
- The competitor-after-the-window test now reads the armed window off the
  PUBLISHED command and compares it against the production budget, so it fails
  when the window constant is reverted (it did not before).
- Busy/ambiguous copy is mode-neutral: "every bridge it listens on".
- Docs: the STOP repeat filter's over-catch (a genuine STOP suppressed within
  1.5 s of an earlier STOP's stamp → measures LONG, the safe direction) recorded
  beside the existing under-catch.

### Task 10: Fifth review round — simplify instead of patching

Files: `learn_session.py`, `config_flow.py`, `strings.json`,
`translations/en.json`, both test modules, spec

The first-capture ambiguity decision had produced a corroborated HIGH three
rounds running, each a deeper timestamp interleaving around an anchor that
moves. The round's instruction was to look for a structural invariant first, and
one exists, so the prescribed dual-timestamp patch was NOT written:

- `_ContestedWindow` per candidate winner. The half of the window before the
  anchor is judged out of the recently-heard presses as the window opens; the
  half after, as each press arrives. The settle sleeps and reads the verdict.
  `_decisive_occurrence`, `_cannot_compete` and `overflowed_at` are deleted, and
  no cap can decide a verdict any more — the remembered-press bound drops the
  oldest while the winner holds the newest slot (Task 12 corrects the reasoning
  first written here, which claimed the dropped press was out of every window's
  reach, and derives the floor of `len(_LEARN_ACTIONS) + 1`); the name cap only
  shortens a screen.
- The stop teardown owns its deadline and its release inside one independent
  task: a deadline on the caller's await vanishes when the caller is cancelled,
  which left hung publications untimed and their bridges claimed for the life of
  the process.
- `_SNIFF_FANOUT_LIMIT` deleted outright — from the arm fan-out, and with it the
  ordering hazard Task 8 had to fix inside it, and from the stop fan-out Task 9
  had just bounded. The shared absolute deadline is what protects the advertised
  window.
- A real arm-timeout test drives nine distinct mismatches and asserts the screen
  offers no adopt, covering the `heard_overflowed` wiring line that every other
  test straddled.
- The mode-neutral busy/ambiguous copy is locked in the existing copy-sync test
  (withdrawing a round-four pushback: this repo does assert string bodies).
- `_SniffAttempt.candidates` → `recent`, with a comment that matches the rule.

### Task 11: Sixth review round — the seams, not the shape

Files: `config_flow.py`, `learn_session.py`, `tests/test_config_flow.py`, spec

All three engines validated the round-5 `_ContestedWindow` refactor by executing
its bookkeeping; the survivors were two production edges and the test quality
that has to hold the invariant in place.

- `async_remove` detaches an open measure session and closes it from a task of
  its own. The arm→STOP handoff is the one path that hands a live session on
  without a task owning it, so a flow removed there left the holders re-arming
  and every bridge claimed until a restart.
- The press bound's justification was false (everything it holds is in window, so
  the dropped press IS reachable). Replaced with the real arithmetic — the winner
  takes the newest slot — which made the bound look safe at 2 or more
  (superseded in Task 12: the floor is `len(_LEARN_ACTIONS) + 1`, because the
  winner's own other buttons take slots a rival cannot); that floor is pinned by
  `test_the_press_bound_leaves_room_for_a_rival`, and the suite no longer
  monkeypatches the bound to 1.
- The before-half tests now record the winner FIRST, as the handler does, and run
  at the bound's floor, so they exercise the interaction that made the old
  justification wrong.
- Four REAL fleet end-to-end tests (two refuse/adopt pairs) cover the two
  refactor seams — the window's look-back and the recognised winner's stored
  window — because the hand-built units stayed green when either was deleted.
- Each candidate keeps its own judging window; the `contested` list and its
  `contested[-1]` re-derivation are gone, and a capture reaching the settle with
  no window refuses rather than adopts.
- `test_more_rivals_than_can_be_named_still_refuse` and
  `test_a_press_that_aged_out_is_never_a_rival` now drive the settle decision
  instead of asserting internals.

### Task 12: Seventh review round — a guardrail and a screen

Files: `config_flow.py`, `strings.json`, `translations/en.json`,
`tests/test_config_flow.py`, spec

No blockers, no highs; both engines re-verified the round-six fixes.

- The press bound's floor is `len(_LEARN_ACTIONS) + 1` (4), not 2. Round six
  counted spare slots instead of slots a RIVAL can occupy: `recent` is keyed by
  the full signature while rivalry ignores the action, so the winner's own other
  buttons on its own selector take slots that refuse nothing. Production is 8 so
  nothing was broken, but the floor as stated licensed lowering the knob into the
  range where a stranger's press is evicted by the user's own DOWN and STOP.
  Corrected in both comments and the spec; the guard test now pins 4 and
  demonstrates the eviction at 3.
- `learn_unchecked` splits off `learn_ambiguous` for the fail-safe refusal, which
  has no rivals to name and was rendering "More than one press arrived … " over a
  single name. `_async_settle_first_capture` now returns the refusal it made
  instead of a bool, so neither call site hard-codes the outcome.
- Spec: the round-three addendum still claimed the arm fan-out keeps a semaphore
  (deleted in round five) and the test inventory still described saturation and
  queueing.

Round-8 addendum (no behaviour findings; one test seam and four prose
corrections):

- The `learn_unchecked` test drives the real `_async_settle_first_capture`
  `window is None` branch through a real fleet learn instead of stubbing the
  outcome, so the screen routing is locked to the production path.
- The floor derivation counted the winner's own press twice (4 signatures + a
  rival read as 5); "verified at 2 and at 3" is now actually exercised by the
  guard test.
- Two live test-inventory bullets in the spec were stale: the press bound's
  "floor of two", and a bridge queueing behind a saturated arm fan-out whose
  bound and test were deleted in round 5.

### Task 13: Ninth review round — documentation reconciliation

Files: docstrings in `travel_capture.py` and `bridge_registry.py`, spec, this
plan. No production behaviour: the executable tokens of both modules are
byte-identical across this task.

Clean on code for the third round running. Every claim in the spec, in this
plan, and in the docstrings of the four modules the PR touches was re-read
against head; each round-by-round task above that a later task reversed now
says so inline, and the "round-N" labels that actually meant "Task N" are
fixed. Details in the spec's round-9 entry.

## Status

Implemented on `feat/fleet-wide-capture-listen`; see PR for gate output and
mutation-check evidence.
