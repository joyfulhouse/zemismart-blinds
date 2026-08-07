# Travel-time capture: measuring a shade with its own remote

Date: 2026-08-06
Status: approved design, not yet implemented
Branch: `feature/easy-setup-timer`

## Problem

Every leaf cover needs `travel_up` and `travel_down`. They are the whole basis of
the open-loop position model, and today the only way to supply them is to type
seconds into a form. `_validate_cover_input` (`config_flow_schema.py:281`)
rejects a cover whose travel fields are blank with `travel_required`, so a user
who does not already know their shade's timing is stuck guessing, or timing it
by hand with a stopwatch and typing the result.

The user already has an instrument that measures exactly this: the remote. Press
DOWN, watch the shade run to its limit, press STOP when it arrives. The interval
between those two RF frames is the travel time, and the integration can hear both
of them on any bridge.

## What we build

Leaving the travel fields blank stops being an error and becomes a request. The
flow asks which bridge to listen on, then the user runs the shade once in each
direction with the physical remote. The integration identifies which button was
pressed from the frame itself, measures the interval between the direction press
and the STOP, rounds up to whole seconds, and shows the result for confirmation
before writing it.

Two runs, either order. Nothing about the design asks the user to press a
particular button first.

## Decisions taken

These were settled before the design was written and are not open questions.

| Decision | Choice |
| --- | --- |
| Trigger | Blank travel fields, **plus** a re-measure entry in the edit-cover path |
| Choreography | Order-free autodetect, two runs |
| Identity matching | Must match this entry's own remote identity, and the cover's exact channel set |
| Rounding | `CEILING` to whole seconds, with a confirm screen before saving |

## Measuring a run

### Identifying which button was pressed

`derive_base(ref_channels, button, ref_cmd, remote_id)` validates its `button`
argument and then never uses it — the body is
`_recover_base(remote_id, normalized, ref_cmd)`, which keeps the capture's opcode
byte and inverts only the low byte. One physical frame therefore yields exactly
one channel-normalized base regardless of what action you claim it is. This is
the same property `_capture_belongs_to_this_action` already relies on to compare
captures across an action boundary.

So a heard frame is matched by computing its derived base and comparing it
against the entry's three calibrated bases (`CommandBases.up/.down/.stop`). That
is an **exact** identification, not an inference.

This is deliberately not `infer_action_button`. That function reads the opcode
byte against an empirical 10-sample table, and #26 established the table is
wrong for real remotes in the field — for one of the eleven surveyed it produced
an UP command the motor provably ignored. The Learn wizard has to tolerate that
because it runs *before* any calibration exists. Travel capture runs *after*, so
it can use the calibration and be exact. Frames whose base matches none of the
three are ignored.

A frame is accepted only when all of the following hold:

1. `(prefix, remote_id)` equals the entry's identity.
2. Decoded channels equal the cover's channel set exactly.
3. The derived base equals one of the entry's three calibrated bases.
4. `_is_own_emission(hass, frame)` is false.

Decoding uses `decode_rx_capture`, the trailer-tolerant reference decoder, for
the same reason the Learn path switched to it in #27: not every OEM remote puts
the nominal `[1, 0]` trailer on air, and the strict decoder silently rejected
those captures before any handler saw them.

### Timing off the bridge, not off Home Assistant

Each RX payload carries the bridge's own `t` (uint32 milliseconds) and `boot`.
Both frames in a run come from one bridge in one boot, so subtracting their `t`
values cancels broker latency and event-loop scheduling jitter completely.

`BridgeClock` is **not** used. That class correlates bridge time onto Home
Assistant's monotonic timeline, which is what `state_sync` needs to order
captures against commands we issued. A travel run needs only the interval
between two samples from one clock, so the raw subtraction is both simpler and
more accurate than a projection.

Guards on the subtraction:

- `boot` must be identical for both frames. A bridge that rebooted mid-run
  invalidates the measurement; discard the run and re-prompt.
