# Setup guide

Four steps. The only hard one is step 2 — it involves soldering two wires.

**Time:** about an hour for your first bridge, then ten minutes per remote.

---

## What you need

| | |
|---|---|
| ☐ **Home Assistant** | 2026.5 or newer, with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) set up |
| ☐ **An MQTT broker** | The [Mosquitto add-on](https://www.home-assistant.io/addons/mosquitto/) is easiest |
| ☐ **A Sonoff RF Bridge R2** | 433 MHz variant — **revision matters**, see [step 2](#step-2--build-a-bridge) |
| ☐ **Your blinds** | AOK 433.92 MHz tubular motors, usually sold as Zemismart |
| ☐ **A stopwatch** | To time how long each blind takes to open and close |

---

## Step 1 — MQTT broker

Already running one? Skip ahead.

Otherwise: **Settings → Add-ons → Add-on store → Mosquitto broker**, install it, start it, and let
Home Assistant's MQTT integration discover it.

Any broker works — standalone Mosquitto, EMQX, NanoMQ, something on a NAS. This integration just
uses whatever broker Home Assistant's MQTT integration is connected to.

---

## Step 2 — Build a bridge

Home Assistant can't speak 433 MHz on its own, so every command goes out through a small radio
bridge.

### Buy the right board

| | |
|---|---|
| **What** | Sonoff RF Bridge R2, **433 MHz** variant |
| **Works** | R2 **V1.0 / V2.0** — Silicon Labs **EFM8BB1** chip |
| **Does not work** | R2 **V2.2** (2022 onward) — **OB38S003** chip |

> ⚠️ **Sonoff changed the radio chip in 2022 without changing the product name.**
>
> The newer chip can't run the firmware this project depends on. Sellers rarely state which
> revision they're shipping, so buying new is a coin flip unless the seller confirms the chip or
> you can return it. **Secondhand V1.0/V2.0 units are the safe buy.**

### Flash it

Each bridge needs **two** firmwares: one for the radio chip, one for the Wi-Fi chip.

👉 **[Full walkthrough: the bridge repo's HARDWARE.md][bridge-hardware]** — identifying your board,
soldering the two programming jumpers, flashing the radio chip, then the ESPHome package.

Two things to know going in:

- **You will need to solder two short wires** to flash the radio chip.
- **Tasmota is only a temporary tool** for that one step. A bridge left running Tasmota *cannot*
  drive this integration — it speaks a different MQTT contract.

### Configure it

Point the bridge at the same broker Home Assistant uses, tag it with the Home Assistant **area ID**
of the room it sits in, and set `default_bridge: "true"` on exactly one bridge.

✅ **It's working when** the retained topic `rf433/<bridge_id>/availability` reads `online`.

### How many do I need?

**Start with one.** Add more only when a room turns out to be out of range. Commands automatically
route to a bridge in the cover's own area and fall back when one is offline.

---

## Step 3 — Install the integration

**Via HACS (recommended)**

1. In HACS, add `https://github.com/joyfulhouse/zemismart-blinds` as a **custom repository**,
   category **Integration**.
2. Install **Zemismart Blinds**.
3. Restart Home Assistant.

**Manually**

1. Copy `custom_components/zemismart_blinds` into your `/config/custom_components/` directory.
2. Restart Home Assistant.

---

## Step 4 — Add your first remote

Each run of this flow sets up **one physical remote** and all the blinds it controls.

1. **Settings → Devices & services → Add integration → Zemismart Blinds**
2. Choose **Learn from remote**.
3. Name the remote, pick its area, and accept the suggested bridge.
4. **Press Up, Down, or Stop on your physical remote** during the 30-second window. The integration
   works out the rest by itself.
5. Confirm what it detected, then add your blinds one at a time:

   | Field | What to enter |
   |---|---|
   | **Name** | e.g. "Living Room Left" |
   | **Channels** | `1` for one blind, `1,2,3` for a group (channels 1–16) |
   | **Travel time** | Stopwatch seconds, fully up and fully down — time each direction separately |

6. Repeat for your next remote.

✅ **It's working when** the cover responds to open, close, and stop.

<details>
<summary><b>Changing things later</b></summary>

Everything lives in the entry's **Reconfigure** menu: **Relearn from remote** replaces the identity
or calibration, **Edit remote settings** covers name/area/RF options, and **Add / Edit / Remove
cover** manage covers without disturbing their entity IDs, history, or automations.

Under **Advanced**, you can also enter a remote manually or mint a virtual one — see the
[README](README.md#setting-up-a-blind).

</details>

---

## If something's wrong

**Blind doesn't respond at all** → check the bridge is `online` (step 2), and that Home Assistant's
MQTT integration uses the *same* broker as the bridge.

**Command accepted but nothing moves** → most often the wrong channel. A motor paired to channel 3
ignores a channel-1 command.

**Position slowly goes wrong** → normal drift; send a full open or close to re-anchor, then tune
the travel seconds.

More detail in the [README troubleshooting section](README.md#troubleshooting). Bridge hardware
problems — a failed flash, a bridge that never comes online — are covered in
[the bridge repo][bridge-troubleshooting].

<details>
<summary><b>Debug logging</b></summary>

```yaml
logger:
  default: warning
  logs:
    custom_components.zemismart_blinds: debug
```

</details>

[bridge-hardware]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/blob/main/HARDWARE.md
[bridge-troubleshooting]: https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/blob/main/HARDWARE.md#troubleshooting
