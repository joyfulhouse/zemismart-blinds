# RF433 State-Sync Consumer — Implementation Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax. This plan is
> calibrated for a **Codex gpt-5.6-sol implementer in an isolated git worktree** with the design spec
> (`docs/design/2026-07-15-state-sync-consumer-design.md`) available. Each task gives exact files, the
> **interface contract** neighbouring tasks depend on, the **concrete acceptance tests**, and the spec
> section that defines the mechanism. Author the implementation from the spec; do not hand-wave a test.

**Goal:** Consume the bridge firmware `/rx`/`/status`/`/info`/`/cmd` contract so a physical remote press
heard over RF updates the matching blind's travel model ("mirror"), with command-scoped emission proof.

**Architecture:** A new self-contained `state_sync.py` holds the pure RX logic (per-bridge clock,
command-frame ledger, classifier). `ZemismartHub` owns one consumer instance, feeds it from a 4th MQTT
subscription, and exposes an RX-listener registry covers register with. Covers gain a `source="heard"`
mirror path and a shared `_apply_stop`. No firmware change.

**Tech Stack:** Python 3.14, Home Assistant custom integration, `paho`/HA MQTT, `uv`, `pytest`,
`ruff`, `mypy --strict`.

## Global Constraints

- **Python 3.14**; `uv` only (`uv run pytest`, `uv run ruff check --fix`, `uv run ruff format`,
  `uv run mypy --strict custom_components/`). Never `pip`.
- **Never disable a lint/type rule** — no `# noqa`, `# type: ignore`. `select = ["ALL"]`; PLR2004 allows
  `0`/`1` only; PLR0915 caps a function at 50 statements — split, don't suppress.
- **Test data is SYNTHETIC ONLY.** Never a real `0x5C`-prefixed RF identity or a real IP. Reuse
  `tests/synthetic.py` fixtures.
- **Gate every task:** `uv run ruff check custom_components/ tests/ && uv run ruff format custom_components/ tests/ && uv run mypy --strict custom_components/ && uv run pytest -q` — all green before commit.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- The RF frame carries **no** command_id/provenance — classification is time-correlation (spec §2).

---

## File Structure

- **Create** `custom_components/zemismart_blinds/state_sync.py` — pure RX logic: `BridgeClock`,
  `CommandLedger`, `FrameSignature`/`frame_signature`, `HeardEvent`, `StateSyncConsumer`. No HA/MQTT
  imports beyond typing; all I/O injected as callables. Depends only on `codec`.
- **Modify** `models.py` — `BridgeInfo` fields; `BridgeRegistry` parse/store/availability; hub owns the
  `StateSyncConsumer`, the RX-listener registry, ledger registration in `_async_execute`, per-channel
  publish-generation, command-scoped emission-proof memory, the disarm waiter, `handle_status`
  `"disarmed"` + clock feed, and `close()` teardown.
- **Modify** `cover.py` — RX-listener registration; `_start_heard_motion`; extracted `_apply_stop`;
  `source="heard"` motion commit; intent-generation supersession check; takeover-disarm trigger.
- **Modify** `__init__.py` — 4th subscription `(MQTT_RX_TOPIC, _handle_rx)`; `_handle_rx` `@callback`
  drops retained → `hub.handle_rx`.
- **Modify** `const.py` — update the onboarding-only `/rx` comment to reflect continuous consumption.
- **Tests** `tests/test_state_sync.py` (new), extend `tests/test_models.py`, `tests/test_cover.py`.

**Worktree/parallelism map (Codex execution):** Phase 1's `state_sync.py` (Tasks 1–4) and the
`BridgeInfo` change (Task 5, `models.py`) touch **different files** → two parallel worktrees. Everything
after wires shared `models.py`/`cover.py` and runs **serial** (one worktree, Tasks 6→12), then the test
matrix (Task 13). Merge Phase-1 worktrees before starting Phase 2.

---

