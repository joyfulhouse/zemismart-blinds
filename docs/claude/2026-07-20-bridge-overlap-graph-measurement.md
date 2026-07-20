# Bridge overlap graph — measured 2026-07-20

Empirical answer to open question #1 of
[the air arbitration design](2026-07-20-cross-bridge-air-arbitration-design.md):
*should all seven bridges be serialized conservatively, or is the scene skew bad
enough to justify measuring and configuring an overlap graph?*

**Answer: serialize conservatively. The overlap graph is not worth building.**

## Method

Each online bridge transmitted, in turn, an inert probe frame while every
bridge's `/rx` sniffer listened. The probe is an AOK-shaped **STOP** frame for
bogus remote `a1b2c3:42` channel 1 — no house remote uses an `a1` prefix (all
ten are `5c…`), and STOP cannot move a stationary blind.

Captures are matched by **decoding** each `/rx` frame and comparing the remote
identity. A capture is re-encoded by the B1 sniffer, so hex substring matching
against the transmitted frame does not work.

Script: session scratchpad `overlap_graph.py`. Two valid runs.

> **Gotcha worth remembering:** the firmware replay ring remembers recent
> `command_id`s. A second run reusing `overlap-probe-{i}` was silently rejected
> fleet-wide as QoS-1 redeliveries and transmitted **nothing** — producing a
> perfect all-zeros matrix that looks like "no bridge can hear any other."
> Probe ids must be unique per run.

## Result

18 of 21 bridge pairs conflict in **both** runs (86%).

| measure | value |
|---|---:|
| conflicting pairs, run 1 | 18/21 |
| conflicting pairs, run 2 | 18/21 |
| edges present in both runs | 17 |
| edges appearing in only one run | 2 |

Marginal, run-to-run unstable links: `starrys-office ↔ sunroom`,
`sunroom ↔ kitchen`.

**Non-conflicting in both runs — only two pairs:**

- `living-room ↔ sunroom`
- `master-bedroom ↔ starrys-office`

**Maximum set that may transmit concurrently: 2 bridges.** The two isolated
pairs do not combine into any larger independent set.

## Why this kills the conflict-graph optimization

A conflict graph buys concurrency proportional to the size of its independent
sets. At a maximum independent set of 2 — and only two stable pairs, neither
guaranteed to be the pair a given scene needs — the best case halves
serialization for a minority of command combinations, in exchange for
per-deployment RF calibration that must be re-measured whenever a bridge moves.

That is not a good trade. Phase 3.3 of the design should be dropped, and the
single-collision-domain default retained.

## The measurement understates conflict

Two reasons the true collision domain is at least this connected, probably more:

1. **Decode threshold is stricter than interference threshold.** A bridge that
   cannot *demodulate* a peer may still *degrade* that peer's reception at a
   receiver. Measured audibility is a lower bound on interference.
2. **Bridges are not blinds.** This is bridge-to-bridge audibility, a proxy for
   what actually matters — whether a *blind* can hear two bridges. Blinds sit at
   different locations than the bridges serving them.

Both push the same way: treat the house as one collision domain.

## Correction to the design's latency estimate

The design computes scene latency from **7 bridges**. Arbitration serializes
**command trains**, and the house has **10 distinct remotes**
(`5cad7c:da` alone backs 7 covers, the other nine back one each). With
per-remote coalescing, a whole-house scene is ~10 trains, not 7.

Exactly one remote (`5cad7c:da`, office) is calibrated with a `base_trailer`;
the other nine are action-only.

| | design estimate | measured basis |
|---|---:|---:|
| whole-house close | ~7.8 s | **~13.2 s** (9 × ~1.2 s + 1 × ~2.4 s) |

The real number is worse than specced. It should be stated as ~13 s before
anyone signs off on the trade.

## What this means for the recommendation

- Keep the single conservative collision domain; drop the conflict graph.
- The lever for latency is **reducing airtime per scene**, not parallelizing:
  fewer trains (coalescing already helps), and trailers only where a remote
  genuinely needs one.
- The cost lands almost entirely on **unattended** scene automations. A single
  interactive tap still publishes immediately — the design's first-command
  fast path is unaffected. "Blinds close over ~13 s" is a different and
  arguably better failure mode than "blinds close at once and one to three
  silently miss."
