"""Per-zone runtime: gathers inputs from Home Assistant and runs the engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import area_registry as ar, issue_registry as ir
from homeassistant.helpers.event import (
    TrackTemplate,
    TrackTemplateResult,
    async_call_later,
    async_track_state_change_event,
    async_track_template_result,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.template import Template, result_as_boolean
from homeassistant.helpers.template.helpers import forgiving_boolean
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_DEFAULTS,
    CONF_NAME,
    CONF_OCCUPANCY_SENSORS,
    CONF_OCCUPANCY_TEMPLATE,
    CONF_OPENING_SENSORS,
    CONF_OPENING_TEMPLATE,
    CONF_OVERRIDES,
    CONF_PEOPLE,
    CONF_ROOMS,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_STALE_LIMIT,
    CONF_TAU_DEACTIVATE,
    CONF_TAU_DROPOUT,
    CONF_TAU_OCCUPANCY_FALL,
    CONF_TAU_OCCUPANCY_RISE,
    CONF_TAU_PERSON_FALL,
    CONF_TAU_PERSON_RISE,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALUE_TYPE,
    CONF_W_BASE,
    CONF_W_OCCUPIED,
    CONF_W_PERSON,
    DOMAIN,
    GRACE_PERIOD_SECONDS,
    UPDATE_INTERVAL_SECONDS,
    VALUE_TYPE_AREA_ID,
)
from .engine import (
    Aggregate,
    RoomConfig,
    RoomInputs,
    RoomState,
    Taus,
    Weights,
    aggregate,
    fallback_epsilon,
    step_room,
)

_LOGGER = logging.getLogger(__name__)

_MISSING = (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE)


@dataclass(slots=True)
class Room:
    """A configured room and its live state."""

    area_id: str
    name: str
    temperature_sensor: str
    occupancy_sensors: list[str]
    occupancy_template: Template | None
    opening_sensors: list[str]
    opening_template: Template | None
    config: RoomConfig
    state: RoomState = field(default_factory=RoomState)
    inputs: RoomInputs | None = None
    people: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Person:
    """A configured person and where to read their location."""

    name: str
    source_entity: str
    source_attribute: str | None
    by_area_id: bool


def _room_config(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> RoomConfig:
    """A room's effective settings: its overrides merged over the instance defaults."""
    values = {**defaults, **overrides}
    return RoomConfig(
        taus=Taus(
            person_rise=values[CONF_TAU_PERSON_RISE],
            person_fall=values[CONF_TAU_PERSON_FALL],
            occupancy_rise=values[CONF_TAU_OCCUPANCY_RISE],
            occupancy_fall=values[CONF_TAU_OCCUPANCY_FALL],
            deactivate=values[CONF_TAU_DEACTIVATE],
            dropout=values[CONF_TAU_DROPOUT],
        ),
        weights=Weights(
            person=values[CONF_W_PERSON],
            occupied=values[CONF_W_OCCUPIED],
            base=values[CONF_W_BASE],
        ),
        stale_limit=defaults[CONF_STALE_LIMIT],
    )


