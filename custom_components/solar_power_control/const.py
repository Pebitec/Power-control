"""Constants for the Solar Power Control integration."""
from enum import StrEnum

DOMAIN = "solar_power_control"
MANUFACTURER = "Solar Power Control"

# Defaults
DEFAULT_CONTROLLER_INTERVAL = 30  # seconds
DEFAULT_SWITCH_INTERVAL = 300  # 5 minutes in seconds
DEFAULT_STARTUP_GRACE_PERIOD = 120  # 2 minutes in seconds

# Hysteresis defaults
DEFAULT_ON_THRESHOLD = 200  # W excess before turning ON
DEFAULT_OFF_THRESHOLD = -50  # W excess before turning OFF

# Limits
MIN_PRIORITY = 1
MAX_PRIORITY = 1000


class Action(StrEnum):
    """Control action for an appliance."""
    ON = "on"
    OFF = "off"
    IDLE = "idle"


# Config flow keys
CONF_PV_POWER = "pv_power"
CONF_GRID_EXPORT = "grid_export"
CONF_IMPORT_EXPORT = "import_export_power"
CONF_LOAD_POWER = "load_power"
CONF_CONTROLLER_INTERVAL = "controller_interval"
CONF_ENABLE_PREEMPTION = "enable_preemption"
CONF_OFF_THRESHOLD = "off_threshold"

# Appliance subentry config keys
CONF_APPLIANCE_NAME = "appliance_name"
CONF_APPLIANCE_ENTITY = "appliance_entity"
CONF_APPLIANCE_PRIORITY = "appliance_priority"
CONF_NOMINAL_POWER = "nominal_power"
CONF_ACTUAL_POWER_ENTITY = "actual_power_entity"
CONF_ON_ONLY = "on_only"
CONF_SWITCH_INTERVAL = "switch_interval"
CONF_AVERAGING_WINDOW = "averaging_window"
CONF_REQUIRES_APPLIANCE = "requires_appliance"
CONF_HELPER_ONLY = "helper_only"
CONF_PROTECT_FROM_PREEMPTION = "protect_from_preemption"
CONF_ON_THRESHOLD = "on_threshold"
CONF_COMPLETION_POWER_THRESHOLD = "completion_power_threshold"
