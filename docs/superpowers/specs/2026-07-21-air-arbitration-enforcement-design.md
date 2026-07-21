# Cross-Bridge RF Air Arbitration — Phase 2 Enforcement Design

Date: 2026-07-21

Status: Implementation-ready (authored by gpt-5.6-sol xhigh, reviewed by Claude; owner directive
2026-07-21: deliver the contention solution end to end)

Consumer baseline: Phase 1 at `1e7ae2e`, plus `DEFAULT_REPEATS = 3` at `da7d2bf`

Release target: `v0.4.0`

## Decision summary

Phase 2 makes the existing `AirArbiter` enforce one conservative RF collision domain across all
online bridges. A normal command publishes at the earliest time at which its immediate train and,
when timed, its future fail-safe STOP window do not overlap another bridge's known use of the air.
An explicit STOP never waits on this calendar. Firmware-scheduled and displaced STOPs remain local
to each bridge and are unchanged.

The calendar is anchored by evidence, not publication timing. Publication creates only a
provisional, start-unknown record. A correlated `started` status atomically replaces that record
with an actual immediate drain hold and optional future STOP reservation anchored at:

```text
local monotonic receipt time - valid age_ms
```

The existing wall-clock/bridge-clock projection continues to produce `CommandAck.started_at` for
cover modeling. It is not used by air arbitration.

Enforcement is the default. One installation-wide YAML escape may select `shadow`; it is not a
config-entry option because the arbiter and bridge registry are domain-scoped. There are no new
MQTT topics, payload fields, firmware changes, entity controls, or per-remote tuning knobs.

At the production `609 ms` slot, `repeats = 3`, and `100 ms` guard, an action-only train costs
`1,827 ms` and consecutive cross-bridge starts are `1,927 ms` apart. A 12-train whole-house scene
therefore has an action-only last-start budget of `21.197 s`. If every one of the first 11 trains
also has a trailer, the worst-case last start is `41.294 s`. This latency is accepted in exchange
for reliability.

## Context and current code boundary

The Phase 1 implementation is deliberately shadow-only:

- `codec.estimate_b0_slot_ms()` mirrors firmware B0 validation and pacing. Production frames cost
  `609 ms` per scheduler dispatch with the fleet's uniform `repeat_gap_ms: 35`.
- `air.plan_for_body()` charges action, trailer, and STOP once per consumer repeat.
- `AirArbiter.observe()` records a publication-time shadow horizon but does not delay anything.
- `ZemismartHub._observe_air()` is called inside `_finalize_and_publish()`, after the authoritative
  `_rebuild_from_live_contributors()` and immediately before the publisher.
- `ZemismartHub` has one normal worker. `_async_execute()` resolves a bridge, registers correlated
  status futures, publishes, awaits `accepted`, and then awaits the first `started`.
- `_async_enqueue()` gives STOP a fast lane through `_async_run_fast()`. A STOP waits only for
  existing earlier overlapping **publication** barriers; it never waits for a command's admission
  or `started` lifecycle.
- `_ordered_publish()` and `_publish_lock` serialize the one scheduling yield needed to hand a
  message to paho. QoS-1 PUBACK completion remains in the background.

Phase 1's provisional horizon cannot be enabled as-is. It is anchored when the payload is
published, although a selected bridge may not hand the frame to RF until later. It also computes
future STOP timing without putting those windows into the calendar. Both gaps are fixed here.

## Goals

1. Prevent overlap between normal HA-originated trains on different bridges in the one configured
   collision domain.
2. Avoid starting a normal train through any known future fail-safe STOP window.
3. Preserve every existing STOP priority and publication-order guarantee.
4. Anchor occupancy to correlated RF handoff using local monotonic time and `age_ms`.
5. Keep every state structure bounded and every wait finite.
6. Fail open on missing, malformed, exhausted, cancelled, or internally inconsistent arbitration
   state.
7. Preserve one-bridge behavior and a first command on a clear calendar.
8. Preserve exact final-body planning after coalescing and contributor cancellation.

## Non-negotiable invariants

1. An explicit STOP does not call or await the air gate. A firmware fail-safe or displaced STOP
   never consults HA.
2. Existing same-target publication barriers still preserve request order. “STOP never waits”
   means it never waits on an air-calendar time or another command's acknowledgement lifecycle;
   the short existing barrier needed to put an older overlapping payload into paho first remains.
3. Fewer than two currently online bridges means OFF. Every held command wakes and publishes
   immediately, subject only to its existing validity and publication barriers.
4. Offline does not erase a started timed command's reservation. A broker-disconnected bridge can
   still fire the STOP held in RAM.
5. A changed, valid `boot` proves old RAM state is gone and retires the old boot's future
   reservations and provisional start records.
6. All scheduling timestamps are from an injected monotonic clock whose production default is
   `time.monotonic`. Wall-clock changes cannot move the calendar.
7. No arbitration exception rejects or suppresses a valid RF command. The command publishes and a
   fail-open counter increments.
8. The guard begins at `100 ms`. Arbitrary `repeat_gap_ms` remains unsupported; the seven-bridge
   fleet is verified uniform at `35 ms`.
9. State is process-local, finite-lived, and not restored after HA restart.
10. Same-bridge work is not cross-bridge contention. The firmware scheduler serializes that
    transmitter and prioritizes its local STOPs.

## Out of scope

- Conflict-graph concurrency or spatial reuse. The measured graph cannot certify safe
  blind-level concurrency; Phase 3.3 remains a separate later design.
- Firmware or ESPHome package changes.
- Distributed MQTT locks, leases, claims, or busy announcements.
- Coordination with physical remotes or other direct MQTT publishers.
- Persisting or reconstructing calendar state across HA restart.
- Changing `repeat_gap_ms`, the `100 ms` guard, or per-entry `repeats` automatically.

## Timing contract

### Frame and train calculation

