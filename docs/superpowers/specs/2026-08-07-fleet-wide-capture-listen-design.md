# Fleet-wide capture listening: every online bridge hears the remote

Date: 2026-08-07
Status: implemented as designed
Branch: `feat/fleet-wide-capture-listen`
Issue: #57

## Problem

The v0.9.4 travel-measurement and Learn flows bind their sniff session to
exactly ONE bridge. `_MeasureSession` is documented as "one armed
travel-measurement listening session on one bridge"; `_async_hold_sniff_open`
re-publishes to a single bridge's command topic; the Learn wizard's
`_async_capture` subscribes to one `rf433/<bridge>/rx`. "Automatic" is not
fleet-wide — `_learn_registry.resolve(area_id)` merely picks the in-area
bridge, falling back to the configured default.

The 2026-08-07 live RF taps (remote `5cb1cc:57`) showed why that fails: the
listening bridge heard 3 of 11 and 2 of 6 physical presses (~30%) while three
to four *other* bridges heard nearly every press. During the Kaelyn
reconfigure the blind obeyed every physical STOP, but the flow — bound to
`rf433-bridge-kaelyn` — never heard them, so the measurement ran on as if
STOP never happened. Meanwhile `state_sync`, which already subscribes
fleet-wide (`rf433/+/rx`), logged 13 STOP captures in the same window: the
presses were on the bus; the flow's one bridge just didn't hear them. This
matches the bogus-identity air-probe finding that a single peer hears only
~every other frame.

## What we build

The measure and Learn sniff sessions listen the way `state_sync` already
does: on every online bridge at once.

- The session subscribes to each target bridge's `/rx` topic and arms
  (re-arms) the bounded sniff window on all of them.
- Frames are deduplicated so the same physical press heard by several bridges
  counts once.
- **Automatic becomes the trustworthy fleet-wide default**; the bridge picker
  becomes an explicit single-bridge override.
- A bridge going offline mid-run degrades nothing: its `/rx` simply falls
  silent and the other bridges carry the session.
- The own-emission guard (`_is_own_emission`) is preserved on every bridge's
  handler — our own TX echoes off *any* listening bridge.

## Decisions taken

| Decision | Choice |
| --- | --- |
| Subscription shape | One subscription **per target bridge**, not the `rf433/+/rx` wildcard — a flow-local wildcard would also hear bridges the user explicitly excluded via the override, and per-bridge subscriptions give each handler its `bridge_id` without re-parsing topics |
| Dedup | A **signature-keyed sliding filter**, keyed and windowed like `state_sync`'s debounce, plus an unconditional rule that the run's opening signature can never re-anchor it — see below |
| Timing across bridges | Bridge-clock subtraction only when the direction press and the STOP were heard by the **same** bridge; otherwise fall back to the monotonic receive times |
| Ownership | Claim **every** target bridge in `_CAPTURE_OWNERS` or none: any bridge another session holds refuses the whole claim as a **conflict**, never as silence on air and never as a quiet listen on the subset |
| First learn capture | Refuses to adopt when a **second press** was heard alongside it, on the resolved and the timed-out path alike — see the trust boundary below |
| Automatic | All online bridges from the flow-local discovery snapshot |
| Explicit pick | Exactly that bridge — the previous behavior, now an override |
| Learn wizard | Same treatment: `_learn_bridge` becomes `_learn_bridges`, `_async_capture` arms and listens on all of them |

## Deduplication: by signature, ahead of the run

`state_sync` dedups with `frame_signature` plus a 1.5 s window
(`_DEBOUNCE_WINDOW_SECONDS`) because it must decide, on an open bus, whether
two captures are one press. Fleet listening gives the capture flows exactly
that problem, so they answer it the same way rather than inventing a second
one.

`press_signature(prefix, remote_id, channels, button)` produces the same
`(remote_key, frozenset(chans), button)` shape `state_sync` keys on. Two
captures share a signature exactly when they are copies of one press on air.
The remote and the channel set are part of the key deliberately: a bare
button would collapse two different remotes — or one remote on two channel
selectors — into a single press, which is precisely what fleet listening
now puts on the same wire.