- `delta = (stop.t - start.t) & 0xFFFFFFFF`, which handles uint32 wrap.
- Reject `delta` greater than half the modulus — that is what reordered
  delivery looks like, not a five-hundred-hour shade.
- If `t` or `boot` is absent or malformed, fall back to the difference of the
  two `received_at_monotonic` values.

### Surviving the repeat burst

One physical press puts eight embedded OEM frames on air inside roughly 609 ms,
and a bridge hears an unreliable subset of them — the bogus-identity air probe
measured peers hearing only about every other frame. The run state machine is
therefore edge-triggered with a debounce:

- The first frame carrying a **direction** base opens the run and stamps its
  start.
- Further frames with that same base inside `TRAVEL_BURST_WINDOW_SECONDS`
  (1.5 s, matching `state_sync._DEBOUNCE_WINDOW_SECONDS`) are ignored.
- A direction frame **outside** that window restarts the run. That is what a
  user changing their mind looks like, including reversing UP to DOWN.
- The first **STOP** frame after an open run closes it and yields the
  measurement.
- A STOP with no run open is ignored; it is the tail of something else.

The second run accepts **only** the direction that has not been measured yet.
Pressing the already-measured direction again is ignored rather than treated as
an overwrite: the screen has explicitly asked for the other direction, and
silently replacing a good measurement would make the redo option on
`cover_measure_next` meaningless. This is why `TravelRun` carries `wanted`.

Missing the true first frame of a burst costs at most one inter-frame gap of
roughly 76 ms, which disappears entirely under the ceiling.

### Keeping the sniff window open

The firmware's `start_sniff` takes the later of the current and candidate
deadlines rather than replacing it (`esphome-rf433-mqtt-bridge/rf433_rx.h:80-85`):

```c
const uint32_t candidate = now_ms + static_cast<uint32_t>(seconds) * 1000U;
if (!this->bounded_active_ ||
    static_cast<int32_t>(candidate - this->bounded_until_ms_) > 0) {
  this->bounded_until_ms_ = candidate;
}
```

So re-publishing the sniff command extends the window instead of restarting it,
and a capture longer than the contract's 60-second cap is achieved by re-arming.
The capture publishes `{"action":"sniff","seconds":30}` every
`TRAVEL_REARM_INTERVAL_SECONDS` and publishes `seconds:0` on exit, exactly as
`_async_capture` already does.

### Two deadlines, because they mean different things

- `TRAVEL_ARM_TIMEOUT_SECONDS` (120 s): no direction press heard at all. The
  user is out of range of the bridge, pressing a different remote, or on a
  virtual-remote entry that nothing physical transmits.
- `TRAVEL_RUN_TIMEOUT_SECONDS` (300 s): a direction press was heard but no STOP
  followed. The user walked away, or the STOP press was not heard.

Splitting them lets each failure screen say something true and specific instead
of one generic timeout. The run deadline also bounds the largest measurable
value at 300 s, comfortably inside both `MAX_TRAVEL_SECONDS` (3600) and the
travel selector's own 600 s maximum.

### Bridge ownership

Reuses the existing `_CAPTURE_OWNERS[(id(hass), bridge)]` guard and its
session-id and release-callback discipline, so a travel capture and a Learn
sniff can never contend for the same bridge.

## The virtual-remote gap

Strict identity matching means a virtual-remote entry can never be measured:
nothing physical transmits a synthesized identity.

`const.py:81-82` defines `MANUAL_REMOTE` and `VIRTUAL_REMOTE`, but nothing reads
them — they are dead constants. **An entry does not record whether its identity
was learned from a physical remote or synthesized**, so provenance cannot be
recovered on reconfigure. Adding a marker would mean a v4 entry migration, which
is not worth it for a nicety so soon after the v3 fixed-opcode migration.

Instead:

- During the initial wizard the flow knows in memory that `async_step_virtual`
  ran, and does not offer measurement for that cover at all.
