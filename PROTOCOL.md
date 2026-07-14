# AOK / Zemismart 433.92 MHz Roller Blinds — Protocol Reference

> Fully reverse-engineered and **live-validated against physical motors**: any command can be
> generated for any `(remote, channel, group)`, and new virtual remotes can be minted and paired.
> Reference implementation: `custom_components/zemismart_blinds/codec.py`. External protocol
> reference: <https://github.com/blark/zemismart-blind-protocol>. Note that its single-prefix
> command base must **not** be generalized to other remote prefixes — the command base is
> per-remote (see [Command field](#command-field-16b)).

## Hardware

- **Motors:** AOK OEM 433.92 MHz tubular roller-shade motors, most commonly sold under the
  **Zemismart** brand. Other resellers of AOK motors are expected to speak the same protocol.
- **Remotes:** single- and multi-channel OEM remotes. A multi-channel remote addresses its blinds
  as channels 1..N plus an ALL/group broadcast.
- **Bridge:** Sonoff RF Bridge R2 (ESP8285 + EFM8BB1 running the **Portisch** firmware), flashed
  with ESPHome (`rf_bridge:` UART @ 19200). See the companion ESPHome bridge package.

**Key insight:** each physical blind motor is paired to **one remote identity's channel**. The
motor learns whatever remote transmits during its pairing mode — including a virtual remote that
never existed as hardware.

## Frame format (64 bits)

```
[ prefix : 24b ][ remote_id : 8b ][ channel : 16b ][ command : 16b ]     (big-endian)
```

No checksum — integrity comes from the command derivation (below).

### Modulation — OOK constant-period PWM (~900 µs/bit)

- **bit 0** = long-high (~600 µs) + short-low (~300 µs)
- **bit 1** = short-high (~300 µs) + long-low (~600 µs)
- Preamble: a `~5100 µs` low+high sync pair (Portisch nibble prefix `38`).
- Frame = preamble + 64 payload bits + **2 trailing bits `[1,0]`** (matches OEM frames).
- B1 bucket captures may retain one additional idle/sync low-high pair after those trailer bits;
  `decode_b0` accepts only that bounded capture padding and never treats it as payload.
- Captured by the Sonoff bridge in **B1 raw-bucket** mode; transmitted as **B0**
  (`AA B0 <len> 04 08 <4 buckets> <data> 55`, buckets `1414 026C 0118 1414`).
- **A4/A6 Portisch decoders DO NOT decode these** — only B1 raw. ESPHome's `rf_bridge` has
  **no `on_bucket_received` trigger** (B1 is log-only) → RX/listen needs a workaround (see below).

### Channel field (16b) — one cleared bit per channel

```python
channel_field(chs) = 0xFFFF ^ OR( 1 << ((ch + 7) % 16)  for ch in chs )
```

- Single channel N → clears bit `(N+7)%16`.  ch1=`0xFEFF`, ch2=`0xFDFF`, ch3=`0xFBFF`,
  ch4=`0xF7FF`, ch5=`0xEFFF`, ch6=`0xDFFF`.
- **Group = clear several bits at once.** `{1,2}`=`0xFCFF`, `{1,2,3}`=`0xF8FF`,
  ALL `{1..6}`=`0xC0FF`. This is how arbitrary subgroups are addressed in one frame.
- The field supports channels 1..16.

### Command field (16b)

```python
command = (base[button] + remote_id - offset(chs)) & 0xFFFF
offset(chs) = signed8( 2 + SUM( 1 << ((ch-1) % 8)  for ch in chs ) )   # single ch: 2 + 2^((ch-1)%8)
signed8(o)  = ((o + 128) % 256) - 128
```

The action base is **calibrated per remote** from one labeled captured reference:

```python
base[button]  = (captured_cmd + offset(captured_channels) - remote_id) & 0xFFFF
cmd(channels) = (captured_cmd + offset(captured_channels) - offset(channels)) & 0xFFFF
```

Across every remote observed so far, action command opcode bytes are `f4` (UP), `bc` (DOWN), and
`dc` (STOP), and their low bytes differ from UP by `0x00`, `-0x38`, and `-0x18` modulo 256.
Therefore **one labeled UP/DOWN/STOP capture derives all three action bases.** Do not apply the
delta as one 16-bit subtraction: low-byte wrap does not carry into the opcode byte. The codec
first translates an arbitrary captured channel set to the ALL `[1..6]` offset before applying
these bytewise action relationships, so references whose raw command crossed into `f3`/`f5`,
`bb`/`bd`, or `db`/`dd` remain valid.

Some remotes additionally transmit an OEM **TRAILER** command after UP/DOWN bursts. A trailer base
is only used when explicitly calibrated from a capture — the codec never invents an unobserved
trailer command, and repeated action frames have been live-proven to work without one. STOP is
standalone.

### Worked example (synthetic identity)

For a fabricated remote `prefix=0xA1B2C3`, `remote_id=0x42` with calibrated UP base `0xF42A`:

```
channel 1 UP:   offset({1}) = signed8(2 + 1) = 3
                command     = (0xF42A + 0x42 - 3) & 0xFFFF = 0xF469
                payload     = 0xA1B2C3_42_FEFF_F469

group {1,2} UP: offset({1,2}) = signed8(2 + 1 + 2) = 5
                command       = (0xF42A + 0x42 - 5) & 0xFFFF = 0xF467
                payload       = 0xA1B2C3_42_FCFF_F467

ALL {1..6} UP:  offset({1..6}) = signed8(2 + 63) = 65
                command        = (0xF42A + 0x42 - 65) & 0xFFFF = 0xF42B
                payload        = 0xA1B2C3_42_C0FF_F42B
```

These vectors are pinned byte-exactly in `tests/` (`tests/synthetic.py`).

## Learning a remote's identity

Decode any captured frame (B0 or B1) to learn a remote's `(prefix, remote_id)` and one labeled
action command; `derive_bases` completes the calibration. Capture options:

1. Sonoff RF Bridge in Portisch B1 raw-bucket sniff mode (the bridge package exposes diagnostic
   buttons for this) — read the `AAB1...55` line from the ESPHome logs.
2. Tasmota + Portisch `RfRaw 177` on the same hardware.
3. RTL-SDR + `rtl_433` (`s=263,l=582,r=9790,g=6700,y=4960`).

**Virtual remotes:** any unused `(prefix, remote_id)` works — the motors learn whatever remote is
put into pairing mode. The integration's `new_virtual_remote` service allocates an identity with a
complete synthesized calibration.

## Codec (validated)

The integration codec provides `derive_base`, `derive_bases`, `derive_bases_from_base`,
`synthesize_bases`, calibrated `make_payload`, `encode_b0`, strict `decode_b0`, and a
calibration-only reference decoder that tolerates legacy captures with a truncated trailer.

Validation performed against physical motors:

- Decoding of whole-house captures is self-consistent; each remote's ID is constant across its
  ALL and single-channel frames.
- `encode_b0` regenerates stored OEM captures **byte-exactly**.
- **Live:** generated-from-scratch `{1,2} UP` / `{1,2} DOWN` group frames (never captured from a
  remote) physically moved exactly blinds 1 and 2.
- **Live:** an ALL/UP capture's derived channel-1 command exactly matched an independent capture
  of the physical remote's channel-1 button.

## Listen mode (RX) — status

Physical-remote presses are captured **only as B1 raw**, and stock ESPHome cannot surface B1 as an
event. To sync state when someone uses a physical remote, one of:

1. **ESPHome external component** adding an `on_bucket_received`/`on_raw_received` trigger to
   `rf_bridge` → publish the B1 hex to MQTT/API; the integration decodes it with the codec.
   *(preferred — keeps ESPHome)*
2. **Tasmota + Portisch RfRaw** on the bridges → `RfReceived` B1 over MQTT.
3. A dedicated **RTL-SDR + rtl_433** receiver.

TX is unaffected by this choice. RX is not motor feedback and must never trigger TX.

## History / gotchas

- The protocol was initially mis-identified as **RAEX** (Manchester) — the decode never validated
  a checksum, which was the tell. It is the AOK PWM protocol with no checksum. The
  [blark/zemismart-blind-protocol](https://github.com/blark/zemismart-blind-protocol) repository
  was the key starting point.
- The command being **channel-dependent** (via `offset`) is why naive "graft the command bits"
  synthesis can appear to work for one channel and break for another (a channel bit can overlap
  the command region).
