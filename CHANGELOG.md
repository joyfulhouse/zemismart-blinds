# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-08-06

### Fixed

- **The command opcode byte no longer absorbs a low-byte carry; commands wrap modulo 256.**
  Live captures from a carry-straddle remote (its DOWN base low byte plus remote id crosses
  0x100 for small channel groups but not for the 1–6 group) proved the OEM keeps the opcode
  high byte fixed per action: the remote transmitted `bc2a`/`bc27`/`bcec` for DOWN on channels
  {1}/{3}/{1..6}, where the previous 16-bit arithmetic produced `bd2a`/`bd27` — commands the
  motor provably ignored while the wrapped form physically moved it. Symptom fixed: UP and STOP
  worked on every cover but DOWN only worked on the all-channel cover. The same fix makes the
  Learn wizard classify such a remote's single-channel presses instead of failing the capture.
  `PROTOCOL.md` now documents the corrected formula and the field evidence.

- **Config entries migrate to version 3, renormalizing stored command bases.** Each base is
  re-derived from the legacy formula's output at every configured cover's channel set: when the
  candidates agree (every carry-uniform remote) the frames the entry was validated on stay
  byte-identical, and when they disagree (a carry-straddle) the known per-action opcode byte
  picks the physically real side. The migration never refuses an entry — values it cannot parse
  pass through byte-identical for setup's own validation to surface, and an unresolvable
  ambiguity is logged and resolved conservatively (an ambiguous OEM trailer is dropped; action
  frames fall back to preserving the first cover's bytes). No user action needed; the migration
  runs once at startup.

### Changed

- **Manual calibration references are validated against the fixed-opcode model.** A calibration
  base or reference frame recorded from a pre-0.9.0 diagnostics dump may carry an opcode byte
  the legacy formula invented (`bd`/`f5`/`dd`); Advanced setup now rejects those instead of
  deriving sibling commands from them. Recapture the remote, or enter the migrated per-action
  bases directly under Edit remote settings. For the same reason, sibling-base derivation from
  one entered base is only possible for remotes using the common opcode layout — a remote
  outside it (issue #26) needs each action entered or captured individually.

  Known residual: a base that was never captured live (the wizard's "calculate the remaining
  bases" fallback) was derived against the 1–6 calibration channel set, which the migration
  cannot know; on a carry-straddle remote with no 1–6 cover such a derived base can migrate to
  the wrong opcode byte. Derived bases were always flagged "test them afterwards" — recapture
  fixes them. No known remote is affected.

- **Learn wizard step titles no longer use placeholders** (`Capture the {action} button`,
  `Captured {captured}`), which the frontend rendered without values in some contexts and
  logged hundreds of `MISSING_VALUE` translation errors. Titles are static; the dynamic
  action names stay in the step descriptions and menu labels.

## [0.8.0] - 2026-08-02

### Added

- **`/tx` commands are stamped with the bridge's boot id.** (firmware
  [#10](https://github.com/joyfulhouse/esphome-rf433-mqtt-bridge/issues/10))
  The integration reads `boot` from each bridge's retained `/info` snapshot and includes it on
  every `/tx` publish. A bridge it has no boot evidence for is refused — `CommandRejectedError`,
  air reservation released — rather than sent a command the bridge cannot validate.

  This is the controller half of firmware contract v3, which requires a bridge running
  esphome-rf433-mqtt-bridge v1.4.0 to reject a retained or replayed `/tx` structurally instead of
  merely by convention. **Deploy this integration release first: firmware ≤ 1.3.0 ignores the
  stamped `boot` field, and firmware v1.4.0 is the first release that enforces it.**

## [0.7.0] - 2026-07-28

### Breaking

- **`position_confidence` no longer reports `verified`; that value is now `anchored`.** (#31)
  Any automation, template or dashboard matching `position_confidence: verified` **stops
  matching** and must be updated to `anchored`. There is deliberately no compatibility shim
  emitting both values: a state attribute is not persisted config, and a clean break with a
  release note is more honest than a transition period in which the misleading word keeps
  working.

  The word was wrong. `verified` was set when a **local timer** reached an endpoint — nothing
  confirmed that the motor received the frame, ran for the calibrated duration, or reached its
  limit switch, and over a one-way protocol nothing can. `anchored` says exactly what the
  integration knows: a full travel was transmitted, a timer ran to completion, and no
  contradicting RF press was heard. The behaviour behind the value is unchanged, as is the
  ranking, now `unknown < suspect < assumed < anchored`.

### Fixed

- **A command cancelled after its frame was published no longer leaves a stale position.** (#28)
  `asyncio.CancelledError` bypassed both of the transmit's error handlers, so an entry reload or
  options change during a group move, a `script.turn_off`, or an automation in `mode: restart`
  moved the blind and left the cover reporting a confident, specific, wrong position. Affected
  covers now go `unknown`. Deliberately pessimistic: a cancellation that landed *before*
  publication invalidates the estimate too, because the entity cannot tell the two apart.

- **A group's position no longer averages away an unknown member, and is weighted by channel
  count.** (#32) A three-member group with one unknown reported the mean of the other two — a
  confident number describing only part of the hardware; it now reports no position at all, as
  `is_closed` already did on a mixed state. And a member covering two channels now counts twice
  as much as one covering a single channel, so the group reports the mean of the *motors* rather
  than of the *entities*.

- **`cover.set_position` on a group no longer moves half of it before failing.** (#32) Members
  that could be positioned were transmitted to and only then did an unpositionable member raise,
  leaving the group in a state nobody intended and making a retry hazardous. Every member is now
  checked before the first frame goes on air.

- **An unexpected error inside a group's position fan-out is no longer silently swallowed.**
  (#33) Results were gathered and discarded, so anything other than the expected member errors
  produced no traceback and a *successful* service call while part of the group had not moved.
  Such errors now fail the service call with their original traceback intact.

- **Live timing no longer runs on the wall clock.** (#29) Motion deadlines, ledger echo windows,
  bridge-clock projection and takeover disarm deadlines all compared `time.time()` values, so a
  single NTP correction or manual clock change could leave a cover "moving" for hours, falsely
  anchor a fractionally-moved blind, or reclassify the integration's own echo as a physical
  press. Every live decision now runs on `time.monotonic()`; wall time survives only in the
  persisted attributes, projected once onto the monotonic axis at restore.

- **A travel that "completed" only because wall time elapsed while Home Assistant was down now
  restores as `suspect`.** (#45) Restore cannot distinguish a clock step from genuine downtime,
  so the committed target position no longer carries `assumed` confidence; a genuine observed
  anchor still clears the doubt. The same rule now applies to a travel *resumed* across a
  restart: its unobserved gap means completion earns `suspect`, never `anchored`, and offline
  evidence that arrived during the gap still revokes a questioned endpoint afterwards.

- **A group frame that fails after publication now invalidates every configured leaf it
  addressed — not just the entities that happened to be alive.** (#44) Disabled leaves, leaves
  replaced mid-flight, and leaves that only load later are covered by per-cover markers that
  survive entity replacement and reload, are consumed before a stale position can restore, and
  are retired only once the corrective state write actually lands. One member's failed write no
  longer shields its siblings.

- **`cover.set_position` targeting the currently displayed position of a moving cover is now
  STOP-only.** Travel elapsed during the STOP round-trip previously turned an apparent no-op
  into a surprise corrective move in the opposite direction.

- **A group no longer claims `suspect` while it has no position at all.** Confidence qualifies
  an existing estimate; with an unknown member the group reports `unknown`, and a suspect
  member's doubt resurfaces the moment a group position is derivable again. Each leaf still
  reports its own confidence directly.

- **Learn, bounds and hygiene hardening.** (#26 #27 #30 #34–#38 #40–#43) Untabled remotes
  enrol from measured captures; truncated OEM trailers are learnable; RX validates the exact
  command value; outstanding command work is capped and pruned; entity listeners no longer leak
  on a failed restore; motion writes stop flooding the recorder; user-facing errors are
  translated; diagnostics exposes triage counters with dump-local labels; state-change fan-out
  is filtered per remote; config-entry migration rejects unknown versions; held RF captures
  get a wired TTL flush.

### Changed

- **The four oversized modules were split along their existing seams.** (#39) `models.py` →
  `config_models` / `bridge_registry` / `transport`; `state_sync.py` → `bridge_clock` /
  `command_ledger` / `state_sync`; `cover.py` → `cover` / `cover_aggregate`; `config_flow.py` →
  `config_flow` / `config_flow_schema` / `learn_session`. A pure move — every definition
  byte-identical, legacy modules keep their full import surface — with one payoff worth naming:
  importing the config models no longer drags in the MQTT transport stack.

## [0.6.0] - 2026-07-26

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

  Three boundaries hardened by adversarial review before shipping:

  - **`verified` requires an *observed* completion.** A travel that finished during Home
    Assistant's own downtime lands on its target as before, but earns no `verified` and settles
    no `suspect`: no RX listener ran while it travelled, so a press in that gap — real or phantom
    — was invisible. The same reasoning is why `verified` deliberately does **not** survive a
    restart: restoring it verbatim would overclaim across exactly the window the integration was
    blind. Covers re-earn it with their next observed completed travel. (Residual even when
    observed: a listener is deaf for roughly one slot after each capture, so a press *can* be
    missed; that risk is identical for commanded and heard travels, which is why both earn
    `verified` rather than only our own commands.)
  - **An unknown member caps its group at `assumed`.** It cannot vote on which known value wins,
    but reporting `verified` over a broken sibling would hide exactly the member an automation
    gating on this attribute needs to fix.

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
