"""Writing and reading a room's saved state."""

from __future__ import annotations

from dataclasses import fields
import math
from typing import Any

import pytest

from custom_components.overengineered_occupied_room_temperature.const import CONF_TEMPERATURE_UNIT
from custom_components.overengineered_occupied_room_temperature.engine import (
    RoomInputs,
    RoomState,
    Status,
    TauName,
)
from custom_components.overengineered_occupied_room_temperature.storage import (
    SAVED_VERSION,
    RoomExtraData,
    saved_room_state,
)
from tests.helpers import SAVED


def test_saved_state_records_its_version() -> None:
    data = RoomExtraData(RoomState(weight=0.5), "°C").as_dict()
    assert data["version"] == SAVED_VERSION == 1
    assert data[CONF_TEMPERATURE_UNIT] == "°C"
    assert saved_room_state(data) == RoomState(weight=0.5)


def test_inputs_and_update_time_are_not_saved_or_restored() -> None:
    state = RoomState(weight=0.5, last_update=200.0, inputs=RoomInputs(False, False, True, 20.0))
    data = RoomExtraData(state, "°C").as_dict()
    assert "inputs" not in data
    assert "last_update" not in data
    restored = saved_room_state({**data, "inputs": {"open": True}, "last_update": 200.0})
    assert restored == RoomState(weight=0.5)


def test_saved_state_ignores_unknown_fields_and_defaults_missing_ones() -> None:
    """A field removed from or added to RoomState in a later version doesn't lose the room."""
    data = {k: v for k, v in SAVED.items() if k not in ("tau_name", "last_seen")}
    state = saved_room_state({**data, "retired_field": 1, "version": 99})
    assert state is not None
    assert (state.weight, state.status, state.tau_name, state.last_seen) == (
        0.8,
        Status.PERSON,
        None,
        None,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"weight": "0.8"},  # wrong type
        {"weight": math.nan},
        {"weight": True},
        {"weight": -0.1},
        {"weight": 1.5},
        {"target": -1.0},
        {"target": 2.0},
        {"tau": -3.0},
        {"tau": math.inf},
        {"status": "bogus"},
        {"status": None},
    ],
)
def test_unusable_saved_state_starts_the_room_afresh(change: dict[str, Any]) -> None:
    assert saved_room_state({**SAVED, **change}) is None


@pytest.mark.parametrize("missing", ["weight", "target", "tau", "status"])
def test_saved_state_without_a_core_field_is_unusable(missing: str) -> None:
    assert saved_room_state({k: v for k, v in SAVED.items() if k != missing}) is None


def test_every_saved_field_is_read_back() -> None:
    state = RoomState(
        weight=0.8,
        target=1.0,
        tau=3.0,
        tau_name=TauName.PERSON_FALL,
        status=Status.OCCUPIED,
        last_occupied_state=Status.PERSON,
        last_known_temperature=20.5,
        last_seen=100.0,
        dropout_since=160.0,
        stale=True,
    )
    unset = [f.name for f in fields(RoomState) if getattr(state, f.name) == f.default]
    assert unset == ["last_update", "inputs"], "set every saved field above"
    assert saved_room_state(RoomExtraData(state, "°C").as_dict()) == state


@pytest.mark.parametrize(
    ("change", "field", "value"),
    [
        ({"last_known_temperature": "20"}, "last_known_temperature", None),
        ({"last_known_temperature": math.nan}, "last_known_temperature", None),
        ({"last_seen": [1]}, "last_seen", None),
        ({"dropout_since": math.inf}, "dropout_since", None),
        ({"stale": "yes"}, "stale", False),
        ({"tau_name": "bogus"}, "tau_name", None),
        ({"last_occupied_state": "bogus"}, "last_occupied_state", None),
        ({"last_occupied_state": "open"}, "last_occupied_state", None),
    ],
)
def test_a_bad_optional_field_takes_its_default(
    change: dict[str, Any], field: str, value: Any
) -> None:
    state = saved_room_state({**SAVED, **change})
    assert state is not None
    assert getattr(state, field) == value
    assert state.weight == SAVED["weight"]