`TravelRun._is_repeat` stamps every copy on a **sliding** window: each copy
re-stamps its signature, so a burst chains for as long as its copies keep
arriving. A window anchored at the first copy expires mid-burst when a bridge
delivers late, and the escaping copy then reads as a fresh press.

**A window cannot be the whole answer, though, and this is the correction the
second review round forced.** An *isolated* copy from a bridge lagging by more
than a whole burst has no chain to slide: it escapes any window, whatever its
width. So the run's anchor is protected by the signature itself rather than by
a clock —

- `_open` refuses to replace an open run's start when the incoming press
  carries the signature that opened it. Always, however late it arrives.
  Re-anchoring stores a travel time short by the delivery spread, the unsafe
  direction, since a short time leaves "closed" visibly open.
- Nothing in the protocol identifies one physical press: there is no sequence
  number and no per-press nonce, so a late copy and a genuine re-press of the
  same button on the same channels are the same bytes. A restart keyed on
  "same button, long enough after" is therefore a guess, and it guesses in the
  unsafe direction. Keeping the first anchor errs long, and it is *right*
  whenever the shade started moving on the first press.
- A press of the **other** direction has its own signature and still restarts
  the run. That is the user changing their mind, and it is the restart the
  wizard actually needs.

The window's remaining job is to keep a duplicate STOP from closing a run
twice. It deliberately does **not** gate `_open`: a press with no run open
cannot shorten anything, and filtering those swallowed a user's re-press after
a run that closed too fast to store — the wizard then waited out its whole
deadline having heard the user twice.

The `recent` map needs no cap. `classify_frame` pins the remote identity and
the channel set before a signature exists, so one run can only ever see UP,
DOWN and STOP; an earlier draft's `_RECENT_CAP` eviction path was unreachable
by construction and is gone.

The `heard` mismatch list still dedups by value (`mismatch not in self.heard`)
— identical frames decoded on different bridges produce equal `HeardPress`
records — so fleet listening adds no duplicate mismatch rows.

**Learn.** `_SniffAttempt.future` still resolves on the first acceptable
capture; later copies from any bridge return at the `future.done()` gate. What
fleet listening does change there is *which* remote may resolve it — see the
trust boundary below.

## Timing: same-bridge preferred, monotonic across bridges

`interval_seconds` subtracts two bridge-local `t` values. Two *different*
bridges' `t` counters share no epoch, and their `boot` counters can collide
by coincidence — a cross-bridge subtraction would be garbage that passes the
boot guard. So:

- `TimedPress` gains `bridge_id: str | None`.
- `interval_seconds` uses the bridge clock only when both presses carry the
  **same** `bridge_id` (both-`None` counts as same, preserving the pure unit
  tests and any payload without attribution). Otherwise it falls through to
  the monotonic receive-time difference, exactly like the missing-`t` path.
- `started` is the first copy heard of the opening press, and the repeat
  filter guarantees it is the only copy that reaches the run. When the STOP
  arrives from that same bridge the interval is computed on the bridge clock;
  when it arrives from any other (the Kaelyn case, or a bridge that died
  mid-run) the monotonic receive times bound it.

There is deliberately **no per-bridge stamp map**. An earlier draft kept one,
so a STOP could always be timed against its own bridge's copy of the start.
Its whole benefit was cancelling MQTT jitter — tens of milliseconds — against
a measurement that is rounded up to whole seconds and already biased long by
human reaction time. It bought precision below the granularity of the thing
being measured, and cost a stale-stamp failure mode of its own that only
existed because the map existed.

## Trust boundary: what may be adopted as "the remote"

Widening the listen set widens what can be *believed*. Three places accepted
whatever arrived first, which was defensible when "first" meant "heard by the
one bridge in this room" and is not once it means "heard anywhere in the
house".

