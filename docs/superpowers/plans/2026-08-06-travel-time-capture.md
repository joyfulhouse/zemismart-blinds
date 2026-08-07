# Travel-Time Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user leave a cover's travel times blank and measure them by running the shade once in each direction with its own remote.

**Architecture:** A new `travel_capture.py` holds a pure, MQTT-free state machine: it identifies which button a captured RF frame is by comparing the frame's derived base against the remote's *calibrated* bases, opens a run on a direction press, and closes it on the next STOP. The config flow wraps that in a bounded MQTT sniff — re-armed periodically because the firmware extends rather than restarts its window — and writes `CEILING(seconds)` after a confirmation screen.

**Tech Stack:** Python 3.13, Home Assistant custom integration, `uv` for all Python operations, pytest, ruff, mypy --strict.

**Spec:** `docs/superpowers/specs/2026-08-06-travel-time-capture-design.md`

## Global Constraints

- Package manager is `uv`. Never `pip`. Test with `uv run pytest`, lint with `uv run ruff check --fix && uv run ruff format`, typecheck with `uv run mypy --strict .`.
- Never disable a linter rule. No `# noqa`, no `# type: ignore`. Fix the root cause.
- `except (A, B):` at py314 gets rewritten back to bare form by `ruff format`. Write two separate `except` blocks instead.
- Test frames are **synthesized** through the codec (`make_payload` → `encode_b0` → `b0_to_b1`). Never paste a real house capture into a fixture.
- Use `time.monotonic()` for timing. Never `asyncio.get_event_loop()`.
- Every new public function and dataclass needs a docstring. Comments explain constraints, not narration.
- Pre-commit gate for every task: `uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest`.
- Working directory for all commands: the repo root of this worktree.

## File Structure

| File | Responsibility |
| --- | --- |
| `custom_components/zemismart_blinds/travel_capture.py` | **New.** Frame→button matcher, interval math, run state machine. No MQTT, no Home Assistant. |
| `custom_components/zemismart_blinds/const.py` | Modify. New timing constants and the two RX payload field names. |
| `custom_components/zemismart_blinds/config_flow.py` | Modify. Capture driver and six new flow steps. |
| `custom_components/zemismart_blinds/config_flow_schema.py` | Modify. Blank-travel routing signal, measure-confirm schema. |
| `custom_components/zemismart_blinds/strings.json` | Modify. Step copy and error keys. |
| `custom_components/zemismart_blinds/translations/en.json` | Modify. Mirror of `strings.json`. |
| `tests/test_travel_capture.py` | **New.** Pure unit tests for the state machine. |
| `tests/test_config_flow.py` | Modify. Flow-level tests for the new steps. |

`travel_capture.py` stays free of Home Assistant imports so its tests need no `hass` fixture and run instantly. `config_flow.py` is already 1331 lines; only the steps go there.

---

### Task 1: Frame-to-button matcher

**Files:**
- Create: `custom_components/zemismart_blinds/travel_capture.py`
- Test: `tests/test_travel_capture.py`

**Interfaces:**
- Consumes: `decode_rx_capture(hexstr) -> DecodedFrame` and `derive_base(ref_channels, button, ref_cmd, remote_id) -> int` from `.codec`; `RemoteIdentity` (fields `prefix`, `remote_id`, `bases`) and `CommandBases.base(button) -> int` from `.config_models`.
- Produces: `DIRECTIONS: tuple[str, str]`, `BUTTONS: tuple[str, str, str]`, `identify_button(identity, channels, frame) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_travel_capture.py`:

```python
"""Unit tests for travel-time capture: matching, timing, and the run machine."""

from __future__ import annotations

from custom_components.zemismart_blinds.codec import (
    CommandBases,
    encode_b0,
    infer_action_button,
    make_payload,
)
from custom_components.zemismart_blinds.config_models import RemoteIdentity
from custom_components.zemismart_blinds.travel_capture import identify_button

from .synthetic import (
    TEST_BASES,
    TEST_PREFIX,
    TEST_REMOTE_ID,
    UNTABLED_BASES,
    UNTABLED_PREFIX,
    UNTABLED_REMOTE_ID,
)


def b1_frame(
    prefix: int,
    remote_id: int,
    channels: tuple[int, ...],
    button: str,
    bases: CommandBases,
) -> str:
    """Synthesize one bridge RX capture for a press we choose."""
    body = encode_b0(make_payload(prefix, remote_id, channels, button, bases=bases))[6:-2]
    return f"AAB1{body[:2]}{body[4:]}3855"


def test_identify_button_matches_each_calibrated_base() -> None:
    """Every action of a calibrated remote is identified from its own frame."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    for button in ("UP", "DOWN", "STOP"):
        frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), button, TEST_BASES)
        assert identify_button(identity, (1, 2), frame) == button


def test_identify_button_works_where_opcode_inference_fails() -> None:
    """The untabled remote from #26 is identified exactly.

    `infer_action_button` reads the opcode byte against a 10-sample empirical
    table, and this remote's F3/F2/F2 opcodes are outside it -- gating on that
    table silently dropped every press of a remote that was transmitting
    perfectly. Matching against the remote's OWN calibrated bases has no table
    to be wrong about, which is the whole reason travel capture can be exact
    where the Learn wizard cannot.
    """
    identity = RemoteIdentity(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, UNTABLED_BASES)
    for button in ("UP", "DOWN", "STOP"):
        payload = make_payload(
            UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1,), button, bases=UNTABLED_BASES
        )
        assert infer_action_button((1,), payload & 0xFFFF) is None, "fixture must be untabled"
        frame = b1_frame(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1,), button, UNTABLED_BASES)
        assert identify_button(identity, (1,), frame) == button


def test_identify_button_rejects_another_remote() -> None:
    """A press on a different remote is not this cover's run."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    frame = b1_frame(UNTABLED_PREFIX, UNTABLED_REMOTE_ID, (1, 2), "UP", UNTABLED_BASES)
    assert identify_button(identity, (1, 2), frame) is None


def test_identify_button_rejects_other_channels() -> None:
    """A press on the same remote but a different channel set drives another blind."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (3,), "UP", TEST_BASES)
    assert identify_button(identity, (1, 2), frame) is None


def test_identify_button_rejects_the_oem_trailer_burst() -> None:
    """The trailer frame that follows UP and DOWN matches no action base.

    A real remote emits it on every direction press, so treating an unmatched
    base as an action would close a run the instant it opened.
    """
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "TRAILER", TEST_BASES)
    assert identify_button(identity, (1, 2), frame) is None


def test_identify_button_rejects_undecodable_input() -> None:
    """Garbage on the RX topic is ignored rather than raising."""
    identity = RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES)
    assert identify_button(identity, (1, 2), "not-a-frame") is None


def test_identify_button_requires_a_calibration() -> None:
    """An uncalibrated identity can match nothing."""
    identity = RemoteIdentity(0x010203, 0x04)
    assert identity.bases is None, "fixture must be uncalibrated"
    frame = b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), "UP", TEST_BASES)
    assert identify_button(identity, (1, 2), frame) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_travel_capture.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.zemismart_blinds.travel_capture'`

- [ ] **Step 3: Write the implementation**

Create `custom_components/zemismart_blinds/travel_capture.py`:

