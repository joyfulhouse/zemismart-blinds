# Cross-bridge RF433 air arbitration

**Date:** 2026-07-20  
**Status:** Proposed design; no production code is included  
**Firmware baseline:** `esphome-rf433-mqtt-bridge` v1.2.x, seven Sonoff RF Bridge R2 devices  
**Consumer baseline:** `zemismart-blinds` v0.3.x, one domain-scoped `ZemismartHub`

## Decision summary

Implement **consumer-side, safety-aware airtime gating** in the Home Assistant integration. Keep the
existing MQTT contract and firmware unchanged for the first deployment.

The domain-scoped `ZemismartHub` already orders normal commands globally, but it advances to the
next command as soon as the current command reports its first `started` status. The selected bridge
then continues its remaining action repeats and optional trailer repeats for roughly another
0.6–1.8 seconds. The proposed gate records that remaining occupancy from the actual `started`
handoff and prevents the next **normal** cross-bridge publish until it is clear. For timed commands,
it also keeps bounded, in-RAM reservations around their future fail-safe STOP windows so it does not
deliberately start a normal train that will run through a known STOP deadline.

Every STOP path is outside the gate:

- an explicit HA STOP retains the current `_async_enqueue()` fast lane;
- a firmware fail-safe STOP in `TargetScheduler::next()` never consults HA or an MQTT lease; and
- a displaced command's flushed STOPs retain their existing priority.

If a STOP becomes due while another bridge is already transmitting, the STOP transmits on the
first locally safe 5 ms scheduler tick even though the two RF frames may collide. Delaying the STOP
would be a worse and prohibited failure. The design reduces preventable collisions; it does not
claim exclusive ownership of a channel that physical remotes and disconnected peers can use.

This is deliberately **not** an MQTT distributed lock. Plain MQTT provides no atomic acquire, a
non-retained claim can be missed, a retained claim can become a fail-closed tombstone, and any
correct STOP implementation must violate the lock anyway. The dominant collision source in this
house is HA scene fan-out, and the existing shared `ZemismartHub` is the simplest place that sees
that whole workload.

## Current design and the precise gap

### Firmware

`rf433_scheduler.h` already has the correct per-bridge safety boundary:

- `normalize_b0_with_airtime()` validates a B0 frame and derives its embedded-repeat airtime.
- `TargetScheduler::schedule()` admits bounded commands, preserves absolute monotonic
  `stop_after_ms` deadlines in RAM, and applies latest-command-wins displacement.
- `Command::owes_stop()`, `displace_overlapping_()`, and `flush_stops_` preserve and prioritize an
  already-owed fail-safe STOP when a live timed command is displaced.
- `TargetScheduler::next()` promotes every due scheduled STOP before normal work, rotates displaced
  STOPs, alternates them with due scheduled STOPs, and then round-robins normal action/trailer work.
- `record_dispatch_()` is the single owner of local pacing. It holds the next UART handoff for the
  larger of `repeat_gap_ms` and UART serialization plus derived RF airtime plus a 5 ms margin.
- `rf_air_clear()` exposes the same busy horizon to the receive-mode reconciler.
- `due_()` uses `static_cast<int32_t>(now_ms - deadline_ms) >= 0`; the age-based reset of
  `next_rf_at_` prevents the historical half-range rollover stall.

The 5 ms interval in `rf433-mqtt-bridge.yaml` calls `tx_scheduler(...).next()`, hands the returned
frame to `RFBridgeComponent::send_raw()`, and publishes `started` only for the command's first
ACTION dispatch. `send_raw()` serializes the whole B0 command at 19200 baud and flushes the UART;
there is no EFM8BB1 completion acknowledgement. Thus `started` proves handoff, not reception or RF
completion.

Wi-Fi, API, and MQTT reboot timeouts are intentionally disabled. The 10 s watchdog may reboot only
after 15 minutes without MQTT **and** when `TargetScheduler::idle()` and `rf_air_clear()` are true.
That preserves RAM-held STOP deadlines during a broker outage. Cross-bridge arbitration must not
weaken this property.

### Consumer

There is already one `DomainRuntime` and one `ZemismartHub` shared across all remote-centric config
entries. `BridgeRegistry.resolve()` selects an online same-area, default, or fallback bridge, while
`_resolve_with_affinity()` keeps follow-up commands on the bridge that owns the remote's live
scheduler state.

Normal commands pass through `_async_worker()` and `_async_execute()`. `_ordered_publish()`
serializes only the synchronous paho enqueue, and `_async_execute()` waits for `accepted` and then
the first `started`. After that, the global worker pops the next command even though the first
bridge may still be transmitting its remaining repeats and trailer frames.

