"""Writing and reading a room's saved state."""

from __future__ import annotations

import math
from typing import Any

import pytest

from custom_components.overengineered_occupied_room_temperature.const import CONF_TEMPERATURE_UNIT
from custom_components.overengineered_occupied_room_temperature.engine import RoomState, Status
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
        {"status": "bogus"},
        {"tau_name": "bogus"},
        {"status": None},
    ],
)
def test_unusable_saved_state_starts_the_room_afresh(change: dict[str, Any]) -> None:
    assert saved_room_state({**SAVED, **change}) is None


@pytest.mark.parametrize("missing", ["weight", "target", "tau", "status"])
def test_saved_state_without_a_core_field_is_unusable(missing: str) -> None:
    assert saved_room_state({k: v for k, v in SAVED.items() if k != missing}) is None