```python
"""Measuring one cover's travel time from its own remote's presses.

The user runs the shade to a limit and presses STOP on arrival. The interval
between the direction frame and the STOP frame is the travel time. Everything
here is pure: no MQTT, no Home Assistant, so the state machine is testable
directly from payload dictionaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .codec import decode_rx_capture, derive_base

if TYPE_CHECKING:
    from .config_models import RemoteIdentity

__all__ = [
    "BUTTONS",
    "DIRECTIONS",
    "identify_button",
]

DIRECTIONS: Final = ("UP", "DOWN")
BUTTONS: Final = ("UP", "DOWN", "STOP")

_DECODE_ERRORS: Final = (KeyError, TypeError, ValueError)


def identify_button(
    identity: RemoteIdentity,
    channels: tuple[int, ...],
    frame: str,
) -> str | None:
    """Return which calibrated button a captured frame is, or None.

    Exact, not inferred. ``derive_base`` validates its ``button`` argument and
    then never uses it -- the recovery keeps the capture's opcode byte and
    inverts only the low byte -- so one physical frame yields one
    channel-normalized base whatever action is claimed, and that base can be
    compared against this remote's own calibration.

    Deliberately NOT ``infer_action_button``: that reads the opcode byte
    against a 10-sample empirical table which #26 proved wrong for a real
    remote in the field. The Learn wizard tolerates it because it runs BEFORE
    any calibration exists. This runs after, so it can be exact.
    """
    bases = identity.bases
    if bases is None:
        return None
    try:
        decoded = decode_rx_capture(frame)
    except _DECODE_ERRORS:
        return None
    if (decoded["prefix"], decoded["remote_id"]) != (identity.prefix, identity.remote_id):
        return None
    if tuple(decoded["chans"]) != channels:
        return None
    try:
        base = derive_base(decoded["chans"], "UP", decoded["cmd"], decoded["remote_id"])
    except _DECODE_ERRORS:
        return None
    for button in BUTTONS:
        if base == bases.base(button):
            return button
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_travel_capture.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Run the full gate**

Run: `uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/zemismart_blinds/travel_capture.py tests/test_travel_capture.py
git commit -m "feat: identify a remote's button from its calibrated bases

Travel capture runs after calibration exists, so it can compare a frame's
derived base against the remote's own bases instead of inferring the action
from the opcode table #26 showed to be wrong for real remotes."
```

---

### Task 2: Interval and rounding

**Files:**
- Modify: `custom_components/zemismart_blinds/travel_capture.py`
- Modify: `custom_components/zemismart_blinds/const.py`
- Test: `tests/test_travel_capture.py`

**Interfaces:**
- Consumes: `MAX_TRAVEL_SECONDS` from `.config_models`.
- Produces: `TimedPress(button, boot, bridge_millis, received_at_monotonic)`, `TravelMeasurement(direction, measured_seconds, stored_seconds)`, `interval_seconds(start, stop) -> float | None`, `stored_value(seconds) -> int | None`.

- [ ] **Step 1: Add the constants**

In `custom_components/zemismart_blinds/const.py`, after `MAX_SNIFF_WINDOW_SECONDS`:

```python
# Travel-time capture. The arming deadline is how long we wait to hear any
# direction press at all; the run deadline is how long a started run may take
# to reach its STOP. Split because they mean different things to the user and
# each gets its own failure copy. The run deadline also bounds the largest
# measurable value, which must stay inside MAX_TRAVEL_SECONDS.
TRAVEL_ARM_TIMEOUT_SECONDS: Final = 120.0
TRAVEL_RUN_TIMEOUT_SECONDS: Final = 300.0
# The firmware's start_sniff takes the LATER of its current and candidate
# deadlines, so re-publishing extends the bounded window instead of restarting
# it. That is the only way to measure a run longer than the contract's 60s cap.
TRAVEL_REARM_INTERVAL_SECONDS: Final = 15.0
# One press puts 8 embedded OEM frames on air across ~609ms and the bridge
# hears an unreliable subset. Matches state_sync's press debounce.
TRAVEL_BURST_WINDOW_SECONDS: Final = 1.5
MIN_MEASURED_SECONDS: Final = 1.0
```

And beside `MQTT_RX_FIELD_FRAME`:

```python
MQTT_RX_FIELD_T: Final = "t"
MQTT_RX_FIELD_BOOT: Final = "boot"
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_travel_capture.py`:

```python
def press(
    button: str,
    *,
    boot: int | None = 7,
    bridge_millis: int | None = 0,
    monotonic: float = 0.0,
) -> TimedPress:
    """Build one accepted press with explicit clocks."""
    return TimedPress(
        button=button,
        boot=boot,
        bridge_millis=bridge_millis,
        received_at_monotonic=monotonic,
    )


def test_interval_uses_the_bridge_clock() -> None:
    """The bridge's own millis cancel broker and event-loop jitter."""
    start = press("DOWN", bridge_millis=1_000, monotonic=100.0)
    stop = press("STOP", bridge_millis=15_310, monotonic=140.0)
    assert interval_seconds(start, stop) == 14.31


def test_interval_survives_a_uint32_wrap() -> None:
    """A run that straddles the bridge's 32-bit millisecond rollover."""
    start = press("UP", bridge_millis=0xFFFFF000)
    stop = press("STOP", bridge_millis=0x00000AC8)
    assert interval_seconds(start, stop) == 6.856


def test_interval_rejects_a_backwards_delta() -> None:
    """Reordered delivery, or a bridge that rebooted its clock to near zero."""
    start = press("UP", bridge_millis=20_000)
    stop = press("STOP", bridge_millis=1_000)
    assert interval_seconds(start, stop) is None


def test_interval_rejects_a_boot_change() -> None:
    """A bridge that restarted mid-run cannot have timed it."""
    start = press("UP", boot=7, bridge_millis=1_000)
    stop = press("STOP", boot=8, bridge_millis=15_000)
    assert interval_seconds(start, stop) is None


def test_interval_falls_back_to_monotonic() -> None:
    """Older firmware omits `t`; the receive times still bound the run."""
    start = press("DOWN", bridge_millis=None, monotonic=100.0)
    stop = press("STOP", bridge_millis=None, monotonic=114.5)
    assert interval_seconds(start, stop) == 14.5


def test_stored_value_rounds_up() -> None:
    """Ceiling errs long, which is the safe direction for an open-loop model."""
    assert stored_value(14.0) == 14
    assert stored_value(14.0001) == 15
    assert stored_value(16.08) == 17


def test_stored_value_rejects_an_impossibly_fast_run() -> None:
    """A double-tap is not a shade running to its limit."""
    assert stored_value(0.5) is None


def test_stored_value_rejects_beyond_the_storable_maximum() -> None:
    """A value CoverConfig would refuse must not reach it."""
    assert stored_value(float(MAX_TRAVEL_SECONDS) + 1.0) is None
```

Extend the imports at the top of the file:

```python
from custom_components.zemismart_blinds.config_models import MAX_TRAVEL_SECONDS, RemoteIdentity
from custom_components.zemismart_blinds.travel_capture import (
    TimedPress,
    identify_button,
    interval_seconds,
    stored_value,
)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_travel_capture.py -v`
Expected: FAIL — `ImportError: cannot import name 'TimedPress'`

- [ ] **Step 4: Write the implementation**

Add to `travel_capture.py`. Extend the imports and `__all__` first:

```python
import math
from dataclasses import dataclass

from .config_models import MAX_TRAVEL_SECONDS
from .const import MIN_MEASURED_SECONDS
```

```python
__all__ = [
    "BUTTONS",
    "DIRECTIONS",
    "TimedPress",
    "TravelMeasurement",
    "identify_button",
    "interval_seconds",
    "stored_value",
]

_UINT32_MODULUS: Final = 1 << 32
_UINT32_HALF_RANGE: Final = _UINT32_MODULUS // 2
_MILLISECONDS_PER_SECOND: Final = 1_000.0
```

```python
@dataclass(frozen=True, slots=True)
class TimedPress:
    """One accepted press: which button, and when the bridge heard it."""

    button: str
    boot: int | None
    bridge_millis: int | None
    received_at_monotonic: float


@dataclass(frozen=True, slots=True)
class TravelMeasurement:
    """One completed direction run, raw and as it will be stored."""

    direction: str
    measured_seconds: float
    stored_seconds: int


