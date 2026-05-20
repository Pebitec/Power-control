"""Data models for Solar Power Control. Pure Python - no HA dependencies."""
from __future__ import annotations

from dataclasses import dataclass
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
    nominal_power: float
    actual_power_entity: str | None
    on_only: bool
    switch_interval: int  # seconds
    averaging_window: int | None = None
    requires_appliance: str | None = None
    helper_only: bool = False
    override_active: bool = False
    override_until: datetime | None = None
    protect_from_preemption: bool = False
    on_threshold: int | None = None
    completion_power_threshold: float | None = None


@dataclass
class ApplianceState:
    """Runtime state of a managed appliance."""
    appliance_id: str
    is_on: bool
    current_power: float
    runtime_today: timedelta
    energy_today: float  # kWh
    last_state_change: datetime | None
    activations_today: int = 0


@dataclass(frozen=True)
class ControlDecision:
    """Output of the optimizer for a single appliance."""
    appliance_id: str
    action: Action
    reason: str
    overrides_plan: bool
    bypasses_cooldown: bool = False


@dataclass
class OptimizerResult:
    """Complete output of the optimizer for a single cycle."""
    decisions: list[ControlDecision]
