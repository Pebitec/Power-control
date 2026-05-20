"""Number platform for PV Excess Control."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_APPLIANCE_NAME,
    DOMAIN,
    MANUFACTURER,
    MAX_PRIORITY,
    MIN_PRIORITY,
)
from .coordinator import SolarPowerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control number entities."""
    coordinator: SolarPowerCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[NumberEntity] = []

    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        entities.append(AppliancePriorityNumber(coordinator, subentry_id, appliance_name))

    async_add_entities(entities)


class AppliancePriorityNumber(CoordinatorEntity[SolarPowerCoordinator], NumberEntity):
    """Per-appliance priority number entity."""

    _attr_has_entity_name = True
    _attr_native_min_value = float(MIN_PRIORITY)
    _attr_native_max_value = float(MAX_PRIORITY)
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:priority-high"

    def __init__(
        self,
        coordinator: SolarPowerCoordinator,
        appliance_id: str,
        appliance_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._appliance_name = appliance_name
        self._attr_name = f"{appliance_name} Priority"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_{appliance_id}_priority"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.appliance_priorities.get(self._appliance_id, 500)
        )

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.appliance_priorities[self._appliance_id] = int(value)
        try:
            subentries = getattr(self.coordinator.config_entry, "subentries", {})
            subentry = subentries.get(self._appliance_id)
            if subentry is not None:
                new_data = dict(subentry.data)
                new_data["appliance_priority"] = int(value)
                await self.hass.config_entries.async_update_subentry(
                    self.coordinator.config_entry, subentry, data=new_data
                )
        except Exception:
            _LOGGER.debug("Could not persist priority for %s", self._appliance_id)
        self.async_write_ha_state()