STOP is intentionally different. `_async_enqueue()` removes queued overlapping movements and sends
STOP through `_async_run_fast()` after only earlier overlapping **publication** barriers. It never
waits behind another command's acknowledgement lifecycle. `ZemismartCover._async_stop()` freezes
the model only at the correlated STOP handoff. This safety path must remain a bypass.

`_coalesce_queued_movements()` already combines eligible single-channel, same-remote, same-area
full moves within the configured 150 ms window. That is useful load reduction, but it does not
combine different remotes or areas, and it does not prevent simultaneous transmissions on distinct
bridges after the first `started` status.

`state_sync.py`'s `_LEDGER_FRAME_AIRTIME_MS = 2_000` is a deliberately broad RX classification
window. It is not an occupancy calculation and must not be reused for arbitration.

## Quantified collision model

### Frame and train occupancy

For a normalized B0 frame, define the same slot calculation used by firmware:

```text
airtime_us = embedded_repeat * sum(bucket_us[pulse_nibble & 0x07])
uart_ms    = ceil(hex_character_count * 5000 / 19200)
air_ms     = ceil(airtime_us / 1000)
frame_slot_ms = max(repeat_gap_ms, uart_ms + air_ms + 5)
```

The UART formula is equivalent to `(hex_chars / 2 bytes) * 10 bits / 19200 baud`, rounded up.
Production AOK frames are about 550–560 ms on air and about 43–45 ms on the UART, so one local
handoff slot is about 598–610 ms. With consumer `repeats: 2`:

| Immediate command shape | Approximate occupied interval from first handoff |
|---|---:|
| action only, 2 repeats | 1.20–1.22 s |
| action + trailer, 2 repeats each | 2.39–2.44 s |
| action + later timed STOP | 1.20–2.44 s now, then about 1.20–1.22 s at the deadline |

The embedded Portisch repeat of 8 is already inside each 550–560 ms frame. Two scheduler-level
repeats therefore put about 16 packet repetitions on air, but they are not independent trials when
two bridges begin together. Both bridges use almost the same frame length and local pacing, so a
10–50 ms MQTT arrival skew leaves roughly 91–98% of each 550 ms burst overlapped and preserves
nearly the same phase relationship for the second scheduler repeat.

An AOK receiver's code filter is downstream of RF interference. It prevents a valid frame for the
wrong remote from operating the blind; it does not make the wrong frame transparent while the
desired frame is being demodulated. Capture effect may let the stronger transmitter win, but that
is a geometry-dependent benefit, not a coordination mechanism.

### Independent traffic

Let:

- `C` be fleet-wide HA command starts per active day;
- `H` be the active-day duration in seconds;
- `T` be the command train duration, approximately 1.2 s without a trailer;
- `B = 7`; and
- `q` be the probability that a randomly selected other bridge is strong enough at the target
  receiver to matter.

For approximately Poisson, independent starts, the probability that one command has an audible
other start in its two-sided vulnerable interval is:

```text
p_exposure ~= 1 - exp(-2 * T * (C/H) * ((B-1)/B) * q)
```

At 20–50 commands over a 16-hour active day, `T = 1.2 s`, and `q = 0.25–0.5`, this predicts only
about **1–8 independently timed audible overlap pairs per year**. Trailers roughly double that
range. The estimate is intentionally coarse: there is no blind acknowledgement or RF power
telemetry with which to fit `q` or the conditional decode-failure rate.

### Scene and automation fan-out

The realistic exposure is correlated traffic, not Poisson traffic. For a scene addressing `K`
bridges within the 10–50 ms MQTT window, collision exposure is effectively deterministic on every
RF-overlap edge. For a particular target, the probability of at least one audible interferer is:

```text
p_target_exposed = 1 - product(1 - q_ij)
```

With `K = 7` and a uniform illustrative `q`:

| `q` | Probability a target has at least one audible interferer |
|---:|---:|
| 0.25 | 82.2% |
| 0.35 | 92.5% |
| 0.50 | 98.4% |

Exposure is not identical to a missed command. If `d` is the conditional probability that an
audible overlap defeats capture, an illustrative target miss model is
`1 - (1 - q*d)^(K-1)`. Across `q*d = 0.0625, 0.175, 0.375`, that ranges from about 32% to 94%.
Those are sensitivity bounds, not measurements. They explain why a stable installation can still
fail conspicuously during a scene: geometry may protect many targets, but the repeats do not turn a
synchronous collision into 16 independent chances.

The actual user-visible damage is asymmetric:

