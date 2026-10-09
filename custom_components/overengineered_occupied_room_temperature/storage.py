"""A room's saved engine state: how it's written and read back.

Home Assistant keeps it as the room weight sensor's restore "extra data", across
restarts and reloads. Reading it is tolerant: fields the engine no longer has
are ignored, and fields it has gained or that hold a bad value take their
defaults, so changing ``RoomState`` doesn't reset every room to 0. Only a bad
weight, target, tau or status makes a room start afresh.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
import math
from typing import Any, Final

from homeassistant.helpers.restore_state import ExtraStoredData

from .const import MAX_WEIGHT
from .engine import RoomState, Status, TauName

# Bump when a field's meaning changes, and convert older data in saved_room_state.
SAVED_VERSION: Final = 1
KEY_VERSION: Final = "version"
# A restored room is stepped afresh: its inputs are replaced and the downtime
# isn't counted, so neither is saved.
_UNSAVED: Final = frozenset({"inputs", "last_update"})
_FIELDS: Final = frozenset(field.name for field in fields(RoomState)) - _UNSAVED


@dataclass(frozen=True, slots=True)
class RoomExtraData(ExtraStoredData):
    """A room's engine state; its reading is in the zone's unit, which never changes."""

    state: RoomState

    def as_dict(self) -> dict[str, Any]:
        saved = {key: value for key, value in asdict(self.state).items() if key in _FIELDS}
        return {**saved, KEY_VERSION: SAVED_VERSION}


def _number(value: Any) -> float | None:
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    return float(value) if is_number and math.isfinite(value) else None


def _delayed_input(
    data: Mapping[str, Any], name: str, since: str
) -> tuple[bool | None, float | None]:
    """A presence input after its delays, and when a change to it started; without a
    valid value the room takes its input as it is, with nothing pending."""
    counted = data.get(name)
    if not isinstance(counted, bool):
        return None, None
    return counted, _number(data.get(since))


def _member[E: StrEnum](kind: type[E], value: Any) -> E | None:
    try:
        return kind(value)
    except ValueError:
        return None


def saved_room_state(data: Mapping[str, Any]) -> RoomState | None:
    """A room's engine state from its saved extra data, or None if unusable."""
    weight, target, tau = (_number(data.get(key)) for key in ("weight", "target", "tau"))
    status = _member(Status, data.get("status"))
    if weight is None or target is None or tau is None or status is None:
        return None
    if not (0 <= weight <= MAX_WEIGHT and 0 <= target <= MAX_WEIGHT and tau >= 0):
        return None
    last_occupied_state = _member(Status, data.get("last_occupied_state"))
    person_present, person_since = _delayed_input(data, "person_present", "person_since")
    occupied, occupied_since = _delayed_input(data, "occupied", "occupied_since")
    return RoomState(
        weight=weight,
        target=target,
        tau=tau,
        tau_name=_member(TauName, data.get("tau_name")),
        status=status,
        last_occupied_state=(
            last_occupied_state if last_occupied_state in (Status.PERSON, Status.OCCUPIED) else None
        ),
        last_known_temperature=_number(data.get("last_known_temperature")),
        last_seen=_number(data.get("last_seen")),
        dropout_since=_number(data.get("dropout_since")),
        stale=data.get("stale") is True,
        person_present=person_present,
        person_since=person_since,
        occupied=occupied,
        occupied_since=occupied_since,
    )