`plan_for_body()` remains the only builder of an `AirPlan`. It consumes the exact mapping that will
be serialized, including `command_id`, and applies these rules:

- `raw` must be a valid normalized B0 string.
- `repeats` must be a real integer in `1..20`; booleans are invalid.
- `trailer_raw`, when present, must be a valid normalized B0 string.
- A timed plan must have both a valid `stop_raw` and a real integer `stop_after_ms` in
  `1..3_600_000`. Having only one makes the body unplannable.
- A non-timed body must not acquire a future reservation merely because an unrelated malformed
  STOP field is present.
- Any estimator or schema failure returns `None`. The normal publish path remains authoritative
  for command validity; arbitration publishes immediately and records a fail-open rather than
  guessing.

For a plan with repeats `R`:

```text
action_ms = R * slot(raw) + (trailer_raw present ? R * slot(trailer_raw) : 0)
stop_ms   = timed ? R * slot(stop_raw) : 0
```

Production values at `R = 3` are:

| Shape | Train time before cross-bridge guard |
|---|---:|
| action only | `3 * 609 = 1,827 ms` |
| action + trailer | `6 * 609 = 3,654 ms` |
| timed STOP family | `3 * 609 = 1,827 ms` |

### Half-open intervals

All comparisons use half-open intervals. Adjacent intervals are safe.

For candidate actual/predicted start `s`, action length `A`, STOP offset `D`, STOP length `T`, and
guard `G = 100 ms` (`s` is monotonic seconds; plan lengths are integer milliseconds):

```text
immediate = [s, s + (A + G) / 1000)

future_stop = [s + (D - G) / 1000,
               s + (D + T + G) / 1000)  # timed commands only
```

The future STOP has guard on both sides. The immediate train has guard only after its predicted
end; no pre-guard is needed because feasibility already puts its start after prior occupancy.

Two intervals `[a0, a1)` and `[b0, b1)` intersect exactly when:

```text
a0 < b1 and b0 < a1
```

### Deadline inside the command's own train

When `D <= A`, the firmware STOP becomes due before or exactly when the predicted action/trailer
train ends. The bridge may finish the local frame already handed to the EFM8BB1, then prioritizes
STOP and preempts remaining normal phases. The calendar must not create a separate future window
that another command could be placed between.

Treat the immediate interval as the union:

```text
immediate = [s, max(s + (A + G) / 1000,
                    s + (D + T + G) / 1000))
future_stop = none
```

This is conservative because `A` still includes action/trailer copies that firmware may preempt.
It is intentionally deterministic and never undercharges the continuous action-to-STOP sequence.

## Arbiter data model

`air.py` replaces `ShadowStats` and the shadow-only fields in `AirArbiter` with the following
bounded model. Names below are normative; private spelling may add a leading underscore.

### `AirMode`

```text
AirMode.ENFORCE = "enforce"
AirMode.SHADOW  = "shadow"
```

### `PendingAirPlan`

One record per payload actually handed to the publisher but not yet correlated with `started`:

```text
key: (bridge_id, command_id)
bridge_id: str
boot: int | None                 # bridge info snapshot at publish
plan: AirPlan
published_at: float              # monotonic, diagnostic only
expires_at: float                # published_at + ack_timeout + started_timeout
is_stop: bool
```

This is a start-unknown blocker for other bridges, not an occupancy interval anchored at
publication. A candidate cannot schedule through it before `started` or its finite expiry. The map
is capped at 256 entries. On exhaustion the new command still publishes without a provisional
record, increments `fail_opens` and `reservation_evictions`, and logs a warning.

The normal worker ordinarily permits at most one normal pending start, but fast-lane STOP tasks can
also be pending. The explicit cap makes the bound independent of caller behavior.

### Current drain holds

Use `drain_until_by_bridge: dict[str, float]`, capped by the registry's 256-bridge limit. A
confirmed immediate train or displaced STOP drain extends, never shortens, that bridge's value.
Natural pruning removes entries with `end <= now`.

A normal candidate for bridge X considers the maximum drain end for every bridge other than X.
It ignores X's own value because X's firmware scheduler already serializes its handoffs. A started
STOP may extend this hold for later normal work even though that STOP itself bypassed the gate.

### Future STOP reservations

`reservations` is a list sorted by:

```text
(starts_at, ends_at, bridge_id, command_id)
```

Each `AirReservation` contains:

```text
bridge_id: str
command_id: str
boot: int | None
starts_at: float                 # actual_start + stop_after - guard
ends_at: float                   # actual_start + stop_after + stop_ms + guard
stop_ms: int
```

The key is `(bridge_id, command_id)`. The list is capped at 256. Insertion uses `bisect`; removal
may scan the bounded list. Before every read, mutation, or stats snapshot, prune reservations with
`ends_at <= now`.

When full, retain the 256 reservations with the nearest `starts_at` values. If a nearer new entry
arrives, evict the farthest; otherwise decline the new entry. Either case increments
`reservation_evictions` and `fail_opens`, logs a warning, wakes waiters, and never rejects the RF
command.

Reservations from the candidate's own bridge do not block it. The local scheduler prevents that
bridge from transmitting two frames at once and gives its due STOP priority.

### Wake generation

The arbiter owns a rotating `asyncio.Event`:

1. A synchronous feasibility calculation returns both its earliest time and the current event.
2. Any state change calls `event.set()` and replaces it with a new `asyncio.Event`.
3. Every waiter holding the old snapshot wakes. A change between calculation and `await` cannot be
   lost because it sets the exact event returned with that calculation.

Do not use `get_event_loop()`, store an event loop, create timer tasks per reservation, or poll.
Waiting uses `asyncio.wait_for(event.wait(), timeout=...)` with a relative timeout derived from the
monotonic clock. `CancelledError` is never swallowed.

## Earliest-feasible-time algorithm

