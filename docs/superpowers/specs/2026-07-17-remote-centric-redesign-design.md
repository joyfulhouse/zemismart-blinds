# Remote-Centric Redesign — Design Spec

Date: 2026-07-17 (rev 2 — post Codex GPT-5.6-sol adversarial review)
Status: Approved pending re-review
Branch: `feat/remote-centric-model` (worktree `.worktrees/remote-centric`, off `main`)

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
| Migration | No migration code; manual swap on the live HA at rollout (runbook below), entity IDs repaired at the registry level |
| Device topology | Remote = parent device; each cover = child device via `via_device` |
| Area | Per remote; assigned to child devices at creation only; user device-area overrides respected; RF routing always uses the remote's configured area |
| Channel sets | Partial overlaps banned within a remote: every pair of covers must be disjoint or strictly nested |
| Travel times | Required when a cover is created as a leaf; hidden when created as an aggregate |
| Group `set_position` | Fan out to **leaf** members only, each with its own travel timing |
| Group state | Aggregate from leaf members: closed only when all closed, opening/closing when any member is (HA cover-group precedence), position = average |
| Onboarding | One wizard: capture remote, then a repeating add-cover step |
| Reload ownership | The integration's entry update listener is the single reload owner for subentry mutations |

## Data model

### Remote (config entry data)

- `name` — entry title, e.g. "Kitchen remote".
- `prefix` (24-bit), `remote_id` (8-bit) — RF identity.
- `base_up`, `base_down`, `base_stop`, optional `base_trailer` — calibration.
- `area_id` — the remote's room; used for bridge routing and initial child
  device area.
- `repeats`, `coalesce_window_ms` — RF transport tuning, per remote.

Entry `unique_id` = remote key (`{prefix:06x}:{remote_id:02x}`).

**Uniqueness guards are explicit, not implicit** (HA replaces same-unique-id
entries rather than aborting): creation calls `async_set_unique_id` +
`_abort_if_unique_id_configured`; relearn/reconfigure scans all other entries
for the new key and aborts `already_configured` on collision before
`async_update_reload_and_abort`.

The entry owns the single copy of the calibration; `_propagate_calibration`
and `_cross_area_overlap` are deleted. Entry options are unused; entry data is
the single source of truth (`effective_values` merge goes away).

### Cover (subentry, type `cover`)

- `name` — e.g. "Kitchen sink shade". Stored in subentry data as the source of
  truth; flows keep the subentry title mirrored to it. Out-of-band title-only
  edits (backend API) are cosmetic and do not affect entity naming.
- `channels` — normalized sorted channel set, 1..16.
- `travel_up`, `travel_down` — required when the cover is created as a leaf;
  omitted (and hidden in the form) when the cover is created as an aggregate,
  i.e. its channels strictly contain an existing cover's.

Subentry `unique_id` = normalized channel key (e.g. `1-2-3`) — one cover per
channel set per remote. Subentry reconfigure updates it via
`async_update_subentry` (HA collision-checks sibling subentries).

**Channel-set validation (add and reconfigure):** against every other cover of
the remote, the new set must be disjoint, a strict superset, or a strict
subset. Partial overlaps (e.g. `{1,2}` vs `{2,3}`) are rejected with a form
error. Equal sets are rejected by unique_id collision.

### Roles and membership (derived, never stored)

- A cover is an **aggregate** iff its channel set strictly contains at least
  one other cover's. Otherwise it is a **leaf**.
- An aggregate's **members are its leaf covers only**: every leaf whose
  channels are a subset of the aggregate's. Nested aggregates are not members
  — commands and state never traverse them, so each physical channel is
  commanded and counted exactly once. (The nesting rule makes leaves pairwise
  disjoint: a cover containing another cover is by definition an aggregate.)
- Channels claimed by no cover (ch6 in the example) still receive RF in the
  aggregate's frames but contribute nothing to state.
- Roles/membership are recomputed on entry reload. Adding a strict-subset
  cover later converts a leaf into an aggregate; its stored travel times
  become unused. Deleting the last member demotes an aggregate to a leaf: if
  it has no stored travel times the entity becomes **unavailable** with a
  repair-style log pointing at subentry reconfigure to add times.

### Runtime derivation

`BlindConfig` remains the per-cover runtime config consumed by `cover.py`, the
hub, and state_sync, now **derived** (remote fields + cover fields), not
stored. It gains an explicit `role` (leaf/aggregate) instead of inferring
behavior from `len(channels)`; travel-time fields are optional **only** for
the aggregate role (leaf runtime always has them or the entity is
unavailable). Existing uses of `is_group` are audited against the new role
field (channel-count grouping and role are no longer the same thing — a
multi-channel cover with no members is a leaf).

