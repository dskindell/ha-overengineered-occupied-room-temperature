"""Tests for the zone flow and its Configure menu."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import area_registry as ar
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.overengineered_occupied_room_temperature.config_flow import (
    validate_settings,
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
    CONF_PERSON,
    CONF_REMOVE,
    CONF_RESTORE_DEFAULTS,
    CONF_ROOM,
    CONF_ROOMS,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_SENSORS,
    CONF_TEMPERATURE_UNIT,
    CONF_VALUE_TYPE,
    CONF_ZONE_SETTINGS,
    DEFAULTS,
    DOMAIN,
    ROOM_SETTINGS,
    SETTINGS,
    ZONE_SETTINGS,
    Setting,
)
from tests.helpers import stored_settings

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

FlowResult = dict[str, Any]


def settings_input(**changes: Any) -> dict[str, Any]:
    """What the Settings form submits: room defaults, then the Zone section."""
    stored = stored_settings(**changes)
    return {**stored[CONF_DEFAULTS], CONF_ZONE_SETTINGS: stored[CONF_ZONE_SETTINGS]}


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
        CONF_TEMPERATURE_SENSORS: ["sensor.room_temperature"],
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
            **stored_settings(**defaults),
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
    await flow.submit(settings_input(w_person=0.8))
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
    assert result["data"] == {CONF_NAME: "Home", CONF_TEMPERATURE_UNIT: "°C"}
    options = result["options"]
    assert {
        CONF_DEFAULTS: options[CONF_DEFAULTS],
        CONF_ZONE_SETTINGS: options[CONF_ZONE_SETTINGS],
    } == (stored_settings(w_person=0.8))
    assert options[CONF_ROOMS] == {
        "kitchen": room_data(
            "kitchen", **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]}
        )
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


async def test_name_taken_by_another_flow_meanwhile_aborts_at_finish(hass: HomeAssistant) -> None:
    """Two create flows at once can't both make "Home"."""
    flow = await start_zone(hass, "Home")
    await flow.add_room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    zone_entry(hass, "home")  # created by another flow in the meantime
    result = await flow.menu("finish")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "name_exists"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_zone_name_must_not_be_blank(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "  "})
    assert result["errors"] == {CONF_NAME: "name_blank"}


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({"tau_person_rise": 0}, "tau_not_positive"),
        ({"w_base": 0}, "base_weight_too_small"),
        ({"w_base": 0.0005}, "base_weight_too_small"),
        ({"stale_limit": 0}, "stale_limit_not_positive"),
    ],
)
async def test_defaults_rejects_invalid_settings(
    hass: HomeAssistant, settings: dict[str, float], error: str
) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit(settings_input(**settings))
    assert flow.step == CONF_DEFAULTS
    assert flow.result["errors"] == {"base": error}


