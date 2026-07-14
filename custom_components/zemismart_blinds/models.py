"""Typed data and MQTT transport models for Zemismart Blinds."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Final, Literal, cast

from .calibrations import KNOWN_CALIBRATIONS
from .codec import (
    CommandBases,
    decode_b0,
    encode_b0,
    make_payload,
    validate_b0_frame,
)
from .const import (
    CONF_AREA_ID,
    CONF_BASE_DOWN,
    CONF_BASE_STOP,
    CONF_BASE_TRAILER,
    CONF_BASE_UP,
    CONF_CHANNELS,
    CONF_COALESCE_WINDOW_MS,
    CONF_NAME,
    CONF_PREFIX,
    CONF_REMOTE_ID,
    CONF_REPEATS,
    CONF_TRAVEL_DOWN,
    CONF_TRAVEL_UP,
    DEFAULT_COALESCE_WINDOW_MS,
    MQTT_ROOT,
)

Button = Literal["UP", "DOWN", "STOP", "TRAILER"]
Publisher = Callable[[str, str], Awaitable[None]]
Unsubscriber = Callable[[], None]
Clock = Callable[[], float]
CommandIdFactory = Callable[[], str]
CommandStatusValue = Literal["accepted", "rejected"]

MIN_REPEATS: Final = 1
MAX_REPEATS: Final = 20
DEFAULT_ACK_TIMEOUT_SECONDS: Final = 2.0
DEFAULT_STARTED_TIMEOUT_SECONDS: Final = 30.0


class NoOnlineBridgeError(RuntimeError):
    """Raised when no discovered bridge is currently online."""


class CommandAckTimeoutError(RuntimeError):
    """Raised when a bridge may have received a command but did not acknowledge it."""


class CommandStartedTimeoutError(RuntimeError):
    """Raised when an admitted command does not report its first RF dispatch."""


class CommandRejectedError(RuntimeError):
    """Raised when a bridge explicitly rejects a correlated command."""


def _parse_hex(value: object, field: str) -> int:
    """Parse an integer or a user-facing hexadecimal string."""
    if isinstance(value, bool):
        msg = f"{field} must be hexadecimal"
        raise ValueError(msg)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized.startswith("0x"):
            normalized = normalized[2:]
        try:
            return int(normalized, 16)
        except ValueError as exc:
            msg = f"{field} must be hexadecimal"
            raise ValueError(msg) from exc
    msg = f"{field} must be hexadecimal"
    raise ValueError(msg)


def _required(mapping: Mapping[str, object], key: str) -> object:
    """Get a required stored configuration value with a useful error."""
    try:
        return mapping[key]
    except KeyError as exc:
        msg = f"missing required config value: {key}"
        raise ValueError(msg) from exc


def _number_scalar(value: object, field: str, kind: str) -> int | float | str:
    """Reject booleans/containers before numeric config coercion."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        msg = f"{field} must be {kind}"
        raise ValueError(msg)
    return value


def _as_float(value: object, field: str) -> float:
    """Coerce a stored JSON scalar to float without accepting arbitrary objects."""
    try:
        return float(_number_scalar(value, field, "numeric"))
    except ValueError as exc:
        msg = f"{field} must be numeric"
        raise ValueError(msg) from exc


def _as_int(value: object, field: str) -> int:
    """Coerce a stored JSON scalar to int without accepting arbitrary objects."""
    try:
        return int(_number_scalar(value, field, "an integer"))
    except ValueError as exc:
        msg = f"{field} must be an integer"
        raise ValueError(msg) from exc