**No spatial qualification is available.** The RX payload carries `frame`,
`t` and `boot` — no RSSI, no per-bridge signal quality (the `state_sync`
design doc lists "RSSI/location disambiguation" as out of scope for the same
reason). Nothing in the protocol says which press was *nearer*. So the fix
cannot be "pick the closest"; it has to be "do not pick when there is a
choice to make".

1. **The first Learn capture.** It is the one capture with no calibrated
   identity to gate on — `_capture_belongs_to_this_action` compares against
   already-measured actions, and on the first there are none — so any
   Zemismart remote satisfies it. It now keeps listening for
   `_LEARN_SETTLE_SECONDS` (one burst window) past its winner, collecting
   every distinct press heard. One candidate: proceed as before. More than
   one: refuse, name them, and offer retry (`learn_ambiguous`). Captures 2
   and 3 need no settle — the first capture has pinned the remote by then.

   Three details decide whether that rule is honest, and the second review
   round corrected all three:

   - **Both winners.** A capture whose opcode byte is outside the codec's
     action table cannot end the window early (nothing separates it from the
     OEM trailer burst until the window closes with nothing recognised), so it
     is adopted on the **timeout** path instead. That path pins the wizard's
     remote exactly as hard, and an untabled remote's trailer burst from two
     rooms away is precisely what reaches it. It settles and qualifies
     identically; the settle is only as long as the winner has not already
     outlasted, so a capture held twenty seconds ago sleeps not at all.
   - **Around the winner, not across the window.** Competitors are counted
     within one settle window either side of the winner's arrival, on the
     event-loop clock. Collecting across the whole 30-second listen makes a
     legitimate learn *impossible to complete* in a house where anyone else
     touches a remote while the wizard is open: every attempt refuses, and the
     screen's advice — press again while nobody else is — cannot be complied
     with. The boundary is inclusive, because refusing is recoverable and
     adopting the wrong remote is not.
   - **Keyed by press, not by remote.** Candidates key on the full
     `press_signature` — remote, channel set, action — not on the remote id.
     The wizard stores the captured channel set as well as the identity (it
     prefills the first cover with it), so one remote heard on two selectors
     at once is two different answers to "which blind is this".

2. **The measure mismatch screen's one-click adopt.** `cover_measure_use_heard`
   rewrites the device's stored identity from `heard[0]`. With the fleet
   listening, `heard` can hold several unrelated remotes, so adoption is now
   offered only when exactly one foreign remote was heard; otherwise the
   screen names them all and asks for a retry. The step re-makes that
   qualification itself rather than trusting the menu it was reached from —
   defence in depth, since Home Assistant does validate a menu choice against
   the options the step published, but the rule belongs where the identity is
   actually rewritten.

   The screen's own diagnosis is taken from **this device's remote wherever it
   landed in `heard`**, not from `heard[0]`. Reading position zero made the
   diagnosis a race between bridges: a stranger's remote arriving first hid
   the fact that the device's own remote had been heard on the wrong channels,
   and the screen then offered to replace a correct identity with the
   stranger's. An identity match is the one exact diagnosis available — only
   the channel selector can have rejected the press — so it wins outright.

3. **Sharing a bridge between sessions.** Rejected — see below.

**Why refuse instead of offering a picker.** The candidates differ only by a
hex remote id (`a1b2c3:42`), which no user knows for the remote in their hand.
A picker would be a guess with extra steps and a confirmation the user cannot
actually give. Pressing again while nobody else is pressing resolves it in one
action, and the wizard says exactly that.

**Cost.** One burst window (1.5 s) added to the first capture of a Learn run,
and nothing anywhere else.

## Ownership: exclusive, and a conflict is not silence

`_CAPTURE_OWNERS` claims stay **exclusive per bridge**. Reference-counted
sharing was considered — it would remove the failure mode rather than report
it — and rejected: two sessions subscribed to one bridge's RX would each
accept the first press they heard, so one person pressing one remote would be
learned by both open wizards. That is the same silent misattribution this
issue is about, arriving by another door.

