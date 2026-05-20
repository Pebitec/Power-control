"""Config flow for Solar Power Control integration.

Multi-step config flow that collects:
1. Sensor mapping (PV, grid, load sensors)
2. Global settings (controller interval, off threshold, preemption)

Also provides ApplianceSubentryFlow for managing appliances as subentries.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

# ConfigSubentryFlow is available in HA 2025.x+
try:
    from homeassistant.config_entries import (
        ConfigSubentryFlow,
        SubentryFlowResult,
    )
except ImportError:
    ConfigSubentryFlow = None  # type: ignore[assignment, misc]
    SubentryFlowResult = dict  # type: ignore[assignment, misc]

from .const import (
    CONF_ACTUAL_POWER_ENTITY,
    CONF_APPLIANCE_ENTITY,
    CONF_APPLIANCE_NAME,
    CONF_APPLIANCE_PRIORITY,
    CONF_AVERAGING_WINDOW,
    CONF_COMPLETION_POWER_THRESHOLD,
    CONF_CONTROLLER_INTERVAL,
    CONF_ENABLE_PREEMPTION,
    CONF_GRID_EXPORT,
    CONF_HELPER_ONLY,
    CONF_IMPORT_EXPORT,
    CONF_INVERT_IMPORT_EXPORT,
    CONF_LOAD_POWER,
    CONF_NOMINAL_POWER,
    CONF_OFF_THRESHOLD,
    CONF_ON_ONLY,
    CONF_ON_THRESHOLD,
    CONF_PROTECT_FROM_PREEMPTION,
    CONF_PV_POWER,
    CONF_REQUIRES_APPLIANCE,
    CONF_SWITCH_INTERVAL,
    DEFAULT_CONTROLLER_INTERVAL,
    DEFAULT_OFF_THRESHOLD,
    DEFAULT_SWITCH_INTERVAL,
    DOMAIN,
    MAX_PRIORITY,
    MIN_PRIORITY,
)

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE_APPLIANCE = "appliance"

CONTROLLER_INTERVAL_OPTIONS = [
    {"value": "15", "label": "15 seconds"},
    {"value": "30", "label": "30 seconds"},
    {"value": "60", "label": "60 seconds"},
]

SENSOR_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["sensor", "input_number"])
)

SWITCH_ENTITY_SELECTOR = EntitySelector(
    EntitySelectorConfig(domain=["switch", "input_boolean", "light", "climate", "fan"])
)


def _sensor_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_PV_POWER,
                description={"suggested_value": d.get(CONF_PV_POWER)},
            ): SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_GRID_EXPORT,
                description={"suggested_value": d.get(CONF_GRID_EXPORT)},
            ): SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_IMPORT_EXPORT,
                description={"suggested_value": d.get(CONF_IMPORT_EXPORT)},
            ): SENSOR_ENTITY_SELECTOR,
            vol.Optional(
                CONF_LOAD_POWER,
                description={"suggested_value": d.get(CONF_LOAD_POWER)},
            ): SENSOR_ENTITY_SELECTOR,
            vol.Required(
                CONF_INVERT_IMPORT_EXPORT,
                default=d.get(CONF_INVERT_IMPORT_EXPORT, False),
            ): BooleanSelector(),
        }
    )


def _settings_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_OFF_THRESHOLD,
                default=d.get(CONF_OFF_THRESHOLD, DEFAULT_OFF_THRESHOLD),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=-500,
                    max=0,
                    step=10,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_CONTROLLER_INTERVAL,
                default=str(d.get(CONF_CONTROLLER_INTERVAL, DEFAULT_CONTROLLER_INTERVAL)),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=CONTROLLER_INTERVAL_OPTIONS,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_ENABLE_PREEMPTION,
                default=d.get(CONF_ENABLE_PREEMPTION, True),
            ): BooleanSelector(),
        }
    )


def _appliance_basic_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_APPLIANCE_NAME,
                default=d.get(CONF_APPLIANCE_NAME, ""),
            ): str,
            vol.Required(
                CONF_APPLIANCE_ENTITY,
                default=d.get(CONF_APPLIANCE_ENTITY),
            ): SWITCH_ENTITY_SELECTOR,
            vol.Required(
                CONF_APPLIANCE_PRIORITY,
                default=d.get(CONF_APPLIANCE_PRIORITY, 500),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_PRIORITY,
                    max=MAX_PRIORITY,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_NOMINAL_POWER,
                default=d.get(CONF_NOMINAL_POWER, 0),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100000,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_ACTUAL_POWER_ENTITY,
                description={"suggested_value": d.get(CONF_ACTUAL_POWER_ENTITY)},
            ): SENSOR_ENTITY_SELECTOR,
        }
    )


def _appliance_constraints_schema(
    defaults: dict[str, Any] | None = None,
    available_appliances: dict[str, str] | None = None,
) -> vol.Schema:
    d = defaults or {}
    schema_dict: dict[vol.Marker, Any] = {
        vol.Required(
            CONF_SWITCH_INTERVAL,
            default=d.get(CONF_SWITCH_INTERVAL, DEFAULT_SWITCH_INTERVAL),
        ): NumberSelector(
            NumberSelectorConfig(
                min=5,
                max=3600,
                step=1,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_AVERAGING_WINDOW,
            description={"suggested_value": d.get(CONF_AVERAGING_WINDOW)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=30,
                max=1800,
                step=30,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_ON_ONLY,
            default=d.get(CONF_ON_ONLY, False),
        ): BooleanSelector(),
        vol.Required(
            CONF_PROTECT_FROM_PREEMPTION,
            default=d.get(CONF_PROTECT_FROM_PREEMPTION, False),
        ): BooleanSelector(),
        vol.Optional(
            CONF_ON_THRESHOLD,
            description={"suggested_value": d.get(CONF_ON_THRESHOLD)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=10000,
                step=10,
                unit_of_measurement="W",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(
            CONF_COMPLETION_POWER_THRESHOLD,
            description={"suggested_value": d.get(CONF_COMPLETION_POWER_THRESHOLD)},
        ): NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=10000,
                step=1,
                unit_of_measurement="W",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_HELPER_ONLY,
            default=d.get(CONF_HELPER_ONLY, False),
        ): BooleanSelector(),
    }

    if available_appliances:
        options = [{"value": "", "label": "(None)"}] + [
            {"value": aid, "label": aname}
            for aid, aname in available_appliances.items()
        ]
        schema_dict[
            vol.Optional(
                CONF_REQUIRES_APPLIANCE,
                description={"suggested_value": d.get(CONF_REQUIRES_APPLIANCE, "")},
            )
        ] = SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
        )

    return vol.Schema(schema_dict)


class SolarPowerControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Solar Power Control."""

    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the sensor mapping step."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            has_grid = bool(user_input.get(CONF_GRID_EXPORT))
            has_combined = bool(user_input.get(CONF_IMPORT_EXPORT))
            has_load = bool(user_input.get(CONF_LOAD_POWER))
            if not has_grid and not has_combined and not has_load:
                errors["base"] = "no_grid_sensor"

            if not errors:
                self.data.update(user_input)
                for key in [CONF_GRID_EXPORT, CONF_IMPORT_EXPORT, CONF_LOAD_POWER]:
                    if key not in user_input:
                        self.data.pop(key, None)
                return await self.async_step_settings()

        return self.async_show_form(
            step_id="user",
            data_schema=_sensor_schema(defaults=self.data),
            errors=errors,
            last_step=False,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the global settings step and create the config entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            controller_interval = int(
                user_input.get(CONF_CONTROLLER_INTERVAL, str(DEFAULT_CONTROLLER_INTERVAL))
            )
            user_input[CONF_CONTROLLER_INTERVAL] = controller_interval
            self.data.update(user_input)
            return self.async_create_entry(title="Solar Power Control", data=self.data)

        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(defaults=self.data),
            errors=errors,
            last_step=True,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type]:
        return {SUBENTRY_TYPE_APPLIANCE: ApplianceSubentryFlowHandler}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SolarPowerControlOptionsFlow:
        return SolarPowerControlOptionsFlow()


class SolarPowerControlOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Solar Power Control."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if not hasattr(self, "data") or not self.data:
            self.data = dict(self.config_entry.data)
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the sensor mapping step in options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            has_grid = bool(user_input.get(CONF_GRID_EXPORT))
            has_combined = bool(user_input.get(CONF_IMPORT_EXPORT))
            has_load = bool(user_input.get(CONF_LOAD_POWER))
            if not has_grid and not has_combined and not has_load:
                errors["base"] = "no_grid_sensor"

            if not errors:
                self.data.update(user_input)
                for key in [CONF_GRID_EXPORT, CONF_IMPORT_EXPORT, CONF_LOAD_POWER]:
                    if key not in user_input:
                        self.data.pop(key, None)
                return await self.async_step_settings()

        form_defaults = {**self.data, **(user_input or {})}
        return self.async_show_form(
            step_id="user",
            data_schema=_sensor_schema(defaults=form_defaults),
            errors=errors,
            last_step=False,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle settings and save options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            controller_interval = int(
                user_input.get(CONF_CONTROLLER_INTERVAL, str(DEFAULT_CONTROLLER_INTERVAL))
            )
            user_input[CONF_CONTROLLER_INTERVAL] = controller_interval
            self.data.update(user_input)

            self.hass.config_entries.async_update_entry(
                self.config_entry, data=self.data
            )
            return self.async_create_entry(data={})

        form_defaults = {**self.data, **(user_input or {})}
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(defaults=form_defaults),
            errors=errors,
            last_step=True,
        )


