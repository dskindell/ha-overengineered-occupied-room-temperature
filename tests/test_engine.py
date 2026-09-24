"""Tests for the pure weighting, smoothing and averaging rules."""

import math

import pytest

from engine import (
    Aggregate,
    RoomConfig,
    RoomInputs,
    RoomState,
    Status,
    Taus,
    Weights,
    aggregate,
    approach,
    fallback_epsilon,
    room_status,
    select_tau,
    step_room,
    target_weight,
)

MINUTE = 60.0
CONFIG = RoomConfig()


def inputs(
    *,
    active: bool = True,
    person: bool = False,
    occupied: bool = False,
    temperature: float | None = 70.0,
) -> RoomInputs:
    return RoomInputs(
        active=active, person_present=person, occupied=occupied, temperature=temperature
    )


def run(state: RoomState, room_inputs: RoomInputs, minutes: float, *, start: float = 0.0,
        step: float = 1.0) -> RoomState:
    """Step a room once per ``step`` minutes for ``minutes``, starting at ``start``."""
    t = start
    state = step_room(state, room_inputs, CONFIG, t)
    while t < start + minutes * MINUTE - 1e-9:
        t += step * MINUTE
        state = step_room(state, room_inputs, CONFIG, t)
    return state


class TestApproach:
    def test_no_elapsed_time_keeps_weight(self) -> None:
        assert approach(0.2, 1.0, 3.0, 0.0) == 0.2

    def test_zero_tau_jumps_to_target(self) -> None:
        assert approach(0.2, 0.0, 0.0, 1.0) == 0.0

    def test_one_tau_covers_63_percent(self) -> None:
        assert approach(0.0, 1.0, 3.0, 3 * MINUTE) == pytest.approx(1 - math.exp(-1))

    def test_many_taus_reach_target(self) -> None:
        assert approach(1.0, 0.001, 3.0, 60 * MINUTE) == pytest.approx(0.001, abs=1e-6)


class TestStatusAndTarget:
    @pytest.mark.parametrize(
        ("room_inputs", "expected"),
        [
            (inputs(active=False, person=True, occupied=True), Status.INACTIVE),
            (inputs(temperature=None, person=True), Status.INACTIVE),
            (inputs(person=True, occupied=True), Status.PERSON),
            (inputs(occupied=True), Status.OCCUPIED),
            (inputs(), Status.UNOCCUPIED),
        ],
    )
    def test_status_priority(self, room_inputs: RoomInputs, expected: Status) -> None:
        assert room_status(room_inputs) is expected

    def test_targets(self) -> None:
        weights = Weights(person=1.0, occupied=0.5, base=0.001)
        assert target_weight(Status.INACTIVE, weights) == 0.0
        assert target_weight(Status.PERSON, weights) == 1.0
        assert target_weight(Status.OCCUPIED, weights) == 0.5
        assert target_weight(Status.UNOCCUPIED, weights) == 0.001


class TestSelectTau:
    taus = Taus(
        person_rise=1, person_fall=2, occupancy_rise=3, occupancy_fall=4, deactivate=5, dropout=6
    )

    def test_rises(self) -> None:
        assert select_tau(Status.PERSON, inputs(), None, self.taus) == 1
        assert select_tau(Status.OCCUPIED, inputs(), None, self.taus) == 3

    def test_fall_uses_last_occupied_state(self) -> None:
        assert select_tau(Status.UNOCCUPIED, inputs(), Status.PERSON, self.taus) == 2
        assert select_tau(Status.UNOCCUPIED, inputs(), Status.OCCUPIED, self.taus) == 4
        assert select_tau(Status.UNOCCUPIED, inputs(), None, self.taus) == 4

    def test_inactive_uses_deactivate_or_dropout(self) -> None:
        assert select_tau(Status.INACTIVE, inputs(active=False), None, self.taus) == 5
        assert select_tau(Status.INACTIVE, inputs(temperature=None), None, self.taus) == 6
        both = inputs(active=False, temperature=None)
        assert select_tau(Status.INACTIVE, both, None, self.taus) == 5