def interval_seconds(start: TimedPress, stop: TimedPress) -> float | None:
    """Return the run's duration, preferring the bridge's own clock.

    Both frames come from one bridge over one MQTT path, so subtracting their
    ``t`` values cancels broker and event-loop jitter outright. This is NOT a
    ``BridgeClock`` projection: that class places events on Home Assistant's
    timeline, and a run needs only an interval on the bridge's own.

    A bridge that rebooted mid-run restarted ``t`` near zero, which reads as a
    backwards delta and is rejected by the half-modulus guard even when the
    payload carries no ``boot`` to compare.
    """
    if start.bridge_millis is not None and stop.bridge_millis is not None:
        if start.boot is not None and stop.boot is not None and start.boot != stop.boot:
            return None
        delta = (stop.bridge_millis - start.bridge_millis) % _UINT32_MODULUS
        if delta >= _UINT32_HALF_RANGE:
            return None
        return delta / _MILLISECONDS_PER_SECOND
    elapsed = stop.received_at_monotonic - start.received_at_monotonic
    if not math.isfinite(elapsed) or elapsed < 0:
        return None
    return elapsed


def stored_value(seconds: float) -> int | None:
    """Round one measured interval up to the whole seconds we store.

    Ceiling errs long, and the user's reaction on the STOP press already errs
    long. For an open-loop model that is the safe direction: a generous travel
    time stalls the motor against its own limit switch, where a short one
    leaves "fully closed" visibly open.
    """
    if not math.isfinite(seconds) or seconds < MIN_MEASURED_SECONDS:
        return None
    value = math.ceil(seconds)
    if value > MAX_TRAVEL_SECONDS:
        return None
    return value
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_travel_capture.py -v`
Expected: PASS, 15 tests.

- [ ] **Step 6: Run the full gate and commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest
git add custom_components/zemismart_blinds/travel_capture.py \
        custom_components/zemismart_blinds/const.py tests/test_travel_capture.py
git commit -m "feat: measure a travel run on the bridge's own clock

Subtracting two t values from one bridge cancels broker jitter, and the
half-modulus guard rejects both reordered delivery and a mid-run reboot.
CEILING errs long, which stalls the motor at its limit rather than leaving
a close visibly short."
```

---

### Task 3: The run state machine

**Files:**
- Modify: `custom_components/zemismart_blinds/travel_capture.py`
- Test: `tests/test_travel_capture.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 2.
- Produces: `TravelRun(identity, channels, wanted, started=None)` with `offer_payload(payload: Mapping[str, object], received_at_monotonic: float) -> TravelMeasurement | None` and the public attribute `started: TimedPress | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_travel_capture.py`:

```python
def run_for(wanted: tuple[str, ...] = ("UP", "DOWN")) -> TravelRun:
    """Build one run against the calibrated test remote on channels 1 and 2."""
    return TravelRun(
        identity=RemoteIdentity(TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES),
        channels=(1, 2),
        wanted=frozenset(wanted),
    )


def rx(button: str, millis: int) -> dict[str, object]:
    """Build one RX payload the bridge would publish for a press."""
    return {
        "frame": b1_frame(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), button, TEST_BASES),
        "t": millis,
        "boot": 7,
    }


def test_a_direction_then_stop_yields_a_measurement() -> None:
    """The happy path: press DOWN, watch it arrive, press STOP."""
    run = run_for()
    assert run.offer_payload(rx("DOWN", 1_000), 100.0) is None
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31)
    assert measurement is not None
    assert measurement.direction == "DOWN"
    assert measurement.measured_seconds == 14.31
    assert measurement.stored_seconds == 15


def test_burst_repeats_neither_restart_nor_close_the_run() -> None:
    """One press is 8 frames across ~609ms; the run starts once, at the first.

    The bridge hears an unreliable subset of a burst, so a later copy must not
    re-stamp the start -- that would silently shorten every measurement by
    however much of the burst was heard.
    """
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    for index in range(1, 8):
        assert run.offer_payload(rx("DOWN", 1_000 + index * 76), 100.0 + index * 0.076) is None
    measurement = run.offer_payload(rx("STOP", 15_310), 114.31)
    assert measurement is not None
    assert measurement.measured_seconds == 14.31, "the FIRST frame must stamp the start"


def test_a_later_press_restarts_the_run() -> None:
    """A direction press outside the burst window is the user starting over."""
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    run.offer_payload(rx("DOWN", 5_000), 104.0)
    measurement = run.offer_payload(rx("STOP", 19_000), 118.0)
    assert measurement is not None
    assert measurement.measured_seconds == 14.0


def test_a_reversal_restarts_on_the_new_direction() -> None:
    """DOWN then UP with no STOP between is a user changing their mind."""
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    run.offer_payload(rx("UP", 4_000), 103.0)
    measurement = run.offer_payload(rx("STOP", 20_000), 119.0)
    assert measurement is not None
    assert measurement.direction == "UP"
    assert measurement.stored_seconds == 16


def test_a_stop_with_no_open_run_is_ignored() -> None:
    """A stray STOP is the tail of something else, not a zero-length run."""
    run = run_for()
    assert run.offer_payload(rx("STOP", 1_000), 100.0) is None


def test_the_second_run_ignores_the_direction_already_measured() -> None:
    """Only the direction still wanted may open the second run.

    The screen has explicitly asked for the other one, and silently overwriting
    a good measurement would make the redo menu option meaningless.
    """
    run = run_for(wanted=("UP",))
    assert run.offer_payload(rx("DOWN", 1_000), 100.0) is None
    assert run.started is None
    assert run.offer_payload(rx("STOP", 15_000), 114.0) is None
    run.offer_payload(rx("UP", 20_000), 119.0)
    measurement = run.offer_payload(rx("STOP", 36_000), 135.0)
    assert measurement is not None
    assert measurement.direction == "UP"


def test_a_run_too_fast_to_be_real_yields_nothing() -> None:
    """A double-tap does not become a half-second travel time."""
    run = run_for()
    run.offer_payload(rx("DOWN", 1_000), 100.0)
    assert run.offer_payload(rx("STOP", 1_400), 100.4) is None


def test_a_malformed_payload_is_ignored() -> None:
    """Missing and mistyped fields never raise out of the handler."""
    run = run_for()
    assert run.offer_payload({}, 100.0) is None
    assert run.offer_payload({"frame": 42}, 100.0) is None
    assert run.offer_payload({"frame": "not-a-frame", "t": 1}, 100.0) is None


def test_a_boolean_timestamp_is_not_a_uint32() -> None:
    """`True` is an int in Python; it is not a bridge timestamp."""
    run = run_for()
    run.offer_payload({**rx("DOWN", 1_000), "t": True}, 100.0)
    measurement = run.offer_payload({**rx("STOP", 15_000), "t": True}, 114.5)
    assert measurement is not None
    assert measurement.measured_seconds == 14.5, "must fall back to monotonic"
```

Extend the `travel_capture` import to add `TravelMeasurement` and `TravelRun`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_travel_capture.py -v`
Expected: FAIL — `ImportError: cannot import name 'TravelRun'`

- [ ] **Step 3: Write the implementation**

Add to `travel_capture.py`. Extend the imports:

```python
from collections.abc import Mapping

from .const import (
    MIN_MEASURED_SECONDS,
    MQTT_RX_FIELD_BOOT,
    MQTT_RX_FIELD_FRAME,
    MQTT_RX_FIELD_T,
    TRAVEL_BURST_WINDOW_SECONDS,
)
```

Add `"TravelRun"` to `__all__`, then:

