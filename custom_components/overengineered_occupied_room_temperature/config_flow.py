"""Config flow for OORT: instances, plus room and person subentries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import area_registry as ar, selector

from .const import (
    CONF_ACTIVE_TEMPLATE,
    CONF_AREA_ID,
    CONF_DEFAULTS,
    CONF_NAME,
    CONF_OCCUPANCY_SENSORS,
    CONF_OCCUPANCY_TEMPLATE,
    CONF_OVERRIDES,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_STALE_LIMIT,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALUE_TYPE,
    CONF_W_BASE,
    DEFAULTS,
    DOMAIN,
    SUBENTRY_PERSON,
    SUBENTRY_ROOM,
    TAUS,
    TAUS_POSITIVE,
    TAUS_ZERO_ALLOWED,
    VALUE_TYPE_AREA_ID,
    VALUE_TYPE_AREA_NAME,
    WEIGHTS,
)

INSTANCE_SETTINGS = (*TAUS, *WEIGHTS, CONF_STALE_LIMIT)
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
    if CONF_W_BASE in values and values[CONF_W_BASE] <= 0:
        return "base_weight_not_positive"
    if CONF_STALE_LIMIT in values and values[CONF_STALE_LIMIT] <= 0:
        return "stale_limit_not_positive"
    return None


def _without_empty(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop optional fields the user left blank."""
    return {key: value for key, value in values.items() if value not in (None, "", [])}


# ---------------------------------------------------------------------------
# Instance (config entry)
# ---------------------------------------------------------------------------

INSTANCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_DEFAULTS): section(
            vol.Schema(
                {vol.Required(key, default=DEFAULTS[key]): _number() for key in INSTANCE_SETTINGS}
            ),
            {"collapsed": True},
        ),
    }
)


