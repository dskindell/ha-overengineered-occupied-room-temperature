"""Every field on every form has a label and a description."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol

from homeassistant.data_entry_flow import section

from custom_components.overengineered_occupied_room_temperature import config_flow as flow
from custom_components.overengineered_occupied_room_temperature.const import CONF_PERSON, CONF_ROOM

TRANSLATIONS = json.loads(
    (
        Path(__file__).parent.parent
        / "custom_components/overengineered_occupied_room_temperature/translations/en.json"
    ).read_text()
)

MENU_STEPS: dict[str, vol.Schema] = {
    "defaults": flow.DEFAULTS_SCHEMA,
    "rooms": flow._choice_schema(CONF_ROOM, {}, "Add"),
    "room": flow._room_schema(new=True),
    "room_edit": flow._room_schema(new=False),
    "people": flow._choice_schema(CONF_PERSON, {}, "Add"),
    "person": flow._person_schema(new=False),  # the edit form has every field (Remove too)
    "person_details": flow._person_details_schema("sensor.phone"),
}


def _fields(schema: vol.Schema) -> tuple[list[str], dict[str, vol.Schema]]:
    fields, sections = [], {}
    for key, value in schema.schema.items():
        if isinstance(value, section):
            sections[str(key)] = value.schema
        else:
            fields.append(str(key))
    return fields, sections


def _missing(step: dict[str, Any], schema: vol.Schema) -> list[str]:
    fields, sections = _fields(schema)
    missing = [
        f"{kind}:{field}"
        for field in fields
        for kind in ("data", "data_description")
        if field not in step.get(kind, {})
    ]
    for name, sub in sections.items():
        translated = step.get("sections", {}).get(name, {})
        missing += [
            f"sections.{name}.{kind}:{field}"
            for field in _fields(sub)[0]
            for kind in ("data", "data_description")
            if field not in translated.get(kind, {})
        ]
    return missing


@pytest.mark.parametrize("flow_type", ["config", "options"])
@pytest.mark.parametrize("step_id", list(MENU_STEPS))
def test_every_field_is_labelled_and_described(flow_type: str, step_id: str) -> None:
    step = TRANSLATIONS[flow_type]["step"][step_id]
    assert _missing(step, MENU_STEPS[step_id]) == []


def test_zone_name_field_is_labelled_and_described() -> None:
    assert _missing(TRANSLATIONS["config"]["step"]["user"], flow.NAME_SCHEMA) == []


def test_create_and_configure_texts_match() -> None:
    config, options = TRANSLATIONS["config"]["step"], TRANSLATIONS["options"]["step"]
    for step_id in MENU_STEPS:
        assert config[step_id] == options[step_id], step_id
