# Plan 03b — Coordinator + Aggregate Covers

> Implementer: controller (Fable 5) inline. Baseline: 641 green at `279dd80`.
> Files: new `coordinator.py`; `models.py` (RemoteRuntime.coordinator field);
> `cover.py` (leaf simplification + aggregate entity + setup wiring);
> `tests/test_cover.py` (group-semantics tests rewritten as aggregate tests);
> `tests/test_init.py` (aggregate entity now created). `test_state_sync.py`
> untouched.

## Design (spec rev 3 §Aggregate/§Coordinator, laminar-simplified)

**Heard-press semantics collapse to per-leaf rules** (laminar family makes the
old owner/suppression heuristics unnecessary):
- leaf.channels ⊆ pressed → the leaf models the press itself (motion/STOP);
- leaf.channels ∩ pressed ≠ ∅ (partial) → the leaf invalidates (unknown);
- disjoint → ignore.
Aggregates never model presses — their state derives from members and
recomputes via reverse notification. The old `_COVERS` registry,
`_member_covers`, `_overlapping_covers`, `_reconcile_overlaps`,
`_heard_press_owned_by_group`, and `_mark_unknown_and_notify_members` are
deleted from the leaf (`_start_member_motion` stays — the aggregate drives it).

**`RemoteCoordinator`** (new `coordinator.py`): per-entry object holding the
cover configs by subentry id, derived roles, aggregate→leaf-members map
(leaves only), and live entity registrations. Reverse path:
`member_changed(subentry_id)` marks containing aggregates dirty and schedules
ONE `loop.call_soon` flush that writes each dirty aggregate's state (batching
multi-member events into one write per aggregate per iteration). Forward path
is invoked by the aggregate entity: `members_of(aggregate_id)` returns live
leaf entities for command fan-out.

**Leaf integration:** `ZemismartCover` gains an optional
`coordinator`/`subentry key` registration: `async_added_to_hass` registers,
`async_will_remove_from_hass` unregisters, and `async_write_ha_state` also
notifies `member_changed`. Constructor takes `coordinator: RemoteCoordinator | None = None`.

**`ZemismartAggregateCover`** (in `cover.py`): `CoverEntity` (no restore).
- State from live, available members: `is_closed` all-closed / any-open False /
  else None; position = unweighted mean of known member positions (None if
  none); `is_opening`/`is_closing` any member (opening first); `available` =
  any bridge online AND at least one member registered.
- `async_open_cover`/`close`: single untimed full-set frame via
  `hub.async_transmit(config, button)`; on ack, drive each member's
  `_start_member_motion(motion, ack=ack, direction, duration=<frame margin>, group_target=0/100)`
  exactly like the old group entity's `_start_motion` member loop.
- `async_stop_cover`: cancel any pending set_position fan-out tasks first
  (STOP never waits on fan-out), transmit STOP, then freeze members
  (`_record_ack` + `_interrupt_motion(ack.started_at)` + write) — the old
  `_apply_stop` member loop.
- `async_set_cover_position`: 0/100 → open/close. Else concurrent fan-out:
  one task per member running `member.async_set_member_position(target)`
  (new small leaf method: `async with self._command_lock: await
  self._async_set_position_locked(target)`); gather with per-member typed
  outcomes; superseded == success; failures aggregate into one
  `HomeAssistantError` naming members after all settle.
- Takeover surface: registers a hub RX listener with its full channel set and
  minimal `TakeoverCoverState` (last command bridge/id/button with the
  untimed disarm-drain deadline), so bridge-held state from aggregate
  commands stays disarm-able; `invalidate_takeover` clears the tracked
  command. No model application in its RX callback.

**Setup wiring** (`cover.py.async_setup_entry`): build the coordinator from
ALL parsed covers, store it on `runtime.coordinator`, create leaf entities
(as today, now passing the coordinator) AND one `ZemismartAggregateCover` per
aggregate subentry (same `config_subentry_id` binding + child-device area
pattern). `models.py`: `RemoteRuntime.coordinator: RemoteCoordinator | None = None`
(TYPE_CHECKING import; assigned during platform setup).

**Tests:** rewrite the old group-fan-out tests in `test_cover.py` as aggregate
tests (aggregate over two single-channel leaves: open drives both member
models; heard full-set press models both members and the aggregate derives;
partial press invalidates only the intersected leaf; set_position fan-out with
one member position-unknown raises naming that member; STOP freezes members
at ack; batched recompute writes the aggregate once). test_init's topology
test gains the aggregate entity assertion (3 adds, aggregate bound to its
subentry).

## Definition of done

Full suite green; `mypy --strict` clean; ruff clean; `test_state_sync.py`
untouched.