The calculation is synchronous and side-effect-free except for natural expiry pruning and shadow
statistics. Inputs are `bridge_id`, final `AirPlan`, `now`, and the command's absolute hold ceiling.

1. If fewer than two bridges are online, return “publish now / OFF” without calculating a shadow
   wait.
2. If mode is shadow, compute the same candidate time for stats but return “publish now.”
3. Start `s` at `now`.
4. For every non-expired pending plan belonging to another bridge, the start is unknown. Return a
   wait decision whose next deadline is the earliest such `expires_at`. A `started`, rejection,
   timeout, cancellation, boot change, close, or online-count change normally wakes it earlier.
5. Set `s = max(s, drain_until)` over every other bridge's current drain.
6. Build the candidate immediate interval and optional future STOP interval at `s`, applying the
   own-train union rule.
7. Scan sorted reservations for other bridges:
   - If a reservation intersects the candidate immediate interval, require
     `s >= reservation.ends_at`.
   - If a reservation intersects the candidate future STOP interval, require the candidate STOP's
     guarded beginning to move to the reservation's end:

     ```text
     s >= reservation.ends_at - (D - G) / 1000
     ```

     `s`/reservation timestamps are seconds; `D` and `G` are integer milliseconds.

8. Apply the maximum required shift, reapply current-drain constraints, rebuild the candidate
   intervals, and scan again until no conflict remains.

`s` only increases. Once either candidate interval has moved beyond a particular reservation it
cannot conflict with that reservation again. With 256 entries, cap the defensive loop at 513
passes (`2 * cap + 1`). Reaching that impossible-under-the-monotonic-proof bound is an internal
fail-open: log, increment `fail_opens`, and publish now.

If the earliest result is already `<= now`, publish immediately. Otherwise return the result and
the current wake event. The wait timeout is the smaller of:

- `earliest - now`, and
- the remaining absolute hard-wait ceiling.

After an event or timeout, the hub discards the decision, rebuilds the final body again, and
recomputes from current state. No feasibility result is reused across an `await`.

## Provisional-to-actual replacement

### At publish commit

Immediately before handing the final payload to the publisher, add its `PendingAirPlan`. Do not
advance an actual drain horizon or insert its future STOP reservation yet. The pending record
blocks later normal work on other bridges until RF start is known.

The pending record and command ledger registration happen before the publisher coroutine is
scheduled. Therefore a synchronous fake publisher can deliver `accepted` and `started` without
racing ahead of correlation state.

### At correlated `started`

`handle_status()` already receives the status on the HA event-loop thread. `_handle_started_status`
must calculate two independent timestamps from the same decoded payload:

1. Existing cover-model timestamp: wall receipt `_now()` with current optional bridge-clock
   projection behavior.
2. Air timestamp:

   ```text
   air_started_at = monotonic_now() - valid_age_ms / 1000
   ```

For air, a valid age is a non-boolean integer in `0..7_200_000`, matching the current replay bound;
missing or invalid age is zero. Never substitute wall time, the bridge's `t`, or a projected bridge
clock value into the air calendar.

Call `AirArbiter.started(bridge_id, command_id, air_started_at, boot, now)` synchronously before
resolving `pending.started`. It atomically:

1. removes the `PendingAirPlan`;
2. creates or extends the actual current drain interval, using the own-train union rule;
3. inserts the future reservation when the STOP lies after the immediate train;
4. drops either actual interval if it has already ended by receipt time;
5. counts each pairwise overlap between a newly inserted future STOP window and an existing
   other-bridge reservation as one `stop_window_conflicts`; and
6. wakes all calendar waiters.

The status `boot` is preferred for the reservation when it is a strict uint32. Otherwise use the
valid boot snapshot captured in `PendingAirPlan`. If neither exists, keep `boot=None`; the entry is
still finite and can be removed by lifecycle status or natural expiry.

This is the required HIGH2 fix: publication time is never retained as the actual horizon anchor.

### Accepted but `started` never arrives

Keep the existing `started_timeout` caller behavior. In `_async_execute()`'s `finally`, remove any
remaining pending air plan for the command on every exit.

- A correlated rejection removes the provisional record and wakes waiters; rejection proves no
  RF start, so it is not an arbitration fail-open.
- An immediate publish failure does the same.
- `accepted` followed by `started_timeout` removes the record, increments `fail_opens`, logs one
  warning, and wakes waiters. The next command may publish even though the bridge could have keyed
  RF without reporting it. This uncertainty is preferable to an indefinite lock.
- A late `started` after `_pending` and the provisional plan were removed is uncorrelated and
  ignored, matching current status behavior.
- The provisional record also has the finite `published + ack_timeout + started_timeout` expiry.
  If normal cleanup fails to run, feasibility pruning removes it at that bound and records the
  same fail-open once.

## Reservation lifecycle

Every path below is mandatory and must have a direct pytest assertion.

| Event | Pending plan | Future reservation | Current drain | Wake behavior |
|---|---|---|---|---|
| Rejected | Remove | None exists | Unchanged | Wake all |
| Immediate publish error | Remove | None exists | Unchanged | Wake all |
| `started` timeout | Remove; count fail-open | None exists | Unchanged | Wake all |
| Displaced before start | Remove | None exists | Unchanged | Wake all |
| Displaced after timed start | Already absent | Remove victim; add `now .. now + stop_ms + G` on victim bridge | Extend | Wake all |
| `disarmed` | Remove if still pending | Remove matching reservation | Do not shorten a frame already handed to RF | Wake all |
| Changed valid boot | Remove old-boot records for bridge | Remove old-boot reservations | Let short current drain expire naturally | Wake all |
| Info tombstone / missing boot | Keep | Keep | Keep | Wake only if other state changed |
| Bridge `offline` | Keep | Keep | Keep | Recompute online count; wake all |
| Natural expiry | Remove when `ends_at <= now` | Remove | Remove expired drain | Waiter's deadline or next access recomputes |
| Selective entry unload | Cancel held unpublished command; it has no pending plan yet | Keep already-started hardware state | Keep | Wake held command |
| Final hub close | Clear all | Clear all | Clear all | Set current event before task cancellation |
| Caller cancelled while held before publish | None exists | None exists | Unchanged | Wake; final live-future check prevents publish |
| Caller cancelled after publish | Keep; worker owns the published lifecycle | Keep if later started | Keep | Normal lifecycle wake |
| Worker/fast execution task cancelled after publish while hub remains live | Remove; count fail-open because RF start is uncertain | Keep an already-confirmed reservation | Keep confirmed drain | Wake all |
| Task cancelled after correlated start | Already absent | Keep until lifecycle cleanup/expiry | Keep | Normal state-change wake already occurred |
| Online count drops below 2 | Keep | Keep | Keep | Wake every waiter; each publishes OFF/fail-open |

