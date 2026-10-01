"""Constants for Overengineered Occupied-Room Temperature (OORT)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.const import UnitOfTime

from .engine import RoomConfig, room_config

DOMAIN: Final = "overengineered_occupied_room_temperature"

# Zone (config entry): `data` holds the name and the temperature unit; `options`
# hold the room defaults, the zone-wide settings, the rooms and the people, all
# edited through the zone's menu.
CONF_NAME: Final = "name"
CONF_TEMPERATURE_UNIT: Final = "temperature_unit"  # fixed when the zone is created
CONF_DEFAULTS: Final = "defaults"  # settings each room uses unless it overrides them
CONF_ZONE_SETTINGS: Final = "zone"  # settings for the whole zone
CONF_ROOMS: Final = "rooms"  # {area_id: room data}
CONF_PEOPLE: Final = "people"  # {person_id: person data}

# Flow-only fields.
CONF_ROOM: Final = "room"
CONF_PERSON: Final = "person"
CONF_REMOVE: Final = "remove"
# List-form choices. The hyphen keeps them valid translation keys that can never
# clash with an area ID (area IDs are slugified: only a-z, 0-9 and "_").
CHOICE_ADD: Final = "add-new"
CHOICE_DONE: Final = "back-to-menu"

# Setting keys (see SETTINGS).
CONF_TAU_PERSON_RISE: Final = "tau_person_rise"
CONF_TAU_PERSON_FALL: Final = "tau_person_fall"
CONF_TAU_OCCUPANCY_RISE: Final = "tau_occupancy_rise"
CONF_TAU_OCCUPANCY_FALL: Final = "tau_occupancy_fall"
CONF_TAU_OPEN: Final = "tau_open"
CONF_TAU_DROPOUT: Final = "tau_dropout"
CONF_DELAY_PERSON_ENTER: Final = "delay_person_enter"
CONF_DELAY_PERSON_EXIT: Final = "delay_person_exit"
CONF_DELAY_OCCUPANCY_ENTER: Final = "delay_occupancy_enter"
CONF_DELAY_OCCUPANCY_EXIT: Final = "delay_occupancy_exit"
CONF_W_PERSON: Final = "w_person"
CONF_W_OCCUPIED: Final = "w_occupied"
CONF_W_BASE: Final = "w_base"
CONF_STALE_LIMIT: Final = "stale_limit"

# Room data (options[CONF_ROOMS][area_id]).
CONF_AREA_ID: Final = "area_id"
# Stored as a list, ready for several sensors per room; the form still takes one.
CONF_TEMPERATURE_SENSORS: Final = "temperature_sensors"
# The room form's single temperature-sensor field.
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_OCCUPANCY_SENSORS: Final = "occupancy_sensors"
CONF_OCCUPANCY_TEMPLATE: Final = "occupancy_template"
CONF_OPENING_SENSORS: Final = "opening_sensors"
CONF_OPENING_TEMPLATE: Final = "opening_template"
CONF_OVERRIDES: Final = "overrides"

# Person data (options[CONF_PEOPLE][person_id]).
CONF_SOURCE_ENTITY: Final = "source_entity"
CONF_SOURCE_ATTRIBUTE: Final = "source_attribute"
CONF_VALUE_TYPE: Final = "value_type"
VALUE_TYPE_AREA_NAME: Final = "area_name"
VALUE_TYPE_AREA_ID: Final = "area_id"

GRACE_PERIOD_SECONDS: Final = 120

# Stored precision: enough for the thermostat and the weights, few enough
# digits that settled values stop producing new recorder rows.
WEIGHT_DECIMALS: Final = 4
# Between status changes, a weight sensor writes once its weight has moved this far
# since it last wrote, or when it reaches its target.
WEIGHT_WRITE_STEP: Final = 0.01
TEMPERATURE_DECIMALS: Final = 1
UPDATE_INTERVAL_SECONDS: Final = 60

# Limits. Smallest unoccupied weight: lower values change the output by
# hundredths of a degree at most, and 0.001 still shows at the stored weight
# precision. Upper limits: weights only matter relative to each other, so
# 0-1 loses nothing (with MIN_BASE_WEIGHT that still allows 1000:1).
MIN_BASE_WEIGHT: Final = 0.001
MAX_TAU: Final = 1440.0  # minutes (a day)
MAX_DELAY: Final = 60.0  # minutes
MAX_WEIGHT: Final = 1.0
MAX_STALE_LIMIT: Final = 1440.0  # minutes (a day)


@dataclass(frozen=True, slots=True)
class Setting:
    """A numeric zone setting: its default, its limits and the errors for them.

    To add a setting: add it here, to the engine (``engine.Taus``/``Weights``/``Delays``
    use the key without its ``tau_``/``w_``/``delay_`` prefix) and to the translations. Zones saved
    without it get the default when they load, so they needn't be recreated.
    """

    key: str
    default: float
    minimum: float
    maximum: float
    too_small: str
    """Error key for a value below ``minimum`` (or equal to it, if not allowed)."""
    too_large: str
    unit: str | None = None
    """Shown next to the form field."""
    minimum_allowed: bool = True
    """Whether ``minimum`` itself is valid (False: must be greater)."""
    per_room: bool = True
    """Whether a room can override it. Zone-wide settings (False) are stored under
    ``options[CONF_ZONE_SETTINGS]`` and shown in the Settings screen's Zone section."""