What the fleet widening does change is how *likely* a conflict is: Automatic
now claims every bridge, so any second open wizard collides. A conflict is
therefore reported as its own outcome (`bridge_busy` → `learn_busy` /
`cover_measure_busy`) naming the bridges actually held. It must never be
reported as "no press was detected": that screen sends the user off testing a
remote, a channel selector and their standing position, none of which is the
problem.

The claim is **all-or-nothing**, which the second review round corrected from
"claim what is free and skip the rest". A subset claim looks identical to a
healthy fleet-wide session from the outside — the progress screen still names
every bridge — while the bridge that could actually hear this remote may be
precisely the excluded one. #57 exists because a session listened on too few
bridges and reported the result as though it had listened on the right ones;
reproducing that quietly inside the fix would be the same bug with a better
excuse. The cost is that one open wizard now blocks another anywhere in the
house, which is exactly what the busy screen says and what closing the other
window fixes.

## Bounds

Fleet-sized work needs fleet-independent limits:

- **Arming** is concurrent across bridges under a semaphore of
  `_SNIFF_FANOUT_LIMIT`, sharing ONE absolute deadline with the
  wait-for-client that precedes it: the MQTT bootstrap budget is a fixed 5 s
  however many bridges exist, so neither serial batches nor a bigger house may
  push the advertised capture window out. Per bridge it stays ordered —
  subscribe, then open the window — so no bridge's window is ever open with
  nothing listening to it.

  A bridge that raises, or that has not finished by the deadline, is **skipped
  rather than fatal**; zero armed bridges is what fails. The fan-out gathers
  with `return_exceptions`, which is what makes skipping safe: propagating the
  first exception leaves every sibling coroutine running behind the caller,
  and a sibling finishing after teardown has walked the channel list leaves a
  live subscription and a bridge sniffing with its owner key already released.
  Only the bridges that actually subscribed get a re-arm hold, so no window is
  held open on a bridge nobody is listening to.
- **Re-arm holds** retry a failed publish `_SNIFF_REARM_FAILURE_LIMIT` times
  consecutively and then stop, logging at warning and error. A bridge that
  dropped off mid-run was previously republished to forever, silently; a
  broker refusing every publish looked exactly like a healthy hold. A success
  resets the count, so an isolated hiccup costs nothing. The `except` stays
  **broad**: anything the publish can raise would otherwise escape the loop,
  kill that bridge's hold and drop it out of the sniff for the rest of the run
  with nothing said. The bounded retry is what stops a dead bridge being
  republished to forever, so breadth costs nothing.
- **Caches**: `_LEARN_CANDIDATE_CAP` competing presses per attempt. The repeat
  filter needs no cap — see the dedup section.

## Session shape

`_MeasureSession` keeps its lifecycle contract — it spans both progress
phases (arm, then STOP), and `closed` keeps teardown idempotent — but its
one-bridge fields become a list of per-bridge channels:

```python
@dataclass(slots=True)
class _SniffChannel:
    """One bridge's share of a fleet-wide sniff session."""

    bridge_id: str
    owner_key: tuple[int, str]
    command_topic: str
    unsubscribe: Unsubscriber | None = None
    holder: asyncio.Task[None] | None = None
```

`_MeasureSession.owner_key/command_topic/unsubscribe/holder` are replaced by
`channels: list[_SniffChannel]`. `run`, `armed`, `future`, `session_id`, and
`closed` are shared: one run machine fed by all bridges is precisely what
makes the dedup work.

**Arming.** `_async_measure_arm` claims every target bridge in
`_CAPTURE_OWNERS` (or none — a Learn sniff holding one bridge refuses the
measure as busy rather than narrowing it), subscribes the claimed bridges'
`/rx` inside the shared bootstrap deadline, then starts one
`_async_hold_sniff_open` holder task per **subscribed** bridge. Zero
subscribed bridges fails the arm.

