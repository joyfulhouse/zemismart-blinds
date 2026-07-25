# Bridge coordination design — same-bridge drain awareness & effective cross-bridge arbitration

Date: 2026-07-25
Scope: DESIGN + INVESTIGATION ONLY. No source changes proposed for deployment.
Issues: #19 (silent nightly close failure under concurrent load), #20 (takeover responsiveness vs repeats).

All line references are against `main` at the time of writing (commit `aca64cc`).

---

## 0. TL;DR

- **Publication is serialized for movements, but NOT for STOPs, and NEVER to train completion.** The one global worker (`models.py:2186`) awaits only `started` (`models.py:2653`), which fires ~0.14 s into a train that lasts **~1.9 s at repeats=3** (measured, §1.4). Fast-lane STOPs (`models.py:1968`, `1984-1990`) publish *concurrently* with the worker. So on-air trains on one bridge overlap by design.
- **Cross-bridge arbitration has never held anything because, for the normal workload, there is almost nothing cross-bridge to hold.** The global worker already serializes movement *publication*, and a movement's own immediate train is short relative to the ~0.14 s start latency. The arbiter's live `commands_held=0` is *correct behavior*, not a malfunction — but it also means the cross-bridge calendar is, in production, largely inert for the contention that actually bites (§1.3). It earns its keep only for the fail-safe-STOP reservation windows it was built around (#12), not for normal sweeps.
- **The #19 contention is same-bridge, and the code path that would coordinate it is deliberately disabled.** `_drain_until_by_bridge` is populated for the command's own bridge (`models.py:1581` → `air.py::started`) but `decide()` filters the own bridge out of every conflict test (`air.py::decide`: `owner != bridge_id`, `pending.bridge_id != bridge_id`, `if reservation.bridge_id == bridge_id: continue`). The data needed for the fix already exists and is thrown away.
- **The "planned=0 → planned=2" discrepancy does not prove a silent uncounted-publish path in the reported regime.** The only publish-without-plan-count path is the arbiter-internal-error path (`count_air_plan=not arbiter_failed`, `models.py:2465`), and it is self-announcing: it increments `fail_opens`/`internal_error`. The snapshot had `fail_opens=0`, so it did not fire. `planned=0` is best explained by arbiter-instance reset on reload or by the 4 commands never publishing (coalesced/superseded). Detail and the honest residual uncertainty in §1.5.
- The durable defect (`started` proves transmission, never actuation) is real and orthogonal to all queueing. Peer corroboration can *lower* confidence but cannot *assert* failure, because a transmitting bridge cannot hear (§4).

---

## 1. Current state: what each layer actually guarantees

### 1.1 Layer A — the global worker (one queue, one worker)

Evidence:
- `self._queue: deque` + single `self._worker_task` (`models.py:1087-1089`).
- `_async_worker` pops one command, sets `_inflight`, `await self._async_run_direct(command)`, then loops (`models.py:2186-2201`).
- `_async_run_direct` → `_async_execute` (`models.py:2022`, `2600`), which runs `_ordered_publish` → `_await_status` (admission) → `_await_started` (`models.py:2646-2653`). Only then does the command return and the worker pop the next.

What it guarantees: **movement/raw-frame *publication* is serialized in request order, and the worker does not start the next command until the current one reports `started`.**

What it does NOT guarantee:
1. **Not train completion.** `_await_started` resolves on the bridge's first RF handoff, not the end of the train (`models.py:2311-2321`; docstring "actual first RF dispatch"). The worker moves on while the previous train is still draining on that bridge's firmware scheduler.
2. **Not STOPs.** A STOP with no queued overlapping live command takes the fast lane (`models.py:1968` `fast_lane = True`; `1984-1990` spawns `_async_run_fast` as a *separate task*). It bypasses the one-at-a-time worker entirely and publishes concurrently, ordered against earlier overlapping commands only by their *publish* events (`models.py:1942-1944`, barriers at `2015-2016`), never their lifecycles.

So "one global queue serializes everything across all bridges" is true for movements' *publication instants* but false for (a) STOP publication and (b) on-air train occupancy.

### 1.2 Layer B — the `_publish_lock` critical section

`async with self._publish_lock` wraps the final revalidate → plan → provision → enqueue block (`models.py:2381-2468`). It serializes the *finalize-and-enqueue* step so two commands cannot interleave inside it. Crucially it is **released before any air-wait** (`wait_event.wait()` at `models.py:2473` is outside the `async with`), which is what lets a fast-lane STOP still grab the lock while a movement is parked waiting for air. This is the correct design and any same-bridge-wait change must preserve it (§5).

### 1.3 Layer C — the air arbiter (cross-bridge calendar)

`air.py::AirArbiter`. Three invariants in the module docstring: STOP never waits; <2 online bridges ⇒ OFF; every failure path publishes.

`decide()` computes an earliest-feasible publish time from three cross-bridge inputs, **each of which excludes the command's own bridge**:
- `pending_expiries = [p.expires_at for p in self._pending.values() if p.bridge_id != bridge_id]`
- `other_drain = max(ends_at for owner, ends_at in self._drain_until_by_bridge.items() if owner != bridge_id, default=now)`
- reservation loop: `if reservation.bridge_id == bridge_id: continue`

`started()` (`air.py`) records the immediate train end into `_drain_until_by_bridge[bridge_id]` and any future armed-STOP window into `_reservations`. So the arbiter *has* the own-bridge occupancy; `decide()` just refuses to consult it. Same-bridge pacing is explicitly delegated to firmware `TargetScheduler::record_dispatch_` (docstring).

**Why `commands_held=0` in production is correct AND why the calendar is largely inert:**
- For a normal sweep, movements are published one at a time by the worker, ~0.14 s apart (the start latency). At the instant the arbiter evaluates cover N, cover N-1 on a *different* bridge has typically already reported `started` (so its pending entry is cleared) and its immediate `other_drain` is short (~1.9 s at repeats=3 — see §1.4). Cross-bridge overlap can occur inside that 1.9 s window, but only if two covers on *different* bridges are evaluated <1.9 s apart, which the serial worker makes uncommon, and even then the hold is short.
- The calendar's real, non-inert job is the **fail-safe-STOP reservation windows** (#12): a bridge with an armed timed STOP owes a future STOP interval, and a *different* bridge's command must not collide with it. That is a genuine cross-bridge guarantee the worker cannot provide. It is rare, so `commands_held=0` is unsurprising.

Plain statement requested by the brief: **the cross-bridge calendar is solving a real but rare problem (future-STOP window protection). It is not solving, and by construction cannot solve, the same-bridge train overlap that #19 is about.** It is not "solving a problem that cannot occur" — the fail-safe-STOP case can occur — but it is close to inert for normal nightly sweeps.

### 1.4 Measured train occupancy (the numbers the latency argument turns on)

Computed from the real production-shaped frame used by the test suite (`tests/test_air.py::_frame`, `encode_b0(make_payload(...))`) via `estimate_b0_slot_ms` and `plan_for_body`:

| Quantity | Value |
|---|---|
| Single-dispatch slot (embedded repeats baked in) | **609 ms** |
| `action_ms` at repeats=1 | 609 ms |
| `action_ms` at repeats=3 (fleet default, `const.py:39` `DEFAULT_REPEATS=3`) | **1827 ms** |
| Immediate drain incl. 100 ms guard, repeats=3 | **1927 ms** |
| `action_ms` at repeats=8 | 4872 ms |

`estimate_b0_slot_ms` mirrors firmware exactly (`codec.py:395-420`), so these are the real per-command on-air occupancies. The 0.14 s `started` latency is ~7% of a repeats=3 train: the worker moves on when the train is ~93% unfinished.

### 1.5 The `planned` counter discrepancy — resolved as far as static analysis allows

`record_plan` is called once per command inside `_finalize_and_publish`, gated `count_air_plan=not arbiter_failed` (`models.py:2465`, `2519-2522`). Auditing every publish path:
- Normal movement, arbitration enabled or disabled(<2 bridges): reaches `_finalize_and_publish` with `count_air_plan=True` ⇒ counted (planned if `plan_for_body` non-None, else unplannable **which increments `fail_opens`**).
- `air_bypass_requested` movement (`models.py:2402`): skips `decide()` but still reaches `_finalize_and_publish` with `count_air_plan=True` ⇒ counted.
- Fast-lane STOP: same `_ordered_publish` → `_finalize_and_publish` path ⇒ counted.
- **Arbiter threw (`arbiter_failed=True`, `models.py:2426-2429`): `count_air_plan=False` ⇒ `record_plan` NOT called, but the command STILL publishes (`_enqueue_publish` at `2468`, `_publisher` at `2544` run unconditionally).** This is the one publish-without-plan-count path. It is **not silent**: the same `except` block calls `_record_air_internal_failure` → `record_internal_error` ⇒ `fail_opens += 1`.

Therefore, in the reported snapshot (`fail_opens=0`), *every* command that published was counted. `planned=0` after a 4-command run is inconsistent with "4 commands published on this arbiter" **unless** one of:
1. **Arbiter/stats were reset between the two reads** (entry reload / re-setup rebuilds the `Hub` and its `AirArbiter`, zeroing `AirStats`). Most likely given "immediately after a restart."
2. The 4 verification commands **never published** on this arbiter — coalesced (`_coalesce_queued_movements`, `models.py:2123`), superseded by a later overlapping command, or displaced — so they never reached `record_plan`.

**Honest residual:** I cannot distinguish (1) from (2) from static reading; it needs a timestamped log correlation of the 4 commands against `air_shadow_stats()` reads. What I *can* assert: there is **no silent uncounted-publish path while `fail_opens==0`**, so the counters are trustworthy in that regime, and the discrepancy is a *counter-lifecycle/observation* artifact, not evidence of an arbitration blind spot that let #19's frames through uncounted. The blind spot in #19 is elsewhere (same-bridge, §2), not in the counting.

---

## 2. Root-cause candidates for #19, ranked

Reminder mandated by the brief: the issue's "8 frames solo vs 2 frames concurrent" is **confounded** (a transmitting bridge cannot receive; more bridges busy ⇒ fewer free to hear) and is used for *nothing* below.

### Candidate 1 (most likely) — same-bridge train interleaving starves the close's repeats
The office bridge was servicing the slider's timed move (a movement carrying an armed STOP, `stop_after_ms`) *plus* the close. The worker publishes the slider move, waits ~0.14 s for `started`, returns, then publishes the close — while the slider's ~1.9 s action train (and its later armed STOP) is still draining on the *same* bridge's firmware scheduler. The firmware `TargetScheduler` interleaves the two trains' dispatches, so the close's 3 repeats are spread out and interrupted rather than delivered as a clean consecutive burst. If the motor needs a cleaner run than it received, it no-ops while HA still reports `closed`.
- **For:** matches the operator's "three commands on one bridge" observation; matches solo-succeeds/concurrent-fails; explained entirely by the documented `started`-only wait (§1.1) and the arbiter's deliberate same-bridge exclusion (§1.3); requires no new failure mode.
- **Against:** unproven that the firmware interleave actually under-delivers repeats — this is inference about `TargetScheduler` behavior we have not measured. The firmware is *supposed* to pace, not drop.
- **Confidence:** moderate-high on "same-bridge overlap is the trigger," low-moderate on the exact motor-starvation mechanism.

### Candidate 2 — the armed STOP fired into the close's train
The slider's timed move arms a STOP that the firmware promotes at its deadline, dispatching STOP *ahead* of normal work (`models.py:2237-2243`). If that deadline lands during the close's train, the STOP frames preempt the close's remaining repeats on that one antenna.
- **For:** a concrete, documented preemption (the ledger airtime logic at `models.py:2244-2246` exists precisely because armed STOP preempts). Explains a *specific* cover failing on a *specific* night (deadline alignment).
- **Against:** timing-dependent; would be intermittent, not "nightly." The operator described it as recurring.
- **Confidence:** low-moderate; a strong secondary contributor, possibly the mechanism behind Candidate 1.

### Candidate 3 — plain RF collision / motor marginality independent of our queueing
The motor at that location is simply marginal and an unlucky concurrent-airtime moment lost it.
- **For:** solo actuated it, concurrent did not — consistent with marginal link + contention.
- **Against:** if it were pure RF luck it would not correlate so cleanly with "three commands on one bridge." Range/routing already ruled out by the solo actuation from the same bridge.
- **Confidence:** low as *primary*; it is the null hypothesis we cannot fully exclude without bridge-side TX counters.

### Candidate 4 (rejected) — cross-bridge arbitration failure
Ruled out: arbitration is cross-bridge only and the failure was same-bridge (issue comment 2, verified §1.3). The arbiter never had jurisdiction. Not a malfunction.

**Ranking:** 1 ≳ 2 > 3 ≫ 4. Candidates 1 and 2 are the same family (same-bridge concurrent occupancy on one antenna) and are what any fix should target. The decisive missing measurement is **bridge-side TX evidence** (esphome logs / TX counters on the office bridge during a reproduction), not peer hearing.

---

## 3. Proposed design

Two problems to address, with different urgency:
- **(P1) Same-bridge drain awareness** — the #19 trigger. Data already exists.
- **(P2) Making cross-bridge coordination genuinely effective** — currently near-inert for sweeps.

### 3.1 Design axis: what "coordination" should mean per bridge

The lever is **how long the worker/arbiter defers the next same-bridge command relative to the previous train.** Three natural stopping points, increasing separation and latency:

| Level | Wait until | Separation delivered | Added latency per same-bridge pair (repeats=3) |
|---|---|---|---|
| L0 (today) | previous `started` (~0.14 s) | none on air | 0 |
| L1 | previous **immediate action train** drains (`_drain_until_by_bridge`) | trains no longer overlap; armed STOP still async | **~1.8 s** |
| L2 | previous train **and any armed-STOP window** clear | full antenna exclusivity incl. fail-safe STOP | up to `stop_after_ms + stop_ms` (seconds) |

L2 is a non-starter for sweeps (it serializes a whole cover's travel before the next cover starts) and would fight the "STOP never waits" invariant if applied to STOPs. **L1 is the target.**

### 3.2 Option A (recommended) — teach `decide()` about the own bridge, movements only

Change the three own-bridge exclusions in `air.py::decide()` to *include* the command's own bridge for the **immediate-drain and pending** tests (NOT the reservation/future-STOP test, and NOT for STOPs, which never call `decide()` — they use `probe_stop`). Concretely: fold `_drain_until_by_bridge[bridge_id]` into the `start` computation, and let a same-bridge pending entry contribute to `pending_expiries`. Movements then wait out the prior same-bridge immediate train (L1). STOPs are untouched (they bypass `decide`, `models.py:2400-2401`), preserving "STOP never waits."

- **Pros:** minimal surface; reuses the existing wait machinery (`wait_event`, ceiling, fail-open) verbatim; the data is already populated by `started()`; enforcement/shadow/ceiling/fail-open accounting all keep working; the wait already happens outside `_publish_lock` (§1.2) so no deadlock risk with fast-lane STOP.
- **Cons:** adds ~1.8 s per *same-bridge* consecutive movement pair (measured). For a 7-cover Night sweep **all on one bridge** that is **+11.0 s** cumulative (measured, worst case). If the sweep spans B bridges roughly evenly, added wall-clock ≈ `1.8 s × (ceil(7/B) − 1)` because per-bridge chains run in parallel across bridges — e.g. 3 bridges ⇒ ~+3.6 s. Still a real regression the operator will feel.
- **Latency mitigation:** only serialize when the *same remote/motor family* would actually be harmed, or gate L1 behind a per-bridge concurrency count (see Option B). Also: the armed-STOP case (Candidate 2) is NOT fixed by L1 alone — the armed STOP is a *future reservation*, not immediate drain — so Option A addresses Candidate 1 but only partially Candidate 2.

### 3.3 Option B (recommended companion) — bounded same-bridge pipelining instead of full L1

Rather than always waiting the full immediate train, allow at most K concurrent unfinished trains per bridge (K=1 ⇒ full L1; K=2 ⇒ one train may overlap). Track per-bridge in-flight train count from the same `_drain_until_by_bridge` horizon; when count would exceed K, wait exactly until the oldest train's drain. This directly caps the "three commands on one bridge" that triggered #19 (K=1 or 2 both forbid three-way overlap) while letting a lightly loaded bridge stay pipelined.

- **Pros:** tunable latency/safety trade; K=2 forbids the specific three-train pileup in #19 while adding latency only when a bridge is genuinely hot; degrades to today's behavior when a bridge sees one command at a time (the common case), so the 7-cover *cross-bridge* sweep is unaffected.
- **Cons:** more state and one more tunable; K is an empirical guess until we have TX-side data; does not by itself separate an armed STOP from a following movement (still Candidate-2-exposed).

### 3.4 Making cross-bridge arbitration genuinely effective (P2)

Two honest options:
- **B1 — accept it as a STOP-window guardian and stop expecting sweep coordination from it.** Document that `commands_held=0` is health, not idleness, and that its value is the fail-safe-STOP reservation (already enforced). Add same-bridge coordination via Option A/B *separately*. This is the least-risk path and matches what the code actually does well.
- **B2 — widen the collision-domain model** so `decide()` treats bridges that can hear each other (peer-corroboration graph, §4) as a shared drain pool, holding a movement on bridge X while a *reachable* bridge Y is mid-train. This would make cross-bridge holds actually happen. **Risk:** it re-introduces latency across the whole fleet for a contention that the confounded evidence never proved is harmful, and it depends on a reachability graph we do not yet measure reliably. Not recommended now.

**Recommendation:** Option A + Option B (K=2) for the same-bridge #19 fix; adopt B1's *framing* for cross-bridge (document, don't expand). Defer B2 until bridge-side TX data exists.

### 3.5 What about #20 (repeats vs takeover)?
Raising `repeats` for the office remote (issue's "possible direction 3") adds redundancy but lengthens the train (repeats=8 ⇒ 4.87 s, §1.4), widening the very same-bridge overlap window that causes #19 and slowing takeover disarm (#20). **Do not raise repeats as the #19 fix** — it worsens both the overlap and the latency it is meant to help. Same-bridge separation (Option A/B) is the correct lever.

---

## 4. The open-loop problem and peer corroboration

`started` proves a bridge began transmitting (`models.py:2311-2321`); nothing proves the motor acted. HA then models full travel and reports `closed` with position-grade authority — the silent-failure core of #19.

**What already exists:** the ledger computes *emission proof* — when a **peer** bridge (`bridge_id != command_bridge`) hears our confirmed command's signature, `_on_emission_proof(command_id)` fires (`state_sync.py:924-926` → `models.py:1182` `_record_emission_proof`, queryable via `was_emission_proven`, `models.py:1201`). This is exactly a peer-corroboration signal and it is *already correlated per command*.

**Can absence of corroboration degrade confidence?** Partly, and only as a *soft* signal:
- **It cannot assert failure.** A transmitting bridge cannot hear itself, and a peer only hears if it is (a) online, (b) not itself transmitting, and (c) in range. Under exactly the concurrent load where #19 happens, peers are *busy*, so corroboration is *most likely to be absent when the system is most loaded* — precisely when a real emission is also most likely. This is the same confound that sank the "2 vs 8 frames" evidence. **False-positive risk is high and load-correlated.**
- **What it can do:** when a command is *confirmed started* but earns *zero* peer emission proofs within its window AND ≥1 peer was demonstrably idle-and-in-range during that window, lower the reported confidence (e.g. mark the position as `assumed` / unverified rather than `closed`), without changing motion modeling. The idle-and-in-range qualifier is what keeps it from firing on every loaded sweep. Building that qualifier needs a reachability/occupancy view we do not currently maintain per-window.
- **Recommendation:** treat corroboration as a **confidence annotation, never a control input.** Do not gate publication, retries, or state on it. Surface "unverified close" as a diagnostic so the operator sees candidates instead of silent success. Full auto-remediation (re-send on missing corroboration) is unsafe: it would re-drive motors on false positives during every heavy sweep.

Honest note: I am inferring the load-correlation of corroboration absence from the RX architecture (`_dispatch_heard`, a transmitting bridge cannot receive), not from measured miss rates. It should be validated before any confidence-degradation ships.

---

## 5. Risks — what a timing/order change could break

The system just shipped a run of phantom-press / ledger-window fixes (#14–#18). Any change to *when* a movement publishes interacts with those windows:

1. **Ledger airtime windows (#14, #17).** The ledger classifies our own echoes as own-emission for `action_ms`/`train_ms` windows (`models.py:2236-2272`). Delaying a movement by ~1.8 s (Option A) shifts *when* its frames hit air relative to when the ledger registered them (`_register_command_ledger` runs at finalize, `models.py:2543`, immediately before publish — so the window is anchored at publish, not enqueue). Must verify the L1 wait happens *before* `_register_command_ledger`, i.e. the ledger window still opens at actual publish, not at the earlier decide. In the current structure the wait (`2473`) precedes the finalize/register on the next loop iteration, so this holds — but it is the #1 thing to re-test.
2. **Armed-STOP preemption accounting (#16).** `displaced()`/`disarmed()` turn a future STOP window into current drain. If Option A makes movements consult own-bridge drain, a `displaced()`-produced drain on the *own* bridge would now delay a following own-bridge movement. That is arguably correct, but it is new coupling between the STOP-flush path and normal movement timing — needs a test that a flushed STOP does not now wedge a legitimate following movement past its ceiling.
3. **"STOP never waits" invariant.** Must stay intact. STOPs use `probe_stop` and the fast lane, never `decide()`; as long as Option A touches only `decide()` (movement path), the invariant holds. A regression here would be severe (fail-safe STOP is the safety property). Explicit test required.
4. **Fast-lane STOP vs a now-waiting movement.** A movement parked on a same-bridge drain wait holds no lock (`2473` is outside `_publish_lock`), so a fast-lane STOP can still preempt it (`air_preempted`, `models.py:1929`). Good — but the interaction (STOP arrives while its own target movement is air-waiting on the same bridge) needs coverage: the movement must be superseded, not resurrected after the wait.
5. **Ceiling / fail-open behavior.** Longer, more frequent waits mean more chances to hit `MAX_AIR_HOLD_MS` (130 s) and the ceiling fail-open. With per-command adds of ~1.8 s this is far from the ceiling, but a hot bridge under Option A K=1 could stack; verify the ceiling still publishes rather than starves.
6. **Latency-driven behavior change for automations.** A Night scene that today completes in ~1 s of publication will take measurably longer (§3.2). Not a bug, but a visible behavior change the operator must sign off on.

---

## 6. What I would NOT do, and why

- **I would not raise `repeats` to fix #19.** It lengthens the train (repeats=8 ⇒ 4.87 s), widening the same-bridge overlap window that causes the failure and slowing #20 takeover. It treats a coordination bug with a redundancy hammer.
- **I would not apply L2 (serialize to full train + armed-STOP window).** It serializes each cover's entire travel before the next starts — a Night sweep would crawl — and it pressures the "STOP never waits" invariant.
- **I would not expand cross-bridge arbitration (Option B2) now.** The evidence that cross-bridge overlap harms anything is confounded; adding fleet-wide holds trades certain latency for unproven benefit.
- **I would not gate publication, retries, or reported state on peer corroboration.** Its absence is load-correlated with exactly the conditions where real emissions occur; auto-remediation would re-drive motors on false positives. Confidence annotation only.
- **I would not trust the `planned` counter as a proxy for "commands that reached a bridge"** until the reset-vs-bypass ambiguity (§1.5) is closed with a logged reproduction. It is trustworthy while `fail_opens==0`, but the observed `planned=0` shows it is easy to misread across a reload boundary.
- **I would not ship any of this without bridge-side TX evidence** (esphome logs / TX counters on the office bridge during a reproduction) confirming the interleave-starvation mechanism. Four confident diagnoses have already been wrong on this subsystem; the peer-hearing signal is confounded; the one measurement that would actually discriminate Candidates 1/2/3 has not been taken.

---

## Appendix — key evidence index

| Claim | Location |
|---|---|
| One queue, one worker | `models.py:1087-1089`, `2186-2201` |
| Worker awaits `started`, not train end | `models.py:2646-2653`, `2311-2321` |
| Fast-lane STOP publishes concurrently | `models.py:1968`, `1984-1990`, `1999-2006` |
| Air wait released before it, outside publish lock | `models.py:2381-2468` vs `2473` |
| `decide()` excludes own bridge (3 filters) | `air.py::decide` (`owner != bridge_id`, `pending.bridge_id != bridge_id`, `reservation.bridge_id == bridge_id: continue`) |
| Own-bridge drain IS recorded but ignored | `air.py::started` (`_drain_until_by_bridge[bridge_id]`), consumed only for `owner != bridge_id` |
| STOP uses probe, never `decide` | `models.py:2400-2401`; `air.py::probe_stop` |
| Only publish-without-count path is self-announcing | `models.py:2426-2429`, `2465`, `2519-2522` |
| Emission proof = peer hears confirmed frame | `state_sync.py:924-926`, `models.py:1182-1204` |
| repeats=3 default | `const.py:39` |
| slot=609 ms; repeats=3 ⇒ 1827 ms train | `estimate_b0_slot_ms` (`codec.py:395-420`), verified against `tests/test_air.py:115` |