```python
def _uint32(value: object) -> int | None:
    """Return a real uint32, rejecting booleans and coercions."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value < _UINT32_MODULUS:
        return None
    return value


@dataclass(slots=True)
class TravelRun:
    """One cover's measurement session for one direction.

    Edge-triggered: the first direction frame opens a run, the first STOP after
    it closes one. A fresh run is constructed per direction, so ``wanted`` is
    set once and never mutated.
    """

    identity: RemoteIdentity
    channels: tuple[int, ...]
    wanted: frozenset[str]
    started: TimedPress | None = None

    def offer_payload(
        self,
        payload: Mapping[str, object],
        received_at_monotonic: float,
    ) -> TravelMeasurement | None:
        """Feed one RX payload in; return a measurement when a run closes."""
        frame = payload.get(MQTT_RX_FIELD_FRAME)
        if not isinstance(frame, str):
            return None
        button = identify_button(self.identity, self.channels, frame)
        if button is None:
            return None
        press = TimedPress(
            button=button,
            boot=_uint32(payload.get(MQTT_RX_FIELD_BOOT)),
            bridge_millis=_uint32(payload.get(MQTT_RX_FIELD_T)),
            received_at_monotonic=received_at_monotonic,
        )
        if button in DIRECTIONS:
            self._open(press)
            return None
        return self._close(press)

    def _open(self, press: TimedPress) -> None:
        """Start a run, ignoring the repeats of the burst that already did."""
        if press.button not in self.wanted:
            return
        started = self.started
        if (
            started is not None
            and started.button == press.button
            and press.received_at_monotonic - started.received_at_monotonic
            <= TRAVEL_BURST_WINDOW_SECONDS
        ):
            return
        self.started = press

    def _close(self, press: TimedPress) -> TravelMeasurement | None:
        """Resolve a STOP against the open run, if there is one."""
        started = self.started
        if started is None:
            return None
        self.started = None
        elapsed = interval_seconds(started, press)
        if elapsed is None:
            return None
        stored = stored_value(elapsed)
        if stored is None:
            return None
        return TravelMeasurement(
            direction=started.button,
            measured_seconds=elapsed,
            stored_seconds=stored,
        )
```

`RemoteIdentity` must move out of the `TYPE_CHECKING` block into a runtime import, because it is now a dataclass field annotation evaluated by `@dataclass(slots=True)`:

```python
from .config_models import MAX_TRAVEL_SECONDS, RemoteIdentity
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_travel_capture.py -v`
Expected: PASS, 24 tests.

- [ ] **Step 5: Run the full gate and commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest
git add custom_components/zemismart_blinds/travel_capture.py tests/test_travel_capture.py
git commit -m "feat: edge-triggered run machine for travel measurement

The first direction frame stamps the start and the burst's remaining seven
frames are ignored, so a partially-heard burst cannot silently shorten the
measurement. A press outside the burst window restarts the run."
```

---

### Task 4: The MQTT capture driver

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow.py`
- Test: covered by the flow tests in Task 5.

**Interfaces:**
- Consumes: `TravelRun`, `TravelMeasurement` from `.travel_capture`; `_async_subscribe_ready`, `_payload_text` from `.learn_session`; `_CAPTURE_OWNERS`, `_release_capture_owner`, `_is_own_emission`, `_MQTT_BOOTSTRAP_TIMEOUT_SECONDS` already in `config_flow.py`.
- Produces: `_PendingMeasure` dataclass, `_handle_travel_message` callback, `ZemismartBlindsConfigFlow._async_capture_travel(session_id, wanted) -> TravelMeasurement | str`.

- [ ] **Step 1: Add the pending-measurement record**

In `config_flow.py`, near `_LearnCapture`'s imports, add to the module imports:

```python
from .const import (
    MQTT_RX_FIELD_BOOT,
    MQTT_RX_FIELD_T,
    TRAVEL_ARM_TIMEOUT_SECONDS,
    TRAVEL_REARM_INTERVAL_SECONDS,
    TRAVEL_RUN_TIMEOUT_SECONDS,
)
from .travel_capture import DIRECTIONS, TravelMeasurement, TravelRun
```

and the record itself:

```python
@dataclass(slots=True)
class _PendingMeasure:
    """Where a travel measurement came from and where its result must go."""

    origin: Literal["wizard", "add", "edit"]
    name: str
    channels: tuple[int, ...]
    cover_id: str | None = None
    bridge: str | None = None
    measured: dict[str, TravelMeasurement] = field(default_factory=dict)

    @property
    def wanted(self) -> frozenset[str]:
        """Return the directions still to be measured."""
        return frozenset(DIRECTIONS) - frozenset(self.measured)
```

- [ ] **Step 2: Add the message handler**

```python
@callback
def _handle_travel_message(
    flow: ZemismartBlindsConfigFlow,
    session_id: str,
    expected_topic: str,
    run: TravelRun,
    armed: asyncio.Event,
    future: asyncio.Future[TravelMeasurement],
    message: ReceiveMessage,
) -> None:
    """Offer one received frame to the open travel run."""
    if (
        flow._sniff_session_id != session_id
        or future.done()
        or message.retain
        or message.topic != expected_topic
    ):
        return
    try:
        text = _payload_text(message.payload)
        decoded_payload: object = json.loads(text)
    except _PAYLOAD_ERRORS:
        return
    if not isinstance(decoded_payload, Mapping):
        return
    frame = decoded_payload.get(MQTT_RX_FIELD_FRAME)
    # Checked before the run sees it: an automation driving this very cover
    # mid-measurement echoes back off the sniffing bridge looking exactly like
    # a human's press.
    if isinstance(frame, str) and _is_own_emission(flow.hass, frame):
        return
    measurement = run.offer_payload(decoded_payload, time.monotonic())
    if run.started is not None:
        armed.set()
    if measurement is not None:
        future.set_result(measurement)
```

Add `import time` to the module imports if absent.

- [ ] **Step 3: Add the re-arm loop and the two-deadline wait**

```python
async def _async_hold_sniff_open(hass: HomeAssistant, command_topic: str) -> None:
    """Keep one bridge's bounded sniff window open for a whole run.

    The firmware's ``start_sniff`` takes the LATER of its current and candidate
    deadlines rather than replacing it, so re-publishing extends the window.
    That is the only way to measure a run longer than the command contract's
    60-second cap.
    """
    from homeassistant.components import mqtt

    while True:
        await mqtt.async_publish(
            hass,
            command_topic,
            json.dumps(
                {
                    MQTT_CMD_FIELD_ACTION: MQTT_CMD_ACTION_SNIFF,
                    MQTT_CMD_FIELD_SECONDS: DEFAULT_SNIFF_WINDOW_SECONDS,
                },
                separators=(",", ":"),
            ),
            qos=1,
            retain=False,
        )
        await asyncio.sleep(TRAVEL_REARM_INTERVAL_SECONDS)


async def _async_await_measurement(
    armed: asyncio.Event,
    future: asyncio.Future[TravelMeasurement],
) -> TravelMeasurement | str:
    """Wait out the arming deadline, then the run deadline.

    Two deadlines because they are two different user situations: nothing was
    heard at all, or a run started and never finished. Each gets its own copy.
    """
    try:
        async with asyncio.timeout(TRAVEL_ARM_TIMEOUT_SECONDS):
            await armed.wait()
    except TimeoutError:
        return "no_press"
    try:
        async with asyncio.timeout(TRAVEL_RUN_TIMEOUT_SECONDS):
            return await future
    except TimeoutError:
        return "no_stop"
```

- [ ] **Step 4: Add the driver method to the flow class**

