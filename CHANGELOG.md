# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- **OEM truncated-trailer captures decode**: some remotes (live-captured office `5cad7c`)
  transmit 64 payload bits plus a single trailer 0-read instead of the nominal `[1, 0]`;
  receive-side decoding (`decode_rx_capture`) now tolerates it, while transport
  encode/decode stays strict. Presses from such remotes were previously dropped silently.
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