For displacement, use monotonic receipt `now`; `displaced` has no age. If the victim has a future
reservation, removal **always** converts its `stop_ms` to the current drain hold. This models the
firmware moving owed STOP copies into `flush_stops_`. It is independent of whether the command's
caller has resumed from `started` yet.

Selective unload must extend `drain_owner()` to cover an `_inflight` command that is still in an
air wait and has not published. Resolve its owner futures as `superseded`, set/wake its air wait,
and rely on the final under-lock live-future check to prevent publication. Never erase a confirmed
reservation merely because its config entry unloads; the bridge still owns that STOP.

Caller cancellation follows the current ownership split: callers await per-command futures, while
the hub worker/fast task owns a payload after enqueue. Cancelling a caller after publication cannot
retract RF and therefore must not discard its provisional calendar state. The worker continues to
`started` or timeout. Execution-task cancellation is a separate defensive cleanup path; final hub
close clears the whole calendar after waking waiters.

`notify_bridge_change()` becomes the single hub hook for availability/info effects. It first gives
the arbiter a fresh immutable bridge snapshot and monotonic `now`, allowing boot cleanup and
online-count transition handling, then invokes entity listeners as today. Production MQTT
handlers already call it after registry mutation. Tests that mutate the registry while a command
waits must call it too.

## Exact `ZemismartHub` enforcement hook

### Constructor and clocks

Add these keyword-only constructor inputs:

```text
air_mode: AirMode = AirMode.ENFORCE
monotonic_now: Clock = time.monotonic
```

Keep `_now` for wall-clock model/state-sync behavior. Store `_monotonic_now` separately and pass it
to `AirArbiter`. Do not store an asyncio loop.

### Replace `_observe_air`, do not gate earlier

Remove the shadow-only `_observe_air()` call from `_finalize_and_publish()`. Planning before
`_async_pop()` or `_coalesce_queued_movements()` is prohibited because the body may still merge.
Planning only in the early `_async_execute()` rebuild is also prohibited because a contributor can
cancel while the command waits for `_publish_lock` or the air calendar.

Refactor `_ordered_publish()` into a retry loop with this exact order:

1. Existing `_async_run_direct()` has already awaited every snapshotted publication barrier with
   no queue or publish lock held.
2. Acquire `_publish_lock`.
3. Preserve the existing one scheduling yield that lets ready cancellation/physical-press
   callbacks run before the authoritative final check.
4. Recheck live futures, then call `_rebuild_from_live_contributors()`.
5. Re-run `_raise_if_overlap_displaced()` and `_raise_if_press_displaced()`.
6. Copy `command.body`, add `command_id`, and call `plan_for_body()` on that final copy.
7. For STOP, run only the synchronous conflict probe used to increment `stop_bypasses`, skip
   feasibility unconditionally, and proceed to step 9. The probe's result can never return a wait.
8. For normal work, synchronously calculate feasibility:
   - If it must wait and the hard ceiling has not expired, capture the returned event/deadline,
     mark the command as air-waiting, release `_publish_lock`, await the event/timeout, clear the
     marker, and restart at step 2.
   - If OFF, shadow, unplannable, ceiling-expired, or an arbitration error says fail-open, proceed.
9. Synchronously commit `PendingAirPlan` from the same final body/plan.
10. Update the final pending-status channels, serialize exactly that body, and register the command
    ledger exactly as today.
11. Start the publisher task through `_enqueue_publish()` while still holding `_publish_lock` and
    retain its one scheduling yield so paho enqueue order is fixed.
12. Release `_publish_lock`; handle immediate transport error, `_record_publish()`, and set
    `command.published` exactly as today.

There is no timer/event wait while `_publish_lock` is held. Every trip around the loop rebuilds and
replans; the committed `PendingAirPlan`, ledger entry, JSON payload, target channels, and repeats
all come from one final post-coalescing body.

The broad fail-open boundary catches `Exception` only around plan/calendar operations. It does not
catch `CancelledError`/`BaseException`, bridge resolution failures, command validation failures,
publisher errors, or existing displacement exceptions. On an arbiter exception, warn, increment
`fail_opens` if possible, and continue at step 9 without an air plan.

### Composition with publication barriers and STOP

An air-held normal command keeps its `published` event unset. Ordinary later commands remain in the
single worker queue. A later fast-lane STOP needs special handling so an existing publication
barrier cannot turn the new air wait into a STOP wait:

- If the unpublished `_inflight` command is an overlapping movement currently marked
  air-waiting, resolve all its futures `superseded`, set its `published` event, and wake the air
  event. The STOP then takes the fast lane. No earlier RF payload exists to preserve.
- If the unpublished `_inflight` command is an overlapping raw debug frame, raw semantics do not
  permit superseding it. Mark that raw command `air_bypass_requested`, wake it, and count a
  `stop_preemption` fail-open. It publishes at once; the STOP retains its existing publication
  barrier and follows immediately after paho enqueue.
