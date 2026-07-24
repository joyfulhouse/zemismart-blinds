# Covers Move Into Entry Data — Retiring Cover Subentries

Date: 2026-07-24

Status: Proposed (owner-directed); Codex-attested (see Attestation section); pending owner
review

Release target: `v0.5.0`

## The problem, from the production UI

On the integrations page, every remote entry renders like this (Office Remote, 7 covers):

```text
Office Remote                                (config entry)
├─ Devices that don't belong to a sub-entry
│  └─ Office Remote — RF433 remote · 7 entities
├─ Backyard Window (Cover)
│  └─ Office Remote — RF433 remote · 7 entities      ← same device again
├─ Left Slider (Cover)
│  └─ Office Remote — RF433 remote · 7 entities      ← and again
└─ … × 7 subentries, each repeating the one device
```

The same remote device appears once per cover subentry plus once in the "don't belong"
bucket. The owner's expected rendering is simply:

```text
Office Remote  →  Office Remote (device)  →  click: its covers
```

## Root cause

Three deliberate choices interact badly:

1. Covers are **config subentries** of the remote entry (v0.3.0).
2. Since v0.3.1 (owner-directed, EG4 pattern) every cover entity lives **inside the one
   remote device** — there are no per-cover devices.
3. `cover.py::async_setup_entry` adds each entity with
   `async_add_entities([entity], config_subentry_id=subentry_id)`.

HA's integrations page groups a config entry's devices **by subentry association**. Because
each cover entity carries a `config_subentry_id`, the shared remote device becomes associated
with every subentry (device registry `config_entries_subentries`), and it is also associated
with the entry itself (association `None`, from `_ensure_remote_device`). The page therefore
prints the device once per association: once under "Devices that don't belong to a
sub-entry" and once under every cover subentry.

HA's subentry rendering assumes per-subentry *devices* (its flagship examples give each
subentry its own device). With the flattened single-device topology — which the owner chose
and keeps — subentries contribute **nothing but this clutter**: their only remaining value is
the per-cover add/edit UI, which an ordinary reconfigure menu provides equally well.

## Decision

**Retire cover subentries entirely.** A remote's covers become part of the remote entry's
own data. One entry = one remote = one device; covers are rows in `entry.data["covers"]`
and entities of the remote device, exactly as they already are at the registry level.

Considered and rejected:

- **Keep subentries, drop only the `config_subentry_id` attachment.** Removes the repeated
  device rows but keeps the per-cover subentry cards and the "Devices that don't belong to a
  sub-entry" header — the page stays noisy — and orphans HA's subentry-scoped cleanup.
- **Per-cover child devices under the remote (revert v0.3.1).** Makes HA's subentry
  rendering look intentional, but reintroduces the topology the owner explicitly rejected,
  re-churns every device/entity association, and adds 16 devices nobody wants.

## Data model

### Entry schema rev 2

`entry.data` (VERSION 1 → 2 via `async_migrate_entry`):

```jsonc
{
  // remote fields, unchanged:
  "name": "Office Remote", "prefix": "0x…", "remote_id": "0x…",
  "base_up": "0x…", "base_down": "0x…", "base_stop": "0x…",
  "area_id": "office", "repeats": 3, "coalesce_window_ms": 150,
  // NEW — replaces subentries:
  "covers": [
    {
      "cover_id": "01JZX…",      // opaque, stable; see identity note
      "name": "Backyard Window",
      "channels": "4",
      "travel_up": 15.0, "travel_down": 15.0
      // …plus any hidden calibration keys the subentry stored, carried verbatim
    }
  ]
}
```

### Identity — the one thing that must not move

Today each cover entity's `unique_id` **is its subentry_id** (cover.py:204, 1123), and the
coordinator keys leaves/aggregates/travel state by it. Therefore:

- `cover_id` is an opaque stable identifier with the same role subentry_id has today.
- **Migration sets `cover_id` := the cover's old `subentry_id`**, so every entity registry
  row keeps its exact `unique_id`, `entity_id`, area override, and customizations. Zero rows
  are recreated.
