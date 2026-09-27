"""Helpers shared by the test modules."""

from __future__ import annotations

from typing import Any

from custom_components.overengineered_occupied_room_temperature.const import (
    CONF_DEFAULTS,
    CONF_ZONE_SETTINGS,
    DEFAULTS,
    ROOM_SETTINGS,
    ZONE_SETTINGS,
)


def stored_settings(**changes: Any) -> dict[str, dict[str, Any]]:
    """A zone's stored settings: room defaults and zone-wide settings apart."""
    values = {**DEFAULTS, **changes}
    return {
        CONF_DEFAULTS: {key: values[key] for key in ROOM_SETTINGS},
        CONF_ZONE_SETTINGS: {key: values[key] for key in ZONE_SETTINGS},
    }


# A room's saved state, as the integration writes it.
SAVED = {
    "weight": 0.8,
    "target": 1.0,
    "tau": 3.0,
    "tau_name": "person_rise",
    "status": "person",
    "last_occupied_state": "person",
    "last_known_temperature": 20.0,
    "last_seen": 100.0,
    "dropout_since": None,
    "stale": False,
    "last_update": 100.0,
}
