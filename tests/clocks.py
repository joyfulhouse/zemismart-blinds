"""Separated wall and monotonic clocks for tests."""

from dataclasses import dataclass
from typing import Final

TEST_WALL_TIME: Final = 1_700_000_000.0
TEST_MONOTONIC_TIME: Final = 200.0


@dataclass(slots=True)
class SteppableClocks:
    """Keep deliberately distinct test clock epochs moving in lockstep."""

    wall: float = TEST_WALL_TIME
    monotonic: float = TEST_MONOTONIC_TIME

    def wall_now(self) -> float:
        """Return the current wall-clock value."""
        return self.wall

    def monotonic_now(self) -> float:
        """Return the current monotonic-clock value."""
        return self.monotonic

    def advance(self, seconds: float) -> None:
        """Advance both clocks by the same elapsed duration."""
        self.wall += seconds
        self.monotonic += seconds
        # The epoch separation is the whole point of this class: at extreme
        # magnitudes binary64 rounding collapses the two axes back together
        # (1e26 + 1.7e9 == 1e26), silently re-hiding axis-swap mutations.
        # Tolerance, not equality: a benign advance rounds in the last place
        # at the wall magnitude (ULP ~ 2.4e-7 s); collapse is off by ~1.7e9 s.
        separation = TEST_WALL_TIME - TEST_MONOTONIC_TIME
        if not abs((self.wall - self.monotonic) - separation) < 1.0:
            msg = (
                f"clock advance of {seconds!r} destroyed the wall/monotonic "
                f"epoch separation ({self.wall!r} vs {self.monotonic!r})"
            )
            raise ValueError(msg)

    def set_monotonic(self, value: float) -> None:
        """Set monotonic time while applying the same delta to wall time."""
        self.advance(value - self.monotonic)
