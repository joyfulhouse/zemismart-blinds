# Remote-Centric Redesign — Design Spec

Date: 2026-07-17 (rev 4 — legacy-compat simplification, user-directed)
Status: Approved
Branch: `feat/remote-centric-model` (worktree `.worktrees/remote-centric`), rebased
onto `main@d03ce0f` so the state-sync baseline (`state_sync.py`,
`test_state_sync.py`) is present and the test guardrail is runnable.

> **Rev 4 (user directive):** no thorough backwards compatibility. Legacy
> per-blind entries do NOT keep working: `async_setup_entry` detects the old
> shape (`channels` in entry data) and raises `ConfigEntryError` with a
> migration message. The entry and its full data are KEPT (disabled device,
> preserved values) purely as the migration reference; Claude performs the
> manual migration (read values → onboard remote via wizard → delete legacy
> entries). The dual-format runtime shim is removed in Plan 03. Because
> legacy entries are inert, RF-identity uniqueness is enforced only among
> remote-format entries — a dead legacy entry holding the same identity must
> NOT block onboarding its replacement (migration order: onboard first, then
> delete legacy).

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
| Migration | No migration code; manual swap on the live HA at rollout (runbook below), entity IDs and registry customizations repaired at the registry level. Legacy entries fail setup with `ConfigEntryError` (data kept as migration reference); no dual-format runtime support (rev 4) |
| Device topology | Remote = parent device; each cover = child device via `via_device` |
| Area | Per remote; device areas set at creation only (parent and children); user device-area overrides always respected; RF routing always uses the remote's configured `area_id` |
| Channel sets | Partial overlaps banned within a remote: every pair of covers must be disjoint or strictly nested (laminar family) |
| Travel times | Required whenever a cover's **current** derived role is leaf; hidden but preserved while aggregate |
| Group `set_position` | Fan out to **leaf** members only, each with its own travel timing |
| Group state | Aggregate from available leaf members: closed only when all closed, opening/closing when any member is (HA cover-group precedence), position = unweighted member average |
| Onboarding | One wizard: capture remote, then a repeating add-cover step |
| Reload ownership | The integration's entry update listener is the **sole** reload scheduler; all flows use non-reloading update helpers |

## Data model

### Remote (config entry data)

- `name` — entry title, e.g. "Kitchen remote".
- `prefix` (24-bit), `remote_id` (8-bit) — RF identity.
- `base_up`, `base_down`, `base_stop`, optional `base_trailer` — calibration.
- `area_id` — the remote's room; used for bridge routing and initial device
  areas.
- `repeats`, `coalesce_window_ms` — RF transport tuning, per remote.

Entry `unique_id` = remote key (`{prefix:06x}:{remote_id:02x}`).

**Uniqueness guards are explicit, not implicit** (HA replaces same-unique-id
entries rather than aborting): creation calls `async_set_unique_id` +
`_abort_if_unique_id_configured`; relearn/reconfigure scans all other entries
for the new key and aborts `already_configured` on collision before updating.

The entry owns the single copy of the calibration; `_propagate_calibration`
and `_cross_area_overlap` are deleted. Entry options are unused; entry data is
the single source of truth (`effective_values` merge goes away).

### Cover (subentry, type `cover`)

- `name` — e.g. "Kitchen sink shade". Stored in subentry data as the source of
  truth; flows keep the subentry title mirrored to it. Out-of-band title-only
  edits (backend API) are cosmetic and do not affect entity naming.
- `channels` — normalized sorted channel set, 1..16.
- `travel_up`, `travel_down` — required whenever the cover's current derived
  role is leaf; hidden in the form while aggregate. **Reconfigure always
  carries forward stored travel keys even when the fields are hidden**
  (`async_update_subentry` replaces the whole mapping — hidden values must be
  explicitly merged back), so a leaf's calibration survives temporary
  promotion to aggregate and back.

Subentry `unique_id` = normalized channel key (e.g. `1-2-3`) — one cover per
channel set per remote. Subentry reconfigure updates it via
`async_update_subentry` (HA collision-checks sibling subentries). **HA does
NOT validate subentry unique_ids at initial entry creation**, so the wizard
performs one final uniqueness + laminarity validation over the whole collected
list immediately before `async_create_entry`.

