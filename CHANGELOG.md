# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`zemismart_blinds.reanchor` service.** An explicit recovery action — operator- or
  automation-driven, never autonomous and never on a timer — that drives a cover (or group) to a
  chosen hard endpoint (`open` or `close`) and re-anchors its position estimate there. It reuses
  the normal full-travel command path (ledger registration, commanded-start, air arbitration and
  coalescing all apply), so the endpoint completion re-anchors through the same outcome-based
  logic a manual full open/close uses. It needs no prior estimate — which is the point: it
  recovers a cover whose position is `unknown`. On an aggregate it is exactly one group frame.

- **`position_confidence` attribute on covers and groups.** Lets an automation ask whether a
  position is trustworthy — especially of an aggregate, which previously exposed only
  `channels`/`remote`/`role`. Values: `verified` (last travel completed to a hard limit and no
  doubt has been raised since), `assumed` (the normal modelled-from-travel-time state), and
  `suspect`. `unknown` is read from the entity state itself rather than duplicated in the
  attribute. An aggregate derives its value from the members that contribute a position — the
  worst wins (`suspect` > `assumed` > `verified`), and an unknown member is excluded rather than
  counted as doubt.

  `suspect` marks the one narrow, genuinely ambiguous case from the freeze incidents: an
  **untimed full travel** cut short by an **uncorroborated heard STOP**. Such a travel runs to the
  motor's own limit switch, so a genuine STOP leaves the blind at the frozen estimate while a
  phantom one leaves it at the endpoint — opposite ground truths the integration cannot tell
  apart. The doubt **survives a Home Assistant restart** (the incident's wrong estimate did, so
  the doubt about it must too) and clears only when a later travel completes to a hard limit — a
  `reanchor` is exactly that — or the cover goes `unknown`. It is deliberately kept separate from
  the `unverified_anchor_*` restore-time provenance markers, which track a different doubt.

## [0.5.5] - 2026-07-26

### Fixed

- **Concurrency counting is now actually shared between the callers that need it.** 0.5.4 added a
  parameter to pass a precomputed count into the window calculation and then never wired it to a
  single caller — dead code. It is now supplied by all three call sites and memoised across
  `match()`'s two anchor-lag passes, so an entry is counted once per classification instead of
  once per pass.

### Changed

- **Corrected the performance figure published in 0.5.4.** That entry quoted ~47 µs for a
  full-miss `match()`. The benchmark behind it measured a capture whose signature *nobody had
  registered*, which short-circuits before the count is ever reached — it made **zero** calls to
  the counting function, so it measured the one path this work cannot slow down.

  Measured against the case that matters — a capture whose signature we *do* own, landing outside
  every window, which is exactly the near-miss the WARNING exists for — at the 64-entry per-bridge
  cap: **140 µs** for a realistic spread (covers across distinct channels), and **1.4 ms** for an
  adversarial shape (all 64 entries contending on one channel) that the 16-channel protocol and a
  16-cover house cannot actually produce. Both are down roughly 2x from 0.5.4 thanks to the
  memoisation above.

  These are dev-machine numbers. **They have not been measured on the target**, and no claim is
  made that they hold there — the SSH add-on exposes no Python runtime and no way into the core
  container, so benchmarking there needs instrumentation this change does not justify. For
  calibration when someone does measure it: the deployment this was found on runs a **Raspberry Pi
  Compute Module 5**, which is a considerably faster machine than the Pi 3-class hardware "a Pi"
  usually implies.

  Found by adversarial review, which reproduced the discrepancy rather than accepting the figure.

## [0.5.4] - 2026-07-26

### Fixed

