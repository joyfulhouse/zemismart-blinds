# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-07-17

### Changed

- `DEFAULT_REPEATS` lowered from 5 to 2. Companion firmware v1.2.0 paces dispatch by frame
  airtime, so every scheduler-level repeat now actually transmits (16 OEM-grade on-air
  repetitions across two time-diverse windows); higher values only occupy air and delay queued
  fail-safe STOPs. Existing entries keep their configured value.

[0.2.1]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.2.1

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
