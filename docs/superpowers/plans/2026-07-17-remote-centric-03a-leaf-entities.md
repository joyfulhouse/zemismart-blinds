# Plan 03a — Leaf Entities on Subentries + Legacy Gate

> Implementer: Codex GPT-5.6-sol xhigh. Controller commits.
> Files in scope: `custom_components/zemismart_blinds/{models.py,__init__.py,cover.py,config_flow.py,strings.json,translations/en.json}`,
> `tests/{test_models.py,test_init.py,test_cover.py,test_config_flow.py}`.
> `tests/test_state_sync.py` byte-for-byte unchanged. Baseline: 634 green at `d907737`.

**Goal:** Remote entries create one **leaf** cover entity per leaf subentry
(aggregate subentries create nothing yet — Plan 03b), with the spec's device
topology. Legacy entries stop loading (`ConfigEntryError`, data kept). The
dual-format shim is deleted.

## Task 1: `BlindConfig` gains `role` + optional travel + `derive` (deferred Plan 01 Task 5)

Apply exactly the change specified in
`docs/superpowers/plans/2026-07-17-remote-centric-01-data-model.md` old Task 5
(the section marked "moved to Plan 03"), including its 5 tests — the plan text
there contains the full field list, role-aware `__post_init__` validation,
`is_aggregate` property, and `derive(remote, cover, role)` classmethod. This
time `cover.py` migrates in the same plan (Tasks 3–4), so package strict mypy
is gated at the END of Task 4, not per-task. `uv run pytest tests/test_models.py -q`
must pass after this task.

## Task 2: Legacy gate + shim removal (`__init__.py`, `models.py`)

- In `async_setup_entry`, replace the `legacy_config` branch logic:

```python
    if CONF_CHANNELS in entry.data:
        # Rev 4: legacy per-blind entries are kept only as migration
        # reference data — they never load. See the deployment runbook.
        msg = (
            "This entry uses the retired per-blind format. Add its remote "
            "through the integration's new wizard, then delete this entry."
        )
        raise ConfigEntryError(msg)
```

  (import `ConfigEntryError` from `homeassistant.exceptions` at the top-level
  imports — module-level, matching the existing import style.) Then always
  build `RemoteRuntime`; delete the `EntryRuntime` construction, the
  `legacy_config` variable, `_entry_config`, and `_async_assign_device_area`
  entirely.
- Type alias becomes `type ZemismartConfigEntry = ConfigEntry[RemoteRuntime]`;
  drop `EntryRuntime`/`BlindConfig` from `__init__.py` imports if unused.
- `models.py`: delete the `EntryRuntime` dataclass (nothing may reference it
  after this plan).
- `config_flow.py`: delete `effective_values` (its only consumer was
  `_entry_config`).