- For a full open/close, one or more blinds simply do not move. HA still receives `started` and
  advances its assumed model, so the UI can show the endpoint even though the blind missed RF.
- For a timed partial move, a missed ACTION means no movement; a received ACTION plus a collided
  fail-safe STOP can run to the motor limit. The latter is less frequent but causes larger state
  error and is why STOP must never wait for arbitration.
- A physical remote press can collide with any system frame and remains irreducible.

If a sunset scene spans several bridges daily, there are roughly 365 high-exposure batches per
year, which dominates the single-digit background estimate. The problem is worth fixing for that
workload. If field usage contains no multi-bridge fan-out and misses are not observed, the same
model supports deferring the change.

## Alternatives

### A. Consumer-side central arbitration

The HA integration knows every command it originates and already has a domain-global queue. It can
derive each frame's occupancy, anchor the hold on the existing `started` status, and delay only the
next normal publish. No broker protocol or firmware rollout is required.

Strengths:

- The first command on an idle channel publishes exactly as today; its caller still completes on
  `started`. Only later contending commands wait.
- A one-bridge installation takes the existing code path with no gate and no latency change.
- Broker loss cannot strand a distributed lease. Bridges continue draining and firing STOPs.
- HA restart simply loses advisory state and fails open.
- No ESP8266 RAM, peer table, clock synchronization, or new 5 ms tick work.
- It directly covers the dominant HA scene/aggregate fan-out.

Limitations:

- It cannot coordinate other MQTT publishers, physical remotes, or an orphaned STOP whose command
  predates an HA restart.
- It duplicates the firmware's B0 occupancy calculation and therefore needs contract vectors.
- Serializing all seven bridges makes the last blind in an action-only scene begin roughly
  `6 * (1.2 s + guard)`, about 7.8 s after the first. A trailer can raise that to roughly 15 s.
- STOPs intentionally break exclusivity.

This is the recommended family, augmented with future STOP reservations as specified below.

### B. Distributed MQTT-mediated claims

A plausible design would have a bridge publish a non-retained intent, wait one or two MQTT
round-trip bounds, choose a deterministic winner, and let losers back off. Relative durations at
receipt can avoid synchronized clocks. It is not a mutex:

1. Two bridges can publish and dispatch before either receives the other's intent unless every
   normal command pays a contention window. With the stated 10–50 ms round trip, a conservative
   settle is at least 100 ms plus scheduler jitter.
2. MQTT ordering is per client publication stream; plain MQTT offers no compare-and-set for a
   shared lock. Different subscribers can act on incomplete claim sets.
3. A non-retained claim is missed by a reconnecting peer. A retained claim can outlive the owner
   and create exactly the fail-closed behavior prohibited here. MQTT LWT availability does not
   atomically revoke a separate retained lease, and a broker-only disconnect does not mean a
   bridge's RAM-held STOP disappeared.
4. In the historical stale-conntrack failure, the disconnected bridge would neither see claims nor
   publish its own, but it could still fire a local STOP. Connected peers cannot distinguish that
   from a powered-off bridge.
5. A due STOP must ignore any winner or lease, so the protocol's exclusivity promise is false at
   the most safety-critical moment.

A bounded fail-open timeout makes this safe enough as an advisory optimization, but after adding
the timeout, race handling, boot/sequence IDs, fixed peer state, and mixed-version rollout, it is
more complicated than the HA gate while covering little of the stated trigger. Reject for the
initial fix.

### C. Hybrid HA pacing plus bridge advisory busy signals

HA pacing handles its own bursts. A bridge could additionally publish a non-retained advisory such
as current-busy duration or a future STOP intent, and peers could defer normal `next()` results.
STOP would bypass it.

This covers direct publishers and queues that outlive the controller, but a `busy` signal emitted
at dispatch arrives too late to stop simultaneous dispatches. Publishing before dispatch either
reintroduces the distributed contention window or remains racy. Future STOP intents are more
valuable, but require bounded per-peer reservation state on every 30–40 KB ESP8266, boot/sequence
handling, expiry under `millis()` rollover, and careful mixed-version semantics.

The hybrid is a reasonable later escalation if measurements show material collisions from sources
outside `ZemismartHub`. It is rejected now because it adds a fleet firmware protocol without being
needed for the dominant HA fan-out.

### D. Do nothing or apply a partial operational measure

Do nothing is defensible only if multi-bridge commands are genuinely rare. Random independent
traffic has low expected overlap, zone placement and capture effect help, and the current 16 on-air
packet repetitions may mask some collisions.

Cheaper partial measures are:

- express one multi-channel AOK group frame instead of many leaf commands where the physical
  remote topology allows it;
