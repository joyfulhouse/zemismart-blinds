"""Aggregate cover entity for Zemismart Blinds."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.cover import ATTR_POSITION
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from . import cover as cover_module
from .const import DOMAIN, ENDPOINT_OPEN
from .models import BlindConfig, Button, CommandAck, TakeoverCoverState, ZemismartHub

if TYPE_CHECKING:
    from .coordinator import MemberCover, RemoteCoordinator
    from .state_sync import HeardEvent

from .cover import (
    _ATTR_POSITION_CONFIDENCE,
    _COMMAND_TIMEOUT_FAILURES,
    _LOGGER,
    _TRANSPORT_FAILURES,
    _UNTIMED_DISARM_DRAIN_SECONDS,
    CONFIDENCE_ANCHORED,
    CONFIDENCE_ASSUMED,
    CONFIDENCE_SUSPECT,
    CONFIDENCE_UNKNOWN,
    ZemismartCover,
    _failure_translation_key,
    _MotionStart,
    _rf_reachable,
    _ZemismartCoverEntity,
)

__all__ = ["ZemismartAggregateCover"]


class ZemismartAggregateCover(_ZemismartCoverEntity):
    """A cover whose state derives from its leaf members.

    RF behavior matches the retired group entries: open/close/stop transmit
    ONE frame addressed to the full channel set; position commands fan out to
    each member's own timed positioning. The aggregate owns no position model
    of its own — members are the single source of truth.

    Its channel set and the channels it can DERIVE state for are therefore two
    different things. Every frame it sends addresses the full configured set,
    including channels no cover is configured for; the state it publishes
    covers only the channels its configured members model. See
    `_modelled_channels`.
    """

    def __init__(
        self,
        cover_id: str,
        remote_entry_id: str,
        config: BlindConfig,
        hub: ZemismartHub,
        coordinator: RemoteCoordinator,
    ) -> None:
        """Initialize one aggregate bound to its coordinator topology."""
        super().__init__(cover_id, remote_entry_id, config, hub)
        self._coordinator = coordinator
        self._last_command_bridge: str | None = None
        self._last_command_id: str | None = None
        self._last_command_button: Button | None = None
        self._last_command_at_monotonic = 0.0
        self._fanout_tasks: set[asyncio.Task[None]] = set()

    async def async_added_to_hass(self) -> None:
        """Register with the coordinator and the hub's takeover machinery."""
        await super().async_added_to_hass()
        # Registered before anything is acquired, for the reason spelled out on
        # ZemismartCover.async_added_to_hass: HA runs these on a failed add too,
        # which async_will_remove_from_hass() never sees.
        self.async_on_remove(self._release_registrations)
        self.async_on_remove(self._cancel_fanout)
        self._coordinator.register_aggregate(self._cover_id, self)
        self._register_rx_listener()
        self._unsubscribe_mqtt_status = cover_module._subscribe_rf_reachability(self.hass, self)

    @callback
    def _release_registrations(self) -> None:
        """Drop every hub and coordinator registration this aggregate holds.

        Idempotent: it runs from the removal path below and from the
        async_on_remove() callbacks.
        """
        if self._unsubscribe_rx_listener is not None:
            self._unsubscribe_rx_listener()
            self._unsubscribe_rx_listener = None
        if self._unsubscribe_mqtt_status is not None:
            self._unsubscribe_mqtt_status()
            self._unsubscribe_mqtt_status = None
        self._coordinator.unregister_aggregate(self._cover_id)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister and cancel any in-flight fan-out."""
        self._release_registrations()
        self._cancel_fanout()
        await super().async_will_remove_from_hass()

    def _members(self) -> tuple[ZemismartCover, ...]:
        """Return the live leaf entities this aggregate derives from."""
        return cast(
            "tuple[ZemismartCover, ...]",
            self._coordinator.members_of(self._cover_id),
        )

    def _failure_members(
        self,
        issued_members: tuple[ZemismartCover, ...],
    ) -> tuple[ZemismartCover, ...]:
        """Return every leaf a failed group frame could have moved.

        The union of the members present when the frame was issued and those
        present now. A leaf that deregistered mid-flight is gone from
        `_members()` but its channels were still addressed; a leaf that joined
        mid-flight was never in the snapshot but is addressed too. Order is kept
        stable and duplicates dropped.
        """
        seen: dict[int, ZemismartCover] = {}
        for member in (*issued_members, *self._members()):
            seen.setdefault(id(member), member)
        return tuple(seen.values())

    @callback
    def _invalidate_failure_members(
        self,
        issued_members: tuple[ZemismartCover, ...],
        invalidated_members: tuple[MemberCover, ...],
    ) -> None:
        """Isolate fallback invalidations for members outside live dispatch."""
        for member in self._failure_members(issued_members):
            if member in invalidated_members:
                continue
            try:
                member.invalidate_for_cancelled_command()
            except Exception:
                # The coordinator recorded every topology marker first. A
                # failed write keeps this member's marker available to restore
                # while fallback continues through the remaining members.
                _LOGGER.exception(
                    "Failed to invalidate position for %s",
                    member.entity_id,
                )

    def _modelled_channels(self) -> frozenset[int]:
        """Return our channels that some CONFIGURED member cover models.

        A channel outside this set is UNMODELLED: no cover on the remote claims
        it, so nothing in Home Assistant knows its travel time, its position or
        whether it is closed. The physical group still contains it -- every
        open/close/stop frame this aggregate sends is addressed to the full
        configured channel set, unchanged -- but it contributes nothing to the
        derived state, because there is nothing to contribute.
        """
        return self._coordinator.modelled_channels(self._cover_id) & frozenset(
            self._config.channels
        )

    def _unmodelled_channels(self) -> tuple[int, ...]:
        """Return our channels no configured member cover models, ascending."""
        return tuple(sorted(frozenset(self._config.channels) - self._modelled_channels()))

    def _missing_member_channels(self) -> frozenset[int]:
        """Return modelled channels whose configured member is not live now.

        The runtime-safety half of #32. `async_setup_entry` skips any cover
        whose config fails to derive -- a leaf with no travel times, say -- and
        a leaf can deregister mid-flight, so a channel this remote DOES have a
        cover for can still have no entity behind it. Those channels are
        modelled and unaccounted for at once, which is the state the aggregate
        must refuse to derive through.
        """
        covered = frozenset(
            channel for member in self._members() for channel in member._config.channels
        )
        return self._modelled_channels() - covered

    def _members_cover_every_modelled_channel(self) -> bool:
        """Return whether every MODELLED channel has a live member behind it.

        Completeness is judged against the configured members, not against our
        full channel set (#32, narrowed). Both halves matter, and they are not
        the same question:

        * A configured member with no live entity leaves a modelled channel
          unaccounted for. The aggregate then reports no position, no
          `is_closed` and `unknown` confidence -- the original #32 guard, intact.
          Left unchecked it published a confident position, possibly `anchored`,
          for hardware it had no model of, and `set_position` moved the channels
          it could and returned success.
        * A channel NO configured cover claims is unmodelled and disregarded.
          The first guard treated the two alike, so a group over {1..6} with
          covers for only {1..5} -- a legitimate configuration; channel 6 may
          have no blind on it at all -- reported unknown forever, which is not
          caution but silence about the five blinds it does model.
        """
        return not self._missing_member_channels()

    @property
    def available(self) -> bool:
        """Available while RF works and at least one member is registered."""
        return bool(self._members()) and _rf_reachable(self.hass, self._hub)

    @property
    def current_cover_position(self) -> int | None:
        """Return the channel-weighted member mean, or None if any is unknown.

        Unknown members are NOT skipped (#32): averaging the rest produced a
        confident, specific number describing only part of the hardware the
        aggregate models, and that number is what dashboards and automations
        read. `is_closed` already returns None on a mixed state; this matches
        its honesty.

        Weighted by channel count because a member is a motor set, not a vote:
        a leaf covering {1,2} moves twice as much hardware as one covering {3},
        so an unweighted mean reported the midpoint of the two LEAVES rather
        than of the three MOTORS.

        The mean spans the MODELLED channels only. An unmodelled channel has no
        position to average in and no weight to carry, so a group over {1..6}
        with covers for {1..5} reports the mean of those five -- the number a
        user configuring five covers asked for.
        """
        if not self._members_cover_every_modelled_channel():
            return None
        travelled = 0.0
        channels = 0
        for member in self._members():
            position = member.current_cover_position
            if position is None:
                return None
            weight = len(member._config.channels)
            travelled += position * weight
            channels += weight
        if not channels:
            return None
        return round(travelled / channels)

    @property
    def is_opening(self) -> bool:
        """Return whether any member is opening (HA reports opening first)."""
        return any(member.is_opening for member in self._members())

    @property
    def is_closing(self) -> bool:
        """Return whether any member is closing."""
        return any(member.is_closing for member in self._members())

    @property
    def is_closed(self) -> bool | None:
        """Closed iff every member is closed; open if any is open; else unknown."""
        states = [member.is_closed for member in self._members()]
        if not states:
            return None
        # `False` needs no completeness: one member demonstrably open makes the
        # GROUP open whatever the channels we cannot see are doing.
        if any(state is False for state in states):
            return False
        # `True` does. This is the entity's PRIMARY state, so an aggregate over
        # {1,2,3,4} that HAS a cover configured for channel 4 but no entity
        # behind it would otherwise be published to HA as `closed` on the
        # strength of three quarters of the evidence -- the same hole
        # current_cover_position and position_confidence close (#32).
        #
        # A channel no cover is configured for is a different matter: it is
        # outside what this group models at all, so `closed` describes the five
        # blinds the user configured and claims nothing about the sixth.
        if all(state is True for state in states) and self._members_cover_every_modelled_channel():
            return True
        return None

    @property
    def position_confidence(self) -> str:
        """Derive confidence from members -- the worst known value wins.

        One rule throughout: no position means `unknown`. That holds for a leaf
        with no estimate, for a group missing a member it has a cover for, and
        for a group with an unknown member -- because `current_cover_position`
        returns None in every one of those cases.

        The older rule capped this at `assumed` instead, which was right while
        the position was the mean of the members that HAD one. Once #32 made a
        single unknown member withhold the whole position, `assumed` started
        claiming an estimate that no longer existed, and an automation gating on
        `position_confidence != 'unknown'` would act on nothing.

        Once the group has a position, a suspect member still marks it suspect.
        """
        members = list(self._members())
        if not self._members_cover_every_modelled_channel():
            # `unknown`, not `assumed`: current_cover_position returns None in
            # this state, and the leaf's rule is that no position means unknown.
            # Reporting `assumed` implied there was an estimate that merely
            # lacked corroboration, so an automation gating on
            # `position_confidence != 'unknown'` would act on a position that
            # does not exist.
            return CONFIDENCE_UNKNOWN
        if self.current_cover_position is None:
            # Confidence qualifies an estimate. If any member withholds its
            # position, the group has no estimate for a sibling's doubt to
            # qualify, so `unknown` is the only honest label.
            return CONFIDENCE_UNKNOWN
        confidences = [member.position_confidence for member in members]
        if not confidences:
            return CONFIDENCE_UNKNOWN
        # A group position exists, so a member frozen by an uncorroborated heard
        # STOP conservatively makes that derived estimate suspect.
        if CONFIDENCE_SUSPECT in confidences:
            return CONFIDENCE_SUSPECT
        if CONFIDENCE_UNKNOWN in confidences:
            return CONFIDENCE_UNKNOWN
        if CONFIDENCE_ASSUMED in confidences:
            return CONFIDENCE_ASSUMED
        return CONFIDENCE_ANCHORED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose topology metadata for diagnostics and restore discrimination."""
        return {
            "channels": list(self._config.channels),
            # Always present, empty when there are none: the derived state of a
            # group with unmodelled channels describes fewer motors than
            # `channels` lists, and that gap should be readable rather than
            # inferable. A stable key means a template never has to guess
            # whether an absent attribute means "none" or "old version".
            "unmodelled_channels": list(self._unmodelled_channels()),
            "remote": self._config.remote_key,
            "role": self._config.role.value,
            _ATTR_POSITION_CONFIDENCE: self.position_confidence,
        }

    @callback
    def _on_heard_press(self, event: HeardEvent) -> None:
        """Track heard STOPs for takeover; members model their own motion."""
        # Members receive their own callbacks and model the press; the
        # aggregate's displayed state re-derives through the coordinator.
        # Only the takeover flag is aggregate-owned: a physical STOP covering
        # the whole set halted this aggregate's own command, and the hub's
        # takeover truth table must not re-invalidate a heard-stopped cover.
        if event.button == "STOP" and frozenset(self._config.channels) <= event.chans:
            self._stopped_by_heard = True

    def _takeover_state(self) -> TakeoverCoverState:
        """Expose the last single-frame command for hub takeover disarms.

        The command identity EXPIRES after the untimed drain window: the hub
        treats unknown command ids as takeover-live, so reporting a
        long-retired command forever would spawn pointless disarm retries on
        every later physical press.
        """
        expired = (
            cover_module.MONOTONIC_CLOCK()
            > self._last_command_at_monotonic + _UNTIMED_DISARM_DRAIN_SECONDS
        )
        if self._last_command_bridge is None or self._last_command_id is None or expired:
            return TakeoverCoverState(
                bridge_id=None,
                command_id=None,
                button=None,
                disarm_deadline_monotonic=None,
                stopped_by_heard=self._stopped_by_heard,
            )
        return TakeoverCoverState(
            bridge_id=self._last_command_bridge,
            command_id=self._last_command_id,
            button=self._last_command_button,
            disarm_deadline_monotonic=(
                cover_module.MONOTONIC_CLOCK() + _UNTIMED_DISARM_DRAIN_SECONDS
            ),
            stopped_by_heard=self._stopped_by_heard,
        )

    @callback
    def _invalidate_for_takeover(self) -> None:
        """Clear the tracked command after a hub-classified takeover."""
        self._last_command_bridge = None
        self._last_command_id = None
        self._last_command_button = None
        self.async_write_ha_state()

    def _cancel_fanout(self) -> None:
        """Cancel every pending member position delegation."""
        for task in tuple(self._fanout_tasks):
            task.cancel()
        self._fanout_tasks.clear()

    async def _async_transmit(self, button: Button) -> CommandAck | None:
        """Send one untimed full-channel-set frame and record its identity."""
        # Membership is read TWICE and the two are unioned. Neither end alone is
        # right: a leaf registers before awaiting its restore, so one can join
        # while the frame is in flight; and a leaf can deregister on unload
        # during the same window, vanishing from `_members()` while the frame
        # that addressed its channels is already on air. Snapshot-only missed
        # the first, current-only missed the second.
        issued_members = self._members()
        try:
            result = await self._hub.async_transmit(
                self._config,
                button,
                owner=self._remote_entry_id,
            )
        except asyncio.CancelledError:
            # Same hazard as the leaf's transmit (#28), but this frame addresses
            # the WHOLE channel set: if it was already published, every member
            # moved. The aggregate owns no position model, so the members are
            # the only place that loss can be recorded -- and they cannot learn
            # it themselves, because this command was never theirs.
            #
            # Each member's epoch is bumped for the same reason the leaf bumps
            # its own: a member whose restore is still pending would otherwise
            # overwrite this invalidation with its cached position.
            invalidated_members = self._coordinator.record_position_invalidations(
                self._remote_entry_id,
                self._config.channels,
            )
            self._invalidate_failure_members(issued_members, invalidated_members)
            self.async_write_ha_state()
            raise
        except _COMMAND_TIMEOUT_FAILURES as exc:
            # The leaf invalidates itself on a timeout; the aggregate has to do
            # it for its members. The frame MAY have reached RF, and this
            # command was never any member's own, so nothing else records the
            # loss: _async_move_full never starts member tracking and
            # async_stop_cover never freezes it, leaving members integrating
            # through a STOP that may have fired, still reporting `anchored`.
            _LOGGER.warning("Command timed out for %s: %s", self._config.name, exc)
            invalidated_members = self._coordinator.record_position_invalidations(
                self._remote_entry_id,
                self._config.channels,
            )
            self._invalidate_failure_members(issued_members, invalidated_members)
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_timeout",
            ) from exc
        except _TRANSPORT_FAILURES as exc:
            _LOGGER.warning("Command failed for %s: %s", self._config.name, exc)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=_failure_translation_key(exc),
            ) from exc
        if result == "superseded":
            return None
        self._last_command_bridge = result.bridge.bridge_id
        self._last_command_id = result.command_id
        self._last_command_button = button
        self._last_command_at_monotonic = result.started_at_monotonic
        self._stopped_by_heard = False
        return result

    async def _async_move_full(self, button: Button, direction: int, target: float) -> None:
        """Run one group frame and start every member's own motion model."""
        # Snapshot BEFORE the transmit await: a physical press heard while
        # this frame is queued is NEWER intent for that member, and the older
        # group command must not overwrite it (mirrors the leaf-side
        # intent-generation checks around every transmit).
        generations = {member: member._intent_generation for member in self._members()}
        ack = await self._async_transmit(button)
        if ack is None:
            return
        motion = _MotionStart(
            source="commanded",
            started_at=ack.started_at,
            started_at_monotonic=ack.started_at_monotonic,
            deadline=ack.deadline,
            deadline_monotonic=ack.deadline_monotonic,
            bridge_id=ack.bridge.bridge_id,
            command_id=ack.command_id,
        )
        for member in self._members():
            generation = generations.get(member)
            if generation is not None and generation != member._intent_generation:
                continue
            member._start_member_motion(
                motion,
                ack=ack,
                direction=direction,
                duration=0.0,
                group_target=target,
            )
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open every channel with one frame; members model their own travel."""
        del kwargs
        # A full-set command supersedes any in-flight position fan-out: a
        # member's timed frame published after the group frame would displace
        # it channel-by-channel on the bridge.
        self._cancel_fanout()
        async with self._command_lock:
            await self._async_move_full("UP", 1, 100.0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close every channel with one frame; members model their own travel."""
        del kwargs
        self._cancel_fanout()
        async with self._command_lock:
            await self._async_move_full("DOWN", -1, 0.0)

    async def async_reanchor(self, endpoint: str) -> None:
        """Re-anchor the whole group with one endpoint frame (recovery).

        Reuses the aggregate's own full open/close, so it is exactly one group
        frame and every member re-anchors through its own outcome-based logic --
        identical to a manual full travel on the aggregate.
        """
        if endpoint == ENDPOINT_OPEN:
            await self.async_open_cover()
        else:
            await self.async_close_cover()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Cancel fan-out, stop every channel with one frame, freeze members."""
        del kwargs
        self._cancel_fanout()
        async with self._command_lock:
            generations = {member: member._intent_generation for member in self._members()}
            ack = await self._async_transmit("STOP")
            if ack is None:
                return
            for member in self._members():
                generation = generations.get(member)
                if generation is not None and generation != member._intent_generation:
                    # A press heard during the STOP await is newer intent for
                    # this member; its own heard model wins the freeze.
                    continue
                member._record_ack(ack)
                member._apply_stop(
                    ack.started_at_monotonic,
                    provenance="commanded",
                )
            self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Delegate a position move to every member's own timed positioning."""
        target = max(0, min(100, int(kwargs[ATTR_POSITION])))
        if target == 0:
            await self.async_close_cover()
            return
        if target == 100:
            await self.async_open_cover()
            return
        registered = self._members()
        members = [member for member in registered if member.available]
        skipped = [member for member in registered if not member.available]
        if not members:
            names = ", ".join(member._config.name for member in skipped) or "none"
            # Nothing failed: every member is simply unreachable right now.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_members_available",
                translation_placeholders={"unavailable": names},
            )
        # Nor can a group missing one of its own covers be positioned: fanning
        # out to the members we have would move some of the channels this
        # aggregate models and leave the rest, then report success (#32).
        #
        # Unmodelled channels are not that case and do not block the move. No
        # cover claims them, so no fan-out was ever going to address them and
        # nothing here can position them; the group positions what it models,
        # exactly as it reports what it models.
        if missing := self._missing_member_channels():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="aggregate_incomplete",
                translation_placeholders={
                    "missing": ", ".join(str(channel) for channel in sorted(missing))
                },
            )
        # PREFLIGHT before any frame reaches the air (#32). A member with no
        # estimate cannot be positioned, and discovering that inside the
        # fan-out meant reporting failure AFTER part of the group had
        # physically moved -- a state neither the caller nor the model
        # intended, and one that makes a retry hazardous: the already-moved
        # members would move again from their new positions.
        #
        # It cannot close the window entirely -- a heard press can invalidate a
        # member between this check and its frame -- but that member then fails
        # in delegate() as before, which is the narrow residual, not the whole
        # class.
        unpositionable = [member for member in members if member.current_cover_position is None]
        if unpositionable:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="member_position_unknown",
                translation_placeholders={
                    "members": ", ".join(member._config.name for member in unpositionable)
                },
            )
        failures: list[str] = []

        async def delegate(member: ZemismartCover) -> None:
            try:
                await member.async_set_member_position(target)
            except HomeAssistantError as exc:
                # The member NAME only. Interpolating `exc` rendered a
                # translated HomeAssistantError to its English fallback and
                # then nested that inside another translated message, so a
                # non-English user got English text either way (#37). The
                # detail is logged instead.
                _LOGGER.warning("Positioning failed for %s: %s", member._config.name, exc)
                failures.append(member._config.name)

        tasks = [
            self.hass.async_create_task(
                delegate(member),
                f"Zemismart {self._config.name} fan-out",
            )
            for member in members
        ]
        self._fanout_tasks.update(tasks)
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._fanout_tasks.difference_update(tasks)
        # Every gather result is inspected (#33). `return_exceptions=True` is
        # required here -- one member failing must not abandon the others
        # mid-fan-out -- but the results used to be discarded, so a TypeError
        # or AttributeError anywhere in a member's command path produced no
        # traceback, no service failure, and a group where some blinds moved
        # and some did not. delegate() collects only HomeAssistantError;
        # anything else reaching this list is by definition unexpected.
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                # Preserve #28's semantics: the cancelled member already marked
                # itself unknown, and cancellation must propagate as
                # cancellation rather than be reported as a member failure.
                raise result
        for result in results:
            if isinstance(result, BaseException):
                # Re-raised, not wrapped: the original traceback is the whole
                # point of surfacing it.
                raise result
        if failures:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="position_delegation_failed",
                translation_placeholders={"members": ", ".join(failures)},
            )
