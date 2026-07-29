"""Separated wall and monotonic clocks for tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable

TEST_WALL_TIME: Final = 1_700_000_000.0
TEST_MONOTONIC_TIME: Final = 200.0


class ClockKwargs(TypedDict):
    """Constructor callbacks for code that consumes both clock axes."""

    now: Callable[[], float]
    monotonic_now: Callable[[], float]


@dataclass(slots=True)
class SteppableClocks:
    """Keep deliberately distinct test clock epochs moving in lockstep."""

    wall: float = TEST_WALL_TIME
    monotonic: float = TEST_MONOTONIC_TIME
    _separation: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Remember the caller's deliberate epoch separation."""
        self._separation = self.wall - self.monotonic

    def wall_now(self) -> float:
        """Return the current wall-clock value."""
        return self.wall

    def monotonic_now(self) -> float:
        """Return the current monotonic-clock value."""
        return self.monotonic

    def as_kwargs(self) -> ClockKwargs:
        """Return both callbacks under their production constructor names."""
        return {
            "now": self.wall_now,
            "monotonic_now": self.monotonic_now,
        }

    def advance(self, seconds: float) -> None:
        """Advance both clocks by the same elapsed duration."""
        self.wall += seconds
        self.monotonic += seconds
        # The epoch separation is the whole point of this class: at extreme
        # magnitudes binary64 rounding collapses the two axes back together
        # (1e26 + 1.7e9 == 1e26), silently re-hiding axis-swap mutations.
        # Tolerance, not equality: a benign advance rounds in the last place
        # at the wall magnitude (ULP ~ 2.4e-7 s); collapse is off by ~1.7e9 s.
        if not abs((self.wall - self.monotonic) - self._separation) < 1.0:
            msg = (
                f"clock advance of {seconds!r} destroyed the wall/monotonic "
                f"epoch separation ({self.wall!r} vs {self.monotonic!r})"
            )
            raise ValueError(msg)

    def step_wall(self, seconds: float) -> None:
        """Apply an intentional wall-clock correction without elapsed time."""
        self.wall += seconds
        self._separation += seconds

    def set_monotonic(self, value: float) -> None:
        """Set monotonic time while applying the same delta to wall time."""
        self.advance(value - self.monotonic)
