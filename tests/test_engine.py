"""Tests for the pure weighting, smoothing and averaging rules."""

import ast
from dataclasses import fields, replace
from itertools import pairwise
import math
from pathlib import Path
import sys
from types import EllipsisType
from typing import Any, ClassVar

import pytest

from custom_components.overengineered_occupied_room_temperature import const, engine
from custom_components.overengineered_occupied_room_temperature.engine import (
    Aggregate,
    RoomInputs,
    RoomSample,
    RoomState,
    Status,
    TauName,
    Taus,
    Weights,
    aggregate,
    approach,
    fallback_epsilon,
    room_config,
    room_status,
    select_tau_name,
    step_room,
    step_zone,
    target_weight,
)
from tests.helpers import stored_settings

MINUTE = 60.0


def select_tau(status: Status, last: Status | None, taus: Taus, **history: Any) -> float:
    """The tau value ``select_tau_name`` picks, as step_room looks it up."""
    tau: float = getattr(taus, select_tau_name(status, last, **history))
    return tau


CONFIG = room_config(const.DEFAULTS, {})


def inputs(
    *,
    open: bool = False,
    person: bool = False,
    occupied: bool = False,
    temperature: float | None = 70.0,
) -> RoomInputs:
    return RoomInputs(open=open, person_present=person, occupied=occupied, temperature=temperature)


def run(
    state: RoomState,
    room_inputs: RoomInputs,
    minutes: float,
    *,
    start: float = 0.0,
    step: float = 1.0,
) -> RoomState:
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
            (inputs(open=True, person=True, occupied=True), Status.OPEN),
            (inputs(open=True, temperature=None), Status.OPEN),  # open wins over dropout
            (inputs(temperature=None, person=True), Status.DROPOUT),
            (inputs(person=True, occupied=True), Status.PERSON),
            (inputs(occupied=True), Status.OCCUPIED),
            (inputs(), Status.UNOCCUPIED),
        ],
    )
    def test_status_priority(self, room_inputs: RoomInputs, expected: Status) -> None:
        assert room_status(room_inputs) is expected

    def test_targets(self) -> None:
        weights = Weights(person=1.0, occupied=0.5, base=0.001)
        assert target_weight(Status.OPEN, weights) == 0.0
        assert target_weight(Status.DROPOUT, weights) == 0.0
        assert target_weight(Status.PERSON, weights) == 1.0
        assert target_weight(Status.OCCUPIED, weights) == 0.5
        assert target_weight(Status.UNOCCUPIED, weights) == 0.001


class TestSelectTau:
    taus = Taus(person_rise=1, person_fall=2, occupancy_rise=3, occupancy_fall=4, open=5, dropout=6)

    def test_rises(self) -> None:
        assert select_tau(Status.PERSON, None, self.taus) == 1
        assert select_tau(Status.OCCUPIED, None, self.taus, rising=True) == 3

    def test_fall_uses_last_occupied_state(self) -> None:
        assert select_tau(Status.UNOCCUPIED, Status.PERSON, self.taus) == 2
        assert select_tau(Status.UNOCCUPIED, Status.OCCUPIED, self.taus) == 4
        assert select_tau(Status.UNOCCUPIED, None, self.taus) == 4

    def test_open_and_dropout_use_their_own_taus(self) -> None:
        assert select_tau(Status.OPEN, None, self.taus) == 5
        assert select_tau(Status.DROPOUT, None, self.taus) == 6

    def test_names_match_the_chosen_tau(self) -> None:
        cases = [
            (Status.PERSON, None, TauName.PERSON_RISE),
            (Status.OCCUPIED, None, TauName.OCCUPANCY_RISE),
            (Status.UNOCCUPIED, Status.PERSON, TauName.PERSON_FALL),
            (Status.UNOCCUPIED, Status.OCCUPIED, TauName.OCCUPANCY_FALL),
            (Status.OPEN, None, TauName.OPEN),
            (Status.DROPOUT, None, TauName.DROPOUT),
        ]
        for status, last, name in cases:
            rising = status is Status.OCCUPIED
            assert select_tau_name(status, last, rising=rising) is name
            assert select_tau(status, last, self.taus, rising=rising) == getattr(self.taus, name)