- increase the existing HA coalescing window only for commands that `_coalesce_queued_movements()`
  can safely union;
- manually stagger the one known sunset automation; or
- test `repeats: 1` in a controlled zone, which approximately halves occupancy but removes the
  second time-diverse scheduler window.

Manual scene delays capture much of the benefit for one automation, but they do not compose across
automations and services. Reducing repeats changes RF reliability and is not recommended without
field data. Given a recurring whole-house scene, retain these as load reduction, not the primary
design.

### Constraint comparison

| Family | STOP never waits | Broker/peer failure opens | Physical remotes acknowledged | ESP cost | Single-command fast path | Dominant HA fan-out |
|---|---|---|---|---|---|---|
| A: HA airtime gate | Yes, explicit bypass; fail-safe remains local | Yes; no lease | Yes; cannot coordinate them | None | Immediate when idle; disabled for one bridge | Strong |
| B: MQTT claims | Only if STOP violates the claim | Only with bounded timeout; partition races remain | Yes | Fixed peer/claim state | Pays settle time in multi-bridge mode | Strong after settlement |
| C: hybrid advisory | Yes, if advisory only | Yes, with expiry | Yes | Fixed peer/reservation state | Immediate if no preclaim | Strong via HA, partial elsewhere |
| D: no change | Existing behavior | Existing behavior | Existing behavior | None | Existing behavior | Poor |

## Recommended design: HA airtime calendar

### Scope and invariants

Add a small, process-local `AirArbiter` owned by the domain-scoped `ZemismartHub`. It is advisory
and has these non-negotiable invariants:

1. `is_stop` commands never await it. They may update its later normal-busy horizon after they
   report `started`, but they do not wait before publish.
2. Firmware fail-safe and displaced STOP selection remains entirely inside
   `TargetScheduler::next()`.
3. If fewer than two bridges currently report `online`, the arbiter is OFF and behavior is exactly
   the current behavior. Waiters wake immediately when the count drops below two.
4. The first normal command while the calendar is clear publishes immediately. The command's
   caller is not held until its repeat train completes; only the next contending publish waits.
5. All timestamps in HA are local event-loop monotonic time. No bridge clocks are compared.
6. All state is bounded, finite-lived, and in RAM. Missing or invalid state causes a warning and an
   immediate publish, never a refusal.

### Exact occupancy calculation

The consumer adds an `estimate_b0_slot_ms()` helper whose accepted input is already normalized by
`validate_b0_frame()`. It implements the same bucket parser and formula as
`normalize_b0_with_airtime()` and `record_dispatch_()`:

- embedded repeat: B0 byte index 4, legal range 1..16;
- bucket table: 1..8 unsigned 16-bit microsecond durations;
- every data nibble contributes the duration of `nibble & 0x07`;
- `airtime_us` is the nibble sum times embedded repeat;
- `air_ms = ceil(airtime_us / 1000)`;
- `uart_ms = ceil(hex_chars * 5000 / 19200)`;
- `slot_ms = max(35, uart_ms + air_ms + 5)` for this deployed fleet.

The parser must reject rather than guess if the normalized frame differs from the firmware
contract. The publish itself still follows today's validation/error path; arbitration failure is
fail-open and does not create a new command rejection.

For a command with consumer `repeats = R`:

```text
action_train_ms = R * slot(raw) + (trailer_raw present ? R * slot(trailer_raw) : 0)
stop_train_ms   = stop_raw present ? R * slot(stop_raw) : 0
```

Use a cross-bridge guard of **100 ms** after predicted train ends and on both sides of a future
STOP window. It is derived from the observed 10–50 ms MQTT round trip/delivery scale, one 5 ms
firmware tick, event-loop scheduling, and rounding; doubling the 50 ms upper typical value is a
simple conservative starting point. The firmware's own 5 ms margin remains in every frame slot.
Record actual delay metrics so 100 ms can be tightened or widened from p99 field behavior.

The current fleet uses `repeat_gap_ms: 35`. A bridge configured with a larger non-default gap is
outside this v1 consumer calculation. Either freeze that deployment value or make scheduler timing
metadata an explicit future `/info` contract revision; do not silently assume arbitrary overrides.

### State

The HA object holds:

| State | Bound | Purpose |
|---|---:|---|
| `normal_busy_until` | one monotonic timestamp | End of the most recently started immediate train plus guard |
| `awaiting_started` | at most one normal command | Prevent a second normal publish between first publish and actual handoff |
| `reservations` | 256 entries | Future fail-safe STOP windows keyed by `(bridge_id, command_id)` |
| `plan_by_pending_id` | bounded by existing `_pending` | Occupancy plan awaiting correlated `started` |