# ---------------------------------------------------------------------------
# Appliance Subentry Flow
# ---------------------------------------------------------------------------

_SubentryBase: type = ConfigSubentryFlow if ConfigSubentryFlow is not None else object


class ApplianceSubentryFlowHandler(_SubentryBase):  # type: ignore[misc]
    """Handle adding / editing an appliance subentry.

    Steps:
      1. user        - Basic info + power profile
      2. constraints - Switch interval, on-only, preemption protection, dependencies
    """

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1: Basic Info + Power Profile
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input.get(CONF_APPLIANCE_NAME, "").strip()
            if not name:
                errors[CONF_APPLIANCE_NAME] = "missing_name"

            entity = user_input.get(CONF_APPLIANCE_ENTITY)
            if not entity:
                errors[CONF_APPLIANCE_ENTITY] = "missing_entity"

            nominal = user_input.get(CONF_NOMINAL_POWER, 0)
            if nominal <= 0:
                errors[CONF_NOMINAL_POWER] = "invalid_power"

            if not errors:
                user_input[CONF_APPLIANCE_PRIORITY] = int(
                    user_input.get(CONF_APPLIANCE_PRIORITY, 500)
                )
                self._data.update(user_input)
                return await self.async_step_constraints()

        form_defaults = {**self._data, **(user_input or {})}
        return self.async_show_form(
            step_id="user",
            data_schema=_appliance_basic_schema(form_defaults),
            errors=errors,
            last_step=False,
        )

    # ------------------------------------------------------------------
    # Step 2: Constraints
    # ------------------------------------------------------------------

    async def async_step_constraints(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        available_appliances = self._get_available_appliances()

        if user_input is not None:
            if user_input.get(CONF_REQUIRES_APPLIANCE) == "":
                user_input.pop(CONF_REQUIRES_APPLIANCE, None)

            req = user_input.get(CONF_REQUIRES_APPLIANCE)
            my_id = getattr(self, "_subentry_id", None)
            entry = self._get_parent_entry()
            if req and entry and my_id:
                req_sub = getattr(entry, "subentries", {}).get(req)
                if req_sub and req_sub.data.get(CONF_REQUIRES_APPLIANCE) == my_id:
                    errors[CONF_REQUIRES_APPLIANCE] = "circular_dependency"

            if user_input.get(CONF_HELPER_ONLY, False) and user_input.get(CONF_REQUIRES_APPLIANCE):
                errors[CONF_HELPER_ONLY] = "helper_only_with_requires"

            if not errors:
                self._data.update(user_input)
                for key in [CONF_AVERAGING_WINDOW, CONF_REQUIRES_APPLIANCE,
                            CONF_ON_THRESHOLD, CONF_COMPLETION_POWER_THRESHOLD]:
                    if key not in user_input:
                        self._data.pop(key, None)
                title = self._data.get(CONF_APPLIANCE_NAME, "Appliance")
                return self.async_create_entry(title=title, data=self._data)

        form_defaults = {**self._data, **(user_input or {})}
        return self.async_show_form(
            step_id="constraints",
            data_schema=_appliance_constraints_schema(
                form_defaults, available_appliances=available_appliances
            ),
            errors=errors,
            last_step=True,
        )

    # ------------------------------------------------------------------
    # Reconfigure: pre-populate from existing subentry data
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if not hasattr(self, "_get_reconfigure_subentry"):
            return self.async_abort(reason="reconfigure_not_supported")
        subentry = self._get_reconfigure_subentry()
        self._data = dict(subentry.data)
        self._subentry_id = getattr(subentry, "subentry_id", None) or getattr(subentry, "id", None)
        return await self.async_step_reconfigure_basic(None)

    async def async_step_reconfigure_basic(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_APPLIANCE_NAME, "").strip():
                errors[CONF_APPLIANCE_NAME] = "missing_name"
            if not user_input.get(CONF_APPLIANCE_ENTITY):
                errors[CONF_APPLIANCE_ENTITY] = "missing_entity"
            if user_input.get(CONF_NOMINAL_POWER, 0) <= 0:
                errors[CONF_NOMINAL_POWER] = "invalid_power"

            if not errors:
                user_input[CONF_APPLIANCE_PRIORITY] = int(
                    user_input.get(CONF_APPLIANCE_PRIORITY, 500)
                )
                self._data.update(user_input)
                return await self.async_step_reconfigure_constraints()

        form_defaults = {**self._data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure_basic",
            data_schema=_appliance_basic_schema(form_defaults),
            errors=errors,
            last_step=False,
        )

    async def async_step_reconfigure_constraints(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        available_appliances = self._get_available_appliances()

        if user_input is not None:
            if user_input.get(CONF_REQUIRES_APPLIANCE) == "":
                user_input.pop(CONF_REQUIRES_APPLIANCE, None)

            req = user_input.get(CONF_REQUIRES_APPLIANCE)
            my_id = getattr(self, "_subentry_id", None)
            entry = self._get_parent_entry()
            if req and entry and my_id:
                req_sub = getattr(entry, "subentries", {}).get(req)
                if req_sub and req_sub.data.get(CONF_REQUIRES_APPLIANCE) == my_id:
                    errors[CONF_REQUIRES_APPLIANCE] = "circular_dependency"

            if user_input.get(CONF_HELPER_ONLY, False) and user_input.get(CONF_REQUIRES_APPLIANCE):
                errors[CONF_HELPER_ONLY] = "helper_only_with_requires"

            if not errors:
                self._data.update(user_input)
                for key in [CONF_AVERAGING_WINDOW, CONF_REQUIRES_APPLIANCE,
                            CONF_ON_THRESHOLD, CONF_COMPLETION_POWER_THRESHOLD]:
                    if key not in user_input:
                        self._data.pop(key, None)
                title = self._data.get(CONF_APPLIANCE_NAME, "Appliance")
                try:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        title=title,
                        data=self._data,
                    )
                except (AttributeError, TypeError):
                    try:
                        entry = self._get_parent_entry()
                        if entry and hasattr(self.hass.config_entries, "async_update_subentry"):
                            subentry_id = getattr(self, "_subentry_id", None)
                            if subentry_id and subentry_id in getattr(entry, "subentries", {}):
                                self.hass.config_entries.async_update_subentry(
                                    entry,
                                    entry.subentries[subentry_id],
                                    data=self._data,
                                    title=title,
                                )
                                return self.async_abort(reason="reconfigure_successful")
                    except Exception:
                        pass
                    return self.async_abort(reason="reconfigure_not_supported")

        form_defaults = {**self._data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure_constraints",
            data_schema=_appliance_constraints_schema(
                form_defaults, available_appliances=available_appliances
            ),
            errors=errors,
            last_step=True,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_parent_entry(self):
        handler = getattr(self, "handler", None)
        if isinstance(handler, (list, tuple)) and len(handler) > 0:
            return self.hass.config_entries.async_get_entry(handler[0])
        return None

    def _get_available_appliances(self) -> dict[str, str]:
        entry = self._get_parent_entry()
        if not entry:
            return {}
        my_id = getattr(self, "_subentry_id", None)
        return {
            sid: sub.data.get(CONF_APPLIANCE_NAME, f"Appliance {sid[:8]}")
            for sid, sub in getattr(entry, "subentries", {}).items()
            if sid != my_id
        }
