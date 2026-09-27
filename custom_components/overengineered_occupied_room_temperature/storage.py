"""A room's saved engine state: how it's written and read back.

Home Assistant keeps it as the room weight sensor's restore "extra data", across
restarts and reloads. Reading it is tolerant: fields the engine no longer has
are ignored and fields it has gained take their defaults, so changing ``RoomState``
doesn't reset every room to 0.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
import math
from typing import Any, Final

from homeassistant.helpers.restore_state import ExtraStoredData

from .engine import RoomState, Status, TauName

# Bump when a field's meaning changes, and convert older data in saved_room_state.
SAVED_VERSION: Final = 1
KEY_VERSION: Final = "version"
KEY_UNIT: Final = "temperature_unit"
# Without these a saved state means nothing; the room then starts afresh.
_REQUIRED: Final = ("weight", "target", "tau", "status")
_NUMBERS: Final = ("weight", "target", "tau")
# Inputs are replaced by the first update after a restore, so they aren't saved.
_UNSAVED: Final = frozenset({"inputs"})
_FIELDS: Final = frozenset(field.name for field in fields(RoomState)) - _UNSAVED


@dataclass(frozen=True, slots=True)
class RoomExtraData(ExtraStoredData):
    """A room's engine state and the unit of its saved reading."""

    state: RoomState
    temperature_unit: str

    def as_dict(self) -> dict[str, Any]:
        saved = {key: value for key, value in asdict(self.state).items() if key in _FIELDS}
        return {
            **saved,
            KEY_UNIT: self.temperature_unit,
            KEY_VERSION: SAVED_VERSION,
        }


def saved_unit(data: Mapping[str, Any]) -> str | None:
    """The unit a saved room reading is in; None if none was saved."""
    unit = data.get(KEY_UNIT)
    return unit if isinstance(unit, str) else None


def saved_room_state(data: Mapping[str, Any]) -> RoomState | None:
    """A room's engine state from its saved extra data, or None if unusable."""
    values = {key: value for key, value in data.items() if key in _FIELDS}
    if any(key not in values for key in _REQUIRED):
        return None
    for key in _NUMBERS:
        value = values[key]
        is_number = isinstance(value, int | float) and not isinstance(value, bool)
        if not is_number or not math.isfinite(value):
            return None
    try:
        values["status"] = Status(values["status"])
        if values.get("last_occupied_state") is not None:
            values["last_occupied_state"] = Status(values["last_occupied_state"])
        if values.get("tau_name") is not None:
            values["tau_name"] = TauName(values["tau_name"])
    except ValueError:
        return None
    return RoomState(**values)