```python
    async def _async_capture_travel(
        self,
        session_id: str,
        wanted: frozenset[str],
    ) -> TravelMeasurement | str:
        """Measure one run, always releasing the bridge sniff session."""
        from homeassistant.components import mqtt

        pending = self._pending_measure
        identity = self._measure_identity()
        if pending is None or pending.bridge is None or identity is None:
            return "failed"
        bridge = pending.bridge
        owner_key = (id(self.hass), bridge)
        if owner_key in _CAPTURE_OWNERS:
            if self._sniff_session_id == session_id:
                self._sniff_session_id = None
            return "failed"
        _CAPTURE_OWNERS[owner_key] = session_id
        rx_topic = f"{MQTT_ROOT}/{bridge}/rx"
        command_topic = MQTT_CMD_TEMPLATE.format(bridge=bridge)
        run = TravelRun(identity=identity, channels=pending.channels, wanted=wanted)
        armed = asyncio.Event()
        future: asyncio.Future[TravelMeasurement] = self.hass.loop.create_future()
        unsubscribe: Unsubscriber | None = None
        holder: asyncio.Task[None] | None = None
        try:
            async with asyncio.timeout(_MQTT_BOOTSTRAP_TIMEOUT_SECONDS):
                if not await mqtt.async_wait_for_mqtt_client(self.hass):
                    return "failed"
                unsubscribe = await _async_subscribe_ready(
                    self.hass,
                    rx_topic,
                    functools.partial(
                        _handle_travel_message,
                        self,
                        session_id,
                        rx_topic,
                        run,
                        armed,
                        future,
                    ),
                )
            holder = self.hass.async_create_task(
                _async_hold_sniff_open(self.hass, command_topic),
                f"{DOMAIN} travel sniff hold",
            )
            return await _async_await_measurement(armed, future)
        except TimeoutError:
            return "failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Flow-local travel capture failed", exc_info=True)
            return "failed"
        finally:
            if self._sniff_session_id == session_id:
                self._sniff_session_id = None
            if holder is not None:
                holder.cancel()
            if unsubscribe is not None:
                unsubscribe()
            if not future.done():
                future.cancel()
            stop_task = self.hass.async_create_task(
                mqtt.async_publish(
                    self.hass,
                    command_topic,
                    json.dumps(
                        {
                            MQTT_CMD_FIELD_ACTION: MQTT_CMD_ACTION_SNIFF,
                            MQTT_CMD_FIELD_SECONDS: 0,
                        },
                        separators=(",", ":"),
                    ),
                    qos=1,
                    retain=False,
                ),
                f"{DOMAIN} travel sniff stop",
            )
            stop_task.add_done_callback(
                functools.partial(_release_capture_owner, owner_key, session_id)
            )
            try:
                with suppress(Exception):
                    await asyncio.shield(stop_task)
            finally:
                if stop_task.done():
                    _release_capture_owner(owner_key, session_id, stop_task)

    def _measure_identity(self) -> RemoteIdentity | None:
        """Return the calibrated identity a travel capture must match.

        None for a virtual remote: its bases are synthesized, so no physical
        remote transmits that identity and no press could ever match. Detected
        in memory rather than from stored data, because an entry does not
        record whether its identity was learned or synthesized -- MANUAL_REMOTE
        and VIRTUAL_REMOTE in const.py are dead constants nothing reads.
        """
        if self._identity_is_virtual:
            return None
        if self._identity is not None and self._identity.bases is not None:
            return self._identity
        if self.source != config_entries.SOURCE_RECONFIGURE:
            return None
        try:
            remote = RemoteConfig.from_entry(self._get_reconfigure_entry().data)
        except _COERCION_ERRORS:
            return None
        return remote.remote if remote.remote.bases is not None else None
```

Add the class attributes beside the existing ones:

```python
    _pending_measure: _PendingMeasure | None = None
    _measure_task: asyncio.Task[TravelMeasurement | str] | None = None
    _measure_outcome: str | None = None
    _measure_error: str | None = None
    _identity_is_virtual: bool = False
```

And set the flag in `async_step_virtual`, immediately after it builds `self._identity`:

```python
        self._identity_is_virtual = True
```

- [ ] **Step 5: Verify it compiles and typechecks**

Run: `uv run mypy --strict custom_components/zemismart_blinds/config_flow.py && uv run pytest tests/test_config_flow.py -q`
Expected: mypy clean, existing flow tests still pass (no behavior wired up yet).

- [ ] **Step 6: Commit**

```bash
git add custom_components/zemismart_blinds/config_flow.py
git commit -m "feat: bounded MQTT driver for a travel measurement run

Re-publishes the sniff command every 15s because the firmware extends its
bounded window rather than restarting it, and splits the arming and run
deadlines so each failure can say what actually went wrong."
```

---

### Task 5: Bridge picker, progress, next, and timeout steps

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow.py`
- Modify: `custom_components/zemismart_blinds/config_flow_schema.py`
- Modify: `custom_components/zemismart_blinds/strings.json`
- Modify: `custom_components/zemismart_blinds/translations/en.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `_PendingMeasure`, `_async_capture_travel` from Task 4; `_async_discover_bridges`, `_AUTOMATIC_BRIDGE`, `BridgeRegistry`, `NoOnlineBridgeError` already present.
- Produces: steps `cover_measure_setup`, `cover_measure_run`, `cover_measure_next`, `cover_measure_timeout`; schema `_measure_setup_schema(registry, suggested)`.

- [ ] **Step 1: Add the bridge-picker schema**

In `config_flow_schema.py`, add beside `_learn_setup_schema` and export it in `__all__`:

```python
def _measure_setup_schema(
    registry: BridgeRegistry,
    suggested: Mapping[str, object] | None,
) -> vol.Schema:
    """Build the bridge picker for one travel measurement."""
    values = suggested or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_BRIDGE,
                default=str(values.get(CONF_BRIDGE, _AUTOMATIC_BRIDGE)),
            ): _bridge_selector(registry),
        }
    )
```

Reuse whatever `_learn_setup_schema` already uses to build its bridge selector; if that construction is inline, extract it into `_bridge_selector(registry)` and call it from both, so the two pickers cannot drift.

- [ ] **Step 2: Add the four steps**

In `config_flow.py`:

```python
    async def async_step_cover_measure_setup(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose the bridge that will listen for this cover's run."""
        pending = self._pending_measure
        if pending is None or self._measure_identity() is None:
            return await self._async_measure_abandoned()
        if self._learn_registry is None:
            self._learn_registry = await self._async_discover_bridges()
        if self._learn_registry is None:
            # Flow-local MQTT discovery failed outright, so no bridge can
            # listen. Retrying cannot help inside this step; the cover form
            # still accepts typed times.
            return await self._async_measure_abandoned(error="measure_no_bridge")

        errors: dict[str, str] = {}
        if user_input is not None:
            bridge_id = str(user_input.get(CONF_BRIDGE, "")).strip()
            try:
                if bridge_id == _AUTOMATIC_BRIDGE:
                    bridge_id = self._learn_registry.resolve(
                        self._measure_area_id()
                    ).bridge_id
                else:
                    self._learn_registry.online_bridge(bridge_id)
            except NoOnlineBridgeError:
                errors[CONF_BRIDGE] = "bridge_unavailable"
            else:
                pending.bridge = bridge_id
                return await self.async_step_cover_measure_run()

        return self.async_show_form(
            step_id="cover_measure_setup",
            data_schema=_measure_setup_schema(self._learn_registry, user_input),
            errors=errors,
            description_placeholders={"name": pending.name},
        )

    async def async_step_cover_measure_run(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start one measurement task, then report its transition."""
        del user_input
        pending = self._pending_measure
        if pending is None:
            return await self._async_measure_abandoned()
        if self._measure_task is not None and self._measure_task.done():
            outcome = (
                "failed" if self._measure_task.cancelled() else self._measure_task.result()
            )
            self._measure_task = None
            if isinstance(outcome, TravelMeasurement):
                pending.measured[outcome.direction] = outcome
                return self.async_show_progress_done(next_step_id="cover_measure_next")
            self._measure_outcome = outcome
            return self.async_show_progress_done(next_step_id="cover_measure_timeout")

        if self._measure_task is None:
            session_id = secrets.token_hex(16)
            self._sniff_session_id = session_id
            self._measure_task = self.hass.async_create_task(
                self._async_capture_travel(session_id, pending.wanted),
                f"{DOMAIN} travel capture",
            )

        return self.async_show_progress(
            step_id="cover_measure_run",
            progress_action="measuring",
            progress_task=self._measure_task,
            description_placeholders={
                "name": pending.name,
                "bridge": pending.bridge or "",
                "wanted": " or ".join(sorted(pending.wanted)),
            },
        )

    async def async_step_cover_measure_next(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Report the direction just measured and ask for the other one."""
        del user_input
        pending = self._pending_measure
        if pending is None or not pending.measured:
            return await self._async_measure_abandoned()
        if not pending.wanted:
            return await self.async_step_cover_measure_confirm()
        last = next(reversed(list(pending.measured.values())))
        return self.async_show_menu(
            step_id="cover_measure_next",
            menu_options=["cover_measure_run", "cover_measure_redo"],
            description_placeholders={
                "measured": last.direction,
                "seconds": f"{last.measured_seconds:.2f}",
                "stored": str(last.stored_seconds),
                "wanted": " or ".join(sorted(pending.wanted)),
            },
        )

    async def async_step_cover_measure_redo(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Discard the direction just measured and run it again."""
        del user_input
        pending = self._pending_measure
        if pending is None or not pending.measured:
            return await self._async_measure_abandoned()
        pending.measured.pop(next(reversed(list(pending.measured))), None)
        self._measure_task = None
        self._sniff_session_id = None
        return await self.async_step_cover_measure_run()

    async def async_step_cover_measure_timeout(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer another attempt or typing the times by hand."""
        del user_input
        pending = self._pending_measure
        if pending is None:
            return await self._async_measure_abandoned()
        self._measure_task = None
        self._sniff_session_id = None
        return self.async_show_menu(
            step_id="cover_measure_timeout",
            menu_options=["cover_measure_run", "cover_measure_manual"],
            description_placeholders={
                "reason": self._measure_outcome or "failed",
                "wanted": " or ".join(sorted(pending.wanted)),
            },
        )
```

