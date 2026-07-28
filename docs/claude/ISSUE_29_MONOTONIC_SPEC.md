# #29 — monotonic live decisions, wall time for persistence only

**Branch:** `harden/29-monotonic` (off `main` @ 7e2c411, post-merge)
**Baseline:** 862 tests green.

## The defect

Every live timing decision runs on `time.time()`. One wall-clock step — NTP correction, manual
change, RTC settle on a battery-less Pi — corrupts all of them at once:

- **Backward step:** a blind whose bridge-armed STOP already fired stays `opening`/`closing` until
  the deadline is reached *again*. A step larger than the remaining travel can leave a cover
  "moving" for hours.
- **Forward step:** `_async_track_motion` sees `remaining <= 0`, completes the travel and — on a
  full travel — calls `_anchor_if_at_limit()`, stamping a hard-limit anchor and
  `position_confidence: anchored` on a blind that physically moved a fraction of the way.
- **Either direction:** our own echoes fall outside their ledger windows, get reclassified as
  physical presses, and hand the cover to a person who never touched it.

The air arbiter already does this correctly — `ZemismartHub.__init__` takes both `now=time.time`
and `monotonic_now=time.monotonic`, and `AirArbiter` gets the monotonic one. The position model and
the ledger never got the same treatment.

## Why this is not a search-and-replace

Wall time is genuinely load-bearing in exactly one place: **restore across a restart**.
`_ATTR_MOTION_STARTED` and `_ATTR_MOTION_DEADLINE` are persisted through `RestoreEntity`
(`cover.py:488-489`) and read back at `cover.py:607,673`. A monotonic clock resets on reboot, so
those two must stay wall.

Everything else is live-only and must become monotonic.

## The clock map as it stands today

The whole chain is *consistently* wall right now, which is why it works — and why a **partial**
conversion is more dangerous than the current state. These are the boundaries where a wall value
meets a monotonic one:

| carrier | field | produced by | consumed by | today |
|---|---|---|---|---|
| `CommandAck` | `acknowledged_at`, `started_at`, `deadline` | hub `self._now` | `cover._start_motion`, `_apply_stop` | wall |
| `HeardEvent` | `heard_at` | `BridgeClock.to_ha_time(boot, t, recv_time)` | `cover._start_heard_motion` | wall |
| `TakeoverCoverState` | `disarm_deadline` | `cover` `WALL_CLOCK()` | hub `models.py:1526-1546`, ledger | wall |
| `_MotionStart` | `started_at`, `deadline` | both of the above | `cover._commit_motion` | wall |
| cover state | `_motion_started`, `_motion_deadline` | `_MotionStart` | `_estimated_position`, `_async_track_motion` | wall |
| cover attrs | `motion_started`, `motion_deadline` | the above | `RestoreEntity` | **wall — must stay** |

## Target state

**Rule: monotonic for every comparison; wall only for values that outlive the process.**

1. **`CommandAck` and `HeardEvent` carry both stamps.** Add a monotonic field alongside each wall
   field rather than replacing it — the wall value is still what gets persisted and shown in
   diagnostics. Name them explicitly (`started_at` / `started_at_monotonic`) so a mixed comparison
   is visible at the call site rather than inferred from context.

2. **Cover's live model goes monotonic.** `_motion_started`, `_motion_deadline`, `_motion_started`
   comparisons in `_estimated_position`, `_async_track_motion`, `_apply_stop`, `_interrupt_motion`,
   and both `_takeover_state` implementations.

3. **Restore converts once, at the boundary.** Compute the remaining duration from the persisted
   *wall* pair exactly once, then immediately express it as a monotonic deadline. Every later
   comparison is monotonic. This is the only place the two clocks legitimately meet, and it must be
   commented as such.

4. **Ledger and state-sync go monotonic wholesale.** `CommandLedger` windows, `_exact_events`,
   `_debounce`, `_commanded_starts`, `_holds`, and `BridgeClock` projection. None survives a
   restart, so none needs wall time. `StateSyncConsumer` and `CommandLedger` take the hub's
   `monotonic_now` instead of `now`.

5. **`extra_state_attributes` keeps emitting wall time.** Restore and diagnostics are unchanged from
   a user's perspective. This means `_motion_started`/`_motion_deadline` need a wall counterpart
   retained purely for publication.

## Invariants the implementation must not break

- **No comparison mixes clocks.** The one sanctioned conversion is the restore boundary (3).
- **`BridgeClock`'s 30 s plausibility clamp** keeps its meaning — it bounds projection error, and
  moving its input from wall to monotonic must not silently change what it rejects.
- **`disarm_deadline` must be on the same clock the hub compares it against.** It crosses from
  cover into the hub and the ledger; converting one side only is the sharpest failure available.
- Restore behaviour across a real restart is unchanged: same attributes emitted, same values.

## Test gap this must close

No existing test moves the clock. Required, each stepping wall time **forward and backward**:

1. during a live **partial** move — physical deadline must be unaffected;
2. during a live **full travel** — must not falsely anchor (`anchored` on a fractionally-moved
   blind is the worst outcome in this system);
3. during **confirmed echo classification** — our own echo must not be reclassified as a press;
4. a **restart restore** with a wall step between persist and restore — the remaining duration must
   still be computed correctly, since that path legitimately reads wall time.

Every one negative-controlled: revert the production change, watch it fail, restore.

## Sequencing

The issue itself warns this touches `cover.py`, `models.py` and `state_sync.py` — the same files
every other open hardening issue touches. It is deliberately serialized after the round-10/11 merge
and before #39 (module split), which would otherwise move all of this code underneath it.


## Outcome

Implemented in `1692a5b`; epochs hardened in `535f048`. **870 tests**, gates green.

`WALL_CLOCK` now has exactly ONE live call site — the restore boundary — commented as the
deliberate wall→monotonic projection. Verified beyond the suite: emitted attribute keys hash
identically to `main`, and `extra_state_attributes` was byte-compared at 1,449 bytes for
default, stopped and moving states.

Review verdict: 6 areas CLEAN, 2 findings.

- **Fixed here.** Three of the four new clock tests started both clocks at the SAME numeric
  value, so the axes coincided and swapping a monotonic read for a wall one was invisible —
  the exact mutation the echo test exists to catch passed both directions. Wall is now a
  realistic unix epoch against a small uptime-style monotonic clock, making axis confusion
  structural. **This is the round's recurring defect class in a new costume: a fixture whose
  own construction makes the thing it tests unobservable.**
- **Filed as #45.** A wall step landing *between persist and restore* is definitionally
  indistinguishable from genuine downtime, so restore can commit a travel that never
  completed. Out of #29's reach: restore must read wall time because the monotonic epoch
  resets on reboot. Bounded in practice — the completion branch does not claim `anchored`,
  and an unverified anchor self-invalidates if the bridge later reports offline.
