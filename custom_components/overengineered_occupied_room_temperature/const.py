"""Constants for Overengineered Occupied-Room Temperature (OORT)."""

from typing import Final

DOMAIN: Final = "overengineered_occupied_room_temperature"

# Zone (config entry): `data` holds the name; `options` hold the defaults, rooms
# and people, all edited through the zone's menu.
CONF_NAME: Final = "name"
CONF_TEMPERATURE_UNIT: Final = "temperature_unit"  # fixed when the zone is created
CONF_DEFAULTS: Final = "defaults"
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

# Zone defaults (options[CONF_DEFAULTS]).
CONF_TAU_PERSON_RISE: Final = "tau_person_rise"
CONF_TAU_PERSON_FALL: Final = "tau_person_fall"
CONF_TAU_OCCUPANCY_RISE: Final = "tau_occupancy_rise"
CONF_TAU_OCCUPANCY_FALL: Final = "tau_occupancy_fall"
CONF_TAU_DEACTIVATE: Final = "tau_deactivate"
CONF_TAU_DROPOUT: Final = "tau_dropout"
CONF_W_PERSON: Final = "w_person"
CONF_W_OCCUPIED: Final = "w_occupied"
CONF_W_BASE: Final = "w_base"
CONF_STALE_LIMIT: Final = "stale_limit"

# Room data (options[CONF_ROOMS][area_id]).
CONF_AREA_ID: Final = "area_id"
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

# Taus that must be > 0; the others allow 0 (= instant).
TAUS_POSITIVE: Final = (
    CONF_TAU_PERSON_RISE,
    CONF_TAU_PERSON_FALL,
    CONF_TAU_OCCUPANCY_RISE,
    CONF_TAU_OCCUPANCY_FALL,
)
TAUS_ZERO_ALLOWED: Final = (CONF_TAU_DEACTIVATE, CONF_TAU_DROPOUT)
TAUS: Final = TAUS_POSITIVE + TAUS_ZERO_ALLOWED
WEIGHTS: Final = (CONF_W_PERSON, CONF_W_OCCUPIED, CONF_W_BASE)

DEFAULTS: Final[dict[str, float]] = {
    CONF_TAU_PERSON_RISE: 3.0,
    CONF_TAU_PERSON_FALL: 3.0,
    CONF_TAU_OCCUPANCY_RISE: 10.0,
    CONF_TAU_OCCUPANCY_FALL: 8.0,
    CONF_TAU_DEACTIVATE: 1.0,
    CONF_TAU_DROPOUT: 5.0,
    CONF_W_PERSON: 1.0,
    CONF_W_OCCUPIED: 0.5,
    CONF_W_BASE: 0.001,
    CONF_STALE_LIMIT: 5.0,
}

GRACE_PERIOD_SECONDS: Final = 120

# Stored precision: enough for the thermostat and the weights, few enough
# digits that settled values stop producing new recorder rows.
WEIGHT_DECIMALS: Final = 4
TEMPERATURE_DECIMALS: Final = 1
# Smallest unoccupied weight: lower values change the output by hundredths of
# a degree at most, and 0.001 still shows at the stored weight precision.
MIN_BASE_WEIGHT: Final = 0.001
# Upper limits. Weights only matter relative to each other, so
# 0-1 loses nothing (with MIN_BASE_WEIGHT that still allows 1000:1) and a weight
# reads as a fraction.
MAX_TAU: Final = 1440.0  # minutes (a day)
MAX_WEIGHT: Final = 1.0
MAX_STALE_LIMIT: Final = 1440.0  # minutes (a day)
UPDATE_INTERVAL_SECONDS: Final = 60
