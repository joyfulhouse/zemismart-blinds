# Remote-Centric Redesign — Design Spec

Date: 2026-07-17
Status: Approved pending user review
Branch: new branch off `main` (implemented in a worktree, independent of `feat/state-sync-consumer`)

## Problem

The integration currently represents each blind or channel-group as its own config
entry. A physical Zemismart remote controls 1–6 channels, so one remote fans out
into several disconnected entries. That model forces:

- Fragmented onboarding: each shade of one remote is a separate flow run.
- A calibration-propagation hack (`_propagate_calibration`) copying recalibrated
  bases across sibling entries that share a remote.
- Cross-entry validation (`_cross_area_overlap`) to keep overlapping channel sets
  routable through one bridge.
- Group entities (e.g. ch1-6) whose state is modeled independently and disagrees
  with their member shades.

Target example (kitchen): **one remote** with covers *Kitchen slider shades*
(ch1-3), *Kitchen counter shade* (ch4), *Kitchen sink shade* (ch5), and the
aggregate *Kitchen shades* (ch1-6).

## Decisions (user-approved)

| Decision | Choice |
|---|---|
| Architecture | Config subentries: remote = config entry, each cover = subentry |
| Migration | No migration code; manual swap on the live HA at rollout (runbook below), entity IDs must stay stable |
| Device topology | Remote = parent device; each cover = child device via `via_device` |
| Area | Per remote; covers inherit it; RF routing uses it; cross-area guard deleted |
| Group `set_position` | Fan out to member covers with their own travel timing |
| Group state | Aggregate from members: closed only when all closed, opening/closing when any member is, position = member average |
| Onboarding | One wizard: capture remote, then a repeating add-cover step |

## Data model

### Remote (config entry data)

