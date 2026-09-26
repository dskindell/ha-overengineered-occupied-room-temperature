"""Sensors: a weight per room and the instance's weighted temperature."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import DOMAIN, TEMPERATURE_DECIMALS, WEIGHT_DECIMALS
from .engine import RoomState, Status, TauName
from .instance import InstanceRuntime, Room


def room_unique_id(entry: ConfigEntry, area_id: str) -> str:
    return f"{entry.entry_id}_{area_id}_weight"


def temperature_unique_id(entry: ConfigEntry) -> str:
    return f"{entry.entry_id}_temperature"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the instance's sensors and remove those of deleted rooms."""
    runtime: InstanceRuntime = entry.runtime_data
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

    def __init__(self, runtime: InstanceRuntime, device: DeviceInfo) -> None:
        self._runtime = runtime
        self._attr_device_info = device

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.async_add_listener(self._async_on_runtime_update))

    @callback
    def _async_on_runtime_update(self) -> None:
        self.async_write_ha_state()


@dataclass(frozen=True, slots=True)
class RoomExtraData(ExtraStoredData):
    """A room's engine state, saved across restarts."""

    state: RoomState

    def as_dict(self) -> dict[str, Any]:
        return asdict(self.state)

    @staticmethod
    def state_from_dict(data: dict[str, Any]) -> RoomState | None:
        try:
            values = dict(data)
            values["status"] = Status(values["status"])
            if values.get("last_occupied_state") is not None:
                values["last_occupied_state"] = Status(values["last_occupied_state"])
            if values.get("tau_name") is not None:
                values["tau_name"] = TauName(values["tau_name"])
            return RoomState(**values)
        except (KeyError, TypeError, ValueError):
            return None


class RoomWeightSensor(_OortSensor, RestoreEntity):
    """A room's current weight, with the inputs behind it as attributes."""

    _attr_suggested_display_precision = 3

    def __init__(self, runtime: InstanceRuntime, room: Room, device: DeviceInfo) -> None:
        super().__init__(runtime, device)
        self._room = room
        self._attr_name = f"{room.name} weight"
        self._attr_unique_id = room_unique_id(runtime.entry, room.area_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (extra := await self.async_get_last_extra_data()) is not None:
            if (state := RoomExtraData.state_from_dict(extra.as_dict())) is not None:
                self._runtime.restore_room(self._room.area_id, state)

    @property
    def extra_restore_state_data(self) -> RoomExtraData:
        return RoomExtraData(self._room.state)

    @callback
    def _async_on_runtime_update(self) -> None:
        # Written on the minute timer and when the room's status or inputs change,
        # not on every temperature reading.
        if self._room.write_pending:
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        # Rounded so a settled weight stops producing new recorder rows.
        return round(self._room.state.weight, WEIGHT_DECIMALS)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state, inputs = self._room.state, self._room.inputs
        return {
            "area_id": self._room.area_id,
            "status": state.status.value,
            "active": inputs.active if inputs else None,
            "temperature_available": inputs.temperature is not None if inputs else None,
            "temperature_stale": state.stale,
            "person_present": inputs.person_present if inputs else None,
            "occupied": inputs.occupied if inputs else None,
            "people": self._room.people,
            "target_weight": state.target,
            "tau": state.tau,
            "tau_name": state.tau_name,
            "last_occupied_state": (
                state.last_occupied_state.value if state.last_occupied_state else None
            ),
        }


class WeightedTemperatureSensor(_OortSensor):
    """The instance's occupancy-weighted temperature."""

    _attr_name = "Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_suggested_display_precision = 1
    # Changes on almost every update; kept on the entity but out of history.
    _unrecorded_attributes = frozenset({"total_weight"})

    def __init__(self, runtime: InstanceRuntime, device: DeviceInfo) -> None:
        super().__init__(runtime, device)
        self._attr_unique_id = temperature_unique_id(runtime.entry)
        self._attr_native_unit_of_measurement = runtime.hass.config.units.temperature_unit

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
            "total_weight": result.total_weight,
            "contributing_rooms": result.contributing_rooms,
            "fallback": result.fallback,
        }