class TestPersonLeavesOccupiedRoom:
    """Leaving ``person`` for ``occupied`` uses person fall, not occupancy rise."""

    taus = Taus(person_rise=1, person_fall=2, occupancy_rise=3, occupancy_fall=4, open=5, dropout=6)

    def test_select_tau_uses_person_fall_when_the_person_leaves(self) -> None:
        name = select_tau_name(Status.OCCUPIED, Status.OCCUPIED, previous_status=Status.PERSON)
        assert name is TauName.PERSON_FALL
        assert (
            select_tau(
                Status.OCCUPIED,
                Status.OCCUPIED,
                self.taus,
                previous_status=Status.PERSON,
            )
            == 2
        )

    def test_arriving_at_occupied_from_elsewhere_still_rises(self) -> None:
        for previous in (None, Status.UNOCCUPIED, Status.OPEN, Status.DROPOUT, Status.OCCUPIED):
            for last in (None, Status.PERSON, Status.OCCUPIED):
                name = select_tau_name(Status.OCCUPIED, last, previous_status=previous, rising=True)
                assert name is TauName.OCCUPANCY_RISE, (previous, last)

    def test_inverted_weights_rise_from_person_on_person_fall(self) -> None:
        config = replace(CONFIG, weights=Weights(person=0.3, occupied=0.8, base=0.001))
        state = RoomState(
            weight=0.3, target=0.3, status=Status.PERSON, last_occupied_state=Status.PERSON
        )
        state = step_room(state, inputs(occupied=True), config, 0.0)
        assert (state.target, state.tau_name) == (0.8, TauName.PERSON_FALL)

    def test_person_then_occupied_then_empty(self) -> None:
        config = replace(CONFIG, taus=self.taus)
        state = RoomState(
            weight=1.0,
            target=1.0,
            tau=1,
            tau_name=TauName.PERSON_RISE,
            status=Status.PERSON,
            last_occupied_state=Status.PERSON,
            last_known_temperature=70.0,
            last_update=0.0,
        )
        # The person leaves; the occupancy sensor is still on.
        state = step_room(state, inputs(occupied=True), config, 0.0)
        assert (state.status, state.tau_name, state.target) == (
            Status.OCCUPIED,
            TauName.PERSON_FALL,
            0.5,
        )
        state = step_room(state, inputs(occupied=True), config, 2 * MINUTE)
        assert state.weight == pytest.approx(0.5 + 0.5 * math.exp(-1))  # person fall, tau 2
        assert state.tau_name is TauName.PERSON_FALL  # still leaving person on later updates

        # The sensor turns off: sensor occupancy is what ends now.
        state = step_room(state, inputs(), config, 2 * MINUTE)
        assert (state.status, state.tau_name) == (Status.UNOCCUPIED, TauName.OCCUPANCY_FALL)


class TestFallingIntoOccupied:
    """A room that becomes occupied from above the occupied weight keeps falling."""

    def test_uses_person_fall(self) -> None:
        for last in (None, Status.PERSON, Status.OCCUPIED):
            for previous in (Status.UNOCCUPIED, Status.OPEN, Status.DROPOUT):
                name = select_tau_name(Status.OCCUPIED, last, previous_status=previous)
                assert name is TauName.PERSON_FALL, (last, previous)

    def test_occupancy_returning_after_a_person_left_continues_the_person_fall(self) -> None:
        state = RoomState(
            weight=1.0, target=1.0, status=Status.PERSON, last_occupied_state=Status.PERSON
        )
        state = step_room(state, inputs(), CONFIG, 0.0)  # the person leaves; motion off
        assert state.tau_name is TauName.PERSON_FALL
        state = step_room(state, inputs(occupied=True), CONFIG, MINUTE)  # motion returns
        assert (state.status, state.target, state.tau_name) == (
            Status.OCCUPIED,
            0.5,
            TauName.PERSON_FALL,
        )
        assert state.weight > 0.5
        state = step_room(state, inputs(occupied=True), CONFIG, 2 * MINUTE)
        assert state.tau_name is TauName.PERSON_FALL

    def test_a_brief_open_period_during_a_person_fall_resumes_the_person_fall(self) -> None:
        state = RoomState(
            weight=1.0, target=1.0, status=Status.PERSON, last_occupied_state=Status.PERSON
        )
        state = step_room(state, inputs(occupied=True), CONFIG, 0.0)  # the person leaves
        state = step_room(state, inputs(open=True, occupied=True), CONFIG, 10.0)
        state = step_room(state, inputs(occupied=True), CONFIG, 20.0)  # closed again
        assert state.weight > 0.5
        assert state.tau_name is TauName.PERSON_FALL


