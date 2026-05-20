"""Controller for Solar Power Control.

Bridges Home Assistant state with the optimizer:
- Collects sensor states and builds PowerState
- Collects appliance states from HA entities
- Applies ControlDecisions by calling HA services
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .const import (
    CONF_GRID_EXPORT,
    CONF_IMPORT_EXPORT,
    CONF_LOAD_POWER,
    CONF_PV_POWER,
)
from .models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    ControlDecision,
    PowerState,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_OFF_STATES = {"off", "false", "False", "0"}
_UNAVAILABLE_STATES = {"unavailable", "unknown", "none", ""}

_POWER_UNIT_MULTIPLIERS: dict[str, float] = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
}


def _normalise_power(value: float, unit: str | None) -> float:
    if unit is None:
        return value
    return value * _POWER_UNIT_MULTIPLIERS.get(unit.lower().strip(), 1.0)


class Controller:
    """Bridges Home Assistant state with the optimizer."""

    def __init__(self, hass: HomeAssistant, config_data: dict) -> None:
        self.hass = hass
        self.config_data = config_data
        self._last_state_change: dict[str, datetime] = {}

    def _read_sensor(self, entity_id: str | None, default: float = 0.0, *, power: bool = False) -> float:
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            return default
        try:
            val = float(state.state)
        except (ValueError, TypeError):
            return default
        if power:
            val = _normalise_power(val, state.attributes.get("unit_of_measurement"))
        return val

    def collect_power_state(self) -> PowerState:
        """Read sensor entities and build PowerState."""
        data = self.config_data

        pv_production = self._read_sensor(data.get(CONF_PV_POWER), power=True)

        grid_export = 0.0
        grid_import = 0.0
        import_export_entity = data.get(CONF_IMPORT_EXPORT)
        grid_export_entity = data.get(CONF_GRID_EXPORT)

        if import_export_entity:
            combined = self._read_sensor(import_export_entity, power=True)
            grid_export = max(combined, 0.0)
            grid_import = abs(min(combined, 0.0))
        elif grid_export_entity:
            grid_export = self._read_sensor(grid_export_entity, power=True)

        load_power = self._read_sensor(data.get(CONF_LOAD_POWER), power=True)

        if load_power > 0:
            excess_power = pv_production - load_power
        else:
            excess_power = grid_export - grid_import

        return PowerState(
            pv_production=pv_production,
            grid_export=grid_export,
            grid_import=grid_import,
            load_power=load_power,
            excess_power=excess_power,
            timestamp=datetime.now(),
        )

    def collect_appliance_states(
        self,
        appliance_configs: list[ApplianceConfig],
        runtime_tracker: dict[str, timedelta],
    ) -> list[ApplianceState]:
        """Read current state of each managed appliance."""
        states: list[ApplianceState] = []

        for config in appliance_configs:
            entity_state = self.hass.states.get(config.entity_id)
            is_on = False
            if entity_state is not None:
                is_on = entity_state.state not in _OFF_STATES and entity_state.state not in _UNAVAILABLE_STATES

            current_power = 0.0
            if config.actual_power_entity:
                current_power = self._read_sensor(config.actual_power_entity, power=True)

            runtime_today = runtime_tracker.get(config.id, timedelta())

            state = ApplianceState(
                appliance_id=config.id,
                is_on=is_on,
                current_power=current_power,
                runtime_today=runtime_today,
                energy_today=0.0,
                last_state_change=None,
            )
            states.append(state)

        return states

    async def apply_decisions(
        self,
        decisions: list[ControlDecision],
        appliance_configs: list[ApplianceConfig],
    ) -> list[dict]:
        """Apply control decisions by calling HA services."""
        applied: list[dict] = []

        for decision in decisions:
            if decision.action == Action.IDLE:
                continue

            config = self._find_config(decision.appliance_id, appliance_configs)
            if not config:
                continue

            if not self._can_change_state(config):
                continue

            current_state = self.hass.states.get(config.entity_id)
            if not self._needs_change(decision, current_state, config):
                continue

            await self._apply_single(decision, config)
            self._last_state_change[config.id] = datetime.now()
            applied.append({"appliance_id": config.id, "action": decision.action})

            self.hass.bus.async_fire(
                "solar_power_control.appliance_switched",
                {
                    "appliance_id": config.id,
                    "appliance_name": config.name,
                    "action": decision.action,
                    "reason": decision.reason,
                },
            )

        return applied

    def _find_config(self, appliance_id: str, configs: list[ApplianceConfig]) -> ApplianceConfig | None:
        for config in configs:
            if config.id == appliance_id:
                return config
        return None

    def _can_change_state(self, config: ApplianceConfig) -> bool:
        last = self._last_state_change.get(config.id)
        if last is None:
            return True
        elapsed = (datetime.now() - last).total_seconds()
        return elapsed >= config.switch_interval

    def _needs_change(self, decision: ControlDecision, current_state, config: ApplianceConfig) -> bool:
        if current_state is None:
            return True

        entity_state = getattr(current_state, "state", None)

        if decision.action == Action.ON:
            if entity_state not in _OFF_STATES and entity_state not in _UNAVAILABLE_STATES:
                return False
        elif decision.action == Action.OFF:
            if entity_state in _OFF_STATES:
                return False
            if config.on_only:
                return False

        return True

    async def _apply_single(self, decision: ControlDecision, config: ApplianceConfig) -> None:
        entity_id = config.entity_id
        domain = entity_id.split(".")[0]

        if decision.action == Action.ON:
            await self._turn_on(domain, entity_id)
        elif decision.action == Action.OFF:
            if config.on_only:
                return
            await self._turn_off(domain, entity_id)

    async def _turn_on(self, domain: str, entity_id: str) -> None:
        service_map = {
            "switch": ("switch", "turn_on"),
            "climate": ("climate", "turn_on"),
            "light": ("light", "turn_on"),
            "water_heater": ("water_heater", "turn_on"),
            "input_boolean": ("input_boolean", "turn_on"),
        }
        if domain in service_map:
            svc_domain, svc_name = service_map[domain]
            await self.hass.services.async_call(svc_domain, svc_name, {"entity_id": entity_id})

    async def _turn_off(self, domain: str, entity_id: str) -> None:
        service_map = {
            "switch": ("switch", "turn_off"),
            "climate": ("climate", "turn_off"),
            "light": ("light", "turn_off"),
            "water_heater": ("water_heater", "turn_off"),
            "input_boolean": ("input_boolean", "turn_off"),
        }
        if domain in service_map:
            svc_domain, svc_name = service_map[domain]
            await self.hass.services.async_call(svc_domain, svc_name, {"entity_id": entity_id})