@dataclass(frozen=True, slots=True)
class RemoteIdentity:
    """The 32-bit identity shared by every channel of one remote."""

    prefix: int
    remote_id: int
    bases: CommandBases | None = None

    def __post_init__(self) -> None:
        """Validate protocol field widths."""
        if not 0 <= self.prefix <= 0xFFFFFF:
            msg = "prefix must be an unsigned 24-bit integer"
            raise ValueError(msg)
        if not 0 <= self.remote_id <= 0xFF:
            msg = "remote_id must be an unsigned 8-bit integer"
            raise ValueError(msg)
        if self.bases is None:
            object.__setattr__(
                self,
                "bases",
                KNOWN_CALIBRATIONS.get((self.prefix, self.remote_id)),
            )

    @property
    def key(self) -> str:
        """Return a stable config-flow/dropdown identity."""
        return f"{self.prefix:06x}:{self.remote_id:02x}"

    def target_key(self, channels: Iterable[int]) -> str:
        """Return the canonical bridge-agnostic key for one channel set."""
        normalized = tuple(sorted(channels))
        if (
            not normalized
            or any(channel < 1 or channel > 16 for channel in normalized)
            or len(normalized) != len(set(normalized))
        ):
            msg = "target channels must be a unique non-empty set in the range 1..16"
            raise ValueError(msg)
        channel_key = ",".join(str(channel) for channel in normalized)
        return f"{self.key}:{channel_key}"