**Holding.** The per-bridge re-arm loop wraps its publish in a broad `except`
with a bounded retry: a broker hiccup or a bridge that dropped off mid-run
must not kill the holder (nor, being separate tasks, the other bridges'
holders). The firmware's `start_sniff` still takes the later of current and
candidate deadlines, so the re-publish semantics are unchanged.

**Teardown.** `_async_measure_session_close` walks every channel: cancel the
holder, unsubscribe, publish `{"action":"sniff","seconds":0}`, and release
that bridge's owner key through the existing stop-task done-callback
discipline (release only after the stop publication finished). All stop
tasks are awaited together under the same shield-and-suppress pattern.

The Learn path's `_async_capture` gets the same per-bridge claim/subscribe/
arm/stop treatment inline (it never had a session object; its lifetime is
one function).

## Flow changes

**`cover_measure_setup` / `learn_setup`.** Automatic resolves to *all*
online bridges — a new `BridgeRegistry.online_bridge_ids()` returning the
sorted online set and raising `NoOnlineBridgeError` when empty (the same
error surface `resolve` had). An explicit pick resolves to that single
bridge via the existing `online_bridge` check. `_PendingMeasure.bridge`
becomes `bridges: tuple[str, ...]`; `_learn_bridge` becomes
`_learn_bridges: tuple[str, ...]`.

`_measure_area_id` existed only to feed `resolve(area_id)`; with Automatic
no longer area-bound it is deleted. `learn_setup` keeps its area field — the
area is entry configuration (TX routing), not listen routing — and now
stores the *raw* picker value in `_learn_suggested` so re-showing the form
round-trips "Automatic" instead of a resolved bridge id.

**Copy.** The `{bridge}` placeholder in the sniff/measure progress text
becomes the joined listening set. Strings change from "the bridge for this
device's area" to "every online bridge"; the picker's description now
frames a named bridge as an override for diagnosing what one bridge hears.

## Out of scope

- `state_sync` and normal cover-control TX: untouched. TX still resolves one
  bridge per command — transmitting from many bridges at once is the
  cross-bridge air-arbitration domain, not this issue's.
- Firmware: no contract change. Arming N bridges is N ordinary sniff
  commands.
- A signature cache shared with `state_sync`: the flows' convergence points
  already dedup (above); importing `frame_signature` machinery would add a
  second code path for a problem the run machine solves structurally.

## Testing

Pure machine (`tests/test_travel_capture.py`):

- One press delivered by seven bridges × eight burst frames opens one run,
  anchored at the first copy.
- A lagging bridge's copies, spread further apart than the window is wide from
  the run start, still never restart it — the short-travel regression.
- An ISOLATED copy arriving one window plus a hair after the first, with no
  chain to slide, still never re-anchors — the residual the second review
  round closed.
- A same-direction press seconds later does not restart the run either, and
  says so: the frames are identical, so erring long is the only safe rule.
- A re-press after a run closed too fast to store still opens a run — the
  filter must not swallow a press that has nothing to shorten.
- A STOP 1.4 s after the direction press closes the run rather than being
  swallowed as a repeat (the button is part of the key).
- `press_signature` separates remotes, channel sets and buttons, and is
  insensitive to channel order.
- Direction on bridge A, STOP only on bridge B → monotonic fallback, never a
  cross-bridge `t` subtraction (even with equal `boot` values).
- A second STOP copy after the run closed is ignored.

Flow (`tests/test_config_flow.py`):

- Automatic arms every online bridge: an RX subscription and a sniff start
  per bridge.
- The same press emitted on two bridges' subscriptions yields exactly one
  measurement, timed from the first delivery.
- Bridge silent mid-run (start heard on A, STOP delivered only via B) still
  completes.
- An explicit bridge pick arms only that bridge.
- The Learn wizard on Automatic captures from whichever bridge heard the
  press.
- Own-emission echoes are dropped on every subscribed bridge.
- Two remotes pressed at once are refused and both named; ONE remote heard on
  two bridges is not ambiguous (the control that keeps the refusal honest).
