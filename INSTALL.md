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

## Step 4 — Calibrate and add your first blind

Each run of the add-integration flow creates exactly one device and one cover entity.

1. Put a bridge into capture mode: press its **Start bucket sniffing** diagnostic button, press a
   button (UP, DOWN, or STOP) on the blind's physical remote, and copy the `AAB1...55` line from
   the bridge's ESPHome logs.
2. **Settings → Devices & services → Add integration → Zemismart Blinds.**
3. Name the blind, choose **Enter a remote manually**, and paste:
   - the remote's 24-bit prefix and 8-bit remote ID (decoded from the capture — the first three
     bytes and the fourth byte of the 64-bit payload; see
     [PROTOCOL.md](PROTOCOL.md#learning-a-remotes-identity)),
   - which button the capture was (UP/DOWN/STOP), and the captured frame itself. The flow derives
     the complete calibration.
4. Enter one channel (`1`) or an arbitrary group (`1,2,3`); channels 1–16 are supported.
5. Enter up/down full-travel seconds and select the blind's Home Assistant area.
6. Repeat for the next blind. Additional blinds on the **same remote** can reuse the calibrated
   identity from the dropdown — no new capture needed.

Use **Configure** on an existing entry to edit its identity, channels, timing, area, or RF repeat
count.

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
