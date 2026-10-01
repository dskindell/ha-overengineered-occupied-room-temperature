"""Per-zone runtime: gathers inputs from Home Assistant and runs the engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
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
    async_track_point_in_utc_time,
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
    CONF_NAME,
    CONF_OCCUPANCY_SENSORS,
    CONF_OCCUPANCY_TEMPLATE,
    CONF_OPENING_SENSORS,
    CONF_OPENING_TEMPLATE,
    CONF_PEOPLE,
    CONF_ROOMS,
    CONF_SOURCE_ATTRIBUTE,
    CONF_SOURCE_ENTITY,
    CONF_TEMPERATURE_SENSORS,
    CONF_TEMPERATURE_UNIT,
    CONF_VALUE_TYPE,
    DOMAIN,
    GRACE_PERIOD_SECONDS,
    UPDATE_INTERVAL_SECONDS,
    VALUE_TYPE_AREA_ID,
    WEIGHT_DECIMALS,
    WEIGHT_WRITE_STEP,
    has_occupancy_source,
    room_configs,
)
from .engine import (
    Aggregate,
    RoomConfig,
    RoomInputs,
    RoomState,
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


def _is_missing(value: Any) -> bool:
    """No value: absent, blank, ``unknown`` or ``unavailable``."""
    return value is None or str(value).strip().lower() in ("", STATE_UNKNOWN, STATE_UNAVAILABLE)


class _ResultKind(StrEnum):
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
    """Only the first is read."""
    occupancy_sensors: list[str]
    occupancy_template: Template | None
    opening_sensors: list[str]
    opening_template: Template | None
    has_occupancy_source: bool
    config: RoomConfig
    state: RoomState = field(default_factory=RoomState)
    people: list[str] = field(default_factory=list)
    """The people in the room now."""
    shown_people: list[str] = field(default_factory=list)
    """The people the weight sensor shows: those in the room while a person counts as
    present, or the last ones seen while their exit is delayed."""
    attributes: dict[str, Any] = field(default_factory=dict)
    """What the room's weight sensor shows besides the weight, as of the last update."""
    write_pending: bool = True
    """Whether the room's weight sensor should write its state after this update."""
    written_weight: float = math.inf
    """The weight, as shown, of the sensor's last write (infinite before the first)."""

    def weight_write_due(self) -> bool:
        """Whether the shown weight has moved far enough, or settled, to be written."""
        weight = round(self.state.weight, WEIGHT_DECIMALS)
        if weight == self.written_weight:
            return False
        moved = round(abs(weight - self.written_weight), WEIGHT_DECIMALS)
        return moved >= WEIGHT_WRITE_STEP or weight == round(self.state.target, WEIGHT_DECIMALS)


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


def repairs_issue_id(entry: ConfigEntry) -> str:
    """ID of the zone's missing-occupancy Repairs issue."""
    return f"no_occupancy_source_{entry.entry_id}"