## Devices and entities

- Remote device: created **before** platform forwarding via
  `device_registry.async_get_or_create` (`identifiers={(DOMAIN, entry_id)}`,
  area = remote's area, manufacturer Zemismart, model "RF433 remote") so
  children's `via_device` always resolves.
- Cover device per subentry: `identifiers={(DOMAIN, subentry_id)}`,
  `via_device=(DOMAIN, entry_id)`. Entities are added with
  `async_add_entities(..., config_subentry_id=...)` so HA ties entity/device
  records to the subentry and native subentry deletion cleans them up.
- Child device area: set to the remote's area **at creation only** (HA does
  not inherit area through `via_device`). User overrides from the device page
  are never overwritten on reload — the current force-reassign
  (`_async_assign_device_area`) is deleted. Overrides affect display, area
  targeting, and voice only; bridge routing keeps using the remote's
  configured `area_id`.
- One cover entity per subentry; entity `unique_id` = `subentry_id` — stable
  through remote recalibration and channel edits.
- Deleting the final cover subentry is allowed; the entry then exposes only
  the remote device.

### Leaf cover behavior

Today's `ZemismartCover` semantics: travel-time position model, timed STOP
positioning, RX heard-event listener registered for its channels, restore on
startup. Restore extra data now records `role` and `channels`; restored
position is discarded when either differs from the current derivation
(topology changes must not seed an incompatible model).

### Aggregate cover behavior

State is derived from leaf members; RF behavior matches today's group entries:

