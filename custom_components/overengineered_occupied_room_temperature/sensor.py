"""Sensors: a weight per room and the zone's weighted temperature."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import OortConfigEntry
from .const import DOMAIN, TEMPERATURE_DECIMALS, WEIGHT_DECIMALS
from .storage import RoomExtraData
from .zone import Room, ZoneRuntime, room_unique_id, temperature_unique_id


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the zone's sensors and remove those of deleted rooms."""
    runtime = entry.runtime_data
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"OORT {entry.title}",
        entry_type=DeviceEntryType.SERVICE,
    )

    expected = {temperature_unique_id(entry)} | {
        room_unique_id(entry, area_id) for area_id in runtime.rooms
    }
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.unique_id not in expected:
            registry.async_remove(registered.entity_id)

    async_add_entities(
        [
            *(RoomWeightSensor(runtime, room, device) for room in runtime.rooms.values()),
            WeightedTemperatureSensor(runtime, device),
        ]
    )


class _OortSensor(SensorEntity):
    """Common wiring: no polling, refreshed whenever the runtime recomputes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime: ZoneRuntime, device: DeviceInfo) -> None:
        self._runtime = runtime
        self._attr_device_info = device

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.async_add_listener(self._async_on_runtime_update))

    @callback
    def _async_on_runtime_update(self) -> None:
        self.async_write_ha_state()


class RoomWeightSensor(_OortSensor, RestoreEntity):
    """A room's current weight, with the inputs behind it as attributes."""

    _attr_suggested_display_precision = WEIGHT_DECIMALS

    def __init__(self, runtime: ZoneRuntime, room: Room, device: DeviceInfo) -> None:
        super().__init__(runtime, device)
        self._room = room
        self._attr_translation_key = "room_weight"
        self._attr_translation_placeholders = {"room": room.name}
        self._attr_unique_id = room_unique_id(runtime.entry, room.area_id)

    @property
    def extra_restore_state_data(self) -> RoomExtraData:
        return RoomExtraData(self._room.state, self._runtime.unit)

    @callback
    def _async_on_runtime_update(self) -> None:
        if self._room.write_pending:
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._room.state.weight, WEIGHT_DECIMALS)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._room.attributes


class WeightedTemperatureSensor(_OortSensor):
    """The zone's occupancy-weighted temperature."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_suggested_display_precision = TEMPERATURE_DECIMALS
    # Changes on almost every update; kept on the entity but out of history.
    _unrecorded_attributes = frozenset({"total_weight"})

    def __init__(self, runtime: ZoneRuntime, device: DeviceInfo) -> None:
        super().__init__(runtime, device)
        self._attr_unique_id = temperature_unique_id(runtime.entry)
        self._attr_native_unit_of_measurement = runtime.unit
        self._total_weight = self._current_total_weight()
        self._written: tuple[Any, ...] | None = None

    def _current_total_weight(self) -> float:
        return round(self._runtime.result.total_weight, WEIGHT_DECIMALS)

    def _visible(self) -> tuple[Any, ...]:
        result = self._runtime.result
        return (self.native_value, result.contributing_rooms, result.fallback)

    @callback
    def _async_on_runtime_update(self) -> None:
        # total_weight changes with almost every reading while weights move, and any
        # changed attribute makes a new recorder row.
        visible = self._visible()
        if self._runtime.any_room_written or visible != self._written:
            self._total_weight = self._current_total_weight()
            self._written = visible
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._runtime.result.temperature is not None

    @property
    def native_value(self) -> float | None:
        temperature = self._runtime.result.temperature
        return None if temperature is None else round(temperature, TEMPERATURE_DECIMALS)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._runtime.result
        return {
            "total_weight": self._total_weight,
            "contributing_rooms": result.contributing_rooms,
            "fallback": result.fallback,
        }