Plus the two small helpers:

```python
    def _measure_area_id(self) -> str:
        """Return the area whose bridge should listen for this measurement."""
        if self._learn_area_id is not None:
            return self._learn_area_id
        if self.source != config_entries.SOURCE_RECONFIGURE:
            return ""
        try:
            return RemoteConfig.from_entry(self._get_reconfigure_entry().data).area_id
        except _COERCION_ERRORS:
            return ""

    async def _async_measure_abandoned(
        self,
        error: str | None = None,
    ) -> ConfigFlowResult:
        """Return to the cover form when measurement cannot continue."""
        pending = self._pending_measure
        self._pending_measure = None
        self._measure_task = None
        self._sniff_session_id = None
        self._measure_error = error
        if pending is None or pending.origin == "wizard":
            return await self.async_step_cover()
        if pending.origin == "add":
            return await self.async_step_cover_add()
        return await self.async_step_cover_edit()
```

Add `_measure_error: str | None = None` to the class attributes; Task 7 renders it on the cover forms.

- [ ] **Step 3: Add the strings**

In `strings.json` under `config.step`, and identically in `translations/en.json`:

```json
"cover_measure_setup": {
  "title": "Measure travel time",
  "description": "Pick the bridge that will listen while you run {name} with its remote. Automatic uses the bridge for this device's area.",
  "data": { "bridge": "Bridge" }
},
"cover_measure_next": {
  "title": "{measured} measured",
  "description": "{measured} took {seconds} s, which will be stored as {stored} s.\n\nNow press {wanted} on the remote, then press STOP the moment the shade stops moving.",
  "menu_options": {
    "cover_measure_run": "I'm ready, listen for {wanted}",
    "cover_measure_redo": "Redo {measured}"
  }
},
"cover_measure_timeout": {
  "title": "Nothing measured",
  "description": "The run did not complete ({reason}). Check that the remote drives this shade's channels, that you are in range of the bridge, and that you press STOP after the shade stops moving.\n\nA remote created by the new_virtual_remote service cannot be measured this way, because no physical remote transmits its identity.",
  "menu_options": {
    "cover_measure_run": "Try {wanted} again",
    "cover_measure_manual": "Type the times instead"
  }
}
```

Under `config.progress`:

```json
"measuring": "Press {wanted} on the remote for {name}, then press STOP the moment the shade stops moving. Listening on {bridge}."
```

Under `config.error`, add `"measure_no_bridge": "No bridge could be reached to listen for the remote. Enter the travel times manually."`

- [ ] **Step 4: Write the flow test**

Add to `tests/test_config_flow.py`. Build the two runs against `bridge-a`, driving the progress step exactly as the Learn tests do:

```python
async def measure_one_direction(
    hass: HomeAssistant,
    fake: FakeMqtt,
    flow_id: str,
    direction: str,
    *,
    subscription_index: int,
    start_millis: int,
    stop_millis: int,
) -> ConfigFlowResult:
    """Deliver one direction press and its STOP to an armed measurement."""
    rx = fake.rx_subscriptions()[subscription_index]
    for button, millis in ((direction, start_millis), ("STOP", stop_millis)):
        frame = b0_to_b1(
            encode_b0(
                make_payload(TEST_PREFIX, TEST_REMOTE_ID, (1, 2), button, bases=TEST_BASES)
            )
        )
        await fake.emit(
            rx,
            "rf433/bridge-a/rx",
            json.dumps({"frame": frame, "t": millis, "boot": 7}),
        )
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_blank_travel_measures_both_directions(
    hass: HomeAssistant,
    fake: FakeMqtt,
) -> None:
    """A cover submitted with no travel times is measured from the remote."""
    flow_id = await start_learned_flow_at_cover_step(hass, fake)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "Sunroom shade", CONF_CHANNELS: "1,2"},
    )
    assert result["step_id"] == "cover_measure_setup"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "measuring"
    await fake.wait_for_publications(1)

    result = await measure_one_direction(
        hass, fake, flow_id, "DOWN",
        subscription_index=-1, start_millis=1_000, stop_millis=15_310,
    )
    assert result["step_id"] == "cover_measure_next"
    assert result["description_placeholders"]["measured"] == "DOWN"
    assert result["description_placeholders"]["stored"] == "15"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "cover_measure_run"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await measure_one_direction(
        hass, fake, flow_id, "UP",
        subscription_index=-1, start_millis=20_000, stop_millis=36_080,
    )
    assert result["step_id"] == "cover_measure_confirm"
    schema_defaults = result["data_schema"]({})
    assert schema_defaults[CONF_TRAVEL_DOWN] == 15
    assert schema_defaults[CONF_TRAVEL_UP] == 17
```

`start_learned_flow_at_cover_step` is a new helper that walks the existing Learn wizard through its three captures and `remote_settings`, stopping at `async_step_cover`. Build it by extracting the setup already duplicated across the Learn tests rather than writing a fourth copy.

Also add a test that the arming deadline reaches `cover_measure_timeout`, patching `TRAVEL_ARM_TIMEOUT_SECONDS` to `0.001` the way the Learn tests patch `_CAPTURE_TIMEOUT_SECONDS`:

```python
async def test_no_press_reaches_the_timeout_menu(
    hass: HomeAssistant,
    fake: FakeMqtt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hearing nothing at all is reported as its own failure, not a bad run."""
    monkeypatch.setattr(config_flow_module, "TRAVEL_ARM_TIMEOUT_SECONDS", 0.001)
    flow_id = await start_learned_flow_at_cover_step(hass, fake)
    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Sunroom shade", CONF_CHANNELS: "1,2"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_BRIDGE: config_flow_module._AUTOMATIC_BRIDGE}
    )
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "cover_measure_timeout"
    assert result["description_placeholders"]["reason"] == "no_press"
```

