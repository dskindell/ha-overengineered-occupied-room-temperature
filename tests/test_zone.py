"""End-to-end tests: a real zone in an in-memory Home Assistant."""

from __future__ import annotations

from datetime import timedelta
import math
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, UnitOfTemperature
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
    restore_state,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
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
    CONF_TEMPERATURE_SENSORS,
    CONF_TEMPERATURE_UNIT,
    CONF_VALUE_TYPE,
    DEFAULTS,
    DOMAIN,
)
from custom_components.overengineered_occupied_room_temperature.engine import (
    RoomState,
    Status,
    TauName,
)
from custom_components.overengineered_occupied_room_temperature.storage import (
    RoomExtraData,
)
from custom_components.overengineered_occupied_room_temperature.zone import (
    repairs_issue_id,
    room_unique_id,
)
from tests.helpers import SAVED, stored_settings

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
            CONF_TEMPERATURE_SENSORS: [f"sensor.{area_id}_temperature"],
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
        **stored_settings(**settings),
        CONF_ROOMS: {key: data for kind, key, data in items if kind == "room"},
        CONF_PEOPLE: {key: data for kind, key, data in items if kind == "person"},
    }


def set_temperature(hass: HomeAssistant, area_id: str, value: str, unit: str = "°C") -> None:
    hass.states.async_set(
        f"sensor.{area_id}_temperature",
        value,
        {"unit_of_measurement": unit, "device_class": "temperature"},
    )


async def setup_zone(
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
        data={CONF_NAME: "Home", CONF_TEMPERATURE_UNIT: hass.config.units.temperature_unit},
        options=zone_options(items, **settings),
    )
    entry.add_to_hass(hass)
    # Register the room sensors as a previous run would have, so saved state (from
    # mock_restore_cache_with_extra_data) is found by entity ID at setup.
    registry = er.async_get(hass)
    for kind, key, _ in items:
        if kind == "room":
            registry.async_get_or_create(
                "sensor",
                DOMAIN,
                room_unique_id(entry, key),
                suggested_object_id=f"oort_home_{key}_weight",
                config_entry=entry,
            )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int = 0, *, seconds: float = 0
) -> None:
    """Move time on one minute at a time (so the minute timer fires), then by ``seconds``."""
    steps = [timedelta(minutes=1)] * minutes + ([timedelta(seconds=seconds)] if seconds else [])
    for step in steps:
        freezer.tick(step)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def record_states(hass: HomeAssistant) -> list[tuple[str, State]]:
    """Collect every state written from now on."""
    seen: list[tuple[str, State]] = []

    def _on(event: Any) -> None:
        if event.data["new_state"] is not None:
            seen.append((event.data["entity_id"], event.data["new_state"]))

    hass.bus.async_listen("state_changed", _on)
    return seen


async def start_home_assistant(hass: HomeAssistant) -> None:
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


