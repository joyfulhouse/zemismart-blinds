# RF433 State-Sync Consumer (integration side) — Design

- **Status:** Draft — pending owner review (do not implement until approved)
- **Date:** 2026-07-15
- **Repo:** `zemismart-blinds` (HA integration). Consumes the firmware contract designed/implemented in
  the `esphome-rf433-mqtt-bridge` companion (`docs/design/2026-07-15-state-sync-firmware-design.md`).
- **Reviewers so far:** Codex `gpt-5.6-sol` (max reasoning) — design gate, verdict `REVISE`; all 14
  findings accepted and folded in (disposition table in §14).

---

## 1. Goal & scope

Correlate a **physical remote press** heard over RF (reported by bridge firmware on
`rf433/<bridge>/rx`) with a blind, and update its travel-time position model as if commanded
("mirror") — plus **emission proof** (a peer bridge overhearing our own commanded frame upgrades that
command's anchor). This is the integration half of state-sync; the firmware half already ships.

**In scope:** a hub-owned RX consumer (decode → classify → correlate → dispatch), a command-frame
ledger, per-bridge clock correlation, cover-side press mirroring, command-scoped emission proof, and
takeover-disarm. **Out of scope:** anything requiring firmware changes (the contract is fixed);
preset/favorite remote buttons; RSSI/location disambiguation.

**Owner-approved decisions:**
- **D1 — Trust-and-mirror.** On a decoded UP/DOWN/STOP press matching one of our covers, drive the
  model immediately, guarded by echo suppression + replay/debounce. Corroboration only where trivially
  available.
- **D2 — Include emission proof.** A peer overhearing our commanded frame upgrades *that command's*
  anchor unverified→verified.
- **D3 — Hub-owned.** Decode/classify/correlate live in `ZemismartHub`; covers are notified through a
  listener list; no per-cover MQTT routing.

---

## 2. Firmware contract consumed (fixed)

- `rf433/<bridge>/rx` (QoS 1, **non-retained**, opt-in `listen_enabled`): `{"frame","t","boot"}` — a
  heard raw B1 capture, bridge monotonic ms `t`, boot-session id `boot`.
- `rf433/<bridge>/status "started"`: `{"command_id","age_ms","t","boot"}` — UART **handoff** =
  `t − age_ms` (mod 2³²). Also existing `accepted`/`rejected`/`displaced`.
- `rf433/<bridge>/status "disarmed"` (new): `{"command_id","t","boot"}` — ack that a disarm applied;
  the firmware `disarm` is an **atomic abort** (erases the whole command + unconditional tombstone), so
  it cancels a command at any pre-emission stage.
- `rf433/<bridge>/info` (retained): `{"bridge","area","default","boot","listen","v":2}`.
- `rf433/<bridge>/cmd`: `{"action":"disarm","command_id":…}`.

**The RF frame carries no command_id/provenance** (physical remote and our replay are bit-identical) —
so classification is inherently **time-correlation**, and immediate unambiguous classification is
impossible; the ledger + hold (§5) is how we resolve it.

---

## 3. Grounding facts (verified; two Codex corrections applied)

- No coordinator; shared `ZemismartHub` + `BridgeRegistry` in `DomainRuntime` at `hass.data[DOMAIN]`;
  one `ZemismartCover` per entry. MQTT correlated by `(bridge_id, command_id)`; covers notified via
  `displaced_listeners`/`bridge_listeners`. MQTT subs at `__init__.py:_async_initialize_domain_runtime`
  (3 topics); `MQTT_RX_TOPIC` defined but unused at domain level (`/rx` consumed only transiently by the
  config-flow Learn wizard).
- `handle_status` (`models.py:~697`) uses `_pending[(bridge,command_id)]`, **removed immediately after
  `started`** — so a disarm waiter must be **separate** (§8).
- Travel model in `cover.py`: `_start_motion` (`~523`) anchors at `ack.started_at`. `_async_move_full`
  (full open/close) sets **no `stop_after_ms`** ⇒ no armed STOP; `_async_set_position_locked` (timed
  partial) arms one. `_overlapping_covers`/`_member_covers`/`_reconcile_overlaps` model groups;
  `_reconcile_unverified_anchor` (`~362`). Cover set `_COVERS` (WeakSet).
- **Correction A:** `_record_publish` runs one event-loop turn after the publisher task is scheduled —
  it has **neither the bridge nor `command_id` nor `started_at`**. ⇒ the ledger registers at
  `_async_execute` (where both are known), before publish.
- **Correction B:** `_reconcile_unverified_anchor`'s marker is **not** a generic emission proxy: it
  means *a restored, expired timed target depends on the old bridge having survived long enough to emit
  its armed STOP*; an online report confirms that scheduler-survival assumption. ⇒ emission proof must
  be command-scoped and must never clear this marker on unrelated evidence (§7).

---

## 4. Architecture

Add a 4th domain subscription `(MQTT_RX_TOPIC, _handle_rx)` at `__init__.py`; `_handle_rx` is a
`@callback` that **drops retained** and forwards to `hub.handle_rx(bridge_id, payload)`. All logic is
hub-side; matched results dispatch to covers via metadata-bearing **RX listeners**.

**Decoupling (finding 11):** covers do **not** expose `_COVERS` to the hub. Each cover registers
`(remote_key, channels, callback)` with the hub (like `displaced_listeners`) in `async_added_to_hass`
and deregisters on removal. The hub matches on that metadata — no reverse import.

**Units (each independently testable):**

| # | Unit | Home |
|---|------|------|
| U1 | `BridgeInfo` + `boot`/`listen`/`contract_v` | `models.py` registry (§9) |
| U2 | `_BridgeClock` (bridge `t` → HA time) | hub (§10) |
| U3 | Command-frame **ledger** (pending→confirmed, full envelope) | hub (§5) |
| U4 | RX **classifier** `handle_rx` | hub (§6) |
| U5 | RX **listener registry** + cover mirror | hub + cover (§6, §7) |
| U6 | **Ordering/supersession** hooks | hub + cover (§6.A) |
| U7 | **Disarm waiter** + takeover | hub + cover (§8) |

---

## 5. Command-frame ledger (findings 3, 4; correction A)

Registered in `_async_execute` **before** the publish task starts, keyed for lookup by **decoded
signature** `(remote_key, frozenset(chans), button)` and by `command_id`:

- **Entry, phase PENDING** (at registration): `command_id`, selected `bridge`, command `channels`,
  `button`/kind, and — since every frame of the command is known up front — the **full emission
  envelope**: the action frame plus `trailer_raw` and `stop_raw` (each decodable to a signature), with
  per-frame windows *derived from real airtime × repeat × pacing* (not a flat 1 s).
- **Phase CONFIRMED** (on `/status started`): fill `handoff = t − age_ms`; each frame's window becomes
  `[handoff_of_frame − slack, handoff_of_frame + airtime + slack]` with **symmetric** clock slack.
- **Retire** an entry (all frames) on `displaced`/`rejected`/disarm/completion, and TTL-expire.

**Hold rule (finding 3):** an RX capture whose signature matches a **PENDING** entry (command issued,
`started` not yet seen — a peer can legitimately hear it first) is **held** in a short bounded queue and
re-classified once the entry CONFIRMS or the hold times out. This removes the "peer hears our TX before
its `started` arrives → misclassified as a press" race.

The ledger is bounded (per-bridge + global caps) and cleared in `hub.close()`.

---

## 6. Classification pipeline (`handle_rx`, per non-retained `/rx {frame,t,boot}` from bridge B)

1. **Exact-event dedup (finding 5):** drop if `(bridge, boot, t, normalized_frame)` seen recently
   (bounded cache) — catches QoS-1 redelivery independent of burst debounce.
2. **Decode:** `decode_b0` + `infer_action_button` → `signature=(remote_key, frozenset(chans), button)`.
   Undecodable / `button is None` → **ignore**.
3. **Clock-convert (§10):** `_BridgeClock[B]` → `heard_at`, clamped `≤ receive_time`; boot-change / no
   offset → receive-time fallback.
4. **Ledger match:** signature (or exact normalized frame) within any ledger frame's window:
   - matches a **CONFIRMED** entry → **our own emission → never mirror**; if hearing bridge B ≠ command
     bridge → **command-scoped emission proof** (§7).
   - matches a **PENDING** entry → **hold** (step 5 of §5), re-run later.
   - no match → **physical press** → step 5.
5. **Burst debounce:** signature-keyed stamp collapses one press's repeats / multi-bridge copies within
   `DEBOUNCE_WINDOW` (~1–2 s). First copy wins.
6. **Owner-driven dispatch (§6.A).**

### 6.A — Ordering, supersession & group batch (findings 1, 2)

A physical press is a **hub ordering event**, applied as **one atomic batch** from a pre-event snapshot:

- **Batch by containment:** every cover whose `channels` are **fully contained** in the pressed set
  (`channels ⊆ chans`) is part of the moved batch — an exact-match **group owns propagation to its
  members** (`_member_covers`), and standalone contained covers are modeled **once** (the batch dedups,
  so a member modeled via its group is not also modeled standalone). A cover the press only **partially**
  addresses (`channels ⊄ chans` — some of its motors weren't pressed) is marked **unknown** (we can't
  prove those moved). Never invoke each intersecting listener as an independent owner.
- **Supersede in-flight commands (finding 1):** for every overlapping channel, **bump the per-channel
  publish generation** (`_publish_seq`) so a set-position move still between its overlap-token snapshot
  and publish resolves *superseded* and aborts; and set a per-cover **intent generation/timestamp** that
  `_start_motion` (and each caller after its command `await`s) checks — a delayed commanded ack whose
  intent generation is stale **aborts instead of overwriting** the press. If the superseded command is a
  live timed move, **disarm** its `command_id` (firmware atomic-abort cancels any remaining/ pending
  frames — no firmware change needed).
- **Apply the mirror** (§7) to the owner + members in the one batch.

---

## 7. Mirror & emission proof (cover-side; findings 6, 8, 9)

**Mirror (source="heard", never transmits):**
- **UP → open-full / DOWN → close-full:** the `_async_move_full` motion commit, driven by a **motion
  event with `source="heard"`** (`started_at=heard_at`, `deadline=None`, `absolute_anchor=True`) — *not*
  a synthesized `CommandAck` through `_record_ack`. Split the **common motion commit** from TX-ack
  recording so a heard move does **not** mutate `last_bridge`/`degraded` (finding 9). A heard full move
  runs to the physical limit → genuine anchor on completion.
- **STOP → freeze:** a shared **`_apply_stop(at, provenance)`** helper (extracted from `_async_stop`)
  performs interruption **+ `_reconcile_unverified_anchor` + member propagation + overlap reconcile +
  state writes**, used by both the transmitted and heard STOP paths (finding 8). Heard STOP does **not**
  transmit and needs **no disarm** (a later STOP is harmless).

**Emission proof (command-scoped, finding 6 + correction B):** a confirmed ledger match heard by a
*different* bridge records **command-scoped emission evidence keyed by `command_id`** in a bounded
recent-proof map (analogous to `was_displaced`, so proof arriving before the cover commits its model is
not lost). It upgrades **only the anchor derived from that exact `command_id`** and **never clears an
unrelated restored-STOP `_unverified_anchor_bridge`** marker (which is settled only by its own exact
scheduler evidence or completed hard-limit travel). Raw-service frames (`stop_raw`/`trailer`) are
echo-suppressed but never upgrade a cover.

---

## 8. Takeover-disarm (findings 1, 7)

Fires only when an **UP/DOWN** press's owner cover is mid **timed-partial** move
(`_motion_timed`, holding `_motion_command_id` C on `_motion_bridge` A):

- **Snapshot `(A, C, deadline)` before the mirror clears the motion fields.**
- Publish `rf433/<A>/cmd {"action":"disarm","command_id":C}`; await a **separate disarm waiter** keyed
  `(A, C)` fed by a new `handle_status` `"disarmed"` branch (**not** `_pending`, which is gone after
  `started`). Hub-**dedup** retries by that key; **schedule** the retry as a task (never awaited on the
  RX callback path).
- **Bound retries to the original `deadline`** — past it the STOP has either fired or cannot; stop
  retrying.
- **On ack before deadline:** the STOP will not fire → keep modeling the press. **On timeout / not-found
  / ambiguity near the deadline:** the old fail-safe STOP may have won → **freeze or mark unknown at the
  old deadline** rather than modeling travel through it.
- All disarm tasks/waiters are cancelled in `hub.close()`.

---

## 9. `/info` ingestion (findings 11, 12)

Extend `BridgeInfo` with `boot: int | None`, `listen: bool | None`, `contract_v: int | None`:
- Parse in `update_info` with **strict** types (reject a bool-as-int for `boot`/`v`); `listen=None`
  means *unknown*.
- Thread through **every** `BridgeInfo` construction — `update_info`, `_store`, and `update_availability`
  — preferably via `dataclasses.replace(current, …)` so an availability flip does not drop the fields.
- Include the new nullable fields in `_store`'s "meaningful state" predicate so an **info-only** bridge
  is not pruned; clear them on a complete-document tombstone.
- **Peer/proof rule:** a live `/rx` from a bridge itself proves it is listening — do **not** require
  retained `listen==True` to accept its emission proof; `/info.listen` is only the *a-priori*
  corroboration-availability inventory.

---

## 10. Clock correlation (`_BridgeClock`, finding 10)

Per bridge: `offset` (HA time − bridge `t`) with **stale-sample rejection**. On each accepted sample:
compute `t` via serial-number (int32) arithmetic **only while ordering is unambiguous**; reject
large-residual/out-of-order samples; **re-seed** on `boot` change, 2³¹ half-range ambiguity, or a major
outlier. `heard_at = t + offset`, **clamped to a plausible interval ending at `receive_time`**. Do
`t − age_ms` modularly. `/rx.boot` + receive-time fallback is sufficient before `/info` arrives. Only
~hundreds-of-ms accuracy is needed (windows are wide) — but a stale sample must never rewind an active
motion's start.

---

## 11. Lifecycle & bounds (finding 11)

Everything the RX path creates is **bounded and torn down**: per-bridge clocks, ledger entries, the
exact-event cache, debounce stamps, emission-proof memory, disarm waiters/tasks, and RX listeners — all
capped (reusing the registry's bridge-id/count bounds so a **forged `/rx` bridge id** cannot grow state
unboundedly) and cleared/cancelled in `hub.close()`. No task may publish after close.

---

## 12. Privacy & HA quality (Codex "conditionally sound")

`_handle_rx` is a `@callback`; listener mutations are synchronous; **disarm is scheduled, never awaited**
on the callback path; `async_write_ha_state()` runs on the loop (covers register listeners only after
being added). The persistent `/rx` stream is acceptable because it is opt-in (firmware
`listen_enabled`), unmatched/raw events stay **memory-only, bounded, and absent from logs/diagnostics**,
and the retained topic is dropped. Update the **onboarding-only `/rx` comment in `const.py`** to reflect
continuous consumption.

---

## 13. Testing (finding 14)

Deterministic tests (synthetic frames, **never** real identities) must include the races, not just the
happy branches: `/rx` **before** `started`; a press **between** `started` resolution and `_start_motion`;
queued/unpublished set-position cancellation; overlap-token invalidation by a press; **exact vs
partial/superset** group intersections; retained and delayed QoS **duplicates**; a delayed `stop_raw`
not mirrored as a physical STOP; **emission proof before model commit**; **unrelated restore-anchor
preservation** under peer proof; disarm **dedup + deadline-timeout freeze**; and **`close()` cleanup /
bounds**. Plus the plain branches: decode/ignore, mirror UP/DOWN/STOP, clock offset/boot/wrap, `/info`
ingestion + availability-flip preservation, cover-match by `remote.key`. Gate: `ruff` + `mypy --strict`
+ `pytest` (project standard).

---

## 14. Non-goals & accepted residual risks

- **Neighbor-identical remote (finding 13):** a genuinely identical 32-bit identity + overlapping
  channel code from another household **cannot** be distinguished under trust-and-mirror — inherent to
  provenance-free RF; **documented, accepted**. Exact configured command/frame matching is used where
  possible; suppressing the same button *after* our own command's handoff is benign, and the *before*-
  handoff timed-move case is covered by the pending-hold (§5).
- Preset/favorite buttons (only UP/DOWN/STOP mirror); cross-bridge wall-clock sync; mirroring to
  unconfigured remotes; sub-100 ms timing precision.

---

## 15. Codex review disposition (verdict `REVISE` → all 14 folded in)

| # | Sev | Area | Resolution |
|---|-----|------|-----------|
| 1 | HIGH | command-vs-heard ordering | §6.A publish-gen + intent-gen supersession + disarm; firmware atomic-abort suffices |
| 2 | HIGH | intersection fan-out | §6.A owner-driven atomic batch; partial/superset → unknown |
| 3 | HIGH | ledger registration race | §5 register at `_async_execute` (PENDING) + hold |
| 4 | MED | echo window / envelope | §5 full-envelope ledger, real airtime windows, symmetric slack |
| 5 | HIGH | retained / QoS replay | §6 drop retained + exact-event `(bridge,boot,t,frame)` dedup |
| 6 | HIGH | emission-proof anchor semantics | §7 command-scoped evidence; never clears unrelated anchor |
| 7 | MED | disarm deadline/result | §8 separate waiter, deadline-bounded, freeze-on-timeout, UP/DOWN-only |
| 8 | MED | heard-STOP reconciliation | §7 shared `_apply_stop` incl. `_reconcile_unverified_anchor` |
| 9 | MED | synthesized-ack provenance | §7 `source="heard"`, split motion-commit from `_record_ack` |
| 10 | MED | clock robustness | §10 stale rejection, clamp, re-seed |
| 11 | MED | listener ownership / bounds | §4 metadata listeners; §11 bounds + teardown |
| 12 | LOW | `BridgeInfo` completeness | §9 `replace`, strict parse, `listen=None` unknown |
| 13 | LOW | identical-remote collision | §14 documented residual risk |
| 14 | MED | test contract | §13 race matrix |

**Codex-validated (no change):** the 4th subscription; immutable `BridgeInfo` extension; a separate
disarm waiter fitting `handle_status`; physical UP/DOWN via the common motion commit and STOP via a
shared helper; boot-bearing `/rx` receive-time fallback; HA loop thread-safety with a `@callback`
handler + scheduled disarm. The firmware contract is **sufficient** — no firmware change is required.
