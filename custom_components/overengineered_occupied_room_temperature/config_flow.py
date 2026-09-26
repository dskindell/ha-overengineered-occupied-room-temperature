"""Config flow for OORT zones.

Creating a zone and configuring it later use the same menu: *Defaults*,
*Rooms*, *People*, then *Finish* (create) or *Save* (configure), offered only
once the zone has a room. Rooms and people are kept in the zone's options
and edited through one list form each.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryBaseFlow,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import area_registry as ar, selector

from .const import (
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
    CONF_STALE_LIMIT,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_UNIT,
    CONF_VALUE_TYPE,
    CONF_W_BASE,
    DEFAULTS,
    DOMAIN,
    MIN_BASE_WEIGHT,
    TAUS,
    TAUS_POSITIVE,
    TAUS_ZERO_ALLOWED,
    VALUE_TYPE_AREA_ID,
    VALUE_TYPE_AREA_NAME,
    WEIGHTS,
)

ZONE_SETTINGS = (*TAUS, *WEIGHTS, CONF_STALE_LIMIT)
ROOM_OVERRIDES = (*TAUS, *WEIGHTS)


def _number() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, step="any", mode=selector.NumberSelectorMode.BOX)
    )


def validate_settings(values: Mapping[str, Any]) -> str | None:
    """Return an error key if any tau, weight or stale limit present in ``values`` is invalid.

    The form's number fields already reject negatives; this adds the "> 0" rules
    and guards data that didn't come through the form.
    """
    if any(values[key] <= 0 for key in TAUS_POSITIVE if key in values):
        return "tau_not_positive"
    if any(values[key] < 0 for key in TAUS_ZERO_ALLOWED if key in values):
        return "tau_negative"
    if any(values[key] < 0 for key in WEIGHTS if key in values):
        return "weight_negative"
    if CONF_W_BASE in values and values[CONF_W_BASE] < MIN_BASE_WEIGHT:
        return "base_weight_too_small"
    if CONF_STALE_LIMIT in values and values[CONF_STALE_LIMIT] <= 0:
        return "stale_limit_not_positive"
    return None


def _without_empty(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop optional fields the user left blank."""
    return {key: value for key, value in values.items() if value not in (None, "", [])}


DEFAULTS_SCHEMA = vol.Schema({vol.Required(key): _number() for key in ZONE_SETTINGS})

NAME_SCHEMA = vol.Schema({vol.Required(CONF_NAME): selector.TextSelector()})


def _room_schema(*, new: bool) -> vol.Schema:
    """Room form. The area is chosen only when the room is added."""
    schema: dict[vol.Marker, Any] = {}
    if new:
        schema[vol.Required(CONF_AREA_ID)] = selector.AreaSelector()
    schema.update(
        {
            vol.Required(CONF_TEMPERATURE_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=selector.EntityWithDeviceFilterSelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                )
            ),
            vol.Optional(CONF_OCCUPANCY_SENSORS): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=selector.EntityWithDeviceFilterSelectorConfig(
                        domain=["binary_sensor", "input_boolean"]
                    ),
                    multiple=True,
                )
            ),
            vol.Optional(CONF_OCCUPANCY_TEMPLATE): selector.TemplateSelector(),
            vol.Optional(CONF_OPENING_SENSORS): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=selector.EntityWithDeviceFilterSelectorConfig(
                        domain=["binary_sensor", "input_boolean"]
                    ),
                    multiple=True,
                )
            ),
            vol.Optional(CONF_OPENING_TEMPLATE): selector.TemplateSelector(),
            vol.Required(CONF_OVERRIDES): section(
                vol.Schema({vol.Optional(key): _number() for key in ROOM_OVERRIDES}),
                {"collapsed": True},
            ),
        }
    )
    if not new:
        schema[vol.Optional(CONF_REMOVE, default=False)] = selector.BooleanSelector()
    return vol.Schema(schema)


def _person_schema(*, new: bool) -> vol.Schema:
    schema: dict[vol.Marker, Any] = {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_SOURCE_ENTITY): selector.EntitySelector(),
    }
    if not new:
        schema[vol.Optional(CONF_REMOVE, default=False)] = selector.BooleanSelector()
    return vol.Schema(schema)


def _person_details_schema(entity_id: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_SOURCE_ATTRIBUTE): selector.AttributeSelector(
                selector.AttributeSelectorConfig(entity_id=entity_id)
            ),
            vol.Required(CONF_VALUE_TYPE, default=VALUE_TYPE_AREA_NAME): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[VALUE_TYPE_AREA_NAME, VALUE_TYPE_AREA_ID],
                    translation_key=CONF_VALUE_TYPE,
                )
            ),
        }
    )


