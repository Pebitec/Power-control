"""Analytics tracker for PV Excess Control."""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)


@dataclass
class ApplianceStats:
    """Per-appliance statistics."""

    energy_today_kwh: float = 0.0
    runtime_today: timedelta = field(default_factory=timedelta)
    savings_today: float = 0.0


class AnalyticsTracker:
    """Tracks energy analytics including self-consumption and savings."""

    def __init__(
        self,
        import_price: float = 0.0,
        feed_in_tariff: float = 0.0,
    ) -> None:
        self.import_price = import_price
        self.feed_in_tariff = feed_in_tariff
        self._appliance_stats: dict[str, ApplianceStats] = {}
        self._total_solar_consumed_kwh: float = 0.0
        self._total_solar_produced_kwh: float = 0.0
        self._total_grid_export_kwh: float = 0.0
        self._total_savings: float = 0.0
        self._last_reset: datetime = datetime.now()

    def record_cycle(
        self,
        appliance_id: str,
        power_watts: float,
        duration_seconds: float,
        source: str,
    ) -> None:
        """Record one control cycle for an appliance.

        source: "solar" when running on surplus PV, "grid" otherwise.
        """
        energy_kwh = (power_watts * duration_seconds) / 3_600 / 1_000
        stats = self._appliance_stats.setdefault(appliance_id, ApplianceStats())
        stats.energy_today_kwh += energy_kwh
        stats.runtime_today += timedelta(seconds=duration_seconds)

        if source == "solar":
            savings = energy_kwh * (self.import_price - self.feed_in_tariff)
            self._total_solar_consumed_kwh += energy_kwh
        else:
            savings = 0.0

        if not math.isfinite(savings):
            savings = 0.0

        stats.savings_today += max(savings, 0.0)
        self._total_savings += max(savings, 0.0)

    def record_solar_production(
        self, power_watts: float, duration_seconds: float
    ) -> None:
        """Record total solar production for self-consumption ratio calculation."""
        energy_kwh = (power_watts * duration_seconds) / 3_600 / 1_000
        self._total_solar_produced_kwh += energy_kwh

    def record_grid_export(self, power_watts: float, duration_seconds: float) -> None:
        """Record grid export for tracking."""
        energy_kwh = (power_watts * duration_seconds) / 3_600 / 1_000
        self._total_grid_export_kwh += energy_kwh

    @property
    def self_consumption_ratio(self) -> float:
        """Percentage of solar energy consumed by managed appliances (0-100)."""
        if self._total_solar_produced_kwh <= 0:
            return 0.0
        return min(
            100.0,
            (self._total_solar_consumed_kwh / self._total_solar_produced_kwh) * 100,
        )

    @property
    def savings_today(self) -> float:
        """Total savings accumulated today."""
        return self._total_savings

    @property
    def solar_consumed_kwh(self) -> float:
        """Total solar energy consumed locally today in kWh."""
        return self._total_solar_consumed_kwh

    @property
    def grid_export_kwh(self) -> float:
        """Total energy exported to the grid today in kWh."""
        return self._total_grid_export_kwh

    def get_appliance_stats(self, appliance_id: str) -> ApplianceStats:
        """Return stats for a specific appliance."""
        return self._appliance_stats.get(appliance_id, ApplianceStats())

    def reset_daily(self) -> None:
        """Reset daily counters. Should be called at midnight."""
        self._appliance_stats.clear()
        self._total_solar_consumed_kwh = 0.0
        self._total_solar_produced_kwh = 0.0
        self._total_grid_export_kwh = 0.0
        self._total_savings = 0.0
        self._last_reset = datetime.now()