- New covers get a freshly generated ULID (same alphabet, collision-free).
- `channel_key` remains a validation concept (duplicate-channel detection), never identity:
  channels are editable, identities are not.

Internally, `coordinator.py` and `cover.py` rename their `subentry_id` keying to `cover_id`.
No behavioral change: the values are identical for every migrated cover.

## Flows

### Add wizard (new remote) — unchanged shape, new terminal write

`user → learn (name/area/bridge → sniff → confirm) → remote_settings → cover → cover_menu →
finish` stays exactly as it is — it is already remote-first. `async_step_finish` now writes
the covers into `data["covers"]` (each with a new `cover_id`) and passes **no** `subentries`.
The existing whole-list channel-conflict backstop stays.

### Per-cover management — reconfigure menu replaces the subentry flows

`CoverSubentryFlow` and `async_get_supported_subentry_types` are deleted. The entry's
reconfigure menu grows from `[reconfigure_learn, reconfigure_edit]` to:

```text
reconfigure          → menu: relearn identity | edit remote settings |
                              add cover | edit cover | remove cover
```

- **add cover** — the existing `_cover_schema` + `_validate_cover_input` against current
  siblings; appends with a new `cover_id`.
- **edit cover** — a picker, then the same form pre-filled via `_cover_display_values`,
  preserving hidden travel calibration exactly as the subentry reconfigure step does today;
  `cover_id` never changes. The edit **merges into** the stored cover row rather than
  reconstructing it, so unknown/hidden keys survive every edit, not only migration.
- **remove cover** — a picker, then a confirm step. Refused when: it is the entry's last
  cover, or it is a leaf that an aggregate's channel set depends on (same laminar family
  rules the validators already enforce). On confirm, the flow must **explicitly delete the
  entity registry row** — look up `er.async_get_entity_id("cover", DOMAIN, cover_id)`,
  verify the row's `config_entry_id` is this entry, and `er.async_remove()` it before the
  data update. Without subentry cleanup, nothing else deletes the row: HA does not
  reconcile registry rows against entities a platform stops creating. Disabled, currently
  unavailable, and never-instantiated covers all have (or may have) rows and must be
  handled; a missing row is not an error.
- **Pickers are keyed by `cover_id`, never by name** — duplicate cover names are legal
  today and must stay unambiguous in the pickers.
- Every terminal step uses the non-reloading update helper + the existing single
  `_async_entry_updated` reload scheduler, matching the current one-reload-per-mutation
  contract.

Strings: the two "Device name" labels on cover forms become "Cover name" (a cover has not
been a device since v0.3.1), and `learn_setup`'s "Choose where this blind is located"
becomes remote wording. No entity-visible strings change.

## Migration (v1 → v2) — the load-bearing part

Runs in `async_migrate_entry` (invoked before integration setup, registries loaded). It is
**staged so that a crash at any boundary is recoverable**: config entries persist on a
~1 s delay while registries defer startup saves up to 180 s, so a hard crash can persist
either store without the other. Every phase below is idempotent and derives from durable
state, never from in-memory progress.

**Phase 0 — legacy branch.** If `CONF_CHANNELS` is in `entry.data`, this is a pre-v0.3.0
per-blind entry: bump it to VERSION 2 with its data byte-for-byte untouched — no `covers`
key, no subentry work (it has none). The existing `async_setup_entry` refusal stays the
authoritative user-facing message. Migration must never manufacture an empty covers list
for these entries.

**Phase A — stage the covers list (entry still v1, subentries intact).** If
`"covers"` is not yet in `entry.data`: build it from `entry.subentries` in insertion order
(`cover_id` := subentry_id, `name` := subentry title, all stored data keys verbatim) and
write it with `async_update_entry(data={**data, "covers": covers})`. If a prior run already
staged it, keep the staged list — **cleanup always derives from the staged list, never from
the live subentries**, so a crash mid-cleanup cannot shrink the list on retry.

