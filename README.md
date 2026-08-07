# Zemismart Blinds for Home Assistant

Control AOK/Zemismart 433 MHz roller blinds from Home Assistant. No cloud, no vendor app, no hub.

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)
[![HACS][hacs-shield]][hacs]
[![CI][ci-shield]][ci]
[![Project Maintenance][maintenance-shield]][maintenance]
[![GitHub Sponsors][sponsors-shield]][sponsors]
[![Ko-fi][kofi-shield]][kofi]

---

## Is this for you?

Your blinds came with a small 433 MHz remote and **no app, no Wi-Fi, no hub** — the remote is the
only way to control them. They're sold as **Zemismart**, or under other names using **AOK** motors.

If that's you, this integration gives you open, close, stop, and set-position in Home Assistant.

**You'll need one piece of hardware:** a Sonoff RF Bridge (~$15) to actually send the radio
signals. Home Assistant has no 433 MHz radio of its own.

## What you get

- **Open, close, stop, and set position** — including partial positions
- **Groups** — "all office blinds" is *one* radio command, not five competing ones
- **Guided setup** — press Up, Down and Stop when asked; each one is measured from your own remote
- **Sees your physical remote** — press the wall remote and Home Assistant follows along
- **Multiple bridges** — big houses work; commands route to the nearest bridge automatically

## Quick start

**1. Get a bridge working** — a Sonoff RF Bridge R2, flashed with
[our firmware][bridge-repo]. This is the only fiddly part, and it involves soldering.

**2. Install this integration** — via HACS, then restart Home Assistant.

[![Open in HACS][hacs-repo-shield]][hacs-repo]

**3. Add your remote** — *Settings → Devices & services → Add integration → Zemismart Blinds*,
choose **Learn from remote**, and press Up, Down and Stop on your physical remote as it asks for
each one.

📖 **[Full setup guide → INSTALL.md](INSTALL.md)**

## How it works

You add a **remote**, and that remote owns its blinds. One physical remote becomes one device in
Home Assistant, and each blind (or group of blinds) becomes a `cover` entity on it.

```
Home Assistant  ──►  MQTT broker  ──►  RF Bridge  ──📡──►  your blinds
                                            ▲
                                            └──📡── your physical remote
```

The integration generates the exact radio signals your remote sends. The bridge is a dumb relay —
it knows nothing about blinds.

## Setting up a blind

After the learn step, you add covers one at a time. Each needs:

| Field | What to enter |
|---|---|
| **Name** | e.g. "Living Room Left" |
| **Channels** | `1` for a single blind, or `1,2,3` for a group |
| **Travel time** | How many seconds it takes to go fully up, and fully down |

Time your blind with a stopwatch — that's how position gets estimated. Or don't: leave both
travel fields **blank** and the wizard measures it for you.

<details>
<summary><b>Measuring travel times with the remote</b></summary>

Leave the travel fields empty and the flow asks which bridge should listen, then you run the
shade once in each direction with its physical remote:

1. Press **UP or DOWN** (either order — the integration detects which from the RF frame itself).
2. Watch the shade run to its limit.
3. Press **STOP** the moment it stops moving.
4. Repeat for the other direction when prompted.

The interval between the direction press and the STOP is measured on the bridge's own clock and
**rounded up** to whole seconds — pressing STOP after arrival already measures a little long, and
a generous travel time simply stalls the motor at its own limit switch instead of leaving a close
visibly short. A confirmation screen shows the raw measurements in editable fields before
anything is saved.

Also available when editing an existing cover: pick the cover under **Edit cover**, then choose
*Measure travel times with the remote* — or just clear both travel fields on the edit form.
While a re-measurement runs, the shade's reported position may look wrong; it resets when the
new values save.

Virtual remotes can't be measured this way — nothing physical transmits a synthesized identity —
so their covers still require typed travel times.

</details>

<details>
<summary><b>Changing things later</b></summary>

Everything lives in the entry's **Reconfigure** menu:

- **Relearn from remote** — recapture the identity or calibration
- **Edit remote settings** — name, area, RF repeats, coalescing
- **Add / Edit / Remove cover** — manage covers; edits keep entity IDs, history, and automations intact

</details>

<details>
<summary><b>Advanced setup (manual entry, virtual remotes)</b></summary>

Instead of learning from a remote, you can enter one manually from a labeled B0/B1 reference or a
direct 16-bit action base. Hex works with or without the `0x` prefix. Leave the optional OEM
TRAILER base blank unless you captured one.

**Virtual remotes** mint an identity that never existed as hardware, so you can pair a motor
directly to Home Assistant. Call `zemismart_blinds.new_virtual_remote`:

```yaml
prefix: "0x5c1a2b"
remote_id: "0x3c"
base_up: "0xf4a1"
base_down: "0xbc69"
base_stop: "0xdc89"
```

To pair one:

1. Add a manual entry using the returned prefix, remote ID, and UP base (calibration action UP).
2. Put the motor into RF pairing mode with its program button (watch for the pairing jog — exact
   timing varies by motor revision, so follow the motor's own instructions).
3. Send OPEN from the new cover while pairing mode is active, exit pairing mode, then verify OPEN,
   CLOSE, and STOP. Keep the original remote paired until you've confirmed it all works.

</details>

## About position

**Position is an estimate, not a measurement.** These motors report nothing back — the integration
counts seconds against your configured travel time.

What that means day to day:

- A **full open or close** is self-correcting. The motor stops at its own limit, so the estimate
  re-anchors at 100 or 0.
- A **partial position** drifts a little over time. Send a full open or close to true it up.
- A brand-new blind reads `unknown` until you move it fully one way.

### How much to trust it: `position_confidence`

Every cover and group publishes a `position_confidence` attribute so an automation can ask how
much the current estimate is worth. The ranking, weakest first:

| Value | What it means |
| --- | --- |
| `unknown` | No position at all. Read from the entity state, never stored. |
| `suspect` | The estimate rests on evidence nobody could corroborate: a full travel cut short by an unconfirmed physical STOP (frozen where the estimate says, or resting at the endpoint — opposite ground truths), or a travel whose completion was inferred purely from wall time elapsed while Home Assistant was down. Survives a restart. |
| `assumed` | The normal state: modelled from travel time since the last anchor. |
| `anchored` | The strongest claim available: a full travel was transmitted, its timer ran to completion, and no contradicting RF press was heard while it ran. |

**`anchored` is not motor confirmation.** Nothing comes back over the radio — the motor never
reports that it received the frame, ran for the calibrated duration, or reached its limit switch.
`anchored` says only what the integration itself witnessed. (This value used to be called
`verified`, which claimed more than the system can know; automations matching the old word must be
updated — see the changelog.)

Two deliberate boundaries: a travel that completed while Home Assistant was **down** earns no
`anchored` — no listener ran, so a press in that gap was invisible — and `anchored` does not
survive a restart for the same reason.

A group reports `unknown` whenever any of its members has no position, and whenever a cover it is
configured to contain has no working entity right now (a cover missing its travel times is skipped
at startup) — in both cases the group has no position at all, and one rule holds throughout: no
position means `unknown`. A `suspect` member still outranks that, because a blind frozen by a STOP
nobody could corroborate says more about the group than a sibling merely being blank.

**Channels with no cover configured for them are a different matter.** A group on channels `1,2,3,4,5,6`
with covers for only `1`–`5` derives its position, its open/closed state and its confidence from
those five; channel 6 is *unmodelled* and disregarded, because nothing in Home Assistant describes
it. The group still transmits open, close and stop to all six channels — the physical remote's
group button always did — so channel 6 keeps moving with the rest; it simply contributes nothing to
what the group reports. Setting a percentage moves only the covers that model a channel. Each group
publishes an `unmodelled_channels` attribute listing them, empty when there are none.

`zemismart_blinds.reanchor` drives a cover to a hard endpoint on purpose, which is how you get
back to `anchored` from `suspect` or `unknown`.

<details>
<summary><b>Why a blind can be "wrong" in Home Assistant</b></summary>

The radio protocol is one-way. The integration knows a command *left the bridge*, never that a
motor *received* it. So a command that never arrives looks exactly like one that worked, and the
cover will report the position it expected — no error, no log entry.

If a blind genuinely matters to an automation, verify by eye or trigger on something else.

</details>

## Automation example

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

<details>
<summary><b>Commands time out</b></summary>

1. Is the bridge online? Its retained `rf433/<bridge_id>/availability` topic should read `online`.
   Check with MQTT Explorer or `mosquitto_sub`.
2. Is Home Assistant's MQTT integration pointed at the **same broker** as the bridges?
3. Watch `rf433/<bridge_id>/status` while sending a command — a `rejected` status explains why.

</details>

<details>
<summary><b>Command is accepted but the blind doesn't move</b></summary>

- **Check the channel.** A motor paired to channel 3 ignores a channel-1 command. This is the most
  common cause.
- **Recheck calibration.** Capture the remote button again and compare the decoded prefix and
  remote ID against the entry.
- **Try more RF repeats** in *Configure* — but read the tradeoff under
  [Good to know](#good-to-know) first. More repeats is not always the right lever.

</details>

<details>
<summary><b>Position is drifting</b></summary>

Send a full open or close to re-anchor, then tune the travel seconds in *Configure*. Motors are
usually slower going up than down, so time each direction separately.

</details>

<details>
<summary><b>Enable debug logging</b></summary>

```yaml
logger:
  default: warning
  logs:
    custom_components.zemismart_blinds: debug
```

</details>

## Good to know

These are real behaviours worth knowing before you rely on the integration for something
important.

- **Position is assumed, never measured.** See [About position](#about-position).
- **More RF repeats trades reliability for responsiveness.** While the integration is
  transmitting, it treats matching signals as its own — so a physical remote press right after a
  command can go **unnoticed by Home Assistant** for roughly **9.5 s** at the default `repeats: 3`,
  or **26.5 s** at the maximum of 20. Several blinds moving at once through one bridge stretches
  that too — about **21 s** at the default with seven moving together.

  **Your blind still stops.** The remote talks straight to the motor; nothing here sits in
  between. What lags is only Home Assistant noticing you took over, so its position estimate can
  be wrong until the next command. This also only applies to *partial* position moves — a plain
  open or close is recognised immediately, however many blinds are moving.
- **A bridge that loses Wi-Fi mid-command** still runs its already-armed stop timer locally. Other
  bridges take over meanwhile, and that late stop can still reach the motor. Re-issue the movement
  if a blind stops unexpectedly.

<details>
<summary><b>More limitations (restarts, takeover, calibration)</b></summary>

- **Live state sync needs the paired bridge firmware.** With the state-sync firmware contract
  (continuous idle-listen `/rx`, boot id, enriched `/status`, `/cmd disarm`), physical remote
  presses are mirrored into each cover. Without it the integration is transmit-only, and RF
  reception is limited to the Learn wizard.
- **`/tx` commands carry the bridge's `boot` id.** Since 0.8.0 the integration stamps every `/tx`
  publish with the `boot` value from the bridge's own retained `/info`, and refuses to publish to
  a bridge it has no boot evidence for. Firmware ≤ 1.3.0 ignores the field; firmware v1.4.0 is the
  first to enforce it, rejecting a mismatched or missing `boot` with `reason: boot_mismatch`. This
  integration release is meant to deploy **before** a firmware upgrade to v1.4.0 — see the
  [firmware README][bridge-repo] for the wire contract.
- **Physical takeover of a restored or clamped timed move.** After a Home Assistant restart, or
  once a group member hits its own limit before the group's frame ends, HA may no longer model the
  bridge's still-armed stop. A remote press reversing such a move isn't guaranteed to disarm it.
- **A bridge that fully reboots during HA's own downtime** loses its armed stop from RAM. A
  restored in-flight partial move can't detect that and models to its target. Bridges offline at
  restore time, or that drop afterwards, are detected and the cover goes `unknown`.
- **Calibration needs one capture per physical remote** — a one-time step.

</details>

## Multiple bridges

One bridge is enough to start. Add more when rooms are out of range.

Commands route to a bridge in the cover's own area, and fall back automatically when one is
offline (the cover then reports `degraded_bridge: true`).

<details>
<summary><b>How simultaneous commands are scheduled</b></summary>

With two or more bridges online, **air arbitration** keeps them from transmitting over each other
on the shared 433 MHz channel. It anchors on each command's actual RF start, reserves known future
fail-safe stop windows, and delays only normal work — an explicit STOP is never held. Arbitration
switches off below two online bridges, and every failure path publishes rather than blocks (a hard
130 s ceiling guarantees it).

The practical effect: a scene that fans out across the house staggers its blinds a couple of
seconds apart instead of colliding.

Counters are included in any entry's diagnostics download. To measure without delaying — or as a
rollback — there's an installation-wide YAML escape:

```yaml
zemismart_blinds:
  air_arbitration_mode: shadow
```

Near-simultaneous commands for blinds on the same remote are also merged into a single group frame
automatically.

</details>

## Hardware

**Bridge:** [Sonoff RF Bridge R2][bridge-hardware-buy], 433 MHz variant, flashed with
[esphome-rf433-mqtt-bridge][bridge-repo].

> ⚠️ **Check the board revision before buying.** Only R2 **V1.0/V2.0** boards (Silicon Labs
> **EFM8BB1** chip) work. The 2022+ **V2.2** uses an **OB38S003**, which cannot run the required
> firmware. Sellers rarely state the revision, so new stock is a gamble — secondhand V1.0/V2.0
> units are the safe buy.

**Motors:** AOK OEM tubular roller-shade motors (Zemismart-branded and others).

**3D-printed adapters** for fitting these motors to other roller tubes:
[joyfulhouse/ZemismartAdapters][adapters-repo].

## Development

```bash
git clone https://github.com/joyfulhouse/zemismart-blinds.git
cd zemismart-blinds
uv sync

uv run ruff check . && uv run ruff format --check .
uv run mypy --strict
uv run pytest
```

The protocol is fully documented in [PROTOCOL.md](PROTOCOL.md). Codec tests pin byte-exact golden
vectors, exhaust all non-empty channel subsets, and cover calibration derivation across
opcode-byte carries.

## Support

- **Bugs and feature requests**: [GitHub Issues][issues]
- **Questions**: [GitHub Discussions][discussions]

## Support development

This is built and maintained in my spare time, with real hardware costs behind every release. If
it's useful to you, sponsoring or a tip genuinely helps keep it moving.

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
[bridge-hardware-buy]: https://itead.cc/product/sonoff-rf-bridge-433/
[adapters-repo]: https://github.com/joyfulhouse/ZemismartAdapters
[issues]: https://github.com/joyfulhouse/zemismart-blinds/issues
[discussions]: https://github.com/joyfulhouse/zemismart-blinds/discussions