- **Round-robin concurrency is now counted for a gradual sweep, not just a simultaneous one**
  (#21). The count in 0.5.3 asked whether a peer's *unstretched* span overlapped this command's.
  That is a strictly smaller question than the one that matters, because stretch is precisely what
  makes real occupancy exceed the nominal span. At the real sweep's cadence — covers admitted about
  two seconds apart, the shape the 2026-07-25 incident actually had — it counted seven concurrent
  targets as two, closed the window at 5.75 s, and dispatched the command's own 8.1 s repeat as a
  physical press. That is the failure 0.5.3 shipped to prevent, still reachable for the admission
  shape that caused it.

  The question is circular: whether a peer shares the antenna depends on how long this command is
  really on it, which depends on how many peers share it. It is now resolved by fixed point —
  start with no stretch, widen by what the current count implies, recount, and settle. The count
  only grows, so it converges, and the sixteen-target cap bounds the passes.

  Concurrency is counted once per entry per classification — memoised across `match()`'s two
  anchor-lag passes — rather than once per window.

  Found by adversarial review. Every test shipped with 0.5.3 registered its peers at a single
  handoff, which is exactly why none of them caught it.

- **The near-miss warning now reports the bounds it actually judged against**, including any
  round-robin stretch. Logging the nominal edge understated the miss and would send a reader
  hunting the wrong gap.

## [0.5.3] - 2026-07-25

### Documentation

- **README and INSTALL rewritten for people who just bought some blinds.** Both opened on
  protocol architecture and hardware caveats before answering "is this for me?". The README now
  leads with that question, states the one piece of hardware needed up front, and folds the
  depth — air arbitration, virtual remotes, restart edge cases — into disclosure sections that
  stay available without being in the way. Visible prose is down to about 40% of before with
  nothing removed. INSTALL is now a four-step path with a what-you-need checklist and an explicit
  "it's working when" check at the end of each step.

### Fixed

- **A commanded start no longer silently swallows a genuine remote press** (#15). Any
  same-remote overlapping-channel press heard before a recorded commanded start was
  dropped with no bound on how far before — no dispatch, no takeover, no disarm, no log —
  for the stamp's whole 60 s retention. A capture held while its command was pending is
  re-classified only once the bridge confirms, so somebody pressing STOP on a moving blind
  reached that guard with a `heard_at` far below the eventual `started_at` and vanished
  whole. The guard now suppresses only genuine **late news** — a press heard before our RF
  started *and* still undelivered when it did — and logs every suppression.

  This supersedes the note in 0.5.2 below, which described `_COMMANDED_START_TTL_SECONDS`
  as an unchanged blanket. It is no longer a blanket: retention is unchanged, but
  suppression depth is now bounded by delivery order rather than unbounded.

  Recognising our own echo was never this guard's job and is not affected: that is the
  ledger window's asymmetric lower edge, added in 0.5.2, and an echo that outruns even
  that tolerance is still reported by the near-miss log rather than silently absorbed.

- **An early-flushed STOP is no longer read as a person stopping the blind** (#16). A timed
  move's `stop_raw` sits armed on the bridge until its deadline, and a newer overlapping
  command flushes it immediately. Nothing orders the peer bridge's report of that flushed
  frame after the transmitting bridge's own `displaced` status — both cross the same
  broker, and the queueing that biases `started` late biases `displaced` too. In the losing
  order the frame was matched against the *original* windows, with the STOP a whole
  `stop_after_ms` away, and dispatched as a physical press — which then displaced the very
  command that caused the flush, so a quick re-command of a moving cover could simply stop
  instead of moving.

  The flush is now recognised from the command that causes it: latest-command-wins means an
  overlapping newer command on the same bridge is what flushes the older one's armed STOP.
  Displacement is a **one-time event at admission**, so ownership is pinned to the newer
  command's measured handoff — the instant its own first frame went on air — and not to how
  long that command stays armed. Bounding it by the displacer's liveness instead would be
  catastrophic: a displacer that is itself a timed move stays armed until its own deadline,
  which firmware caps at one hour, so adjusting a blind twice in quick succession would
  leave a real STOP on the first command's channels invisible for the whole remaining span
  of the second.

  Deliberately **not** a blanket widening — our `stop_raw` is byte-identical to the frame a
  person's remote puts on air, so a mid-travel STOP outside a displacing command's
  admission instant still takes the cover over exactly as before.

  Separately, `displaced` carries no `age_ms`, so its window was anchored on pure
  wall-clock receipt — worse than `started_at`, which at least removes the firmware's own
  queueing. The drain window now takes the same lower-edge tolerance every other confirmed
  window gets. Firmware stamping `age_ms` on `displaced` would let this be measured rather
  than budgeted.

- **Emission proof now follows the frame rather than the clock** (#17). Two sequential
  commands for one cover — a repeated `close_cover` — share a frame signature, and the
  anchor-lag lower edge added in 0.5.2 let the newer command's window reach back across the
  older one's. An echo of the older command's own repeat train was credited to its
  successor. Press suppression was never affected (both are our own frames and neither
  dispatches); the casualty was the restore-time anchor verification, where a cover waiting
  on one specific command for post-restart proof never received it. Window fit is now
  ranked: a capture that fits without spending the anchor-lag budget wins outright, and
  only when nothing fits on those terms is the budget spent.

- **Our own repeats are no longer read as a physical press when one bridge serves several
  blinds** (#21). A bridge holding more than one target dispatches them round-robin, one slot
  each, so a command's own repeat train is spread out rather than sent back-to-back. The
  own-emission window's upper edge was computed as if the train were always contiguous, so past
  four concurrent targets our own later repeats fell outside it and were dispatched as a physical
  remote press — invalidating the in-flight commanded motion and either re-anchoring travel at a
  wrong instant or marking the cover unknown. Measured on air: at seven concurrent targets our own
  frames were still going out 8.1 s after their handoff, against a window that closed at 3.75 s.

  The upper edge now stretches by the round-robin concurrency actually observed, counted at
  classification time from ledger state — not fixed at registration, because the peers that
  stretch a train are usually admitted after it. Only the first repeat keeps native timing, so the
  widening is `(repeats - 1) x (concurrent - 1)` slots rather than a blanket multiply of the whole
  train, and it collapses to exactly today's behaviour when a bridge serves one target. It is
  bounded by the firmware's sixteen-target limit so a miscount cannot widen a window without end.
  The lower edge, and its anchor-lag tolerance from 0.5.2, are untouched.

  The cost is honest and bounded: while a window is open a genuine same-signature press is
  absorbed rather than recognised as a takeover. Measured on the frame's own window, seven
  concurrent targets hold it open about 15.7 s instead of 3.75 s. Counted the way the `repeats`
  help text counts it — the full span a real press can be missed, including the anchor-lag
  tolerance below the window — the same case moves from about 9.5 s to about 21 s.

  Note what is and is not affected. Only a press matching a frame we actually own can be absorbed,
  so this reaches **timed partial moves**, which carry an armed `stop_raw`. A plain open or close
  owns no STOP frame, so a physical STOP during one is still recognised immediately at any
  concurrency. And nothing here sits between the remote and the motor: the blind stops either way,
  it is Home Assistant's model that lags.

  Widening remains the safe direction — the alternative is asserting a takeover we cannot
  distinguish from our own transmission.

- **A travel that ends against a hard limit re-anchors itself** (#23). Both endpoints are
  physical stops, so a cover that ran a travel out to 0 or 100 is held there by the motor's own
  limit switch and its estimate is corroborated by the hardware. That already settled a
  questioned restore-time anchor, but only when the *commanded* target was an endpoint. A group
  member whose own travel clamps to its limit while the group aims somewhere in between reaches
  the same hard stop and was left questioned anyway, because `absolute_anchor` records the
  group's intent rather than the member's outcome. Re-anchoring now keys on where the motion
  actually ended.

  Deliberately not applied to a position that merely *reads* 0 or 100 without a completed travel
  behind it: a restored estimate from a questioned origin would then launder itself into a
  verified one, which is what marking it unknown exists to prevent.

### Documentation

- **The RF-repeats selector now states the takeover-responsiveness tradeoff** (#20). `repeats`
  is the number of complete press bursts the bridge puts on air — each already a full OEM burst
  of embedded frame repeats, not a single frame. Raising it improves reliability for distant or
  obstructed blinds, but each extra burst the bridge owes widens the interval in which a genuine
  physical STOP press near a just-issued overlapping command is classified as our own flushed
  emission instead of a takeover — about 9.5 s at the default 3, growing to about 26.5 s at the
  maximum 20. That coupling was invisible at the point of choice; the `repeats` field's help
  text in the add and reconfigure flows now quantifies it. This is documentation only: widening
  the window is the correct, physically honest direction (see #16), so the fix is to inform the
  choice, not bound it. The README note added after 0.5.2 covers the same tradeoff for readers
  who never open the dialog.

## [0.5.2] - 2026-07-25

### Fixed

- **The integration no longer reads its own STOP transmission as a physical remote press.**
  `started_at` is derived as `recv_time - age_ms/1000`: `age_ms` corrects the firmware's own
  queueing delay, but nothing corrects the MQTT transport leg between the bridge publishing
  its status and Home Assistant's callback running. The anchor is therefore biased **late and
  never early**, and every emission window built from it sits later than the RF it describes
  — measured at **1.117 s** during a concurrent seven-cover burst, against 0.75 s of slack.

  A `stop_raw` frame fires `stop_after_ms` after its action frame, so its echo arrives long
  after the command confirmed and is classified through the ordinary window path — where
  that shift pushed our own STOP outside its own window. It was then dispatched as a
  physical press, freezing HA's travel model partway while the motor ran on to its limit,
  and leaving HA reporting a partial position for a cover that had physically closed. The
  `_dispatch_press` commanded-start guard cannot help: it only drops presses heard *before*
  a commanded start, and this echo arrives after one.

  Confirmed windows are now **asymmetric** — the lower edge absorbs
  `_LEDGER_ANCHOR_LAG_SECONDS`, the upper edge keeps its original tight slack, since the
  bias only ever runs one way. Verified end-to-end at consumer level: against the previous
  code the phantom `STOP` is dispatched; with this change it is not. A concurrent
  seven-cover burst regression test covers the workload that exposed it — single-cover
  operation never showed it.

  `_LEDGER_ANCHOR_LAG_SECONDS` is calibrated against a single measurement and is **not** an
  architectural ceiling. A capture matching a known signature but falling outside its window
  is logged, so a load pattern exceeding the bound is visible rather than silently becoming
  a phantom press again. Note also the pre-existing `_COMMANDED_START_TTL_SECONDS` (60 s)
  guard, which blanket-suppresses same-remote overlapping-channel presses heard before a
  commanded start; it is unchanged here and is a far larger blanket than anything added.

- The remote's device is now identified by the durable remote key (`prefix:remote_id`, the
  same identity as the entry's unique id) instead of the config **entry id**. Existing
  devices are re-identified **in place** on the next setup, so `device_id`, area overrides,
  name, and all attached entities are preserved — automations targeting the remote device
  keep working. Previously, deleting and re-adding a remote minted a new entry id and
  therefore a brand-new device, silently breaking every `device_id` target while
  `entity_id` targets returned intact. A relearn — the one flow that changes an existing
  entry's identity — re-keys the device in place for the same reason.

  Scope: `device_id` survives a delete-and-re-add, because Home Assistant restores the
  deleted row by identifier. Rolling back to 0.5.1 churns `device_id` once as the old code
  recreates the device under the retired entry-id key; no duplicates or orphans accumulate.

- **A user's device-page area override now survives a delete-and-re-add too** (#18).
  Decision: a restored device keeps its area; the remote's configured area seeds a
  genuinely **new** device and nothing else.

  Three things settle it. Home Assistant's own deleted-device record deliberately carries
  `area_id`, the user's rename and labels across the delete and replays them for 30 days on
  restore — the override was never lost, we were overwriting it. We already preserve the
  area everywhere else the row survives, including the in-place re-key above, and the
  rename and labels already survived this same delete because we never touched them; area
  was the lone exception. And "a full delete resets to defaults" does not describe what
  happens here: if a delete really reset the device, `device_id` would not survive either.
  A half-reset — id and name persist, area silently does not — is worse than either whole
  behaviour, because nothing tells the user which of their settings are the durable ones.

  The cost is the opposite case: someone who deletes and re-adds a remote specifically to
  clear a bad area must now clear it on the device page instead. That is one visible click,
  against an override that used to disappear with no indication it ever had.

  New versus restored is decided by asking the device registry whether it still holds the
  deleted row — the identical lookup Home Assistant itself performs to choose between
  restoring and creating. The row's own fields cannot answer it: a restored device whose
  area the user had **cleared** comes back with no area, exactly like a fresh one. Both
  cases are pinned by tests.

### Notes

- **Correction to the 0.3.1 release notes (retroactive).** 0.3.1 moved covers into the
  remote's device and pruned the pre-0.3.1 per-cover child devices. Those notes promised
  only that friendly names and entity ids stay byte-stable; they did not say that the
  per-cover **child devices were removed**, so any automation or script targeting a cover
  by `device_id` stopped matching from 0.3.1 onward and silently did nothing. Retarget
  those actions **by `entity_id`** (cover entity ids were preserved throughout), or target
  the remote's device. 0.5.0 did **not** cause this: its migration preserves cover
  identity, and `unique_id` values that look freshly minted are ULIDs created when the
  covers were first onboarded.

[0.5.2]: https://github.com/joyfulhouse/zemismart-blinds/releases/tag/v0.5.2

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

- **OEM truncated-trailer captures decode**: some remotes transmit 64 payload bits plus a single
  trailer 0-read instead of the nominal `[1, 0]`; receive-side decoding (`decode_rx_capture`) now
  tolerates it, while transport encode/decode stays strict. The regression fixtures retain real
  field-captured bucket timing jitter and trailer structure but are re-keyed to a synthetic
  identity rather than storing verbatim captures. Presses with this structure were previously
  dropped silently.
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
