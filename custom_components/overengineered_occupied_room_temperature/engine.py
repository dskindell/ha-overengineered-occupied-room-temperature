"""Weighting, smoothing and averaging rules for OORT.

This module has no Home Assistant imports so the rules can be unit-tested
directly. The runtime gathers inputs from Home Assistant, calls these
functions, and writes the results to entities.

Units: timestamps and elapsed times are in seconds; taus and the stale limit
are in minutes, matching the configuration.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
import math


class Status(StrEnum):
    """The state driving a room's weight, in priority order."""

    INACTIVE = "inactive"
    PERSON = "person"
    OCCUPIED = "occupied"
    UNOCCUPIED = "unoccupied"


class TauName(StrEnum):
    """The six taus; values match the ``Taus`` field names."""

    PERSON_RISE = "person_rise"
    PERSON_FALL = "person_fall"
    OCCUPANCY_RISE = "occupancy_rise"
    OCCUPANCY_FALL = "occupancy_fall"
    DEACTIVATE = "deactivate"
    DROPOUT = "dropout"


@dataclass(frozen=True, slots=True)
class Taus:
    """Time constants in minutes. A tau of 0 means the target is reached immediately."""

    person_rise: float = 3.0
    person_fall: float = 3.0
    occupancy_rise: float = 10.0
    occupancy_fall: float = 8.0
    deactivate: float = 1.0
    dropout: float = 5.0


@dataclass(frozen=True, slots=True)
class Weights:
    """Target weights for each status (inactive is always 0)."""

    person: float = 1.0
    occupied: float = 0.5
    base: float = 0.001


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """A room's effective settings: its overrides merged over the instance defaults."""

    taus: Taus = field(default_factory=Taus)
    weights: Weights = field(default_factory=Weights)
    stale_limit: float = 60.0


@dataclass(frozen=True, slots=True)
class RoomInputs:
    """A room's inputs at one moment, already evaluated by the runtime."""

    active: bool
    """Result of the active template; True when the room has none."""
    person_present: bool
    occupied: bool
    temperature: float | None
    """Current reading converted to the system unit, or None if unavailable or unconvertible."""


@dataclass(frozen=True, slots=True)
class RoomState:
    """Everything carried from one update to the next (and restored after a restart)."""

    weight: float = 0.0
    target: float = 0.0
    tau: float = 0.0
    """Tau (minutes) used while approaching ``target``."""
    tau_name: TauName | None = None
    """Which tau that is; None before the first update."""
    status: Status = Status.UNOCCUPIED
    last_occupied_state: Status | None = None
    last_known_temperature: float | None = None
    dropout_since: float | None = None
    stale: bool = False
    last_update: float | None = None

    @property
    def usable_temperature(self) -> float | None:
        """Temperature to average with: the last known reading unless it has gone stale."""
        return None if self.stale else self.last_known_temperature


def approach(weight: float, target: float, tau: float, elapsed: float) -> float:
    """Move ``weight`` toward ``target`` exponentially over ``elapsed`` seconds."""
    if elapsed <= 0:
        return weight
    if tau <= 0:
        return target
    return target + (weight - target) * math.exp(-elapsed / (tau * 60))


def room_status(inputs: RoomInputs) -> Status:
    """Pick the status driving the weight: inactive, then person, then occupied."""
    if not inputs.active or inputs.temperature is None:
        return Status.INACTIVE
    if inputs.person_present:
        return Status.PERSON
    if inputs.occupied:
        return Status.OCCUPIED
    return Status.UNOCCUPIED


def target_weight(status: Status, weights: Weights) -> float:
    """Weight a room heads toward for a given status."""
    return {
        Status.INACTIVE: 0.0,
        Status.PERSON: weights.person,
        Status.OCCUPIED: weights.occupied,
        Status.UNOCCUPIED: weights.base,
    }[status]


