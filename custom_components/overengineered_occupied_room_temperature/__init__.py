"""Overengineered Occupied-Room Temperature (OORT)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .zone import ZoneRuntime, missing_entities_issue_id, repairs_issue_id

PLATFORMS: list[Platform] = [Platform.SENSOR]

type OortConfigEntry = ConfigEntry[ZoneRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: OortConfigEntry) -> bool:
    """Set up an OORT zone."""
    entry.runtime_data = ZoneRuntime(hass, entry)
    # Restore rooms and compute a first result before the entities are added, so
    # they never write a placeholder state.
    entry.runtime_data.async_prime()
    # Platform setup waits for its entities to be added (with the primed state);
    # only then do the listeners start, so every recompute can write.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.runtime_data.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OortConfigEntry) -> bool:
    """Unload an OORT zone; its Repairs issues go with it (set again on load)."""
    _delete_issues(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: OortConfigEntry) -> None:
    """Clear the zone's Repairs issues when it is deleted."""
    _delete_issues(hass, entry)


def _delete_issues(hass: HomeAssistant, entry: OortConfigEntry) -> None:
    for issue_id in (repairs_issue_id(entry), missing_entities_issue_id(entry)):
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_update_listener(hass: HomeAssistant, entry: OortConfigEntry) -> None:
    """Reload the zone after its options are saved."""
    hass.config_entries.async_schedule_reload(entry.entry_id)