class TestStepRoom:
    def test_new_room_starts_at_zero_and_rises(self) -> None:
        state = step_room(RoomState(), inputs(person=True), CONFIG, 0.0)
        assert state.weight == 0.0
        assert state.target == 1.0
        state = step_room(state, inputs(person=True), CONFIG, 3 * MINUTE)
        assert state.weight == pytest.approx(1 - math.exp(-1))

    def test_piecewise_uses_previous_target_for_elapsed_time(self) -> None:
        # Occupied at 0.5; a person arrives 30 s after the last update.
        state = RoomState(weight=0.5, target=0.5, tau=10.0, status=Status.OCCUPIED, last_update=0.0)
        state = step_room(state, inputs(person=True), CONFIG, 30.0)
        assert state.weight == pytest.approx(0.5)  # not pulled toward 1.0 yet
        assert state.target == 1.0
        assert state.tau == CONFIG.taus.person_rise

    def test_fall_after_person_uses_person_fall_even_if_weights_inverted(self) -> None:
        config = replace(
            CONFIG,
            taus=replace(CONFIG.taus, person_fall=2, occupancy_fall=20),
            weights=replace(CONFIG.weights, person=0.2, occupied=0.8),
        )
        state = step_room(RoomState(), inputs(person=True), config, 0.0)
        state = step_room(state, inputs(), config, MINUTE)
        assert state.last_occupied_state is Status.PERSON
        assert state.tau == 2

    def test_open_room_fades_with_the_open_tau(self) -> None:
        state = RoomState(weight=1.0, target=1.0, tau=3.0, status=Status.PERSON, last_update=0.0)
        state = step_room(state, inputs(open=True, person=True), CONFIG, 0.0)
        assert state.status is Status.OPEN
        state = step_room(state, inputs(open=True, person=True), CONFIG, MINUTE)
        assert state.weight == pytest.approx(math.exp(-1))  # open tau = 1 min

    def test_grace_freezes_weight_but_tracks_status(self) -> None:
        state = RoomState(weight=0.5, target=0.5, tau=10.0, last_update=0.0)
        state = step_room(state, inputs(person=True), CONFIG, 10 * MINUTE, grace=True)
        assert state.weight == 0.5
        assert state.status is Status.PERSON
        assert state.last_update == 10 * MINUTE

    def test_dropout_keeps_last_known_temperature_until_stale(self) -> None:
        state = step_room(RoomState(), inputs(temperature=68.0), CONFIG, 0.0)
        assert state.last_seen == 0.0
        state = step_room(state, inputs(temperature=None), CONFIG, MINUTE)
        assert state.status is Status.DROPOUT
        assert state.tau == CONFIG.taus.dropout
        assert state.dropout_since == 0.0  # counted from when it was last seen working
        assert state.usable_temperature == 68.0

        limit = CONFIG.stale_limit * MINUTE
        state = step_room(state, inputs(temperature=None), CONFIG, limit)
        assert not state.stale  # exactly at the stale limit
        assert state.dropout_since == 0.0
        state = step_room(state, inputs(temperature=None), CONFIG, limit + 1)
        assert state.stale
        assert state.usable_temperature is None

    def test_restored_reading_older_than_the_limit_is_stale_at_once(self) -> None:
        """After a long downtime, a saved reading isn't treated as fresh."""
        saved = RoomState(
            weight=1.0, target=1.0, status=Status.PERSON, last_known_temperature=68.0, last_seen=0.0
        )
        state = step_room(saved, inputs(temperature=None), CONFIG, 2 * 3600.0)
        assert state.stale
        assert state.usable_temperature is None

    def test_restored_recent_reading_bridges_a_reboot(self) -> None:
        saved = RoomState(
            weight=1.0, target=1.0, status=Status.PERSON, last_known_temperature=68.0, last_seen=0.0
        )
        state = step_room(saved, inputs(temperature=None), CONFIG, 2 * MINUTE)
        assert not state.stale
        assert state.usable_temperature == 68.0

    def test_default_stale_limit_is_5_minutes(self) -> None:
        assert CONFIG.stale_limit == 5

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