## Phase 1 — Pure infrastructure (`state_sync.py`) + registry fields

### Task 1: `FrameSignature` + `frame_signature()`

**Files:** Create `state_sync.py`; Test `tests/test_state_sync.py`.
**Interfaces — Produces:**
- `FrameSignature = tuple[str, frozenset[int], str]`  # (remote_key, channels, button)
- `def frame_signature(frame_hex: str) -> FrameSignature | None` — `decode_b0` + `infer_action_button`;
  returns `None` if undecodable or the button is not `UP`/`DOWN`/`STOP`. `remote_key` is
  `f"{prefix:06x}:{remote_id:02x}"` (matches `RemoteIdentity.key`).
**Consumes:** `codec.decode_b0`, `codec.infer_action_button`.
**Tests (concrete):** a synthetic UP `AAB0…55` → `("<key>", frozenset({1}), "UP")`; a group frame →
multi-channel frozenset; a non-movement `cmd` → `None`; garbage hex → `None`. Spec §6 step 2.
**Acceptance:** gate green; `frame_signature` is a pure function (no HA imports).

### Task 2: `BridgeClock` (spec §10)

**Files:** Modify `state_sync.py`; Test `tests/test_state_sync.py`.
**Interfaces — Produces:**
- `class BridgeClock:`
  - `def observe(self, boot: int, t: int, recv_time: float) -> None`
  - `def to_ha_time(self, boot: int, t: int, recv_time: float) -> float` — returns `heard_at`, always
    `<= recv_time`.
**Behavior (spec §10):** EMA `offset = recv_time - t` per boot; **re-seed** on boot change, 2³¹
half-range ambiguity, or a large residual outlier; serial (int32) `t` deltas only while ordering is
unambiguous; reject stale/out-of-order samples from mutating the offset; `t - age_ms` done modularly by
callers. No `/info` needed first — a first sample seeds from `recv_time`.
**Tests (concrete):** steady samples → `to_ha_time` ≈ `recv_time`; a `boot` change re-seeds (old offset
discarded); a single stale sample (t far in the past) does **not** rewind the offset; a `t` near
`UINT32_MAX` then wrapping past 0 is handled (serial arithmetic); `to_ha_time` is clamped `<= recv_time`
even if the offset would project the future. Spec §10, finding 10.
**Acceptance:** gate green.

### Task 3: `CommandLedger` — pending→confirmed, full envelope (spec §5)

**Files:** Modify `state_sync.py`; Test `tests/test_state_sync.py`.
**Interfaces — Produces:**
- `@dataclass(frozen=True) class LedgerFrameSpec: signature: FrameSignature; offset_ms: int; airtime_ms: int`
  (offset from the command's handoff to this frame's own handoff; airtime of the frame).
- `class CommandLedger:`
  - `def register_pending(self, command_id: str, bridge_id: str, channels: tuple[int, ...], button: str, frames: list[LedgerFrameSpec]) -> None`
  - `def confirm(self, command_id: str, handoff: float) -> None` — fills each frame's HA-time window
    `[handoff + offset - SLACK, handoff + offset + airtime + SLACK]` (symmetric `SLACK`).
  - `def retire(self, command_id: str) -> None`
  - `def match(self, signature: FrameSignature, heard_at: float) -> LedgerMatch | None` where
    `LedgerMatch = tuple[Literal["pending","confirmed"], str, str]` = (phase, command_id, command_bridge).
  - `def gc(self, now: float) -> None` — TTL-expire; enforce per-bridge + global caps (finding 11).
**Behavior:** a PENDING entry matches by signature regardless of window (windows unknown until confirm);
a CONFIRMED entry matches only within a frame window. All frames of a command (action + trailer + stop)
are registered so none is later misread as a physical press (finding 4).
**Tests (concrete):** register→match returns `"pending"`; confirm then a heard copy inside a window →
`"confirmed"` with the command bridge; a heard copy of the **stop_raw** frame inside its window →
`"confirmed"` (not a press); outside all windows → `None`; `retire`/`gc` remove entries and enforce the
cap (register > cap → oldest evicted). Spec §5, findings 3/4.
**Acceptance:** gate green.

