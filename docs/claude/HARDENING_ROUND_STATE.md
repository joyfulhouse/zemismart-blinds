# Hardening round — state of play (2026-07-27)

Durable record so this round survives a session boundary. All four implementation agents died on
an account session limit (resets 11:00 America/Los_Angeles) with round-2 fixes dispatched but not
applied, so the orchestrating session took the work over directly.

**Status: all round-1 findings are CLOSED on `harden/integration` (829 tests, gates green).**
The four branches are merged; every fix below carries a confirmed negative control -- reverted, the
new test fails; restored, it passes. What remains is the full-set adversarial review of the combined
diff, then the #29 and #39 fast-follows, then release.

## Where the code is

Four worktrees under `.claude/worktrees/`, all branched from `main` @ `09ad346`, all **round-1
complete and green**, none pushed:

| branch | commits | tests | issues |
|---|---|---|---|
| `harden/a-learn` | 4 | 794 | #26 #27 #30 |
| `harden/b-model` | 5 | 791 | #28 #31 #32 #33 |
| `harden/c-hygiene` | 7 | 799 | #35 #36 #37 #38 #40 #41 #43 |
| `harden/d-bounds` | 2 | 793 | #34 #42 |

Baseline on `main` is 785 tests. All four: ruff clean, `mypy --strict` clean.

Deferred by maintainer decision, to run serialized as fast-follows after the merge:
**#29** (wall clock → monotonic) and **#39** (module split).

## Round-1 adversarial review — findings, ALL NOW FIXED

Panel: Codex `gpt-5.6-sol` xhigh, Gemini 3.1 Pro (`agy`), Claude Opus. Every finding below was
independently verified against the code by the orchestrator before being accepted.

### A — `harden/a-learn`
1. **HIGH** — `_capture_belongs_to_this_action` (`config_flow.py`) compares `measured.base ==
   capture.base`, but `capture.base` is computed at `config_flow.py:479` under the CURRENT
   `attempt.action`'s offset while `measured.base` used a PREVIOUS action's offset. The offsets
   differ, so the comparison is always False for the case it exists to catch. Tabled remotes are
   masked by the earlier `inferred is not None` drop; for UNTABLED remotes (the F3/BC/DB hardware
   #26 exists to support) a lingering UP repeat is stored as the measured DOWN base — the original
   #26 failure mode, reintroduced. Fix: compare identity-stable values (`_recover_base(...)` or raw
   `cmd`). The existing test uses a TABLED fixture and so never exercises the guard.
   *(Found by Gemini; missed by Codex.)*
2. **MEDIUM** — `learn_next` offers `learn_derive` after every SUCCESSFUL capture, so derivation is
   still a normal onboarding path rather than a fallback. Reach it only from `learn_timeout`.
   Tests at `test_config_flow.py:789` and `:2076` pin the bypass.
3. **LOW** — modified `strings.json` / `translations/en.json`, outside declared ownership. Overlaps
   workstream C, which also edited them. Resolve at merge.

### B — `harden/b-model`
1. **HIGH** — both `CancelledError` handlers (`cover.py:1073` leaf, `:1506` aggregate) call
   `_mark_unknown()` without incrementing `_restore_epoch`. A still-pending `_async_restore_state`
   then passes its guard at `cover.py:377` and overwrites the invalidation with the cached
   confident position. Precedent to follow: `_apply_stop` does `self._restore_epoch += 1` for
   exactly this reason. Existing cancellation tests attach fully-restored entities and cannot catch it.
2. **HIGH** — aggregate completeness is never checked. `_members()` returns only live configured
   leaves and the laminar topology does not require their channel union to equal the aggregate's
   channels. An aggregate over `{1..6}` with leaves `{1,2,3}`,`{4}`,`{5}` reports a position —
   possibly `anchored` — for all six, and `set_position` moves 1–5 and returns success. A member
   disabled or skipped by `async_setup_entry` (`cover.py:153-166`) does the same. Fix: require the
   union to equal `self._config.channels` before deriving position/confidence or fanning out.

### C — `harden/c-hygiene`
1. **HIGH** — `diagnostics.py:63` pseudonymises `remote_key` as `remote-<sha256[:12]>`. That is a
   deterministic unsalted digest of a low-entropy identity: virtual remotes use a 24-bit space, and
   the reviewer brute-forced a worst-case key in ~7 s of plain Python. Also enables correlation
   across unrelated public dumps. Fix: dump-local opaque labels (`remote-1`, …) from a per-call
   mapping. The privacy test only asserts the literal identity is absent, so it accepts a
   reversible encoding.