**Phase B — resumable cleanup, per staged cover.** For each `cover_id` in the staged list:
re-home any entity registry row of this entry whose `config_subentry_id == cover_id` via
`er.async_update_entity(entity_id, config_subentry_id=None)`; then, if the subentry still
exists, `hass.config_entries.async_remove_subentry(entry, cover_id)`. Removal itself clears
device-registry subentry associations before entity cleanup (verified: HA 2026.7.2
`config_entries.py:2677` updates the entry, then `dr.async_clear_config_subentry`, then
`er.async_clear_config_subentry`), so with rows re-homed first nothing is deleted and the
shared remote device keeps its entry-level (`None`) association. No explicit device
registry step exists — subentry removal is the complete mechanism.

**Phase C — commit.** Assert `entry.subentries` is empty (a fail-safe against externally
corrupted or concurrently altered staged data — every normal and specified crash state
satisfies it), then bump the entry to VERSION 2.

**Setup repair (every v2 setup, idempotent).** Because the two stores can persist at
different times around a crash, `async_setup_entry` on a v2 entry additionally sweeps:
any entity row of this entry with a non-`None` `config_subentry_id` is re-homed to `None`,
and any lingering device-registry subentry association of this entry is cleared by
collecting the non-`None` ids from the device's `config_entries_subentries` and calling
`dr.async_clear_config_subentry(entry_id, stale_id)` for each — never
`async_remove_subentry`, because a dirty-v2 entry no longer necessarily holds the
corresponding config subentry. On a healthy install both sweeps are no-ops; after a
worst-case crash they converge the registries without user action.

**Acceptance:** on the production install (10 entries, 16 covers), every entity registry
row's **identity fields** — `entity_id`, `unique_id`, registry UUID, area override, labels,
options, customizations — are byte-identical before and after migration. The rows
themselves necessarily change (`config_subentry_id` becomes `None`; `modified_at` moves):
no cover entity is deleted or recreated during the upgrade. The integrations page shows one
device row per entry and no subentry sections. A `remove cover` on a test cover deletes
exactly that entity's row.

## Everything else that touches subentries

- `cover.py::async_setup_entry`: iterate `runtime.remote.covers` (from entry data);
  `async_add_entities([entity])` with no `config_subentry_id`.
- `coordinator.py`: pure rename (subentry_id → cover_id); role/aggregate/leaf derivation
  unchanged.
- `models.py::RemoteConfig.from_entry`: gains the covers list; `RemoteRuntime` construction
  stops reading `entry.subentries`. **Every `RemoteConfig` constructor and serializer must
  round-trip the covers list (including unknown keys) verbatim** — `as_dict()` is used by
  both remote-edit reconfigure paths (config_flow.py:755, :794), and a remote-settings edit
  must never drop or reshape covers it did not touch.
- `__init__.py::_async_entry_updated` docstring loses its subentry clause; the
  subentry-notification path is gone with the flows.
- `_prune_stale_cover_devices` keeps running (it guards pre-0.3.1 leftovers), unchanged.
- Diagnostics, air arbitration, state sync, MQTT contract: untouched.

## Testing

- **Migration**: build a v1 entry with subentries (incl. hidden calibration keys, an
  aggregate, and a disabled entity row), run migration, assert: covers list order and
  content; every entity row survives with identical identity fields; no subentries remain;
  device association is entry-only. Additional required cases: idempotence (migrating a v2
  entry is a no-op); **failure injection at every phase boundary** (crash after staging,
  crash mid-cleanup with some subentries removed, config-store/registry-store skew) with a
  re-run converging to the same end state; **legacy `CONF_CHANNELS` entries** pass through
  byte-for-byte with no covers key and still refuse setup; **dirty-v2 repair** (a v2 entry
  with lingering `config_subentry_id` rows and device associations converges at setup);
  stale pre-0.3.1 child devices still pruned.