- If the older overlapping raw frame is still queued, mark its `air_bypass_requested` at the same
  point where current `_async_enqueue()` places STOP directly behind it. When the raw reaches the
  worker it skips any air delay, so the STOP inherits only today's queue/publication ordering and
  never a newly introduced calendar wait.
- If an earlier overlapping command has already reached paho, retain today's barrier and bridge
  latest-command-wins behavior.
- An unrelated STOP has no overlap barrier and publishes through the fast lane immediately. It
  may wait for at most the current `_publish_lock` enqueue yield, never an air deadline.

Add explicit `_QueuedCommand` booleans `air_waiting` and `air_bypass_requested`, both defaulting to
false. They are event-loop-owned and need no separate lock.

This cannot deadlock:

- publication barriers are awaited before the air gate and without `_publish_lock`;
- air events are awaited without `_queue_ready` or `_publish_lock`;
- status, availability, cancellation, close, and STOP preemption synchronously set the event and
  do not acquire `_publish_lock`;
- `_publish_lock` is held only for final checks and the existing paho handoff yield; and
- STOP never tries to acquire or await an arbiter ownership primitive.

## Hard wait ceiling

Set `MAX_AIR_HOLD_MS = 130_000` and apply it per command from the first positive enforcing wait.
Coalescing time, publication barriers, admission waits, and `started` waits do not consume this
budget; it bounds only time imposed by air arbitration.

The value is derived from the maximum legal contiguous command shape, not the production frame:

```text
max B0 bytes                         = 260
max hex characters                  = 520
max UART time                       = ceil(520 * 5000 / 19200) = 136 ms
max RF airtime                      = 2,000 ms
firmware margin                     = 5 ms
max legal slot                      = 2,141 ms
max action + trailer, repeats 20    = 2 * 20 * 2,141 = 85,640 ms
max STOP train, repeats 20          = 20 * 2,141 = 42,820 ms
latest inside-own-train union + G   = 85,640 + 42,820 + 100 = 128,560 ms
rounded operational ceiling         = 130,000 ms
```

The legal `stop_after_ms <= 3_600_000` is a future offset, not permission to hold a command for an
hour. The scheduler shifts around its bounded STOP window. A pathological chain of many future
reservations can still compute a delay above 130 seconds; reliability degrades rather than making
HA appear hung.

At the ceiling, rebuild and validate the body one final time, publish regardless of the calendar,
increment both `ceiling_hits` and `fail_opens`, add the actual held duration to stats, and log a
warning. The command still creates a provisional plan so a later correlated `started` protects
subsequent work.

## Mode and configuration escape

Default mode is `enforce`. Add one optional, installation-wide YAML value:

```yaml
zemismart_blinds:
  air_arbitration_mode: shadow
```

Accepted values are exactly `enforce` and `shadow`; absence means `enforce`. Invalid values fail
configuration validation rather than silently choosing a safety policy. `async_setup()` stores the
validated mode under a private, domain-specific `hass.data` key; `_create_domain_runtime()` passes
it into the one domain hub. A restart is required to change mode.

Do not put the value in remote config-entry data or options. Multiple remote entries share one hub,
so per-entry values could disagree about a domain-global safety mechanism. Do not add `off`:
shadow already provides unchanged publication timing while retaining evidence.

This is the only new knob. It is justified as a persistent rollback short of reinstalling a prior
version. It stays out of the config flow and normal UI so it does not become routine tuning.

In shadow mode, build provisional/actual holds and reservations and run the exact feasibility
algorithm, but never await it. Preserve `would_wait`, total, and maximum metrics. STOP behavior and
all existing command timing remain unchanged.

## Observability

Rename `ShadowStats` to `AirStats`. `air_shadow_stats()` remains the accessor name for compatibility
with Phase 1 callers and tests, but its docstring states that it now returns both shadow and
enforcement statistics. Its dictionary has these exact keys:

| Key | Meaning |
|---|---|
| `mode` | `"enforce"` or `"shadow"` |
| `planned` | Commands with one valid final plan; count once per command, not retry loop |
| `unplannable` | Commands published without a plan |
| `would_wait` | Commands that shadow mode would have held |
| `would_wait_total_ms` | Sum of first computed shadow holds |
| `would_wait_max_ms` | Maximum first computed shadow hold |
| `commands_held` | Enforcing commands that awaited the calendar at least once |
| `held_total_ms` | Sum of monotonic first-hold-to-publish durations, floored to integer ms |
| `held_max_ms` | Maximum actual held duration |
| `stop_bypasses` | STOP commands that bypassed a conflicting other-bridge hold, pending start, or reservation |
| `stop_window_conflicts` | Pairwise overlaps between actual HA-known future STOP reservations |
| `ceiling_hits` | Commands published after the 130 s ceiling |
| `fail_opens` | Arbitration degradations that permitted publication or released uncertain state |
| `reservation_evictions` | Pending/reservation cap declines or evictions |
| `disabled_single_bridge` | Commands published with fewer than two online bridges |
| `waits_by_bridge` | Held/would-held command counts keyed by selected bridge; bounded by registry bridge IDs |
| `active_reservations` | Current pruned future reservation count |
| `pending_starts` | Current pruned provisional count |
| `fail_open_reasons` | Fixed-key reason counters listed below |

`commands_held` and `waits_by_bridge` increment once when a command first receives a positive
enforcing wait. Held milliseconds are recorded exactly once on publication, supersession,
cancellation, OFF release, or ceiling release. A STOP increments `stop_bypasses` at most once.

Add a fixed-key `fail_open_reasons` mapping beneath the accessor snapshot for operational
attribution. The allowed keys are `unplannable`, `started_timeout`, `cancelled_after_publish`,
`online_below_two`, `pending_cap`, `reservation_cap`, `iteration_bound`, `ceiling`,
`stop_preemption`, and `internal_error`. Each reason increment also increments `fail_opens`
exactly once, so the mapping's sum equals `fail_opens`; one command may contribute more than one
independent degradation.