### Task 4: `StateSyncConsumer` classifier skeleton (spec §6)

**Files:** Modify `state_sync.py`; Test `tests/test_state_sync.py`.
**Interfaces — Produces:**
- `@dataclass(frozen=True) class HeardEvent: button: str; chans: frozenset[int]; remote_key: str; heard_at: float; bridge_id: str`
- `class StateSyncConsumer:`
  - `def __init__(self, *, ledger: CommandLedger, clock: BridgeClock, dispatch: Callable[[HeardEvent], None], on_emission_proof: Callable[[str], None], now: Callable[[], float]) -> None`
  - `def handle_rx(self, bridge_id: str, boot: int, t: int, frame_hex: str, recv_time: float) -> None`
    — the full pipeline: exact-event dedup `(bridge,boot,t,normalized_frame)` → decode → clock-convert →
    ledger match (confirmed=echo[+`on_emission_proof(command_id)` if bridge≠command_bridge]; pending=hold;
    none=press → burst-debounce → `dispatch(HeardEvent)`).
  - `def resume_holds(self, command_id: str) -> None` — re-run held captures when a pending entry
    confirms/retires (called by the hub on `started`/timeout).
  - `def close(self) -> None` — clear all bounded maps.
**Behavior:** bounded exact-event cache + debounce map + hold queue, all capped and cleared in `close()`.
`dispatch`/`on_emission_proof` are injected (the hub wires them to covers). No HA imports.
**Tests (concrete):** a fresh press frame → `dispatch` called once with the right `HeardEvent`; the same
`(bridge,boot,t,frame)` twice → dispatched once (exact dedup); two different-`t` repeats of one press
within debounce → dispatched once; a frame matching a confirmed ledger entry heard by a **different**
bridge → **no dispatch**, `on_emission_proof(command_id)` called; a frame matching a **pending** entry →
held (no dispatch) until `resume_holds` after `confirm` reclassifies it as echo. Spec §6, findings 3/5.
**Acceptance:** gate green.

### Task 5: `BridgeInfo` gains `boot`/`listen`/`contract_v` (spec §9) — *parallel with Tasks 1–4*

**Files:** Modify `models.py` (`BridgeInfo`, `BridgeRegistry.update_info`/`_store`/`update_availability`);
Test `tests/test_models.py`.
**Interfaces — Produces:** `BridgeInfo` gains `boot: int | None`, `listen: bool | None`,
`contract_v: int | None` (frozen). No signature change to `update_info`/`update_availability`.
**Behavior (spec §9, finding 12):** parse strictly in `update_info` (reject bool-as-int for `boot`/`v`;
`listen` must be a real bool else `None`=unknown); thread through **every** `BridgeInfo` construction via
`dataclasses.replace(current, …)` so `update_availability` never drops them; include the nullable fields
in `_store`'s meaningful-state predicate (an info-only bridge is not pruned); clear on a
complete-document tombstone.
**Tests (concrete):** `update_info` with `{boot,listen:false,v:2}` populates the fields;
`listen:false` stays `False` (not truthy); a following `update_availability(online=False)` **preserves**
`boot`/`listen`/`v`; a `boot` given as `"3"` (string/bool) is rejected to `None`; an info-only bridge
survives the prune predicate. Spec §9, findings 11/12.
**Acceptance:** gate green.

**Phase-1 merge:** merge the `state_sync.py` worktree and the `BridgeInfo` worktree (different files →
clean) before Phase 2.

---

## Phase 2 — Hub wiring & subscription (serial, `models.py` + `__init__.py`)

### Task 6: Clock feed + `handle_status` `started` t/boot + `"disarmed"` branch

