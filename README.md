# Zemismart Blinds for Home Assistant

Local RF control of AOK/Zemismart 433.92 MHz roller blinds from Home Assistant — no cloud, no
hub app, just MQTT and a flashed Sonoff RF Bridge.

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)
[![HACS][hacs-shield]][hacs]
[![CI][ci-shield]][ci]
[![Project Maintenance][maintenance-shield]][maintenance]
[![GitHub Sponsors][sponsors-shield]][sponsors]
[![Ko-fi][kofi-shield]][kofi]

## What Does This Integration Do?

Many roller blinds sold under the **Zemismart** brand (and other resellers of **AOK** OEM tubular
motors) are RF-only: a 433.92 MHz remote is the sole way to control them. This integration gives
each blind — or an arbitrary group of blinds on the same remote — a first-class Home Assistant
`cover` entity by generating the motors' native RF protocol from scratch, transmitted through one
or more inexpensive Sonoff RF Bridge R2 units.

- **Open, close, stop, and set position** with assumed-state travel-time position modeling.
- **True group commands**: a group is one RF transmission, not several colliding commands.
- **Calibrate from a single capture**: one labeled button press from your remote derives the
  remote's complete command set ([fully reverse-engineered protocol](PROTOCOL.md)).
- **Virtual remotes**: mint identities that never existed as hardware and pair motors to them.
- **Reliable delivery**: correlated bridge acknowledgements, bridge-side STOP deadlines for
  partial movement, and area-aware multi-bridge failover.

## Architecture

This project is two cooperating parts:

| Repository | Role |
|---|---|
| **zemismart-blinds** (this repo) | Home Assistant integration — owns every remote identity, generates Portisch B0 frames, models position, chooses a bridge |
| **[esphome-rf433-mqtt-bridge][bridge-repo]** | ESPHome firmware — a deliberately dumb MQTT-to-433 MHz beacon with no blind codes and no cover entities |

The two meet at a fixed MQTT topic contract (`rf433/<bridge>/...`) through **whatever MQTT broker
your Home Assistant already uses** — the Mosquitto add-on works out of the box, and any other
broker (standalone Mosquitto, EMQX, a NAS container) works identically.

```
Home Assistant ──(HA MQTT integration)── broker ──(rf433/<bridge>/tx)── Sonoff RF Bridge ──📡── blinds
```

## Prerequisites