Add `custom_components/zemismart_blinds/diagnostics.py` with config-entry diagnostics returning:

```text
{"air_arbitration": hub.air_shadow_stats()}
```

The scope is domain-global and therefore the same in every loaded remote entry's diagnostic
download. Do not include command IDs, targets, raw frames, config-entry data, or new entity state
attributes. Counters reset when the final domain hub is closed or HA restarts.

Logging uses the existing `air:` prefix:

- INFO once at hub creation: mode, `100 ms` guard, `130,000 ms` ceiling, and cap `256`.
- DEBUG once when a command first holds and once when it publishes/releases, including bridge and
  duration but not raw payload.
- WARNING for ceiling, cap degradation, started-timeout fail-open, stop-preemption of a held raw
  frame, and internal arbiter failure.
- No per-reservation-expiry log and no new log for a normal successful plan.

## Scene latency budget

The production action-only train is:

```text
3 repeats * 609 ms = 1,827 ms
cross-bridge start spacing = 1,827 + 100 = 1,927 ms
```

For approximately 12 trains after safe coalescing from a 16-entity whole-house scene, the first
starts immediately and 11 preceding gaps determine the last start:

```text
action-only last start = 11 * 1,927 = 21,197 ms = 21.197 s
action-only final train end = 21,197 + 1,827 = 23,024 ms
```

A trailer adds another complete three-repeat family, `1,827 ms`, to that command. General last
start is:

```text
21,197 ms + 1,827 ms * (number of trailer-bearing commands among the first 11)
```

Worst case, all first 11 carry trailers:

```text
last start = 11 * (3,654 + 100) = 41,294 ms = 41.294 s
```

If the final command also has a trailer, the scene's final predicted train ends at:

```text
41,294 + 3,654 = 44,948 ms = 44.948 s
```

Future STOP reservations can add delay beyond this simple scene budget; the 130-second absolute
air hold remains the final bound. The latency is last-start/final-train timing, not HA service-call
completion time, and it assumes statuses arrive normally.

## STOP behavior, precisely

There are three STOP paths, and none acquires an air lease:

1. **Explicit HA STOP.** `_async_enqueue()` retains the fast lane and current overlapping
   publication-order semantics. `_ordered_publish()` performs a nonblocking conflict probe for
   stats, commits a provisional plan, and enqueues without an air wait. Its correlated `started`
   replaces the provisional record with a STOP-train drain hold so later normal work waits.
2. **Firmware fail-safe STOP.** HA's future reservation prevents avoidable normal starts across its
   predicted window, but HA does not publish or acknowledge the STOP at the deadline. The owning
   `TargetScheduler::next()` promotes it locally and transmits at the first tick permitted by that
   bridge's own `next_rf_at_`, ahead of normal phases. Broker, HA, peer availability, calendar mode,
   and the hard wait ceiling cannot delay it.
3. **Displaced fail-safe STOP.** Firmware moves owed copies into `flush_stops_`. A correlated
   `displaced` status converts HA's victim reservation to an immediate `stop_ms + 100 ms` drain
   from local monotonic receipt. The flushed STOP remains firmware-prioritized and never consults
   HA.

If any STOP becomes due while another bridge is already transmitting, both local schedulers keep
their safety behavior and the frames may collide. Do not cancel the normal owner's current hold,
delay either STOP, or claim exclusive air ownership. `stop_bypasses` and
`stop_window_conflicts` make the residual risk visible; they do not alter dispatch.

## Failure and edge behavior

| Situation | Required result |
|---|---|
| HA restart | Calendar is empty; first command publishes immediately. Existing bridge STOPs are orphaned from HA by design. |
| Broker loss before enqueue | Existing publisher error; provisional record removed; next command cannot be locked out. |
| Broker loss after enqueue | Pending start expires through normal ack/started timeout; fail open. Bridge retains local scheduler safety. |
| Bridge offline with reservation | Reservation survives. If online count is below two, waiters publish OFF; state remains useful if a second bridge returns before expiry. |
| Retained stale `online` | May create a finite conservative delay, never a STOP delay; hard ceiling applies. |
| Changed boot | Remove old future/pending state. Do not remove another bridge's reservations. |
| Missing/malformed plan | Publish immediately, increment `unplannable`, `fail_opens`, and reason `unplannable`. |
| Reservation cap | Keep nearest deadlines, degrade farther future state, publish, count/log. |
| STOP due inside another train | Firmware transmits at its first locally safe tick. HA never delays it; RF collision remains possible. |
| Explicit STOP during a hold | Fast lane; later normal work accounts for its drain after correlated `started`. |
| Physical remote | Uncoordinated; state sync behavior remains unchanged. |
| Wall-clock/NTP step | No effect on wait or reservation times. |
| One online bridge | No enforced wait. Existing publish/status behavior is byte- and timing-equivalent. |
| Final unload | Wake state first, then existing task cancellation clears all RAM calendar state. |

## Implementation changes by file

### `custom_components/zemismart_blinds/air.py`

- Update the module docstring from shadow-first wording to enforce-by-default wording while keeping
  the three invariant statements.
- Add `AirMode`, `PendingAirPlan`, `AirReservation`, `AirStats`, constants for cap and ceiling, and
  interval helpers.
- Tighten `plan_for_body()` to the complete legal plan rules above.
- Replace `observe()` with synchronous decision/commit/lifecycle methods and rotating-event wait
  support. Remove `observe()`; it is an internal Phase 1 API with no production caller after the
  hub hook changes.
- Keep all collection/state mutations on the HA event-loop thread. No lock is needed inside the
  arbiter because no method awaits while touching state.

### `custom_components/zemismart_blinds/models.py`

- Inject and separate the monotonic clock.
- Extend `_QueuedCommand` with the two air-wait flags.
- Extend `_handle_started_status()` to perform the monotonic air replacement before resolving the
  existing future.