Each reservation contains `bridge_id`, `command_id`, `boot`, `starts_at`, `ends_at`,
`stop_train_ms`, and `expires_at`. Expiry is the stop-window end plus 100 ms. The cap matches the
consumer command ledger's global cap. If full, retain the nearest deadlines and evict the
farthest-future reservation; log and continue. Only a correlated local command can allocate one,
so arbitrary MQTT status traffic cannot grow this table.

Availability `offline` alone does **not** erase a reservation. A bridge disconnected only from the
broker still owns and can fire its RAM-held STOP. A new `/info` `boot` value proves a reboot and
removes reservations belonging to the old boot. Normal expiry, `disarmed`, and applicable
`displaced` lifecycle events also retire or transform state.

### State machine

```text
OFF (<2 online bridges)
  normal command -> existing publish path
  online count becomes >=2 -> CLEAR

CLEAR
  normal command -> calculate earliest feasible start; if now, PUBLISHING
  STOP command   -> bypass directly

WAITING
  calendar/availability changes -> recompute earliest feasible start
  online count <2               -> publish immediately (OFF)
  cancellation/unload           -> existing superseded/cancel path

PUBLISHING / AWAIT_STARTED
  accepted only -> remain; current started timeout still bounds the lifecycle
  started       -> anchor busy interval and optional future STOP reservation; HOLDING
  rejected/error/timeout/displaced before start -> clear provisional state, wake next waiter
  STOP command  -> bypass; no wait

HOLDING
  time reaches normal_busy_until -> CLEAR or WAITING according to reservations
  STOP command                   -> bypass immediately
```

The existing one-at-a-time normal worker provides `awaiting_started`; it should not be replaced by
a second queue. The arbiter is a not-before calculation around `_async_execute()`, after bridge
selection and immediately before `_ordered_publish()`.

### Earliest-feasible-start algorithm

For a normal candidate at monotonic start `s`, build half-open intervals:

```text
immediate = [s, s + action_train_ms + 100 ms)
future_stop = [s + stop_after_ms - 100 ms,
               s + stop_after_ms + stop_train_ms + 100 ms)  # if timed
```

The candidate is feasible when:

- `immediate` starts no earlier than `normal_busy_until`;
- `immediate` intersects no existing future STOP reservation; and
- `future_stop`, if present, intersects no existing future STOP reservation.

Start at `max(now, normal_busy_until)`. On an immediate conflict, move `s` to the conflicting
reservation's end. On a future-STOP conflict, move `s` so the candidate future STOP begins at the
conflicting reservation's end. Iterate over the sorted, capped reservation set until stable. Every
interval is finite (`stop_after_ms <= 3,600,000`, `repeats <= 20`, frame airtime <= 2,000 ms), so
this cannot create an infinite lease. Recompute under the arbiter condition immediately before
publish.

If a command's own deadline falls inside its immediate action/trailer prediction, treat the union
through the end of its STOP train as its immediate occupied interval. This mirrors
`TargetScheduler::next()`: a due STOP preempts remaining phases at the first locally safe handoff,
but it cannot interrupt a frame already inside the EFM8BB1.

After `started`, replace the provisional origin with the actual local receipt monotonic timestamp
minus valid `age_ms`. The 100 ms guard covers normal fresh-status delivery error; the existing
bridge-clock projection remains useful for cover wall-clock modeling but is not required by the
air arbiter. The future reservation uses the actual start plus `stop_after_ms`.

If two future STOP windows nonetheless overlap, do not delay either STOP. Record a diagnostic
counter. The scheduler should have shifted HA-originated starts to prevent the normal case, but an
HA restart, direct publisher, physical takeover, or partial broker partition can make overlap
unavoidable.

### STOP behavior, precisely

There are three STOP cases:

1. **Explicit HA STOP.** `_async_enqueue()` retains its fast lane and does not call
   `wait_until_feasible()`. It preserves only existing same-target publication barriers, as today.
   On `started`, its predicted STOP train extends `normal_busy_until` so later normal work waits.
2. **Scheduled fail-safe STOP.** At the deadline, the bridge's `TargetScheduler::next()` first
   respects only its own `next_rf_at_`—the EFM8 cannot accept another B0 safely while its local
   frame is transmitting—then promotes the command to `Phase::STOP` and chooses STOP before normal
   work. It does not query HA, MQTT, peer availability, or `normal_busy_until`.