**Files:** Modify `models.py` (`handle_status`), maybe `const.py`; Test `tests/test_models.py`.
**Interfaces — Produces:**
- Hub holds `self._state_sync: StateSyncConsumer` and `self._clock: BridgeClock` (constructed in Task 7,
  but `handle_status` starts feeding the clock from `started` t/boot here) — coordinate ordering with
  Task 7; if Task 7 lands first this is trivial.
- New `handle_status` branch: `status == "disarmed"` resolves a disarm waiter keyed `(bridge_id,
  command_id)` (Task 11) — for now just parse+route to a `hub.on_disarmed(bridge_id, command_id)` hook.
**Behavior:** read `t`/`boot` on `started` (feed the clock); add the `disarmed` route. Do **not** reuse
`_pending` for disarm (removed after `started`, spec §3).
**Tests:** a `started` payload with `t`/`boot` feeds the clock (observable via a spy);
a `disarmed` payload routes to `on_disarmed`; an unknown status is still rejected as before. Findings 7.
**Acceptance:** gate green.

### Task 7: `StateSyncConsumer` construction + RX-listener registry (spec §4)

**Files:** Modify `models.py` (hub `__init__`, listener registry, `close()`); Test `tests/test_models.py`.
**Interfaces — Produces:**
- `def register_rx_listener(self, remote_key: str, channels: frozenset[int], callback: Callable[[HeardEvent], None]) -> Callable[[], None]` (returns an unsubscribe), stored in `self._rx_listeners`.
- Hub constructs `self._clock`, `self._ledger`, `self._state_sync = StateSyncConsumer(ledger=…, clock=…, dispatch=self._dispatch_heard, on_emission_proof=self._record_emission_proof, now=…)`.
- `def handle_rx(self, bridge_id: str, payload: Mapping, recv_time: float) -> None` — validate/parse
  `{frame,t,boot}`, feed the clock, delegate to `self._state_sync.handle_rx(...)`.
- `def _dispatch_heard(self, event: HeardEvent) -> None` — resolve listeners by `remote_key` +
  **containment** (spec §6.A) and invoke the owner batch (cover side does the batch in Task 9).
- `close()` clears listeners, cancels tasks, and calls `self._state_sync.close()` (finding 11).
**Behavior:** listener lookup is metadata-only (no `_COVERS` import); per-bridge state is bounded by the
registry's bridge-id/count caps against forged ids.
**Tests:** register a listener, `handle_rx` a matching press → the listener callback fires with the right
`HeardEvent`; a forged/unknown bridge id does not grow unbounded (cap); `close()` clears everything.
Spec §4, finding 11.
**Acceptance:** gate green.

### Task 8: 4th subscription + `_handle_rx` (spec §4, §12) + `const.py` comment

**Files:** Modify `__init__.py` (add the sub + `_handle_rx`), `const.py` (comment); Test
`tests/test_init.py`.
**Interfaces — Consumes:** `hub.handle_rx`. **Produces:** a 4th entry in the subscription tuple at
`_async_initialize_domain_runtime`; `_handle_rx(runtime, msg)` is `@callback`, **drops retained**, parses
JSON, calls `hub.handle_rx(bridge_id, payload, recv_time)`.
**Behavior:** mirror `_handle_status`'s retained-drop + `_bridge_id` parse. Update the `const.py`
onboarding-only `/rx` comment to state continuous, opt-in consumption.
**Tests:** a retained `/rx` message is ignored; a live `/rx` reaches `hub.handle_rx` (spy); a malformed
payload is dropped without raising. Spec §4/§12, finding 5.
**Acceptance:** gate green.

---

## Phase 3 — Mirror, supersession, emission-proof, disarm (serial, `cover.py` + `models.py`)

### Task 9: Cover RX-listener + `source="heard"` mirror + containment batch (spec §6.A, §7)