- **RX/takeover topology is unchanged from today**: the aggregate registers
  its own RX listener with its full channel set and owns takeover state for
  its commands, exactly as current group entries do. Existing member
  suppression mechanics (a member yielding to a containing group's press)
  keep working against it. State-sync listener topology therefore does not
  change in this redesign; only entity-state *derivation* changes.
- `is_closed` = true iff every member reports closed; false if any member is
  open; unknown if no member is open and any member's position is unknown.
  Position = average over members with known positions; unknown if none.
- Availability follows HA cover-group semantics: available while at least one
  member is available; state derives from available members.
- `is_opening` / `is_closing` = any member opening/closing, with HA's standard
  precedence (opening reported first) when both are true.
- **open/close/stop**: one RF frame addressed to the aggregate's full channel
  set through the existing hub path. On the started ack, the coordinator
  notifies each member to start (or freeze, for STOP) its own motion model at
  the acked timestamp. Members do not transmit in this path.
- **set_position**: delegates concurrently to each leaf member's positioning
  logic (`asyncio.gather`-style): members are commanded in parallel, not
  sequentially, and go through the hub queue as independent commands (timed
  frames do not coalesce — accepted RF cost). Partial failure: the service
  call raises an aggregated error naming failed members after all delegations
  settle; successful members keep their outcome. Supersession per member
  follows existing per-channel generation rules.

### Entry coordinator

Per-entry object built at setup: covers indexed by subentry id, the derived
role/membership map, and two notification paths:

- **Forward** (aggregate command → members): start/freeze member models on
  group command acks, as above.
- **Reverse** (member change → aggregates): every member-model mutation —
  service commands, RX-driven changes, motion completion, restore,
  invalidation, availability — marks containing aggregates dirty. Dirty
  aggregates recompute and write state **once per event-loop iteration** (a
  scheduled zero-delay callback coalesces multi-member events into one state
  write, preventing 25%→50%→75% intermediate writes from a single RX event).

### Reload and lifecycle

- The integration registers an entry **update listener**; it is the single
  reload owner. Native subentry add (which only stores data) and delete, and
  subentry reconfigure (which uses the non-reloading
  `async_update_and_abort`), all funnel through it: any subentry mutation
  schedules an entry reload, rebuilding entities and the membership map.
  Entry-level reconfigure uses `async_update_reload_and_abort` — permitted
  because HA's restriction applies to OptionsFlowWithReload-style double
  reloads, and the update listener treats a data-identical reload as a no-op
  guard (implementation verifies exact HA behavior; requirement: exactly one
  reload per mutation).
- **Pending-command drain**: unloading an entry (including the reload in
  relearn) must ensure none of its covers' queued-but-unpublished hub
  commands can transmit afterwards — cancelled caller futures make coalesced
  contributors dead (existing rebuild drops them), and the unload path
  additionally requires the hub to skip publishing any queued command none of
  whose futures are awaited. No frame with a pre-relearn identity may
  transmit after the reload completes.

## Flows

### Onboarding (config flow)

1. Menu: **Learn** (primary) | **Advanced**.
2. Advanced menu: **Manual** (identity + calibration form) | **Virtual**
   (synthesized identity). The old **Reuse known remote** path is deleted.
3. Learn path (existing machinery retained): form collects remote name, area,
   bridge → sniff progress step → confirm decoded identity (prefix, id,
   pressed channels, button).
4. Remote settings step: repeats + coalesce window in a collapsed advanced
   section (defaults prefilled).
5. **Cover loop**: cover form (name, channels — on the Learn path the first
   iteration prefills channels from the sniffed press; travel times required
   unless the entered channels strictly contain an already-collected cover's),
   then a menu: **Add another cover** | **Finish**. Each iteration validates
   channel-set rules (uniqueness + no partial overlap) against covers already
   collected.
6. Finish creates the entry with all collected cover subentries in one shot
   (`async_create_entry(..., subentries=[...])`, supported in HA 2026.5.4).
   Creation aborts `already_configured` on remote unique_id collision.

At least one cover is required before Finish is offered.

### Subentry flows (type `cover`)

- **Add**: same cover form, validated against existing subentries (uniqueness
  + no partial overlap; travel-time requirement by born-role).
- **Reconfigure**: same form prefilled; rename, change channels, adjust travel
  times. Ends with `async_update_and_abort` (no reload — the update listener
  reloads).
- **Delete**: native HA subentry delete; subentry-bound entity and device are
  removed by HA; the update listener reloads the entry and membership is
  recomputed.

### Entry reconfigure flow (the remote)

Menu:

- **Relearn**: sniff path re-runs; on success replaces prefix/remote_id/bases
  after an explicit collision scan of other entries. Subentries untouched;
  entities survive (subentry-based unique_ids).
- **Edit settings**: name, area, repeats, coalesce window, and manual
  calibration fields (bases + optional trailer).

No options flow.

## Deployment runbook (manual, no migration code)

Performed by Claude on the live HA at rollout, per remote:

1. **Full export first**: capture every old entry's complete config (data +
   options as merged by `effective_values` — channels, travel times, repeats,
   coalesce, calibration, area), plus the entity registry records (entity_id,
   including user-customized ids, name, unique_id) and device ids. Snapshot
   automations/scripts and scenes referencing the covers, noting which use
   `device_id` targeting.
2. Reconcile per-cover repeats/coalesce to the remote level: **max** across
   the old siblings (reliability-safe).
3. Delete the old per-blind entries for that remote.
4. Onboard the remote via the new wizard using the exported channels, travel
   times, and settings; name covers identically.
5. **Registry-level entity_id repair** (names are not identity): set each new
   entity's entity_id to the recorded old value via the entity registry,
   using temporary ids two-phase when swaps collide. Customized entity_ids
   are restored from the export, not from names.
6. Rewrite `device_id`-targeted automations/scripts/scenes to the new device
   ids (or convert to entity_id targeting), from the step-1 snapshot.
7. Verify: entity_ids match the export, each automation's targets resolve,
   spot-check one motion per remote.

## Testing

- **Flow tests**: wizard end-to-end (learn → settings → multi-cover loop →
  entry + subentries), duplicate-remote abort at creation and relearn,
  channel-collision and partial-overlap rejection (loop, subentry add,
  subentry reconfigure), travel-time requirement by born-role, subentry
  delete → update-listener reload, exactly-one-reload-per-mutation.
- **Model tests**: BlindConfig derivation with explicit role, leaf/aggregate
  derivation (nesting rule, leaves-only membership, unclaimed channels,
  demotion-to-unavailable), restore role/channel discriminator.
- **Cover tests**: aggregate state math (all-closed / any-open / unknown
  propagation, average position, availability, opening-precedence on mixed
  direction), batched single-write recomputation for multi-member events,
  group open/close single frame + member model start/freeze, concurrent
  set_position fan-out with partial-failure aggregation, leaf behavior
  unchanged, RX/takeover listener registration identical in shape to today's
  group entries.
- **Lifecycle tests**: config_subentry_id binding (subentry delete removes
  exactly its entity/device), parent-device-first creation, child-area
  assigned at creation and user override preserved across reload,
  pending-command drain on unload/relearn.
- **Guardrail**: `test_state_sync.py` passes without modification.

## Out of scope

- Any change to the state-sync consumer beyond keeping its tests green
  (listener topology is intentionally unchanged; only entity-state
  derivation is new).
- Migration/repair code for old entries.
