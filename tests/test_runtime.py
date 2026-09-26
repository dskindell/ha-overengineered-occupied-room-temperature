"""End-to-end tests: a real instance in an in-memory Home Assistant."""

from __future__ import annotations

from datetime import timedelta
import math
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, UnitOfTemperature
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from custom_components.overengineered_occupied_room_temperature.const import (
    CHOICE_ADD,
    CHOICE_DONE,
    CONF_AREA_ID,
    CONF_DEFAULTS,
    CONF_NAME,
    CONF_OCCUPANCY_SENSORS,
    CONF_OCCUPANCY_TEMPLATE,
    CONF_OPENING_SENSORS,
    CONF_OPENING_TEMPLATE,
    CONF_OVERRIDES,
    CONF_PEOPLE,
    CONF_ROOM,
    CONF_ROOMS,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALUE_TYPE,
    DEFAULTS,
    DOMAIN,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

KITCHEN_WEIGHT = "sensor.oort_home_kitchen_weight"
OFFICE_WEIGHT = "sensor.oort_home_office_weight"
TEMPERATURE = "sensor.oort_home_temperature"


def room(area_id: str, **fields: Any) -> tuple[str, str, dict[str, Any]]:
    return (
        "room",
        area_id,
        {
            CONF_AREA_ID: area_id,
            CONF_TEMPERATURE_SENSOR: f"sensor.{area_id}_temperature",
            CONF_OVERRIDES: {},
            **fields,
        },
    )


def person(name: str, source: str, **fields: Any) -> tuple[str, str, dict[str, Any]]:
    return (
        "person",
        name.lower(),
        {CONF_NAME: name, CONF_SOURCE_ENTITY: source, CONF_VALUE_TYPE: "area_name", **fields},
    )


def zone_options(items: list[tuple[str, str, dict[str, Any]]], **settings: float) -> dict[str, Any]:
    return {
        CONF_DEFAULTS: {**DEFAULTS, **settings},
        CONF_ROOMS: {key: data for kind, key, data in items if kind == "room"},
        CONF_PEOPLE: {key: data for kind, key, data in items if kind == "person"},
    }


def set_temperature(hass: HomeAssistant, area_id: str, value: str, unit: str = "°C") -> None:
    hass.states.async_set(
        f"sensor.{area_id}_temperature",
        value,
        {"unit_of_measurement": unit, "device_class": "temperature"},
    )


async def setup_instance(
    hass: HomeAssistant, items: list[tuple[str, str, dict[str, Any]]], **settings: float
) -> MockConfigEntry:
    areas = ar.async_get(hass)
    for area in ("kitchen", "office"):
        if areas.async_get_area(area) is None:
            areas.async_create(area.title())
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        version=1,
        data={CONF_NAME: "Home", "temperature_unit": hass.config.units.temperature_unit},
        options=zone_options(items, **settings),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int) -> None:
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def weight(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


@pytest.fixture(autouse=True)
def metric(hass: HomeAssistant) -> None:
    assert hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS


async def test_creates_device_and_entities(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    await setup_instance(hass, [room("kitchen"), room("office")])

    for entity_id in (KITCHEN_WEIGHT, OFFICE_WEIGHT, TEMPERATURE):
        assert hass.states.get(entity_id) is not None, entity_id
    temperature = hass.states.get(TEMPERATURE)
    assert temperature.attributes["unit_of_measurement"] == "°C"
    assert temperature.attributes["state_class"] == "measurement"
    assert hass.states.get(KITCHEN_WEIGHT).attributes["state_class"] == "measurement"


async def test_person_pulls_the_temperature_toward_their_room(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_instance(
        hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")]
    )

    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["status"] == "person"
    assert kitchen.attributes["people"] == ["Alex"]
    assert float(kitchen.state) == 0.0  # new rooms start at 0
    assert kitchen.attributes["tau_name"] == "person_rise"
    assert kitchen.attributes["tau"] == 3.0
    assert hass.states.get(OFFICE_WEIGHT).attributes["tau_name"] == "occupancy_fall"

    await advance(hass, freezer, 3)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1 - math.exp(-1), abs=5e-5)

    await advance(hass, freezer, 60)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1.0, abs=5e-5)
    assert weight(hass, OFFICE_WEIGHT) == pytest.approx(0.001, abs=5e-5)
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(
        round((20 * 1.0 + 24 * 0.001) / 1.001, 1), abs=1e-9
    )


async def test_person_location_by_area_id_attribute(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_phone", "home", {"area_id": "kitchen"})
    await setup_instance(
        hass,
        [
            room("kitchen"),
            person(
                "Alex", "sensor.alex_phone",
                **{CONF_VALUE_TYPE: "area_id", CONF_SOURCE_ATTRIBUTE: "area_id"},
            ),
        ],
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["person_present"] is True


async def test_unregistered_area_matches_no_room(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Garage")
    await setup_instance(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "unoccupied"


async def test_occupancy_sensor_and_template(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    hass.states.async_set("input_boolean.office_desk", "off")
    await setup_instance(
        hass,
        [
            room("kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]}),
            room(
                "office",
                **{CONF_OCCUPANCY_TEMPLATE: "{{ is_state('input_boolean.office_desk', 'on') }}"},
            ),
        ],
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "unoccupied"
    assert hass.states.get(OFFICE_WEIGHT).attributes["status"] == "unoccupied"

    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    hass.states.async_set("input_boolean.office_desk", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "occupied"
    assert hass.states.get(OFFICE_WEIGHT).attributes["status"] == "occupied"


async def test_opening_template_true_fades_room_out(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_instance(
        hass,
        [
            room(
                "kitchen",
                **{CONF_OPENING_TEMPLATE: "{{ is_state('binary_sensor.kitchen_window', 'on') }}"},
            ),
            person("Alex", "sensor.alex_area"),
        ],
    )
    await advance(hass, freezer, 60)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1.0, abs=5e-5)

    hass.states.async_set("binary_sensor.kitchen_window", "on")
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["status"] == "inactive"
    assert kitchen.attributes["active"] is False
    await advance(hass, freezer, 1)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(math.exp(-1), abs=5e-5)  # deactivate τ


async def test_any_opening_entity_on_makes_room_inactive(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    hass.states.async_set("input_boolean.kitchen_door_open", "off")
    await setup_instance(
        hass,
        [
            room(
                "kitchen",
                **{
                    CONF_OPENING_SENSORS: [
                        "binary_sensor.kitchen_window",
                        "input_boolean.kitchen_door_open",
                    ]
                },
            )
        ],
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is True

    hass.states.async_set("input_boolean.kitchen_door_open", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "inactive"

    hass.states.async_set("input_boolean.kitchen_door_open", "off")
    hass.states.async_set("binary_sensor.kitchen_window", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is False

    hass.states.async_set("binary_sensor.kitchen_window", "off")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is True


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_unavailable_opening_entity_is_not_open(hass: HomeAssistant, state: str) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", state)
    await setup_instance(
        hass, [room("kitchen", **{CONF_OPENING_SENSORS: ["binary_sensor.kitchen_window"]})]
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is True


async def test_missing_opening_entity_is_not_open(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(
        hass, [room("kitchen", **{CONF_OPENING_SENSORS: ["binary_sensor.does_not_exist"]})]
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is True


async def test_opening_entities_and_template_combine(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    hass.states.async_set("input_boolean.heater", "off")
    await setup_instance(
        hass,
        [
            room(
                "kitchen",
                **{
                    CONF_OPENING_SENSORS: ["binary_sensor.kitchen_window"],
                    CONF_OPENING_TEMPLATE: "{{ is_state('input_boolean.heater', 'on') }}",
                },
            )
        ],
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is True
    hass.states.async_set("input_boolean.heater", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is False  # template alone


async def test_fahrenheit_sensor_is_converted(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "68", unit="°F")
    await setup_instance(hass, [room("kitchen")])
    # Only one room: the plain-average term dominates while it rises from 0.
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0)


async def test_dropout_then_stale_goes_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen")], stale_limit=10)
    await advance(hass, freezer, 30)

    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["temperature_available"] is False
    assert kitchen.attributes["status"] == "inactive"
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0)  # last known

    await advance(hass, freezer, 11)
    assert hass.states.get(KITCHEN_WEIGHT).attributes["temperature_stale"] is True
    assert hass.states.get(TEMPERATURE).state == STATE_UNAVAILABLE

    set_temperature(hass, "kitchen", "21")
    await hass.async_block_till_done()
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(21.0)


async def test_all_rooms_inactive_uses_plain_average(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    hass.states.async_set("input_boolean.windows_open", "off")
    await setup_instance(
        hass,
        [
            room("kitchen", **{CONF_OPENING_SENSORS: ["input_boolean.windows_open"]}),
            room("office", **{CONF_OPENING_SENSORS: ["input_boolean.windows_open"]}),
            person("Alex", "sensor.alex_area"),
        ],
    )
    await advance(hass, freezer, 60)
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0, abs=0.01)

    hass.states.async_set("input_boolean.windows_open", "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)
    temperature = hass.states.get(TEMPERATURE)
    assert temperature.attributes["fallback"] is True
    assert float(temperature.state) == pytest.approx(22.0, abs=0.01)


async def test_repairs_issue_when_no_occupancy_source(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_instance(hass, [room("kitchen")])
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"no_occupancy_source_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders == {"zone": "Home", "rooms": "Kitchen"}


async def test_no_repairs_issue_when_people_exist(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_instance(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"no_occupancy_source_{entry.entry_id}"
    ) is None


async def test_removing_a_room_removes_its_entity(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    entry = await setup_instance(hass, [room("kitchen"), room("office")])
    registry = er.async_get(hass)
    assert registry.async_get(OFFICE_WEIGHT) is not None

    options = {**entry.options, CONF_ROOMS: {"kitchen": entry.options[CONF_ROOMS]["kitchen"]}}
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    assert registry.async_get(OFFICE_WEIGHT) is None
    assert registry.async_get(KITCHEN_WEIGHT) is not None


async def test_weights_restored_after_restart(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(KITCHEN_WEIGHT, "0.8"),
                {
                    "weight": 0.8, "target": 1.0, "tau": 3.0, "status": "person",
                    "last_occupied_state": "person", "last_known_temperature": 20.0,
                    "dropout_since": None, "stale": False, "last_update": 12345.0,
                },
            )
        ],
    )
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen")])
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert float(kitchen.state) == pytest.approx(0.8)  # resumed; downtime ignored
    assert kitchen.attributes["last_occupied_state"] == "person"
    assert kitchen.attributes["area_id"] == "kitchen"


async def test_grace_period_holds_weights_at_startup(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass.set_state(CoreState.not_running)
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_instance(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await advance(hass, freezer, 1)
    assert weight(hass, KITCHEN_WEIGHT) == 0.0  # held
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "person"

    await advance(hass, freezer, 2)  # grace ends 2 minutes after startup completes
    assert weight(hass, KITCHEN_WEIGHT) > 0.0


# ---------------------------------------------------------------------------
# Settings, templates, people and zones
# ---------------------------------------------------------------------------


async def test_room_overrides_replace_zone_defaults(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    hass.states.async_set("sensor.sam_area", "Office")
    await setup_instance(
        hass,
        [
            room("kitchen", **{CONF_OVERRIDES: {"tau_person_rise": 10, "w_person": 2.0}}),
            room("office"),
            person("Alex", "sensor.alex_area"),
            person("Sam", "sensor.sam_area"),
        ],
    )
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["tau"] == 10
    assert kitchen.attributes["target_weight"] == 2.0
    assert hass.states.get(OFFICE_WEIGHT).attributes["tau"] == DEFAULTS["tau_person_rise"]

    await advance(hass, freezer, 10)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(2.0 * (1 - math.exp(-1)), abs=5e-5)
    assert weight(hass, OFFICE_WEIGHT) == pytest.approx(1 - math.exp(-10 / 3), abs=5e-5)


async def test_room_added_through_configure_appears_after_save(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    entry = await setup_instance(hass, [room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ false }}"})])
    assert hass.states.get(OFFICE_WEIGHT) is None

    options = hass.config_entries.options
    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(result["flow_id"], {"next_step_id": CONF_ROOMS})
    result = await options.async_configure(result["flow_id"], {CONF_ROOM: CHOICE_ADD})
    result = await options.async_configure(
        result["flow_id"],
        {
            CONF_AREA_ID: "office",
            CONF_TEMPERATURE_SENSOR: "sensor.office_temperature",
            CONF_OCCUPANCY_TEMPLATE: "{{ true }}",
            CONF_OVERRIDES: {},
        },
    )
    result = await options.async_configure(result["flow_id"], {CONF_ROOM: CHOICE_DONE})
    result = await options.async_configure(result["flow_id"], {"next_step_id": "save"})
    await hass.async_block_till_done()

    office = hass.states.get(OFFICE_WEIGHT)
    assert office is not None
    assert office.attributes["status"] == "occupied"
    assert hass.states.get(TEMPERATURE).attributes["contributing_rooms"] == 2


async def test_failing_templates_count_as_not_open_and_not_occupied(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(
        hass,
        [
            room(
                "kitchen",
                **{CONF_OPENING_TEMPLATE: "{{ 1 / 0 }}", CONF_OCCUPANCY_TEMPLATE: "{{ 2 / 0 }}"},
            )
        ],
    )
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["active"] is True  # only a clear "true" means open
    assert kitchen.attributes["occupied"] is False
    assert kitchen.attributes["status"] == "unoccupied"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 2, "each failing template is logged once, by OORT only"
    assert all("Kitchen" in r.getMessage() for r in errors)


@pytest.mark.parametrize("result", ["unavailable", "unknown", ""])
async def test_unavailable_template_result_is_not_open_and_not_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, result: str
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: result})])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is True
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR") and "OORT" in r.getMessage()]


async def test_unrecognised_template_result_is_not_open_and_warned_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: "maybe"})])
    await advance(hass, freezer, 3)
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is True
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "maybe" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    ("value", "attributes"),
    [
        ("20", {}),  # no unit
        ("20", {"unit_of_measurement": "%"}),  # not a temperature unit
        ("warm", {"unit_of_measurement": "°C"}),  # not a number
    ],
)
async def test_unusable_temperature_counts_as_dropout(
    hass: HomeAssistant, value: str, attributes: dict[str, str]
) -> None:
    set_temperature(hass, "office", "24")
    hass.states.async_set("sensor.kitchen_temperature", value, attributes)
    await setup_instance(hass, [room("kitchen"), room("office")])
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["temperature_available"] is False
    assert kitchen.attributes["status"] == "inactive"
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(24.0)


async def test_any_of_several_occupancy_sensors_occupies_the_room(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    hass.states.async_set("input_boolean.kitchen_cooking", "off")
    await setup_instance(
        hass,
        [
            room(
                "kitchen",
                **{
                    CONF_OCCUPANCY_SENSORS: [
                        "binary_sensor.kitchen_motion",
                        "input_boolean.kitchen_cooking",
                    ]
                },
            )
        ],
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["occupied"] is False

    hass.states.async_set("input_boolean.kitchen_cooking", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["occupied"] is True

    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    hass.states.async_set("input_boolean.kitchen_cooking", "off")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["occupied"] is True

    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["occupied"] is False


async def test_zones_are_independent(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_instance(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    upstairs = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="Upstairs",
        data={CONF_NAME: "Upstairs"},
        options=zone_options([room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ false }}"})]),
    )
    upstairs.add_to_hass(hass)
    assert await hass.config_entries.async_setup(upstairs.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "person"
    other = hass.states.get("sensor.oort_upstairs_kitchen_weight")
    assert other is not None, "the same area is a room in both zones"
    assert other.attributes["status"] == "unoccupied"
    assert other.attributes["people"] == []


async def test_temperature_sensor_attributes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    await setup_instance(hass, [room("kitchen"), room("office")])
    attributes = hass.states.get(TEMPERATURE).attributes
    assert attributes["contributing_rooms"] == 2
    assert attributes["total_weight"] == 0.0
    assert attributes["fallback"] is True  # new rooms start at 0, so the plain average leads

    await advance(hass, freezer, 60)
    attributes = hass.states.get(TEMPERATURE).attributes
    assert attributes["total_weight"] == round(2 * 0.001 * (1 - math.exp(-60 / 8)), 4)
    assert attributes["fallback"] is False


async def test_area_name_matching_ignores_case_and_spaces(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "  KIT chen ")
    await setup_instance(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["people"] == ["Alex"]


async def test_deleting_a_zone_clears_its_repairs_issue(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_instance(hass, [room("kitchen")])
    issues = ir.async_get(hass)
    issue_id = f"no_occupancy_source_{entry.entry_id}"
    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_corrupt_saved_state_is_ignored(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(
        hass, [(State(KITCHEN_WEIGHT, "0.8"), {"weight": 0.8})]  # no status etc.
    )
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen")])
    assert weight(hass, KITCHEN_WEIGHT) == 0.0  # starts fresh


async def test_person_with_missing_entity_is_in_no_room(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen"), person("Alex", "sensor.does_not_exist")])
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["people"] == []
    assert kitchen.attributes["person_present"] is False


async def test_renaming_a_zone_renames_its_device_not_its_entity_ids(
    hass: HomeAssistant,
) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_instance(hass, [room("kitchen")])
    hass.config_entries.async_update_entry(entry, title="Upstairs")
    await hass.async_block_till_done()

    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.name == "OORT Upstairs"
    entity_ids = {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert entity_ids == {KITCHEN_WEIGHT, TEMPERATURE}  # entity IDs keep the old name


@pytest.mark.parametrize(
    ("state", "attributes", "attribute"),
    [
        ("unavailable", {}, None),  # the location is unknown
        ("Kitchen", {}, "area_id"),  # the chosen attribute isn't there
    ],
)
async def test_person_with_unusable_location_is_in_no_room(
    hass: HomeAssistant, state: str, attributes: dict[str, str], attribute: str | None
) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", state, attributes)
    fields = {CONF_SOURCE_ATTRIBUTE: attribute} if attribute else {}
    await setup_instance(hass, [room("kitchen"), person("Alex", "sensor.alex_area", **fields)])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["people"] == []


async def test_person_leaving_an_occupied_room_uses_person_fall(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await setup_instance(
        hass,
        [
            room("kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]}),
            person("Alex", "sensor.alex_area"),
        ],
    )
    await advance(hass, freezer, 60)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1.0, abs=5e-5)

    hass.states.async_set("sensor.alex_area", "Garage")  # Alex leaves; motion still on
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert (kitchen.attributes["status"], kitchen.attributes["tau_name"]) == (
        "occupied",
        "person_fall",
    )
    await advance(hass, freezer, 3)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(0.5 + 0.5 * math.exp(-1), abs=5e-5)
    assert hass.states.get(KITCHEN_WEIGHT).attributes["tau_name"] == "person_fall"


async def test_total_outage_with_instant_dropout_holds_until_stale_limit(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """With a dropout tau of 0, recent readings still carry the output to the stale limit."""
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    await setup_instance(hass, [room("kitchen"), room("office")], tau_dropout=0, stale_limit=15)
    await advance(hass, freezer, 30)

    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    hass.states.async_set("sensor.office_temperature", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)  # every weight is now exactly 0
    temperature = hass.states.get(TEMPERATURE)
    assert float(temperature.state) == pytest.approx(22.0)
    assert temperature.attributes["fallback"] is True

    await advance(hass, freezer, 14)  # past the 15-minute stale limit
    assert hass.states.get(TEMPERATURE).state == STATE_UNAVAILABLE


def test_default_stale_limit_is_5_minutes() -> None:
    assert DEFAULTS["stale_limit"] == 5


@pytest.mark.parametrize(("downtime_minutes", "reading_used"), [(2, True), (120, False)])
async def test_restored_reading_used_only_after_a_short_downtime(
    hass: HomeAssistant, downtime_minutes: int, reading_used: bool
) -> None:
    """A reboot is bridged by saved readings; a long outage isn't."""
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(KITCHEN_WEIGHT, "1.0"),
                {
                    "weight": 1.0, "target": 1.0, "tau": 3.0, "status": "person",
                    "last_occupied_state": "person", "last_known_temperature": 30.0,
                    "dropout_since": None, "stale": False, "last_update": now,
                    "last_seen": now - downtime_minutes * 60,
                },
            )
        ],
    )
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)  # not back yet
    set_temperature(hass, "office", "20")
    await setup_instance(hass, [room("kitchen"), room("office")])
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["temperature_stale"] is not reading_used
    temperature = float(hass.states.get(TEMPERATURE).state)
    if reading_used:
        assert temperature > 25.0  # the kitchen's saved 30° still dominates
    else:
        assert temperature == pytest.approx(20.0)  # only the office counts


# ---------------------------------------------------------------------------
# Recorder load
# ---------------------------------------------------------------------------


async def test_temperature_change_does_not_rewrite_room_weights(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    await setup_instance(hass, [room("kitchen"), room("office")])
    await advance(hass, freezer, 5)
    kitchen_before = hass.states.get(KITCHEN_WEIGHT)
    temperature_before = hass.states.get(TEMPERATURE)

    freezer.tick(timedelta(seconds=5))
    set_temperature(hass, "office", "23")
    await hass.async_block_till_done()

    assert hass.states.get(KITCHEN_WEIGHT).last_reported == kitchen_before.last_reported, (
        "a temperature reading must not rewrite the weight sensors"
    )
    assert hass.states.get(TEMPERATURE).last_updated > temperature_before.last_updated


async def test_status_change_rewrites_that_room_at_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await setup_instance(
        hass,
        [room("kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]}), room("office")],
    )
    await advance(hass, freezer, 5)
    office_before = hass.states.get(OFFICE_WEIGHT)

    freezer.tick(timedelta(seconds=5))
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "occupied"
    assert hass.states.get(OFFICE_WEIGHT).last_reported == office_before.last_reported


async def test_stored_values_are_rounded(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20.123456")
    set_temperature(hass, "office", "24.987654")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_instance(hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")])
    await advance(hass, freezer, 2)
    weight_text = hass.states.get(KITCHEN_WEIGHT).state
    assert len(weight_text.split(".")[1]) <= 4, weight_text
    temperature_text = hass.states.get(TEMPERATURE).state
    assert len(temperature_text.split(".")[1]) <= 1, temperature_text


async def test_settled_weights_stop_changing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen")])
    await advance(hass, freezer, 120)
    before = hass.states.get(KITCHEN_WEIGHT)
    await advance(hass, freezer, 5)
    after = hass.states.get(KITCHEN_WEIGHT)
    assert after.last_updated == before.last_updated  # no new value or attribute, so no new row


async def test_total_weight_is_not_recorded(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen")])
    temperature = hass.states.get(TEMPERATURE)
    assert "total_weight" in temperature.attributes  # still on the entity
    assert "total_weight" in temperature.state_info["unrecorded_attributes"]


async def test_temperature_sensor_not_rewritten_when_nothing_visible_changes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An update that changes nothing shown must not create a new recorder row."""
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_instance(
        hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")]
    )
    await advance(hass, freezer, 60)
    before = hass.states.get(TEMPERATURE)

    freezer.tick(timedelta(seconds=5))
    hass.states.async_set("sensor.alex_area", "Kitchen", {"rssi": -70})  # attribute-only
    await hass.async_block_till_done()
    assert hass.states.get(TEMPERATURE).last_updated == before.last_updated


async def test_changing_unrecognised_template_result_warns_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A template returning a new unrecognised value each time warns only once."""
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.window_battery", "50%")
    await setup_instance(
        hass,
        [room("kitchen", **{CONF_OPENING_TEMPLATE: "{{ states('sensor.window_battery') }}"})],
    )
    for value in ("49%", "48%", "47%"):
        hass.states.async_set("sensor.window_battery", value)
        await hass.async_block_till_done()

    def warnings() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "OORT" in r.getMessage()]

    assert len(warnings()) == 1

    # Back to a meaningful value, then broken again: that's a new problem, so warn again.
    hass.states.async_set("sensor.window_battery", "off")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.window_battery", "low")
    await hass.async_block_till_done()
    assert len(warnings()) == 2


@pytest.mark.parametrize(
    ("template", "opened"),
    [
        ("{{ true }}", True),
        ("on", True),
        ("enable", True),
        ("{{ 2 }}", True),  # Home Assistant's rule: any non-zero number is true
        ("{{ 21.5 }}", True),
        ("{{ 0 }}", False),
        ("off", False),
    ],
)
async def test_opening_template_follows_home_assistants_true_rule(
    hass: HomeAssistant, template: str, opened: bool
) -> None:
    """The README and form text document this rule; keep them in step."""
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: template})])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["active"] is not opened


async def test_person_fall_carries_on_after_a_restart(hass: HomeAssistant) -> None:
    """Across a restart, the saved tau name keeps a room that a person left on person fall."""
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(KITCHEN_WEIGHT, "0.8"),
                {
                    "weight": 0.8, "target": 0.5, "tau": 3.0, "tau_name": "person_fall",
                    "status": "occupied", "last_occupied_state": "occupied",
                    "last_known_temperature": 20.0, "last_seen": now,
                    "dropout_since": None, "stale": False, "last_update": now,
                },
            )
        ],
    )
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await setup_instance(
        hass, [room("kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]})]
    )
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["status"] == "occupied"
    assert kitchen.attributes["tau_name"] == "person_fall"


async def test_room_going_stale_is_written_on_the_next_event_not_just_the_timer(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Staleness is part of what a weight sensor shows, so any update writes it at once."""
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    await setup_instance(hass, [room("kitchen"), room("office")])
    await advance(hass, freezer, 5)
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["temperature_stale"] is False

    # Past the stale limit without a timer tick; an unrelated reading triggers the update.
    freezer.tick(timedelta(minutes=DEFAULTS["stale_limit"], seconds=30))
    set_temperature(hass, "office", "23")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["temperature_stale"] is True


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


async def test_unit_change_without_reload_keeps_the_output_right(
    hass: HomeAssistant,
) -> None:
    """The zone's unit is fixed at load, so a switch can't double-convert its output."""
    set_temperature(hass, "kitchen", "20")
    await setup_instance(hass, [room("kitchen")])
    hass.config.units = US_CUSTOMARY_SYSTEM  # changed without the zone reloading
    set_temperature(hass, "kitchen", "20.5")  # the sensor still reports °C
    await hass.async_block_till_done()
    temperature = hass.states.get(TEMPERATURE)
    assert temperature.attributes["unit_of_measurement"] == "°F"
    assert float(temperature.state) == pytest.approx(68.9, abs=0.01)


async def test_unit_system_change_and_reload_never_double_convert(hass: HomeAssistant) -> None:
    """The zone keeps the unit it was created with, across reloads."""
    set_temperature(hass, "kitchen", "20.5")
    entry = await setup_instance(hass, [room("kitchen")])
    await hass.config.async_update(unit_system="us_customary")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.unit == "°C"
    temperature = hass.states.get(TEMPERATURE)
    # Home Assistant keeps a registered sensor's unit; either way the value must match it.
    if temperature.attributes["unit_of_measurement"] == "°C":
        assert float(temperature.state) == pytest.approx(20.5, abs=0.01)
    else:
        assert float(temperature.state) == pytest.approx(68.9, abs=0.01)


async def test_saved_reading_is_converted_if_the_unit_changed_while_down(
    hass: HomeAssistant,
) -> None:
    now = dt_util.utcnow().timestamp()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(KITCHEN_WEIGHT, "1.0"),
                {
                    "weight": 1.0, "target": 1.0, "tau": 3.0, "status": "person",
                    "last_occupied_state": "person", "last_known_temperature": 20.0,
                    "last_seen": now, "dropout_since": None, "stale": False,
                    "last_update": now, "temperature_unit": "°C",
                },
            )
        ],
    )
    hass.config.units = US_CUSTOMARY_SYSTEM  # a zone created now works in °F
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)  # not back yet
    entry = await setup_instance(hass, [room("kitchen")])
    assert entry.runtime_data.unit == "°F"
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(68.0, abs=0.01)