3. **Displaced fail-safe STOP.** `schedule()` moves the owed copies to `flush_stops_`; `next()`
   rotates and prioritizes them before the replacement ACTION. A local replacement's `started`
   status cannot occur until that drain permits it. If a `displaced` status is caused by an outside
   publisher and HA has a reservation for the victim, conservatively convert that reservation into
   a current busy interval of `stop_train_ms + 100 ms` before retiring the future deadline.

If bridge A owns the advisory air interval and bridge B's STOP becomes due, bridge B transmits its
STOP at its next locally safe tick. The advisory owner is not revoked and B does not wait. The RF
frames may collide. This is an explicit safety trade: the design must never turn a possible STOP
collision into a guaranteed late or missing STOP.

## MQTT/interface specification

The recommendation adds **no MQTT topics and no payload fields**. Existing QoS and retained
semantics stay unchanged:

| Topic | Relevant schema and use |
|---|---|
| `rf433/<bridge>/availability` | QoS 0 retained text `online` or `offline`; used only to enable the gate when at least two bridges are online, never as a lock |
| `rf433/<bridge>/info` | QoS 0 retained JSON `{"bridge":str,"area":str,"default":bool,"boot":uint32,"listen":bool,"v":2}`; a changed `boot` retires old reservations |
| `rf433/<bridge>/tx` | QoS 1 non-retained JSON shown below; publication remains the act being gated |
| `rf433/<bridge>/status` | QoS 1 non-retained lifecycle JSON; `started` anchors the actual interval |
| `rf433/<bridge>/cmd` | QoS 1 non-retained `disarm` remains independent of air gating |
| `rf433/<bridge>/rx` | QoS 1 non-retained observations remain classification input only |

Relevant TX schema:

```json
{
  "command_id": "nonempty-valid-key-up-to-64-chars",
  "target": "a1b2c3:42:1,2",
  "raw": "AAB0...55",
  "trailer_raw": "AAB0...55",
  "repeats": 2,
  "stop_after_ms": 8000,
  "stop_raw": "AAB0...55"
}
```

`trailer_raw`, `stop_after_ms`, and `stop_raw` are optional; a timed command has both STOP fields.
The arbiter plans from the final body built by `ZemismartHub._command_body()` and rebuilt by
`_rebuild_from_live_contributors()`, not from stale pre-coalescing input.

Relevant status schemas:

```json
{"status":"accepted","command_id":"..."}
{"status":"rejected","command_id":"...","reason":"..."}
{"status":"started","command_id":"...","age_ms":0,"t":123456,"boot":2718281828}
{"status":"displaced","command_id":"..."}
{"status":"disarmed","command_id":"...","t":123456,"boot":2718281828}
```

Retained `/status` remains ignored by the consumer. `/tx`, `/status`, and arbitration state must
never be retained. There is intentionally no `rf433/air/lock`, `claim`, `owner`, or `busy` topic.

## Failure and edge behavior

| Situation | Required behavior | Residual risk |
|---|---|---|
| Broker loss | HA cannot enqueue new TX. Every bridge continues its RAM scheduler and fail-safe STOPs. No lease exists to deadlock. If HA remains running, its finite reservations remain useful after reconnect; publish failures clear provisional state. | Physical remotes and already-armed STOPs can overlap each other. |
| HA restart | All air-calendar state disappears. Bridges keep their RAM deadlines. The first post-restart command publishes immediately: deliberate fail-open. Restored cover motion is not treated as authority to reconstruct a lock. | A new command can overlap an orphaned future STOP; this is accepted over fail-closed reconstruction from uncertain state. |
| Peer bridge offline / stale conntrack | Offline peers are never awaited. A reservation learned from a command that actually started is retained through broker-only `offline`, because the device can still fire its STOP. A changed boot retires it; normal expiry is finite. With fewer than two online bridges, gating is OFF. | During partial connectivity, a disconnected peer's STOP is uncoordinated if HA lacks or lost its reservation. |
| STOP due during contention | The due bridge ignores HA ownership and transmits at the first tick allowed by its own `next_rf_at_`. Explicit STOP likewise bypasses before publish. | It can collide with the current owner. No safe design can both guarantee exclusivity and guarantee zero STOP delay after the other frame has begun. |
| Physical remote interference | No assumption of exclusive channel control. The system neither delays nor suppresses a human press; state sync continues to classify received frames. | A remote can collide with any bridge frame, and its frame may also cause takeover/disarm logic after reception. |
| Single-bridge install | `online_count < 2` selects OFF. Existing worker, firmware pacing, status timing, and fast STOP path are unchanged. | None added. |
| `millis()` rollover | Firmware `due_()`, `record_dispatch_()`, and `rf_air_clear()` remain unchanged. HA uses only local monotonic time and the relative `age_ms`/`stop_after_ms`; no cross-bridge `t` comparison is made. All bridge intervals are far below the signed half-range. | Existing bridge-clock state sync continues its own uint32 unwrapping, independently. |
| Retained stale data | Only existing availability/info are retained. They enable/describe participants but never grant ownership. Status remains non-retained and correlated. | Retained online can briefly overcount before LWT correction; that can add a finite normal delay, never suppress STOP. |
| Reservation cap or malformed plan | Keep nearest valid reservations, drop/decline farther state, log a warning, and publish normally. Do not reject the RF command because arbitration bookkeeping failed. | Collision avoidance degrades, matching today's behavior. |
| Consumer cancellation/unload | Existing future resolution, `drain_owner()`, publish barriers, and `close()` semantics win. Remove provisional plans and wake the next waiter. | Already-enqueued MQTT work remains governed by today's lifecycle semantics. |

