"""Tests for the instance config flow and the room and person subentry flows."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import area_registry as ar

from custom_components.overengineered_occupied_room_temperature.const import (
    CONF_ACTIVE_TEMPLATE,
    CONF_AREA_ID,
    CONF_DEFAULTS,
    CONF_NAME,
    CONF_OCCUPANCY_SENSORS,
    CONF_OCCUPANCY_TEMPLATE,
    CONF_OVERRIDES,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALUE_TYPE,
    DEFAULTS,
    DOMAIN,
    SUBENTRY_PERSON,
    SUBENTRY_ROOM,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def instance_input(name: str = "Home", **overrides: float) -> dict[str, Any]:
    return {CONF_NAME: name, CONF_DEFAULTS: {**DEFAULTS, **overrides}}


def room_input(area_id: str, **fields: Any) -> dict[str, Any]:
    return {
        CONF_AREA_ID: area_id,
        CONF_TEMPERATURE_SENSOR: f"sensor.{area_id}_temperature",
        CONF_OVERRIDES: {},
        **fields,
    }


@pytest.fixture
def kitchen(hass: HomeAssistant) -> ar.AreaEntry:
    return ar.async_get(hass).async_create("Kitchen")


async def add_instance(
    hass: HomeAssistant, subentries: list[ConfigSubentryData] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={CONF_NAME: "Home", **DEFAULTS},
        subentries_data=subentries or [],
    )
    entry.add_to_hass(hass)
    return entry


async def start_subentry(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> dict:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": SOURCE_USER}
    )


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


async def test_create_instance(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], instance_input("  Home  ", w_person=2.0)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {CONF_NAME: "Home", **DEFAULTS, "w_person": 2.0}


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"tau_person_rise": 0}, "tau_not_positive"),
        ({"w_base": 0}, "base_weight_not_positive"),
        ({"stale_limit": 0}, "stale_limit_not_positive"),
    ],
)
async def test_create_instance_rejects_invalid_settings(
    hass: HomeAssistant, overrides: dict[str, float], error: str
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], instance_input(**overrides)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize("overrides", [{"tau_deactivate": -1}, {"w_occupied": -0.1}])
async def test_form_rejects_negative_numbers(
    hass: HomeAssistant, overrides: dict[str, float]
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], instance_input(**overrides)
        )


async def test_zero_deactivate_and_dropout_taus_allowed(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], instance_input(tau_deactivate=0, tau_dropout=0)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure_instance(hass: HomeAssistant) -> None:
    entry = await add_instance(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], instance_input("Upstairs", stale_limit=30)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Upstairs"
    assert entry.data["stale_limit"] == 30


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


async def test_add_room(hass: HomeAssistant, kitchen: ar.AreaEntry) -> None:
    entry = await add_instance(hass)
    result = await start_subentry(hass, entry, SUBENTRY_ROOM)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        room_input(
            kitchen.id,
            **{
                CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"],
                CONF_ACTIVE_TEMPLATE: "{{ is_state('binary_sensor.kitchen_window', 'off') }}",
                CONF_OVERRIDES: {"tau_person_fall": 2},
            },
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.subentry_type == SUBENTRY_ROOM
    assert subentry.unique_id == kitchen.id
    assert subentry.title == "Kitchen"
    assert subentry.data[CONF_OVERRIDES] == {"tau_person_fall": 2}


async def test_add_room_rejects_duplicate_area(hass: HomeAssistant, kitchen: ar.AreaEntry) -> None:
    entry = await add_instance(
        hass,
        [
            ConfigSubentryData(
                data=room_input(kitchen.id), subentry_type=SUBENTRY_ROOM,
                title="Kitchen", unique_id=kitchen.id,
            )
        ],
    )
    result = await start_subentry(hass, entry, SUBENTRY_ROOM)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        room_input(kitchen.id, **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_AREA_ID: "area_already_configured"}


async def test_same_area_allowed_in_another_instance(
    hass: HomeAssistant, kitchen: ar.AreaEntry
) -> None:
    await add_instance(
        hass,
        [
            ConfigSubentryData(
                data=room_input(kitchen.id), subentry_type=SUBENTRY_ROOM,
                title="Kitchen", unique_id=kitchen.id,
            )
        ],
    )
    other = await add_instance(hass)
    result = await start_subentry(hass, other, SUBENTRY_ROOM)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        room_input(kitchen.id, **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"]}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_add_room_rejects_invalid_template(
    hass: HomeAssistant, kitchen: ar.AreaEntry
) -> None:
    entry = await add_instance(hass)
    result = await start_subentry(hass, entry, SUBENTRY_ROOM)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            room_input(kitchen.id, **{CONF_OCCUPANCY_TEMPLATE: "{{ is_state( }}"}),
        )


async def test_add_room_rejects_invalid_override(
    hass: HomeAssistant, kitchen: ar.AreaEntry
) -> None:
    entry = await add_instance(hass)
    result = await start_subentry(hass, entry, SUBENTRY_ROOM)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        room_input(
            kitchen.id,
            **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.kitchen_motion"], CONF_OVERRIDES: {"w_base": 0}},
        ),
    )
    assert result["errors"] == {"base": "base_weight_not_positive"}


async def test_room_without_occupancy_source_shows_note(
    hass: HomeAssistant, kitchen: ar.AreaEntry
) -> None:
    entry = await add_instance(hass)
    result = await start_subentry(hass, entry, SUBENTRY_ROOM)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], room_input(kitchen.id)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "no_occupancy_source"

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_room_without_occupancy_source_skips_note_when_people_exist(
    hass: HomeAssistant, kitchen: ar.AreaEntry
) -> None:
    entry = await add_instance(
        hass,
        [
            ConfigSubentryData(
                data={CONF_NAME: "Alex", CONF_SOURCE_ENTITY: "sensor.alex_area",
                      CONF_VALUE_TYPE: "area_name"},
                subentry_type=SUBENTRY_PERSON, title="Alex", unique_id=None,
            )
        ],
    )
    result = await start_subentry(hass, entry, SUBENTRY_ROOM)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], room_input(kitchen.id)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure_room_keeps_area(hass: HomeAssistant, kitchen: ar.AreaEntry) -> None:
    entry = await add_instance(
        hass,
        [
            ConfigSubentryData(
                data=room_input(kitchen.id, **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.a"]}),
                subentry_type=SUBENTRY_ROOM, title="Kitchen", unique_id=kitchen.id,
            )
        ],
    )
    (subentry_id,) = entry.subentries
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert CONF_AREA_ID not in result["data_schema"].schema

    new_input = room_input(kitchen.id, **{CONF_OCCUPANCY_SENSORS: ["binary_sensor.b"]})
    del new_input[CONF_AREA_ID]
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], new_input)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    subentry = entry.subentries[subentry_id]
    assert subentry.data[CONF_AREA_ID] == kitchen.id
    assert subentry.data[CONF_OCCUPANCY_SENSORS] == ["binary_sensor.b"]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute_input", "expected_attribute"),
    [({CONF_SOURCE_ATTRIBUTE: "area_id"}, "area_id"), ({}, None)],
)
async def test_add_person(
    hass: HomeAssistant, attribute_input: dict[str, str], expected_attribute: str | None
) -> None:
    entry = await add_instance(hass)
    result = await start_subentry(hass, entry, SUBENTRY_PERSON)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: " Alex ", CONF_SOURCE_ENTITY: "sensor.alex_phone", CONF_VALUE_TYPE: "area_id"},
    )
    assert result["step_id"] == "attribute"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], attribute_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Alex"
    assert subentry.data.get(CONF_SOURCE_ATTRIBUTE) == expected_attribute
    assert subentry.data[CONF_VALUE_TYPE] == "area_id"


async def test_reconfigure_person(hass: HomeAssistant) -> None:
    entry = await add_instance(
        hass,
        [
            ConfigSubentryData(
                data={CONF_NAME: "Alex", CONF_SOURCE_ENTITY: "sensor.alex_phone",
                      CONF_SOURCE_ATTRIBUTE: "area_id", CONF_VALUE_TYPE: "area_id"},
                subentry_type=SUBENTRY_PERSON, title="Alex", unique_id=None,
            )
        ],
    )
    (subentry_id,) = entry.subentries
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Alex", CONF_SOURCE_ENTITY: "sensor.alex_area", CONF_VALUE_TYPE: "area_name"},
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    data = entry.subentries[subentry_id].data
    assert data[CONF_SOURCE_ENTITY] == "sensor.alex_area"
    assert CONF_SOURCE_ATTRIBUTE not in data