- Connect rejection/timeout/error/cancellation, displacement, disarm, bridge change, selective
  drain, and close to the lifecycle table.
- Replace `_observe_air()`/current finalization with the retrying `_ordered_publish()` hook.
- Preserve existing ledger, affinity, state-sync, publish-barrier, and caller result semantics.

### `custom_components/zemismart_blinds/const.py` and `__init__.py`

- Add the one global config key and validated mode values.
- Pass the mode into `_create_domain_runtime()`.
- Have `notify_bridge_change()` update air state before entity listeners.
- Do not change any MQTT subscription, topic, QoS, retained flag, or payload.

### `custom_components/zemismart_blinds/diagnostics.py`

- Add the domain-scoped stats snapshot only.

### `custom_components/zemismart_blinds/manifest.json`

- Set version to `0.4.0`.

### Tests

- Expand `tests/test_air.py` for the pure interval, sorted-cap, lifecycle, stats, and ceiling model.
- Expand `tests/test_models.py` for hub scheduling and current concurrency/barrier behavior.
- Add global-mode config coverage in `tests/test_init.py` and the diagnostics snapshot contract in
  a new `tests/test_diagnostics.py`.
- Do not modify firmware tests or state-sync behavior.

## Consumer pytest mapping

Use a mutable fake monotonic callable passed to `ZemismartHub(monotonic_now=...)`. To advance a
held command without sleeping, update the fake value and call `hub.notify_bridge_change()`; this
sets the arbiter's rotating event and forces recomputation. Publishers append `(topic, body,
monotonic_now())` and deliver statuses explicitly through `handle_status()`, following current
`tests/test_models.py` idiom. Never patch wall time to drive an air wait.

The 15 consumer cases from the original design map as follows:

| Original case | Concrete pytest coverage against current code |
|---:|---|
| 1 | Replace `test_shadow_arbiter_stays_off_for_a_single_bridge_install` with `test_enforcement_is_off_for_one_online_bridge`; assert immediate publish/result timing, `commands_held == 0`, and OFF count. |
| 2 | Add `test_first_cross_bridge_command_publishes_immediately_and_completes_on_started`; two online area-routed bridges, one command, no fake advance. |
| 3 | Replace `test_shadow_arbiter_observes_cross_bridge_without_delaying` with `test_second_cross_bridge_publish_waits_from_actual_started_anchor`; report first `started` at receipt `100.250` with `age_ms=250`, assert bridge B cannot publish before `100.000 + 1.827 + .100`, then advances exactly to it. |
| 4 | Add `test_trailer_charges_all_three_repeats_before_cross_bridge_publish`; assert `3,654 + 100 ms` from actual start, not publication, and retain `test_plan_covers_action_trailer_and_stop_trains` with repeats 3. |
| 5 | Extend `test_stop_fast_lane_bypasses_unrelated_inflight_command`, `test_stop_overlapping_inflight_command_stays_ordered`, `test_stop_publishes_while_overlapping_movement_awaits_ack`, and `test_stop_queues_behind_an_overlapping_queued_raw_frame`; add `test_stop_preempts_an_air_held_movement` and `test_stop_forces_held_raw_to_fail_open_then_follows_its_publish_barrier`. |
| 6 | Add `test_normal_train_is_shifted_past_known_future_stop`; start a timed bridge-A command, advance near its reservation, and assert bridge B publishes at reservation `ends_at`. |
| 7 | Add `test_two_ha_timed_commands_get_non_overlapping_stop_reservations`; inspect the bounded snapshot/helper and assert half-open disjoint windows after the second actual start. |
| 8 | Add pure `test_externally_forced_stop_reservation_overlap_is_counted_but_neither_stop_waits`; insert/confirm conflicting actual reservations as restart/external-state simulation, assert pairwise counter and STOP bypass. |
| 9 | Add parameterized `test_pending_air_plan_is_released_on_prestart_terminal_path` for rejection, accepted-without-started timeout, immediate publisher failure, execution-task cancellation, and final `close`; assert no pending record and a woken second command. Add `test_caller_cancellation_while_air_held_prevents_publish` and `test_caller_cancellation_after_publish_keeps_air_lifecycle_owned_by_worker` to prove both sides of current ownership. Preserve current exception/result types. |
| 10 | Extend `test_displaced_status_rewindows_confirmed_stop_echoes`, `test_started_then_displaced_broker_batch_still_rewindows_stops`, and `test_disarm_ack_keeps_displaced_stop_drain_suppressed`; assert reservation becomes `now + stop_ms + guard`, then disarm removes a still-future reservation without shortening current drain. |
| 11 | Add `test_offline_retains_reservation_but_changed_boot_removes_it`; use registry mutation plus `notify_bridge_change()`. Include info tombstone to prove missing boot is not reboot evidence. |
| 12 | Add `test_online_count_drop_wakes_air_waiter_and_publishes_off`; bridge B goes offline while bridge C waits behind A, assert immediate publish, `online_below_two` fail-open reason, and retained A reservation. |
| 13 | Add pure `test_reservation_cap_keeps_nearest_deadlines_and_fails_open`; insert 257 entries, assert length 256, stable sort, farthest eviction/decline, counters, and no TX rejection at hub level. Add the same bound for provisional starts. |
| 14 | Add `test_air_started_anchor_uses_monotonic_receipt_minus_age_only`; step wall time backward/forward and seed a disagreeing bridge clock, assert the air end is unchanged and based only on fake monotonic minus valid `age_ms`. Retain existing wall-model projection tests. |
| 15 | Extend `test_cancelled_contributor_is_rechecked_after_publish_lock` with contributors having different repeats; cancel the high-repeat contributor during an air wait, then assert the final payload, ledger, and `PendingAirPlan` all use the surviving lower repeat count. |

Additional pure tests must cover half-open adjacency, the own-train union, future-conflict shift
formula, same-bridge exemption, natural expiry, deterministic 513-pass fail-open, actual held stats,
130-second ceiling behavior, and shadow-mode no-delay behavior.

### Existing timing tests that must change

- The three Phase 1 shadow tests at the end of `tests/test_models.py` become enforce/default,
  same-bridge, and explicit-shadow tests using separate wall/monotonic clocks.
- `test_affinity_is_partitioned_by_area` currently sends consecutive commands through two online
  bridges and assumes immediate second publication. It must advance fake monotonic time through
  the enforced `action_ms + guard` while preserving its routing assertion.
- Audit every test with two online bridges. Tests whose second command routes to the same bridge,
  whose online count has dropped to one, or whose command is STOP keep present timing. Any genuine
  cross-bridge normal sequence must either assert the new gap or explicitly construct the hub in
  shadow mode when timing is irrelevant to the test's purpose.
- Existing publication-lock cancellation, physical-press displacement, final-ledger-frame,
  coalescing, fast STOP, background PUBACK, entry drain, and close tests are regression guards and
  must remain; do not weaken them to accommodate the new loop.

The baseline is 685 passing tests. All 685 existing behaviors, with only the intentional
cross-bridge timing expectation changed, plus the new tests must pass.

## Verification and acceptance

Implementation targets Python 3.14 (`pyproject.toml` requires `>=3.14.2,<3.15`; the codebase
already uses PEP 758 unparenthesized `except` clauses). Use the repository's `uv` toolchain.
Required checks, matching CI exactly, with no lint/type suppressions added anywhere:

```text
uv run pytest
uv run mypy --strict
uv run ruff check .
uv run ruff format --check .
```

Acceptance criteria:

1. No two normal HA-originated trains on different bridges overlap according to the actual-start
   calendar, except an explicitly counted fail-open.
2. No explicit STOP awaits an air event/deadline; no firmware STOP behavior changes.
3. A candidate cannot cross a known other-bridge future STOP reservation.
4. Actual horizons and reservations use monotonic receipt minus valid `age_ms`, never publication
   or wall-clock time.
5. Fewer than two online bridges wakes all waiters and changes no single-bridge publish timing.
6. Every state collection is bounded, sorted where specified, and cleared/retained according to
   the lifecycle table.
7. Every calendar wait is bounded at 130 seconds and the ceiling publishes with counters.
8. Final body, air plan, ledger entry, and MQTT payload agree after coalescing cancellation races.
9. STOP publication barriers, paho enqueue ordering, and background PUBACK behavior retain their
   current tests.
10. No new MQTT topics or fields exist; manifest is `0.4.0`; full pytest/mypy/ruff checks pass.

## Production rollout — seven bridges

### Preflight

1. Confirm all seven firmware packages still use `repeat_gap_ms: 35` and 19200-baud UART. Do not
   deploy enforcement if any bridge differs.
2. Inspect every production `zemismart_blinds` config entry's stored data. Verify `repeats` is
   explicitly `3` for every remote. The `DEFAULT_REPEATS` bump affects only newly created/defaulted
   values; existing stored per-entry values do not follow it. Use each remote's **Edit settings**
   flow to change stale values—never edit `.storage` in place.
3. Export a HA backup and record the current integration version, loaded entry count, seven bridge
   IDs/areas/boot values, and a pre-upgrade diagnostics snapshot.
4. Verify there is no routine direct publisher of `rf433/+/tx` besides this integration.

### Deploy

1. Install the `v0.4.0` custom component and initially set the installation-wide mode to `shadow`.
2. Restart HA. Confirm all seven bridges become online, the log reports shadow mode with guard and
   ceiling, and ordinary commands/STOPs retain pre-upgrade timing.
3. Exercise one known overlapping two-bridge pair. Confirm `would_wait` rises and correlated
   `started` creates/clears the expected actual state in diagnostics.
4. Remove the YAML override (or set `enforce`) and restart HA during a daytime maintenance window.
5. Exercise the same two bridges. Capture status timestamps and verify the second normal start is
   no earlier than the first actual start plus calculated train and `100 ms` guard. Send a STOP
   while another bridge is held and verify immediate fast-lane enqueue.
6. Run the full seven-bridge/approximately 12-train whole-house scene. Expect roughly `21.2 s`
   action-only last-start, up to `41.3 s` when the first 11 trains all carry trailers. Verify all 16
   entities' blinds move.
7. Leave enforcement enabled. The immediate rollback is to set `air_arbitration_mode: shadow` and
   restart HA; no firmware or config-entry rollback is required.

### First 24 hours

Take diagnostics after the first whole-house scene and again at 24 hours. Watch:

- `commands_held`, `held_total_ms`, and `held_max_ms`: nonzero during scenes and consistent with the
  latency budget;
- `would_wait*`: zero in enforcement and useful only during the shadow preflight;
- `stop_bypasses`: allowed to rise when users/automations issue STOP during known occupancy;
- `stop_window_conflicts`: expected zero for HA-originated timed work;
- `ceiling_hits`: expected zero;
- `fail_opens` and every `fail_open_reasons` value: expected zero after the shadow-to-enforce
  restart;
- `unplannable`, `reservation_evictions`, and sustained `pending_starts`: expected zero;
- `active_reservations`: may be nonzero only while timed moves retain future STOPs; and
- warnings for `air:`, bridge acknowledgement/started timeouts, MQTT publish failure, or repeated
  boot changes.

Operational acceptance after 24 hours is: no missed blind in the scheduled evening scene, no STOP
latency regression, zero ceiling/cap/internal fail-opens, zero unplanned STOP-window overlap, and
observed last-start timing within the calculated command mix plus normal status-delivery jitter.
Any safety or availability concern switches the installation to shadow first, preserving stats for
diagnosis.

## Open questions

None block `v0.4.0`. Guard tuning and conflict-graph concurrency remain explicitly deferred Phase 3
work and require new field evidence plus a separate reviewed design.
