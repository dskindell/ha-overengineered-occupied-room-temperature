"""Per-zone runtime: gathers inputs from Home Assistant and runs the engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
import logging
import math
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
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    entity_registry as er,
    issue_registry as ir,
    restore_state,
)
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
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter
import voluptuous as vol

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
    CONF_TEMPERATURE_SENSORS,
    CONF_TEMPERATURE_UNIT,
    CONF_VALUE_TYPE,
    CONF_ZONE_SETTINGS,
    DOMAIN,
    GRACE_PERIOD_SECONDS,
    ROOM_SETTINGS,
    UPDATE_INTERVAL_SECONDS,
    VALUE_TYPE_AREA_ID,
    ZONE_SETTINGS,
    with_defaults,
)
from .engine import (
    Aggregate,
    RoomConfig,
    RoomInputs,
    RoomState,
    room_config,
    step_zone,
)
from .storage import saved_room_state, saved_unit

_LOGGER = logging.getLogger(__name__)


def _is_boolean(value: Any) -> bool:
    """Whether Home Assistant's true/false rule recognises ``value`` (``cv.boolean``,
    which ``result_as_boolean`` applies)."""
    try:
        cv.boolean(value)
    except vol.Invalid:
        return False
    return True


def _render(template: Template) -> Any:
    """Render a template once, as HA's template tracker does: the result, or the
    ``TemplateError`` if it fails (``helpers/event.py`` ``_render_template_if_ready``)."""
    try:
        return template.async_render_to_info(None).result()
    except TemplateError as err:
        return err


_MISSING = (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE)


class _Result(StrEnum):
    """What kind of result a template gave, for logging each change once."""

    OK = "ok"
    ERROR = "error"
    UNRECOGNISED = "unrecognised"


@dataclass(slots=True)
class Room:
    """A configured room and its live state."""

    area_id: str
    name: str
    temperature_sensors: list[str]
    """One for now; stored as a list for several sensors per room later."""
    occupancy_sensors: list[str]
    occupancy_template: Template | None
    opening_sensors: list[str]
    opening_template: Template | None
    config: RoomConfig
    state: RoomState = field(default_factory=RoomState)
    inputs: RoomInputs | None = None
    people: list[str] = field(default_factory=list)
    write_pending: bool = True
    """Whether the room's weight sensor should write its state after this update."""


@dataclass(frozen=True, slots=True)
class Person:
    """A configured person and where to read their location."""

    name: str
    source_entity: str
    source_attribute: str | None
    by_area_id: bool


def room_unique_id(entry: ConfigEntry, area_id: str) -> str:
    """Unique ID of a room's weight sensor; the area is the room's identity."""
    return f"{entry.entry_id}_{area_id}_weight"


def temperature_unique_id(entry: ConfigEntry) -> str:
    """Unique ID of the zone's Temperature sensor."""
    return f"{entry.entry_id}_temperature"


def issue_id(entry: ConfigEntry) -> str:
    """ID of the zone's missing-occupancy Repairs issue."""
    return f"no_occupancy_source_{entry.entry_id}"


def _signature(room: Room) -> tuple[Any, ...]:
    """What a room's weight sensor shows apart from the weight itself."""
    state, inputs = room.state, room.inputs
    return (
        state.status,
        state.target,
        state.tau_name,
        state.stale,
        tuple(room.people),
        None
        if inputs is None
        else (
            inputs.open,
            inputs.person_present,
            inputs.occupied,
            inputs.temperature is not None,
        ),
    )