def _choice_schema(key: str, items: dict[str, str], add_label: str) -> vol.Schema:
    """A list form: the existing items, then "add" and "done".

    The translation key covers only "add" and "done"; the items' own labels are
    names from the user's setup (experiment: does the frontend fall back to them?).
    """
    options = [
        selector.SelectOptionDict(value=value, label=label) for value, label in items.items()
    ]
    options.append(selector.SelectOptionDict(value=CHOICE_ADD, label=add_label))
    options.append(selector.SelectOptionDict(value=CHOICE_DONE, label="Done"))
    return vol.Schema(
        {
            vol.Required(key, default=CHOICE_DONE if items else CHOICE_ADD): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key=f"{key}_choice",
                    )
                )
            )
        }
    )


class ZoneMenu(ConfigEntryBaseFlow):
    """The zone menu and its screens, shared by the create and configure flows.

    Subclasses set ``finish_step`` and provide ``async_step_<finish_step>``.
    Nothing is written to the config entry until that step runs.
    """

    finish_step: str

    _defaults: dict[str, Any]
    _rooms: dict[str, dict[str, Any]]
    _people: dict[str, dict[str, Any]]
    _room_id: str | None
    _person_id: str | None
    _pending_person: dict[str, Any]

    def _load(self, options: Mapping[str, Any]) -> None:
        options = deepcopy(dict(options))
        self._defaults = options.get(CONF_DEFAULTS, dict(DEFAULTS))
        self._rooms = options.get(CONF_ROOMS, {})
        self._people = options.get(CONF_PEOPLE, {})

    def _options(self) -> dict[str, Any]:
        return {CONF_DEFAULTS: self._defaults, CONF_ROOMS: self._rooms, CONF_PEOPLE: self._people}

    def _area_name(self, area_id: str) -> str:
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else area_id

    # -- menu -----------------------------------------------------------------

    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The zone menu; *Finish*/*Save* only once there's a room."""
        options = [CONF_DEFAULTS, CONF_ROOMS, CONF_PEOPLE]
        if self._rooms:
            options.append(self.finish_step)
        rooms = sorted(self._area_name(area_id) for area_id in self._rooms)
        people = sorted(person[CONF_NAME] for person in self._people.values())
        return self.async_show_menu(
            step_id="menu",
            menu_options=options,
            description_placeholders={
                "rooms": ", ".join(rooms) or "none yet",
                "people": ", ".join(people) or "none",
            },
        )

    async def async_step_defaults(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The zone's default taus, weights and stale limit."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if (error := validate_settings(user_input)) is None:
                self._defaults = dict(user_input)
                return await self.async_step_menu()
            errors["base"] = error
        return self.async_show_form(
            step_id=CONF_DEFAULTS,
            data_schema=self.add_suggested_values_to_schema(
                DEFAULTS_SCHEMA, user_input or self._defaults
            ),
            errors=errors,
        )

    # -- rooms ----------------------------------------------------------------

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a room to edit, add one, or go back to the menu."""
        if user_input is not None:
            choice = user_input[CONF_ROOM]
            if choice == CHOICE_DONE:
                return await self.async_step_menu()
            if choice == CHOICE_ADD:
                self._room_id = None
                return await self.async_step_room()
            self._room_id = choice
            return await self.async_step_room_edit()
        items = {area_id: self._area_name(area_id) for area_id in self._rooms}
        return self.async_show_form(
            step_id=CONF_ROOMS,
            data_schema=_choice_schema(CONF_ROOM, items, "Add a new room"),
        )

    async def async_step_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a room."""
        return await self._async_room_form("room", user_input, None)

    async def async_step_room_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit or remove a room. Its area is fixed."""
        return await self._async_room_form("room_edit", user_input, self._room_id)

    async def _async_room_form(
        self, step_id: str, user_input: dict[str, Any] | None, area_id: str | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if area_id is not None and user_input.get(CONF_REMOVE):
                del self._rooms[area_id]
                return await self.async_step_rooms()
            fields = {k: v for k, v in user_input.items() if k not in (CONF_OVERRIDES, CONF_REMOVE)}
            data = _without_empty(fields)
            data[CONF_OVERRIDES] = _without_empty(user_input.get(CONF_OVERRIDES, {}))
            if area_id is None:
                if data[CONF_AREA_ID] in self._rooms:
                    errors[CONF_AREA_ID] = "area_already_configured"
            else:
                data[CONF_AREA_ID] = area_id
            if (error := validate_settings(data[CONF_OVERRIDES])) is not None:
                errors["base"] = error
            if not errors:
                self._rooms[data[CONF_AREA_ID]] = data
                if not self._people and not (
                    data.get(CONF_OCCUPANCY_SENSORS) or data.get(CONF_OCCUPANCY_TEMPLATE)
                ):
                    return await self.async_step_room_no_occupancy()
                return await self.async_step_rooms()

        suggested = user_input
        if suggested is None and area_id is not None:
            suggested = self._rooms[area_id]
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _room_schema(new=area_id is None), suggested
            ),
            errors=errors,
            description_placeholders={"area": self._area_name(area_id)} if area_id else None,
        )

    async def async_step_room_no_occupancy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Note (not an error): this room can never count as occupied yet."""
        if user_input is not None:
            return await self.async_step_rooms()
        return self.async_show_form(step_id="room_no_occupancy", data_schema=vol.Schema({}))

    # -- people ---------------------------------------------------------------

    async def async_step_people(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a person to edit, add one, or go back to the menu."""
        if user_input is not None:
            choice = user_input[CONF_PERSON]
            if choice == CHOICE_DONE:
                return await self.async_step_menu()
            self._person_id = None if choice == CHOICE_ADD else choice
            return await self.async_step_person()
        items = {person_id: person[CONF_NAME] for person_id, person in self._people.items()}
        return self.async_show_form(
            step_id=CONF_PEOPLE,
            data_schema=_choice_schema(CONF_PERSON, items, "Add a new person"),
        )

    async def async_step_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or edit a person, step 1: name and location entity."""
        errors: dict[str, str] = {}
        person_id = self._person_id
        if user_input is not None:
            if person_id is not None and user_input.get(CONF_REMOVE):
                del self._people[person_id]
                return await self.async_step_people()
            name = user_input[CONF_NAME].strip()
            if not name:
                errors[CONF_NAME] = "name_blank"
            else:
                self._pending_person = {
                    CONF_NAME: name,
                    CONF_SOURCE_ENTITY: user_input[CONF_SOURCE_ENTITY],
                }
                return await self.async_step_person_details()

        suggested = user_input
        if suggested is None and person_id is not None:
            suggested = self._people[person_id]
        return self.async_show_form(
            step_id=CONF_PERSON,
            data_schema=self.add_suggested_values_to_schema(
                _person_schema(new=person_id is None), suggested
            ),
            errors=errors,
        )

    async def async_step_person_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: optional attribute, and whether the value is an area name or ID."""
        pending = self._pending_person
        entity_id = pending[CONF_SOURCE_ENTITY]
        if user_input is not None:
            person = {**pending, **_without_empty(user_input)}
            self._people[self._person_id or uuid4().hex] = person
            return await self.async_step_people()

        suggested: dict[str, Any] = {}
        if self._person_id is not None:
            previous = self._people[self._person_id]
            suggested[CONF_VALUE_TYPE] = previous[CONF_VALUE_TYPE]
            if previous[CONF_SOURCE_ENTITY] == entity_id and CONF_SOURCE_ATTRIBUTE in previous:
                suggested[CONF_SOURCE_ATTRIBUTE] = previous[CONF_SOURCE_ATTRIBUTE]
        state = self.hass.states.get(entity_id)
        return self.async_show_form(
            step_id="person_details",
            data_schema=self.add_suggested_values_to_schema(
                _person_details_schema(entity_id), suggested
            ),
            description_placeholders={
                "entity": entity_id,
                "state": state.state if state is not None else "not found",
            },
        )


class OortConfigFlow(ZoneMenu, ConfigFlow, domain=DOMAIN):
    """Create a zone: name it, then the zone menu."""

    VERSION = 1
    finish_step = "finish"

    _name: str

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OortOptionsFlow:
        """Configure an existing zone through the same menu."""
        return OortOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name the zone."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            taken = {
                entry.title.strip().casefold()
                for entry in self.hass.config_entries.async_entries(DOMAIN)
            }
            if not name:
                errors[CONF_NAME] = "name_blank"
            elif name.casefold() in taken:
                errors[CONF_NAME] = "name_exists"
            else:
                self._name = name
                self._load({})
                return await self.async_step_menu()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(NAME_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the zone with everything set up in the menu."""
        return self.async_create_entry(
            title=self._name,
            data={
                CONF_NAME: self._name,
                # The zone keeps this unit for life, so its Temperature sensor's unit
                # never changes under Home Assistant's unit handling.
                CONF_TEMPERATURE_UNIT: self.hass.config.units.temperature_unit,
            },
            options=self._options(),
        )


class OortOptionsFlow(ZoneMenu, OptionsFlow):
    """Configure a zone: the same menu, then *Save*."""

    finish_step = "save"

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start from the zone's saved options."""
        self._load(self.config_entry.options)
        return await self.async_step_menu()

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Save the changes; the zone reloads (update listener)."""
        return self.async_create_entry(data=self._options())
