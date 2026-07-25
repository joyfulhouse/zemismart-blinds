# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.2] - 2026-07-25

### Fixed

- **The integration no longer loses proof that its own command reached the air.** A
  capture held while its command was still
  pending is now resolved against *that command* rather than re-derived from the confirmed
  emission window. The window's bounds come from the bridge's `started` status, which is
  published separately from the RF it describes and can arrive *after* a peer bridge has
  already reported hearing the frame — measured at **1.117 s** of skew during a concurrent
  seven-cover burst, against 0.75 s of window slack. The window therefore rejected our own
  frame, costing the command its emission proof — which clears the unverified anchor and can
  drive a cover to `unknown`. Only visible under concurrent multi-remote bursts; a burst
  regression test now covers that workload.

  **Scope, stated precisely:** this does NOT prevent a phantom physical-press takeover in
  that regime. `_dispatch_press` already drops a press predating a recorded commanded
  start, and the hub always records one before resolving the future that unblocks
  confirmation — verified by running the new tests against the pre-change code, where the
  no-press assertions pass and only the emission-proof assertions fail. The originally
  reported mid-travel model freeze is therefore **not** closed by this release.

  Ownership of a held capture is bounded to a plausible status lag
  (`_LEDGER_HELD_TRUST_SECONDS`, 5 s). A timed move registers its own `stop_raw`, so a
  person pressing STOP produces a capture identical to one of our own frames; without that
  bound such a press would have been silently absorbed — bypassing takeover handling
  entirely — for as long as the bridge took to confirm, up to the 30 s started-status
  timeout.

- **The mid-travel model freeze on covers whose remote also runs timed partial moves is
  fixed too.** Same root cause, different sub-case. The held-capture fix above only reaches
  frames still *pending* when their echo arrives — true of an action frame, emitted within
  ~250 ms, but not of a `stop_raw` frame, emitted `stop_after_ms` later when the command
  has long since confirmed. That echo arrives through the ordinary `match()` window path,
  where the same late anchor has shifted *every* window in the entry. Confirmed windows are
  now **asymmetric**: the lower edge absorbs `_LEDGER_ANCHOR_LAG_SECONDS` of anchor lag
  while the upper edge keeps its original tight slack, because `started_at` is biased late
  and never early. Our own STOP echo is no longer read as a person stopping the blind by
  hand — which froze HA's travel model partway while the motor ran on to its limit, leaving
  HA reporting a partial position for a cover that had physically closed.

- The remote's device is now identified by the durable remote key (`prefix:remote_id`, the
  same identity as the entry's unique id) instead of the config **entry id**. Existing
  devices are re-identified **in place** on the next setup, so `device_id`, area overrides,
  name, and all attached entities are preserved — automations targeting the remote device
  keep working. Previously, deleting and re-adding a remote minted a new entry id and
  therefore a brand-new device, silently breaking every `device_id` target while
  `entity_id` targets returned intact.

### Notes

- **Correction to the 0.3.1 release notes (retroactive).** 0.3.1 moved covers into the
  remote's device and pruned the pre-0.3.1 per-cover child devices. Those notes promised
  only that friendly names and entity ids stay byte-stable; they did not say that the
  per-cover **child devices were removed**, so any automation or script targeting a cover
  by `device_id` stopped matching from 0.3.1 onward and silently did nothing. Retarget
  those actions **by `entity_id`** (cover entity ids were preserved throughout), or target
  the remote's device. 0.5.0 did **not** cause this: its migration preserves cover
  identity, and `unique_id` values that look freshly minted are ULIDs created when the
  covers were first onboarded.

[0.5.2]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.5.2

## [0.5.1] - 2026-07-24

### Changed

- Documentation only: README, INSTALL, and this changelog now describe the remote-centric
  model (remote device owning cover entities, reconfigure-menu cover management) and
  cross-bridge air arbitration; changelog backfilled for 0.3.0–0.5.0.

[0.5.1]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.5.1

## [0.5.0] - 2026-07-24

Covers move into the remote entry's data; config subentries are retired.

### Changed

- **BREAKING (storage rev 2, migrates automatically):** each remote entry now stores its
  covers directly in entry data with stable per-cover identities; the per-cover config
  subentries are removed. The staged, crash-recoverable migration preserves every cover
  entity's `unique_id`, `entity_id`, area, and customizations byte-identically (old
  subentry ids become the permanent cover ids), and every setup runs an idempotent repair
  sweep against interrupted upgrades. Downgrading to 0.4.x after migration requires
  restoring config-entry and registry stores from a stopped-core backup.
- The integrations page now renders one device row per remote entry — previously the single
  remote device was repeated under every cover subentry plus a "Devices that don't belong
  to a sub-entry" bucket.
- Per-cover management moved into the entry's **Reconfigure** menu (add / edit / remove
  cover, keyed by stable cover identity; removal deletes exactly that cover's entity, and
  removing the last cover or a leaf an aggregate depends on is refused).
- Legacy pre-0.3.0 per-blind reference entries pass through migration byte-for-byte and
  keep their existing setup refusal.

[0.5.0]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.5.0

## [0.4.0] - 2026-07-21

Cross-bridge RF air arbitration: multi-bridge installs stop talking over each other.