class InstanceRuntime:
    """Runs one OORT instance: its rooms, people, subscriptions and update loop."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        areas = ar.async_get(hass)
        options = entry.options
        defaults = options[CONF_DEFAULTS]
        self.rooms: dict[str, Room] = {}
        for area_id, data in options[CONF_ROOMS].items():
            area = areas.async_get_area(area_id)
            self.rooms[area_id] = Room(
                area_id=area_id,
                name=area.name if area else area_id,
                temperature_sensor=data[CONF_TEMPERATURE_SENSOR],
                occupancy_sensors=list(data.get(CONF_OCCUPANCY_SENSORS, [])),
                occupancy_template=self._template(data.get(CONF_OCCUPANCY_TEMPLATE)),
                opening_sensors=list(data.get(CONF_OPENING_SENSORS, [])),
                opening_template=self._template(data.get(CONF_OPENING_TEMPLATE)),
                config=_room_config(defaults, data.get(CONF_OVERRIDES, {})),
            )
        self.people: list[Person] = [
            Person(
                name=data[CONF_NAME],
                source_entity=data[CONF_SOURCE_ENTITY],
                source_attribute=data.get(CONF_SOURCE_ATTRIBUTE),
                by_area_id=data[CONF_VALUE_TYPE] == VALUE_TYPE_AREA_ID,
            )
            for data in options[CONF_PEOPLE].values()
        ]
        self.epsilon = (
            fallback_epsilon(room.config.weights.base for room in self.rooms.values())
            if self.rooms
            else 0.0
        )
        self.result = Aggregate(None, 0.0, 0, False)
        self._template_results: dict[Template, Any] = {}
        self._template_labels: dict[Template, list[str]] = {}
        self._hold = False
        self._listeners: list[Callable[[], None]] = []

    def _template(self, value: str | None) -> Template | None:
        return Template(value, self.hass) if value else None

    # -- restore and listeners ------------------------------------------------

    @callback
    def restore_room(self, area_id: str, state: RoomState) -> None:
        """Seed a room with state saved before a restart.

        The downtime is not applied as elapsed time, so ``last_update`` is cleared.
        """
        if (room := self.rooms.get(area_id)) is not None:
            room.state = replace(state, last_update=None)

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Call ``update`` after every recompute; returns a function that removes it."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    # -- lifecycle ------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe to every input and compute the first result."""
        entry = self.entry
        watched = {person.source_entity for person in self.people}
        for room in self.rooms.values():
            watched.add(room.temperature_sensor)
            watched.update(room.occupancy_sensors)
            watched.update(room.opening_sensors)
        if watched:
            entry.async_on_unload(
                async_track_state_change_event(self.hass, sorted(watched), self._async_on_state)
            )

        for room in self.rooms.values():
            for kind, template in (
                ("occupancy", room.occupancy_template),
                ("opening", room.opening_template),
            ):
                if template is not None:
                    self._template_labels.setdefault(template, []).append(
                        f"{room.name} {kind} template"
                    )
        templates = list(self._template_labels)
        if templates:
            info = async_track_template_result(
                self.hass,
                [TrackTemplate(template, None) for template in templates],
                self._async_on_templates,
            )
            entry.async_on_unload(info.async_remove)
            info.async_refresh()

        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_on_timer, timedelta(seconds=UPDATE_INTERVAL_SECONDS)
            )
        )

        # Grace period only while Home Assistant itself is starting.
        if self.hass.state is not CoreState.running:
            self._hold = True
            entry.async_on_unload(async_at_started(self.hass, self._async_start_grace))

        self._async_update_repairs()
        self.async_update()

    @callback
    def _async_start_grace(self, _hass: HomeAssistant) -> None:
        self.entry.async_on_unload(
            async_call_later(self.hass, GRACE_PERIOD_SECONDS, self._async_end_grace)
        )

    @callback
    def _async_end_grace(self, _now: datetime) -> None:
        self._hold = False
        self.async_update()

    @callback
    def _async_update_repairs(self) -> None:
        """Warn when rooms can never be occupied: no people and no occupancy source."""
        issue_id = f"no_occupancy_source_{self.entry.entry_id}"
        rooms = sorted(
            room.name
            for room in self.rooms.values()
            if not room.occupancy_sensors and room.occupancy_template is None
        )
        if rooms and not self.people:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_occupancy_source",
                translation_placeholders={"zone": self.entry.title, "rooms": ", ".join(rooms)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # -- triggers -------------------------------------------------------------

    @callback
    def _async_on_state(self, _event: Event[EventStateChangedData]) -> None:
        self.async_update()

    @callback
    def _async_on_timer(self, _now: datetime) -> None:
        self.async_update()

    @callback
    def _async_on_templates(
        self, _event: Event[EventStateChangedData] | None, updates: list[TrackTemplateResult]
    ) -> None:
        for update in updates:
            self._template_results[update.template] = update.result
            self._log_template_result(update.template, update.result)
        self.async_update()

    def _log_template_result(self, template: Template, result: Any) -> None:
        """Log a template result that can't count as true or false.

        Called only when a template's result changes, so each problem is logged
        once until the result changes again. ``unavailable``/``unknown``/blank are
        expected while a source entity is down and aren't logged.
        """
        labels = ", ".join(self._template_labels.get(template, []))
        if isinstance(result, TemplateError):
            _LOGGER.error(
                "OORT zone %s: %s failed (%s); counting it as false",
                self.entry.title,
                labels,
                result,
            )
        elif (
            result is not None
            and str(result).strip().lower() not in ("", STATE_UNAVAILABLE, STATE_UNKNOWN)
            and forgiving_boolean(result, None) is None
        ):
            _LOGGER.warning(
                "OORT zone %s: %s returned %r, which isn't true or false; counting it as false",
                self.entry.title,
                labels,
                result,
            )

    # -- inputs ---------------------------------------------------------------

    def _template_true(self, template: Template | None) -> bool:
        """Whether a template's latest result is clearly true.

        Only recognised true values count (``true``, ``on``, ``yes``, ``1``, …);
        anything else — ``unavailable``, ``unknown``, other text, an error — is false.
        """
        if template is None:
            return False
        result = self._template_results.get(template)
        if result is None or isinstance(result, TemplateError):
            return False
        return result_as_boolean(result)

    def _any_on(self, entity_ids: list[str]) -> bool:
        return any(
            (state := self.hass.states.get(entity_id)) is not None and state.state == STATE_ON
            for entity_id in entity_ids
        )

    def _temperature(self, entity_id: str) -> float | None:
        """A sensor's reading converted to the system unit, or None if unusable."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _MISSING:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit not in TemperatureConverter.VALID_UNITS:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        return TemperatureConverter.convert(value, unit, self.hass.config.units.temperature_unit)

    def _person_area(self, person: Person) -> str | None:
        """The ID of the room area a person is in, or None."""
        state = self.hass.states.get(person.source_entity)
        if state is None:
            return None
        value = (
            state.attributes.get(person.source_attribute)
            if person.source_attribute
            else state.state
        )
        if value in _MISSING:
            return None
        areas = ar.async_get(self.hass)
        area = (
            areas.async_get_area(str(value))
            if person.by_area_id
            else areas.async_get_area_by_name(str(value))
        )
        return area.id if area is not None and area.id in self.rooms else None

    # -- update ---------------------------------------------------------------

    @callback
    def async_update(self) -> None:
        """Advance every room to now, recompute the output and notify entities."""
        now = dt_util.utcnow().timestamp()
        people_by_area: dict[str, list[str]] = {}
        for person in self.people:
            if (area_id := self._person_area(person)) is not None:
                people_by_area.setdefault(area_id, []).append(person.name)

        current_temperatures: list[float] = []
        for room in self.rooms.values():
            temperature = self._temperature(room.temperature_sensor)
            if temperature is not None:
                current_temperatures.append(temperature)
            room.people = people_by_area.get(room.area_id, [])
            room.inputs = RoomInputs(
                active=not (
                    self._any_on(room.opening_sensors)
                    or self._template_true(room.opening_template)
                ),
                person_present=bool(room.people),
                occupied=self._any_on(room.occupancy_sensors)
                or self._template_true(room.occupancy_template),
                temperature=temperature,
            )
            room.state = step_room(room.state, room.inputs, room.config, now, hold=self._hold)

        self.result = aggregate(
            ((room.state.weight, room.state.usable_temperature) for room in self.rooms.values()),
            current_temperatures,
            self.epsilon,
        )
        for update in list(self._listeners):
            update()
