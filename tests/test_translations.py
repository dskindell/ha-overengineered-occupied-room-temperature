"""Every field on every form is labelled and described, with the placeholders it uses."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from homeassistant.data_entry_flow import section
import pytest
import voluptuous as vol

from custom_components.overengineered_occupied_room_temperature import config_flow as flow
from custom_components.overengineered_occupied_room_temperature.const import (
    CONF_PERSON,
    CONF_ROOM,
    SETTINGS,
)

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


SETTING_STEPS = ("defaults", "room", "room_edit")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _placeholders(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set().union(*map(_placeholders, node.values()))
    return set(PLACEHOLDER.findall(node))


@pytest.mark.parametrize("step_id", list(MENU_STEPS))
def test_setting_placeholders_are_only_used_where_the_flow_supplies_them(step_id: str) -> None:
    used = _placeholders(TRANSLATIONS["config"]["step"][step_id]) & flow.SETTING_PLACEHOLDERS.keys()
    assert not used or step_id in SETTING_STEPS


def test_setting_texts_use_known_placeholders() -> None:
    used = _placeholders({step: TRANSLATIONS["config"]["step"][step] for step in SETTING_STEPS})
    assert used - {"area"} <= flow.SETTING_PLACEHOLDERS.keys()


def test_setting_errors_use_known_placeholders() -> None:
    setting_errors = {name for s in SETTINGS for name in (s.too_small, s.too_large)}
    for key, text in TRANSLATIONS["config"]["error"].items():
        used = _placeholders(text)
        assert used <= flow.SETTING_PLACEHOLDERS.keys(), key
        assert not used or key in setting_errors, f"{key} is shown on forms without placeholders"


def test_every_default_is_shown_from_the_settings_table() -> None:
    assert not re.search(r"Default \d", json.dumps(TRANSLATIONS))
    step = TRANSLATIONS["config"]["step"]["defaults"]
    descriptions = {**step["data_description"], **step["sections"]["zone"]["data_description"]}
    for setting in SETTINGS:
        assert f"Default {{{setting.key}_default}}." in descriptions[setting.key]


@pytest.mark.parametrize("limit", ["too_small", "too_large"])
def test_settings_sharing_an_error_share_its_limit(limit: str) -> None:
    """An error's text shows one setting's limit, so every setting using it has that limit."""
    limits: dict[str, set[tuple[float, bool]]] = {}
    for setting in SETTINGS:
        value = (
            (setting.minimum, setting.minimum_allowed)
            if limit == "too_small"
            else (setting.maximum, True)
        )
        limits.setdefault(getattr(setting, limit), set()).add(value)
    assert all(len(values) == 1 for values in limits.values()), limits


def test_minute_settings_show_their_unit() -> None:
    _, sections = _fields(flow.DEFAULTS_SCHEMA)
    units = {
        str(key): value.config.get("unit_of_measurement")
        for schema in (flow.DEFAULTS_SCHEMA, *sections.values())
        for key, value in schema.schema.items()
        if not isinstance(value, section)
    }
    minutes = [s.key for s in SETTINGS if s.key.startswith(("tau_", "stale_"))]
    assert units == {s.key: "min" if s.key in minutes else None for s in SETTINGS}