`_async_await_measurement` must read `TRAVEL_ARM_TIMEOUT_SECONDS` and `TRAVEL_RUN_TIMEOUT_SECONDS` off the `config_flow` module namespace for that patch to reach it — import the names into `config_flow.py` and reference them unqualified, which is what the existing `_CAPTURE_TIMEOUT_SECONDS` patch relies on.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_config_flow.py -k measure -v`
Expected: PASS. Then the full gate.

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest
git add custom_components/zemismart_blinds/ tests/test_config_flow.py
git commit -m "feat: bridge picker and progress steps for travel measurement

Direction is autodetected, so the run screen asks for either direction and
names only the one still outstanding on the second pass."
```

---

### Task 6: Confirm step and write-back

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow.py`
- Modify: `custom_components/zemismart_blinds/config_flow_schema.py`
- Modify: `custom_components/zemismart_blinds/strings.json`, `translations/en.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `_PendingMeasure.measured`, `_validate_cover_input`, `_entry_cover_rows`, `_find_cover_row`, `_sibling_channel_sets`, `_update_covers_and_abort`.
- Produces: step `cover_measure_confirm`, step `cover_measure_manual`, schema `_measure_confirm_schema(measured)`.

- [ ] **Step 1: Add the confirm schema**

In `config_flow_schema.py`:

```python
def _measure_confirm_schema(measured: Mapping[str, int]) -> vol.Schema:
    """Build the confirm form, pre-filled with the rounded measurements."""
    travel_selector = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0.1,
            max=600,
            step=0.1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )
    return vol.Schema(
        {
            vol.Required(CONF_TRAVEL_UP, default=measured["UP"]): travel_selector,
            vol.Required(CONF_TRAVEL_DOWN, default=measured["DOWN"]): travel_selector,
        }
    )
```

- [ ] **Step 2: Add the confirm and manual steps**

```python
    async def async_step_cover_measure_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show what was measured, let it be corrected, then store it.

        A form, not a menu: a config-flow form has one submit, so there is no
        second "measure again" button here. Correcting a mistimed run is done
        by typing the right value, and a full redo lives on cover_measure_next.
        """
        pending = self._pending_measure
        if pending is None or pending.wanted:
            return await self._async_measure_abandoned()
        measured = {
            direction: measurement.stored_seconds
            for direction, measurement in pending.measured.items()
        }
        if user_input is not None:
            return await self._async_store_measured_cover(pending, user_input)
        return self.async_show_form(
            step_id="cover_measure_confirm",
            data_schema=_measure_confirm_schema(measured),
            description_placeholders={
                "name": pending.name,
                "up": f"{pending.measured['UP'].measured_seconds:.2f}",
                "down": f"{pending.measured['DOWN'].measured_seconds:.2f}",
            },
        )

    async def async_step_cover_measure_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Abandon measurement and return to the cover form."""
        del user_input
        return await self._async_measure_abandoned()

    async def _async_store_measured_cover(
        self,
        pending: _PendingMeasure,
        user_input: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Write the confirmed cover back where the measurement came from.

        Dispatches on the recorded origin rather than on ``self.source``: the
        wizard and the reconfigure add path both persist differently, and
        inferring the destination would let a new entry point silently reuse
        the wrong one.
        """
        fields = {
            CONF_NAME: pending.name,
            CONF_CHANNELS: ",".join(str(channel) for channel in pending.channels),
            CONF_TRAVEL_UP: user_input.get(CONF_TRAVEL_UP),
            CONF_TRAVEL_DOWN: user_input.get(CONF_TRAVEL_DOWN),
        }
        self._pending_measure = None
        if pending.origin == "wizard":
            covers = self._covers
            if covers is None:
                return await self.async_step_user()
            cover, errors = _validate_cover_input(
                fields, [existing.channels for existing in covers]
            )
            if cover is None:
                self._measure_error = errors.get("base", "invalid_config")
                return await self.async_step_cover()
            covers.append(cover)
            return await self.async_step_cover_menu()

        entry = self._get_reconfigure_entry()
        try:
            rows = _entry_cover_rows(entry)
        except ValueError:
            return self.async_abort(reason="invalid_config")
        if pending.origin == "add":
            cover, errors = _validate_cover_input(fields, _sibling_channel_sets(entry))
            if cover is None:
                self._measure_error = errors.get("base", "invalid_config")
                return await self.async_step_cover_add()
            rows.append({CONF_COVER_ID: ulid_now(), **cover.as_dict()})
            return self._update_covers_and_abort(rows, "cover_added")

        cover_id = pending.cover_id
        if cover_id is None:
            return self.async_abort(reason="cover_not_found")
        try:
            index, stored = _find_cover_row(rows, cover_id)
        except ValueError:
            return self.async_abort(reason="cover_not_found")
        cover, errors = _validate_cover_input(
            fields, _sibling_channel_sets(entry, exclude_cover_id=cover_id)
        )
        if cover is None:
            self._measure_error = errors.get("base", "invalid_config")
            return await self.async_step_cover_edit()
        rows[index] = {**stored, **cover.as_dict(), CONF_COVER_ID: cover_id}
        return self._update_covers_and_abort(rows, "cover_updated")
```

- [ ] **Step 3: Add the strings**

```json
"cover_measure_confirm": {
  "title": "Confirm travel times for {name}",
  "description": "Measured: down {down} s, up {up} s. Stored values are rounded up, because pressing STOP after the shade arrives always measures a little long and a generous time simply stalls the motor at its own limit.\n\nCorrect either value before saving.",
  "data": { "travel_up": "Travel up", "travel_down": "Travel down" }
}
```

- [ ] **Step 4: Write the tests**

Extend the Task 5 happy-path test to submit the confirm form and assert the created entry's cover row carries `travel_down: 15` and `travel_up: 17`. Add a reconfigure-origin test that measures an existing cover and asserts the flow aborts with `cover_updated` and the stored row changed.

- [ ] **Step 5: Run the gate and commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest
git add custom_components/zemismart_blinds/ tests/test_config_flow.py
git commit -m "feat: confirm measured travel times before storing them

Raw against rounded, editable, and dispatched back to the origin the
measurement started from rather than inferred from the flow source."
```

---

### Task 7: Blank-travel routing and the re-measure menu

**Files:**
- Modify: `custom_components/zemismart_blinds/config_flow_schema.py:279-282`
- Modify: `custom_components/zemismart_blinds/config_flow.py:511-585`
- Modify: `custom_components/zemismart_blinds/strings.json`, `translations/en.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `_PendingMeasure` from Task 4, all steps from Tasks 5 and 6.
- Produces: `MEASURE_REQUESTED: str` sentinel error key; step `cover_edit_menu`.

- [ ] **Step 1: Make blank travel a routable signal**

In `config_flow_schema.py`, replace the `travel_required` return:

```python
    if not born_aggregate and (raw_up is None or raw_down is None):
        # Blank travel is a request to measure, not a mistake. The caller routes
        # to the capture flow; only a caller that cannot measure (no calibrated
        # identity) renders this as the old travel_required error.
        return None, {"base": MEASURE_REQUESTED}
```

with `MEASURE_REQUESTED: Final = "measure_requested"` defined at module level and exported.

- [ ] **Step 2: Route from the three cover forms**

In each of `async_step_cover`, `async_step_cover_add`, and `async_step_cover_edit`, replace the `cover is None` failure branch with:

```python
                if cover is None and errors.get("base") == MEASURE_REQUESTED:
                    if self._measure_identity() is None:
                        errors = {"base": "travel_required"}
                    else:
                        self._pending_measure = _PendingMeasure(
                            origin="wizard",   # "add" / "edit" in the other two
                            name=str(user_input.get(CONF_NAME, "")).strip(),
                            channels=parse_channels(user_input.get(CONF_CHANNELS, "")),
                            cover_id=None,     # self._cover_id in cover_edit
                        )
                        return await self.async_step_cover_measure_setup()
```

In `async_step_cover_edit`, **delete** the backfill at lines 557-560:

```python
            merged = dict(user_input)
            for key in (CONF_TRAVEL_UP, CONF_TRAVEL_DOWN):
                stored_value = stored.get(key)
                if key not in merged and stored_value not in (None, ""):
                    merged[key] = stored_value
```