**Channel-set validation (wizard loop, subentry add, subentry reconfigure):**
against every other cover of the remote, the set must be disjoint, a strict
superset, or a strict subset. Partial overlaps (e.g. `{1,2}` vs `{2,3}`) are
rejected with a form error. Equal sets are rejected as duplicates.

### Roles and membership (derived, never stored)

- A cover is an **aggregate** iff its channel set strictly contains at least
  one other cover's. Otherwise it is a **leaf**.
- An aggregate's **members are its leaf covers only**: every leaf whose
  channels are a subset of the aggregate's. Nested aggregates are not members.
  The laminar rule makes leaves pairwise disjoint, so fan-out commands address
  each physical channel at most once.
- Channels claimed by no cover (ch6 in the example) still receive RF in the
  aggregate's **open/close/stop** frames but contribute nothing to state, and
  — explicitly — are **not moved by aggregate `set_position`** (no position
  model exists for them; positioning is leaf-delegated by design).
- Roles/membership are recomputed on entry reload. Role validation in flows
  is always against the **current** derived role at submit time, not the role
  at creation. Deleting the last member demotes an aggregate to a leaf; its
  preserved travel times (if any) reactivate, otherwise the entity is
  **unavailable** with a log pointing at subentry reconfigure.

### Runtime derivation