class TestStepRoom:
    def test_new_room_starts_at_zero_and_rises(self) -> None:
        state = step_room(RoomState(), inputs(person=True), CONFIG, 0.0)
        assert state.weight == 0.0
        assert state.target == 1.0
        state = step_room(state, inputs(person=True), CONFIG, 3 * MINUTE)
        assert state.weight == pytest.approx(1 - math.exp(-1))

    def test_piecewise_uses_previous_target_for_elapsed_time(self) -> None:
        # Occupied at 0.5; a person arrives 30 s after the last update.
        state = RoomState(
            weight=0.5, target=0.5, tau=10.0, status=Status.OCCUPIED, last_update=0.0
        )
        state = step_room(state, inputs(person=True), CONFIG, 30.0)
        assert state.weight == pytest.approx(0.5)  # not pulled toward 1.0 yet
        assert state.target == 1.0
        assert state.tau == CONFIG.taus.person_rise

    def test_fall_after_person_uses_person_fall_even_if_weights_inverted(self) -> None:
        config = RoomConfig(
            taus=Taus(person_fall=2, occupancy_fall=20), weights=Weights(person=0.2, occupied=0.8)
        )
        state = step_room(RoomState(), inputs(person=True), config, 0.0)
        state = step_room(state, inputs(), config, MINUTE)
        assert state.last_occupied_state is Status.PERSON
        assert state.tau == 2

    def test_inactive_room_fades_with_deactivate_tau(self) -> None:
        state = RoomState(weight=1.0, target=1.0, tau=3.0, status=Status.PERSON, last_update=0.0)
        state = step_room(state, inputs(active=False, person=True), CONFIG, 0.0)
        assert state.status is Status.INACTIVE
        state = step_room(state, inputs(active=False, person=True), CONFIG, MINUTE)
        assert state.weight == pytest.approx(math.exp(-1))  # deactivate tau = 1 min

    def test_hold_freezes_weight_but_tracks_status(self) -> None:
        state = RoomState(weight=0.5, target=0.5, tau=10.0, last_update=0.0)
        state = step_room(state, inputs(person=True), CONFIG, 10 * MINUTE, hold=True)
        assert state.weight == 0.5
        assert state.status is Status.PERSON
        assert state.last_update == 10 * MINUTE

    def test_dropout_keeps_last_known_temperature_until_stale(self) -> None:
        state = step_room(RoomState(), inputs(temperature=68.0), CONFIG, 0.0)
        state = step_room(state, inputs(temperature=None), CONFIG, MINUTE)
        assert state.status is Status.INACTIVE
        assert state.tau == CONFIG.taus.dropout
        assert state.dropout_since == MINUTE
        assert state.usable_temperature == 68.0

        state = step_room(state, inputs(temperature=None), CONFIG, 61 * MINUTE)
        assert not state.stale  # exactly 60 minutes out
        assert state.dropout_since == MINUTE
        state = step_room(state, inputs(temperature=None), CONFIG, 61 * MINUTE + 1)
        assert state.stale
        assert state.usable_temperature is None

    def test_sensor_recovery_clears_dropout(self) -> None:
        state = RoomState(
            last_known_temperature=68.0, dropout_since=0.0, stale=True, last_update=0.0
        )
        state = step_room(state, inputs(temperature=71.0), CONFIG, MINUTE)
        assert state.dropout_since is None
        assert not state.stale
        assert state.usable_temperature == 71.0
        assert state.status is Status.UNOCCUPIED

    def test_unoccupied_room_settles_at_base_weight(self) -> None:
        state = RoomState(
            weight=1.0,
            target=1.0,
            tau=3.0,
            status=Status.PERSON,
            last_occupied_state=Status.PERSON,
            last_update=0.0,
        )
        state = run(state, inputs(), 120)
        assert state.tau == CONFIG.taus.person_fall
        assert state.weight == pytest.approx(CONFIG.weights.base, abs=1e-9)


class TestAggregate:
    def test_epsilon_is_one_percent_of_smallest_base(self) -> None:
        assert fallback_epsilon([0.001, 0.002]) == pytest.approx(0.00001)

    def test_weighted_average_with_negligible_fallback(self) -> None:
        eps = fallback_epsilon([0.001])
        result = aggregate([(1.0, 66.0), (0.001, 72.0)], [66.0, 72.0], eps)
        plain_weighted = (66.0 * 1.0 + 72.0 * 0.001) / 1.001
        assert result.temperature == pytest.approx(plain_weighted, abs=1e-3)
        assert result.total_weight == pytest.approx(1.001)
        assert result.contributing_rooms == 2
        assert not result.fallback

    def test_empty_house_is_plain_average(self) -> None:
        eps = fallback_epsilon([0.001])
        result = aggregate([(0.001, 68.0), (0.001, 70.0), (0.001, 72.0)], [68, 70, 72], eps)
        assert result.temperature == pytest.approx(70.0)

    def test_all_rooms_fading_glides_into_plain_average(self) -> None:
        # One occupied cold room and one warm room, both inactive and fading together.
        eps = fallback_epsilon([0.001])
        readings = []
        for minutes in range(0, 31):
            fade = math.exp(-minutes)  # deactivate tau = 1 min
            result = aggregate([(1.0 * fade, 64.0), (0.001 * fade, 74.0)], [64.0, 74.0], eps)
            readings.append(result.temperature)
        assert readings[0] == pytest.approx(64.0, abs=0.05)
        assert readings[-1] == pytest.approx(69.0, abs=0.01)
        steps = [b - a for a, b in zip(readings, readings[1:])]
        assert all(step >= 0 for step in steps)  # moves one way, no overshoot
        assert max(steps) < 5.0 / 2  # never jumps the whole gap in one minute

    def test_fallback_flag(self) -> None:
        eps = 0.00001
        assert aggregate([(0.000001, 70.0)], [70.0], eps).fallback
        assert not aggregate([(0.001, 70.0)], [70.0], eps).fallback

    def test_no_current_readings_omits_fallback_term(self) -> None:
        result = aggregate([(0.5, 68.0)], [], 0.00001)
        assert result.temperature == pytest.approx(68.0)
        assert not result.fallback

    def test_nothing_to_average_is_unavailable(self) -> None:
        assert aggregate([(0.5, None)], [], 0.00001) == Aggregate(
            temperature=None, total_weight=0.0, contributing_rooms=0, fallback=False
        )
