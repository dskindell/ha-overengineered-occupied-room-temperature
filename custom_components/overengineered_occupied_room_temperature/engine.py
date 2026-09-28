"""Weighting, smoothing and averaging rules for OORT.

This module has no Home Assistant imports so the rules can be unit-tested
directly. The runtime gathers inputs from Home Assistant, calls these
functions, and writes the results to entities.

Units: timestamps and elapsed times are in seconds; taus, delays and the stale
limit are in minutes, matching the configuration.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, replace
from enum import StrEnum
import math
from typing import Final

# A room counts as contributing once its weight shows as non-zero at 4 decimals;
# a fading weight only approaches 0.
CONTRIBUTING_WEIGHT: Final = 0.00005


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
class Delays:
    """Minutes a presence must last (enter) or an absence must last (exit) before the
    room acts on it; 0 = at once."""

    person_enter: float
    person_exit: float
    occupancy_enter: float
    occupancy_exit: float


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """A room's effective settings: its overrides merged over the zone defaults."""

    taus: Taus
    weights: Weights
    delays: Delays
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
    """Everything carried from one update to the next (and restored after a restart),
    and the inputs of the update that produced it."""

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
    person_present: bool | None = None
    """Whether a tracked person counts as present, after the enter and exit delays;
    None until the room first takes its input."""
    person_since: float | None = None
    """When the person input started to differ from ``person_present``, if it does."""
    occupied: bool | None = None
    """Whether the room counts as occupied, after the delays; None like ``person_present``."""
    occupied_since: float | None = None
    """When the occupancy input started to differ from ``occupied``, if it does."""
    inputs: RoomInputs | None = None
    """The inputs this state was stepped with, before the delays; None before the
    first update. Not saved."""

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


def delayed(
    counted: bool | None,
    since: float | None,
    current: bool,
    enter: float,
    exit_: float,
    now: float,
) -> tuple[bool, float | None]:
    """Apply the enter or exit delay to one presence input.

    ``counted`` is what the room acts on and ``since`` when ``current`` started to
    differ from it. Returns the new pair: ``current`` counts once it has lasted the
    enter delay (if present) or the exit delay (if absent); a change that reverts
    sooner is forgotten.
    """
    if counted is None or current == counted:
        return current, None
    since = now if since is None else since
    if now >= since + (enter if current else exit_) * 60:
        return current, None
    return counted, since


def pending_deadline(state: RoomState, delays: Delays) -> float | None:
    """When the earliest delayed change in a stepped room will count, if one is waiting."""
    deadlines = [
        since + (exit_ if counted else enter) * 60
        for counted, since, enter, exit_ in (
            (state.person_present, state.person_since, delays.person_enter, delays.person_exit),
            (state.occupied, state.occupied_since, delays.occupancy_enter, delays.occupancy_exit),
        )
        if since is not None
    ]
    return min(deadlines, default=None)


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
    match status:
        case Status.PERSON:
            return weights.person
        case Status.OCCUPIED:
            return weights.occupied
        case Status.UNOCCUPIED:
            return weights.base
        case _:
            return 0.0


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
    ``occupied``. So does a room that becomes ``occupied`` from above the occupied
    weight: only a person leaves a room that high. An empty room *below* its
    unoccupied weight (after being open or dropped out, or when new) is rising,
    so it uses occupancy rise.
    """
    if status is Status.PERSON:
        return TauName.PERSON_RISE
    if status is Status.OCCUPIED:
        if previous_status is Status.PERSON or (
            previous_status is Status.OCCUPIED and previous_tau_name is TauName.PERSON_FALL
        ):
            return TauName.PERSON_FALL
        return TauName.OCCUPANCY_RISE if rising else TauName.PERSON_FALL
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
    grace: bool = False,
) -> RoomState:
    """Advance a room to ``now`` and apply its latest inputs.

    Smoothing is piecewise: the time since the last update is applied toward the
    target that was in effect during that time, and only then is the new target
    chosen. ``grace`` (the startup grace period) freezes the weight but still
    tracks inputs, status and dropouts.
    """
    elapsed = 0.0
    if not grace and state.last_update is not None:
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

    person_present, person_since = delayed(
        state.person_present,
        state.person_since,
        inputs.person_present,
        config.delays.person_enter,
        config.delays.person_exit,
        now,
    )
    occupied, occupied_since = delayed(
        state.occupied,
        state.occupied_since,
        inputs.occupied,
        config.delays.occupancy_enter,
        config.delays.occupancy_exit,
        now,
    )
    status = room_status(replace(inputs, person_present=person_present, occupied=occupied))
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
    if tau <= 0 and not grace:
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
        person_present=person_present,
        person_since=person_since,
        occupied=occupied,
        occupied_since=occupied_since,
        inputs=inputs,
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
    """Rooms with a usable reading and a weight of at least ``CONTRIBUTING_WEIGHT``."""
    fallback: bool
    """The plain-average term outweighs all the rooms' weights together."""


