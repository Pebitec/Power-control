"""Data models for PV Excess Control. Pure Python - no HA dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .const import Action


@dataclass(frozen=True)
class PowerState:
    """Snapshot of the current power situation.

    Sensor-backed fields are ``float | None``: ``None`` means the
    underlying HA sensor was ``unavailable`` when this snapshot was
    taken, while ``0.0`` means the sensor reported a genuine zero.
    """
    pv_production: float | None
    grid_export: float | None
    grid_import: float | None
    load_power: float | None
    excess_power: float | None
    timestamp: datetime


@dataclass
class ApplianceConfig:
    """Configuration for a managed appliance."""
    id: str
    name: str
    entity_id: str
    priority: int  # 1-1000, 1 = highest
    phases: int
    nominal_power: float
    actual_power_entity: str | None

    # Dynamic current (e.g. EV charger)
    dynamic_current: bool
    current_entity: str | None
    min_current: float
    max_current: float

    # EV-specific
    ev_soc_entity: str | None
    ev_connected_entity: str | None

    # Constraints
    on_only: bool

    # Switch interval in seconds
    switch_interval: int

    # Fields with defaults (must come after non-default fields)
    ev_target_soc: float | None = None
    averaging_window: int | None = None  # Per-appliance history window in seconds
    requires_appliance: str | None = None  # Subentry ID of required dependency appliance
    helper_only: bool = False
    override_active: bool = False
    override_until: datetime | None = None

    # Preemption protection
    protect_from_preemption: bool = False

    # Dynamic current step size (default 0.1A)
    current_step: float = 0.1

    # Per-appliance activation buffer in watts (None = use type-dependent default)
    on_threshold: int | None = None

    # Completion power threshold: power below which runtime stops counting (None = disabled)
    completion_power_threshold: float | None = None


@dataclass
class ApplianceState:
    """Runtime state of a managed appliance."""
    appliance_id: str
    is_on: bool
    current_power: float
    current_amperage: float | None
    runtime_today: timedelta
    energy_today: float  # kWh
    last_state_change: datetime | None
    ev_connected: bool | None  # None if not EV
    ev_soc: float | None = None
    activations_today: int = 0


@dataclass(frozen=True)
class ControlDecision:
    """Output of the optimizer for a single appliance."""
    appliance_id: str
    action: Action
    target_current: float | None
    reason: str
    overrides_plan: bool
    bypasses_cooldown: bool = False


@dataclass
class OptimizerResult:
    """Complete output of the optimizer for a single cycle."""
    decisions: list[ControlDecision]