2. **MEDIUM** — exceptions only half-translated: raw `str(exc)` is interpolated into the
   `{error}`/`{failures}` placeholders (`__init__.py:291`, `cover.py:1129`, `:1701`), so a
   non-English user still receives English model text. The aggregate path renders
   `str(HomeAssistantError)` to English then nests it in another translated exception.
3. **LOW** — `test_cover.py:5822` exercises only `ZemismartCover`; reverting the aggregate's
   narrowed handler leaves it green.
4. **LOW** — `test_cover.py:5812` throttle test does not pin the completion write; deleting the
   write at `cover.py:1106` still satisfies `10 <= len(writes) <= 12`.

### D — `harden/d-bounds`
1. **MEDIUM** — `models.py:1974` counts STOPs in the cap denominator (a live fast-lane STOP sits in
   `_fast_inflight` for its whole life), so 128 outstanding STOPs refuse the next legitimate
   movement/raw frame. The code exempts STOP from the *check* but not the *count*, contradicting
   its own comment. Fix: count only `not command.is_stop`.
   **Verified safe:** STOP can never itself be rejected — `if not command.is_stop` guards the cap
   and `send_raw` always builds with `is_stop=False`, so the flood vector cannot create STOPs.
2. **LOW** — `test_models.py:5543` passes with `_publish_seq` pruning entirely reverted (the
   protected key survives because nothing is ever evicted).
3. **LOW** — `test_models.py:5587` has no assertion; passes if `maintain()` becomes a no-op.

## Cross-file work — reassigned to whoever owns `models.py`

The file-ownership boundary kept the four agents from colliding but left these unreachable:

1. **BLOCKER** — `resolve_bases` is never passed to `StateSyncConsumer` at `models.py:1077`, so
   #30's exact-command validation is **inert in production**. Workstream A's test only passes
   because it injects the resolver by hand. *(Found independently by Codex and Gemini.)*
2. **HIGH** — the four `frame_signature()` callers at `models.py:1176`, `2233`, `2256`, `2626` have
   no resolver, so for an F3/BC/DB remote `infer_action_button` returns None and the signature is
   None. At `2233` `_ledger_registration` then bails before registering the action OR its armed
   `stop_raw`: the timed command is absent from the ledger, our own echo is classified as a
   physical press, and `async_disarm_remote` finds nothing — leaving the previous identity's
   fail-safe STOP **armed** after a relearn. This breaks the reconfiguration safety contract, and
   it applies to exactly the hardware #26 was filed to support.
3. **MEDIUM** — #38's hub counters are still a comment, so a stuck hub dumps identically to an idle
   one. Needs a read-only accessor exposing queue depth, fast-lane depth, pending count, ledger
   entries, held captures, `len(_publish_seq)`, `len(_bridge_affinity)`.
4. **MEDIUM** — `models.py:1476` still uses PEP 758 bare multi-except. See the ruff-format trap below.

## Two places the orchestrator's spec was WRONG

Recorded because both will re-trap anyone who repeats the instruction.

1. **`except (A, B):` cannot be used in this project.** `ruff format` with
   `target-version = "py314"` (`pyproject.toml:26`) rewrites the parenthesised form straight back
   into the bare PEP 758 form. Independently reproduced:
   `except (UnicodeDecodeError, json.JSONDecodeError):` → `except UnicodeDecodeError, json.JSONDecodeError:`.
   It is the *formatter*, not a lint rule, so there is nothing to configure short of lowering
   `target-version`. The working fix is to split into two single-exception blocks — the pattern
   `_handle_info` already uses.
2. **`manifest.json` cannot carry an HA version floor.** HA's `Manifest` TypedDict
   (`loader.py:246-282`) has no minimum-version key; that is a HACS concept (`hacs.json`). The
   spec's fallback of "document the floor in manifest.json" would be a no-op and hassfest may
   reject it. Removing the syntax is the only real fix.

## Open question worth investigating

`attach_cover` in `tests/test_cover.py` leaves `_platform_state` at `NOT_ADDED`, so HA silently
drops every `async_write_ha_state()` in tests that use it. Some existing tests may therefore be
asserting less than they appear to. Flagged by workstream C; not yet investigated.

## Findings that were CHECKED AND REJECTED

Do not re-file these; each was traced and is false.

- Gemini/B: "`position_confidence` uses a set comprehension, so the length check is inverted" —
  the code uses a **list**; the check is correct. Consequent claim that B's suite fails is false
  (791 pass).