- On reconfigure the user reaches `TRAVEL_ARM_TIMEOUT_SECONDS` and the failure
  copy names this case explicitly, alongside the more common out-of-range one.

## Flow

### Entry points

**Blank travel fields.** The `travel_required` branch in `_validate_cover_input`
becomes a route rather than an error, for `cover` (initial wizard), `cover_add`,
and `cover_edit`. Aggregate covers are untouched — `born_aggregate` already
bypasses the travel check and must not be offered measurement.

This requires removing the backfill at `config_flow.py:557-560`, which restores
stored travel values when the key is absent on edit. The edit form pre-fills
stored values through `_cover_display_values`, so a cleared field there is
deliberate, and the backfill would make "blank" unreachable on that form.

**Re-measure menu.** `cover_pick_edit` gains a menu after the cover is selected:

- `cover_edit` — edit name, channels and travel times
- `cover_measure_setup` — measure travel times with the remote

### Steps

| Step | Kind | Purpose |
| --- | --- | --- |
| `cover_edit_menu` | menu | Edit fields, or measure |
| `cover_measure_setup` | form | Bridge picker |
| `cover_measure_run` | progress | One capture task |
| `cover_measure_next` | menu | Report the direction measured, ask for the other, offer redo |
| `cover_measure_timeout` | menu | Retry, enter manually, cancel |
| `cover_measure_confirm` | form | Raw against proposed, editable, then save |

`cover_measure_setup` reuses `BridgeRegistry` and the Automatic-resolves-by-area
behavior from `_learn_setup_schema`, and reuses the `bridge_unavailable` error
key. When `_async_discover_bridges` returns `None` — flow-local MQTT discovery
failed outright, the case `learn_unavailable` handles for the Learn wizard —
measurement is not possible at all, so the flow returns to the cover form with
`measure_no_bridge` rather than offering a retry loop. Typing the times by hand
remains available there.

`cover_measure_run` uses the `async_show_progress` / `async_show_progress_done`
pattern already established by `async_step_learn_sniff`.

`cover_measure_confirm` is a **form**, not a menu — a Home Assistant config-flow
form has a single submit, so there is no second "measure again" button on it.
The raw measurements appear in the description text and the ceiling values are
pre-filled into editable travel fields:

```
Measured   down 14.31 s   up 16.08 s

  Travel down [ 15 ]   Travel up [ 17 ]
             [ Submit ]
```

Correcting a mistimed run is therefore done by typing the right value, which is
strictly better than repeating the run. A genuine redo is available one screen
earlier, on `cover_measure_next`, which offers it after each run.

The user presses STOP after *observing* arrival, so reaction time already biases
the measurement long, and the ceiling biases it long again. That is the safe
direction for an open-loop model: a slightly generous travel time means the
motor stalls against its own limit switch, where a short one leaves "fully
closed" visibly open. A measurement below `MIN_MEASURED_SECONDS` (1.0) is
rejected as too fast to be a real run.

### Where the result goes

The confirmed values return to different places depending on origin, tracked
explicitly on the flow rather than inferred:

- initial wizard — into the pending `CoverConfig`, then `cover_menu`
- `cover_add` — appended as a new row, abort `cover_added`
- `cover_edit` / re-measure — merged into the stored row, abort `cover_updated`

## Code placement

New module `travel_capture.py`, alongside `learn_session.py`, holding the run
state machine and the frame matcher as pure functions with no MQTT dependency.
The config-flow steps live in `config_flow.py`, which is already 1331 lines; the
logic worth testing directly does not go there.

### Types

```python
@dataclass(frozen=True, slots=True)
class TimedPress:
    button: str                     # "UP" | "DOWN" | "STOP"
    boot: int | None
    bridge_millis: int | None
    received_at_monotonic: float


@dataclass(frozen=True, slots=True)
class TravelMeasurement:
    direction: str                  # "UP" | "DOWN"
    measured_seconds: float
    stored_seconds: int             # ceil(measured_seconds)


@dataclass(slots=True)
class TravelRun:
    identity: RemoteIdentity        # bases required, not None
    channels: tuple[int, ...]
    wanted: frozenset[str]          # directions not yet measured
    started: TimedPress | None = None
```

