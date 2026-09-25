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
from homeassistant.helpers import area_registry as ar, entity_registry as er, issue_registry as ir

from custom_components.overengineered_occupied_room_temperature.const import (
    CONF_ACTIVE_TEMPLATE,
    CONF_AREA_ID,
    CONF_DEFAULTS,
    CONF_NAME,
    CONF_OCCUPANCY_SENSORS,
    CONF_OCCUPANCY_TEMPLATE,
    CONF_OVERRIDES,
    CONF_PEOPLE,
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
        data={CONF_NAME: "Home"},
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
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1 - math.exp(-1), abs=1e-6)

    await advance(hass, freezer, 60)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1.0, abs=1e-6)
    assert weight(hass, OFFICE_WEIGHT) == pytest.approx(0.001, abs=1e-6)
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(
        (20 * 1.0 + 24 * 0.001) / 1.001, abs=1e-3
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


async def test_active_template_false_fades_room_out(
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
                **{CONF_ACTIVE_TEMPLATE: "{{ is_state('binary_sensor.kitchen_window', 'off') }}"},
            ),
            person("Alex", "sensor.alex_area"),
        ],
    )
    await advance(hass, freezer, 60)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1.0, abs=1e-6)

    hass.states.async_set("binary_sensor.kitchen_window", "on")
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["status"] == "inactive"
    assert kitchen.attributes["active"] is False
    await advance(hass, freezer, 1)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(math.exp(-1), abs=1e-6)  # deactivate τ


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
    closed = "{{ is_state('input_boolean.windows_open', 'off') }}"
    hass.states.async_set("input_boolean.windows_open", "off")
    await setup_instance(
        hass,
        [
            room("kitchen", **{CONF_ACTIVE_TEMPLATE: closed}),
            room("office", **{CONF_ACTIVE_TEMPLATE: closed}),
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
