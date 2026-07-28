"""Shared synthetic remote identities used across the test suite.

These identities are deliberately fabricated: the byte-exact golden vectors
below were generated with the hardware-validated codec so regressions in the
protocol math still fail loudly, without shipping any real remote's identity
or replayable command material.
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
TEST_CH12_DOWN_PAYLOAD: Final = 0xA1B2C342FCFFBD2F
TEST_CH12_DOWN_B0: Final = (
    "AAB04D04081414026C01181414381A192A192929292A1A192A1A19292A192A1A192929292A1A192A"
    "192929292A192A1A1A1A1A1A19292A1A1A1A1A1A1A1A1A192A1A1A1A192A19292A192A1A1A1A1A1955"
)

# (name, prefix, remote_id, bases, expected channel-1 UP payload)
SYNTHETIC_REMOTES: Final = (
    ("A", TEST_PREFIX, TEST_REMOTE_ID, TEST_BASES, 0xA1B2C342FEFFF469),
    ("B", 0x123456, 0x0D, CommandBases(0xF449, 0xBD11, 0xDD31), 0x1234560DFEFFF453),
    ("C", 0x7E55AA, 0xE5, CommandBases(0xF38F, 0xBC57, 0xDB77), 0x7E55AAE5FEFFF471),
    ("D", 0x0FF1CE, 0x10, CommandBases(0xF52F, 0xBCF7, 0xDD17), 0x0FF1CE10FEFFF53C),
)

# A remote whose ON-AIR action opcodes fall OUTSIDE `_ACTION_COMMAND_HIGH`.
#
# Issue #26 proved that table is a 10-sample empirical fit, not protocol: a real
# AOK/Zemismart remote emits F3/BC/DB where the table assumes F4/BC/DC, and the
# derived UP frame was transmitted, heard by five bridges, and ignored by the
# motor. The low-byte offset rule (DOWN = UP - 0x38, STOP = UP - 0x18) held
# 11/11 across that evidence and is preserved here.
#
# Chosen so the COMMAND ON AIR is untabled, not merely the base: inference reads
# the command, and a base with an F3 high byte can still put an F4 command on
# air once the remote id and group offset are applied. `SYNTHETIC_REMOTES[2]`
# looks untabled but is not, for exactly that reason.
UNTABLED_PREFIX: Final = 0x7E55AA
UNTABLED_REMOTE_ID: Final = 0xE5
UNTABLED_BASES: Final = CommandBases(0xF300, 0xF2C8, 0xF2E8)
