"""DataUpdateCoordinator for PV Excess Control.

Central data hub that:
1. Collects sensor states from Home Assistant
2. Maintains a rolling power history buffer
3. Runs the optimizer on each update cycle
4. Applies control decisions to HA entities
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .analytics import AnalyticsTracker
from .const import (
    CONF_ACTUAL_POWER_ENTITY,
    CONF_APPLIANCE_ENTITY,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCE_PRIORITY,
    CONF_AVERAGING_WINDOW,
    CONF_COMPLETION_POWER_THRESHOLD,
    CONF_CONTROLLER_INTERVAL,
    CONF_CURRENT_ENTITY,
    CONF_CURRENT_STEP,
    CONF_DYNAMIC_CURRENT,
    CONF_ENABLE_PREEMPTION,
    CONF_EV_CONNECTED_ENTITY,
    CONF_EV_SOC_ENTITY,
    CONF_EV_TARGET_SOC,
    CONF_GRID_EXPORT,
    CONF_GRID_VOLTAGE,
    CONF_HELPER_ONLY,
    CONF_IMPORT_EXPORT,
    CONF_LOAD_POWER,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_NOMINAL_POWER,
    CONF_OFF_THRESHOLD,
    CONF_ON_ONLY,
    CONF_ON_THRESHOLD,
    CONF_PHASES,
    CONF_PROTECT_FROM_PREEMPTION,
    CONF_PV_POWER,
    CONF_REQUIRES_APPLIANCE,
    CONF_SWITCH_INTERVAL,
    DEFAULT_CONTROLLER_INTERVAL,
    DEFAULT_GRID_VOLTAGE,
    DEFAULT_OFF_THRESHOLD,
    DEFAULT_STARTUP_GRACE_PERIOD,
    DEFAULT_SWITCH_INTERVAL,
    DOMAIN,
)
from .models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    ControlDecision,
    OptimizerResult,
    PowerState,
)
from .optimizer import Optimizer

_LOGGER = logging.getLogger(__name__)

_OFF_STATES = {"off", "false", "False", "0"}
_UNAVAILABLE_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, "none", ""}

MAX_HISTORY_SIZE = 60

_POWER_UNIT_MULTIPLIERS: dict[str, float] = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
}


def _normalise_power(value: float, unit: str | None) -> float:
    if unit is None:
        return value
    return value * _POWER_UNIT_MULTIPLIERS.get(unit.lower().strip(), 1.0)


def _parse_sensor_float(
    hass: HomeAssistant,
    entity_id: str | None,
    *,
    power: bool = False,
) -> float | None:
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        return None
    try:
        val = float(state.state)
        if math.isnan(val) or math.isinf(val):
            return None
    except (ValueError, TypeError):
        return None
    if power:
        val = _normalise_power(val, state.attributes.get("unit_of_measurement"))
    return val


def _parse_sensor_bool(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    if entity_id is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _UNAVAILABLE_STATES:
        return None
    return state.state in ("on", "true", "True", "1")


def _parse_time_string(value: str | None):
    if value is None:
        return None
    try:
        from datetime import time
        parts = value.split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError, TypeError):
        return None


class PvExcessCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for PV Excess Control."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        controller_interval = config_entry.data.get(
            CONF_CONTROLLER_INTERVAL, DEFAULT_CONTROLLER_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=controller_interval),
        )

        grid_voltage = config_entry.data.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)
        tz_name = str(hass.config.time_zone) if hasattr(hass.config, "time_zone") else "UTC"
        enable_preemption = config_entry.data.get(CONF_ENABLE_PREEMPTION, True)
        off_threshold = config_entry.data.get(CONF_OFF_THRESHOLD, DEFAULT_OFF_THRESHOLD)
        self.optimizer = Optimizer(
            grid_voltage=grid_voltage,
            timezone_str=tz_name,
            enable_preemption=enable_preemption,
            off_threshold=off_threshold,
        )

        self.power_history: list[PowerState] = []
        self._last_sensor_available: dict[str, bool] = {}
        self._last_appliance_configs: list[ApplianceConfig] = []
        self.appliance_states: dict[str, ApplianceState] = {}
        self.control_decisions: list[ControlDecision] = []

        self._was_enabled = True
        self._startup_time = datetime.now()
        self._enabled = config_entry.data.get("control_enabled", True)

        disabled_ids = set(config_entry.data.get("disabled_appliances", []))
        overridden_ids = set(config_entry.data.get("overridden_appliances", []))
        self.appliance_enabled: dict[str, bool] = {aid: False for aid in disabled_ids}
        self.appliance_overrides: dict[str, bool] = {aid: True for aid in overridden_ids}
        self.appliance_priorities: dict[str, int] = {}

        subentries = getattr(config_entry, "subentries", {})
        for subentry_id, subentry in subentries.items():
            d = subentry.data
            saved_priority = d.get(CONF_APPLIANCE_PRIORITY, 500)
            self.appliance_priorities[subentry_id] = saved_priority

        self._last_state_change: dict[str, datetime] = {}
        self._last_applied_current: dict[str, float] = {}
        self._activations_today: dict[str, int] = {}
        self._needed_by_others: set[str] = set()
        self._previous_is_on: dict[str, bool] = {}

        self.analytics = AnalyticsTracker(import_price=0.25, feed_in_tariff=0.0)

        _LOGGER.info(
            "PV Excess Control initialized: voltage=%sV, controller_interval=%ss",
            config_entry.data.get(CONF_GRID_VOLTAGE, "?"),
            controller_interval,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def reset_daily(self) -> None:
        """Reset daily counters at midnight."""
        new_states: dict[str, ApplianceState] = {}
        for key, state in self.appliance_states.items():
            new_states[key] = ApplianceState(
                appliance_id=state.appliance_id,
                is_on=state.is_on,
                current_power=state.current_power,
                current_amperage=state.current_amperage,
                runtime_today=timedelta(),
                energy_today=0.0,
                last_state_change=state.last_state_change,
                ev_connected=state.ev_connected,
                ev_soc=state.ev_soc,
                activations_today=0,
            )
        self.appliance_states = new_states
        new_last_change = {}
        for key, state in new_states.items():
            if state.is_on and key in self._last_state_change:
                new_last_change[key] = self._last_state_change[key]
        self._last_state_change = new_last_change
        self._activations_today.clear()
        self.analytics.reset_daily()
        _LOGGER.info("Midnight reset: cleared daily counters")

    async def _async_update_data(self) -> dict[str, Any]:
        """Main control loop, called every controller_interval seconds."""
        # 1. Collect power state
        power_state = self._collect_power_state()

        _LOGGER.debug(
            "Cycle: PV=%s export=%s import=%s load=%s excess=%s",
            f"{power_state.pv_production:.0f}W" if power_state.pv_production is not None else "unavail",
            f"{power_state.grid_export:.0f}W" if power_state.grid_export is not None else "unavail",
            f"{power_state.grid_import:.0f}W" if power_state.grid_import is not None else "unavail",
            f"{power_state.load_power:.0f}W" if power_state.load_power is not None else "unavail",
            f"{power_state.excess_power:.0f}W" if power_state.excess_power is not None else "unavail",
        )

        # 2. Append to history
        self.power_history.append(power_state)
        if len(self.power_history) > MAX_HISTORY_SIZE:
            self.power_history.pop(0)

        # 3. Skip optimizer if disabled
        if not self._enabled:
            _LOGGER.debug("Controller disabled, skipping optimization")
            if self._was_enabled:
                await self._turn_off_all_managed()
            self._was_enabled = False
            return self._build_coordinator_data()

        self._was_enabled = True

        # 4. Get appliance configs and states
        appliance_configs = self._get_appliance_configs()
        self._last_appliance_configs = appliance_configs
        appliance_states = self._get_appliance_states(appliance_configs)

        elapsed = (datetime.now() - self._startup_time).total_seconds()
        if elapsed < DEFAULT_STARTUP_GRACE_PERIOD:
            _LOGGER.debug(
                "Startup grace period (%ds remaining), skipping optimization",
                int(DEFAULT_STARTUP_GRACE_PERIOD - elapsed),
            )
            return self._build_coordinator_data()

        # Refresh grid_voltage from config each cycle
        grid_voltage = self.config_entry.data.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)
        self.optimizer.grid_voltage = grid_voltage

        # 5. Run optimizer
        try:
            result = self.optimizer.optimize(
                power_state=power_state,
                appliances=appliance_configs,
                appliance_states=list(appliance_states.values()),
                power_history=self.power_history,
            )
        except Exception as err:
            _LOGGER.error("Optimizer error: %s", err)
            raise UpdateFailed(f"Optimizer error: {err}") from err

        self.control_decisions = result.decisions

        on_count = sum(1 for d in result.decisions if d.action == Action.ON)
        off_count = sum(1 for d in result.decisions if d.action == Action.OFF)
        set_count = sum(1 for d in result.decisions if d.action == Action.SET_CURRENT)
        _LOGGER.debug(
            "Optimizer: %d decisions (ON=%d OFF=%d SET_CURRENT=%d)",
            len(result.decisions), on_count, off_count, set_count,
        )

        # 6. Record analytics
        cycle_seconds = self.update_interval.total_seconds()
        for decision in result.decisions:
            if decision.action in (Action.ON, Action.SET_CURRENT):
                if not self.appliance_enabled.get(decision.appliance_id, True):
                    continue
                config = self._get_appliance_config_by_id(decision.appliance_id)
                if config is None:
                    continue
                app_state = appliance_states.get(decision.appliance_id)
                power = (app_state.current_power if app_state and app_state.current_power > 0
                         else config.nominal_power if config else 0)
                source = "solar" if (power_state.excess_power is not None and power_state.excess_power > 0) else "grid"
                self.analytics.record_cycle(decision.appliance_id, power, cycle_seconds, source)

        if power_state.pv_production is not None:
            self.analytics.record_solar_production(power_state.pv_production, cycle_seconds)
        if power_state.grid_export is not None and power_state.grid_export > 0:
            self.analytics.record_grid_export(power_state.grid_export, cycle_seconds)

        # 7. Apply decisions
        await self._apply_decisions(result)

        return self._build_coordinator_data()

    def _track_sensor_availability(self, entity_id: str | None, value: float | None) -> None:
        if entity_id is None:
            return
        is_available = value is not None
        previous = self._last_sensor_available.get(entity_id)
        if previous is None:
            if not is_available:
                _LOGGER.warning("Required sensor %s is unavailable", entity_id)
            self._last_sensor_available[entity_id] = is_available
            return
        if previous and not is_available:
            _LOGGER.warning("Required sensor %s became unavailable", entity_id)
        elif not previous and is_available:
            _LOGGER.info("Sensor %s is available again", entity_id)
        self._last_sensor_available[entity_id] = is_available

    def _collect_power_state(self) -> PowerState:
        """Read power sensor entities and build a PowerState snapshot."""
        data = self.config_entry.data

        pv_production: float | None = _parse_sensor_float(self.hass, data.get(CONF_PV_POWER), power=True)
        self._track_sensor_availability(data.get(CONF_PV_POWER), pv_production)

        grid_export: float | None = None
        grid_import: float | None = None
        import_export_entity = data.get(CONF_IMPORT_EXPORT)
        grid_export_entity = data.get(CONF_GRID_EXPORT)

        if import_export_entity:
            combined = _parse_sensor_float(self.hass, import_export_entity, power=True)
            self._track_sensor_availability(import_export_entity, combined)
            if combined is None:
                grid_export = None
                grid_import = None
            else:
                grid_export = max(combined, 0.0)
                grid_import = abs(min(combined, 0.0))
        elif grid_export_entity:
            grid_export = _parse_sensor_float(self.hass, grid_export_entity, power=True)
            self._track_sensor_availability(grid_export_entity, grid_export)
            grid_import = 0.0 if grid_export is not None else None

        load_power: float | None = _parse_sensor_float(self.hass, data.get(CONF_LOAD_POWER), power=True)
        self._track_sensor_availability(data.get(CONF_LOAD_POWER), load_power)

        # Calculate excess
        excess_power: float | None
        if import_export_entity:
            if grid_export is None or grid_import is None:
                excess_power = None
            else:
                excess_power = grid_export - grid_import
        elif grid_export_entity:
            if grid_export is None:
                excess_power = None
            elif grid_export > 0:
                excess_power = grid_export
            elif pv_production is not None and load_power is not None and load_power > 0:
                excess_power = pv_production - load_power
            else:
                excess_power = None
        elif load_power is not None and load_power > 0:
            excess_power = pv_production - load_power if pv_production is not None else None
        else:
            excess_power = 0.0

        return PowerState(
            pv_production=pv_production,
            grid_export=grid_export,
            grid_import=grid_import,
            load_power=load_power,
            excess_power=excess_power,
            timestamp=datetime.now(),
        )

    def _get_appliance_configs(self) -> list[ApplianceConfig]:
        """Convert config entry subentries to ApplianceConfig list."""
        configs: list[ApplianceConfig] = []

        subentries = getattr(self.config_entry, "subentries", {})
        for subentry_id, subentry in subentries.items():
            sub_data = subentry.data
            priority = self.appliance_priorities.get(
                subentry_id, sub_data.get(CONF_APPLIANCE_PRIORITY, 500)
            )
            override_active = self.appliance_overrides.get(subentry_id, False)
            is_enabled = self.appliance_enabled.get(subentry_id, True)

            if not is_enabled and not override_active:
                continue

            entity_id = sub_data.get(CONF_APPLIANCE_ENTITY, "")
            if not entity_id:
                _LOGGER.warning(
                    "Appliance %s has no entity configured, skipping",
                    sub_data.get(CONF_APPLIANCE_NAME, subentry_id),
                )
                continue

            switch_interval = int(max(5, sub_data.get(CONF_SWITCH_INTERVAL, DEFAULT_SWITCH_INTERVAL)))

            config = ApplianceConfig(
                id=subentry_id,
                name=sub_data.get(CONF_APPLIANCE_NAME, f"Appliance {subentry_id}"),
                entity_id=entity_id,
                priority=priority,
                phases=int(sub_data.get(CONF_PHASES, 1)),
                nominal_power=sub_data.get(CONF_NOMINAL_POWER, 0.0),
                actual_power_entity=sub_data.get(CONF_ACTUAL_POWER_ENTITY),
                dynamic_current=sub_data.get(CONF_DYNAMIC_CURRENT, False),
                current_entity=sub_data.get(CONF_CURRENT_ENTITY),
                min_current=sub_data.get(CONF_MIN_CURRENT, 6.0),
                max_current=sub_data.get(CONF_MAX_CURRENT, 16.0),
                ev_soc_entity=sub_data.get(CONF_EV_SOC_ENTITY),
                ev_connected_entity=sub_data.get(CONF_EV_CONNECTED_ENTITY),
                ev_target_soc=sub_data.get(CONF_EV_TARGET_SOC),
                on_only=sub_data.get(CONF_ON_ONLY, False),
                switch_interval=switch_interval,
                averaging_window=sub_data.get(CONF_AVERAGING_WINDOW),
                requires_appliance=sub_data.get(CONF_REQUIRES_APPLIANCE),
                helper_only=sub_data.get(CONF_HELPER_ONLY, False),
                protect_from_preemption=sub_data.get(CONF_PROTECT_FROM_PREEMPTION, False),
                current_step=sub_data.get(CONF_CURRENT_STEP, 0.1),
                override_active=override_active,
                on_threshold=sub_data.get(CONF_ON_THRESHOLD),
                completion_power_threshold=sub_data.get(CONF_COMPLETION_POWER_THRESHOLD),
            )
            configs.append(config)

        self._needed_by_others = {
            c.requires_appliance for c in configs if c.requires_appliance
        }

        active_ids = set(subentries.keys())
        for d in (
            self._last_state_change,
            self._last_applied_current,
            self._activations_today,
            self._previous_is_on,
            self.appliance_enabled,
            self.appliance_overrides,
            self.appliance_priorities,
        ):
            stale = [k for k in d if k not in active_ids]
            for k in stale:
                del d[k]

        return configs

    def _get_appliance_states(self, configs: list[ApplianceConfig]) -> dict[str, ApplianceState]:
        """Read current state of each controlled appliance entity."""
        states: dict[str, ApplianceState] = {}

        for config in configs:
            entity_state = self.hass.states.get(config.entity_id)
            is_on = False
            if entity_state is not None:
                is_on = entity_state.state not in _OFF_STATES and entity_state.state not in _UNAVAILABLE_STATES

            prev_is_on = self._previous_is_on.get(config.id)
            if prev_is_on is False and is_on is True:
                self._activations_today[config.id] = self._activations_today.get(config.id, 0) + 1
            self._previous_is_on[config.id] = is_on

            current_power = 0.0
            if config.actual_power_entity:
                current_power = (_parse_sensor_float(self.hass, config.actual_power_entity, power=True) or 0.0)

            current_amperage: float | None = None
            if config.current_entity:
                current_amperage = _parse_sensor_float(self.hass, config.current_entity)

            ev_connected: bool | None = None
            if config.ev_connected_entity:
                ev_connected = _parse_sensor_bool(self.hass, config.ev_connected_entity)

            ev_soc: float | None = None
            if config.ev_soc_entity:
                ev_soc = _parse_sensor_float(self.hass, config.ev_soc_entity)

            previous = self.appliance_states.get(config.id)
            runtime_today = previous.runtime_today if previous else timedelta()
            energy_today = previous.energy_today if previous else 0.0
            last_state_change = previous.last_state_change if previous else None

            if is_on and previous is not None:
                cycle_seconds = self.update_interval.total_seconds()
                counts_as_running = (
                    config.completion_power_threshold is None
                    or current_power >= config.completion_power_threshold
                )
                if counts_as_running:
                    runtime_today += timedelta(seconds=cycle_seconds)
                power_for_energy = (
                    current_power if current_power > 0
                    else (0.0 if config.actual_power_entity else config.nominal_power)
                )
                energy_today += (power_for_energy * cycle_seconds) / 3600 / 1000

            if is_on and config.id not in self._last_state_change:
                self._last_state_change[config.id] = datetime.now()

            state = ApplianceState(
                appliance_id=config.id,
                is_on=is_on,
                current_power=current_power,
                current_amperage=current_amperage,
                runtime_today=runtime_today,
                energy_today=energy_today,
                last_state_change=last_state_change,
                ev_connected=ev_connected,
                ev_soc=ev_soc,
                activations_today=self._activations_today.get(config.id, 0),
            )
            states[config.id] = state

        # Preserve state for disabled appliances
        subentries = getattr(self.config_entry, "subentries", {})
        for sub_id in subentries:
            if sub_id not in states and sub_id in self.appliance_states:
                old = self.appliance_states[sub_id]
                sub_data = subentries[sub_id].data

                entity_id = sub_data.get(CONF_APPLIANCE_ENTITY, "")
                entity_state = self.hass.states.get(entity_id) if entity_id else None
                is_on = (
                    entity_state is not None
                    and entity_state.state not in _OFF_STATES
                    and entity_state.state not in _UNAVAILABLE_STATES
                )

                power_entity = sub_data.get(CONF_ACTUAL_POWER_ENTITY)
                current_power = (
                    _parse_sensor_float(self.hass, power_entity, power=True) or 0.0
                ) if power_entity else 0.0

                states[sub_id] = ApplianceState(
                    appliance_id=old.appliance_id,
                    is_on=is_on,
                    current_power=current_power,
                    current_amperage=old.current_amperage,
                    runtime_today=old.runtime_today,
                    energy_today=old.energy_today,
                    last_state_change=old.last_state_change,
                    ev_connected=old.ev_connected,
                    ev_soc=old.ev_soc,
                    activations_today=old.activations_today,
                )

        self.appliance_states = states
        return states

    async def _apply_decisions(self, result: OptimizerResult) -> list[str]:
        """Apply control decisions by calling HA services."""
        if not self._enabled:
            return []

        applied_ids: list[str] = []

        def _dep_sort_key(d):
            cfg = self._get_appliance_config_by_id(d.appliance_id)
            has_dep = cfg.requires_appliance if cfg else None
            if d.action == Action.OFF:
                return (0 if has_dep else 1,)
            else:
                return (1 if has_dep else 0,)

        sorted_decisions = sorted(result.decisions, key=_dep_sort_key)

        for decision in sorted_decisions:
            if decision.action == Action.IDLE:
                continue

            if not self.appliance_enabled.get(decision.appliance_id, True) and not self.appliance_overrides.get(decision.appliance_id, False):
                continue

            appliance_config = self._get_appliance_config_by_id(decision.appliance_id)
            if appliance_config is None:
                continue

            entity_id = appliance_config.entity_id
            domain = entity_id.split(".")[0] if "." in entity_id else "switch"

            current_state = self.hass.states.get(entity_id)
            if current_state is None:
                _LOGGER.warning("Entity %s not found in HA, skipping", entity_id)
                continue

            is_on = current_state.state not in _OFF_STATES and current_state.state not in _UNAVAILABLE_STATES
            if decision.action == Action.ON and is_on:
                continue
            if decision.action == Action.OFF and not is_on:
                continue

            is_needed_by_others = decision.appliance_id in self._needed_by_others
            if not decision.bypasses_cooldown and not is_needed_by_others:
                if decision.action != Action.SET_CURRENT or not is_on:
                    last_change = self._last_state_change.get(decision.appliance_id)
                    if last_change is not None:
                        elapsed = (datetime.now() - last_change).total_seconds()
                        if elapsed < appliance_config.switch_interval:
                            _LOGGER.debug(
                                "Skipping %s for %s: switch interval not elapsed (%.0fs of %ds)",
                                decision.action, appliance_config.name, elapsed, appliance_config.switch_interval,
                            )
                            continue

            try:
                if decision.action == Action.ON:
                    async with asyncio.timeout(10):
                        await self.hass.services.async_call(
                            domain, "turn_on", {"entity_id": entity_id}, blocking=True,
                        )
                elif decision.action == Action.OFF:
                    async with asyncio.timeout(10):
                        await self.hass.services.async_call(
                            domain, "turn_off", {"entity_id": entity_id}, blocking=True,
                        )
                    self._last_applied_current.pop(decision.appliance_id, None)
                elif decision.action == Action.SET_CURRENT:
                    if appliance_config.current_entity and decision.target_current is not None:
                        if (
                            is_on
                            and decision.target_current == self._last_applied_current.get(decision.appliance_id)
                        ):
                            continue

                        current_domain = (
                            appliance_config.current_entity.split(".")[0]
                            if "." in appliance_config.current_entity
                            else "number"
                        )
                        async with asyncio.timeout(10):
                            await self.hass.services.async_call(
                                current_domain,
                                "set_value",
                                {"entity_id": appliance_config.current_entity, "value": decision.target_current},
                                blocking=True,
                            )
                        self._last_applied_current[decision.appliance_id] = decision.target_current

                        if not is_on:
                            async with asyncio.timeout(10):
                                await self.hass.services.async_call(
                                    domain, "turn_on", {"entity_id": entity_id}, blocking=True,
                                )
                    else:
                        continue

                if decision.action in (Action.ON, Action.OFF):
                    self._last_state_change[decision.appliance_id] = datetime.now()
                elif decision.action == Action.SET_CURRENT and not is_on:
                    self._last_state_change[decision.appliance_id] = datetime.now()

                applied_ids.append(decision.appliance_id)
                _LOGGER.info(
                    "Applied %s to %s (%s): %s",
                    decision.action, appliance_config.name, entity_id, decision.reason,
                )
            except Exception as err:
                _LOGGER.error("Failed to apply decision for %s: %s", decision.appliance_id, err)

        return applied_ids

    def _get_appliance_config_by_id(self, appliance_id: str) -> ApplianceConfig | None:
        """Look up an appliance config by its subentry ID."""
        subentries = getattr(self.config_entry, "subentries", {})
        subentry = subentries.get(appliance_id)
        if subentry is None:
            return None

        sub_data = subentry.data
        priority = self.appliance_priorities.get(appliance_id, sub_data.get(CONF_APPLIANCE_PRIORITY, 500))
        override_active = self.appliance_overrides.get(appliance_id, False)

        return ApplianceConfig(
            id=appliance_id,
            name=sub_data.get(CONF_APPLIANCE_NAME, f"Appliance {appliance_id}"),
            entity_id=sub_data.get(CONF_APPLIANCE_ENTITY, ""),
            priority=priority,
            phases=int(sub_data.get(CONF_PHASES, 1)),
            nominal_power=sub_data.get(CONF_NOMINAL_POWER, 0.0),
            actual_power_entity=sub_data.get(CONF_ACTUAL_POWER_ENTITY),
            dynamic_current=sub_data.get(CONF_DYNAMIC_CURRENT, False),
            current_entity=sub_data.get(CONF_CURRENT_ENTITY),
            min_current=sub_data.get(CONF_MIN_CURRENT, 6.0),
            max_current=sub_data.get(CONF_MAX_CURRENT, 16.0),
            ev_soc_entity=sub_data.get(CONF_EV_SOC_ENTITY),
            ev_connected_entity=sub_data.get(CONF_EV_CONNECTED_ENTITY),
            ev_target_soc=sub_data.get(CONF_EV_TARGET_SOC),
            on_only=sub_data.get(CONF_ON_ONLY, False),
            switch_interval=int(max(5, sub_data.get(CONF_SWITCH_INTERVAL, DEFAULT_SWITCH_INTERVAL))),
            averaging_window=sub_data.get(CONF_AVERAGING_WINDOW),
            requires_appliance=sub_data.get(CONF_REQUIRES_APPLIANCE),
            helper_only=sub_data.get(CONF_HELPER_ONLY, False),
            protect_from_preemption=sub_data.get(CONF_PROTECT_FROM_PREEMPTION, False),
            current_step=sub_data.get(CONF_CURRENT_STEP, 0.1),
            override_active=override_active,
            on_threshold=sub_data.get(CONF_ON_THRESHOLD),
            completion_power_threshold=sub_data.get(CONF_COMPLETION_POWER_THRESHOLD),
        )

    async def _turn_off_all_managed(self) -> None:
        """Turn off all currently-ON managed appliances."""
        subentries = getattr(self.config_entry, "subentries", {})
        for subentry_id, subentry in subentries.items():
            entity_id = subentry.data.get(CONF_APPLIANCE_ENTITY, "")
            if not entity_id:
                continue
            current_state = self.hass.states.get(entity_id)
            if current_state is None:
                continue
            if current_state.state in _OFF_STATES or current_state.state in _UNAVAILABLE_STATES:
                continue
            domain = entity_id.split(".")[0] if "." in entity_id else "switch"
            name = subentry.data.get(CONF_APPLIANCE_NAME, subentry_id)
            try:
                async with asyncio.timeout(10):
                    await self.hass.services.async_call(
                        domain, "turn_off", {"entity_id": entity_id}, blocking=True,
                    )
                _LOGGER.info("Master switch disabled: turned off %s (%s)", name, entity_id)
            except Exception as err:
                _LOGGER.error("Failed to turn off %s on master disable: %s", name, err)

    def _build_coordinator_data(self) -> dict[str, Any]:
        """Build a data dict that entity platforms can read."""
        latest_power = self.power_history[-1] if self.power_history else None

        elapsed = (datetime.now() - self._startup_time).total_seconds()
        grace_period_remaining: float | None = (
            DEFAULT_STARTUP_GRACE_PERIOD - elapsed if elapsed < DEFAULT_STARTUP_GRACE_PERIOD else None
        )

        return {
            "power_state": latest_power,
            "power_history": list(self.power_history),
            "control_decisions": list(self.control_decisions),
            "appliance_states": dict(self.appliance_states),
            "appliance_configs": {c.id: c for c in self._last_appliance_configs},
            "grace_period_remaining": grace_period_remaining,
            "enabled": self._enabled,
            "analytics": {
                "self_consumption_ratio": self.analytics.self_consumption_ratio,
                "savings_today": self.analytics.savings_today,
                "solar_consumed_kwh": self.analytics.solar_consumed_kwh,
                "grid_export_kwh": self.analytics.grid_export_kwh,
            },
        }