async def test_defaults_form_shows_defaults_and_limits(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    placeholders = flow.result["description_placeholders"]
    assert placeholders["tau_occupancy_rise_default"] == "10"
    assert placeholders["w_base_minimum"] == "0.001"
    assert placeholders["tau_person_rise_maximum"] == "1440"


async def test_room_forms_show_the_setting_limits(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_ROOMS)
    await flow.submit({CONF_ROOM: CHOICE_ADD})
    assert flow.result["description_placeholders"]["w_base_minimum"] == "0.001"


async def test_defaults_form_rejects_negative_numbers(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    with pytest.raises(InvalidData):
        await flow.submit(settings_input(w_person=-1))


@pytest.mark.parametrize("key", ["tau_person_rise", "tau_dropout", "w_person", "stale_limit"])
async def test_defaults_form_rejects_nan(hass: HomeAssistant, key: str) -> None:
    """The number field turns the text "nan" into NaN."""
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit(settings_input(**{key: "nan"}))
    assert flow.result["errors"] == {"base": "value_not_finite"}


@pytest.mark.parametrize(
    "settings",
    [{"tau_person_rise": 1441}, {"w_person": 1.5}, {"stale_limit": 1441}, {"w_person": "inf"}],
)
async def test_defaults_form_rejects_values_over_the_limits(
    hass: HomeAssistant, settings: dict[str, Any]
) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    with pytest.raises(InvalidData):
        await flow.submit(settings_input(**settings))


async def test_defaults_form_accepts_values_at_the_limits(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit(settings_input(tau_occupancy_fall=1440, w_person=1, stale_limit=1440))
    assert flow.step == "menu"


async def test_room_override_rejects_nan(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room("kitchen", **{CONF_OVERRIDES: {"tau_person_fall": "nan"}})
    assert flow.step == CONF_ROOM
    assert flow.result["errors"] == {"base": "value_not_finite"}


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({"w_person": math.inf}, "value_not_finite"),
        ({"tau_person_rise": math.nan}, "value_not_finite"),
        ({"tau_open": 1440.5}, "tau_too_large"),
        ({"w_base": 1.01}, "weight_too_large"),
        ({"stale_limit": 1440.5}, "stale_limit_too_large"),
        ({"delay_person_exit": 60.5}, "delay_too_large"),
        ({"delay_occupancy_enter": -0.5}, "delay_negative"),
        ({"tau_open": 1440, "w_base": 1, "stale_limit": 1440}, None),
        ({"delay_person_enter": 0, "delay_occupancy_exit": 60}, None),
    ],
)
def test_settings_limits_also_checked_outside_the_form(
    values: dict[str, float], error: str | None
) -> None:
    assert validate_settings(values) == error


async def test_zone_wide_settings_have_their_own_section(hass: HomeAssistant) -> None:
    """The stale limit is zone-wide, so it isn't listed with the room defaults."""
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    schema = {str(key): value for key, value in flow.result["data_schema"].schema.items()}
    assert set(schema) == {*ROOM_SETTINGS, CONF_ZONE_SETTINGS}
    assert {str(key) for key in schema[CONF_ZONE_SETTINGS].schema.schema} == set(ZONE_SETTINGS)
    await flow.submit(settings_input(stale_limit=12))
    await flow.add_room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    result = await flow.menu("finish")
    assert result["options"][CONF_ZONE_SETTINGS] == {"stale_limit": 12}
    assert "stale_limit" not in result["options"][CONF_DEFAULTS]


async def test_zero_open_and_dropout_taus_allowed(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit(settings_input(tau_open=0, tau_dropout=0))
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
    assert flow.result["errors"] == {"base": "base_weight_too_small"}


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
        **{
            CONF_OCCUPANCY_SENSORS: ["binary_sensor.motion"],
            CONF_OVERRIDES: {"tau_person_rise": 4},
        },
    )


async def test_room_without_occupancy_source_shows_note(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room("kitchen")
    assert flow.step == "room_no_occupancy"
    await flow.submit({})
    assert flow.step == CONF_ROOMS


async def test_whitespace_only_template_is_not_an_occupancy_source(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "   "})
    assert flow.step == "room_no_occupancy"


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
    suggested = {
        str(key): key.description["suggested_value"]
        for key in flow.result["data_schema"].schema
        if key.description
    }
    assert suggested[CONF_TEMPERATURE_SENSOR] == "sensor.room_temperature"  # from the list

    await flow.submit(
        room_input(**{CONF_TEMPERATURE_SENSOR: "sensor.new", CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})
    )
    assert flow.step == CONF_ROOMS
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    result = await flow.menu("save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ROOMS]["kitchen"] == room_data(
        "kitchen",
        **{CONF_TEMPERATURE_SENSORS: ["sensor.new"], CONF_OCCUPANCY_TEMPLATE: "{{ true }}"},
    )


async def test_remove_room(hass: HomeAssistant) -> None:
    entry = zone_entry(hass, rooms={"kitchen": room_data("kitchen"), "office": room_data("office")})
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


async def test_person_details_state_cannot_break_its_formatting(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.phone", "Amy`s room")
    flow = await start_zone(hass)
    await flow.menu(CONF_PEOPLE)
    await flow.submit({CONF_PERSON: CHOICE_ADD})
    await flow.submit({CONF_NAME: "Amy", CONF_SOURCE_ENTITY: "sensor.phone"})
    assert flow.result["description_placeholders"]["state"] == "Amy's room"


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
    entry = zone_entry(hass, w_person=0.8)
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_DEFAULTS)
    suggested = {
        str(key): key.description["suggested_value"]
        for key in flow.result["data_schema"].schema
        if key.description
    }
    assert suggested["w_person"] == 0.8
    await flow.submit(settings_input(w_person=0.9))
    await flow.menu("save")
    assert entry.options[CONF_DEFAULTS]["w_person"] == 0.9
    assert entry.options[CONF_ROOMS] == {"kitchen": room_data("kitchen")}


def _suggested(result: FlowResult) -> dict[str, Any]:
    """The Settings form's prefilled values, the Zone section's flattened in."""
    values: dict[str, Any] = {}
    for key, value in result["data_schema"].schema.items():
        if str(key) == CONF_ZONE_SETTINGS:
            values |= {str(k): k.description["suggested_value"] for k in value.schema.schema}
        elif key.description:
            values[str(key)] = key.description["suggested_value"]
    return values


async def test_settings_matching_the_defaults_offer_no_restore(hass: HomeAssistant) -> None:
    flow = await start_configure(hass, zone_entry(hass))
    await flow.menu(CONF_DEFAULTS)
    placeholders = flow.result["description_placeholders"]
    assert {placeholders[f"{key}_differs"] for key in DEFAULTS} == {""}
    assert CONF_RESTORE_DEFAULTS not in flow.result["data_schema"].schema


async def test_settings_note_values_that_differ_from_the_defaults(hass: HomeAssistant) -> None:
    flow = await start_configure(hass, zone_entry(hass, tau_person_fall=3, stale_limit=12))
    await flow.menu(CONF_DEFAULTS)
    placeholders = flow.result["description_placeholders"]
    differing = {key for key in DEFAULTS if placeholders[f"{key}_differs"]}
    assert differing == {"tau_person_fall", "stale_limit"}
    assert placeholders["tau_person_fall_differs"] == " — yours differs"
    assert placeholders["tau_person_fall_default"] == "2"
    assert CONF_RESTORE_DEFAULTS in flow.result["data_schema"].schema


async def test_settings_note_the_values_of_a_rejected_form(hass: HomeAssistant) -> None:
    flow = await start_configure(hass, zone_entry(hass))
    await flow.menu(CONF_DEFAULTS)
    await flow.submit(settings_input(w_base=0))
    assert flow.result["errors"] == {"base": "base_weight_too_small"}
    assert flow.result["description_placeholders"]["w_base_differs"]
    assert _suggested(flow.result)["w_base"] == 0


async def test_restore_defaults_refills_the_form_for_review(hass: HomeAssistant) -> None:
    kitchen = room_data("kitchen", **{CONF_OVERRIDES: {"tau_person_fall": 5}})
    entry = zone_entry(hass, rooms={"kitchen": kitchen}, tau_open=1, w_occupied=0.3, stale_limit=12)
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit({**settings_input(w_base=0), CONF_RESTORE_DEFAULTS: True})
    assert flow.step == CONF_DEFAULTS
    assert flow.result["errors"] == {}
    assert _suggested(flow.result) == DEFAULTS
    assert CONF_RESTORE_DEFAULTS not in flow.result["data_schema"].schema
    await flow.submit(settings_input(tau_open=4))
    await flow.menu("save")
    assert entry.options == {
        **stored_settings(tau_open=4),
        CONF_ROOMS: {"kitchen": kitchen},
        CONF_PEOPLE: {},
    }


async def test_configure_fills_in_settings_missing_from_an_older_zone(hass: HomeAssistant) -> None:
    """A setting added since the zone was saved shows its default; unknown keys go."""
    entry = zone_entry(hass)
    stored = {
        key: value
        for key, value in DEFAULTS.items()
        if key in ROOM_SETTINGS and key != "tau_dropout"
    }
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_DEFAULTS: {**stored, "retired_setting": 7}}
    )
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_DEFAULTS)
    suggested = {
        str(key): key.description["suggested_value"]
        for key in flow.result["data_schema"].schema
        if key.description
    }
    assert suggested["tau_dropout"] == DEFAULTS["tau_dropout"]
    await flow.submit(settings_input())
    await flow.menu("save")
    assert entry.options[CONF_DEFAULTS] == stored_settings()[CONF_DEFAULTS]
    assert entry.options[CONF_ZONE_SETTINGS] == stored_settings()[CONF_ZONE_SETTINGS]


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda setting: setting.key)
def test_every_setting_is_checked_against_its_limits(setting: Setting) -> None:
    assert validate_settings({setting.key: setting.default}) is None
    assert validate_settings({setting.key: setting.maximum}) is None
    assert validate_settings({setting.key: setting.maximum * 1.01}) == setting.too_large
    at_minimum = validate_settings({setting.key: setting.minimum})
    assert at_minimum == (None if setting.minimum_allowed else setting.too_small)
    assert validate_settings({setting.key: setting.minimum - 0.0001}) == setting.too_small


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


async def test_smallest_unoccupied_weight_is_accepted(hass: HomeAssistant) -> None:
    """0.001 is the minimum (and the default)."""
    flow = await start_zone(hass)
    await flow.menu(CONF_DEFAULTS)
    await flow.submit(settings_input(w_base=0.001))
    assert flow.step == "menu"


async def test_too_small_unoccupied_weight_override_rejected(hass: HomeAssistant) -> None:
    flow = await start_zone(hass)
    await flow.add_room("kitchen", **{CONF_OVERRIDES: {"w_base": 0.0001}})
    assert flow.step == CONF_ROOM
    assert flow.result["errors"] == {"base": "base_weight_too_small"}


# ---------------------------------------------------------------------------
# Editing edge cases
# ---------------------------------------------------------------------------


async def test_clearing_overrides_on_edit_removes_them(hass: HomeAssistant) -> None:
    entry = zone_entry(
        hass,
        rooms={
            "kitchen": room_data(
                "kitchen",
                **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}", CONF_OVERRIDES: {"tau_person_rise": 10}},
            )
        },
    )
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_ROOMS)
    await flow.submit({CONF_ROOM: "kitchen"})
    await flow.submit(room_input(**{CONF_OCCUPANCY_TEMPLATE: "{{ true }}", CONF_OVERRIDES: {}}))
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    await flow.menu("save")
    assert entry.options[CONF_ROOMS]["kitchen"][CONF_OVERRIDES] == {}


async def test_room_whose_area_was_deleted_can_still_be_edited(hass: HomeAssistant) -> None:
    entry = zone_entry(
        hass, rooms={"kitchen": room_data("kitchen", **{CONF_OCCUPANCY_TEMPLATE: "{{ true }}"})}
    )
    areas = ar.async_get(hass)
    areas.async_delete(areas.async_get_area("kitchen").id)
    flow = await start_configure(hass, entry)
    await flow.menu(CONF_ROOMS)
    labels = {
        option["value"]: option["label"]
        for option in flow.result["data_schema"].schema[CONF_ROOM].config["options"]
    }
    assert labels["kitchen"] == "kitchen"  # the area ID stands in for the missing name
    await flow.submit({CONF_ROOM: "kitchen"})
    assert flow.result["description_placeholders"]["area"] == "kitchen"
    await flow.submit(room_input(**{CONF_OCCUPANCY_TEMPLATE: "{{ false }}"}))
    await flow.submit({CONF_ROOM: CHOICE_DONE})
    result = await flow.menu("save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ROOMS]["kitchen"][CONF_OCCUPANCY_TEMPLATE] == "{{ false }}"