@dataclass(frozen=True, slots=True)
class RoomSample:
    """One room's part in the zone average, after it has been stepped."""

    weight: float
    usable: float | None
    """Last known reading unless it has gone stale (``RoomState.usable_temperature``)."""
    current: float | None
    """This update's reading, or None if the sensor is unusable."""
    closed: bool
    """Not open; preferred for the plain average."""

    @classmethod
    def of(cls, state: RoomState) -> RoomSample:
        """A stepped room's sample."""
        inputs = state.inputs
        return cls(
            weight=state.weight,
            usable=state.usable_temperature,
            current=inputs.temperature if inputs else None,
            closed=not inputs.open if inputs else True,
        )


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
        contributing_rooms=sum(s.weight >= CONTRIBUTING_WEIGHT for s in contributing),
        fallback=bool(plain) and epsilon > total_weight,
    )


def room_config(defaults: Mapping[str, float], overrides: Mapping[str, float]) -> RoomConfig:
    """A room's effective settings: its overrides merged over the zone defaults.

    Setting keys are the field names with a prefix: ``tau_<Taus field>``,
    ``w_<Weights field>`` and ``delay_<Delays field>``; ``stale_limit`` is
    zone-wide, so it's read from the defaults only.
    """
    values = {**defaults, **overrides}
    return RoomConfig(
        taus=Taus(**{f.name: values[f"tau_{f.name}"] for f in fields(Taus)}),
        weights=Weights(**{f.name: values[f"w_{f.name}"] for f in fields(Weights)}),
        delays=Delays(**{f.name: values[f"delay_{f.name}"] for f in fields(Delays)}),
        stale_limit=defaults["stale_limit"],
    )


@dataclass(frozen=True, slots=True)
class ZoneStep:
    """The result of stepping every room of a zone to one moment."""

    rooms: dict[str, RoomState]
    result: Aggregate
    next_deadline: float | None = None
    """When the earliest delayed presence change will count, if one is waiting."""


def step_zone(
    rooms: Mapping[str, tuple[RoomState, RoomInputs, RoomConfig]],
    now: float,
    *,
    grace: bool = False,
) -> ZoneStep:
    """Advance every room to ``now`` and compute the zone's weighted temperature.

    ``rooms`` maps a room's key to its previous state, its inputs now and its
    settings. This is the whole calculation, free of Home Assistant, so recorded
    inputs can be replayed through it (e.g. to tune the taus).
    """
    states = {
        key: step_room(state, inputs, config, now, grace=grace)
        for key, (state, inputs, config) in rooms.items()
    }
    epsilon = (
        fallback_epsilon(config.weights.base for _, _, config in rooms.values()) if rooms else 0.0
    )
    result = aggregate(map(RoomSample.of, states.values()), epsilon)
    deadlines = [
        deadline
        for key, (_, _, config) in rooms.items()
        if (deadline := pending_deadline(states[key], config.delays)) is not None
    ]
    return ZoneStep(states, result, min(deadlines, default=None))
