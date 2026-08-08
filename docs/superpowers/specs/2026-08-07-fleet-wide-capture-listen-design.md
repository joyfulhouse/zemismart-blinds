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
| Dedup | A **signature-keyed sliding filter** ahead of the run, keyed and windowed like `state_sync`'s debounce — see below |
| Timing across bridges | Bridge-clock subtraction only when the direction press and the STOP were heard by the **same** bridge; otherwise fall back to the monotonic receive times |
| Ownership | Claim every target bridge in `_CAPTURE_OWNERS`; bridges already owned by another session are **skipped**, not fatal; zero claimable bridges is reported as a **conflict**, never as silence on air |
| First learn capture | Refuses to adopt when a **second remote** was heard alongside it — see the trust boundary below |
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

`TravelRun._is_repeat` filters copies **before** `_open` or `_close` sees
them, so those two only ever handle genuinely new presses. That separation is
the fix. Previously one predicate — "same button, within 1.5 s of the run
anchor" — served as both the dedup and the re-press detector, and the two
want different windows:

- The window **slides**: every copy re-stamps its signature, so a burst chains
  for as long as its copies keep arriving. A window anchored at the first copy
  expires mid-burst when a bridge delivers late, and the escaping copy then
  reads as a fresh press, **restarts the run, and stores a travel time short
  by the whole spread** — the unsafe direction, since a short time leaves
  "closed" visibly open.
- Human re-presses are seconds apart and land outside the window either way,
  so restart detection is unharmed.

The `heard` mismatch list still dedups by value (`mismatch not in self.heard`)
— identical frames decoded on different bridges produce equal `HeardPress`
records — so fleet listening adds no duplicate mismatch rows.

Residual: copies of one press separated by more than the 1.5 s window from
*each other* (not merely from the run start) still read as a re-press. That
needs a bridge whose MQTT path lags another's by longer than a whole burst.
The bridges report no clock we could use instead — `t` is bridge-local with no
shared epoch — so the receive clock is what there is.

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
   every distinct remote heard. One candidate: proceed as before. More than
   one: refuse, name them, and offer retry (`learn_ambiguous`). Captures 2
   and 3 need no settle — the first capture has pinned the remote by then.

2. **The measure mismatch screen's one-click adopt.** `cover_measure_use_heard`
   rewrites the device's stored identity from `heard[0]`. With the fleet
   listening, `heard` can hold several unrelated remotes, so adoption is now
   offered only when exactly one foreign remote was heard; otherwise the
   screen names them all and asks for a retry.

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
now claims every bridge, so any second open wizard collides. Zero claimable
bridges is therefore reported as its own outcome (`bridge_busy` →
`learn_busy` / `cover_measure_busy`) naming the held bridges. It must never be
reported as "no press was detected": that screen sends the user off testing a
remote, a channel selector and their standing position, none of which is the
problem.

## Bounds

Fleet-sized work needs fleet-independent limits:

- **Arming** is concurrent across bridges in batches of `_SNIFF_FANOUT_LIMIT`,
  because the MQTT bootstrap budget is a fixed 5 s however many bridges exist,
  and serial SUBACK round trips grow with the house. Per bridge it stays
  ordered — subscribe, then open the window — so no bridge's window is ever
  open with nothing listening to it.
- **Re-arm holds** retry a failed publish `_SNIFF_REARM_FAILURE_LIMIT` times
  consecutively and then stop, logging at warning and error. A bridge that
  dropped off mid-run was previously republished to forever, silently; a
  broker refusing every publish looked exactly like a healthy hold. A success
  resets the count, so an isolated hiccup costs nothing.
- **Caches** are capped: `_RECENT_CAP` signatures in the repeat filter,
  `_LEARN_CANDIDATE_CAP` competing remotes per attempt.

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

**Arming.** `_async_measure_arm` claims each target bridge in
`_CAPTURE_OWNERS` (skipping ones another session owns — a Learn sniff on one
bridge no longer blocks a fleet measure, it just excludes that bridge),
subscribes each claimed bridge's `/rx` inside the existing bootstrap
timeout, then starts one `_async_hold_sniff_open` holder task per bridge.
Zero claimed bridges fails the arm.

**Holding.** The per-bridge re-arm loop wraps its publish in a broad
`except`: a broker hiccup or a bridge that dropped off mid-run must not kill
the holder (nor, being separate tasks, the other bridges' holders). The
firmware's `start_sniff` still takes the later of current and candidate
deadlines, so the re-publish semantics are unchanged.

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
- A claim conflict reports busy, on both the learn and the measure path.
- Several foreign remotes heard during a measurement withdraw the one-click
  adopt, leaving the stored identity untouched.
- The arm fan-out is batched and loses no bridge; a hold that keeps failing
  to re-arm gives up, and a success clears the failure count.
- The confirm screen names the bridges that HEARD the remote, not the armed
  set.

`tests/conftest.py` asserts at teardown that every test released its bridge
claims, so a leak fails the test that caused it rather than some later test
that inherits a claimed bridge.

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