class TestEmptyRoomAfterAnOpenPeriod:
    """An empty room below its unoccupied weight is rising."""

    WEIGHTS = Weights(person=1.0, occupied=0.5, base=0.3)

    def _after_open(self, minutes: float) -> RoomState:
        config = replace(CONFIG, weights=self.WEIGHTS)
        state = run(RoomState(), inputs(person=True), 60)  # a person room at ~1.0
        opened = step_room(state, inputs(open=True, person=True), config, 60 * MINUTE)
        return step_room(opened, inputs(open=True), config, (60 + minutes) * MINUTE)

    def test_rises_on_occupancy_rise_after_a_long_open_period(self) -> None:
        config = replace(CONFIG, weights=self.WEIGHTS)
        opened = self._after_open(60)  # faded to ~0
        closed = step_room(opened, inputs(), config, 121 * MINUTE)
        assert closed.weight < closed.target
        assert (closed.status, closed.tau_name) == (Status.UNOCCUPIED, TauName.OCCUPANCY_RISE)

    def test_still_falls_on_person_fall_after_a_brief_open_period(self) -> None:
        config = replace(CONFIG, weights=self.WEIGHTS)
        opened = self._after_open(0.1)  # barely faded from ~1.0
        closed = step_room(opened, inputs(), config, 60.2 * MINUTE)
        assert closed.weight > closed.target
        assert closed.tau_name is TauName.PERSON_FALL


class TestZeroTauIsInstant:
    """A tau of 0 reaches the new target in the same step."""

    PERSON = RoomState(
        weight=1.0,
        target=1.0,
        tau=3.0,
        tau_name=TauName.PERSON_RISE,
        status=Status.PERSON,
        last_update=0.0,
        last_known_temperature=70.0,
        last_seen=0.0,
    )
    INSTANT = replace(CONFIG, taus=replace(CONFIG.taus, open=0.0, dropout=0.0))

    def test_opened_room_drops_to_zero_at_once(self) -> None:
        state = step_room(self.PERSON, inputs(open=True, person=True), self.INSTANT, 0.0)
        assert (state.weight, state.target, state.tau_name) == (0.0, 0.0, TauName.OPEN)

    def test_dropped_out_sensor_drops_to_zero_at_once(self) -> None:
        state = step_room(self.PERSON, inputs(person=True, temperature=None), self.INSTANT, 0.0)
        assert (state.weight, state.tau_name) == (0.0, TauName.DROPOUT)

    def test_grace_still_freezes_the_weight(self) -> None:
        state = step_room(
            self.PERSON, inputs(open=True, person=True), self.INSTANT, 0.0, grace=True
        )
        assert state.weight == 1.0

    def test_positive_tau_still_starts_from_the_current_weight(self) -> None:
        state = step_room(self.PERSON, inputs(open=True, person=True), CONFIG, 0.0)
        assert state.weight == 1.0


def sample(
    weight: float,
    usable: float | None,
    current: float | EllipsisType | None = ...,
    *,
    closed: bool = True,
) -> RoomSample:
    """A stepped room for ``aggregate``; its current reading defaults to ``usable``."""
    return RoomSample(weight, usable, usable if current is ... else current, closed)


