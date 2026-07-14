# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
