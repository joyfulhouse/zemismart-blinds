"""Optional pre-seeded per-remote command calibrations.

This table ships empty.  Every remote calibrated through the config flow
stores its command bases in its own config entry, so the integration has no
runtime dependency on this module.  A private deployment may pre-seed known
``(prefix, remote_id) -> CommandBases`` pairs here (for example from a fleet
of already-captured remotes) so the config flow can reuse them without
re-entering a capture; doing so requires a runtime ``CommandBases`` import.

Seeded values are interpreted under the fixed-opcode command model
(PROTOCOL.md, 2026-08-06): a base's high byte IS the on-air opcode byte for
every channel set.  Values recorded under the pre-0.9.0 16-bit-carry formula
must be renormalized the way the v3 entry migration does before seeding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .codec import CommandBases

KNOWN_CALIBRATIONS: Final[dict[tuple[int, int], CommandBases]] = {}