Functions:

- `identify_button(identity, channels, frame) -> str | None` — decode and match
  against the calibrated bases; `None` when the frame is not this remote's, not
  these channels, or matches no base.
- `TravelRun.offer(...) -> TravelMeasurement | None` — the edge-triggered state
  machine, returning a measurement only when a run closes.
- `interval_seconds(start, stop) -> float | None` — the bridge-clock delta with
  its boot and wrap guards, falling back to monotonic.
- `stored_value(seconds) -> int` — ceiling with the floor guard.

Flow-side state:

```python
@dataclass(slots=True)
class _PendingMeasure:
    origin: Literal["wizard", "add", "edit"]
    name: str
    channels: tuple[int, ...]
    cover_id: str | None
    bridge: str | None
    measured: dict[str, TravelMeasurement]
```

### Constants

These go in `const.py`, alongside `DEFAULT_SNIFF_WINDOW_SECONDS` and the other
tuning values, not in the new module:

```python
TRAVEL_ARM_TIMEOUT_SECONDS: Final = 120.0
TRAVEL_RUN_TIMEOUT_SECONDS: Final = 300.0
TRAVEL_REARM_INTERVAL_SECONDS: Final = 15.0
TRAVEL_BURST_WINDOW_SECONDS: Final = 1.5
MIN_MEASURED_SECONDS: Final = 1.0
```

The re-arm publishes the existing `DEFAULT_SNIFF_WINDOW_SECONDS` (30) as its
`seconds` value rather than defining a second constant with the same meaning.

### Strings

New `strings.json` and `translations/en.json` entries for each step above, the
`measuring` progress action, and new error keys `measure_too_fast`,
`measure_no_press`, `measure_incomplete`, and `measure_no_bridge`.

## Testing

The state machine needs no MQTT. Tests synthesize RX payloads through the codec
and feed them to `identify_button` and `TravelRun.offer`, asserting direction and
interval. Frames are **synthesized, never pasted from house captures** — the
2026-07-24 fixture re-key established that captured house frames must not be
pinned verbatim into tests.

Coverage:

- A burst of repeats neither restarts a run nor closes it early.
- A direction frame outside the burst window restarts the run.
- A reversal (DOWN then UP with no STOP) restarts on UP.
- On the second run, a press of the already-measured direction is ignored and
  the run stays open for the direction still wanted.
- A `boot` change between the two frames discards the run.
- A uint32 wrap of `t` across a run still yields the correct delta.
- A backwards delta beyond half the modulus is rejected.
- Foreign remotes, foreign channel sets, and own emissions are ignored.
- Frames matching no calibrated base are ignored.
- Ceiling boundaries: exactly 14.0 stores 14, and 14.0001 stores 15.
- Below `MIN_MEASURED_SECONDS` is rejected.
- Flow: blank travel routes to the bridge picker; aggregates do not; confirm
  writes the row for each of the three origins; the arming and run timeouts each
  reach their own screen.

## Known behavior worth documenting

On re-measure the entry is loaded, so `state_sync` sees these presses as genuine
physical presses and drives the runtime position model using the **old** travel
time while the measurement is in progress. This is harmless — the entry reloads
on save and the model resets — but the shade's Home Assistant position will look
wrong for the duration. The re-measure screen should say so.

## Out of scope

- **Self-driven measurement**, where the integration transmits DOWN itself and
  the user clicks an "arrived" button in the UI. It would cover virtual remotes
  and users without a remote in hand, but it cannot work during the initial add
  wizard, because the entry does not exist yet and nothing can transmit. That is
  the primary case, so the RF-press design is the one that ships.
- **Storing identity provenance** to detect virtual remotes on reconfigure. See
  the virtual-remote gap above.
- **Measuring aggregate covers.** They carry no travel by design.
