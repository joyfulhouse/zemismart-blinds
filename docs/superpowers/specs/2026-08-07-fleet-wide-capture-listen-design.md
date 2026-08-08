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
| Dedup | In the run state machine (measure) and the existing first-result-wins future (Learn), not a new signature cache — see below |
| Timing across bridges | Bridge-clock subtraction only when the direction press and the STOP were heard by the **same** bridge; otherwise fall back to the monotonic receive times |
| Ownership | Claim every target bridge in `_CAPTURE_OWNERS`; bridges already owned by another session are **skipped**, not fatal; zero claimable bridges is the failure |
| Automatic | All online bridges from the flow-local discovery snapshot |
| Explicit pick | Exactly that bridge — the previous behavior, now an override |
| Learn wizard | Same treatment: `_learn_bridge` becomes `_learn_bridges`, `_async_capture` arms and listens on all of them |

## Deduplication: why the run machine already is the dedup

`state_sync` dedups with `frame_signature` plus a 1.5 s debounce window
(`_DEBOUNCE_WINDOW_SECONDS`), because it must *classify* every distinct press
on an open bus. The capture flows need less: they only ever wait for one
specific remote's next press, and both already have exactly the right
convergence point.

**Measure.** One physical press is 8 embedded OEM frames across ~609 ms, and
`TravelRun._open` already treats a same-direction frame inside
`TRAVEL_BURST_WINDOW_SECONDS` (1.5 s — deliberately equal to state_sync's
debounce) as a repeat of the press that opened the run. A copy of the press
heard 40 ms later by a second bridge is indistinguishable from the burst's
own repeats, and is absorbed by the same guard. A STOP copy from a second
bridge arrives after the first STOP closed the run and future — the handler's
`future.done()` check and the machine's "STOP with no open run" branch drop
it. The `heard` mismatch list dedups by value (`mismatch not in self.heard`),
and identical frames decoded on different bridges produce equal `HeardPress`
records, so fleet listening adds no duplicate mismatch rows.

**Learn.** `_SniffAttempt.future` resolves on the first acceptable capture;
every later copy — same bridge or another — returns at the `future.done()`
gate. The held `unrecognized` candidate keeps first-writer-wins semantics
(`if attempt.unrecognized is None`). No changes needed beyond subscribing
more bridges.

What *does* need new machinery is timing attribution, below.

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
- `TravelRun` keeps, alongside `started` (the earliest press of the current
  run, which defines the run's direction and the monotonic anchor), a
  per-bridge stamp map `starts: dict[str, TimedPress]` — the first copy of
  the *current* press each bridge heard. When the STOP arrives on bridge X
  and bridge X also stamped the start, the interval is computed same-bridge
  on the bridge clock: full precision, broker jitter cancelled. When the
  STOP's bridge never heard the start (the Kaelyn case, or a bridge that
  died mid-run), the earliest start plus monotonic fallback bounds the run.

The monotonic fallback's error is HA receive jitter across two bridges' MQTT
paths — tens of milliseconds against a measurement that is rounded up to
whole seconds and already biased long by human reaction time. A restart of
the run (direction press outside the burst window, or a reversal) clears the
stamp map with it.

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

- The same press heard by two bridges opens one run; the second copy neither
  restarts the run nor shortens it.
- Direction on bridge A, STOP only on bridge B → monotonic fallback, never a
  cross-bridge `t` subtraction (even with equal `boot` values).
- Direction copies on A then B, STOP on B → B's own stamp, bridge-clock
  precision.
- A restart clears the per-bridge stamps.
- A second STOP copy after the run closed is ignored.

Flow (`tests/test_config_flow.py`):

- Automatic arms every online bridge: an RX subscription and a sniff start
  per bridge.
- The same press emitted on two bridges' subscriptions yields exactly one
  measurement with the same-bridge value.
- Bridge silent mid-run (start heard on A, STOP delivered only via B) still
  completes.
- An explicit bridge pick arms only that bridge.
- The Learn wizard on Automatic captures from whichever bridge heard the
  press.
- Own-emission echoes are dropped on every subscribed bridge.

Frames are synthesized through the codec; never pasted from house captures.
Every new test must fail with the fleet-wide change reverted (mutation
check).

### As built

Everything above landed as designed, with one testing-shape change worth
recording:

- The own-emission check is covered by driving `_handle_travel_message`
  directly on two bridge ids rather than through a full measure flow. Proving
  a negative through the flow means waiting out `TRAVEL_ARM_TIMEOUT_SECONDS`
  (30 s) to distinguish "the echo was ignored" from "the run has not armed
  yet"; the handler-level test is deterministic and is the shape the Learn
  wizard's equivalent echo test already uses.
- Two mutation-check targets fall outside a whole-feature revert, because
  pre-#57 code also satisfies them: "an explicit pick must not widen to the
  fleet" and the burst/close guards the run machine already had. Those are
  covered by targeted mutations instead (see the PR).