def weight(hass: HomeAssistant, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


def restore_room_state(hass: HomeAssistant, *, unit: str = "°C", **fields: Any) -> None:
    """Seed Home Assistant's restore store with the kitchen's saved state, written
    the way the integration writes it (RoomState → RoomExtraData)."""
    state = RoomState(**fields)
    mock_restore_cache_with_extra_data(
        hass, [(State(KITCHEN_WEIGHT, str(state.weight)), RoomExtraData(state, unit).as_dict())]
    )


@pytest.fixture(autouse=True)
def metric(hass: HomeAssistant) -> None:
    assert hass.config.units.temperature_unit == UnitOfTemperature.CELSIUS


async def test_creates_device_and_entities(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    await setup_zone(hass, [room("kitchen"), room("office")])

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
    await setup_zone(hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")])

    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["status"] == "person"
    assert kitchen.attributes["people"] == ["Alex"]
    assert float(kitchen.state) == 0.0  # new rooms start at 0
    assert kitchen.attributes["tau_name"] == "person_rise"
    assert kitchen.attributes["tau"] == 3.0
    # An empty new room rises from 0 to the unoccupied weight.
    assert hass.states.get(OFFICE_WEIGHT).attributes["tau_name"] == "occupancy_rise"

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
    await setup_zone(
        hass,
        [
            room("kitchen"),
            person(
                "Alex",
                "sensor.alex_phone",
                **{CONF_VALUE_TYPE: "area_id", CONF_SOURCE_ATTRIBUTE: "area_id"},
            ),
        ],
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["person_present"] is True


async def test_unregistered_area_matches_no_room(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Garage")
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "unoccupied"


async def test_occupancy_sensor_and_template(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    hass.states.async_set("input_boolean.office_desk", "off")
    await setup_zone(
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
    await setup_zone(
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
    assert kitchen.attributes["status"] == "open"
    assert kitchen.attributes["open"] is True
    await advance(hass, freezer, 1)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(math.exp(-1), abs=5e-5)  # open τ


async def test_any_opening_entity_on_makes_room_open(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    hass.states.async_set("input_boolean.kitchen_door_open", "off")
    await setup_zone(
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
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is False

    hass.states.async_set("input_boolean.kitchen_door_open", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == "open"

    hass.states.async_set("input_boolean.kitchen_door_open", "off")
    hass.states.async_set("binary_sensor.kitchen_window", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is True

    hass.states.async_set("binary_sensor.kitchen_window", "off")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is False


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_unavailable_opening_entity_is_not_open(hass: HomeAssistant, state: str) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", state)
    await setup_zone(
        hass, [room("kitchen", **{CONF_OPENING_SENSORS: ["binary_sensor.kitchen_window"]})]
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is False


async def test_missing_opening_entity_is_not_open(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_zone(
        hass, [room("kitchen", **{CONF_OPENING_SENSORS: ["binary_sensor.does_not_exist"]})]
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is False


async def test_opening_entities_and_template_combine(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    hass.states.async_set("input_boolean.heater", "off")
    await setup_zone(
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
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is False
    hass.states.async_set("input_boolean.heater", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is True  # template alone


async def test_fahrenheit_sensor_is_converted(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "68", unit="°F")
    await setup_zone(hass, [room("kitchen")])
    # Only one room: the plain-average term dominates while it rises from 0.
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0)


async def test_dropout_then_stale_goes_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen")], stale_limit=10)
    await advance(hass, freezer, 30)

    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["temperature_available"] is False
    assert kitchen.attributes["status"] == "dropout"
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0)  # last known

    await advance(hass, freezer, 11)
    assert hass.states.get(KITCHEN_WEIGHT).attributes["temperature_stale"] is True
    assert hass.states.get(TEMPERATURE).state == STATE_UNAVAILABLE

    set_temperature(hass, "kitchen", "21")
    await hass.async_block_till_done()
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(21.0)


async def test_all_rooms_open_uses_plain_average(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    hass.states.async_set("input_boolean.windows_open", "off")
    await setup_zone(
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
    entry = await setup_zone(hass, [room("kitchen")])
    issue = ir.async_get(hass).async_get_issue(DOMAIN, repairs_issue_id(entry))
    assert issue is not None
    assert issue.translation_placeholders == {"zone": "Home", "rooms": "Kitchen"}


async def test_no_repairs_issue_when_people_exist(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    assert ir.async_get(hass).async_get_issue(DOMAIN, repairs_issue_id(entry)) is None


async def test_removing_a_room_removes_its_entity(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    entry = await setup_zone(hass, [room("kitchen"), room("office")])
    registry = er.async_get(hass)
    assert registry.async_get(OFFICE_WEIGHT) is not None

    options = {**entry.options, CONF_ROOMS: {"kitchen": entry.options[CONF_ROOMS]["kitchen"]}}
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    assert registry.async_get(OFFICE_WEIGHT) is None
    assert registry.async_get(KITCHEN_WEIGHT) is not None


async def test_weights_restored_after_restart(hass: HomeAssistant) -> None:
    restore_room_state(
        hass,
        weight=0.8,
        target=1.0,
        tau=3.0,
        status=Status.PERSON,
        last_occupied_state=Status.PERSON,
        last_known_temperature=20.0,
        last_update=12345.0,
    )
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen")])
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
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])

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
    await setup_zone(
        hass,
        [
            room("kitchen", **{CONF_OVERRIDES: {"tau_person_rise": 10, "w_person": 0.8}}),
            room("office"),
            person("Alex", "sensor.alex_area"),
            person("Sam", "sensor.sam_area"),
        ],
    )
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["tau"] == 10
    assert kitchen.attributes["target_weight"] == 0.8
    assert hass.states.get(OFFICE_WEIGHT).attributes["tau"] == DEFAULTS["tau_person_rise"]

    await advance(hass, freezer, 10)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(0.8 * (1 - math.exp(-1)), abs=5e-5)
    assert weight(hass, OFFICE_WEIGHT) == pytest.approx(1 - math.exp(-10 / 3), abs=5e-5)


async def test_room_added_through_configure_appears_after_save(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    entry = await setup_zone(hass, [room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ false }}"})])
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
    await setup_zone(
        hass,
        [
            room(
                "kitchen",
                **{CONF_OPENING_TEMPLATE: "{{ 1 / 0 }}", CONF_OCCUPANCY_TEMPLATE: "{{ 2 / 0 }}"},
            )
        ],
    )
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["open"] is False  # only a clear "true" means open
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
    await setup_zone(hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: result})])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is False
    assert not [
        r
        for r in caplog.records
        if r.levelname in ("WARNING", "ERROR") and "OORT" in r.getMessage()
    ]


async def test_unrecognised_template_result_is_not_open_and_warned_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: "maybe"})])
    await advance(hass, freezer, 3)
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "maybe" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    ("value", "attributes"),
    [
        ("20", {}),  # no unit
        ("20", {"unit_of_measurement": "%"}),  # not a temperature unit
        ("warm", {"unit_of_measurement": "°C"}),  # not a number
        ("nan", {"unit_of_measurement": "°C"}),  # not finite
        ("inf", {"unit_of_measurement": "°C"}),
        ("1e400", {"unit_of_measurement": "°C"}),  # overflows to inf
    ],
)
async def test_unusable_temperature_counts_as_dropout(
    hass: HomeAssistant, value: str, attributes: dict[str, str]
) -> None:
    set_temperature(hass, "office", "24")
    hass.states.async_set("sensor.kitchen_temperature", value, attributes)
    await setup_zone(hass, [room("kitchen"), room("office")])
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["temperature_available"] is False
    assert kitchen.attributes["status"] == "dropout"
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(24.0)


async def test_any_of_several_occupancy_sensors_occupies_the_room(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    hass.states.async_set("input_boolean.kitchen_cooking", "off")
    await setup_zone(
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
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
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
    await setup_zone(hass, [room("kitchen"), room("office")])
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
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["people"] == ["Alex"]


async def test_disabling_a_zone_clears_its_repairs_issue(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_zone(hass, [room("kitchen")])
    issues = ir.async_get(hass)
    repairs_id = repairs_issue_id(entry)
    assert issues.async_get_issue(DOMAIN, repairs_id) is not None

    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, repairs_id) is None

    await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, repairs_id) is not None, "set again when enabled"


async def test_deleting_a_zone_clears_its_repairs_issue(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_zone(hass, [room("kitchen")])
    issues = ir.async_get(hass)
    repairs_id = repairs_issue_id(entry)
    assert issues.async_get_issue(DOMAIN, repairs_id) is not None

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, repairs_id) is None


async def test_corrupt_saved_state_is_ignored(hass: HomeAssistant) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [(State(KITCHEN_WEIGHT, "0.8"), {"weight": 0.8})],  # no status etc.
    )
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen")])
    assert weight(hass, KITCHEN_WEIGHT) == 0.0  # starts fresh


async def test_person_with_missing_entity_is_in_no_room(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.does_not_exist")])
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["people"] == []
    assert kitchen.attributes["person_present"] is False


async def test_renaming_a_zone_renames_its_device_not_its_entity_ids(
    hass: HomeAssistant,
) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_zone(hass, [room("kitchen")])
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
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area", **fields)])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["people"] == []


async def test_person_leaving_an_occupied_room_uses_person_fall(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await setup_zone(
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
    await setup_zone(hass, [room("kitchen"), room("office")], tau_dropout=0, stale_limit=15)
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


@pytest.mark.parametrize(("downtime_minutes", "reading_used"), [(2, True), (120, False)])
async def test_restored_reading_used_only_after_a_short_downtime(
    hass: HomeAssistant, downtime_minutes: int, reading_used: bool
) -> None:
    """A reboot is bridged by saved readings; a long outage isn't."""
    now = dt_util.utcnow().timestamp()
    restore_room_state(
        hass,
        weight=1.0,
        target=1.0,
        tau=3.0,
        status=Status.PERSON,
        last_occupied_state=Status.PERSON,
        last_known_temperature=30.0,
        last_update=now,
        last_seen=now - downtime_minutes * 60,
    )
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)  # not back yet
    set_temperature(hass, "office", "20")
    await setup_zone(hass, [room("kitchen"), room("office")])
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
    await setup_zone(hass, [room("kitchen"), room("office")])
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
    await setup_zone(
        hass,
        [
            room("kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]}),
            room("office"),
        ],
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
    await setup_zone(hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")])
    await advance(hass, freezer, 2)
    weight_text = hass.states.get(KITCHEN_WEIGHT).state
    assert len(weight_text.split(".")[1]) <= 4, weight_text
    temperature_text = hass.states.get(TEMPERATURE).state
    assert len(temperature_text.split(".")[1]) <= 1, temperature_text


async def test_settled_weights_stop_changing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen")])
    await advance(hass, freezer, 120)
    before = hass.states.get(KITCHEN_WEIGHT)
    await advance(hass, freezer, 5)
    after = hass.states.get(KITCHEN_WEIGHT)
    assert after.last_updated == before.last_updated  # no new value or attribute, so no new row


async def test_total_weight_is_not_recorded(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen")])
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
    await setup_zone(hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")])
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
    await setup_zone(
        hass,
        [room("kitchen", **{CONF_OPENING_TEMPLATE: "{{ states('sensor.window_battery') }}"})],
    )
    for value in ("49%", "48%", "47%"):
        hass.states.async_set("sensor.window_battery", value)
        await hass.async_block_till_done()

    def warnings() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING" and "OORT" in r.getMessage()
        ]

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
    await setup_zone(hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: template})])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["open"] is opened


async def test_person_fall_carries_on_after_a_restart(hass: HomeAssistant) -> None:
    """Across a restart, the saved tau name keeps a room that a person left on person fall."""
    now = dt_util.utcnow().timestamp()
    restore_room_state(
        hass,
        weight=0.8,
        target=0.5,
        tau=3.0,
        tau_name=TauName.PERSON_FALL,
        status=Status.OCCUPIED,
        last_occupied_state=Status.OCCUPIED,
        last_known_temperature=20.0,
        last_seen=now,
        last_update=now,
    )
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await setup_zone(
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
    await setup_zone(hass, [room("kitchen"), room("office")])
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
    await setup_zone(hass, [room("kitchen")])
    hass.config.units = US_CUSTOMARY_SYSTEM  # changed without the zone reloading
    set_temperature(hass, "kitchen", "20.5")  # the sensor still reports °C
    await hass.async_block_till_done()
    temperature = hass.states.get(TEMPERATURE)
    assert temperature.attributes["unit_of_measurement"] == "°F"
    assert float(temperature.state) == pytest.approx(68.9, abs=0.01)


async def test_unit_system_change_and_reload_never_double_convert(hass: HomeAssistant) -> None:
    """The zone keeps the unit it was created with, across reloads."""
    set_temperature(hass, "kitchen", "20.5")
    entry = await setup_zone(hass, [room("kitchen")])
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
    restore_room_state(
        hass,
        weight=1.0,
        target=1.0,
        tau=3.0,
        status=Status.PERSON,
        last_occupied_state=Status.PERSON,
        last_known_temperature=20.0,
        last_seen=now,
        last_update=now,
    )
    hass.config.units = US_CUSTOMARY_SYSTEM  # a zone created now works in °F
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)  # not back yet
    entry = await setup_zone(hass, [room("kitchen")])
    assert entry.runtime_data.unit == "°F"
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(68.0, abs=0.01)


# ---------------------------------------------------------------------------
# First states
# ---------------------------------------------------------------------------


async def test_setup_never_writes_a_placeholder_state(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    seen = record_states(hass)
    await setup_zone(hass, [room("kitchen")])
    await hass.async_block_till_done()
    temperatures = [state.state for entity_id, state in seen if entity_id == TEMPERATURE]
    assert STATE_UNAVAILABLE not in temperatures
    first_weight = next(state for entity_id, state in seen if entity_id == KITCHEN_WEIGHT)
    assert first_weight.attributes["open"] is False  # inputs already evaluated


async def test_reload_never_writes_a_placeholder_state(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    entry = await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    await hass.async_block_till_done()
    seen = record_states(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    temperatures = [state.state for entity_id, state in seen if entity_id == TEMPERATURE]
    # Unloading marks entities unavailable (Home Assistant does that); what matters is
    # that the reloaded zone's first state is a real value.
    after_unload = (
        temperatures[temperatures.index(STATE_UNAVAILABLE) + 1 :]
        if STATE_UNAVAILABLE in temperatures
        else temperatures
    )
    assert after_unload
    assert STATE_UNAVAILABLE not in after_unload
    weights = [
        state
        for entity_id, state in seen
        if entity_id == KITCHEN_WEIGHT and state.state != STATE_UNAVAILABLE
    ]
    assert weights[0].attributes["status"] == "person"


async def test_open_room_does_not_drive_the_output_during_an_outage(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The only room still reporting is open, but a closed room has a recent reading."""
    set_temperature(hass, "kitchen", "12")  # the open room (e.g. a sunroom with the door open)
    set_temperature(hass, "office", "20")
    hass.states.async_set("binary_sensor.kitchen_door", "on")
    await setup_zone(
        hass,
        [room("kitchen", **{CONF_OPENING_SENSORS: ["binary_sensor.kitchen_door"]}), room("office")],
        stale_limit=60,
    )
    await advance(hass, freezer, 60)  # kitchen has faded out (open)
    hass.states.async_set("sensor.office_temperature", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await advance(hass, freezer, 30)  # office weight fades too; its reading isn't stale yet
    # Only the open kitchen is live, but the closed office's recent reading wins.
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0, abs=0.1)

    await advance(hass, freezer, 31)  # past the 60-minute stale limit
    # Nothing closed is left, so the open kitchen is used rather than going unavailable.
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(12.0, abs=0.1)


# ---------------------------------------------------------------------------
# Non-finite readings and failing entities
# ---------------------------------------------------------------------------


async def test_non_finite_reading_keeps_the_last_good_one(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "21")
    await setup_zone(hass, [room("kitchen")])
    set_temperature(hass, "kitchen", "nan")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["temperature_available"] is False
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(21.0)


async def test_reading_that_overflows_on_conversion_is_unusable(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM  # zone in °F
    set_temperature(hass, "office", "70", unit="°F")
    set_temperature(hass, "kitchen", "1e308")  # finite in °C, inf in °F
    await setup_zone(hass, [room("kitchen"), room("office")])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["temperature_available"] is False
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(70.0)


async def test_saved_non_finite_reading_is_not_restored(hass: HomeAssistant) -> None:
    now = dt_util.utcnow().timestamp()
    restore_room_state(
        hass,
        weight=1.0,
        target=1.0,
        tau=3.0,
        status=Status.PERSON,
        last_occupied_state=Status.PERSON,
        last_known_temperature=math.nan,
        last_seen=now,
        last_update=now,
    )
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    set_temperature(hass, "office", "24")
    await setup_zone(hass, [room("kitchen"), room("office")])
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(24.0)


async def test_one_failing_entity_does_not_stop_the_others(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    set_temperature(hass, "kitchen", "20")
    entry = await setup_zone(hass, [room("kitchen")])

    def fail() -> None:
        raise ValueError("boom")

    entry.runtime_data._listeners.insert(0, fail)
    set_temperature(hass, "kitchen", "22")
    await hass.async_block_till_done()
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(22.0, abs=0.1)
    assert "Error updating an entity of zone Home" in caplog.text


async def test_re_added_room_starts_at_zero_not_its_old_state(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """HA keeps a removed entity's saved state for 7 days."""
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "24")
    office = room("office", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    entry = await setup_zone(hass, [room("kitchen"), office])
    await advance(hass, freezer, 60)
    assert weight(hass, OFFICE_WEIGHT) == pytest.approx(0.5, abs=0.01)

    all_rooms = entry.options[CONF_ROOMS]
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ROOMS: {"kitchen": all_rooms["kitchen"]}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(OFFICE_WEIGHT) is None
    saved = restore_state.async_get(hass).last_states[OFFICE_WEIGHT]
    assert saved.extra_data.as_dict()["weight"] == pytest.approx(0.5, abs=0.01)

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_ROOMS: all_rooms})
    await hass.async_block_till_done()
    assert weight(hass, OFFICE_WEIGHT) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Templates in the first state
# ---------------------------------------------------------------------------


async def test_first_state_reflects_the_occupancy_template(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    seen = record_states(hass)
    await setup_zone(hass, [room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})])
    statuses = [
        state.attributes["status"] for entity_id, state in seen if entity_id == KITCHEN_WEIGHT
    ]
    assert statuses
    assert set(statuses) == {"occupied"}


async def test_first_state_reflects_the_opening_template(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "20")
    seen = record_states(hass)
    await setup_zone(hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: "{{ true }}"})])
    first = next(state for entity_id, state in seen if entity_id == KITCHEN_WEIGHT)
    assert first.attributes["open"] is True
    assert first.attributes["status"] == "open"


async def test_person_fall_carries_on_after_a_restart_with_an_occupancy_template(
    hass: HomeAssistant,
) -> None:
    now = dt_util.utcnow().timestamp()
    restore_room_state(
        hass,
        weight=0.8,
        target=0.5,
        tau=3.0,
        tau_name=TauName.PERSON_FALL,
        status=Status.OCCUPIED,
        last_occupied_state=Status.OCCUPIED,
        last_known_temperature=20.0,
        last_seen=now,
        last_update=now,
    )
    set_temperature(hass, "kitchen", "20")
    await setup_zone(hass, [room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})])
    assert hass.states.get(KITCHEN_WEIGHT).attributes["tau_name"] == "person_fall"


async def test_template_failing_at_startup_is_logged_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    set_temperature(hass, "kitchen", "20")
    await setup_zone(
        hass, [room("kitchen", **{CONF_OPENING_TEMPLATE: "{{ states('x') | float }}"})]
    )
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["open"] is False  # a failing opening template isn't open
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "OORT zone" in r.getMessage()]
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# Temperature sensor rows while weights move
# ---------------------------------------------------------------------------


async def test_temperature_readings_alone_do_not_rewrite_the_temperature_sensor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_zone(hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")])
    await advance(hass, freezer, 3)  # the kitchen weight is still rising
    seen = record_states(hass)
    for i in range(11):  # 55 s of readings; the output stays 20.0
        freezer.tick(timedelta(seconds=5))
        set_temperature(hass, "office", f"20.0{i % 2}")
        await hass.async_block_till_done()
    assert [state for entity_id, state in seen if entity_id == TEMPERATURE] == []

    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)  # the minute timer refreshes total_weight
    await hass.async_block_till_done()
    rows = [state for entity_id, state in seen if entity_id == TEMPERATURE]
    assert len(rows) == 1
    assert rows[0].attributes["total_weight"] == pytest.approx(
        weight(hass, KITCHEN_WEIGHT) + weight(hass, OFFICE_WEIGHT), abs=2e-4
    )


async def test_a_person_moving_rewrites_the_temperature_sensor_at_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "20")  # same reading, so the value can't change
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_zone(hass, [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")])
    await advance(hass, freezer, 3)
    seen = record_states(hass)
    freezer.tick(timedelta(seconds=30))
    hass.states.async_set("sensor.alex_area", "Office")
    await hass.async_block_till_done()
    rows = [state for entity_id, state in seen if entity_id == TEMPERATURE]
    assert len(rows) == 1, "written straight away, with the weights as of the move"
    assert rows[0].attributes["total_weight"] == pytest.approx(
        weight(hass, KITCHEN_WEIGHT) + weight(hass, OFFICE_WEIGHT), abs=2e-4
    )


# ---------------------------------------------------------------------------
# A tau of 0 is instant
# ---------------------------------------------------------------------------


async def test_open_and_dropout_are_separate_statuses(hass: HomeAssistant) -> None:
    """Status says why a room is left out; open wins over dropout."""
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    await setup_zone(
        hass, [room("kitchen", **{CONF_OPENING_SENSORS: ["binary_sensor.kitchen_window"]})]
    )
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert (kitchen.attributes["status"], kitchen.attributes["open"]) == ("dropout", False)
    assert kitchen.attributes["tau_name"] == "dropout"

    hass.states.async_set("binary_sensor.kitchen_window", "on")
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert (kitchen.attributes["status"], kitchen.attributes["open"]) == ("open", True)
    assert kitchen.attributes["tau_name"] == "open"


async def test_open_room_leaves_the_output_at_once_with_open_tau_0(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "10")
    set_temperature(hass, "office", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    hass.states.async_set("binary_sensor.kitchen_window", "off")
    await setup_zone(
        hass,
        [
            room("kitchen", **{CONF_OPENING_SENSORS: ["binary_sensor.kitchen_window"]}),
            room("office"),
            person("Alex", "sensor.alex_area"),
        ],
        tau_open=0.0,
    )
    await advance(hass, freezer, 30)
    assert float(hass.states.get(TEMPERATURE).state) < 11

    freezer.tick(timedelta(seconds=5))
    hass.states.async_set("binary_sensor.kitchen_window", "on")
    await hass.async_block_till_done()
    assert weight(hass, KITCHEN_WEIGHT) == 0.0
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0, abs=0.1)


async def test_dropped_out_room_leaves_the_output_at_once_with_dropout_tau_0(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "10")
    set_temperature(hass, "office", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_zone(
        hass,
        [room("kitchen"), room("office"), person("Alex", "sensor.alex_area")],
        tau_dropout=0.0,
    )
    await advance(hass, freezer, 30)
    freezer.tick(timedelta(seconds=5))
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert weight(hass, KITCHEN_WEIGHT) == 0.0
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0, abs=0.1)


# ---------------------------------------------------------------------------
# Settings added or removed
# ---------------------------------------------------------------------------


async def test_zone_saved_without_a_setting_loads_with_its_default(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    areas = ar.async_get(hass)
    areas.async_create("Kitchen")
    options = zone_options([room("kitchen")])
    stored = {key: value for key, value in options[CONF_DEFAULTS].items() if key != "w_base"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        version=1,
        data={CONF_NAME: "Home", CONF_TEMPERATURE_UNIT: "°C"},
        options={**options, CONF_DEFAULTS: {**stored, "retired_setting": 7}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 120)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(DEFAULTS["w_base"], abs=5e-5)


# ---------------------------------------------------------------------------
# Saved room state
# ---------------------------------------------------------------------------


async def test_room_restored_from_state_saved_by_another_version(hass: HomeAssistant) -> None:
    data = {k: v for k, v in SAVED.items() if k != "last_seen"}
    mock_restore_cache_with_extra_data(
        hass, [(State(KITCHEN_WEIGHT, "0.8"), {**data, "retired_field": 1})]
    )
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Grace period, reload and unload
# ---------------------------------------------------------------------------


async def test_grace_lasts_two_minutes_and_held_time_is_not_counted(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass.set_state(CoreState.not_running)
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    await start_home_assistant(hass)

    await advance(hass, freezer, seconds=119)
    assert weight(hass, KITCHEN_WEIGHT) == 0.0  # still held just before 2 minutes
    await advance(hass, freezer, seconds=2)
    released = weight(hass, KITCHEN_WEIGHT)
    # Released at 2 minutes, but the held time isn't applied: at most the last
    # few seconds count, not the whole 2 minutes (which would give ~0.49).
    assert 0.0 < released < 0.02

    await advance(hass, freezer, seconds=60)
    expected = 1 - (1 - released) * math.exp(-1 / 3)  # one minute at person rise 3
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(expected, abs=5e-5)


async def test_reload_during_grace_releases_the_hold(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    hass.set_state(CoreState.not_running)
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    entry = await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    await start_home_assistant(hass)
    await advance(hass, freezer, seconds=30)
    assert weight(hass, KITCHEN_WEIGHT) == 0.0

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, seconds=60)  # no grace after a reload
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1 - math.exp(-1 / 3), abs=0.01)

    await advance(hass, freezer, seconds=60)  # past when the old zone's grace would have ended
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1 - math.exp(-2 / 3), abs=0.01)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_nothing_reaches_a_zone_after_it_is_unloaded(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    entry = await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    runtime = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    seen = record_states(hass)
    before = runtime.result
    set_temperature(hass, "kitchen", "25")
    hass.states.async_set("sensor.alex_area", "Office")
    await advance(hass, freezer, seconds=180)
    assert [e for e, _ in seen if e.startswith("sensor.oort_")] == []
    assert runtime.result is before  # the runtime didn't recompute either
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_changed_settings_take_effect_on_reload_without_a_jump(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    entry = await setup_zone(hass, [room("kitchen"), person("Alex", "sensor.alex_area")])
    await advance(hass, freezer, 60)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(1.0, abs=5e-5)

    options = entry.options
    hass.config_entries.async_update_entry(
        entry, options={**options, CONF_DEFAULTS: {**options[CONF_DEFAULTS], "w_person": 0.5}}
    )
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert float(kitchen.state) == pytest.approx(1.0, abs=5e-5)  # no jump on reload
    assert kitchen.attributes["target_weight"] == 0.5

    await advance(hass, freezer, 3)
    assert weight(hass, KITCHEN_WEIGHT) == pytest.approx(0.5 + 0.5 * math.exp(-1), abs=5e-5)


async def test_grace_end_writes_every_weight_sensor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass.set_state(CoreState.not_running)
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "22")
    await setup_zone(hass, [room("kitchen"), room("office")])
    # So the grace end doesn't coincide with the minute timer.
    await advance(hass, freezer, seconds=30)
    await start_home_assistant(hass)
    await advance(hass, freezer, seconds=100)  # 130 s after setup: past the first minute tick
    reported = {e: hass.states.get(e).last_reported for e in (KITCHEN_WEIGHT, OFFICE_WEIGHT)}

    await advance(hass, freezer, seconds=21)  # grace ends 120 s after start (150 s after setup)
    for entity_id, before in reported.items():
        assert hass.states.get(entity_id).last_reported > before, entity_id


# ---------------------------------------------------------------------------
# People, shared templates and units
# ---------------------------------------------------------------------------


async def test_one_template_text_used_by_two_rooms(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "22")
    hass.states.async_set("input_text.mode", "off")
    shared = "{{ states('input_text.mode') }}"
    await setup_zone(
        hass,
        [
            room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: shared}),
            room("office", **{CONF_OPENING_TEMPLATE: shared}),
        ],
    )
    hass.states.async_set("input_text.mode", "on")
    await hass.async_block_till_done()
    assert hass.states.get(KITCHEN_WEIGHT).attributes["occupied"] is True
    assert hass.states.get(OFFICE_WEIGHT).attributes["open"] is True

    hass.states.async_set("input_text.mode", "maybe")
    await hass.async_block_till_done()
    [warning] = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and r.getMessage().startswith("OORT zone")
    ]
    assert "Kitchen occupancy template" in warning
    assert "Office opening template" in warning


async def test_one_of_two_people_leaving_keeps_the_room_person_occupied(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    set_temperature(hass, "kitchen", "20")
    set_temperature(hass, "office", "22")
    hass.states.async_set("sensor.alex_area", "Kitchen")
    hass.states.async_set("sensor.sam_area", "Kitchen")
    await setup_zone(
        hass,
        [
            room("kitchen"),
            room("office"),
            person("Alex", "sensor.alex_area"),
            person("Sam", "sensor.sam_area"),
        ],
    )
    await advance(hass, freezer, 1)
    assert sorted(hass.states.get(KITCHEN_WEIGHT).attributes["people"]) == ["Alex", "Sam"]

    freezer.tick(timedelta(seconds=5))
    hass.states.async_set("sensor.sam_area", "Office")
    await hass.async_block_till_done()
    kitchen = hass.states.get(KITCHEN_WEIGHT)
    assert kitchen.attributes["status"] == "person"
    assert kitchen.attributes["people"] == ["Alex"]  # written at once, not on the next minute
    assert hass.states.get(OFFICE_WEIGHT).attributes["people"] == ["Sam"]


@pytest.mark.parametrize(("value", "room_status"), [("Kitchen", "person"), (5, "unoccupied")])
async def test_person_location_by_area_name_attribute(
    hass: HomeAssistant, value: Any, room_status: str
) -> None:
    set_temperature(hass, "kitchen", "20")
    hass.states.async_set("sensor.alex_phone", "home", {"room": value})
    await setup_zone(
        hass,
        [room("kitchen"), person("Alex", "sensor.alex_phone", **{CONF_SOURCE_ATTRIBUTE: "room"})],
    )
    assert hass.states.get(KITCHEN_WEIGHT).attributes["status"] == room_status


async def test_kelvin_sensor_is_converted(hass: HomeAssistant) -> None:
    set_temperature(hass, "kitchen", "293.15", unit="K")
    await setup_zone(hass, [room("kitchen")])
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0, abs=0.05)


async def test_saved_kelvin_reading_is_converted_on_restore(hass: HomeAssistant) -> None:
    now = dt_util.utcnow().timestamp()
    restore_room_state(
        hass,
        unit="K",
        weight=1.0,
        target=1.0,
        tau=3.0,
        status=Status.PERSON,
        last_known_temperature=293.15,
        last_seen=now,
        last_update=now,
    )
    hass.states.async_set("sensor.kitchen_temperature", STATE_UNAVAILABLE)
    await setup_zone(hass, [room("kitchen")])
    assert float(hass.states.get(TEMPERATURE).state) == pytest.approx(20.0, abs=0.05)
