# Takeover-disarm & restore-ordering refactor

**Date:** 2026-07-16
**Branch:** `feat/state-sync-consumer`
**Scope:** consolidation refactor of the state-sync consumer's physical-takeover handling and cover restore ordering. Behaviour-preserving except four fixes (below). All prior tests kept green.

## Why

Across the pre-merge adversarial-review loop the takeover-disarm logic — what happens to each blind when a physical remote press interrupts a live RF command — accreted into overlapping special cases: an eager unpressed-invalidation, a lazy `on_timeouts` list, an `on_resolves` list, a `_stopped_by_heard` flag, per-press channel *snapshots*, a cover-owned disarm path separate from the generic one, and three resolution paths (disarmed / timed-out / displaced) each handling a different subset of covers. Successive rounds kept finding stale-snapshot and forgotten-path edge cases; each local patch spawned the next. This refactor replaces that with one pipeline so the edge classes stop existing.

## Architecture

### C1 — one takeover-resolution evaluator over current state
`_DisarmRequest` carries only command metadata (`command_channels`, `command_button`, `remote_key`) and a **cumulative** `pressed_channels` set (union over every merged press of `press.chans ∩ command.channels`). No per-event handler lists.

`ZemismartHub._evaluate_takeover_resolution(request, outcome, *, displaced_flushed=False)` runs when the disarm resolves by **any** outcome and walks the **current** rx listeners on the command's channels, deciding each from its live `TakeoverCoverState` (no captured snapshots).

Truth table (per affected listener `L`):

| Condition | Fate |
|---|---|
| `L` runs its own *different* live command | exempt (its own lifecycle governs) |
| `L` is `stopped_by_heard` | exempt (known position; no STOP moves a stopped motor) |
| pressed (`L.channels ∩ pressed_channels ≠ ∅`), outcome `disarmed` | keep (press won) |
| pressed, outcome `timed_out` | unknown |
| pressed, outcome `displaced` | unknown **iff** `displaced_flushed` else keep |
| unpressed, movement command, any terminal outcome | unknown |
| unpressed, STOP command | keep (remaining STOP frames can't move an idle motor) |
| eager pass (`outcome is None`) | invalidate unpressed movement-command listeners |

The `displaced_flushed` bit is `CommandLedger.displace()`'s return (did the displaced command flush an owed fail-safe STOP). This preserves the J1 silent-settle (raw command, no flush, displaced → pressed mirror keeps) vs the K1 flush case (timed command → pressed mirror unknown).

### C2 — restore ordering via a generation guard
`async_added_to_hass` registers the rx listener, snapshots `restore_generation = self._intent_generation`, then `await self._async_restore_state(restore_generation)`. Live events during the await (a heard press, a takeover invalidation) apply directly and bump `_intent_generation`; the restore body returns early if the generation changed, so cached state never clobbers a newer live event and the chronologically-last event wins. Replaces the previous `_restoring`/`_restore_invalidated` boolean latch.

### C3 — one disarm targeting + liveness
`_takeover_targets` gathers the commands to disarm from **both** sources — ledger overlaps (`CommandLedger.live_overlapping`) and any intersecting cover's own modeled command (`TakeoverCoverState`, for restored motions the ledger may not carry) — dedup by `(bridge_id, command_id)` as `_TakeoverTarget`, filtered by one liveness predicate (`command_live_for_takeover`: pending OR confirmed-with-open-window; displaced excluded; ledger-absent ⇒ conservative live) and the button rule (a heard STOP only targets pending commands). One `_DisarmRequest` per command drives the C1 evaluator, so group-member fan-out is inherent (each member is its own listener) and the heard-STOP exemption applies uniformly.

## The four fixes (round-11 findings, each with a fail-before/pass-after regression)

- **M1** — heard-STOP exemption now applies on every resolution path (incl. the former cover-owned path) and is propagated to group members frozen by a heard group STOP.
- **M2** — multi-press cross-invalidation removed: cumulative `pressed_channels` means a later press's channel is never treated as unpressed by an earlier press's resolution.
- **M3** — the DISPLACED path now runs the evaluator (previously it skipped the unpressed/late-attached invalidation).
- **M5** — restore generation guard: a heard press or invalidation arriving during the restore await wins over cached state; the latch no longer overtakes a later press.
- **M4 — refuted, not implemented:** it assumed a displaced fail-safe STOP airs seconds after displacement, but the firmware flushes owed STOPs immediately and the ledger's 30 s displaced window is `match()` echo-suppression only.

## Deleted mechanisms (behaviour reproduced by the evaluator; each preserved by named tests)

`_DisarmRequest.on_timeouts` / `on_resolves`; `_RxListener.prepare` / `invalidate` / `disarm_timeout`; hub `_pressed_disarm_timeout` / `_invalidate_unpressed_listeners` / `_ignore_takeover_disarm_timeout` / `request_disarm`; cover `_prepare_heard_press` / `_request_takeover_disarm` / `_on_disarm_timeout` / `_on_takeover_disarm_timeout` / `_on_command_invalidated`; cover `_restoring` / `_restore_invalidated`. New surface: `TakeoverCoverState`, `_TakeoverTarget`, `_evaluate_takeover_resolution`, `_takeover_targets`, `_start_disarm_request`, cover `_takeover_state` / `_invalidate_for_takeover`.

## Out of scope (unchanged)

The publish barrier / `_finalize_and_publish`, the `CommandLedger` windows / `match()` echo suppression / displaced drain, emission proof, `BridgeClock` correlation, `record_commanded_start`, hold/resume, debounce, dedup, and the firmware-contract handling.