- The window closing on a held unrecognised-opcode capture refuses under the
  same rule, rather than adopting on the path with no gate.
- A press OUTSIDE the settle window does not veto the learn — the control
  that keeps the refusal compliable-with.
- One remote heard on two channel selectors at once is two presses, not one.
- A claim conflict reports busy, on both the learn and the measure path, and
  a fleet claim is refused whole while any one bridge is held.
- Several foreign remotes heard during a measurement withdraw the one-click
  adopt, leaving the stored identity untouched — and the adopt step re-makes
  that refusal itself.
- This device's own remote on the wrong channels is diagnosed wherever in the
  heard list it arrived, not only when it arrived first.
- The arm fan-out saturates its bound without exceeding it and loses no
  bridge; one bridge raising strands no sibling behind the caller; one bridge
  missing the deadline is skipped rather than fatal; a hold that keeps failing
  to re-arm gives up, and a success clears the failure count.
- The confirm screen names the bridges that HEARD the remote, not the armed
  set.

`tests/test_config_flow.py` asserts at teardown that every test released its
bridge claims, so a leak fails the test that caused it rather than some later
test that inherits a claimed bridge. It lives beside the flow tests rather
than in `conftest.py`: the capture flows that claim bridges are all here, and
a suite-wide autouse fixture would charge every unrelated test for an
invariant it cannot break.

Frames are synthesized through the codec; never pasted from house captures.
Every new test must fail with the fleet-wide change reverted (mutation
check).

### As built

Testing-shape notes:

- The own-emission check is covered by driving `_handle_travel_message`
  directly on two bridge ids rather than through a full measure flow. Proving
  a negative through the flow means waiting out `TRAVEL_ARM_TIMEOUT_SECONDS`
  (30 s) to distinguish "the echo was ignored" from "the run has not armed
  yet"; the handler-level test is deterministic and is the shape the Learn
  wizard's equivalent echo test already uses.
- Some mutation-check targets fall outside a whole-feature revert, because
  pre-#57 code also satisfies them: "an explicit pick must not widen to the
  fleet" and the burst/close guards the run machine already had. Those are
  covered by targeted mutations instead (see the PR).
- `_LEARN_SETTLE_SECONDS` is patched down in `install_mqtt` so the settle
  costs the suite nothing; the ambiguity tests set their own.

### Revised after review

The first draft of this design claimed the flows needed no dedup machinery
because the run's burst window and Learn's first-result-wins future already
converged. Review showed both halves of that wrong, and this document has
been corrected rather than annotated:

- The burst window was doing double duty as dedup AND re-press detection, and
  a fleet-delivered copy escaping it stored a silently SHORT travel time.
  Replaced with the signature-keyed sliding filter above.
- First-result-wins was a trust boundary, not just a convergence point: with
  the fleet listening it let any remote in the house win the first capture.
  Replaced with the settle-and-refuse rule above.

The per-bridge start-stamp map from the first draft is gone; the Timing
section records why.

### Revised after review round 2

The round-1 fixes were on the right track and incomplete in the same shape
twice: each closed one code path and left the second one open.

- The trust boundary was wired into the future-resolved capture only. The
  unrecognised-opcode TIMEOUT path adopted with no candidate check and no
  settle, so a foreign remote's trailer burst could still pin the wizard.
- The signature dedup protected the run through a *window*, and an isolated
  late copy has no chain to slide. The anchor is now protected by the
  signature itself, unconditionally, and the window no longer gates a press
  that would open a run.

Also corrected: the fleet claim is atomic rather than best-effort; the
competing-press set is collected around the winner rather than across the
whole listen; candidates key on the full press signature rather than the
remote id; the arm fan-out gathers with `return_exceptions` and skips a bridge
that misses the shared deadline; the mismatch screen's identity diagnosis no
longer depends on which bridge delivered first; `_RECENT_CAP` is deleted as
unreachable.