**Files:** Modify `cover.py` (register listener in `async_added_to_hass`; `_start_heard_motion`; split
the common motion commit from `_record_ack`); Test `tests/test_cover.py`.
**Interfaces — Produces:**
- `def _start_heard_motion(self, event: HeardEvent) -> None` — UP→open-full / DOWN→close-full via the
  **common motion commit** with `source="heard"` (`started_at=event.heard_at`, `deadline=None`,
  `absolute_anchor=True`); must **not** call `_record_ack` routing/`degraded` mutations (finding 9).
- The hub `_dispatch_heard` applies the **containment batch** (spec §6.A): every cover whose channels ⊆
  pressed `chans` moves (group owns member propagation via `_member_covers`, standalone contained covers
  once, dedup); a partially-overlapped cover is `_mark_unknown` (finding 2).
**Behavior:** never transmits. Reuse `_start_motion`/`_start_member_motion`.
**Tests:** a heard UP on an exact-match cover opens it (position climbs), no MQTT publish, `last_bridge`
/`degraded` unchanged; a heard `{1,2}` with a `{1,2}` group + members moves all once (no double-invalidate);
a heard `{1}` against only a `{1,2}` group marks it unknown. Spec §6.A/§7, findings 2/9.
**Acceptance:** gate green.

### Task 10: Shared `_apply_stop` + heard STOP (spec §7, finding 8)

**Files:** Modify `cover.py` (extract `_apply_stop(at, *, provenance)` from `_async_stop`; heard STOP
path); Test `tests/test_cover.py`.
**Interfaces — Produces:** `def _apply_stop(self, at: float, *, provenance: str) -> None` performing
`_interrupt_motion` + `_reconcile_unverified_anchor` + member propagation + `_reconcile_overlaps` +
state writes. `_async_stop` (transmitted) and `_start_heard_motion`'s STOP branch both call it.
**Behavior:** heard STOP does **not** transmit and needs no disarm.
**Tests:** a heard STOP freezes at the estimate **and** runs `_reconcile_unverified_anchor` (a
known-offline exempt full travel becomes unknown on the heard STOP, matching the transmitted-STOP path);
the transmitted STOP path is unchanged (existing tests still pass). Spec §7, finding 8.
**Acceptance:** gate green.

### Task 11: Ledger registration + publish-generation + intent-generation supersession (spec §5, §6.A, finding 1)

**Files:** Modify `models.py` (`_async_execute` registers the pending ledger before publish; per-channel
publish generation; `resume_holds` on `started`/timeout) and `cover.py` (intent generation checked after
each command `await` and before `_start_motion`); Test `tests/test_models.py`, `tests/test_cover.py`.
**Interfaces — Produces:**
- In `_async_execute`: build `list[LedgerFrameSpec]` from the command's action/trailer/stop frames + call
  `ledger.register_pending(...)` **before** the publish task; on `started` → `ledger.confirm(command_id,
  handoff)` + `state_sync.resume_holds(command_id)`; on displaced/rejected/timeout → `ledger.retire` +
  `resume_holds`.
- A physical press (in `_dispatch_heard`) **bumps the per-channel publish generation** (extend the
  existing `_publish_seq`/`overlap_token` mechanism, `models.py:~787`) and sets a per-cover **intent
  generation**; `_start_motion` and every command caller re-check it after their `await` and abort (like
  the existing `if result == "superseded": return None`, `cover.py:718`) if a press superseded.
**Behavior:** closes the "delayed commanded ack overwrites a press" and "peer hears TX before started"
races. The firmware atomic-abort `disarm` cancels the superseded command (Task 12).
**Tests (race):** a timed DOWN awaiting `started` + a physical UP heard → after the DOWN's `started`
resolves, `_start_motion` **aborts** (press wins, model shows opening); a press between a set-position
overlap-token snapshot and publish → the stale movement resolves superseded and does not transmit; a peer
`/rx` of our just-issued (pending) command → held, then classified as echo on confirm (no false press).
Spec §5/§6.A, findings 1/3.
**Acceptance:** gate green.