class TestAggregate:
    def test_epsilon_is_one_percent_of_smallest_base(self) -> None:
        assert fallback_epsilon([0.001, 0.002]) == pytest.approx(0.00001)

    def test_weighted_average_with_negligible_fallback(self) -> None:
        eps = fallback_epsilon([0.001])
        result = aggregate([sample(1.0, 66.0), sample(0.001, 72.0)], eps)
        plain_weighted = (66.0 * 1.0 + 72.0 * 0.001) / 1.001
        assert result.temperature == pytest.approx(plain_weighted, abs=1e-3)
        assert result.total_weight == pytest.approx(1.001)
        assert result.contributing_rooms == 2
        assert not result.fallback

    def test_empty_house_is_plain_average(self) -> None:
        eps = fallback_epsilon([0.001])
        result = aggregate([sample(0.001, 68.0), sample(0.001, 70.0), sample(0.001, 72.0)], eps)
        assert result.temperature == pytest.approx(70.0)

    def test_all_rooms_fading_glides_into_plain_average(self) -> None:
        # One occupied cold room and one warm room, both open and fading together.
        eps = fallback_epsilon([0.001])
        readings = []
        for minutes in range(31):
            fade = math.exp(-minutes)  # open tau = 1 min
            result = aggregate(
                [sample(1.0 * fade, 64.0, closed=False), sample(0.001 * fade, 74.0, closed=False)],
                eps,
            )
            readings.append(result.temperature)
        assert readings[0] == pytest.approx(64.0, abs=0.05)
        assert readings[-1] == pytest.approx(69.0, abs=0.01)
        steps = [b - a for a, b in pairwise(readings)]
        assert all(step >= 0 for step in steps)  # moves one way, no overshoot
        assert max(steps) < 5.0 / 2  # never jumps the whole gap in one minute

    def test_fallback_flag(self) -> None:
        eps = 0.00001
        assert aggregate([sample(0.000001, 70.0)], eps).fallback
        assert not aggregate([sample(0.001, 70.0)], eps).fallback

    def test_no_current_readings_falls_back_on_recent_readings(self) -> None:
        result = aggregate([sample(0.5, 68.0, None)], 0.00001)
        assert result.temperature == pytest.approx(68.0)
        assert not result.fallback

    def test_all_weights_zero_and_no_current_readings_uses_recent_readings(self) -> None:
        """For example, dropout tau 0 during a total sensor outage, before the stale limit."""
        result = aggregate([sample(0.0, 20.0, None), sample(0.0, 22.0, None)], 0.00001)
        assert result.temperature == pytest.approx(21.0)
        assert result.fallback
        assert result.contributing_rooms == 0

    def test_rooms_count_as_contributing_once_their_weight_shows(self) -> None:
        samples = [sample(0.00004, 20.0), sample(0.00005, 21.0), sample(0.5, 22.0)]
        result = aggregate(samples, 0.00001)
        assert result.contributing_rooms == 2
        assert result.total_weight == pytest.approx(0.50009)

    def test_contributing_weight_is_where_the_shown_weight_leaves_0(self) -> None:
        assert round(engine.CONTRIBUTING_WEIGHT, const.WEIGHT_DECIMALS) > 0
        assert round(engine.CONTRIBUTING_WEIGHT * 0.99, const.WEIGHT_DECIMALS) == 0

    def test_fallback_prefers_closed_rooms(self) -> None:
        """An open room's reading is used only if no closed room has one."""
        result = aggregate([sample(0.0, 20.0), sample(0.0, 12.0, closed=False)], 0.00001)
        assert result.temperature == pytest.approx(20.0)
        assert result.fallback

    def test_fallback_prefers_a_closed_rooms_recent_reading_over_an_open_live_one(self) -> None:
        result = aggregate([sample(0.0, 20.0, None), sample(0.0, 12.0, closed=False)], 0.00001)
        assert result.temperature == pytest.approx(20.0)

    def test_fallback_uses_open_rooms_when_every_room_is_open(self) -> None:
        result = aggregate(
            [sample(0.0, 20.0, closed=False), sample(0.0, 12.0, closed=False)], 0.00001
        )
        assert result.temperature == pytest.approx(16.0)

    def test_stale_rooms_are_not_in_the_fallback(self) -> None:
        # usable is None once stale, so only the recent room counts.
        result = aggregate([sample(0.0, 20.0, None), sample(0.0, None, None)], 0.00001)
        assert result.temperature == pytest.approx(20.0)

    def test_fallback_when_the_only_weighted_room_is_stale(self) -> None:
        result = aggregate([sample(1.0, None, None), sample(0.0, 20.0)], 0.00001)
        assert result.temperature == pytest.approx(20.0)
        assert result.total_weight == 0.0
        assert result.fallback

    def test_nothing_to_average_is_unavailable(self) -> None:
        assert aggregate([sample(0.5, None, None)], 0.00001) == Aggregate(
            temperature=None, total_weight=0.0, contributing_rooms=0, fallback=False
        )


class TestRoomConfig:
    DEFAULTS: ClassVar[dict[str, float]] = {
        "tau_person_rise": 3.0,
        "tau_person_fall": 3.0,
        "tau_occupancy_rise": 10.0,
        "tau_occupancy_fall": 8.0,
        "tau_open": 1.0,
        "tau_dropout": 5.0,
        "w_person": 1.0,
        "w_occupied": 0.5,
        "w_base": 0.001,
        "stale_limit": 5.0,
    }

    def test_overrides_replace_defaults(self) -> None:
        config = room_config(self.DEFAULTS, {"tau_person_fall": 1.0, "w_occupied": 0.8})
        assert config.taus == Taus(3.0, 1.0, 10.0, 8.0, 1.0, 5.0)
        assert config.weights == Weights(1.0, 0.8, 0.001)
        assert config.stale_limit == 5.0

    def test_stale_limit_is_zone_wide(self) -> None:
        assert room_config(self.DEFAULTS, {"stale_limit": 60.0}).stale_limit == 5.0

    def test_setting_keys_match_the_integration(self) -> None:
        """room_config builds keys from field names; they must be the stored keys."""
        engine_keys = {f"tau_{f.name}" for f in fields(Taus)} | {
            f"w_{f.name}" for f in fields(Weights)
        }
        assert engine_keys | {"stale_limit"} == set(const.DEFAULTS)
        assert engine_keys == set(const.ROOM_SETTINGS)
        assert self.DEFAULTS == const.DEFAULTS


