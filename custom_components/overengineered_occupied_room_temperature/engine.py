"""Weighting, smoothing and averaging rules for OORT.

This module has no Home Assistant imports so the rules can be unit-tested
directly. The runtime gathers inputs from Home Assistant, calls these
functions, and writes the results to entities.

Units: timestamps and elapsed times are in seconds; taus and the stale limit
are in minutes, matching the configuration.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
import math


class Status(StrEnum):
    """The state driving a room's weight, in priority order.

    ``open`` and ``dropout`` both leave the room out (target 0); an open room that
    has also dropped out is ``open``.
    """

    OPEN = "open"
    """An opening entity is on or the opening template is true."""
    DROPOUT = "dropout"
    """The temperature sensor has no usable reading."""
    PERSON = "person"
    OCCUPIED = "occupied"
    UNOCCUPIED = "unoccupied"


class TauName(StrEnum):
    """The six taus; values match the ``Taus`` field names."""

    PERSON_RISE = "person_rise"
    PERSON_FALL = "person_fall"
    OCCUPANCY_RISE = "occupancy_rise"
    OCCUPANCY_FALL = "occupancy_fall"
    OPEN = "open"
    DROPOUT = "dropout"


@dataclass(frozen=True, slots=True)
class Taus:
    """Time constants in minutes. A tau of 0 means the target is reached immediately."""

    person_rise: float
    person_fall: float
    occupancy_rise: float
    occupancy_fall: float
    open: float
    dropout: float


@dataclass(frozen=True, slots=True)
class Weights:
    """Target weights for each status (open and dropout are always 0)."""

    person: float
    occupied: float
    base: float


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """A room's effective settings: its overrides merged over the zone defaults."""

    taus: Taus
    weights: Weights
    stale_limit: float


@dataclass(frozen=True, slots=True)
class RoomInputs:
    """A room's inputs at one moment, already evaluated by the runtime."""

    open: bool
    """True while an opening entity is on or the opening template is true."""
    person_present: bool
    occupied: bool
    temperature: float | None
    """Current reading converted to the zone's unit, or None if unavailable or unconvertible."""


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
    """The status driving the weight at the last update (``room_status``)."""
    last_occupied_state: Status | None = None
    """The last ``person`` or ``occupied`` status, which picks the fall tau."""
    last_known_temperature: float | None = None
    """The last valid reading, in the zone's unit; kept through a dropout."""
    last_seen: float | None = None
    """When the temperature sensor last had a valid reading. Saved across restarts,
    so a reading older than the stale limit isn't reused after a long downtime."""
    dropout_since: float | None = None
    """Start of the current dropout for the stale clock: when the sensor was last
    seen working, or when the dropout was noticed if it never was."""
    stale: bool = False
    """The last reading is older than the stale limit, so it isn't used."""
    last_update: float | None = None
    """When the room was last stepped; None after a restore, so downtime isn't
    applied as elapsed time."""

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
    """Pick the status driving the weight: open, dropout, person, then occupied."""
    if inputs.open:
        return Status.OPEN
    if inputs.temperature is None:
        return Status.DROPOUT
    if inputs.person_present:
        return Status.PERSON
    if inputs.occupied:
        return Status.OCCUPIED
    return Status.UNOCCUPIED


def target_weight(status: Status, weights: Weights) -> float:
    """Weight a room heads toward for a given status."""
    return {
        Status.OPEN: 0.0,
        Status.DROPOUT: 0.0,
        Status.PERSON: weights.person,
        Status.OCCUPIED: weights.occupied,
        Status.UNOCCUPIED: weights.base,
    }[status]


def select_tau_name(
    status: Status,
    last_occupied_state: Status | None,
    *,
    previous_status: Status | None = None,
    previous_tau_name: TauName | None = None,
    rising: bool = False,
) -> TauName:
    """Which tau approaches the target for ``status``.

    Falls use the room's last non-empty state rather than its current weight, so
    the choice is correct whatever order the weights are configured in. A room a
    tracked person has just left, but whose occupancy sensor is still on, is
    falling from ``person``: it uses person fall for as long as it stays
    ``occupied``. An empty room *below* its unoccupied weight (after being
    open or dropped out, or when new) is rising, so it uses occupancy rise.
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
        if rising:
            return TauName.OCCUPANCY_RISE
        if last_occupied_state is Status.PERSON:
            return TauName.PERSON_FALL
        return TauName.OCCUPANCY_FALL
    return TauName.OPEN if status is Status.OPEN else TauName.DROPOUT


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
        last_seen: float | None = now
        dropout_since = None
        stale = False
    else:
        last_known_temperature = state.last_known_temperature
        last_seen = state.last_seen
        # The stale clock counts from when the sensor was last seen working.
        if state.dropout_since is not None:
            dropout_since = state.dropout_since
        elif state.last_seen is not None:
            dropout_since = state.last_seen
        else:
            dropout_since = now
        stale = now - dropout_since > config.stale_limit * 60

    status = room_status(inputs)
    last_occupied_state = (
        status if status in (Status.PERSON, Status.OCCUPIED) else state.last_occupied_state
    )

    target = target_weight(status, config.weights)
    tau_name = select_tau_name(
        status,
        last_occupied_state,
        previous_status=state.status,
        previous_tau_name=state.tau_name,
        rising=weight < target,
    )
    tau = getattr(config.taus, tau_name)
    if tau <= 0 and not hold:
        # A tau of 0 means instantly: reach the new target now, not at the next update.
        weight = target
    return RoomState(
        weight=weight,
        target=target,
        tau=tau,
        tau_name=tau_name,
        status=status,
        last_occupied_state=last_occupied_state,
        last_known_temperature=last_known_temperature,
        last_seen=last_seen,
        dropout_since=dropout_since,
        stale=stale,
        last_update=now,
    )


def fallback_epsilon(base_weights: Iterable[float]) -> float:
    """Weight of the plain-average term: 1% of the smallest room base weight."""
    return 0.01 * min(base_weights)


@dataclass(frozen=True, slots=True)
class Aggregate:
    """The zone's weighted temperature and its attributes."""

    temperature: float | None
    """None means the sensor is unavailable: nothing could be averaged."""
    total_weight: float
    contributing_rooms: int
    fallback: bool
    """The plain-average term outweighs all the rooms' weights together."""