def _tau(key: str, default: float, *, zero_allowed: bool) -> Setting:
    return Setting(
        key=key,
        default=default,
        minimum=0.0,
        maximum=MAX_TAU,
        too_small="tau_negative" if zero_allowed else "tau_not_positive",
        too_large="tau_too_large",
        unit=UnitOfTime.MINUTES,
        minimum_allowed=zero_allowed,
    )


def _delay(key: str) -> Setting:
    return Setting(
        key=key,
        default=0.0,
        minimum=0.0,
        maximum=MAX_DELAY,
        too_small="delay_negative",
        too_large="delay_too_large",
        unit=UnitOfTime.MINUTES,
    )


def _weight(
    key: str, default: float, minimum: float = 0.0, too_small: str = "weight_negative"
) -> Setting:
    return Setting(
        key=key,
        default=default,
        minimum=minimum,
        maximum=MAX_WEIGHT,
        too_small=too_small,
        too_large="weight_too_large",
    )


SETTINGS: Final = (
    _tau(CONF_TAU_PERSON_RISE, 3.0, zero_allowed=False),
    _tau(CONF_TAU_PERSON_FALL, 3.0, zero_allowed=False),
    _tau(CONF_TAU_OCCUPANCY_RISE, 10.0, zero_allowed=False),
    _tau(CONF_TAU_OCCUPANCY_FALL, 8.0, zero_allowed=False),
    _tau(CONF_TAU_OPEN, 1.0, zero_allowed=True),  # 0 = instant
    _tau(CONF_TAU_DROPOUT, 5.0, zero_allowed=True),
    _delay(CONF_DELAY_PERSON_ENTER),
    _delay(CONF_DELAY_PERSON_EXIT),
    _delay(CONF_DELAY_OCCUPANCY_ENTER),
    _delay(CONF_DELAY_OCCUPANCY_EXIT),
    _weight(CONF_W_PERSON, 1.0),
    _weight(CONF_W_OCCUPIED, 0.5),
    _weight(CONF_W_BASE, 0.001, MIN_BASE_WEIGHT, "base_weight_too_small"),
    Setting(
        key=CONF_STALE_LIMIT,
        default=5.0,
        minimum=0.0,
        maximum=MAX_STALE_LIMIT,
        too_small="stale_limit_not_positive",
        too_large="stale_limit_too_large",
        unit=UnitOfTime.MINUTES,
        minimum_allowed=False,
        per_room=False,
    ),
)
DEFAULTS: Final[dict[str, float]] = {setting.key: setting.default for setting in SETTINGS}
ROOM_SETTINGS: Final = tuple(setting.key for setting in SETTINGS if setting.per_room)
ZONE_SETTINGS: Final = tuple(setting.key for setting in SETTINGS if not setting.per_room)


def has_occupancy_source(room: Mapping[str, Any]) -> bool:
    """Whether a room's stored data gives any way to detect that it is occupied."""
    return bool(room.get(CONF_OCCUPANCY_SENSORS) or room.get(CONF_OCCUPANCY_TEMPLATE))


def with_defaults(stored: Mapping[str, float], keys: tuple[str, ...]) -> dict[str, float]:
    """Stored values for ``keys``, with the default for any not stored.

    Keys no longer known are dropped, so settings can be added or removed
    without recreating zones.
    """
    return {key: stored.get(key, DEFAULTS[key]) for key in keys}


def room_configs(options: Mapping[str, Any], *, overrides: bool = True) -> dict[str, RoomConfig]:
    """Each room's effective settings from a zone's stored options, by area ID.

    With ``overrides=False`` every room uses the zone defaults (e.g. to replay
    recorded inputs without the per-room overrides).
    """
    defaults = {
        **with_defaults(options.get(CONF_DEFAULTS, {}), ROOM_SETTINGS),
        **with_defaults(options.get(CONF_ZONE_SETTINGS, {}), ZONE_SETTINGS),
    }
    return {
        area_id: room_config(defaults, room.get(CONF_OVERRIDES, {}) if overrides else {})
        for area_id, room in options.get(CONF_ROOMS, {}).items()
    }