class TestRoomConfigs:
    OPTIONS: ClassVar[dict[str, Any]] = {
        **stored_settings(w_occupied=0.4, stale_limit=10.0),
        const.CONF_ROOMS: {
            "kitchen": {const.CONF_OVERRIDES: {"tau_person_fall": 1.0}},
            "office": {},
        },
    }

    def test_rooms_get_their_overrides_over_the_zone_settings(self) -> None:
        configs = const.room_configs(self.OPTIONS)
        assert configs["kitchen"].taus.person_fall == 1.0
        assert configs["office"].taus.person_fall == const.DEFAULTS["tau_person_fall"]
        assert {c.weights.occupied for c in configs.values()} == {0.4}
        assert {c.stale_limit for c in configs.values()} == {10.0}

    def test_without_overrides_every_room_uses_the_zone_settings(self) -> None:
        configs = const.room_configs(self.OPTIONS, overrides=False)
        assert configs["kitchen"] == configs["office"]
        assert configs["kitchen"].weights.occupied == 0.4

    def test_settings_not_stored_take_their_defaults(self) -> None:
        configs = const.room_configs({const.CONF_ROOMS: {"kitchen": {}}})
        assert configs["kitchen"] == room_config(const.DEFAULTS, {})


def test_engine_imports_only_the_standard_library() -> None:
    """The engine runs outside Home Assistant, e.g. to replay recorded inputs."""
    tree = ast.parse(Path(engine.__file__).read_text())
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules.append("." * node.level + (node.module or ""))
    assert all(name.split(".")[0] in sys.stdlib_module_names for name in modules), modules


class TestStepZone:
    def test_steps_every_room_and_averages_them(self) -> None:
        person = (RoomState(), inputs(person=True, temperature=20.0), CONFIG)
        empty = (RoomState(), inputs(temperature=24.0), CONFIG)
        step = step_zone({"kitchen": person, "office": empty}, 0.0)
        step = step_zone(
            {
                "kitchen": (step.rooms["kitchen"], person[1], CONFIG),
                "office": (step.rooms["office"], empty[1], CONFIG),
            },
            3 * MINUTE,
        )
        kitchen, office = step.rooms["kitchen"], step.rooms["office"]
        assert kitchen.weight == pytest.approx(1 - math.exp(-1))
        expected = aggregate(
            [sample(kitchen.weight, 20.0), sample(office.weight, 24.0)], fallback_epsilon([0.001])
        )
        assert step.result == expected

    def test_open_room_is_not_preferred_for_the_fallback(self) -> None:
        closed = (RoomState(), inputs(temperature=20.0), CONFIG)
        opened = (RoomState(), inputs(open=True, temperature=12.0), CONFIG)
        step = step_zone({"a": closed, "b": opened}, 0.0)
        assert step.result.temperature == pytest.approx(20.0)

    def test_grace_freezes_weights(self) -> None:
        state = RoomState(weight=0.4, target=0.4, tau=3.0, last_update=0.0)
        step = step_zone({"a": (state, inputs(person=True), CONFIG)}, 10 * MINUTE, grace=True)
        assert step.rooms["a"].weight == 0.4

    def test_no_rooms(self) -> None:
        assert step_zone({}, 0.0).result.temperature is None

    def test_each_room_records_the_inputs_it_was_stepped_with(self) -> None:
        room_inputs = inputs(open=True, occupied=True, temperature=20.0)
        step = step_zone({"a": (RoomState(), room_inputs, CONFIG)}, 0.0)
        assert step.rooms["a"].inputs == room_inputs


class TestRoomSample:
    def test_uses_the_inputs_the_room_was_stepped_with(self) -> None:
        state = step_room(RoomState(), inputs(open=True, temperature=20.0), CONFIG, 0.0)
        assert RoomSample.of(state) == RoomSample(
            weight=0.0, usable=20.0, current=20.0, closed=False
        )

    def test_a_room_never_stepped_is_closed_with_no_current_reading(self) -> None:
        state = RoomState(weight=0.5, last_known_temperature=20.0)
        assert RoomSample.of(state) == RoomSample(
            weight=0.5, usable=20.0, current=None, closed=True
        )