def _instance_data(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the form's defaults section into the stored entry data."""
    return {CONF_NAME: user_input[CONF_NAME].strip(), **user_input[CONF_DEFAULTS]}


def _instance_form_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """Inverse of ``_instance_data``, for pre-filling the form."""
    return {
        CONF_NAME: data[CONF_NAME],
        CONF_DEFAULTS: {key: data[key] for key in INSTANCE_SETTINGS},
    }


class OortConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create and reconfigure OORT instances."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Rooms and people are added to an instance as subentries."""
        return {SUBENTRY_ROOM: RoomSubentryFlow, SUBENTRY_PERSON: PersonSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create an instance."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _instance_data(user_input)
            if (error := validate_settings(data)) is None:
                return self.async_create_entry(title=data[CONF_NAME], data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(INSTANCE_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change an instance's name or defaults."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _instance_data(user_input)
            if (error := validate_settings(data)) is None:
                return self.async_update_reload_and_abort(entry, title=data[CONF_NAME], data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                INSTANCE_SCHEMA, user_input or _instance_form_values(entry.data)
            ),
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Room subentry
# ---------------------------------------------------------------------------


def _room_schema(*, include_area: bool) -> vol.Schema:
    """Room form. The area can only be chosen when the room is added."""
    schema: dict[vol.Marker, Any] = {}
    if include_area:
        schema[vol.Required(CONF_AREA_ID)] = selector.AreaSelector()
    schema.update(
        {
            vol.Required(CONF_TEMPERATURE_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=selector.EntityFilterSelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                )
            ),
            vol.Optional(CONF_OCCUPANCY_SENSORS): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=selector.EntityFilterSelectorConfig(
                        domain=["binary_sensor", "input_boolean"]
                    ),
                    multiple=True,
                )
            ),
            vol.Optional(CONF_OCCUPANCY_TEMPLATE): selector.TemplateSelector(),
            vol.Optional(CONF_ACTIVE_TEMPLATE): selector.TemplateSelector(),
            vol.Required(CONF_OVERRIDES): section(
                vol.Schema({vol.Optional(key): _number() for key in ROOM_OVERRIDES}),
                {"collapsed": True},
            ),
        }
    )
    return vol.Schema(schema)


def _has_occupancy_source(data: Mapping[str, Any]) -> bool:
    return bool(data.get(CONF_OCCUPANCY_SENSORS) or data.get(CONF_OCCUPANCY_TEMPLATE))


class RoomSubentryFlow(ConfigSubentryFlow):
    """Add or edit a room."""

    _pending: dict[str, Any]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a room."""
        return await self._async_step_room(user_input, reconfiguring=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a room."""
        return await self._async_step_room(user_input, reconfiguring=True)

    async def _async_step_room(
        self, user_input: dict[str, Any] | None, *, reconfiguring: bool
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            data = _without_empty({k: v for k, v in user_input.items() if k != CONF_OVERRIDES})
            data[CONF_OVERRIDES] = _without_empty(user_input.get(CONF_OVERRIDES, {}))
            if reconfiguring:
                data[CONF_AREA_ID] = self._get_reconfigure_subentry().data[CONF_AREA_ID]
            elif any(
                subentry.subentry_type == SUBENTRY_ROOM and subentry.unique_id == data[CONF_AREA_ID]
                for subentry in entry.subentries.values()
            ):
                errors[CONF_AREA_ID] = "area_already_configured"

            if (error := validate_settings(data[CONF_OVERRIDES])) is not None:
                errors["base"] = error

            if not errors:
                self._pending = data
                if _has_occupancy_source(data) or any(
                    subentry.subentry_type == SUBENTRY_PERSON
                    for subentry in entry.subentries.values()
                ):
                    return self._async_save_room()
                return await self.async_step_no_occupancy_source()

        suggested = user_input
        if suggested is None and reconfiguring:
            suggested = dict(self._get_reconfigure_subentry().data)
        return self.async_show_form(
            step_id="reconfigure" if reconfiguring else "user",
            data_schema=self.add_suggested_values_to_schema(
                _room_schema(include_area=not reconfiguring), suggested
            ),
            errors=errors,
        )

    async def async_step_no_occupancy_source(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Note (not an error) that this room can never count as occupied."""
        if user_input is not None:
            return self._async_save_room()
        return self.async_show_form(step_id="no_occupancy_source", data_schema=vol.Schema({}))

    @callback
    def _async_save_room(self) -> SubentryFlowResult:
        data = self._pending
        area = ar.async_get(self.hass).async_get_area(data[CONF_AREA_ID])
        title = area.name if area else data[CONF_AREA_ID]
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), title=title, data=data
            )
        return self.async_create_entry(title=title, data=data, unique_id=data[CONF_AREA_ID])


# ---------------------------------------------------------------------------
# Person subentry
# ---------------------------------------------------------------------------

PERSON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(CONF_SOURCE_ENTITY): selector.EntitySelector(),
        vol.Required(CONF_VALUE_TYPE, default=VALUE_TYPE_AREA_NAME): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[VALUE_TYPE_AREA_NAME, VALUE_TYPE_AREA_ID],
                translation_key=CONF_VALUE_TYPE,
            )
        ),
    }
)


class PersonSubentryFlow(ConfigSubentryFlow):
    """Add or edit a person: the location source, then an optional attribute of it."""

    _pending: dict[str, Any]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a person."""
        return await self._async_step_person(user_input, suggested=None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a person."""
        return await self._async_step_person(
            user_input, suggested=dict(self._get_reconfigure_subentry().data)
        )

    async def _async_step_person(
        self, user_input: dict[str, Any] | None, *, suggested: dict[str, Any] | None
    ) -> SubentryFlowResult:
        if user_input is not None:
            self._pending = {**user_input, CONF_NAME: user_input[CONF_NAME].strip()}
            return await self.async_step_attribute()
        return self.async_show_form(
            step_id=self.source,
            data_schema=self.add_suggested_values_to_schema(PERSON_SCHEMA, suggested),
        )

    async def async_step_attribute(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Optionally read an attribute of the source entity instead of its state."""
        if user_input is not None:
            data = {**self._pending, **_without_empty(user_input)}
            if self.source == "reconfigure":
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    title=data[CONF_NAME],
                    data=data,
                )
            return self.async_create_entry(title=data[CONF_NAME], data=data)

        suggested = None
        if self.source == "reconfigure":
            previous = self._get_reconfigure_subentry().data
            if previous[CONF_SOURCE_ENTITY] == self._pending[CONF_SOURCE_ENTITY]:
                suggested = {CONF_SOURCE_ATTRIBUTE: previous.get(CONF_SOURCE_ATTRIBUTE)}
        return self.async_show_form(
            step_id="attribute",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Optional(CONF_SOURCE_ATTRIBUTE): selector.AttributeSelector(
                            selector.AttributeSelectorConfig(
                                entity_id=self._pending[CONF_SOURCE_ENTITY]
                            )
                        )
                    }
                ),
                suggested,
            ),
        )
