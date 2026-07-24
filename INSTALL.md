# Installation Guide

Complete setup takes three parts: an MQTT broker, at least one RF bridge, and this integration.

## Prerequisites

| Requirement | Details |
|---|---|
| **Home Assistant** | 2026.5 or newer with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) configured |
| **MQTT broker** | Any broker works — the [Mosquitto add-on](https://www.home-assistant.io/addons/mosquitto/) is the easiest |
| **RF bridge** | One or more Sonoff RF Bridge R2 units flashed with [joyfulhouse/esphome-rf433-mqtt-bridge](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge) |
| **Blinds** | AOK OEM 433.92 MHz tubular motors (commonly sold as Zemismart) |

## Step 1 — MQTT broker

If you don't already run a broker, install the Mosquitto add-on
(**Settings → Add-ons → Add-on store → Mosquitto broker**), start it, and let Home Assistant's
MQTT integration discover it. Any other broker (standalone Mosquitto, EMQX, NanoMQ, a NAS
container) works the same way — this integration publishes through whatever broker Home
Assistant's MQTT integration is connected to.

## Step 2 — Flash the bridge(s)

Follow the [bridge README](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge) to flash each
Sonoff RF Bridge R2 (the EFM8BB1 RF coprocessor must run Portisch firmware). Point every bridge at
the same broker Home Assistant uses, tag it with the Home Assistant **area ID** it lives in, and
set `default_bridge: "true"` on exactly one.

A healthy bridge shows retained `rf433/<bridge_id>/availability` = `online` on the broker.

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

See the [README troubleshooting section](README.md#troubleshooting) for common issues.