### Task 12: Command-scoped emission proof + takeover-disarm waiter (spec §7, §8, findings 6/7)

**Files:** Modify `models.py` (emission-proof memory keyed by `command_id`; disarm waiter + retry task;
`on_disarmed`) and `cover.py` (anchor upgrade for the exact command; takeover-disarm trigger); Test
`tests/test_models.py`, `tests/test_cover.py`.
**Interfaces — Produces:**
- `def _record_emission_proof(self, command_id: str) -> None` — bounded recent-proof map; a cover that
  later commits (or already holds) an anchor from **that exact command_id** upgrades unverified→verified;
  it **never** clears an unrelated `_unverified_anchor_bridge` (finding 6, correction B).
- `async def _disarm(self, bridge_id: str, command_id: str, deadline: float) -> None` — publish
  `/cmd disarm`, retry (deduped by `(bridge,command_id)`) until `on_disarmed` resolves or `deadline`;
  scheduled as a task, cancelled in `close()`.
- Cover takeover: on a heard UP/DOWN whose owner is mid **timed-partial** (`_motion_timed`), snapshot
  `(bridge, command_id, deadline)` **before** the mirror clears the fields, start `_disarm`, and on
  disarm timeout near the deadline **freeze/mark-unknown at the old deadline** (finding 7).
**Behavior:** disarm only for UP/DOWN takeovers; a heard STOP needs none.
**Tests (race):** peer proof of command C upgrades only C's cover anchor, and a **different** cover's
restored-STOP `_unverified_anchor_bridge` is **preserved** when its bridge later reports offline; a heard
UP during a timed move publishes `disarm` and, on `disarmed` ack before the deadline, keeps modeling; a
disarm that times out at the deadline freezes/marks-unknown instead of modeling through. Spec §7/§8,
findings 6/7.
**Acceptance:** gate green.

---

## Phase 4 — Race test matrix & docs

### Task 13: Full race/edge test matrix (spec §13, finding 14)

**Files:** Extend `tests/test_state_sync.py`, `tests/test_models.py`, `tests/test_cover.py`.
**Tests (deterministic, add any not already covered by Tasks 1–12):** `/rx` before `started`; press
between `started` resolution and `_start_motion`; queued/unpublished set-position cancellation;
overlap-token invalidation by a press; exact vs partial/superset group intersections; retained + delayed
QoS duplicates; a delayed `stop_raw` not mirrored; emission proof arriving **before** the cover model
commits (bounded-memory replay); unrelated restore-anchor preservation under peer proof; disarm
dedup + deadline-timeout freeze; `close()` cleanup/bounds; neighbor-identical-remote residual (document +
assert current trust-and-mirror behavior). Spec §13/§14.
**Acceptance:** full gate green; coverage of every spec §15 disposition row demonstrable.

---

## Self-Review

**Spec coverage:** §4 arch → T7/T8/T9; §5 ledger → T3/T11; §6 pipeline → T4; §6.A ordering/batch →
T9/T11; §7 mirror/emission → T9/T10/T12; §8 disarm → T12; §9 `/info` → T5; §10 clock → T2; §11 lifecycle
→ T4/T7/T12; §12 privacy → T8; §13 tests → T13; §14 non-goals → T13 (residual assertion); §15 findings →
mapped per-task. All 15 sections covered.

**Placeholder scan:** none — each task carries files, exact interface signatures, concrete test cases,
and the spec section. (Implementation code is authored by the Codex worker from the spec, per the header
calibration — the *tests* are the specified acceptance gates.)

**Type consistency:** `FrameSignature`, `HeardEvent`, `LedgerFrameSpec`, `LedgerMatch`,
`register_rx_listener`, `handle_rx`, `register_pending`/`confirm`/`retire`/`match`/`gc`, `_apply_stop`,
`_start_heard_motion`, `_record_emission_proof`, `_disarm`, `resume_holds` — names/signatures are
consistent across the tasks that produce and consume them.