@dataclass(frozen=True, slots=True)
class RoomSample:
    """One room's part in the zone average, after it has been stepped."""

    weight: float
    usable: float | None
    """Last known reading unless it has gone stale (``RoomState.usable_temperature``)."""
    current: float | None
    """This update's reading, or None if the sensor is unusable (``RoomInputs.temperature``)."""
    closed: bool
    """Not open (``not RoomInputs.open``); preferred for the plain average."""


def aggregate(samples: Iterable[RoomSample], epsilon: float) -> Aggregate:
    """Blend the rooms' weighted average with a plain-average fallback.

    Rooms with a usable reading are averaged by weight. The plain average always
    takes part with weight ``epsilon``: negligible while any room has weight, and
    dominant as every room fades toward 0. It uses the first of these that
    has any readings:

    1. closed rooms' current readings, then 2. their usable last readings;
    3. any room's current readings, then 4. any usable last readings — so during a
       total sensor outage the output holds until the readings go stale.
    """
    samples = list(samples)
    contributing = [s for s in samples if s.usable is not None]
    weighted_sum = sum(s.weight * s.usable for s in contributing if s.usable is not None)
    total_weight = sum(s.weight for s in contributing)

    def readings(values: Iterable[float | None]) -> list[float]:
        return [value for value in values if value is not None]

    plain = (
        readings(s.current for s in samples if s.closed)
        or readings(s.usable for s in samples if s.closed)
        or readings(s.current for s in samples)
        or readings(s.usable for s in samples)
    )
    numerator, denominator = weighted_sum, total_weight
    if plain:
        numerator += epsilon * (sum(plain) / len(plain))
        denominator += epsilon

    return Aggregate(
        temperature=numerator / denominator if denominator > 0 else None,
        total_weight=total_weight,
        contributing_rooms=len(contributing),
        fallback=bool(plain) and epsilon > total_weight,
    )


def room_config(defaults: Mapping[str, float], overrides: Mapping[str, float]) -> RoomConfig:
    """A room's effective settings: its overrides merged over the zone defaults.

    Setting keys are the field names with a prefix: ``tau_<Taus field>`` and
    ``w_<Weights field>``; ``stale_limit`` is zone-wide, so it's read from the
    defaults only.
    """
    values = {**defaults, **overrides}
    return RoomConfig(
        taus=Taus(**{f.name: values[f"tau_{f.name}"] for f in fields(Taus)}),
        weights=Weights(**{f.name: values[f"w_{f.name}"] for f in fields(Weights)}),
        stale_limit=defaults["stale_limit"],
    )


@dataclass(frozen=True, slots=True)
class ZoneStep:
    """The result of stepping every room of a zone to one moment."""

    rooms: dict[str, RoomState]
    result: Aggregate


def step_zone(
    rooms: Mapping[str, tuple[RoomState, RoomInputs, RoomConfig]],
    now: float,
    *,
    hold: bool = False,
) -> ZoneStep:
    """Advance every room to ``now`` and compute the zone's weighted temperature.

    ``rooms`` maps a room's key to its previous state, its inputs now and its
    settings. This is the whole calculation, free of Home Assistant, so recorded
    inputs can be replayed through it (e.g. to tune the taus).
    """
    states = {
        key: step_room(state, inputs, config, now, hold=hold)
        for key, (state, inputs, config) in rooms.items()
    }
    epsilon = (
        fallback_epsilon(config.weights.base for _, _, config in rooms.values()) if rooms else 0.0
    )
    result = aggregate(
        (
            RoomSample(
                weight=states[key].weight,
                usable=states[key].usable_temperature,
                current=inputs.temperature,
                closed=not inputs.open,
            )
            for key, (_, inputs, _) in rooms.items()
        ),
        epsilon,
    )
    return ZoneStep(states, result)