- Tests (`tests/test_init.py`): replace the legacy setup test(s) with one
  asserting a legacy-shaped entry raises `ConfigEntryError` at setup (HA marks
  the entry `SETUP_ERROR`; assert via
  `entry.state is config_entries.ConfigEntryState.SETUP_ERROR` after
  `await hass.config_entries.async_setup(entry.entry_id)`), that its data is
  untouched, and that no cover entities exist. Keep/adapt the shared-runtime
  lifecycle tests to use remote-format entries (build them with
  `RemoteConfig(...).as_dict()` + `subentries_data` where they need entities —
  see Task 3's test helper). Any test importing `EntryRuntime` or
  `effective_values` must be migrated or deleted.

## Task 3: Per-subentry leaf entities + device topology

### `__init__.py` — parent device before platform forward

Inside the lifecycle-lock block, immediately BEFORE
`async_forward_entry_setups`, create the remote's parent device:

```python
                from homeassistant.helpers import device_registry as dr

                remote = cast("RemoteRuntime", entry.runtime_data).remote
                registry = dr.async_get(hass)
                device = registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    identifiers={(DOMAIN, entry.entry_id)},
                    manufacturer="Zemismart",
                    model="RF433 remote",
                    name=remote.name,
                )
                if device.area_id is None:
                    # Configured area applies at creation only; a user's
                    # later device-page override must survive reloads.
                    registry.async_update_device(device.id, area_id=remote.area_id)
```

(Place the `device_registry` import at module top with the other HA imports,
not inline, if the existing style allows — the file currently imports HA
modules lazily inside functions; follow the file's existing lazy-import style.)

Note `async_get_or_create` is called on every setup; it returns the existing
device unchanged on reload, and the `area_id is None` guard makes area
assignment first-creation-only. This satisfies the spec's
"area via async_update_device at creation only, user override preserved".

### `cover.py` — subentry-driven setup

Replace `async_setup_entry` with:

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[RemoteRuntime],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one leaf cover entity per leaf subentry of this remote."""
    from homeassistant.helpers import device_registry as dr

    runtime = entry.runtime_data
    covers: dict[str, CoverConfig] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != "cover":
            continue
        try:
            covers[subentry.subentry_id] = CoverConfig.from_subentry(subentry.data)
        except TypeError, ValueError:
            _LOGGER.warning(
                "Skipping unreadable cover subentry %s of %s",
                subentry.subentry_id,
                entry.title,
            )
    registry = dr.async_get(hass)
    for subentry_id, cover in covers.items():
        role = derive_role(cover, covers.values())
        if role is not Role.LEAF:
            # Aggregate covers gain entities in the next phase.
            continue
        config = BlindConfig.derive(runtime.remote, cover, role)
        entity = ZemismartCover(subentry_id, config, runtime.hub)
        async_add_entities([entity], config_subentry_id=subentry_id)
        device = registry.async_get_device(identifiers={(DOMAIN, subentry_id)})
        if device is not None and device.area_id is None:
            registry.async_update_device(device.id, area_id=runtime.remote.area_id)
```

(`_LOGGER = logging.getLogger(__name__)` — add if absent. Imports:
`CoverConfig`, `Role`, `derive_role`, `RemoteRuntime` from `.models`.)

Note: one `async_add_entities` call per entity keeps the subentry binding
correct (each entity carries its own `config_subentry_id`). The
device-area assignment must run AFTER the entity is added (the device is
created during `async_add_entities`); `async_add_entities` awaits entity
addition, so querying the registry immediately after is safe.

### `ZemismartCover.__init__` — decoupled from EntryRuntime

Change the constructor signature to
`def __init__(self, subentry_id: str, config: BlindConfig, hub: ZemismartHub) -> None:`
storing `self._config = config`, `self._hub = hub`,
`self._entry_id = subentry_id` (keep the attribute NAME `_entry_id` to avoid
touching every reference — it now holds the subentry id; rename in a follow-up
only if trivial), `self._attr_unique_id = subentry_id`. Update `device_info`:

```python
    @property
    def device_info(self) -> DeviceInfo:
        """Represent this cover as a child device of its remote."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=self._config.name,
            manufacturer="Zemismart",
            model="433 MHz blind group" if self._config.is_group else "433 MHz blind",
            via_device=(DOMAIN, self._via_entry_id),
        )
```

which requires passing the parent entry id: extend the constructor to
`(self, subentry_id: str, via_entry_id: str, config: BlindConfig, hub: ZemismartHub)`
and store `self._via_entry_id = via_entry_id`; the platform setup passes
`entry.entry_id`. Everything else in `ZemismartCover` stays byte-identical —
its behavior (motion model, RX listener, restore, members/overlaps) is
untouched in this phase. (`_member_covers`/`_reconcile_overlaps` still use
`self._config.is_group`; that is acceptable interim behavior for multi-channel
LEAF covers until Plan 03b replaces member logic with the coordinator.)

### Restore discriminator

In `_async_restore_state`, the existing remote/channels attribute check
already discards mismatched restores. Add `role` to `extra_state_attributes`
(`"role": self._config.role.value`) and extend the discard condition: if the
restored state's `role` attribute exists and differs from the current
`self._config.role.value`, discard (return). Missing `role` attribute (old
persisted states) is treated as leaf.

### Tests

- `tests/test_cover.py`: adapt the construction fixture/helpers — wherever a
  cover entity is built from an entry/EntryRuntime, build it as
  `ZemismartCover(subentry_id, entry_id, BlindConfig(...), hub)` (the file
  largely constructs `BlindConfig` already; keep behavior assertions
  unchanged). The goal is mechanical adaptation, NOT behavioral edits.
- `tests/test_init.py`: new test — a remote entry with subentries
  (`subentries_data=[...]` on the ConfigEntry constructor, or created via the
  wizard helper pattern from test_config_flow) sets up: leaf subentries get
  cover entities bound to their subentry (assert entity registry entries carry
  `config_subentry_id`), the aggregate subentry has NO entity yet, the parent
  remote device exists with `via_device` children, parent+child devices got
  the remote's `area_id`, and a second reload does not overwrite a manually
  changed device area (update the device's area in the registry, reload the
  entry, assert preserved).

## Task 4: Gates

`uv run pytest -q` (all green), `uv run mypy --strict custom_components/zemismart_blinds/`
(now includes the BlindConfig optional-travel change WITH its migrated
consumer — must be clean), `uv run ruff check custom_components/zemismart_blinds/ tests/`,
strings identical check, `git diff --stat main -- tests/test_state_sync.py` empty.

Note on mypy: `cover.py` leaf arithmetic (`self._config.travel_up` etc.) now
sees `float | None`. The leaf entity is only constructed with
`Role.LEAF` configs whose validation guarantees floats, but mypy cannot know
that. Resolve WITHOUT ignores: give `ZemismartCover` two private float fields
assigned in `__init__`:

```python
        if config.travel_up is None or config.travel_down is None:
            msg = "leaf cover entities require travel calibration"
            raise ValueError(msg)
        self._travel_up: float = config.travel_up
        self._travel_down: float = config.travel_down
```

and replace every `self._config.travel_up`/`travel_down` read in the class
with `self._travel_up`/`self._travel_down` (mechanical; ~6 sites).
