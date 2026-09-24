"""Constants for Overengineered Occupied-Room Temperature (OORT)."""

from typing import Final

DOMAIN: Final = "overengineered_occupied_room_temperature"

SUBENTRY_ROOM: Final = "room"
SUBENTRY_PERSON: Final = "person"

# Instance settings (config entry data).
CONF_NAME: Final = "name"
CONF_DEFAULTS: Final = "defaults"
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

# Room subentry data.
CONF_AREA_ID: Final = "area_id"
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_OCCUPANCY_SENSORS: Final = "occupancy_sensors"
CONF_OCCUPANCY_TEMPLATE: Final = "occupancy_template"
CONF_ACTIVE_TEMPLATE: Final = "active_template"
CONF_OVERRIDES: Final = "overrides"

# Person subentry data.
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
    CONF_STALE_LIMIT: 60.0,
}

GRACE_PERIOD_SECONDS: Final = 120
UPDATE_INTERVAL_SECONDS: Final = 60