@dataclass(frozen=True, slots=True)
class BlindConfig:
    """Persisted configuration for exactly one blind or group device."""

    name: str
    remote: RemoteIdentity
    channels: tuple[int, ...]
    travel_up: float
    travel_down: float
    area_id: str
    repeats: int
    coalesce_window_ms: int = DEFAULT_COALESCE_WINDOW_MS

    def __post_init__(self) -> None:
        """Normalize and validate values at the config-entry boundary."""
        name = self.name.strip()
        area_id = self.area_id.strip()
        channels = tuple(sorted(self.channels))
        if not name:
            msg = "name must not be empty"
            raise ValueError(msg)
        if not area_id:
            msg = "area_id must not be empty"
            raise ValueError(msg)
        if self.remote.bases is None:
            msg = "remote calibration is required"
            raise ValueError(msg)
        if not channels or any(channel < 1 or channel > 16 for channel in channels):
            msg = "channels must contain values in the range 1..16"
            raise ValueError(msg)
        if len(channels) != len(set(channels)):
            msg = "channels must be unique"
            raise ValueError(msg)
        if self.travel_up <= 0 or self.travel_down <= 0:
            msg = "travel times must be greater than zero"
            raise ValueError(msg)
        if not MIN_REPEATS <= self.repeats <= MAX_REPEATS:
            msg = f"repeats must be in the range {MIN_REPEATS}..{MAX_REPEATS}"
            raise ValueError(msg)
        if (
            isinstance(self.coalesce_window_ms, bool)
            or not isinstance(self.coalesce_window_ms, int)
            or self.coalesce_window_ms < 0
        ):
            msg = "coalesce_window_ms must be a non-negative integer"
            raise ValueError(msg)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "area_id", area_id)
        object.__setattr__(self, "channels", channels)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> BlindConfig:
        """Build a typed config from Home Assistant entry data/options."""
        raw_channels = _required(values, CONF_CHANNELS)
        if isinstance(raw_channels, str):
            channels: Iterable[int] = (
                int(part.strip()) for part in raw_channels.strip("{} ").split(",") if part.strip()
            )
        elif isinstance(raw_channels, Iterable):
            channels = (int(channel) for channel in raw_channels)
        else:
            msg = "channels must be an iterable"
            raise ValueError(msg)

        prefix = _parse_hex(_required(values, CONF_PREFIX), CONF_PREFIX)
        remote_id = _parse_hex(_required(values, CONF_REMOTE_ID), CONF_REMOTE_ID)
        configured_bases = [key in values for key in (CONF_BASE_UP, CONF_BASE_DOWN, CONF_BASE_STOP)]
        if any(configured_bases) and not all(configured_bases):
            msg = "base_up, base_down, and base_stop must be configured together"
            raise ValueError(msg)
        bases = (
            CommandBases(
                up=_parse_hex(_required(values, CONF_BASE_UP), CONF_BASE_UP),
                down=_parse_hex(_required(values, CONF_BASE_DOWN), CONF_BASE_DOWN),
                stop=_parse_hex(_required(values, CONF_BASE_STOP), CONF_BASE_STOP),
                trailer=(
                    _parse_hex(values[CONF_BASE_TRAILER], CONF_BASE_TRAILER)
                    if values.get(CONF_BASE_TRAILER) not in (None, "")
                    else None
                ),
            )
            if all(configured_bases)
            else None
        )

        # RemoteIdentity.__post_init__ fills bases from KNOWN_CALIBRATIONS when
        # none are stored (the table ships empty; deployments may pre-seed it).
        # A remote with no stored bases and no pre-seeded calibration stays
        # None and is rejected by BlindConfig.__post_init__ ("remote
        # calibration is required") — never silently defaulted, which would
        # emit wrong codes for a different remote.
        remote = RemoteIdentity(
            prefix=prefix,
            remote_id=remote_id,
            bases=bases,
        )

        return cls(
            name=str(_required(values, CONF_NAME)),
            remote=remote,
            channels=tuple(channels),
            travel_up=_as_float(_required(values, CONF_TRAVEL_UP), CONF_TRAVEL_UP),
            travel_down=_as_float(_required(values, CONF_TRAVEL_DOWN), CONF_TRAVEL_DOWN),
            area_id=str(_required(values, CONF_AREA_ID)),
            repeats=_as_int(_required(values, CONF_REPEATS), CONF_REPEATS),
            coalesce_window_ms=_as_int(
                values.get(CONF_COALESCE_WINDOW_MS, DEFAULT_COALESCE_WINDOW_MS),
                CONF_COALESCE_WINDOW_MS,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe config-entry storage values."""
        assert self.remote.bases is not None
        values: dict[str, object] = {
            CONF_NAME: self.name,
            CONF_PREFIX: f"{self.remote.prefix:06x}",
            CONF_REMOTE_ID: f"{self.remote.remote_id:02x}",
            CONF_CHANNELS: list(self.channels),
            CONF_TRAVEL_UP: self.travel_up,
            CONF_TRAVEL_DOWN: self.travel_down,
            CONF_AREA_ID: self.area_id,
            CONF_REPEATS: self.repeats,
            CONF_COALESCE_WINDOW_MS: self.coalesce_window_ms,
            CONF_BASE_UP: f"{self.remote.bases.up:04x}",
            CONF_BASE_DOWN: f"{self.remote.bases.down:04x}",
            CONF_BASE_STOP: f"{self.remote.bases.stop:04x}",
        }
        if self.remote.bases.trailer is not None:
            values[CONF_BASE_TRAILER] = f"{self.remote.bases.trailer:04x}"
        return values

    @property
    def is_group(self) -> bool:
        """Return whether this device addresses more than one motor channel."""
        return len(self.channels) > 1

    @property
    def remote_key(self) -> str:
        """Return the shared remote identity key."""
        return self.remote.key

    @property
    def target_key(self) -> str:
        """Return the bridge scheduler's canonical RF target key."""
        return self.remote.target_key(self.channels)


@dataclass(frozen=True, slots=True)
class BridgeInfo:
    """Retained discovery state for one ESPHome bridge beacon."""

    bridge_id: str
    area_id: str | None = None
    online: bool = False
    is_default: bool = False


class BridgeRegistry:
    """Track retained bridge availability/info and resolve one TX target."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._bridges: dict[str, BridgeInfo] = {}

    @property
    def bridges(self) -> tuple[BridgeInfo, ...]:
        """Return a stable snapshot ordered by bridge id."""
        return tuple(self._bridges[key] for key in sorted(self._bridges))

    def update_availability(self, bridge_id: str, payload: str) -> None:
        """Apply a retained LWT availability message."""
        bridge_id = bridge_id.strip()
        if not bridge_id:
            return
        current = self._bridges.get(bridge_id, BridgeInfo(bridge_id))
        self._bridges[bridge_id] = BridgeInfo(
            bridge_id=bridge_id,
            area_id=current.area_id,
            online=payload.strip().lower() == "online",
            is_default=current.is_default,
        )

    def update_info(self, bridge_id: str, payload: Mapping[str, object]) -> None:
        """Apply retained bridge metadata, including its HA area tag."""
        bridge_id = bridge_id.strip()
        if not bridge_id:
            return
        current = self._bridges.get(bridge_id, BridgeInfo(bridge_id))
        raw_area = payload.get("area_id", payload.get("area"))
        area_id = str(raw_area).strip() if raw_area is not None else current.area_id
        if not area_id:
            area_id = None
        raw_default = payload.get("default", current.is_default)
        is_default = (
            raw_default.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(raw_default, str)
            else bool(raw_default)
        )
        self._bridges[bridge_id] = BridgeInfo(
            bridge_id=bridge_id,
            area_id=area_id,
            online=current.online,
            is_default=is_default,
        )

    def resolve(self, area_id: str) -> BridgeInfo:
        """Choose one online bridge: same area, default, then deterministic fallback."""
        online = [bridge for bridge in self.bridges if bridge.online]
        same_area = [bridge for bridge in online if bridge.area_id == area_id]
        if same_area:
            return same_area[0]
        defaults = [bridge for bridge in online if bridge.is_default]
        if defaults:
            return defaults[0]
        if online:
            return online[0]
        msg = "no RF433 bridge is online"
        raise NoOnlineBridgeError(msg)

    def online_bridge(self, bridge_id: str) -> BridgeInfo:
        """Resolve a specific online bridge for the debug raw service."""
        bridge = self._bridges.get(bridge_id)
        if bridge is None or not bridge.online:
            msg = f"RF433 bridge {bridge_id!r} is not online"
            raise NoOnlineBridgeError(msg)
        return bridge


@dataclass(frozen=True, slots=True)
class _BridgeStatus:
    """One accepted/rejected result correlated by bridge and command ID."""

    status: CommandStatusValue
    acknowledged_at: float
    reason: str | None = None


@dataclass(slots=True)
class _PendingStatuses:
    """The two lightweight lifecycle waiters for one correlated command."""

    admission: asyncio.Future[_BridgeStatus]
    started: asyncio.Future[float]


@dataclass(frozen=True, slots=True)
class CommandAck:
    """Correlated bridge admission and actual first RF dispatch."""

    bridge: BridgeInfo
    command_id: str
    acknowledged_at: float
    started_at: float
    deadline: float | None


type CommandResult = CommandAck | Literal["superseded"]


@dataclass(slots=True)
class _QueuedCommand:
    """One unpublished command waiting for the hub's global worker."""

    target: str
    area_id: str | None
    bridge_id: str | None
    body: dict[str, object]
    stop_after_ms: int | None
    is_movement: bool
    is_stop: bool
    coalesce_key: tuple[RemoteIdentity, Button] | None
    coalesce_config: BlindConfig | None
    coalesce_button: Button | None
    enqueued_at: float
    coalesce_deadline: float | None
    futures: list[asyncio.Future[CommandResult]]


class ZemismartHub:
    """Publish and await first RF dispatch for one command at a time globally."""

    def __init__(
        self,
        registry: BridgeRegistry,
        publisher: Publisher,
        *,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT_SECONDS,
        started_timeout: float = DEFAULT_STARTED_TIMEOUT_SECONDS,
        command_id_factory: CommandIdFactory | None = None,
        now: Clock = time.time,
    ) -> None:
        """Initialize the global command queue and correlated status transport."""
        if ack_timeout <= 0 or started_timeout <= 0:
            msg = "ack_timeout and started_timeout must be greater than zero"
            raise ValueError(msg)
        self.registry = registry
        self._publisher = publisher
        self._ack_timeout = ack_timeout
        self._started_timeout = started_timeout
        self._command_id_factory = command_id_factory or (lambda: uuid.uuid4().hex)
        self._now = now
        self._pending: dict[tuple[str, str], _PendingStatuses] = {}
        self._queue: deque[_QueuedCommand] = deque()
        self._queue_ready = asyncio.Condition()
        self._worker_task: asyncio.Task[None] | None = None

    def _new_command_id(self) -> str:
        """Allocate a non-empty command ID suitable for status correlation."""
        command_id = self._command_id_factory().strip()
        if not command_id:
            msg = "command_id factory returned an empty value"
            raise ValueError(msg)
        return command_id

    def handle_status(
        self,
        bridge_id: str,
        payload: str | bytes | bytearray | Mapping[str, object],
    ) -> bool:
        """Resolve only a correlated admission or first-dispatch status."""
        decoded: object
        if isinstance(payload, bytes | bytearray):
            try:
                decoded = json.loads(payload.decode())
            except UnicodeDecodeError, json.JSONDecodeError:
                return False
        elif isinstance(payload, str):
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                return False
        else:
            decoded = payload
        if not isinstance(decoded, Mapping):
            return False
        raw_status = decoded.get("status")
        command_id = decoded.get("command_id")
        if (
            raw_status not in {"accepted", "rejected", "started"}
            or not isinstance(command_id, str)
            or not command_id
        ):
            return False
        pending = self._pending.get((bridge_id, command_id))
        if pending is None:
            return False
        if raw_status == "started":
            if pending.started.done():
                return False
            pending.started.set_result(self._now())
            return True
        if pending.admission.done():
            return False
        raw_reason = decoded.get("reason")
        pending.admission.set_result(
            _BridgeStatus(
                status=cast("CommandStatusValue", raw_status),
                acknowledged_at=self._now(),
                reason=str(raw_reason) if raw_reason is not None else None,
            )
        )
        return True

    def _ensure_worker(self) -> None:
        """Start the one queue worker lazily on the current event loop."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._async_worker(),
                name="Zemismart global command worker",
            )

    async def _async_enqueue(self, command: _QueuedCommand) -> CommandResult:
        """Queue a command, giving STOP front priority and same-cover supersession."""
        future = command.futures[0]
        async with self._queue_ready:
            if command.is_stop:
                retained: deque[_QueuedCommand] = deque()
                while self._queue:
                    queued = self._queue.popleft()
                    if queued.target == command.target and queued.is_movement:
                        for queued_future in queued.futures:
                            if not queued_future.done():
                                queued_future.set_result("superseded")
                    else:
                        retained.append(queued)
                self._queue = retained
                self._queue.appendleft(command)
            else:
                self._queue.append(command)
            self._ensure_worker()
            self._queue_ready.notify()
        try:
            return await future
        except asyncio.CancelledError:
            async with self._queue_ready:
                self._queue_ready.notify()
            raise

    async def _async_pop(self) -> _QueuedCommand:
        """Wait for the next command and union one expired movement batch."""
        async with self._queue_ready:
            while True:
                while not self._queue:
                    await self._queue_ready.wait()
                while self._queue and all(future.done() for future in self._queue[0].futures):
                    self._queue.popleft()
                if not self._queue:
                    continue
                command = self._queue[0]
                if command.coalesce_deadline is None:
                    return self._queue.popleft()
                for queued in self._queue:
                    if (
                        queued.coalesce_key == command.coalesce_key
                        and queued.enqueued_at <= command.coalesce_deadline
                        and queued.stop_after_ms == command.stop_after_ms
                        and queued.coalesce_deadline is not None
                        and any(not future.done() for future in queued.futures)
                    ):
                        command.coalesce_deadline = min(
                            command.coalesce_deadline,
                            queued.coalesce_deadline,
                        )
                remaining = command.coalesce_deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._queue_ready.wait(), timeout=remaining)
                    continue
                command = self._queue.popleft()
                self._coalesce_queued_movements(command)
                return command

    def _coalesce_queued_movements(self, command: _QueuedCommand) -> None:
        """Absorb eligible siblings that arrived within the first command's window."""
        if (
            command.coalesce_key is None
            or command.coalesce_config is None
            or command.coalesce_button is None
            or command.coalesce_deadline is None
        ):
            return
        channels = set(command.coalesce_config.channels)
        repeats = command.coalesce_config.repeats
        retained: deque[_QueuedCommand] = deque()
        merged = False
        while self._queue:
            queued = self._queue.popleft()
            if all(future.done() for future in queued.futures):
                continue
            if (
                queued.coalesce_key == command.coalesce_key
                and queued.enqueued_at <= command.coalesce_deadline
                and queued.stop_after_ms == command.stop_after_ms
                and queued.coalesce_config is not None
            ):
                channels.update(queued.coalesce_config.channels)
                repeats = max(repeats, queued.coalesce_config.repeats)
                command.futures.extend(queued.futures)
                merged = True
            else:
                retained.append(queued)
        self._queue = retained
        if not merged:
            return
        config = replace(
            command.coalesce_config,
            channels=tuple(sorted(channels)),
            repeats=repeats,
        )
        command.target = config.target_key
        command.body = self._command_body(
            config,
            command.coalesce_button,
            stop_after_ms=command.stop_after_ms,
        )

    async def _async_worker(self) -> None:
        """Resolve, publish, and await one command before popping another."""
        try:
            while True:
                command = await self._async_pop()
                if all(future.done() for future in command.futures):
                    continue
                try:
                    result = await self._async_execute(command)
                except asyncio.CancelledError:
                    for future in command.futures:
                        future.cancel()
                    raise
                except Exception as exc:
                    for future in command.futures:
                        if not future.done():
                            future.set_exception(exc)
                else:
                    for future in command.futures:
                        if not future.done():
                            future.set_result(result)
        finally:
            self._worker_task = None

    def _register_pending(
        self,
        bridge: BridgeInfo,
        command_id: str,
    ) -> _PendingStatuses:
        """Register admission and start correlation before MQTT publication."""
        key = (bridge.bridge_id, command_id)
        if key in self._pending:
            msg = f"duplicate pending command_id {command_id!r} for {bridge.bridge_id!r}"
            raise ValueError(msg)
        loop = asyncio.get_running_loop()
        pending = _PendingStatuses(loop.create_future(), loop.create_future())
        self._pending[key] = pending
        return pending

    async def _await_status(
        self,
        future: asyncio.Future[_BridgeStatus],
        command_id: str,
    ) -> _BridgeStatus:
        """Await the one accepted/rejected result with a fixed bound."""
        try:
            status = await asyncio.wait_for(future, timeout=self._ack_timeout)
        except TimeoutError as exc:
            msg = f"bridge acknowledgement timed out for command {command_id}"
            raise CommandAckTimeoutError(msg) from exc
        if status.status == "rejected":
            detail = f": {status.reason}" if status.reason else ""
            msg = f"bridge rejected command {command_id}{detail}"
            raise CommandRejectedError(msg)
        return status

    async def _await_started(
        self,
        future: asyncio.Future[float],
        command_id: str,
    ) -> float:
        """Await actual first RF dispatch with a scheduler-sized fixed bound."""
        try:
            return await asyncio.wait_for(future, timeout=self._started_timeout)
        except TimeoutError as exc:
            msg = f"bridge RF start timed out for command {command_id}"
            raise CommandStartedTimeoutError(msg) from exc

    async def _async_execute(self, command: _QueuedCommand) -> CommandAck:
        """Resolve, publish, then await admission and first RF dispatch."""
        if command.bridge_id is not None:
            bridge = self.registry.online_bridge(command.bridge_id)
        else:
            assert command.area_id is not None
            bridge = self.registry.resolve(command.area_id)
        command_id = self._new_command_id()
        body = dict(command.body)
        body["command_id"] = command_id
        pending = self._register_pending(bridge, command_id)
        key = (bridge.bridge_id, command_id)
        try:
            await self._publisher(
                f"{MQTT_ROOT}/{bridge.bridge_id}/tx",
                json.dumps(body, separators=(",", ":")),
            )
            status = await self._await_status(pending.admission, command_id)
            started_at = await self._await_started(pending.started, command_id)
        finally:
            self._pending.pop(key, None)
        deadline = (
            started_at + command.stop_after_ms / 1_000
            if command.stop_after_ms is not None
            else None
        )
        return CommandAck(
            bridge=bridge,
            command_id=command_id,
            acknowledged_at=status.acknowledged_at,
            started_at=started_at,
            deadline=deadline,
        )

    @staticmethod
    def _frame(config: BlindConfig, button: Button) -> str:
        """Generate and transport-validate one protocol frame."""
        assert config.remote.bases is not None
        return validate_b0_frame(
            encode_b0(
                make_payload(
                    config.remote.prefix,
                    config.remote.remote_id,
                    config.channels,
                    button,
                    bases=config.remote.bases,
                )
            )
        )

    @classmethod
    def _command_body(
        cls,
        config: BlindConfig,
        button: Button,
        *,
        stop_after_ms: int | None,
    ) -> dict[str, object]:
        """Build one validated firmware command body for a cover target."""
        body: dict[str, object] = {
            "target": config.target_key,
            "raw": cls._frame(config, button),
            "repeats": config.repeats,
        }
        if (
            button in {"UP", "DOWN"}
            and config.remote.bases is not None
            and config.remote.bases.trailer is not None
        ):
            body["trailer_raw"] = cls._frame(config, "TRAILER")
        if stop_after_ms is not None:
            body["stop_after_ms"] = stop_after_ms
            body["stop_raw"] = cls._frame(config, "STOP")
        return body

    async def async_transmit(
        self,
        config: BlindConfig,
        button: Button,
        *,
        stop_after_ms: int | None = None,
    ) -> CommandResult:
        """Queue one validated cover command and await its result."""
        if stop_after_ms is not None and stop_after_ms <= 0:
            msg = "stop_after_ms must be greater than zero"
            raise ValueError(msg)
        body = self._command_body(config, button, stop_after_ms=stop_after_ms)
        loop = asyncio.get_running_loop()
        enqueued_at = loop.time()
        coalesces = (
            button in {"UP", "DOWN"} and not config.is_group and config.coalesce_window_ms > 0
        )
        return await self._async_enqueue(
            _QueuedCommand(
                target=config.target_key,
                area_id=config.area_id,
                bridge_id=None,
                body=body,
                stop_after_ms=stop_after_ms,
                is_movement=button in {"UP", "DOWN"},
                is_stop=button == "STOP",
                coalesce_key=(config.remote, button) if coalesces else None,
                coalesce_config=config if coalesces else None,
                coalesce_button=button if coalesces else None,
                enqueued_at=enqueued_at,
                coalesce_deadline=(
                    enqueued_at + config.coalesce_window_ms / 1_000 if coalesces else None
                ),
                futures=[loop.create_future()],
            )
        )

    async def async_send_raw(self, bridge_id: str, raw: str, repeats: int) -> CommandAck:
        """Queue an acknowledged debug B0 frame for one explicitly named bridge."""
        if not MIN_REPEATS <= repeats <= MAX_REPEATS:
            msg = f"repeats must be in the range {MIN_REPEATS}..{MAX_REPEATS}"
            raise ValueError(msg)
        normalized = validate_b0_frame(raw)
        decoded = decode_b0(normalized)
        remote = RemoteIdentity(decoded["prefix"], decoded["remote_id"])
        decoded_channels = cast("Iterable[int]", decoded["chans"])
        target = remote.target_key(decoded_channels)
        result = await self._async_enqueue(
            _QueuedCommand(
                target=target,
                area_id=None,
                bridge_id=bridge_id,
                body={"target": target, "raw": normalized, "repeats": repeats},
                stop_after_ms=None,
                is_movement=False,
                is_stop=False,
                coalesce_key=None,
                coalesce_config=None,
                coalesce_button=None,
                enqueued_at=asyncio.get_running_loop().time(),
                coalesce_deadline=None,
                futures=[asyncio.get_running_loop().create_future()],
            )
        )
        if result == "superseded":
            raise AssertionError("raw commands cannot be superseded")
        return result

    def close(self) -> None:
        """Cancel the worker and all queued or in-flight waiters on final unload."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            self._worker_task = None
        for command in self._queue:
            for future in command.futures:
                future.cancel()
        self._queue.clear()
        for pending in self._pending.values():
            pending.admission.cancel()
            pending.started.cancel()
        self._pending.clear()


@dataclass(slots=True)
class EntryRuntime:
    """Runtime data owned by one blind/group config entry."""

    config: BlindConfig
    hub: ZemismartHub


@dataclass(slots=True)
class DomainRuntime:
    """MQTT registry/subscriptions shared by every config entry."""

    hub: ZemismartHub
    unsubscribers: list[Unsubscriber]
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loaded_entries: set[str] = field(default_factory=set)
    initialized: bool = False
    setup_users: int = 0