- **Flows**: finish() writes covers into data with fresh cover_ids and no subentries;
  add/edit/remove cover menu paths incl. refusal cases (last cover, aggregate dependency,
  channel conflict); edit merges (hidden calibration and unknown keys survive) and never
  changes cover_id; **remove cover deletes the registry row** for enabled, disabled, and
  never-instantiated covers, and tolerates a missing row; pickers disambiguate duplicate
  names via cover_id; remote-settings reconfigure round-trips covers verbatim; malformed or
  duplicate `cover_id`s in stored data are rejected at load with a clear error.
- **Platform**: entities built from data covers carry unique_id == cover_id; coordinator
  aggregate membership unchanged across the rename.
- **Strings**: every new menu/form/confirm/refusal step has `strings.json` +
  `translations/en.json` coverage; the hassfest CI job stays the validation gate.
- Full gates: `uv run pytest`, `uv run mypy --strict`, `uv run ruff check .`,
  `uv run ruff format --check .` — no suppressions; baseline 731 tests, nothing regresses
  except tests that constructed subentries directly, which convert to data covers.
- **Dev-environment parity (required change from attestation)**: bump the dev dependency
  pin from homeassistant 2026.5.4 to 2026.7.2 (prod's version) as part of implementation,
  so the suite runs against the APIs the migration relies on.

## Rollout

1. **Stop HA core first**, then back up on hass
   (`/config/rollbacks/zemismart-consumer/<ts>/`): the component, `core.config_entries`,
   `core.entity_registry`, `core.device_registry`. The stores save on independent delays
   (1 s vs up to 180 s), so a running-system copy can capture incoherent generations —
   the snapshot must be taken with core stopped.
2. Deploy v0.5.0 while stopped, start core. Migration runs on load — no storage surgery.
3. Verify: registry diff shows zero entity deletions and byte-stable identity fields;
   integrations page renders `entry → single device`; add/edit/remove-cover menu works on
   one test cover; covers still move (one probe command through an audible pair — air
   arbitration stats confirm normal planning).
4. Rollback = stop core, restore the backed-up component **and all three stores** as one
   coherent set (the entry VERSION went 1→2, so v0.4.0 code refuses migrated entries —
   restoring only the component is not a rollback).

## Attestation

Reviewed by Codex gpt-5.6-sol (xhigh), session 019f9449; verdict on the initial draft:
**"SOUND WITH THE FOLLOWING REQUIRED CHANGES"** — all of which this revision incorporates:
explicit registry-row deletion in `remove cover`; staged, crash-recoverable migration with
an idempotent v2 setup repair; the legacy `CONF_CHANNELS` migration branch; deletion of the
redundant explicit device-association step (subentry removal already clears it: HA
`config_entries.py::async_remove_subentry` → entry update → `dr.async_clear_config_subentry`
→ `er.async_clear_config_subentry`); acceptance language narrowed to identity fields;
covers round-tripping through every `RemoteConfig` serializer with cover_id-keyed pickers;
the expanded test matrix; offline rollback snapshots.

**Final verdict (same session, on this revision): "SOUND AS WRITTEN — the migration WILL
preserve every production entity's identity fields byte-identically, and the resulting
integrations page WILL render one device row per entry with no subentry sections."** Its
two LOW hardening notes (setup-repair uses `dr.async_clear_config_subentry` directly, and
Phase C asserts zero remaining subentries) are incorporated above.

API verification: Codex verified all seven attestation items against the installed HA
2026.5.4 source (`er.async_update_entity` supports explicit `config_subentry_id=None`
re-homing; removal cleanup order entry → devices → entities; migration timing legal;
VERSION-2 downgrade refusal; dropping `async_get_supported_subentry_types` safe; the
repeated-device rendering follows from subentry associations). The 2026.5.4-vs-prod
version gap was flagged as a blocker and closed by direct verification against the
**HA 2026.7.2 tag source**: `helpers/entity_registry.py:1900,1909` (same
`config_subentry_id: str | None | UndefinedType = UNDEFINED` on `async_update_entity`) and
`config_entries.py:2677` (same removal order). The dev-pin bump to 2026.7.2 remains a
required implementation-phase change.