def select_tau_name(
    status: Status,
    inputs: RoomInputs,
    last_occupied_state: Status | None,
    *,
    previous_status: Status | None = None,
    previous_tau_name: TauName | None = None,
) -> TauName:
    """Which tau approaches the target for ``status``.

    Falls use the room's last non-empty state rather than its current weight, so
    the choice is correct whatever order the weights are configured in. A room a
    tracked person has just left, but whose occupancy sensor is still on, is
    falling from ``person``: it uses person fall for as long as it stays
    ``occupied``.
    """
    if status is Status.PERSON:
        return TauName.PERSON_RISE
    if status is Status.OCCUPIED:
        if previous_status is Status.PERSON or (
            previous_status is Status.OCCUPIED and previous_tau_name is TauName.PERSON_FALL
        ):
            return TauName.PERSON_FALL
        return TauName.OCCUPANCY_RISE
    if status is Status.UNOCCUPIED:
        if last_occupied_state is Status.PERSON:
            return TauName.PERSON_FALL
        return TauName.OCCUPANCY_FALL
    # Deactivation takes precedence over a sensor dropout.
    return TauName.DEACTIVATE if not inputs.active else TauName.DROPOUT


def select_tau(
    status: Status,
    inputs: RoomInputs,
    last_occupied_state: Status | None,
    taus: Taus,
    *,
    previous_status: Status | None = None,
    previous_tau_name: TauName | None = None,
) -> float:
    """Tau (minutes) used to approach the target for ``status``."""
    name = select_tau_name(
        status,
        inputs,
        last_occupied_state,
        previous_status=previous_status,
        previous_tau_name=previous_tau_name,
    )
    tau: float = getattr(taus, name)
    return tau


def step_room(
    state: RoomState,
    inputs: RoomInputs,
    config: RoomConfig,
    now: float,
    *,
    hold: bool = False,
) -> RoomState:
    """Advance a room to ``now`` and apply its latest inputs.

    Smoothing is piecewise: the time since the last update is applied toward the
    target that was in effect during that time, and only then is the new target
    chosen. ``hold`` (the startup grace period) freezes the weight but still
    tracks inputs, status and dropouts.
    """
    elapsed = 0.0
    if not hold and state.last_update is not None:
        elapsed = max(0.0, now - state.last_update)
    weight = approach(state.weight, state.target, state.tau, elapsed)

    last_known_temperature: float | None
    if inputs.temperature is not None:
        last_known_temperature = inputs.temperature
        dropout_since = None
        stale = False
    else:
        last_known_temperature = state.last_known_temperature
        dropout_since = now if state.dropout_since is None else state.dropout_since
        stale = now - dropout_since > config.stale_limit * 60

    status = room_status(inputs)
    last_occupied_state = (
        status if status in (Status.PERSON, Status.OCCUPIED) else state.last_occupied_state
    )

    tau_name = select_tau_name(
        status,
        inputs,
        last_occupied_state,
        previous_status=state.status,
        previous_tau_name=state.tau_name,
    )
    return RoomState(
        weight=weight,
        target=target_weight(status, config.weights),
        tau=getattr(config.taus, tau_name),
        tau_name=tau_name,
        status=status,
        last_occupied_state=last_occupied_state,
        last_known_temperature=last_known_temperature,
        dropout_since=dropout_since,
        stale=stale,
        last_update=now,
    )


def fallback_epsilon(base_weights: Iterable[float]) -> float:
    """Weight of the plain-average term: 1% of the smallest room base weight."""
    return 0.01 * min(base_weights)


@dataclass(frozen=True, slots=True)
class Aggregate:
    """The instance's weighted temperature and its attributes."""

    temperature: float | None
    """None means the sensor is unavailable: nothing could be averaged."""
    total_weight: float
    contributing_rooms: int
    fallback: bool


def aggregate(
    rooms: Iterable[tuple[float, float | None]],
    current_temperatures: Iterable[float],
    epsilon: float,
) -> Aggregate:
    """Blend the rooms' weighted average with a plain-average fallback.

    ``rooms`` holds each room's ``(weight, usable_temperature)``;
    ``current_temperatures`` holds every room's current valid reading, whether
    or not the room is active. The plain average always takes part with weight
    ``epsilon``: negligible while any room is active, and dominant as every
    room fades toward 0.
    """
    weighted_sum = 0.0
    total_weight = 0.0
    contributing_rooms = 0
    for weight, temperature in rooms:
        if temperature is None:
            continue
        weighted_sum += temperature * weight
        total_weight += weight
        contributing_rooms += 1

    numerator, denominator = weighted_sum, total_weight
    current = list(current_temperatures)
    if current:
        numerator += epsilon * (sum(current) / len(current))
        denominator += epsilon

    return Aggregate(
        temperature=numerator / denominator if denominator > 0 else None,
        total_weight=total_weight,
        contributing_rooms=contributing_rooms,
        fallback=bool(current) and epsilon > total_weight,
    )