The form pre-fills stored values through `_cover_display_values`, so a cleared field there is deliberate; the backfill would restore it and make "blank" unreachable on the one form where re-measuring matters most.

Each form must also surface `self._measure_error` once and clear it, so an abandoned measurement explains itself.

- [ ] **Step 3: Add the re-measure menu**

Change `async_step_cover_pick_edit` to route to a menu rather than straight to the edit form:

```python
                self._cover_id = cover_id
                return await self.async_step_cover_edit_menu()
```

```python
    async def async_step_cover_edit_menu(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Offer editing this cover's fields or measuring its travel times."""
        del user_input
        if self._cover_id is None:
            return await self.async_step_cover_pick_edit()
        return self.async_show_menu(
            step_id="cover_edit_menu",
            menu_options=["cover_edit", "cover_measure_start"],
        )

    async def async_step_cover_measure_start(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Begin measuring the already-chosen cover."""
        del user_input
        cover_id = self._cover_id
        if cover_id is None:
            return await self.async_step_cover_pick_edit()
        entry = self._get_reconfigure_entry()
        try:
            _index, stored = _find_cover_row(_entry_cover_rows(entry), cover_id)
            cover = CoverConfig.from_stored(cover_id, stored)
        except ValueError:
            return self.async_abort(reason="cover_not_found")
        if self._measure_identity() is None:
            return self.async_abort(reason="invalid_config")
        self._pending_measure = _PendingMeasure(
            origin="edit",
            name=cover.name,
            channels=cover.channels,
            cover_id=cover_id,
        )
        return await self.async_step_cover_measure_setup()
```

- [ ] **Step 4: Add the strings**

```json
"cover_edit_menu": {
  "title": "Edit cover",
  "menu_options": {
    "cover_edit": "Edit name, channels and travel times",
    "cover_measure_start": "Measure travel times with the remote"
  }
}
```

Keep the existing `travel_required` error string — it is still reached when no calibrated identity exists.

- [ ] **Step 5: Write the tests**

```python
async def test_an_aggregate_cover_is_never_measured(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cover that aggregates its siblings carries no travel by design."""
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [{CONF_NAME: "Left", CONF_CHANNELS: "1", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 13}],
    )
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cover_add"}
    )
    # Channels 1,2 strictly contain the stored leaf on channel 1, so this row is
    # born_aggregate: blank travel is correct there, not a measurement request.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Both", CONF_CHANNELS: "1,2"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cover_added"
    added = next(row for row in stored_cover_rows(entry) if row[CONF_CHANNELS] == [1, 2])
    assert CoverConfig.from_stored(str(added[CONF_COVER_ID]), added).travel_up is None


async def test_clearing_travel_on_edit_routes_to_measurement(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edit form's backfill must not resurrect the stored value.

    The form arrives pre-filled, so an empty field there is a deliberate clear.
    Restoring it made re-measurement unreachable from the one screen where a
    user with a wrong travel time actually goes.
    """
    entry = await create_remote_entry(
        hass,
        monkeypatch,
        [{CONF_NAME: "Slider", CONF_CHANNELS: "1,2", CONF_TRAVEL_UP: 12, CONF_TRAVEL_DOWN: 13}],
    )
    slider = stored_cover_rows(entry)[0]
    result = await start_reconfigure_flow(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cover_pick_edit"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COVER_ID: slider[CONF_COVER_ID]}
    )
    assert result["step_id"] == "cover_edit_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cover_edit"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Slider", CONF_CHANNELS: "1,2"}
    )
    assert result["step_id"] == "cover_measure_setup"


async def test_a_virtual_remote_still_refuses_blank_travel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthesized identity cannot be measured, so blank travel stays an error.

    Nothing physical transmits a virtual remote's identity, so every press
    would fail to match and the user would wait out the arming deadline to
    learn that. The wizard knows in memory that it allocated this identity.
    """
    prepare_config_flow(hass, monkeypatch)
    result = await start_user_flow(hass)
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "advanced"})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "virtual"})
    await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "Virtual remote",
            CONF_AREA_ID: "kitchen",
            ADVANCED_SECTION: {CONF_REPEATS: 5, CONF_COALESCE_WINDOW_MS: 150},
        },
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Shade", CONF_CHANNELS: "1"}
    )
    assert result["step_id"] == "cover"
    assert result["errors"] == {"base": "travel_required"}
```

Check the exact menu step ids the advanced path uses for `virtual` against the existing advanced-path tests before running; the rest of the body is the established pattern.

- [ ] **Step 6: Update the existing tests the new menu displaces**

Inserting `cover_edit_menu` between `cover_pick_edit` and `cover_edit` breaks every
existing test that submits a `cover_id` and expects the edit form immediately.
`test_cover_edit_prefills_display_values` (`tests/test_config_flow.py:1653`) is one;
find the rest with:

```bash
uv run pytest tests/test_config_flow.py -q 2>&1 | grep -E "^FAILED"
```

Each needs one extra hop inserted after the cover picker:

```python
    assert result["step_id"] == "cover_edit_menu"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cover_edit"}
    )
```

Do not weaken any assertion to make a test pass — the extra hop is the real
behavior change and the tests should show it.

- [ ] **Step 7: Run the gate and commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest
git add custom_components/zemismart_blinds/ tests/test_config_flow.py
git commit -m "feat: blank travel times request a measurement

Removes the cover_edit backfill that silently restored stored travel values
when the key was absent, which made a deliberately cleared field unreachable
as a signal. Adds a re-measure entry to the edit-cover path."
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document the feature**

Add a short section to `README.md` under the configuration documentation: leaving travel times blank starts a guided measurement, what the user physically does, that direction is detected automatically, that values round up, and that a virtual remote cannot be measured this way.

- [ ] **Step 2: Add the changelog entry**

Add an `## Unreleased` section describing the feature, the removal of the `cover_edit` travel backfill as a behavior change, and the new `cover_edit_menu` step in the reconfigure path.

- [ ] **Step 3: Run the gate and commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run mypy --strict . && uv run pytest
git add README.md CHANGELOG.md
git commit -m "docs: describe measuring travel times from the remote"
```

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: the matcher and its
`infer_action_button` rationale to Task 1; bridge-clock timing, the boot and wrap
guards, and the ceiling to Task 2; burst debounce, restart, reversal, and the
already-measured rule to Task 3; re-arming, the two deadlines, and bridge
ownership to Task 4; the bridge picker and the four screens to Task 5; the
confirm form and the three write-back origins to Task 6; blank-travel routing,
the backfill removal, and the re-measure menu to Task 7. The virtual-remote gap
is covered by `_measure_identity` returning `None` in Task 4, the
`cover_measure_timeout` copy in Task 5, and the `travel_required` fallback in
Task 7.

**Type consistency.** `identify_button` and `TravelRun.offer_payload` return
`str | None` and `TravelMeasurement | None` respectively everywhere they appear.
`_async_capture_travel` returns `TravelMeasurement | str`, and every caller
narrows with `isinstance(outcome, TravelMeasurement)`. `_PendingMeasure.wanted`
is a `frozenset[str]` in both its definition and its two call sites.

**Placeholder scan.** One deliberate omission remains: Task 5's
`start_learned_flow_at_cover_step` helper is specified by what it must do —
walk the existing Learn wizard through its three captures and `remote_settings`,
stopping at `async_step_cover` — rather than written out, because that setup is
already duplicated across several Learn tests and should be extracted from them
rather than written a fourth time. Every other step carries complete code.

**Reachability.** The `travel_required` fallback in Task 7 is reachable only for
a virtual remote, which is exactly what `test_a_virtual_remote_still_refuses_blank_travel`
exercises. Without the `_identity_is_virtual` flag added in Task 4 that branch
would be dead code, since every other wizard path sets a calibrated
`self._identity` before reaching the cover form.