### Added

- **Cross-bridge air arbitration (enforcing by default with 2+ online bridges):** a
  process-local calendar schedules normal commands onto the shared 433 MHz channel,
  anchored at each command's correlated actual RF start and reserving known future
  fail-safe STOP windows. An explicit STOP is never delayed; fewer than two online bridges
  disables arbitration; every failure path publishes (attributed fail-open counters, hard
  130 s hold ceiling). Whole-house scenes start their blinds ~1.9 s apart instead of
  colliding on air.
- Installation-wide YAML escape `zemismart_blinds: air_arbitration_mode: shadow` — computes
  and records what enforcement would have done without delaying anything (measurement mode
  and rollback path).
- Config-entry diagnostics expose the full arbitration counter snapshot.

### Changed

- Default RF repeats raised from 2 to 3: repeats within one train share a single collision
  window, so a third time-diverse ~609 ms window is the per-command reliability lever.
  Existing entries keep their stored value.

[0.4.0]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.4.0

## [0.3.1] - 2026-07-18

### Changed

- Covers are entities **inside the remote's device** (like inverter controls), no longer
  child devices via `via_device`; empty pre-0.3.1 per-cover child devices are pruned after
  entities re-home. Deployed friendly names and entity ids stay byte-stable.

### Fixed

- Relearn on a sole loaded entry no longer cancels pending bridge disarm retries during
  the reload (which could leave a stale identity's fail-safe STOP armed on the bridge).

[0.3.1]: https://github.com/joyfulhouse/zemismart-blinds/pull/6

## [0.3.0] - 2026-07-17

Remote-centric model: one config entry per physical remote.

### Changed

- **BREAKING:** the integration is organized around remotes — one entry per remote identity
  (unique id `prefix:remote_id`) owning all of that remote's covers, replacing one entry
  per blind. Legacy per-blind entries stop loading and are kept as migration reference
  data.
- Onboarding is one wizard: learn/manual/virtual identity → remote settings → add covers.

### Added

- **Aggregate covers:** a channel-superset cover (e.g. ALL over `1,2,3`) derives its state
  from its member covers, transmits a single group frame, and fans SET_POSITION out to
  members with STOP preemption; laminar channel-set validation keeps leaves and groups
  consistent.
- Coordinator press-ownership arbitration so a physical press updates the innermost
  matching cover.

[0.3.0]: https://github.com/joyfulhouse/zemismart-blinds/pull/3

## [0.2.0] - 2026-07-17

Live state sync: physical remote presses now move the matching covers.

### Added

- **Live state sync from physical remotes** (with companion firmware v1.1.0): bridges
  idle-listen and publish heard presses on `rf433/<bridge_id>/rx`; correlated UP/DOWN/STOP
  presses update the matching cover motion models — observation only, RX never transmits.
  Includes suppression of the integration's own command echoes heard by other bridges,
  cross-bridge replay/dedup, heard-STOP freeze semantics, and takeover/disarm interplay
  validated across a 12-round adversarial review.
- Guided sniff-based onboarding and reconfigure ("Learn from remote") wizard using bounded
  bucket-sniff windows on any online bridge.

### Fixed

- **OEM truncated-trailer captures decode**: some remotes transmit 64 payload bits plus a single
  trailer 0-read instead of the nominal `[1, 0]`; receive-side decoding (`decode_rx_capture`) now
  tolerates it, while transport encode/decode stays strict. The regression fixtures retain real
  field-captured bucket timing jitter and trailer structure but are re-keyed to a synthetic
  identity rather than storing verbatim captures. Presses with this structure were previously
  dropped silently.
- Release-hardening rounds 11–16 on the cover/scheduler core (displaced-STOP freeze
  regression, timeout snapshot coverage, restore ordering, echo anchoring, teardown race).

[0.2.0]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.2.0

## [0.1.0] - 2026-07-14

First public release.

### Added

- One Home Assistant `cover` entity per blind or arbitrary same-remote channel group,
  with OPEN, CLOSE, STOP, and SET_POSITION (assumed-state travel-time position).
- Config flow with per-remote calibration: one labeled captured Portisch B0 reference or one
  direct 16-bit action base derives all three action bases; optional OEM TRAILER base.
- Reuse of calibrated remote identities already stored by another entry.
- Channels 1–16, addressed individually or as exact grouped RF frames (a group is one
  transmission, not several colliding commands).
- Retained MQTT discovery of bridge availability, area, and default flag; area-aware bridge
  selection with default/any-online fallback and a `degraded_bridge` indicator.
- One globally serialized command queue with correlated `accepted`/`rejected`/`started`
  acknowledgements and intelligent same-remote group coalescing (on by default).
- Per-target RF repetition with absolute bridge-side STOP deadlines for partial movement.
- `zemismart_blinds.send_raw` debug service and `zemismart_blinds.new_virtual_remote`, which
  returns a complete synthesized calibration usable directly in the manual add flow.
- Works with any MQTT broker configured in Home Assistant's MQTT integration (the Mosquitto
  add-on works out of the box).
- Companion ESPHome firmware:
  [joyfulhouse/esphome-rf433-mqtt-bridge](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge).

[0.1.0]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.1.0
