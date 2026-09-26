"""Tests for the zone flow and its Configure menu."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import area_registry as ar

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
    CONF_PERSON,
    CONF_REMOVE,
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

FlowResult = dict[str, Any]


def room_input(area_id: str | None = None, **fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        CONF_TEMPERATURE_SENSOR: "sensor.room_temperature",
        CONF_OVERRIDES: {},
        **fields,
    }
    if area_id is not None:
        values[CONF_AREA_ID] = area_id
    return values


def room_data(area_id: str, **fields: Any) -> dict[str, Any]:
    return {
        CONF_AREA_ID: area_id,
        CONF_TEMPERATURE_SENSOR: "sensor.room_temperature",
        CONF_OVERRIDES: {},
        **fields,
    }


@pytest.fixture(autouse=True)
def areas(hass: HomeAssistant) -> None:
    registry = ar.async_get(hass)
    for name in ("Kitchen", "Office"):
        registry.async_create(name)


def zone_entry(
    hass: HomeAssistant,
    name: str = "Home",
    rooms: dict[str, dict[str, Any]] | None = None,
    people: dict[str, dict[str, Any]] | None = None,
    **defaults: float,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title=name,
        data={CONF_NAME: name},
        options={
            CONF_DEFAULTS: {**DEFAULTS, **defaults},
            CONF_ROOMS: rooms if rooms is not None else {"kitchen": room_data("kitchen")},
            CONF_PEOPLE: people or {},
        },
    )
    entry.add_to_hass(hass)
    return entry


class Flow:
    """Drives one config or options flow, keeping the latest result."""

    def __init__(self, hass: HomeAssistant, manager: Any, result: FlowResult) -> None:
        self.hass = hass
        self.manager = manager
        self.result = result

    async def submit(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self.result = await self.manager.async_configure(self.result["flow_id"], user_input)
        return self.result

    async def menu(self, option: str) -> FlowResult:
        assert self.result["type"] is FlowResultType.MENU, self.result
        return await self.submit({"next_step_id": option})

    @property
    def step(self) -> str:
        return self.result["step_id"]

    @property
    def menu_options(self) -> list[str]:
        assert self.result["type"] is FlowResultType.MENU, self.result
        return list(self.result["menu_options"])

    async def add_room(self, area_id: str, **fields: Any) -> FlowResult:
        await self.menu(CONF_ROOMS)
        await self.submit({CONF_ROOM: CHOICE_ADD})
        await self.submit(room_input(area_id, **fields))
        return self.result

    async def add_person(
        self, name: str, source: str, value_type: str = "area_name", attribute: str | None = None
    ) -> FlowResult:
        await self.menu(CONF_PEOPLE)
        await self.submit({CONF_PERSON: CHOICE_ADD})
        await self.submit({CONF_NAME: name, CONF_SOURCE_ENTITY: source})
        details: dict[str, Any] = {CONF_VALUE_TYPE: value_type}
        if attribute:
            details[CONF_SOURCE_ATTRIBUTE] = attribute
        await self.submit(details)
        return self.result


async def start_zone(hass: HomeAssistant, name: str = "Home") -> Flow:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    flow = Flow(hass, hass.config_entries.flow, result)
    await flow.submit({CONF_NAME: name})
    return flow


async def start_configure(hass: HomeAssistant, entry: MockConfigEntry) -> Flow:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return Flow(hass, hass.config_entries.options, result)


# ---------------------------------------------------------------------------
# Creating a zone
# ---------------------------------------------------------------------------


async def test_create_zone_full_journey(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.phone", "Kitchen", {"area_id": "kitchen"})
    flow = await start_zone(hass, "  Home  ")
    assert flow.step == "menu"
    assert flow.menu_options == [CONF_DEFAULTS, CONF_ROOMS, CONF_PEOPLE]

    await flow.menu(CONF_DEFAULTS)
    assert flow.step == CONF_DEFAULTS
    await flow.submit({**DEFAULTS, "w_person": 2.0})
    assert flow.step == "menu"

    await flow.add_room("kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]})
    assert flow.step == CONF_ROOMS, "saving a room returns to the rooms list"
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    assert flow.menu_options == [CONF_DEFAULTS, CONF_ROOMS, CONF_PEOPLE, "finish"]

    await flow.add_person("Alice", "sensor.phone", "area_id", "area_id")
    assert flow.step == CONF_PEOPLE
    await flow.submit({CONF_PERSON: CHOICE_DONE})

    result = await flow.menu("finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {CONF_NAME: "Home"}
    options = result["options"]
    assert options[CONF_DEFAULTS] == {**DEFAULTS, "w_person": 2.0}
    assert options[CONF_ROOMS] == {
        "kitchen": room_data("kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]})
    }
    [person] = options[CONF_PEOPLE].values()
    assert person == {
        CONF_NAME: "Alice",
        CONF_SOURCE_ENTITY: "sensor.phone",
        CONF_SOURCE_ATTRIBUTE: "area_id",
        CONF_VALUE_TYPE: "area_id",
    }
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.version == 1


async def test_finish_only_offered_once_there_is_a_room(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    assert "finish" not in flow.menu_options
    await flow.menu(CONF_ROOMS)
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    assert "finish" not in flow.menu_options
    await flow.add_room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    assert "finish" in flow.menu_options


async def test_zone_name_must_be_unique(hass: HomeAssistant) -> None:
    zone_entry(hass, "Home")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "home "})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "name_exists"}


async def test_zone_name_must_not_be_blank(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "  "})
    assert result["errors"] == {CONF_NAME: "name_blank"}


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({"tau_person_rise": 0}, "tau_not_positive"),
        ({"w_base": 0}, "base_weight_not_positive"),
        ({"stale_limit": 0}, "stale_limit_not_positive"),
    ],
)
async def test_defaults_rejects_invalid_settings(
    hass: HomeAssistant, settings: dict[str, float], error: str
) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit({**DEFAULTS, **settings})
    assert flow.step == CONF_DEFAULTS
    assert flow.result["errors"] == {"base": error}


async def test_defaults_form_rejects_negative_numbers(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    with pytest.raises(InvalidData):
        await flow.submit({**DEFAULTS, "w_person": -1})


async def test_zero_deactivate_and_dropout_taus_allowed(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit({**DEFAULTS, "tau_deactivate": 0, "tau_dropout": 0})
    assert flow.step == "menu"


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


async def test_rooms_list_offers_existing_rooms_add_and_done(hass: HomeAssistant) -> None:
    flow = await start_configure(hass, zone_entry(hass))
    await flow.menu(CONF_ROOMS)
    schema = flow.result["data_schema"].schema
    [key] = schema
    options = schema[key].config["options"]
    assert [option["value"] for option in options] == ["kitchen", CHOICE_ADD, CHOICE_DONE]
    assert options[0]["label"] == "Kitchen"


async def test_add_room_rejects_duplicate_area(hass: HomeAssistant) -> None:
    flow = await start_configure(hass, zone_entry(hass))
    await flow.add_room("kitchen")
    assert flow.step == CONF_ROOM
    assert flow.result["errors"] == {CONF_AREA_ID: "area_already_configured"}


async def test_add_room_rejects_invalid_template(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_ROOMS)
    await flow.submit({CONF_ROOM: CHOICE_ADD})
    with pytest.raises(InvalidData):
        await flow.submit(room_input("kitchen", **{CONF_OPENING_TEMPLATE: "{{ states("}))


async def test_add_room_rejects_invalid_override(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room("kitchen", **{CONF_OVERRIDES: {"w_base": 0}})
    assert flow.step == CONF_ROOM
    assert flow.result["errors"] == {"base": "base_weight_not_positive"}


async def test_blank_optional_room_fields_are_dropped(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room(
        "kitchen",
        **{
            CONF_OCCUPANCY_SENSORS: ["binary_sensor.motion"],
            CONF_OCCUPANCY_TEMPLATE: "",
            CONF_OVERRIDES: {"tau_person_rise": 4},
        },
    )
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    result = await flow.menu("finish")
    assert result["options"][CONF_ROOMS]["kitchen"] == room_data(
        "kitchen",
        **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.motion"], CONF_OVERRIDES: {"tau_person_rise": 4}},
    )


async def test_room_without_occupancy_source_shows_note(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room("kitchen")
    assert flow.step == "room_no_occupancy"
    await flow.submit({})
    assert flow.step == CONF_ROOMS


async def test_room_without_occupancy_source_no_note_when_people_exist(
    hass: HomeAssistant,
) -> None:
    flow = await start_zone(hass)
    await flow.add_person("Alice", "sensor.phone")
    await flow.submit({CONF_PERSON: CHOICE_DONE})
    await flow.add_room("kitchen")
    assert flow.step == CONF_ROOMS


async def test_edit_room_keeps_area_and_prefills(hass: HomeAssistant) -> None:
    entry = zone_entry(
        hass, rooms={"kitchen": room_data("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})}
    )
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_ROOMS)
    await flow.submit({CONF_ROOM: "kitchen"})
    assert flow.step == "room_edit"
    schema_keys = [str(key) for key in flow.result["data_schema"].schema]
    assert CONF_AREA_ID not in schema_keys, "the area can't be changed"
    assert CONF_REMOVE in schema_keys
    assert flow.result["description_placeholders"]["area"] == "Kitchen"

    await flow.submit(
        room_input(**{CONF_TEMPERATURE_SENSOR: "sensor.new", CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    )
    assert flow.step == CONF_ROOMS
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    result = await flow.menu("save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ROOMS]["kitchen"] == room_data(
        "kitchen", **{CONF_TEMPERATURE_SENSOR: "sensor.new", CONF_OCCUPANCY_TEMPLATE: "{{ true }}"}
    )


async def test_remove_room(hass: HomeAssistant) -> None:
    entry = zone_entry(
        hass, rooms={"kitchen": room_data("kitchen"), "office": room_data("office")}
    )
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_ROOMS)
    await flow.submit({CONF_ROOM: "office"})
    await flow.submit({**room_input(), CONF_REMOVE: True})
    assert flow.step == CONF_ROOMS
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    await flow.menu("save")
    assert list(entry.options[CONF_ROOMS]) == ["kitchen"]


async def test_removing_last_room_hides_save(hass: HomeAssistant) -> None:
    flow = await start_configure(hass, zone_entry(hass))
    assert flow.menu_options == [CONF_DEFAULTS, CONF_ROOMS, CONF_PEOPLE, "save"]
    await flow.menu(CONF_ROOMS)
    await flow.submit({CONF_ROOM: "kitchen"})
    await flow.submit({**room_input(), CONF_REMOVE: True})
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    assert "save" not in flow.menu_options


async def test_same_area_allowed_in_another_zone(hass: HomeAssistant) -> None:
    zone_entry(hass, "Upstairs")
    flow = await start_zone(hass, "Downstairs")
    await flow.add_room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    assert flow.step == CONF_ROOMS


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


async def test_person_details_step_shows_current_value(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.phone", "Kitchen")
    flow = await start_zone(hass)
    await flow.menu(CONF_PEOPLE)
    await flow.submit({CONF_PERSON: CHOICE_ADD})
    await flow.submit({CONF_NAME: "  Alice ", CONF_SOURCE_ENTITY: "sensor.phone"})
    assert flow.step == "person_details"
    assert flow.result["description_placeholders"]["state"] == "Kitchen"
    await flow.submit({CONF_VALUE_TYPE: "area_name"})
    assert flow.step == CONF_PEOPLE


async def test_person_name_must_not_be_blank(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_PEOPLE)
    await flow.submit({CONF_PERSON: CHOICE_ADD})
    await flow.submit({CONF_NAME: " ", CONF_SOURCE_ENTITY: "sensor.phone"})
    assert flow.step == CONF_PERSON
    assert flow.result["errors"] == {CONF_NAME: "name_blank"}


async def test_edit_person_keeps_attribute_only_for_same_entity(hass: HomeAssistant) -> None:
    person = {
        CONF_NAME: "Alice",
        CONF_SOURCE_ENTITY: "sensor.phone",
        CONF_SOURCE_ATTRIBUTE: "area_id",
        CONF_VALUE_TYPE: "area_id",
    }
    entry = zone_entry(hass, people={"p1": person})
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_PEOPLE)
    await flow.submit({CONF_PERSON: "p1"})
    assert flow.step == CONF_PERSON

    await flow.submit({CONF_NAME: "Alice", CONF_SOURCE_ENTITY: "sensor.phone"})
    suggested = {
        str(key): key.description["suggested_value"]
        for key in flow.result["data_schema"].schema
        if key.description
    }
    assert suggested == {CONF_SOURCE_ATTRIBUTE: "area_id", CONF_VALUE_TYPE: "area_id"}
    await flow.submit({CONF_VALUE_TYPE: "area_id"})  # attribute cleared
    await flow.submit({CONF_PERSON: CHOICE_DONE})
    await flow.menu("save")
    assert entry.options[CONF_PEOPLE] == {
        "p1": {CONF_NAME: "Alice", CONF_SOURCE_ENTITY: "sensor.phone", CONF_VALUE_TYPE: "area_id"}
    }


async def test_edit_person_new_entity_starts_without_attribute(hass: HomeAssistant) -> None:
    person = {
        CONF_NAME: "Alice",
        CONF_SOURCE_ENTITY: "sensor.phone",
        CONF_SOURCE_ATTRIBUTE: "area_id",
        CONF_VALUE_TYPE: "area_id",
    }
    flow = await start_configure(hass, zone_entry(hass, people={"p1": person}))
    await flow.menu(CONF_PEOPLE)
    await flow.submit({CONF_PERSON: "p1"})
    await flow.submit({CONF_NAME: "Alice", CONF_SOURCE_ENTITY: "sensor.tablet"})
    suggested = {
        str(key): key.description["suggested_value"]
        for key in flow.result["data_schema"].schema
        if key.description
    }
    assert CONF_SOURCE_ATTRIBUTE not in suggested


async def test_remove_person(hass: HomeAssistant) -> None:
    person = {CONF_NAME: "Alice", CONF_SOURCE_ENTITY: "sensor.phone", CONF_VALUE_TYPE: "area_name"}
    entry = zone_entry(hass, people={"p1": person})
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_PEOPLE)
    await flow.submit({CONF_PERSON: "p1"})
    await flow.submit({CONF_NAME: "Alice", CONF_SOURCE_ENTITY: "sensor.phone", CONF_REMOVE: True})
    assert flow.step == CONF_PEOPLE
    await flow.submit({CONF_PERSON: CHOICE_DONE})
    await flow.menu("save")
    assert entry.options[CONF_PEOPLE] == {}


# ---------------------------------------------------------------------------
# Configure
# ---------------------------------------------------------------------------


async def test_configure_defaults_prefilled_and_saved(hass: HomeAssistant) -> None:
    entry = zone_entry(hass, w_person=2.0)
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_DEFAULTS)
    suggested = {
        str(key): key.description["suggested_value"]
        for key in flow.result["data_schema"].schema
        if key.description
    }
    assert suggested["w_person"] == 2.0
    await flow.submit({**DEFAULTS, "w_person": 3.0})
    await flow.menu("save")
    assert entry.options[CONF_DEFAULTS]["w_person"] == 3.0
    assert entry.options[CONF_ROOMS] == {"kitchen": room_data("kitchen")}


async def test_closing_configure_without_saving_changes_nothing(hass: HomeAssistant) -> None:
    entry = zone_entry(hass)
    before = dict(entry.options)
    flow = await start_configure(hass, entry)
    await flow.add_room("office", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    hass.config_entries.options.async_abort(flow.result["flow_id"])
    assert dict(entry.options) == before


async def test_room_stores_opening_entities_and_template(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room(
        "kitchen",
        **{
            CONF_OCCUPANCY_TEMPLATE: "{{ true }}",
            CONF_OPENING_SENSORS: ["binary_sensor.kitchen_window"],
            CONF_OPENING_TEMPLATE: "{{ is_state('input_boolean.heater', 'on') }}",
        },
    )
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    result = await flow.menu("finish")
    assert result["options"][CONF_ROOMS]["kitchen"] == room_data(
        "kitchen",
        **{
            CONF_OCCUPANCY_TEMPLATE: "{{ true }}",
            CONF_OPENING_SENSORS: ["binary_sensor.kitchen_window"],
            CONF_OPENING_TEMPLATE: "{{ is_state('input_boolean.heater', 'on') }}",
        },
    )
