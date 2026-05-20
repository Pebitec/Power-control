"""Sensor entities for PV Excess Control."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APPLIANCE_NAME, DOMAIN, MANUFACTURER
from .coordinator import SolarPowerCoordinator
from .status_formatter import FormattedStatus, format_status

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PV Excess Control sensor entities from a config entry."""
    coordinator: SolarPowerCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[SensorEntity] = [
        PvExcessPowerSensor(coordinator),
    ]

    subentries = getattr(config_entry, "subentries", {})
    for subentry_id, subentry in subentries.items():
        appliance_name = subentry.data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}")
        entities.extend([
            PvAppliancePowerSensor(coordinator, subentry_id, appliance_name),
            PvApplianceRuntimeSensor(coordinator, subentry_id, appliance_name),
            PvApplianceEnergySensor(coordinator, subentry_id, appliance_name),
            PvApplianceActivationsSensor(coordinator, subentry_id, appliance_name),
            PvApplianceStatusSensor(coordinator, subentry_id, appliance_name),
        ])

    async_add_entities(entities)


class SolarPowerBaseSensor(CoordinatorEntity[SolarPowerCoordinator], SensorEntity):
    """Base class for PV Excess Control sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SolarPowerCoordinator,
        unique_id_suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{unique_id_suffix}"
        self._attr_name = name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name="PV Excess Control",
            manufacturer=MANUFACTURER,
        )

    @property
    def _data(self) -> dict[str, Any] | None:
        return self.coordinator.data


class PvExcessPowerSensor(SolarPowerBaseSensor):
    """Sensor reporting current PV excess power in Watts."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator: SolarPowerCoordinator) -> None:
        super().__init__(coordinator, "excess_power", "Excess Power")

    @property
    def native_value(self) -> float | None:
        data = self._data
        if data is None:
            return None
        power_state = data.get("power_state")
        if power_state is None:
            return None
        return power_state.excess_power


class PvApplianceBaseSensor(SolarPowerBaseSensor):
    """Base class for per-appliance sensors."""

    def __init__(
        self,
        coordinator: SolarPowerCoordinator,
        appliance_id: str,
        appliance_name: str,
        suffix: str,
        sensor_label: str,
    ) -> None:
        unique_id_suffix = f"appliance_{appliance_id}_{suffix}"
        name = f"{appliance_name} {sensor_label}"
        super().__init__(coordinator, unique_id_suffix, name)
        self._appliance_id = appliance_id

    def _appliance_state(self):
        data = self._data
        if data is None:
            return None
        appliance_states = data.get("appliance_states", {})
        return appliance_states.get(self._appliance_id)


class PvAppliancePowerSensor(PvApplianceBaseSensor):
    """Sensor reporting current power draw of an appliance."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator, appliance_id, appliance_name):
        super().__init__(coordinator, appliance_id, appliance_name, "power", "Power")

    @property
    def native_value(self) -> float | None:
        state = self._appliance_state()
        if state is None:
            return None
        return state.current_power


class PvApplianceRuntimeSensor(PvApplianceBaseSensor):
    """Sensor reporting today's runtime of an appliance."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(self, coordinator, appliance_id, appliance_name):
        super().__init__(coordinator, appliance_id, appliance_name, "runtime_today", "Runtime Today")

    @property
    def native_value(self) -> float | None:
        state = self._appliance_state()
        if state is None:
            return None
        runtime: timedelta = state.runtime_today
        return round(runtime.total_seconds() / 3600, 4)


class PvApplianceEnergySensor(PvApplianceBaseSensor):
    """Sensor reporting today's energy consumption of an appliance."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, appliance_id, appliance_name):
        super().__init__(coordinator, appliance_id, appliance_name, "energy_today", "Energy Today")

    @property
    def last_reset(self):
        from homeassistant.util import dt as dt_util
        return dt_util.start_of_local_day()

    @property
    def native_value(self) -> float | None:
        state = self._appliance_state()
        if state is None:
            return None
        return state.energy_today


class PvApplianceActivationsSensor(PvApplianceBaseSensor):
    """Sensor reporting today's activation count of an appliance."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, appliance_id, appliance_name):
        super().__init__(coordinator, appliance_id, appliance_name, "activations_today", "Activations Today")

    @property
    def native_value(self) -> int | None:
        state = self._appliance_state()
        if state is None:
            return None
        return state.activations_today


class PvApplianceStatusSensor(PvApplianceBaseSensor):
    """Sensor reporting the composed status for an appliance."""

    def __init__(self, coordinator, appliance_id, appliance_name):
        super().__init__(coordinator, appliance_id, appliance_name, "status", "Status")
        self._compose_cache_key: int = 0
        self._compose_cache_value: FormattedStatus | None = None

    def _compose(self) -> FormattedStatus | None:
        data = self._data
        cache_key = id(data) if data is not None else 0
        if self._compose_cache_key == cache_key:
            return self._compose_cache_value

        result = self._compose_inner(data)
        self._compose_cache_key = cache_key
        self._compose_cache_value = result
        return result

    def _compose_inner(self, data: dict[str, Any] | None) -> FormattedStatus | None:
        if data is None:
            return None

        grace_remaining = data.get("grace_period_remaining")
        if grace_remaining is not None and grace_remaining > 0:
            return FormattedStatus(
                text=(
                    f"Startup grace period - {math.ceil(grace_remaining)}s "
                    f"remaining before decisions begin"
                ),
                action="idle",
                overrides_plan=False,
                cooldown_seconds_remaining=None,
                switch_deferred=False,
                headroom_watts=None,
                plan_action=None,
                plan_window_start=None,
                plan_window_end=None,
            )

        decisions = data.get("control_decisions", [])
        decision = next(
            (d for d in decisions if d.appliance_id == self._appliance_id),
            None,
        )
        if decision is None:
            return None

        appliance_states = data.get("appliance_states", {})
        state = appliance_states.get(self._appliance_id)
        appliance_configs = data.get("appliance_configs", {})
        config = appliance_configs.get(self._appliance_id)
        if state is None or config is None:
            text = decision.reason
            if len(text) > 255:
                text = text[: 252] + "..."
            return FormattedStatus(
                text=text,
                action=decision.action.value,
                overrides_plan=decision.overrides_plan,
                cooldown_seconds_remaining=None,
                switch_deferred=False,
                headroom_watts=None,
                plan_action=None,
                plan_window_start=None,
                plan_window_end=None,
            )

        return format_status(
            decision,
            state,
            config,
            switch_interval=config.switch_interval,
            now=datetime.now(),
        )

    @property
    def native_value(self) -> str | None:
        fs = self._compose()
        return fs.text if fs else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fs = self._compose()
        if fs is None:
            return None
        return {
            "action": fs.action,
            "overrides_plan": fs.overrides_plan,
            "cooldown_seconds_remaining": fs.cooldown_seconds_remaining,
            "switch_deferred": fs.switch_deferred,
        }