`BlindConfig` remains the per-cover runtime config consumed by `cover.py`, the
hub, and state_sync, now **derived** (remote fields + cover fields), not
stored. It gains an explicit `role` (leaf/aggregate); travel-time fields are
optional only for the aggregate role. Existing uses of `is_group` are audited
against the new role field (a multi-channel cover with no members is a leaf,
which changes coalescing eligibility relative to today's channel-count rule).

## Devices and entities

- Remote device: created **before** platform forwarding
  (`identifiers={(DOMAIN, entry_id)}`, manufacturer Zemismart, model "RF433
  remote") so children's `via_device` always resolves.
- Cover device per subentry: `identifiers={(DOMAIN, subentry_id)}`,
  `via_device=(DOMAIN, entry_id)`. Entities are added with
  `async_add_entities(..., config_subentry_id=...)` so HA ties entity/device
  records to the subentry and native subentry deletion cleans them up.
- **Area assignment API**: `async_get_or_create`/`DeviceInfo` accept only
  `suggested_area` (an area *name*, not id). Passing the stored `area_id`
  there would create a bogus area named after the id. Devices therefore get
  their area by detecting first creation and calling
  `async_update_device(device_id, area_id=...)` with the configured id.
  Creation-only for parent and children alike; user overrides from the device
  page are never overwritten (the current force-reassign
  `_async_assign_device_area` is deleted). Entry-area edits change RF routing
  only, never existing device areas.
- One cover entity per subentry; entity `unique_id` = `subentry_id` — stable
  through remote recalibration and channel edits.
- Deleting the final cover subentry is allowed; the entry then exposes only
  the remote device.
- **Disabled member entities**: HA does not instantiate disabled registry
  entities, so a disabled leaf has no live model. It is excluded from
  aggregate state and from `set_position` delegation (documented; the
  aggregate's open/close/stop full-channel frame still physically reaches its
  motor). Aggregate availability and state derive from instantiated,
  available members only.

### Leaf cover behavior

Today's `ZemismartCover` semantics: travel-time position model, timed STOP
positioning, RX heard-event listener for its channels, restore on startup.
Restore extra data now records `role` and `channels`; restored position is
discarded when either differs from the current derivation.

### Aggregate cover behavior

State derives from members; RF behavior matches today's group entries.

State (over **instantiated, available** leaf members only; if none, the
aggregate is unavailable):

- `is_closed` = true iff every such member reports closed; false if any is
  open; unknown if none is open and any position is unknown.
- Position = **unweighted average** of members with known positions (HA
  cover-group convention; channel-count weighting considered and rejected).
- `is_opening` / `is_closing` = any member opening/closing, HA's standard
  precedence (opening reported first) when both are true.

Commands:

- **open/close/stop**: one RF frame addressed to the aggregate's full channel
  set through the existing hub path. On the started ack, the coordinator
  starts (or freezes, for STOP) each member's motion model at the acked
  timestamp. Members do not transmit in this path.
- **set_position**: delegates concurrently to each instantiated leaf member's
  positioning logic; members go through the hub queue as independent commands
  (timed frames do not coalesce — accepted RF cost). Delegation results are
  **typed internally** (ok / superseded / failed): `superseded` counts as
  success-equivalent (a newer command took that member over — same semantics
  as a directly-commanded cover); failures aggregate into one
  `HomeAssistantError` naming the failed members after all delegations
  settle. Disabled/unavailable members are skipped and named in the error
  only if nothing else moved.
- **STOP preemption contract**: aggregate STOP does not queue behind in-flight
  delegations — it cancels the aggregate's pending member delegations
  (cancelled ones settle as superseded), then sends the single full-channel
  STOP frame, which also freezes member models via the coordinator. The
  aggregate does not hold a lock that would make STOP wait on fan-out
  completion.

### RX press ownership (laminar leaf-local rules — rev 4.1)

> **Rev 4.1 (implementation-validated):** with the laminar channel family
> enforced at the config layer, coordinator-side owner arbitration is
> unnecessary — the rules below collapse to leaf-local decisions that are
> behaviorally equivalent (each leaf is either fully covered by a press,
> partially intersected, or disjoint; aggregates never model presses and
> re-derive from members). The implementation uses the leaf-local rules;
> both Gemini 3.1 Pro and Grok 4.5 flagged the deviation in the panel
> review, and it is accepted as the simpler equivalent design.

#### Original (superseded) coordinator-arbitrated description

The hub still dispatches every intersecting RX listener (unchanged), and
aggregates still register listeners with their full channel sets and own
takeover state for their commands, like today's group entries. What changes is
who applies a physical press to models: the **coordinator selects exactly one
owner per heard press** — the innermost configured cover whose channel set
contains the pressed set (unique in a laminar family; the equal-set cover when
one exists). The owner applies its model/ownership mechanics; the coordinator
then fans derived start/freeze/invalidations out to affected member models
**once**, replacing today's per-entity yield heuristics (which only handled
single-channel members and would double-apply under nesting). Presses whose
set no configured cover contains fall back to per-listener intersection
invalidation, as today. The state-sync consumer itself is unchanged.

### Entry coordinator

Per-entry object built at setup: covers indexed by subentry id, the derived
role/membership map, press-ownership arbitration (above), and two
notification paths:

- **Forward** (aggregate command → members): start/freeze member models on
  group command acks.
- **Reverse** (member change → aggregates): every member-model mutation —
  service commands, RX-driven changes, motion completion, restore,
  invalidation, availability — marks containing aggregates dirty. Dirty
  aggregates recompute and write state once per event-loop iteration (a
  zero-delay scheduled callback coalesces multi-member events into one state
  write).

### Reload and lifecycle

- The integration registers its entry update listener via
  `entry.async_on_unload(entry.add_update_listener(...))` (no accumulation
  across reloads). It is the **sole reload scheduler**. Consequently every
  flow terminator uses the non-reloading helpers: subentry flows use
  `async_update_and_abort`, and the entry reconfigure flow uses
  `async_update_and_abort`-style update **without** the reload helper
  (`async_update_reload_and_abort` would schedule a second, uncoalesced
  reload; HA's subentry helper even raises when an update listener exists).
  Native subentry add/delete also funnel through the listener. Requirement:
  exactly one reload per mutation.
- **Pending-command drain (entry-scoped)**: queued hub commands carry an
  **owner token** (entry_id). Unloading an entry performs an awaited
  selective drain: its queued-unpublished commands are resolved
  `superseded` and skipped at publish, regardless of whether callers still
  await them (a pending service-call future is NOT proof the command should
  transmit — HA awaits service coroutines independently).
- **Relearn additionally disarms bridge-held state**: a timed command's STOP
  lives on the bridge once published; cancelling queue state cannot retract
  it. Relearn (and only relearn — settings edits keep the same identity)
  must, before its reload completes, issue and **await** the existing
  acknowledged bridge-disarm for every live timed command of this remote,
  bounded by the outstanding STOP deadline: proceed on ack, or once the last
  old-identity STOP window has expired. No frame with a pre-relearn identity
  may transmit after the reload completes.

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
   when the entered channels make the cover a leaf against covers collected
   so far), then a menu: **Add another cover** | **Finish**. Each iteration
   validates the channel-set rules against covers already collected.
6. Finish runs the final whole-list validation (uniqueness + laminarity),
   then creates the entry with all cover subentries
   (`async_create_entry(..., subentries=[...])`, supported in HA 2026.5.4).
   Creation aborts `already_configured` on remote unique_id collision.

At least one cover is required before Finish is offered.

### Subentry flows (type `cover`)

The config flow class **must register the subentry flow**:
`async_get_supported_subentry_types` returning
`{"cover": CoverSubentryFlow}` — HA raises `UnknownHandler` otherwise.

- **Add**: same cover form, validated against existing subentries.
- **Reconfigure**: same form prefilled; rename, change channels, adjust travel
  times; hidden travel keys carried forward; role validated as currently
  derived. Ends with `async_update_and_abort`.
- **Delete**: native HA subentry delete; subentry-bound entity and device are
  removed by HA; the update listener reloads the entry.

### Entry reconfigure flow (the remote)

Menu:

- **Relearn**: sniff path re-runs; on success replaces prefix/remote_id/bases
  after an explicit collision scan of other entries and the awaited
  bridge-disarm drain (above). Subentries untouched; entities survive.
- **Edit settings**: name, area (routing-only effect), repeats, coalesce
  window, and manual calibration fields (bases + optional trailer).

No options flow.

## Deployment runbook (manual, no migration code)

Performed by Claude on the live HA at rollout, per remote:

1. **Full export first**: every old entry's complete config (data + options
   merged — channels, travel times, repeats, coalesce, calibration, area);
   entity registry records **including customizations** (entity_id, custom
   name, icon, labels, categories, entity-level area override,
   hidden/disabled state); device ids. Snapshot automations/scripts/scenes
   referencing the covers, noting `device_id`-targeted ones.
