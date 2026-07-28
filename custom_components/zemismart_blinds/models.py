"""Compatibility exports for Zemismart Blinds models and transport.

This shim guarantees that every pre-split import keeps resolving and that
the monkeypatch seams the test suite uses stay live (the module replaces
itself with ``transport``, so patching a transport timing constant through
this name reaches the runtime read). It does NOT guarantee that patching
every private constant re-exported here reaches code that moved to
``config_models`` or ``bridge_registry`` — those read their own module
globals. Patch the canonical module that owns the code under test.
"""

import sys

from . import transport as _transport
from .bridge_registry import BridgeInfo, BridgeRegistry, NoOnlineBridgeError
from .config_models import (
    MAX_COALESCE_WINDOW_MS,
    MAX_REPEATS,
    MAX_TRAVEL_SECONDS,
    MIN_REPEATS,
    BlindConfig,
    CoverConfig,
    RemoteConfig,
    RemoteIdentity,
    Role,
    derive_role,
    laminar_conflict,
    member_covers,
    parse_channels,
    parse_hex,
    whole_number,
)
from .transport import (
    _BRIDGE_CLOCK_CAP as _BRIDGE_CLOCK_CAP,
)
from .transport import (
    _DISARM_RETRY_SECONDS as _DISARM_RETRY_SECONDS,
)
from .transport import (
    _MAX_OUTSTANDING_COMMANDS as _MAX_OUTSTANDING_COMMANDS,
)
from .transport import (
    _PRESTART_DISARM_DEADLINE_SECONDS as _PRESTART_DISARM_DEADLINE_SECONDS,
)
from .transport import (
    _PUBLISH_SEQ_CAP as _PUBLISH_SEQ_CAP,
)
from .transport import (
    DEFAULT_ACK_TIMEOUT_SECONDS,
    DEFAULT_STARTED_TIMEOUT_SECONDS,
    Button,
    Clock,
    CommandAck,
    CommandAckTimeoutError,
    CommandDisplacedError,
    CommandIdFactory,
    CommandQueueFullError,
    CommandRejectedError,
    CommandResult,
    CommandStartedTimeoutError,
    CommandStatusValue,
    DomainRuntime,
    Publisher,
    RemoteRuntime,
    TakeoverCoverState,
    Unsubscriber,
    ZemismartHub,
)
from .transport import (
    _ledger_airtime_ms as _ledger_airtime_ms,
)

__all__ = [
    "DEFAULT_ACK_TIMEOUT_SECONDS",
    "DEFAULT_STARTED_TIMEOUT_SECONDS",
    "MAX_COALESCE_WINDOW_MS",
    "MAX_REPEATS",
    "MAX_TRAVEL_SECONDS",
    "MIN_REPEATS",
    "BlindConfig",
    "BridgeInfo",
    "BridgeRegistry",
    "Button",
    "Clock",
    "CommandAck",
    "CommandAckTimeoutError",
    "CommandDisplacedError",
    "CommandIdFactory",
    "CommandQueueFullError",
    "CommandRejectedError",
    "CommandResult",
    "CommandStartedTimeoutError",
    "CommandStatusValue",
    "CoverConfig",
    "DomainRuntime",
    "NoOnlineBridgeError",
    "Publisher",
    "RemoteConfig",
    "RemoteIdentity",
    "RemoteRuntime",
    "Role",
    "TakeoverCoverState",
    "Unsubscriber",
    "ZemismartHub",
    "derive_role",
    "laminar_conflict",
    "member_covers",
    "parse_channels",
    "parse_hex",
    "whole_number",
]

sys.modules[__name__] = _transport