- Gemini/B: "#31 rename missing from strings/translations" — the rename is of a state-attribute
  *value*, not a translation key; the only surviving `verified` hits are the unrelated
  pre-existing `unverified_anchor_*` and a comment explaining the rename.
- Gemini/C: "`CommandStartedTimeoutError` unhandled in the leaf" — `cover.py:1124` catches both
  timeout types.
- Gemini/D: "add `_fast_stops` to `_token_protected_channels()` and to the cap count" —
  `_fast_stops` holds `asyncio.Task`, not commands; the commands are already in `_fast_inflight`.
  The suggested change would double-count and would not type-check.

## Next steps, in order

1. Apply the round-1 findings above in each worktree.
2. Apply the four cross-file items (needs one owner across `models.py` + `state_sync.py` +
   `config_flow.py`).
3. Re-review each branch until clean.
4. Merge A–D; expect conflicts in `cover.py` (B vs C) and `strings.json`/`en.json` (A vs C).
5. Full-set adversarial review of the combined diff vs `main`.
6. Fast-follow #29, then #39, each with its own review loop.
7. Release: CHANGELOG incl. the #31 breaking note, manifest bump, strings/en.json parity.


---

---

# Addendum — the fix passes (same day)

`harden/integration`, 37 commits, **841 tests**, ruff + mypy --strict clean.
Every fix below carries a confirmed negative control: reverted individually, its test fails.

## The headline number

**Sixteen defects across three review rounds of a change that was green on every gate from the
first commit.** Green gates were worth close to nothing here. What found things was: an
adversarial reviewer with the whole diff, and reverting each fix to watch its own test fail.

## Round 1 of the full-set review — 6 defects

1. HIGH: aggregate command timeouts never invalidated members, so a group whose `started` was lost
   kept integrating -- possibly through a STOP that did fire -- still reporting `anchored`.
2. HIGH: `is_closed` ignored channel coverage, so an incomplete aggregate published itself to HA as
   `closed` with a channel entirely unmodelled.
3. MEDIUM: `maintain()` had NO production caller. It shipped with a docstring claiming HA drove it
   periodically while the only callers were tests -- #42 was never finished.
4. MEDIUM: `CommandQueueFullError` (a RuntimeError) was in neither cover boundary's tuple, so
   reaching the cap surfaced a raw traceback.
5. LOW: the cancellation tests did not pin `_restore_epoch` at all.
6. LOW: the untabled test pinned 1 of 4 `frame_signature` call sites while claiming to cover the
   armed STOP.

Plus a defect introduced by the translation commit itself: `command_failed` still demanded an
`{error}` placeholder no raise site supplied any more.

## Round 2 — 6 defects

1. HIGH: the #32 position fix created an inconsistency -- an aggregate with an unknown member
   reported no position but `assumed` confidence, and a test pinned the wrong answer. Now one rule
   everywhere: **no position means unknown**, with suspect still outranking it.
2. MEDIUM: `send_raw` still interpolated `str(exc)`. The "admin-only debug service" exemption I
   wrote does not hold -- the placeholder still renders into the user's locale -- and a test
   affirmatively required the English word "hex", actively holding the violation in place.
3-6. Four tests that could not fail, two of them written in round 1.

## Round 3 — 4 defects

1. MEDIUM: `send_raw` rebuilt `RemoteIdentity` without the loaded remote's bases, so the raw path
   silently fell back to opcode inference: a raw movement frame for a loaded untabled remote never
   entered the ledger, never stamped a commanded start, and had its own echo dispatched as a
   physical press. The one surface still outside #26/#30 after every other was fixed.
2. MEDIUM: rewriting the leaf restore-race test to drive a real cancelled command unpinned
   `invalidate_for_cancelled_command()`'s own bump.
3. LOW: the `_record_publish` prune path was untested -- both prune tests crossed the cap via the
   other call site.
4. LOW: README and a test docstring documented the confidence rule replaced one commit earlier.

## The pattern worth carrying forward

**A fix for a gap opens another gap.** It happened twice, in the same shape both times:

- the #32 position fix (unknown member withholds the position) left the confidence rule behind,
  and the two then contradicted each other;
- rewriting the restore-race test to pin the leaf's bump removed the only exercise of the helper's.

Round 4's prompt asks reviewers to check for exactly this. Any future fix round should.

## Reviewer scoring, three rounds