class ZoneRuntime:
    """Runs one OORT zone: its rooms, people, subscriptions and update loop."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        # The zone's unit, fixed when it was created: used for every conversion
        # and as the Temperature sensor's unit, so a unit-system switch can't
        # double-convert the output. Home Assistant converts it for display.
        self.unit: str = entry.data.get(CONF_TEMPERATURE_UNIT, hass.config.units.temperature_unit)
        self.entry = entry
        areas = ar.async_get(hass)
        options = entry.options
        # Settings added since the zone was saved get their defaults; room
        # defaults and zone-wide settings are stored apart.
        defaults = {
            **with_defaults(options.get(CONF_DEFAULTS, {}), ROOM_SETTINGS),
            **with_defaults(options.get(CONF_ZONE_SETTINGS, {}), ZONE_SETTINGS),
        }
        self.rooms: dict[str, Room] = {}
        for area_id, data in options[CONF_ROOMS].items():
            area = areas.async_get_area(area_id)
            self.rooms[area_id] = Room(
                area_id=area_id,
                name=area.name if area else area_id,
                temperature_sensors=data[CONF_TEMPERATURE_SENSORS],
                occupancy_sensors=list(data.get(CONF_OCCUPANCY_SENSORS, [])),
                occupancy_template=self._template(data.get(CONF_OCCUPANCY_TEMPLATE)),
                opening_sensors=list(data.get(CONF_OPENING_SENSORS, [])),
                opening_template=self._template(data.get(CONF_OPENING_TEMPLATE)),
                config=room_config(defaults, data.get(CONF_OVERRIDES, {})),
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
        self.result = Aggregate(None, 0.0, 0, False)
        # True when this update came from the timer/startup/grace end, or any room's
        # status or inputs changed: the Temperature sensor then refreshes its
        # attributes too.
        self.write_all = True
        self._template_results: dict[Template, Any] = {}
        self._template_labels: dict[Template, list[str]] = {}
        self._template_problems: dict[Template, _Result] = {}
        # Grace period only while Home Assistant itself is starting.
        self._hold = hass.state is not CoreState.running
        self._listeners: list[Callable[[], None]] = []

    def _template(self, value: str | None) -> Template | None:
        return Template(value, self.hass) if value else None

    # -- restore and listeners ------------------------------------------------

    @callback
    def restore_room(self, area_id: str, state: RoomState, unit: str | None = None) -> None:
        """Seed a room with state saved before a restart.

        The downtime is not applied as elapsed time, so ``last_update`` is cleared.
        A saved reading in another unit is converted to this zone's unit.
        """
        if (room := self.rooms.get(area_id)) is None:
            return
        temperature = state.last_known_temperature
        if (
            temperature is not None
            and unit is not None
            and unit != self.unit
            and unit in TemperatureConverter.VALID_UNITS
        ):
            temperature = TemperatureConverter.convert(temperature, unit, self.unit)
        if temperature is not None and not math.isfinite(temperature):
            temperature = None
        room.state = replace(state, last_update=None, last_known_temperature=temperature)

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Call ``update`` after every recompute; returns a function that removes it."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    # -- lifecycle ------------------------------------------------------------

    @callback
    def async_prime(self) -> None:
        """Restore saved room state and compute a first result, before any entity
        is added, so their first states are real values.

        Reads Home Assistant's restore store directly: it's loaded before any
        integration starts, and on a reload it holds the states saved at unload.
        Nothing is written here, since no listener is registered yet.
        """
        registry = er.async_get(self.hass)
        saved = restore_state.async_get(self.hass).last_states
        for area_id in self.rooms:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, room_unique_id(self.entry, area_id)
            )
            stored = saved.get(entity_id) if entity_id else None
            if stored is None or stored.extra_data is None:
                continue
            data = stored.extra_data.as_dict()
            if (state := saved_room_state(data)) is not None:
                self.restore_room(area_id, state, saved_unit(data))
        # The template tracker only starts in async_start, so render each template
        # once now; otherwise the first result would count every template as false.
        for room in self.rooms.values():
            for template in (room.occupancy_template, room.opening_template):
                if template is not None and template not in self._template_results:
                    self._template_results[template] = _render(template)
        self.async_update()

    @callback
    def async_start(self) -> None:
        """Subscribe to every input and compute the first result."""
        entry = self.entry
        watched = {person.source_entity for person in self.people}
        for room in self.rooms.values():
            watched.update(room.temperature_sensors)
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
                log_fn=self._log_tracker_message,
            )
            entry.async_on_unload(info.async_remove)
            info.async_refresh()

        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_on_timer, timedelta(seconds=UPDATE_INTERVAL_SECONDS)
            )
        )

        if self._hold:
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
        repairs_id = issue_id(self.entry)
        rooms = sorted(
            room.name
            for room in self.rooms.values()
            if not room.occupancy_sensors and room.occupancy_template is None
        )
        if rooms and not self.people:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                repairs_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="no_occupancy_source",
                translation_placeholders={"zone": self.entry.title, "rooms": ", ".join(rooms)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, repairs_id)

    # -- triggers -------------------------------------------------------------

    @callback
    def _async_on_state(self, _event: Event[EventStateChangedData]) -> None:
        # Sensors are rewritten only when something they show changes.
        self.async_update(write_everything=False)

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
        self.async_update(write_everything=False)

    def _log_template_result(self, template: Template, result: Any) -> None:
        """Log a template result that can't count as true or false.

        Logged when the *kind* of result changes (fine → error → unrecognised),
        not on every new value, so a template returning a stream of odd values
        warns once. ``unavailable``/``unknown``/blank are expected while a source
        entity is down; they count as fine and aren't logged.
        """
        if isinstance(result, TemplateError):
            kind = _Result.ERROR
        elif (
            result is not None
            and str(result).strip().lower() not in ("", STATE_UNAVAILABLE, STATE_UNKNOWN)
            and not _is_boolean(result)
        ):
            kind = _Result.UNRECOGNISED
        else:
            kind = _Result.OK
        if self._template_problems.get(template, _Result.OK) is kind:
            return
        self._template_problems[template] = kind
        labels = ", ".join(self._template_labels.get(template, []))
        if kind is _Result.ERROR:
            _LOGGER.error(
                "OORT zone %s: %s failed (%s); counting it as false",
                self.entry.title,
                labels,
                result,
            )
        elif kind is _Result.UNRECOGNISED:
            _LOGGER.warning(
                "OORT zone %s: %s returned %r, which isn't true or false; counting it as false",
                self.entry.title,
                labels,
                result,
            )

    @staticmethod
    def _log_tracker_message(level: int, message: str) -> None:
        """Messages from HA's template tracker.

        Its render errors are already reported by ``_log_template_result`` with the
        zone and room, so they go to debug; anything else keeps its level.
        """
        _LOGGER.log(logging.DEBUG if level >= logging.ERROR else level, message)

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
        """A sensor's reading converted to the zone's unit, or None if unusable.

        Non-finite values (``nan``, ``inf``, or a conversion that overflows) are unusable:
        Home Assistant refuses to write them as a sensor state.
        """
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
        converted = TemperatureConverter.convert(value, unit, self.unit)
        return converted if math.isfinite(converted) else None

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
    def async_update(self, *, write_everything: bool = True) -> None:
        """Advance every room to now, recompute the output and notify entities.

        With ``write_everything`` (the minute timer, startup, grace end) every sensor
        writes its state; otherwise (a state or template event) a sensor writes only
        if something it shows has changed.
        """
        now = dt_util.utcnow().timestamp()
        people_by_area: dict[str, list[str]] = {}
        for person in self.people:
            if (area_id := self._person_area(person)) is not None:
                people_by_area.setdefault(area_id, []).append(person.name)

        inputs: dict[str, RoomInputs] = {}
        before: dict[str, tuple[Any, ...]] = {}
        for room in self.rooms.values():
            before[room.area_id] = _signature(room)
            room.people = people_by_area.get(room.area_id, [])
            room.inputs = inputs[room.area_id] = RoomInputs(
                open=self._any_on(room.opening_sensors)
                or self._template_true(room.opening_template),
                person_present=bool(room.people),
                occupied=self._any_on(room.occupancy_sensors)
                or self._template_true(room.occupancy_template),
                temperature=self._temperature(room.temperature_sensors[0]),
            )

        step = step_zone(
            {
                area_id: (room.state, inputs[area_id], room.config)
                for area_id, room in self.rooms.items()
            },
            now,
            hold=self._hold,
        )
        self.result = step.result
        for area_id, room in self.rooms.items():
            room.state = step.rooms[area_id]
            room.write_pending = write_everything or _signature(room) != before[area_id]
        self.write_all = any(room.write_pending for room in self.rooms.values())
        for update in list(self._listeners):
            # One entity failing to write must not stop the others (as HA's
            # DataUpdateCoordinator does).
            try:
                update()
            except Exception:
                _LOGGER.exception("Error updating an entity of zone %s", self.entry.title)