## Why the recommendation is proportionate

The design does not promise a house-wide mutex. Such a promise is false because physical remotes
are uncoordinated and STOP must preempt. It instead targets the preventable, high-correlation burst
that produces most exposure, while preserving the firmware's strongest existing property: a
bridge can stop its motor without HA, the broker, Wi-Fi, or any peer.

The marginal implementation is in the component with ample memory and global knowledge. The
ESP8266 stays unchanged. There is no retained lock to audit, no fleet-wide mixed-protocol rollout,
and no new dependency on the broker for local safety. The cost is visible scene staggering and
incomplete coverage of external publishers. For this house, that is the correct first trade.

## Phased implementation plan

No phase should change firmware STOP priority or persist arbitration state.

### Phase 0: validate assumptions and establish a baseline

1. Confirm all seven production packages use `repeat_gap_ms: 35` and the same 19200-baud UART.
2. Inventory direct publishers of `rf433/+/tx`; verify `ZemismartHub` is the only routine one.
3. Record one week of whole-house scene count and user-observed misses. Where continuous listening
   is enabled, record peer echo timing as evidence of actual train length, not as blind ACKs.
4. Decide whether all bridges form one conservative collision domain or whether a measured overlap
   graph is needed. Start with one domain unless the 7–15 s scene skew is unacceptable.

### Phase 1: exact estimator and shadow calendar

1. Add the B0 slot estimator beside consumer transport modeling, with firmware-derived golden
   vectors and strict bounds.
2. Build `AirCommandPlan` from the final post-coalescing command body.
3. Add the bounded monotonic calendar in shadow mode: calculate would-wait durations and STOP
   conflicts but do not delay publication.
4. Expose debug counters/logs: estimated action/STOP train, would-overlap count, maximum queue wait,
   STOP bypass count, reservation-cap degradation, and stop-window conflict.
5. Confirm observed peer echo/started gaps agree with estimates within the 100 ms guard.

### Phase 2: enforce normal-command gating

1. Enable only when `BridgeRegistry` has at least two online bridges.
2. Gate normal `_ordered_publish()` calls at the earliest feasible time.
3. Preserve explicit STOP fast-lane publication exactly; after `started`, allow it to extend only
   the horizon for later normal work.
4. Add future timed-STOP reservations and lifecycle cleanup for rejection, timeout, displacement,
   disarm, boot change, expiry, unload, and cancellation.
5. Roll out to two overlapping bridges, then all seven after a day without lifecycle regressions.

### Phase 3: tune and reduce scene latency

1. Tune the 100 ms guard from measured p99 status/echo timing, never below firmware's own 5 ms
   margin plus observed scheduling error.
2. Prefer real multi-channel group frames and existing safe coalescing where possible.
3. If needed, configure a measured conflict graph in HA so non-overlapping bridge pairs may run
   concurrently. Absence of graph data must mean conflict, not permission.

### Phase 4: optional hybrid escalation

Only if data shows meaningful residual collisions from direct MQTT publishers should a separate
design introduce firmware advisory intents. That design must use non-retained messages, fixed peer
state, relative time at receipt, bounded expiry, boot/sequence IDs, a no-wait STOP bypass, and an
immediate fail-open path whenever MQTT is unavailable. It is not part of this recommendation.

## Test plan

### Firmware native C++ contract tests

Run the existing firmware harness with `uv run pytest -q`. Phase 1 does not change firmware, but
the native tests remain the reference for:

- production and maximum B0 airtime derivation in `normalize_b0_with_airtime()`;
- UART serialization plus airtime plus 5 ms pacing in `record_dispatch_()`;
- repeat/trailer ordering;
- due STOP priority over unfinished actions;
- displaced STOP rotation and scheduled-STOP alternation;
- `rf_air_clear()` across `UINT32_MAX`; and
- the stale-gate rollover regression.

