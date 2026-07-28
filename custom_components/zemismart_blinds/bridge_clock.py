"""Bridge clock correlation for Zemismart Blinds."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Protocol, cast

__all__ = ["BridgeClock"]

_MILLISECONDS_PER_SECOND: Final = 1_000.0
_UINT32_MODULUS: Final = 1 << 32
_UINT32_MASK: Final = _UINT32_MODULUS - 1
_UINT32_HALF_RANGE: Final = _UINT32_MODULUS // 2
_CLOCK_EMA_ALPHA: Final = 0.2
_CLOCK_RESEED_RESIDUAL_SECONDS: Final = 30.0
_CLOCK_LONG_GAP_SECONDS: Final = 1_036_800.0
_CLOCK_MAX_PROJECTION_LAG_SECONDS: Final = 30.0


class _StateSyncConstants(Protocol):
    """Legacy constants whose patch points remain canonical in state_sync."""

    _MILLISECONDS_PER_SECOND: float
    _UINT32_HALF_RANGE: int
    _CLOCK_EMA_ALPHA: float
    _CLOCK_RESEED_RESIDUAL_SECONDS: float
    _CLOCK_LONG_GAP_SECONDS: float
    _CLOCK_MAX_PROJECTION_LAG_SECONDS: float


def _state_sync_constants() -> _StateSyncConstants:
    """Return the legacy module that owns patchable clock constants."""
    from . import state_sync

    return cast("_StateSyncConstants", state_sync)


@dataclass(frozen=True, slots=True)
class _ClockOutlier:
    """Retain one unapplied offset outlier for next-sample confirmation."""

    boot: int
    raw_t: int
    received_at_monotonic: float
    implied_offset: float


class BridgeClock:
    """Correlate one bridge's uint32 millisecond clock with monotonic time."""

    def __init__(self) -> None:
        """Initialize an unseeded clock correlation."""
        self._boot: int | None = None
        self._raw_t: int | None = None
        self._unwrapped_seconds: float | None = None
        self._offset_seconds: float | None = None
        self._last_received_at_monotonic: float | None = None
        self._outlier: _ClockOutlier | None = None

    def observe(self, boot: int, t: int, received_at_monotonic: float) -> None:
        """Incorporate one ordered bridge timestamp sample into the EMA."""
        constants = _state_sync_constants()
        raw_t = t & _UINT32_MASK
        if self._boot is None or boot != self._boot:
            self._seed(boot, raw_t, received_at_monotonic)
            return
        if self._is_long_gap(received_at_monotonic):
            self._seed(boot, raw_t, received_at_monotonic)
            return

        raw_delta = self._forward_delta(raw_t)
        if raw_delta == constants._UINT32_HALF_RANGE:
            self._seed(boot, raw_t, received_at_monotonic)
            return
        if raw_delta == 0 or raw_delta > constants._UINT32_HALF_RANGE:
            self._outlier = None
            return

        previous_unwrapped = self._unwrapped_seconds
        previous_offset = self._offset_seconds
        if previous_unwrapped is None or previous_offset is None:
            self._seed(boot, raw_t, received_at_monotonic)
            return
        unwrapped = previous_unwrapped + raw_delta / constants._MILLISECONDS_PER_SECOND
        observed_offset = received_at_monotonic - unwrapped
        residual = observed_offset - previous_offset
        if not math.isfinite(residual):
            self._outlier = None
            return
        if abs(residual) > constants._CLOCK_RESEED_RESIDUAL_SECONDS:
            self._record_or_confirm_outlier(
                boot,
                raw_t,
                received_at_monotonic,
                observed_offset,
            )
            return

        self._outlier = None
        self._raw_t = raw_t
        self._unwrapped_seconds = unwrapped
        self._offset_seconds = previous_offset + constants._CLOCK_EMA_ALPHA * residual
        self._last_received_at_monotonic = received_at_monotonic

    def can_project(self, boot: int) -> bool:
        """Return whether this boot has a seeded monotonic correlation."""
        return (
            boot == self._boot
            and self._raw_t is not None
            and self._unwrapped_seconds is not None
            and self._offset_seconds is not None
        )

    def to_monotonic_time(
        self,
        boot: int,
        t: int,
        received_at_monotonic: float,
    ) -> float:
        """Project a bridge timestamp into monotonic time, never after receipt."""
        constants = _state_sync_constants()
        if boot != self._boot:
            return received_at_monotonic
        raw_t = t & _UINT32_MASK
        signed_delta = self._signed_delta(raw_t)
        unwrapped = self._unwrapped_seconds
        offset = self._offset_seconds
        if signed_delta is None or unwrapped is None or offset is None:
            return received_at_monotonic
        projected = unwrapped + signed_delta / constants._MILLISECONDS_PER_SECOND + offset
        if not math.isfinite(projected):
            return received_at_monotonic
        if (
            projected > received_at_monotonic
            or projected < received_at_monotonic - constants._CLOCK_MAX_PROJECTION_LAG_SECONDS
        ):
            return received_at_monotonic
        return projected

    def _record_or_confirm_outlier(
        self,
        boot: int,
        raw_t: int,
        received_at_monotonic: float,
        implied_offset: float,
    ) -> None:
        """Remember one offset outlier or reseed after a consistent successor."""
        constants = _state_sync_constants()
        previous = self._outlier
        if (
            previous is not None
            and previous.boot == boot
            and math.isfinite(implied_offset)
            and abs(implied_offset - previous.implied_offset)
            <= constants._CLOCK_RESEED_RESIDUAL_SECONDS
        ):
            self._seed(boot, raw_t, received_at_monotonic)
            return
        self._outlier = _ClockOutlier(
            boot=boot,
            raw_t=raw_t,
            received_at_monotonic=received_at_monotonic,
            implied_offset=implied_offset,
        )

    def _is_long_gap(self, received_at_monotonic: float) -> bool:
        """Return whether serial ordering is unsafe after a quiet interval."""
        constants = _state_sync_constants()
        return (
            self._last_received_at_monotonic is not None
            and received_at_monotonic - self._last_received_at_monotonic
            > constants._CLOCK_LONG_GAP_SECONDS
        )

    def _seed(self, boot: int, raw_t: int, received_at_monotonic: float) -> None:
        """Reset the correlation from one receive-time sample."""
        unwrapped = raw_t / _state_sync_constants()._MILLISECONDS_PER_SECOND
        self._boot = boot
        self._raw_t = raw_t
        self._unwrapped_seconds = unwrapped
        self._offset_seconds = received_at_monotonic - unwrapped
        self._last_received_at_monotonic = received_at_monotonic
        self._outlier = None

    def _forward_delta(self, raw_t: int) -> int:
        """Return the unsigned serial delta from the most recent sample."""
        previous = self._raw_t
        if previous is None:
            return _state_sync_constants()._UINT32_HALF_RANGE
        return (raw_t - previous) & _UINT32_MASK

    def _signed_delta(self, raw_t: int) -> int | None:
        """Return an unambiguous signed serial delta from the latest sample."""
        half_range = _state_sync_constants()._UINT32_HALF_RANGE
        raw_delta = self._forward_delta(raw_t)
        if raw_delta == half_range:
            return None
        if raw_delta < half_range:
            return raw_delta
        return raw_delta - _UINT32_MODULUS

    def clear(self) -> None:
        """Forget the current bridge correlation."""
        self._boot = None
        self._raw_t = None
        self._unwrapped_seconds = None
        self._offset_seconds = None
        self._last_received_at_monotonic = None
        self._outlier = None