- **Codex `gpt-5.6-sol` xhigh: 16 real defects, 0 false.** Every finding survived verification.
- **Gemini 3.1 Pro (`agy`): 0 real defects, ~10 false claims, and five consecutive confident
  "no defects found" verdicts on trees that contained HIGH defects.** Its verification prose reads
  plausibly and is frequently accurate about the code it quotes -- and it hallucinated a set
  comprehension, inverted a dict's key/value types, and quoted a comprehension with its filter
  clause removed. Do not treat its all-clear as evidence.

One Gemini finding was worth acting on, and only incidentally: a remark buried inside an otherwise
false report noted that an incomplete aggregate returned `assumed` while its position was None.

## Traps recorded so they are not walked into again

1. `SYNTHETIC_REMOTES[2]` looks untabled (base `0xF38F`) but puts an `0xF471` command ON AIR once
   the remote id and group offset are applied -- and inference reads the command, not the base.
   Use `UNTABLED_*` from `synthetic.py`.
2. A forged frame sent alongside an honest one reduces to the same signature under inference, so
   the debounce swallows it and the event count is unchanged either way. Give it its own hub.
3. `ruff format` at target-version py314 rewrites `except (A, B):` back to the bare PEP 758 form.
   Bind the tuple to a NAME.
4. A syntax-floor test scoped to one file stays green while ten bad sites sit elsewhere.
5. `attach_cover` left `_platform_state` at NOT_ADDED, so `async_write_ha_state()` never reached the
   state machine and anything observing states asserted nothing. Fixed in the helper.
6. `codex exec` inherits an open stdin from a background shell and hangs on "Reading additional
   input from stdin" forever. Redirect `< /dev/null`. A 39-byte output that never grows is the tell.

## Full-set review: nine rounds, thirty defects

| round | found | shape |
|---|---|---|
| 1 | 6 | aggregate timeouts, `is_closed`, `maintain()` had no caller, untranslated cap error, 2 dead tests |
| 2 | 6 | confidence/position contradiction, send_raw English text, 4 dead tests |
| 3 | 4 | send_raw missed the loaded remote's bases, 2 dead tests, stale docs |
| 4 | 5 | cancellation erased a newer press, setup-window bases, derivation reference, 2 dead tests |
| 5 | 2 | the guard could not distinguish a STOP that stopped nothing; 1 dead test |
| 6 | 2 | the guard could not prove RF ordering at all -> REVERTED; aggregate missed a late joiner |
| 7 | 1 | the timeout path lacked the restore-epoch bump the cancel path had |
| 8 | 3 | aggregate missed a mid-flight leaver; stale onboarding docs; contradictory test docstring |
| 9 | 1 | aggregate invalidation is keyed on live entities, not topology -> filed as #44 |

**Round 9 verdict: SHIP.** The remaining finding is a pre-existing condition this branch
incompletely fixes, not a regression -- on `main` those paths invalidate no members at all.

## The three lessons this round actually taught