- `name` — entry title, e.g. "Kitchen remote".
- `prefix` (24-bit), `remote_id` (8-bit) — RF identity.
- `base_up`, `base_down`, `base_stop`, optional `base_trailer` — calibration.
- `area_id` — the remote's room; inherited by all covers; used for bridge routing.
- `repeats`, `coalesce_window_ms` — RF transport tuning, now per remote (they
  describe the remote's protocol, not a shade).

Entry `unique_id` = remote key (`{prefix:06x}:{remote_id:02x}`). Duplicate
remotes abort with `already_configured`. The entry owns the single copy of the
calibration; `_propagate_calibration` is deleted.

### Cover (subentry, type `cover`)

- `name` — e.g. "Kitchen sink shade".
- `channels` — normalized sorted channel set, 1..16, unique per remote.
- `travel_up`, `travel_down` — **optional**. Present → full position model.
  Absent → open/close/stop only with assumed state (and irrelevant when the
  cover is an aggregate).

Subentry `unique_id` = normalized channel key (e.g. `1-2-3`), enforcing one
cover per channel set per remote — the same uniqueness rule as today's
`remote_key:channels` entry unique_id, scoped to the entry.

### Runtime derivation

`BlindConfig` remains the per-cover runtime config consumed by `cover.py`, the
hub, and state_sync — but it is now **derived** (remote fields + cover fields),
not stored. Its travel-time fields become `float | None`; validation accepts
None and position modeling is gated on presence. This keeps hub/state-sync
signatures nearly untouched to minimize collision with the in-flight
state-sync consumer work.

### Membership (derived, never stored)

Within one entry, cover B is a **member** of cover A iff `set(B.channels)` is a
proper subset of `set(A.channels)`.

- A cover with ≥1 member is an **aggregate** (Kitchen shades ← slider, counter,
  sink).
- A cover with no members is a **leaf** with its own travel-time position model.
- Channels claimed by no cover (ch6 in the example) still receive RF in group
  frames but contribute nothing to state.
- Partial overlaps (neither subset nor superset, e.g. ch1-2 vs ch2-3) are
  permitted but create no membership.
- Membership is recomputed on entry reload (every subentry add/edit/delete
  reloads), so adding a subset cover later converts a leaf into an aggregate
  automatically.

## Devices and entities

- Remote device: `identifiers={(DOMAIN, entry_id)}`, area = remote's area,
  manufacturer Zemismart, model "RF433 remote".
- Cover device per subentry: `identifiers={(DOMAIN, subentry_id)}`,
  `via_device=(DOMAIN, entry_id)`, created through the subentry so HA ties
  device and subentry lifecycles together.
- One cover entity per subentry; entity `unique_id` = `subentry_id`. This is
  stable through remote recalibration AND channel edits, so re-channeling or
  relearning never breaks entity IDs or automations.

### Leaf cover behavior

Today's `ZemismartCover` unchanged: travel-time position model, timed STOP
positioning, RX heard-event listener registered with the hub for its channels.
Without travel times: open/close/stop only, no position, assumed state.

### Aggregate cover behavior (new entity class)

Cover-group semantics driven by members:

- `is_closed` = all members closed; position = average of member positions
  (HA renders non-0/100 averages as partially open). Members without a
  position model (no travel times) are excluded from the average and from
  `is_closed`; if no member has known state, the aggregate reports assumed
  state with no position.
- `is_opening` / `is_closing` = any member opening/closing — regardless of
  whether the member was moved individually, via the aggregate, or by a
  physical remote press (member RX listeners already capture presses; the
  aggregate only re-derives).
- **open/close/stop**: one RF frame addressed to the aggregate's full channel
  set through the existing hub path (single frame, as RF-verified). On the
  started ack, the entry coordinator tells each member to start (or freeze,
  for STOP) its own motion model at the acked timestamp. Members do not
  transmit in this path.
- **set_position**: delegates to each member's own positioning logic with its
  own travel timing. The hub's existing same-direction coalescing merges
  frames where possible.
- Aggregates register no RX listener of their own and own no position model.

### Entry coordinator

New small per-entry object built during setup: covers indexed by subentry id,
the membership graph, and the member-notification path used by aggregate
commands. It is in-process plumbing only; no storage, no MQTT surface.

### State-sync coordination note (deferred)

An aggregate's group command enters the hub ledger with the full channel set,
owned by an entity that models no position itself — a new command-ownership
shape for the state-sync consumer's takeover/disarm logic. Deliberately NOT
solved here; flagged for the state-sync work to account for after both
branches land. Existing state-sync tests must keep passing unmodified.

## Flows

### Onboarding (config flow)

1. Menu: **Learn** (primary) | **Advanced**.
2. Advanced menu: **Manual** (identity + calibration form) | **Virtual**
   (synthesized identity). The old **Reuse known remote** path is deleted —
   remotes are entries now; duplicates abort on unique_id.
3. Learn path (existing machinery retained): form collects remote name, area,
   bridge → sniff progress step → confirm decoded identity (prefix, id,
   pressed channels, button).
4. Remote settings step: repeats + coalesce window in a collapsed advanced
   section (defaults prefilled).
5. **Cover loop**: cover form (name, channels — on the Learn path the first
   iteration prefills channels from the sniffed press — optional travel
   times), then a menu:
   **Add another cover** | **Finish**. Each iteration validates channel-set
   uniqueness against covers already collected.
6. Finish creates the entry with all collected cover subentries in one shot.

At least one cover is required before Finish is offered.

### Subentry flows (type `cover`)

- **Add**: same cover form, validated against existing subentries.
- **Reconfigure**: same form prefilled; rename, change channels (subentry
  unique_id updates to the new channel key), adjust travel times.
- **Delete**: native HA subentry delete; device/entity removed, entry reloads,
  membership recomputed.

### Entry reconfigure flow (the remote)

Menu:

- **Relearn**: sniff path re-runs; on success replaces prefix/remote_id/bases
  (unique_id collision with another entry aborts). Subentries untouched;
  entities survive because their unique_ids are subentry-based.
- **Edit settings**: name, area, repeats, coalesce window, and manual
  calibration fields (bases + optional trailer) for expert correction.

The old options flow is deleted; entry data is the single source of truth
(`effective_values`' data/options merge goes away).

## Deployment runbook (manual, no migration code)

Performed by Claude on the live HA at rollout, per remote:

1. Record each existing cover's name and entity_id.
2. Delete the old per-blind config entries for that remote.
3. Onboard the remote via the new wizard, naming covers identically.
4. Verify each new entity received the original entity_id (deleting first
   frees the slug); repair any `_2`-suffixed IDs via the entity registry.
5. Spot-check automations referencing those entity_ids.

## Testing

- **Flow tests**: wizard end-to-end (learn → settings → multi-cover loop →
  entry + subentries), duplicate-remote abort, channel-collision rejection
  inside the loop and in subentry add/reconfigure, subentry delete reload,
  relearn preserving subentries, manual/virtual paths.
- **Model tests**: BlindConfig derivation from remote + cover records,
  optional travel-time validation, membership derivation (subset rule,
  partial-overlap non-membership, dynamic recompute).
- **Cover tests**: aggregate state math (all-closed, any-moving, average
  position), group open/close single frame + member model start/freeze,
  set_position fan-out, leaf behavior unchanged, travel-time-less leaf
  degradation.
- **Guardrail**: `test_state_sync.py` passes without modification.

## Out of scope

- Any change to the state-sync consumer beyond keeping its tests green.
- RX/takeover semantics for aggregate-owned commands (flagged above).
- Migration/repair code for old entries.