| Requirement | Details |
|---|---|
| **Home Assistant** | Version **2026.5** or newer (ships Python 3.14, which this integration's syntax requires), with the MQTT integration configured |
| **MQTT broker** | Any — the Mosquitto add-on is the easiest |
| **RF bridge** | Sonoff RF Bridge R2 flashed with [esphome-rf433-mqtt-bridge][bridge-repo] (Portisch RF firmware required) |
| **Blinds** | AOK OEM 433.92 MHz tubular motors — commonly sold as Zemismart; other AOK resellers are expected to be compatible |

## Installation

See **[INSTALL.md](INSTALL.md)** for the complete guide, including bridge flashing and first-blind
calibration.

**Quick version (HACS):** add this repository as a custom repository in HACS, install
**Zemismart Blinds**, restart Home Assistant, then add the integration from
**Settings → Devices & services**.

[![Open in HACS][hacs-repo-shield]][hacs-repo]

## Configuration

Each run of the add-integration flow creates exactly one device with one cover entity:

1. **Name** the blind or group.
2. **Choose a remote**: reuse an already-calibrated identity, or enter one manually (24-bit prefix
   and 8-bit remote ID as hex).
3. **Calibrate a new remote** with either one captured Portisch B0/B1 reference (labeled UP, DOWN,
   or STOP) or that action's direct 16-bit base — the flow derives all three action bases. The
   optional OEM TRAILER base should be left blank unless captured.
4. **Channels**: one channel (`1`) or an arbitrary group (`1,2,3`). Channels 1–16 are supported.
5. **Travel times** for full up/down travel, and the blind's Home Assistant **area**.

Use **Configure** on an existing entry to edit identity, channels, timing, area, or RF repeats.
The flow accepts hex with or without the `0x` prefix.

### Position behavior

Position is estimated, not measured, and a new entity starts unknown. Full OPEN/CLOSE re-anchors
at 100/0 after one complete configured travel plus a margin. SET_POSITION requires a known
estimate and arms an absolute STOP deadline **on the bridge**, so a partial move stops even if
Home Assistant restarts mid-travel. An acknowledgement timeout makes position unknown instead of
pretending a command moved the motor.

### Multi-bridge behavior

If no bridge in the cover's area is online, the integration falls back to the retained default
bridge, then any online bridge, and exposes `degraded_bridge: true`. One shared worker publishes
one command at a time and waits for the bridge's acknowledgement before the next — with
intelligent coalescing that merges near-simultaneous commands for blinds on the same remote into
a single group frame.

## Services

### `zemismart_blinds.send_raw`

Debug escape hatch: send one complete `AAB0...55` frame through a named bridge with optional
repeats.

### `zemismart_blinds.new_virtual_remote`

Returns a fresh remote identity **with a complete synthesized calibration**:

```yaml
prefix: "0x5c1a2b"
remote_id: "0x3c"
base_up: "0xf4a1"
base_down: "0xbc69"
base_stop: "0xdc89"
```

To pair one:

1. Call the service, then add a manual integration entry using the returned prefix, remote ID, and
   UP base (calibration action UP).
2. Put the motor into its RF pairing mode with its physical program button (confirm the pairing
   jog; exact button timing varies by motor revision, so keep the motor's own instructions).
3. Send OPEN from the new cover while the motor is in pairing mode, exit pairing mode, and verify
   OPEN, CLOSE, and STOP. Keep the original remote paired until validation is complete.

## Hardware

- **Bridge:** Sonoff RF Bridge R2 — see [esphome-rf433-mqtt-bridge][bridge-repo] for flashing.
- **Motors:** AOK OEM tubular roller-shade motors (Zemismart-branded and others).
- **3D-printed tube adapters:** printable adapters for fitting these motors to other roller
  tubes are maintained in [joyfulhouse/ZemismartAdapters][adapters-repo].

## Automation Example

```yaml
automation:
  - alias: "Close blinds at sunset"
    trigger:
      - platform: sun
        event: sunset
    action:
      - service: cover.close_cover
        target:
          entity_id: cover.living_room_blinds
```

## Troubleshooting

### Cover commands time out

1. Check the bridge is online: the retained `rf433/<bridge_id>/availability` topic should be
   `online` (use an MQTT explorer, e.g. **MQTT Explorer** or `mosquitto_sub`).
2. Confirm Home Assistant's MQTT integration is connected to the **same broker** as the bridges.
3. Watch `rf433/<bridge_id>/status` while commanding — a `rejected` status includes a reason.

### Blind doesn't move but commands are accepted

- Re-check the calibration: capture the remote button again and compare the decoded
  prefix/remote ID with the entry's configuration.
- Increase **RF repeats** in the entry's Configure dialog (distant or obstructed blinds).
- Verify the blind's channel: a motor paired to remote channel 3 ignores a channel-1 frame.

### Position drifts

Travel-time position is an estimate. Re-anchor with a full OPEN or CLOSE, and tune the up/down
travel seconds in Configure (motors are often slower upward).

### Enable debug logging

```yaml
logger:
  default: warning
  logs:
    custom_components.zemismart_blinds: debug
```

## Known Limitations

- **No RX/state sync yet**: presses on the physical remote are not observed (stock ESPHome cannot
  surface Portisch B1 captures as events). Planned as Phase 2 — see [PROTOCOL.md](PROTOCOL.md).
- **Assumed position**: there is no motor feedback; position is modeled from travel time.
- **Bridge isolated from MQTT mid-command**: a bridge that loses its network link (but not power)
  keeps executing its already-armed fail-safe STOP locally. With multiple bridges, commands fail
  over to another bridge meanwhile, and the isolated bridge's late STOP can still reach the motor
  over the air. One-way RF offers no way to recall it; re-issue the movement if a blind stops
  unexpectedly after a bridge drops.
- **Bridge reboot during an HA restart**: a bridge's armed fail-safe STOP lives in its RAM. If the
  bridge power-cycles entirely within Home Assistant's own downtime (offline and back online before
  HA restores state), a restored in-flight partial move cannot detect that its STOP was lost and
  models to its target. Bridges that are offline at restore time, or drop offline afterwards, are
  detected and the cover becomes `unknown`.
- **Calibration needs one capture** per new physical remote (a one-time step per remote).

## Development

```bash
git clone https://github.com/joyfulhouse/zemismart-blinds.git
cd zemismart-blinds
uv sync

# Lint, type check, test
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict
uv run pytest
```

The codec tests pin byte-exact golden vectors (generated with the hardware-validated codec for
synthetic remote identities), exhaust all non-empty channel subsets, and cover calibration
derivation across opcode-byte carries. See [PROTOCOL.md](PROTOCOL.md) for the full protocol
specification.

## Support

- **Bug reports / feature requests**: [GitHub Issues][issues]
- **Questions**: [GitHub Discussions][discussions]

## Support Development

This integration is built and maintained in my spare time, with real hardware and tooling costs
behind every release. If it's useful to you, consider sponsoring the project or leaving a tip to
help offset development and testing — it's genuinely appreciated and helps keep the project
moving.

[![GitHub Sponsors][sponsors-shield]][sponsors] [![Ko-fi][kofi-shield]][kofi]

## Credits

- **[blark/zemismart-blind-protocol](https://github.com/blark/zemismart-blind-protocol)** — the
  starting point for the RF protocol reverse engineering.
- **[Portisch/RF-Bridge-EFM8BB1](https://github.com/Portisch/RF-Bridge-EFM8BB1)** — the RF
  coprocessor firmware that makes raw B0/B1 capture and transmission possible.

## License

MIT — see [LICENSE](LICENSE).

---

[releases-shield]: https://img.shields.io/github/v/release/joyfulhouse/zemismart-blinds?style=for-the-badge
[releases]: https://github.com/joyfulhouse/zemismart-blinds/releases
[license-shield]: https://img.shields.io/github/license/joyfulhouse/zemismart-blinds?style=for-the-badge
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge
[hacs]: https://github.com/hacs/integration
[hacs-repo-shield]: https://my.home-assistant.io/badges/hacs_repository.svg
[hacs-repo]: https://my.home-assistant.io/redirect/hacs_repository/?owner=joyfulhouse&repository=zemismart-blinds&category=integration
[ci-shield]: https://img.shields.io/github/actions/workflow/status/joyfulhouse/zemismart-blinds/ci.yml?branch=main&label=CI&style=for-the-badge
[ci]: https://github.com/joyfulhouse/zemismart-blinds/actions/workflows/ci.yml
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40btli-blue.svg?style=for-the-badge
[maintenance]: https://github.com/btli
[sponsors-shield]: https://img.shields.io/badge/Sponsor-GitHub-EA4AAA.svg?style=for-the-badge&logo=githubsponsors&logoColor=white
[sponsors]: https://github.com/sponsors/btli
[kofi-shield]: https://img.shields.io/badge/Ko--fi-support-FF5E5B.svg?style=for-the-badge&logo=ko-fi&logoColor=white
[kofi]: https://ko-fi.com/bryanli
[bridge-repo]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge
[adapters-repo]: https://github.com/joyfulhouse/ZemismartAdapters
[issues]: https://github.com/joyfulhouse/zemismart-blinds/issues
[discussions]: https://github.com/joyfulhouse/zemismart-blinds/discussions