1. **A fix for a gap opens another gap.** Four times. The `#32` position fix left the confidence
   rule behind; rewriting the restore-race test unpinned the helper; and aggregate member selection
   was wrong three consecutive times (snapshot -> current -> union -> still incomplete, #44).
   When a review names a specific defect, fix the CLASS and enumerate the siblings.

2. **A review finding is an observation, not a specification.** Round 4 observed that cancellation
   destroys a good re-sync. True. I read it as "so preserve it" and built a guard that took three
   attempts and two rounds to prove unsound. The correct reading was that cancellation after
   publication is genuinely unknown, and the finding described a COST, not a bug.

3. **Green gates proved nothing.** Thirty defects in a change that was green on ruff, mypy --strict
   and the full suite from its first commit. What found them was an adversarial reader with the
   whole diff, and reverting each fix to watch its own test fail. Roughly a third of all findings
   were "this test cannot fail".

## Reviewer scoring, nine rounds

- **Codex `gpt-5.6-sol` xhigh: 30 real defects, 0 false.** Every finding survived verification.
- **Gemini 3.1 Pro (`agy`): 0 real defects, ~12 false claims, 10 confident "no defects found"
  verdicts on trees containing HIGH defects** -- and in round 9 it repeated verbatim a BLOCKER it
  had already been shown was false in full-set round 1 (`_ACTION_COMMAND_HIGH` is `dict[str, int]`;
  it insists on `dict[int, str]`). Dropped from the panel at that point. Its one useful
  contribution in nine rounds was an incidental remark inside an otherwise false report.

## Round 10 — DRY and simplification passes

Three commits after the ship verdict, on the same branch: `47e5224`, `9e39731`, `897d4e2`.
Claimed behavior-preserving apart from three deliberately added tests.

**Production duplication removed**

| what | was | why it mattered |
|---|---|---|
| bases parse / serialize / pacing validation | 2 copies (RemoteConfig, BlindConfig) | the empty-trailer marker is what makes options-over-data merging correct |
| final pre-air validity sequence | 2 copies (`_ordered_publish`, `_finalize_and_publish`) | it is what stops an already-retired command from reaching the air |
| entity class attrs + `device_info` + `__init__` wiring | 2 copies (leaf, aggregate) | the old comment worried IN PROSE that an attribute could start recording at travel rate in one class only |
| transport-failure tuple | 2 literal `except (...)` tuples | now derived from the translation-key mapping, so "mapped but never caught" is impossible |

**Test duplication removed** — 286 lines net. The inert MQTT subscribe stub was written out 13
times; the auto-acking hub preamble 19 times.

**Complexity** — `_ordered_publish` 24 -> 20 (85 -> 71 statements) via `_commit_and_enqueue` +
`_finish_air_hold`; `_async_restore_state` 15 -> 14 (65 -> 58) via
`_restored_state_describes_this_cover` + `_restore_confidence_signals`.

**Two real gaps the DRY pass exposed** — both pre-existing, both found by mutation rather than
by reading:

1. Removing `NoOnlineBridgeError` and `CommandRejectedError` from the caught set left all 850
   tests green. A remote with no reachable bridge, and a bridge NACK, each surfaced a raw
   `RuntimeError` through the service layer. Nothing pinned either. Now pinned at both boundaries.
2. Neutering `close()`'s waiter cancellation left all 852 green. The neighbouring close test
   cancels its own task before asserting, so it never observed who did the cancelling. Now pinned
   — with a BOUNDED await, because the failure mode is a hang and a bare `await` wedged the whole
   suite instead of naming the waiter left behind.

**One equivalent mutant, deliberately not "fixed"** — `commit_air_plan = not arbiter_failed`
cannot be observed: `arbiter_failed` is only ever set alongside `plan = None`, and the downstream
use is gated on `count_air_plan`. It is defensive redundancy, not a coverage gap. Worth recording
so a later round does not "discover" it again and write a test that cannot fail.

**The round's own worst moment — `d21ab01` shipped the regression it prevented**

`d21ab01` consolidated the two byte-identical `register_rx_listener(...)` calls into one
`_register_rx_listener()` on the shared base, precisely because the ARGUMENT LIST is the #30
contract. It shipped **without `bases=self._config.remote.bases`**. The negative-control mutation
I ran on that exact line is what got committed; the restore-then-`858 passed` reading I acted on
described the working tree, not the bytes that landed. `ccec231` fixes it.

Two things to carry:

1. **A green suite after a restore is not evidence about the commit.** Verify the ARTIFACT:
   `git show HEAD:<file> | grep <the line>`, and for anything load-bearing run the suite in a
   throwaway worktree at HEAD (`git worktree add --detach /tmp/x HEAD`). This is a new entry in a
   long list of ways a negative control lies — and the first where the control itself worked
   perfectly and the *commit* was the thing that diverged.
2. **The counterpart is genuinely reassuring.** `test_cover_registration_supplies_bases_to_the_hub`,
   written in an earlier round precisely because deleting `bases=` had once left the suite green,
   caught this deterministically on the very next commit — alone and in the full run. The round's
   own output is what caught the round's own mistake.

**How behavior-preservation was proven, beyond the suite**

- Differential test against the pre-refactor module imported side by side: `as_dict()` key ORDER
  and values identical for both classes across no-trailer / trailer / empty-trailer, and identical
  exception type AND message text for 14 validation cases.
- Effective entity surface (`assumed_state`, `device_class`, `should_poll`, `supported_features`,
  `_unrecorded_attributes`, `unique_id`, `name`, `device_info`) equal for both classes;
  `RestoreEntity` still in the leaf's MRO and still absent from the aggregate's.
  NOTE: comparing `_attr_*` on the CLASS is meaningless — HA's `CachedProperties` metaclass turns
  each into a per-class descriptor, so identity always differs. Compare via an instance.
- Token-level (wrapping-insensitive) comparison: `_finalize_and_publish` is identical to its
  pre-refactor form once `_revalidated_body` is inlined, and the revalidation window inside
  `_ordered_publish` matches token for token.

## Round 11 — the review that settled the test-consolidation question

The strongest method used in this whole hardening effort, and worth reusing. Rather than spot-check
the 19 helper substitutions, the reviewer ran the **entire pre-refactor test suite against
post-refactor production code**: `git show 54b0697:tests/...` added verbatim as `*_legacy.py`
alongside the current suite, then **28 production mutations**, comparing the FAILURE SETS of the
two suites.

- `caught_by_legacy_only`: **empty for all 28**. The consolidated suite never lost a kill.
- `caught_by_current_only`: non-empty exactly twice, both deliberate additions.

That is a proof about the refactor, not a sample. Use it whenever a test refactor is large enough
that reading the diff cannot settle the question.

**Verdict: no behavioral defect in the refactor.** Areas 1-4 clean, each proved by runtime dump or
mutation rather than reading. Notably the entity-class check dumped MRO, `__abstractmethods__`,
`__combined_unrecorded_attributes`, the metaclass's backing store for every cached property, and
the defining class of every name in `dir()`, for both classes under both revisions: zero
differences beyond the three expected new origins.

**What it found (all pre-existing, none a regression):**

| finding | status |
|---|---|
| partial-bases guard untested | already fixed in `d34c98d` before the report landed |
| three dead `BridgeRegistry` locals | already fixed in `d34c98d` |
| **role half of the restore identity guard untested** | fixed here — HIGH, see below |
| `_finish_air_hold` fail-open uncovered | fixed here; the asymmetry with its sibling was unintentional |
| `close()` test docstring overstates what it pins | corrected here |
| `test_overlap_token_is_rechecked_after_waiting_for_publish_lock` misnamed | renamed here |

The role finding is the one that mattered. Making `restored_role == self._config.role.value`
always-true left **all 860 tests green**, because every restore fixture supplies a role that
matches by construction. It is reachable and it is this integration's worst failure mode: an
aggregate publishes `role` precisely so a restore can discriminate, and the position it publishes
is DERIVED from its members — flip that `cover_id` to a leaf and the member-derived number is
reinstated as the new leaf's own dead-reckoned estimate. The sibling remote/channels half of the
same guard was tested; only the role half was naked.

**Still open from round 11 — the contended overlap re-check.** `_raise_if_overlap_displaced` runs
both pre-lock in `_async_execute` and under the lock in `_revalidated_body`. Dropping the pre-lock
site alone changes nothing the suite can see (the under-lock site is now pinned by the round-10
ordering test). The case the under-lock repeat exists for — an overlapping PUBLICATION landing
while a command waits for `_publish_lock` — has no test, and writing one is awkward because
publishing requires the very lock the test must hold. Recorded rather than papered over with a
synthetic state-poking test.

## Still open

- #29 (wall clock -> monotonic), then #39 (module split), each serialized with its own review loop.
- Release: CHANGELOG incl. the #31 breaking note, manifest bump, catalogue parity.
- #44: aggregate failure invalidation keyed on live entities rather than configured topology.

---

# Finale — v0.7.0 (2026-07-28)

All 20 open issues (#26–#45) closed. Fast-follows #29, #45, #44, #39 each ran its own
Codex review loop to SHIP (4, 1, 4 and 3 rounds respectively), then a DRY pass and a
four-model adversarial panel (Codex `gpt-5.6-sol`, Claude Fable, grok-4.5, Gemini 3.1 Pro)
over the combined delta ran four rounds to a unanimous SHIP: 6 + 3 + 2 + 0 findings, the
later rounds all cross-change compositions (recovered-travel confidence, tombstone/live
entity synchronization, aggregate confidence-without-position).

Panel scoring this cycle: Codex 3 real findings (1 declined as the adjudicated #44
option-(a) residual), grok-4.5 4 real (1 initially declined, later accepted on new
evidence), Claude Fable 1 real + the most rigorous verifications, Gemini 3.1 Pro 1 real
(its FIRST in this project — corroborating Fable's independently, with a runtime trace).

Adjudications recorded: full-HA-restart tombstone loss for absent leaves stays the
documented #44 option-(a) residual; legacy-shim private constants are import-compatible
but NOT live patch seams (canonical modules are, per shim docstrings); `_UINT32_*` are
math constants, not tunables. Non-blocking leftovers: an unreachable ranking arm in the
aggregate confidence property after the unknown early-return; empty per-entry topology
records survive entry deletion (process-lifetime memory only).

Released as v0.7.0 (manifest bump + CHANGELOG). main NOT pushed — awaiting maintainer.