Add table-driven golden vectors shared by value, not by runtime dependency, with consumer tests:
production action, production STOP, trailer, maximum legal frame, malformed bucket reference, and
embedded repeat 1/8/16. The C++ expected slot and Python expected slot must match exactly.

### Consumer pytest

Run the integration harness with `uv run pytest`. Add deterministic fake-monotonic tests for:

1. one online bridge: publish times and completion behavior exactly match the current tests;
2. two bridges, one normal command: immediate publish and result on first `started`;
3. two simultaneous action-only commands: second publish no earlier than first actual start plus
   `repeats * slot + guard`;
4. action plus trailer: all four frame slots are charged for `repeats: 2`;
5. explicit STOP during HOLDING/WAITING/AWAIT_STARTED: publisher is invoked immediately, subject
   only to today's same-target publication barriers;
6. a known timed STOP: a normal train that would cross its reservation is shifted after it;
7. two timed commands: their predicted STOP windows do not overlap when both originate in HA;
8. STOP windows forced to overlap by simulated restart/external state: neither STOP is delayed and
   a diagnostic conflict is recorded;
9. accepted without started, started timeout, rejection, immediate publish failure, cancellation,
   and unload all release provisional calendar state;
10. displacement converts the victim's future STOP into a current drain hold; disarm removes it;
11. `offline` retains an armed reservation, while a changed `boot` removes the old one;
12. online count changing from two to one wakes a waiter and fails open;
13. reservation cap retains near deadlines and degrades without rejecting TX;
14. wall-clock jumps do not affect the monotonic calendar; valid `age_ms` moves the actual handoff
    backward on the local monotonic axis; and
15. coalesced contributor cancellation rebuilds the body before occupancy planning, preserving
    the current `_rebuild_from_live_contributors()` race guarantees.

### System tests on Mosquitto and hardware

1. Publish a seven-bridge scene and capture `/status` plus `/rx`. Verify adjacent normal RF trains
   are separated by the calculated slots and the last-start latency matches the plan.
2. Pull the HA broker connection during a timed move. Verify the originating bridge fires STOP and
   HA creates no lock-related recovery dependency.
3. Reproduce a broker-only bridge disconnect while its timed STOP is armed. Verify connected HA
   retains the reservation until its window, but no STOP waits on it.
4. Restart HA with an armed bridge STOP. Verify the first new command is not silently refused; log
   the accepted fail-open collision risk.
5. Press an OEM remote during an HA train and at a known STOP deadline. Verify no code path claims
   exclusive channel control and state sync remains bounded/debounced.
6. Force two STOPs due during another bridge's normal train. Verify both due bridges dispatch on
   their local schedules even though the capture may show collision.
7. Leave the system past `millis()` rollover coverage in native simulation; no new firmware state
   exists, so existing signed-serial tests must remain authoritative.

Acceptance criteria for the stated trigger: no two **normal HA-originated** trains overlap in the
configured collision domain; no STOP publication or firmware STOP dispatch is delayed by the
calendar; one-bridge and first-command timing is unchanged; broker/HA/peer failures cannot create
an indefinite wait.

## Open questions and human decisions

1. **Collision domain:** Should all seven bridges be serialized conservatively, or is the 7–15 s
   worst-case scene skew unacceptable enough to justify measuring and configuring an overlap
   graph?
2. **Timing contract:** Can `repeat_gap_ms` be frozen at 35 ms for this fleet? If arbitrary bridge
   overrides must be supported, approve a later `/info` contract revision carrying scheduler timing
   rather than letting the consumer guess.
3. **Guard:** Is 100 ms acceptable as the initial cross-bridge guard, subject to shadow-mode p99
   measurement?
4. **STOP policy acknowledgement:** Confirm that simultaneous safety STOPs must transmit and may
   collide; there is no safe post-contention policy that both preserves zero delay and guarantees
   RF success.
5. **External publishers:** Are there routine publishers of `rf433/+/tx` other than this HA
   integration? If yes, the residual exposure may justify Phase 4 earlier.
6. **Latency versus reliability:** Is roughly 7.8 s last-start latency for seven action-only
   bridge trains acceptable? If trailers are common, should group frames/conflict domains be
   addressed before enforcement?
7. **Evidence threshold:** What observed miss rate should trigger the optional firmware hybrid?
   Suggested threshold: any repeatable residual collision between system-originated normal frames
   after Phase 2, not isolated physical-remote interference.