2. **Cross-area check**: if one remote's old entries span areas (disjoint
   sets in different rooms — valid today, unrepresentable per-remote), STOP
   and consult the user before onboarding that remote (routing would change).
3. Reconcile per-cover repeats/coalesce to the remote level: **max** across
   the old siblings (reliability-safe).
4. Delete the old per-blind entries for that remote.
5. Onboard the remote via the new wizard using the exported channels, travel
   times, and settings; name covers identically.
6. **Registry-level repair** (names are not identity; new unique_ids will NOT
   revive old registry metadata): set each new entity's entity_id to the
   recorded value (temporary ids two-phase when swaps collide), then reapply
   exported customizations — custom name, icon, labels, area override,
   hidden/disabled flags.
7. Rewrite `device_id`-targeted automations/scripts/scenes to the new device
   ids (or convert to entity_id targeting).
8. Verify: entity_ids and customizations match the export, each automation's
   targets resolve, spot-check one motion per remote.

## Testing

- **Flow tests**: wizard end-to-end (learn → settings → multi-cover loop →
  entry + subentries), duplicate-remote abort at creation and relearn,
  channel-collision and partial-overlap rejection in all three validation
  sites plus the final pre-create whole-list check, current-role travel-time
  requirement (including reconfigure role flips and hidden-key preservation),
  subentry flow registration, subentry delete → update-listener reload,
  exactly-one-reload-per-mutation (reconfigure must not double-reload).
- **Model tests**: BlindConfig derivation with explicit role, laminar
  validation, leaves-only membership, unclaimed channels, demotion paths,
  restore role/channel discriminator.
- **Cover tests**: aggregate state math (availability rules, closed/open/
  unknown propagation, unweighted average, opening-precedence), batched
  single-write recomputation, group open/close single frame + member model
  start/freeze, concurrent set_position fan-out with typed results
  (superseded-as-success, failure aggregation, disabled-member skip), STOP
  preemption of in-flight fan-out, press-ownership arbitration (innermost
  owner, single fan-out application, multi-channel leaves, nested
  aggregates), leaf behavior unchanged.
- **Lifecycle tests**: config_subentry_id binding, parent-device-first
  creation, area set by `async_update_device` on first creation only and
  user override preserved, entry-scoped queued-command drain on unload,
  relearn awaited bridge-disarm (no old-identity frame after reload).
- **Guardrail**: `test_state_sync.py` passes without modification (baseline
  present after rebase onto `main@d03ce0f`).

## Out of scope

- Any change to the state-sync consumer beyond keeping its tests green (the
  hub/RX listener dispatch is unchanged; press-ownership arbitration lives in
  the new coordinator layer above it).
- Migration/repair code for old entries.
