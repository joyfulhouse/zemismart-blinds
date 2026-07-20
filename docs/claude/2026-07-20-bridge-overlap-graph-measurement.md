# Bridge overlap graph — measured 2026-07-20

Partial evidence toward open question #1 of
[the air arbitration design](2026-07-20-cross-bridge-air-arbitration-design.md):
*should all seven bridges be serialized conservatively, or is the scene skew bad
enough to justify measuring and configuring an overlap graph?*

**Answer: start with one conservative collision domain — because this evidence
cannot certify safe concurrency, not because concurrency was shown worthless.
Phase 3.3 stays on the table.**

> **This document was corrected after adversarial review.** Its first version
> concluded "the conflict graph is not worth building; drop Phase 3.3." That
> conclusion was wrong on two counts: it used the wrong graph metric (max
> independent set instead of chromatic number), and it treated two sweeps as
> sufficient to certify a non-edge. Both errors are documented below rather
> than quietly edited out. Review session `019f8183-0f27-7461-8043-6114c1188da5`.

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
> `command_id`s. A run reusing `overlap-probe-{i}` was silently rejected
> fleet-wide as QoS-1 redeliveries and transmitted **nothing** — producing a
> perfect all-zeros matrix that looks like "no bridge can hear any other."
> Probe ids must be unique per run.

### Probe representativeness — verified, no bias

This was the main threat to validity and it does not materialize. All 32
production UP/DOWN frames for the ten remotes were reconstructed and compared:

| Metric | Probe | All production UP/DOWN frames |
|---|---:|---:|
| Encoded length | 162 hex chars / 81 bytes | 162 / 81 |
| RF interval | 560,160 µs | 560,160 µs |
| UART time | 43 ms | 43 ms |
| Firmware slot | 609 ms | 609 ms |

The fixed 64-bit encoder and bucket table mean payload contents reorder pulses
without changing total duration. Carrier-high time: probe 292,320 µs vs
production range 278,720–316,800 µs — comfortably inside. No length bias.

## Result

| measure | value |
|---|---:|
| conflicting pairs, run 1 | 18/21 |
| conflicting pairs, run 2 | 18/21 |
| edges present in **both** runs (intersection) | **17** |
| edges present in **either** run (union) | **19** |
| non-conflicting in both runs | **2** |

Run-to-run unstable links: `kitchen ↔ sunroom`, `starrys-office ↔ sunroom`
(one run each).

**Non-conflicting in both runs:** `living-room ↔ sunroom`,
`master-bedroom ↔ starrys-office`.

## Graph metrics — independent set is the WRONG metric

| Graph | Edges | α (max independent set) | χ (chromatic number) |
|---|---:|---:|---:|
| Run 1 | 18 | 2 | 5 |
| Run 2 | 18 | 2 | 5 |
| Intersection | 17 | 2 | 5 |
| Union (conservative) | 19 | 2 | 5 |

α = 2 says only two bridges may transmit *simultaneously*. But the scheduling
question is how many **rounds** are needed to get everyone through, which is
the chromatic number. A five-colouring of the conservative union graph:

1. living-room + sunroom
2. master-bedroom + starrys-office
3. kaelyn
4. kitchen
5. office

With 1.218 s action trains and a 100 ms guard, all-seven last-start latency:

- fully serial: 6 × 1.318 = **7.908 s**
- five rounds: 4 × 1.318 = **5.272 s**

That is a **33% reduction**, not a marginal one. The first version of this
document reasoned from α and wrongly concluded the optimization was dead.

Implementing it is not free: the hub's worker currently awaits each command's
first `started` before taking the next (`models.py:2048`), so concurrent rounds
need scheduler changes. That cost should be weighed explicitly — but on its
merits, not dismissed via α.

## Why this still cannot certify concurrency

**1. Audibility is neither necessary nor sufficient for a collision at a blind.**
The experiment never transmitted two frames simultaneously and never observed a
blind. It measured whether bridge B can decode bridge A *in isolation*.

The earlier claim that both uncertainties "push the same way" (i.e. that decode
success is a lower bound on interference) is **false**:

- a signal too weak to decode alone can still raise the error rate of a desired
  signal near sensitivity — pushing toward *more* conflict; but
- **capture effect** means a peer that decodes perfectly alone may cause no
  failure when the desired transmitter is substantially stronger — making an
  audible edge a *false* conflict;
- AGC and preamble acquisition make the outcome timing-dependent.

So the measured graph bounds the real one in neither direction.

**2. Bridges are not blinds.** What matters for a blind served by bridge B is
the desired B→blind signal relative to the interfering A→blind signal. A→B
audibility does not determine that ratio. The blind-level graph may be
materially sparser *or* denser, and it can be target-specific and directional.

**3. Two sweeps cannot certify a non-edge.** Every cell carries 0–1
observations (`repeats: 2` did not yield two recorded captures). After zero
detections, one-sided 95% bounds are:

| trials with zero detections | true detection rate could still be as high as |
|---:|---:|
| 0/2 | 77.6% |
| 0/4 | 52.7% |

Bounding a pair under 10% at 95% needs ~29 zero-event trials; under 5%, ~59 —
and ~58/~118 after correcting across 21 pairs. Claiming a blind-level miss rate
below 1% would need roughly 300 independent collision tests. Those must be
**blind-level simultaneous-transmission** tests across varied start offsets;
more passive bridge decodes cannot validate the missing proxy.

## Scene latency — corrected

The design's ~7.8 s is **last-start** latency. An earlier revision of this
document compared it against 13.2 s, which summed all ten trains *including the
last* and omitted guards — different metrics.

Using the design's own model (609 ms slot, 1,218 ms action train, 2,436 ms
action+trailer):

| scenario | last start |
|---|---:|
| trailer command last | 11.862 s |
| trailer command among the first nine | 13.080 s |

So **~11.9–13.1 s last-start**, with the final RF train finishing ≈14.3 s.

The "ten trains" premise is workload-dependent. Coalescing applies only to
untimed non-group UP/DOWN commands (`models.py:2458`), and any multi-channel
cover is a group (`models.py:631`). The office remote has five single-channel
leaves and two groups, so a scene targeting all 16 entities emits **≥12
trains**, not 10. Ten holds for a curated one-logical-cover-per-remote scene.

## What to do

- Deploy Phase 2 with **one conservative collision domain**. Justified by
  inability to certify concurrency, not by concurrency being worthless.
- Keep **Phase 3.3 open**. χ = 5 offers ~33% last-start reduction if the
  blind-level graph supports it.
- Any future attempt to enable spatial reuse must be validated at the
  **blind level** with simultaneous transmissions and adequate sample size —
  not by re-running this passive sweep more times.