def room_attributes(room: Room) -> dict[str, Any]:
    """The attributes of a room's weight sensor."""
    state = room.state
    inputs = state.inputs
    return {
        "area_id": room.area_id,
        "status": state.status.value,
        "open": inputs.open if inputs else None,
        "temperature_available": inputs.temperature is not None if inputs else None,
        "temperature_stale": state.stale,
        "person_present": state.person_present,
        "occupied": state.occupied,
        "people": room.shown_people,
        "target_weight": state.target,
        "tau": state.tau,
        "tau_name": state.tau_name.value if state.tau_name else None,
        "last_occupied_state": (
            state.last_occupied_state.value if state.last_occupied_state else None
        ),
    }


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
        configs = room_configs(options)
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
                has_occupancy_source=has_occupancy_source(data),
                config=configs[area_id],
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
        self._template_results: dict[Template, Any] = {}
        self._template_labels: dict[Template, list[str]] = {}
        self._template_result_kinds: dict[Template, _ResultKind] = {}
        # The attributes read from each watched entity, besides its state.
        self._read_attributes: dict[str, set[str]] = {}
        # Grace period only while Home Assistant itself is starting.
        self._grace = hass.state is not CoreState.running
        self._started = False
        self._cancel_deadline: Callable[[], None] | None = None
        self._deadline: float | None = None
        self._listeners: list[Callable[[], None]] = []

    def _template(self, value: str | None) -> Template | None:
        return Template(value, self.hass) if value else None

    # -- restore and listeners ------------------------------------------------

    def _in_zone_unit(self, value: float, unit: str | None) -> float | None:
        """``value`` in the zone's unit (None: already in it); None if the unit isn't a
        temperature unit or the result isn't finite (Home Assistant won't write it)."""
        if unit is not None and unit != self.unit:
            if unit not in TemperatureConverter.VALID_UNITS:
                return None
            value = TemperatureConverter.convert(value, unit, self.unit)
        return value if math.isfinite(value) else None

    @callback
    def _restore_room(self, room: Room, state: RoomState, unit: str | None) -> None:
        """Seed a room with saved state; the downtime isn't applied as elapsed time."""
        temperature = state.last_known_temperature
        if temperature is not None:
            temperature = self._in_zone_unit(temperature, unit)
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
        A disabled sensor's saved state is never refreshed, so its room starts
        afresh. Nothing is written here, since no listener is registered yet.
        """
        registry = er.async_get(self.hass)
        saved = restore_state.async_get(self.hass).last_states
        for area_id, room in self.rooms.items():
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, room_unique_id(self.entry, area_id)
            )
            if entity_id is None or registry.entities[entity_id].disabled:
                continue
            stored = saved.get(entity_id)
            if stored is None or stored.extra_data is None:
                continue
            data = stored.extra_data.as_dict()
            if (state := saved_room_state(data)) is not None:
                self._restore_room(room, state, saved_unit(data))
                # While an exit is delayed the people have gone, so show who was there.
                people = stored.state.attributes.get("people")
                if state.person_present and isinstance(people, list):
                    room.shown_people = [name for name in people if isinstance(name, str)]
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
            for entity_id in room.temperature_sensors:
                self._read_attributes.setdefault(entity_id, set()).add(ATTR_UNIT_OF_MEASUREMENT)
        for person in self.people:
            if person.source_attribute:
                self._read_attributes.setdefault(person.source_entity, set()).add(
                    person.source_attribute
                )
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

        if self._grace:
            entry.async_on_unload(async_at_started(self.hass, self._async_start_grace))

        self._started = True
        entry.async_on_unload(self._async_cancel_deadline)
        self._async_update_repairs()
        self.async_update()

    @callback
    def _async_cancel_deadline(self) -> None:
        if self._cancel_deadline is not None:
            self._cancel_deadline()
            self._cancel_deadline = None
        self._deadline = None

    @callback
    def _async_schedule_deadline(self, deadline: float | None) -> None:
        """Update when a delayed presence change will count, so it counts on time."""
        if deadline == self._deadline or not self._started:
            return
        self._async_cancel_deadline()
        if deadline is not None:
            self._deadline = deadline
            # A millisecond late, so the update is past the deadline despite rounding.
            self._cancel_deadline = async_track_point_in_utc_time(
                self.hass,
                self._async_on_deadline,
                datetime.fromtimestamp(deadline + 0.001, UTC),
            )

    @callback
    def _async_on_deadline(self, _now: datetime) -> None:
        self._cancel_deadline = None
        self._deadline = None
        self.async_update(refresh=False)

    @callback
    def _async_start_grace(self, _hass: HomeAssistant) -> None:
        self.entry.async_on_unload(
            async_call_later(self.hass, GRACE_PERIOD_SECONDS, self._async_end_grace)
        )

    @callback
    def _async_end_grace(self, _now: datetime) -> None:
        self._grace = False
        self.async_update()

    @callback
    def _async_update_repairs(self) -> None:
        """Warn when rooms can never be occupied: no people and no occupancy source."""
        repairs_id = repairs_issue_id(self.entry)
        rooms = sorted(room.name for room in self.rooms.values() if not room.has_occupancy_source)
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
    def _async_on_state(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if (
            old is not None
            and new is not None
            and old.state == new.state
            and all(
                old.attributes.get(name) == new.attributes.get(name)
                for name in self._read_attributes.get(event.data["entity_id"], ())
            )
        ):
            return  # only attributes OORT doesn't read changed
        self.async_update(refresh=False)

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
        self.async_update(refresh=False)

    def _log_template_result(self, template: Template, result: Any) -> None:
        """Log a template result that can't count as true or false.

        Logged when the *kind* of result changes (ok → error → unrecognised), not on
        every new value, so a template returning a stream of odd values warns once.
        A missing result is expected while a source entity is down; it counts as ok.
        """
        if isinstance(result, TemplateError):
            kind = _ResultKind.ERROR
        elif not _is_missing(result) and not _is_boolean(result):
            kind = _ResultKind.UNRECOGNISED
        else:
            kind = _ResultKind.OK
        if self._template_result_kinds.get(template, _ResultKind.OK) is kind:
            return
        self._template_result_kinds[template] = kind
        labels = ", ".join(self._template_labels.get(template, []))
        if kind is _ResultKind.ERROR:
            _LOGGER.error(
                "OORT zone %s: %s failed (%s); counting it as false",
                self.entry.title,
                labels,
                result,
            )
        elif kind is _ResultKind.UNRECOGNISED:
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
        if state is None or _is_missing(state.state):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit not in TemperatureConverter.VALID_UNITS:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        return self._in_zone_unit(value, unit)

    def _person_area(self, person: Person, areas: ar.AreaRegistry) -> str | None:
        """The ID of the room area a person is in, or None."""
        state = self.hass.states.get(person.source_entity)
        if state is None:
            return None
        value = (
            state.attributes.get(person.source_attribute)
            if person.source_attribute
            else state.state
        )
        if _is_missing(value):
            return None
        area = (
            areas.async_get_area(str(value))
            if person.by_area_id
            else areas.async_get_area_by_name(str(value))
        )
        return area.id if area is not None and area.id in self.rooms else None

    def _people_by_area(self) -> dict[str, list[str]]:
        areas = ar.async_get(self.hass)
        people: dict[str, list[str]] = {}
        for person in self.people:
            if (area_id := self._person_area(person, areas)) is not None:
                people.setdefault(area_id, []).append(person.name)
        return people

    def _room_inputs(self, room: Room) -> RoomInputs:
        return RoomInputs(
            open=self._any_on(room.opening_sensors) or self._template_true(room.opening_template),
            person_present=bool(room.people),
            occupied=self._any_on(room.occupancy_sensors)
            or self._template_true(room.occupancy_template),
            temperature=self._temperature(room.temperature_sensors[0]),
        )

    # -- update ---------------------------------------------------------------

    @callback
    def async_update(self, *, refresh: bool = True) -> None:
        """Advance every room to now, recompute the output and notify entities.

        A weight sensor writes when anything it shows besides the weight changes; with
        ``refresh`` (the minute timer, startup, grace end) also when its weight is due
        (``Room.weight_write_due``).
        """
        people_by_area = self._people_by_area()
        zone: dict[str, tuple[RoomState, RoomInputs, RoomConfig]] = {}
        for area_id, room in self.rooms.items():
            room.people = people_by_area.get(area_id, [])
            zone[area_id] = (room.state, self._room_inputs(room), room.config)

        step = step_zone(zone, dt_util.utcnow().timestamp(), grace=self._grace)
        self.result = step.result
        for area_id, room in self.rooms.items():
            room.state = step.rooms[area_id]
            if not room.state.person_present:
                room.shown_people = []
            elif room.people:
                room.shown_people = room.people
            attributes = room_attributes(room)
            room.write_pending = attributes != room.attributes or (
                refresh and room.weight_write_due()
            )
            room.attributes = attributes
            if room.write_pending:
                room.written_weight = round(room.state.weight, WEIGHT_DECIMALS)
        self._async_schedule_deadline(step.next_deadline)
        for update in list(self._listeners):
            # One entity failing to write must not stop the others (as HA's
            # DataUpdateCoordinator does).
            try:
                update()
            except Exception:
                _LOGGER.exception("Error updating an entity of zone %s", self.entry.title)
