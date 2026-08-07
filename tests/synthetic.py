"""Shared synthetic remote identities used across the test suite.

These identities are deliberately fabricated: the byte-exact golden vectors
below were generated with the hardware-validated codec so regressions in the
protocol math still fail loudly, without shipping any real remote's identity
or replayable command material.

Regenerated 2026-08-06 with the corrected command formula: the OEM keeps the
opcode high byte fixed per action and wraps only the command low byte mod 256.
The previous vectors carried a low-byte overflow into the opcode byte, which a
live carry-straddle remote proved wrong (its motor ignored the carried 0xBDxx
DOWN and moved on the wrapped 0xBCxx one).
"""

from __future__ import annotations

from typing import Final

from custom_components.zemismart_blinds.codec import CommandBases

TEST_PREFIX: Final = 0xA1B2C3
TEST_REMOTE_ID: Final = 0x42
TEST_BASES: Final = CommandBases(0xF42A, 0xBCF2, 0xDC12, trailer=0xDD05)
TEST_ACTION_BASES: Final = CommandBases(0xF42A, 0xBCF2, 0xDC12)

TEST_ALL_UP_PAYLOAD: Final = 0xA1B2C342C0FFF42B
TEST_ALL_UP_B0: Final = (
    "AAB04D04081414026C01181414381A192A192929292A1A192A1A19292A192A1A192929292A1A192A"
    "192929292A192A1A1929292929292A1A1A1A1A1A1A1A1A1A1A1A192A192929292A192A192A1A1A1955"
)
TEST_CH12_UP_PAYLOAD: Final = 0xA1B2C342FCFFF467
TEST_CH12_UP_B0: Final = (
    "AAB04D04081414026C01181414381A192A192929292A1A192A1A19292A192A1A192929292A1A192A"
    "192929292A192A1A1A1A1A1A19292A1A1A1A1A1A1A1A1A1A1A1A192A1929292A1A19292A1A1A1A1955"
)
TEST_CH12_DOWN_PAYLOAD: Final = 0xA1B2C342FCFFBC2F
TEST_CH12_DOWN_B0: Final = (
    "AAB04D04081414026C01181414381A192A192929292A1A192A1A19292A192A1A192929292A1A192A"
    "192929292A192A1A1A1A1A1A19292A1A1A1A1A1A1A1A1A192A1A1A1A192929292A192A1A1A1A1A1955"
)

# (name, prefix, remote_id, bases, expected channel-1 UP payload)
#
# Remote D is the carry-straddle shape validated live on 2026-08-06: its
# DOWN low byte plus remote id (0x30 + 0xFD) crosses 0x100 for small channel
# groups but not for the full 1..6 group, so any carry into the opcode byte
# produces different opcodes per channel set — which real remotes never do.
SYNTHETIC_REMOTES: Final = (
    ("A", TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES, 0xA1B2C342FEFFF469),
    ("B", 0x123456, 0x0D, CommandBases(0xF449, 0xBC11, 0xDC31), 0x1234560DFEFFF453),
    ("C", 0x7E55AA, 0xE5, CommandBases(0xF48F, 0xBC57, 0xDC77), 0x7E55AAE5FEFFF471),
    ("D", 0x0FF1CE, 0xFD, CommandBases(0xF468, 0xBC30, 0xDC50), 0x0FF1CEFDFEFFF462),
)

# Remote D's field captures (identity re-keyed; command math byte-exact from
# the six live captures of 2026-08-06). One dict per action, keyed by the
# channel set the physical remote's selector was on when the button was
# pressed. DOWN is the straddle witness: offset 3 overflows the low-byte sum
# while offset 0x41 does not, yet the opcode byte is 0xBC in both captures.
STRADDLE_NAME, STRADDLE_PREFIX, STRADDLE_REMOTE_ID, STRADDLE_BASES, _ = SYNTHETIC_REMOTES[3]
STRADDLE_UP_CAPTURES: Final[dict[tuple[int, ...], int]] = {(1,): 0xF462}
STRADDLE_DOWN_CAPTURES: Final[dict[tuple[int, ...], int]] = {
    (1,): 0xBC2A,
    (3,): 0xBC27,
    (1, 2, 3, 4, 5, 6): 0xBCEC,
}
STRADDLE_STOP_CAPTURES: Final[dict[tuple[int, ...], int]] = {(1,): 0xDC4A, (3,): 0xDC47}

# A remote whose action opcode bytes fall OUTSIDE `_ACTION_COMMAND_HIGH`.
#
# Issue #26 proved that table is a 10-sample empirical fit, not protocol: a real
# AOK/Zemismart remote emits F3/BC/DB where the table assumes F4/BC/DC, and the
# derived UP frame was transmitted, heard by five bridges, and ignored by the
# motor. The low-byte offset rule (DOWN = UP - 0x38, STOP = UP - 0x18) held
# 11/11 across that evidence and is preserved here. The opcode byte is
# channel-invariant (2026-08-06), so an untabled base always puts an untabled
# command on air.
UNTABLED_PREFIX: Final = 0x7E55AA
UNTABLED_REMOTE_ID: Final = 0xE5
UNTABLED_BASES: Final = CommandBases(0xF300, 0xF2C8, 0xF2E8)
