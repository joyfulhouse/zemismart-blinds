# Installation Guide

Complete setup takes three parts: an MQTT broker, at least one RF bridge, and this integration.

## Prerequisites

| Requirement | Details |
|---|---|
| **Home Assistant** | 2026.5 or newer with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) configured |
| **MQTT broker** | Any broker works — the [Mosquitto add-on](https://www.home-assistant.io/addons/mosquitto/) is the easiest |
| **RF bridge** | One or more Sonoff RF Bridge R2 units flashed with [joyfulhouse/esphome-rf433-mqtt-bridge][bridge-repo] — see [Step 2](#step-2--build-your-rf-bridges) |
| **Blinds** | AOK OEM 433.92 MHz tubular motors (commonly sold as Zemismart) |

## Step 1 — MQTT broker

If you don't already run a broker, install the Mosquitto add-on
(**Settings → Add-ons → Add-on store → Mosquitto broker**), start it, and let Home Assistant's
MQTT integration discover it. Any other broker (standalone Mosquitto, EMQX, NanoMQ, a NAS
container) works the same way — this integration publishes through whatever broker Home
Assistant's MQTT integration is connected to.

## Step 2 — Build your RF bridge(s)

Home Assistant has no 433 MHz radio, so every command reaches your blinds through a **Sonoff RF
Bridge R2** running open firmware. This is the one part of the setup that involves hardware.

### Buying the hardware

| | |
|---|---|
| **What** | Sonoff RF Bridge R2, **433 MHz** variant |
| **Where** | [itead.cc](https://itead.cc/product/sonoff-rf-bridge-433/) and the usual marketplaces |
| **Validated on** | R2 **V1.0 / V2.0** (Silicon Labs **EFM8BB1** coprocessor) |
| **Not supported** | R2 **V2.2** (2022+, **OB38S003** coprocessor) |

> **Check the revision before you buy.** Sonoff swapped the RF coprocessor in 2022. This project
> is validated only on the older **EFM8BB1** boards; the newer **OB38S003** cannot run the
> Portisch firmware everything here depends on. Current stock from any seller — including the
> link above — may ship either revision without saying so, so a new purchase is a gamble unless
> the seller confirms the chip or you can return it. Secondhand R2 V1.0/V2.0 units are the safe
> buy.

### Flashing

Each bridge needs **two** firmwares — Portisch on the RF coprocessor, then the ESPHome package on
the Wi-Fi chip. **[The bridge repo's HARDWARE.md][bridge-hardware] is the complete walkthrough**:
identifying your board, soldering the two programming jumpers, using Tasmota as a one-time tool to
flash Portisch, then replacing it with the ESPHome package.

Two things worth knowing before you start: the coprocessor flash requires **soldering two short
wires**, and although Tasmota is the practical way to perform that one step, **a Tasmota bridge
cannot drive this integration** — the integration speaks the ESPHome package's MQTT contract
(correlated acknowledgements, bridge-held fail-safe STOP deadlines, idle-listen receive), which
Tasmota's `RfRaw` topics do not provide.

### Per-bridge configuration

Point every bridge at the same broker Home Assistant uses, tag it with the Home Assistant **area
ID** it lives in, and set `default_bridge: "true"` on exactly one.

A healthy bridge shows retained `rf433/<bridge_id>/availability` = `online` on the broker.

### How many bridges?

One is enough to start. Add more when rooms are out of RF range of the first — the integration
routes each command to a bridge in the cover's own area, falls back automatically when one is
offline, and schedules transmissions across bridges so they do not talk over each other on the
shared 433 MHz channel.

## Step 3 — Install the integration

### HACS (recommended)

1. In HACS, add `https://github.com/joyfulhouse/zemismart-blinds` as a **custom repository**
   (category: Integration).
2. Install **Zemismart Blinds**.
3. Restart Home Assistant.

### Manual

1. Copy `custom_components/zemismart_blinds` into your Home Assistant `/config/custom_components/`
   directory.
2. Restart Home Assistant.

## Step 4 — Calibrate and add your first remote

Each run of the add-integration flow creates one **remote** device; the remote's blinds and
groups are added as its cover entities in the same run.

1. Open **Settings → Devices & services → Add integration → Zemismart Blinds**, then choose
   **Learn from remote**.
2. Name the remote, select its Home Assistant area, and accept the automatically selected online
   RF bridge or choose another one.
3. During the 30-second capture window, press **Up**, **Down**, or **Stop** on the physical remote.
   The flow detects the remote prefix, remote ID, channels, and button automatically.
4. Confirm the detected identity and the remote's transport settings, then add covers one at a
   time: a cover name, one channel (`1`) or an arbitrary group (`1,2,3`), and the up/down
   full-travel seconds. Channels 1–16 are supported. Add every blind and group the remote
   controls, then finish.
5. Repeat for the next remote. Under **Advanced**, manual capture entry and virtual remotes are
   available.

Everything about an existing remote lives in its entry's **Reconfigure** menu: **Relearn from
remote** replaces the identity or calibration, **Edit remote settings** covers name/area/RF
options, and **Add / Edit / Remove cover** manage its covers without touching their entity IDs.

## Verify

- The cover entity responds to OPEN/CLOSE/STOP.
- `zemismart_blinds.send_raw` (Developer tools → Actions) can replay a captured frame through a
  named bridge for debugging.

## Troubleshooting

Enable debug logging in `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.zemismart_blinds: debug
```

See the [README troubleshooting section](README.md#troubleshooting) for common issues. Problems
with the bridge hardware itself — a failed coprocessor flash, a bridge that never comes online —
are covered in [the bridge repo's troubleshooting section][bridge-troubleshooting].

[bridge-repo]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge
[bridge-hardware]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/blob/main/HARDWARE.md
[bridge-troubleshooting]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/blob/main/HARDWARE.md#troubleshooting
