"""Optimizer for Solar Power Control. Pure logic engine - no Home Assistant dependencies.

Runs 3 phases per cycle:
  Phase 1:   ASSESS  - Calculate averaged excess, apply hysteresis, check constraints
  Phase 2:   ALLOCATE - Assign excess power to appliances by priority
  Phase 2.5: PREEMPT - Shed lower-priority ON appliances to start higher-priority IDLE ones
  Phase 3:   SHED - Turn off lowest-priority appliances when excess is negative
"""
from __future__ import annotations

import logging
import math
from zoneinfo import ZoneInfo

from custom_components.solar_power_control.const import (
    DEFAULT_OFF_THRESHOLD,
    DEFAULT_ON_THRESHOLD,
)
from custom_components.solar_power_control.models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    ControlDecision,
    OptimizerResult,
    PowerState,
)

_LOGGER = logging.getLogger(__name__)


class Optimizer:
    """Pure-logic optimization engine.

    Takes power state, appliance configs/states as input.
    Returns control decisions as output.
    No side effects, no HA dependencies.
    """

    def __init__(
        self,
        timezone_str: str | None = None,
        enable_preemption: bool = True,
        off_threshold: int = DEFAULT_OFF_THRESHOLD,
        min_good_samples: int = 3,
    ) -> None:
        self._tz = ZoneInfo(timezone_str) if timezone_str else None
        self.enable_preemption = enable_preemption
        self._off_threshold = off_threshold
        self._min_good_samples = min_good_samples

    def optimize(
        self,
        power_state: PowerState,
        appliances: list[ApplianceConfig],
        appliance_states: list[ApplianceState],
        power_history: list[PowerState],
    ) -> OptimizerResult:
        """Run the optimization cycle and return decisions.

        Phase 1:   ASSESS - compute averaged excess, apply hysteresis
        Phase 2:   ALLOCATE - assign excess to appliances by priority
        Phase 2.5: PREEMPT - shed lower-priority ON appliances for higher-priority IDLE ones
        Phase 3:   SHED - turn off lowest priority when over-budget
        """
        state_by_id: dict[str, ApplianceState] = {
            s.appliance_id: s for s in appliance_states
        }

        self._config_by_id: dict[str, ApplianceConfig] = {a.id: a for a in appliances}
        self._state_by_id = state_by_id
        self._pending_dep_decisions: dict[str, ControlDecision] = {}

        self._reverse_deps: dict[str, list[str]] = {}
        for a in appliances:
            if a.requires_appliance and a.requires_appliance in self._config_by_id:
                self._reverse_deps.setdefault(a.requires_appliance, []).append(a.id)

        # Phase 1: ASSESS
        avg_excess = self._calculate_average_excess(power_history)

        _LOGGER.debug(
            "Optimizer start: %d appliances, avg_excess=%s, current_excess=%s",
            len(appliances),
            f"{avg_excess:.0f}W" if avg_excess is not None else "unavailable",
            f"{power_state.excess_power:.0f}W" if power_state.excess_power is not None else "unavailable",
        )

        sorted_appliances = sorted(appliances, key=lambda a: (a.helper_only, a.priority, a.id))

        if avg_excess is None:
            return self._optimize_safety_only(
                state_by_id=state_by_id,
                sorted_appliances=sorted_appliances,
                power_state=power_state,
            )

        self._appliance_avg_excess: dict[str, float] = {}
        controller_interval = 30
        for app in appliances:
            if app.averaging_window is not None and app.averaging_window > 0:
                entries_needed = max(1, int(app.averaging_window / controller_interval))
                recent = power_history[-entries_needed:] if len(power_history) >= entries_needed else power_history
                per_app_avg = self._calculate_average_excess(recent)
                if per_app_avg is not None:
                    self._appliance_avg_excess[app.id] = per_app_avg

        # Phase 2: ALLOCATE
        decisions: list[ControlDecision] = []
        avg_budget: float = avg_excess
        instant_budget: float = (
            power_state.excess_power
            if power_state.excess_power is not None
            else avg_excess
        )

        total_consumed = 0.0
        for appliance in sorted_appliances:
            state = state_by_id.get(appliance.id)
            if state is None:
                decisions.append(ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.IDLE,
                    reason="No state data available",
                    overrides_plan=False,
                ))
                continue

            if appliance.id in self._appliance_avg_excess:
                app_avg_budget = self._appliance_avg_excess[appliance.id] - total_consumed
            else:
                app_avg_budget = avg_budget

            decision, power_consumed = self._allocate_appliance(
                appliance, state, app_avg_budget, instant_budget,
                decisions=decisions,
                state_by_id=state_by_id,
            )
            decisions.append(decision)
            _LOGGER.debug(
                "  Allocate %s (p=%d, %sW): avg=%.0fW inst=%.0fW -> %s (%s)",
                appliance.name, appliance.priority, appliance.nominal_power,
                avg_budget, instant_budget, decision.action, decision.reason,
            )
            avg_budget -= power_consumed
            instant_budget -= power_consumed
            total_consumed += power_consumed

        for dep_id, dep_decision in self._pending_dep_decisions.items():
            for i, d in enumerate(decisions):
                if d.appliance_id == dep_id and d.action == Action.IDLE:
                    decisions[i] = dep_decision
                    break

        # Phase 2.5: PREEMPT
        if self.enable_preemption:
            avg_budget, instant_budget = self._preempt(
                decisions, sorted_appliances, state_by_id,
                avg_budget, instant_budget,
            )

        # Phase 3: SHED
        self._shed(decisions, sorted_appliances, state_by_id, instant_budget)

        return OptimizerResult(decisions=decisions)

    def _calculate_average_excess(self, power_history: list[PowerState]) -> float | None:
        """Calculate the average excess power from the history window."""
        good_samples = [
            ps.excess_power
            for ps in power_history
            if ps.excess_power is not None
            and not math.isnan(ps.excess_power)
            and not math.isinf(ps.excess_power)
        ]
        if len(good_samples) < self._min_good_samples:
            return None
        return sum(good_samples) / len(good_samples)

    def _has_running_dependent(
        self,
        helper_id: str,
        decisions: list[ControlDecision],
        state_by_id: dict[str, ApplianceState],
    ) -> bool:
        """Return True if any appliance with requires_appliance=helper_id is running."""
        dependent_ids = self._reverse_deps.get(helper_id, [])
        if not dependent_ids:
            return False
        decision_by_id = {d.appliance_id: d for d in decisions}
        for dep_id in dependent_ids:
            dec = decision_by_id.get(dep_id)
            if dec is not None and dec.action == Action.ON:
                return True
            if dec is not None and dec.action == Action.OFF:
                continue
            dep_state = state_by_id.get(dep_id)
            if dep_state is not None and dep_state.is_on:
                return True
        return False

    def _apply_safety_rules(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState,
        decisions: list[ControlDecision],
        state_by_id: dict[str, ApplianceState],
    ) -> tuple[ControlDecision, float] | None:
        """Apply excess-independent safety rules to a single appliance."""
        # Manual override check
        if appliance.override_active:
            power_consumed = appliance.nominal_power if not state.is_on else 0.0
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    reason="Manual override active",
                    overrides_plan=True,
                ),
                power_consumed,
            )

        # Helper-only short-circuit
        if appliance.helper_only:
            if self._has_running_dependent(appliance.id, decisions, state_by_id):
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=Action.ON,
                        reason="Helper-only: dependent is running",
                        overrides_plan=False,
                    ),
                    0.0,
                )
            else:
                action = Action.OFF if state.is_on else Action.IDLE
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=action,
                        reason="Helper-only: no dependent running",
                        overrides_plan=False,
                        bypasses_cooldown=True,
                    ),
                    0.0,
                )

        # on_only check
        if appliance.on_only and state.is_on:
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    reason="on_only appliance - staying on",
                    overrides_plan=False,
                ),
                0.0,
            )

        # Dependency availability check
        if appliance.requires_appliance:
            dep_config = self._config_by_id.get(appliance.requires_appliance)
            if dep_config is None:
                action = Action.OFF if state.is_on else Action.IDLE
                return (
                    ControlDecision(
                        appliance_id=appliance.id, action=action,
                        reason=f"Dependency '{appliance.requires_appliance}' unavailable (disabled or removed)",
                        overrides_plan=False,
                    ),
                    0.0,
                )

        return None

    def _optimize_safety_only(
        self,
        state_by_id: dict[str, ApplianceState],
        sorted_appliances: list[ApplianceConfig],
        power_state: PowerState,
    ) -> OptimizerResult:
        """Run safety checks only; skip Phase 2/2.5/3.

        Called when ASSESS returns None (insufficient history).
        """
        decisions: list[ControlDecision] = []
        for appliance in sorted_appliances:
            state = state_by_id.get(appliance.id)
            if state is None:
                decisions.append(ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.IDLE,
                    reason="No state data available",
                    overrides_plan=False,
                ))
                continue
            safety_result = self._apply_safety_rules(
                appliance, state,
                decisions=decisions,
                state_by_id=state_by_id,
            )
            if safety_result is not None:
                decision, _ = safety_result
                decisions.append(decision)

        return OptimizerResult(decisions=decisions)

    def _allocate_appliance(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState,
        avg_budget: float,
        instant_budget: float,
        decisions: list[ControlDecision] | None = None,
        state_by_id: dict[str, ApplianceState] | None = None,
    ) -> tuple[ControlDecision, float]:
        """Determine the desired action for a single appliance."""
        safety_result = self._apply_safety_rules(
            appliance, state,
            decisions=decisions if decisions is not None else [],
            state_by_id=state_by_id if state_by_id is not None else {},
        )
        if safety_result is not None:
            return safety_result

        # Already-ON appliances
        if state.is_on:
            reason = _format_staying_on(
                current_power=state.current_power,
                off_threshold=self._off_threshold,
                instant_budget=instant_budget,
            )
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    reason=reason,
                    overrides_plan=False,
                ),
                0.0,
            )

        # Currently OFF appliances
        return self._allocate_standard(appliance, state, avg_budget)

    def _allocate_standard(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState,
        avg_budget: float,
    ) -> tuple[ControlDecision, float]:
        """Allocate a standard on/off appliance using hysteresis thresholds."""
        dep_power = 0.0
        if appliance.requires_appliance:
            dep_state = self._state_by_id.get(appliance.requires_appliance)
            dep_config = self._config_by_id.get(appliance.requires_appliance)
            if dep_state and not dep_state.is_on and dep_config:
                dep_power = dep_config.nominal_power

        on_buf = appliance.on_threshold if appliance.on_threshold is not None else DEFAULT_ON_THRESHOLD
        threshold = appliance.nominal_power + on_buf

        power_needed = threshold + dep_power
        if avg_budget >= power_needed:
            power_consumed = appliance.nominal_power
            if dep_power > 0:
                self._pending_dep_decisions[appliance.requires_appliance] = ControlDecision(
                    appliance_id=appliance.requires_appliance, action=Action.ON,
                    reason=f"Started as dependency for {appliance.name}",
                    overrides_plan=False,
                )
                power_consumed = power_consumed + dep_power
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    reason=f"Excess available ({avg_budget:.0f}W >= {power_needed:.0f}W needed)",
                    overrides_plan=False,
                ),
                power_consumed,
            )

        return (
            ControlDecision(
                appliance_id=appliance.id,
                action=Action.IDLE,
                reason=f"Insufficient excess ({avg_budget:.0f}W < {power_needed:.0f}W needed)",
                overrides_plan=False,
            ),
            0.0,
        )

    def _preempt(
        self,
        decisions: list[ControlDecision],
        sorted_appliances: list[ApplianceConfig],
        state_by_id: dict[str, ApplianceState],
        avg_budget: float,
        instant_budget: float,
    ) -> tuple[float, float]:
        """Phase 2.5: PREEMPT - shed lower-priority appliances to start higher-priority ones."""
        appliance_by_id: dict[str, ApplianceConfig] = {a.id: a for a in sorted_appliances}
        decision_index: dict[str, int] = {d.appliance_id: i for i, d in enumerate(decisions)}

        idle_candidates: list[tuple[str, ApplianceConfig]] = []
        for decision in decisions:
            if decision.action != Action.IDLE:
                continue
            if "insufficient excess" not in decision.reason.lower():
                continue
            appliance = appliance_by_id.get(decision.appliance_id)
            if appliance is None:
                continue
            idle_candidates.append((decision.appliance_id, appliance))

        idle_candidates.sort(key=lambda item: (item[1].priority, item[0]))

        for idle_id, idle_app in idle_candidates:
            on_buf = idle_app.on_threshold if idle_app.on_threshold is not None else DEFAULT_ON_THRESHOLD
            power_needed = idle_app.nominal_power + on_buf

            dep_power = 0.0
            dep_id: str | None = None
            if idle_app.requires_appliance:
                dep_state = state_by_id.get(idle_app.requires_appliance)
                dep_config = appliance_by_id.get(idle_app.requires_appliance)
                if dep_state and not dep_state.is_on and dep_config:
                    dep_idx = decision_index.get(idle_app.requires_appliance)
                    if dep_idx is not None:
                        dep_decision = decisions[dep_idx]
                        if dep_decision.action != Action.ON:
                            dep_power = dep_config.nominal_power
                            dep_id = idle_app.requires_appliance

            power_needed += dep_power

            preemptable: list[tuple[str, ApplianceConfig, float]] = []
            for decision in decisions:
                if decision.action != Action.ON:
                    continue
                app = appliance_by_id.get(decision.appliance_id)
                if app is None:
                    continue
                if app.priority <= idle_app.priority:
                    continue
                if idle_app.requires_appliance and app.id == idle_app.requires_appliance:
                    continue
                if app.on_only or app.protect_from_preemption or app.override_active:
                    continue
                if "grid supplement" in decision.reason.lower():
                    continue
                if app.id in self._reverse_deps:
                    has_running_dep = any(
                        d.action == Action.ON
                        for d in decisions
                        if d.appliance_id in self._reverse_deps[app.id]
                    )
                    if has_running_dep:
                        continue

                state = state_by_id.get(app.id)
                freed = (
                    state.current_power
                    if state and state.current_power > 0
                    else app.nominal_power
                )
                preemptable.append((app.id, app, freed))

            preemptable.sort(key=lambda item: (-item[1].priority, item[0]))

            total_freed = 0.0
            to_preempt: list[tuple[str, ApplianceConfig, float]] = []
            for p_id, p_app, freed in preemptable:
                to_preempt.append((p_id, p_app, freed))
                total_freed += freed
                if avg_budget + total_freed >= power_needed:
                    break

            if avg_budget + total_freed < power_needed:
                continue

            for p_id, p_app, freed in to_preempt:
                idx = decision_index[p_id]
                decisions[idx] = ControlDecision(
                    appliance_id=p_id,
                    action=Action.OFF,
                    reason=f"Preempted for higher-priority {idle_app.name}",
                    overrides_plan=False,
                )
                avg_budget += freed
                instant_budget += freed

            idle_idx = decision_index[idle_id]
            power_consumed = idle_app.nominal_power
            decisions[idle_idx] = ControlDecision(
                appliance_id=idle_id,
                action=Action.ON,
                reason="Preemption: started after shedding lower-priority appliances",
                overrides_plan=False,
            )
            avg_budget -= power_consumed
            instant_budget -= power_consumed

            if dep_id is not None and dep_id in decision_index:
                dep_idx = decision_index[dep_id]
                decisions[dep_idx] = ControlDecision(
                    appliance_id=dep_id,
                    action=Action.ON,
                    reason=f"Started as dependency for {idle_app.name} (preemption)",
                    overrides_plan=False,
                )
                avg_budget -= dep_power
                instant_budget -= dep_power

        return avg_budget, instant_budget

    def _shed(
        self,
        decisions: list[ControlDecision],
        sorted_appliances: list[ApplianceConfig],
        state_by_id: dict[str, ApplianceState],
        instant_budget: float,
    ) -> float:
        """Phase 3: SHED - turn off lowest-priority appliances first."""
        if instant_budget >= self._off_threshold:
            return instant_budget

        appliance_by_id: dict[str, ApplianceConfig] = {a.id: a for a in sorted_appliances}
        decision_index: dict[str, int] = {d.appliance_id: i for i, d in enumerate(decisions)}

        candidates: list[tuple[str, ApplianceConfig]] = []
        for decision in decisions:
            if decision.action != Action.ON:
                continue
            appliance = appliance_by_id.get(decision.appliance_id)
            if appliance is None:
                continue
            if appliance.on_only or appliance.override_active or decision.bypasses_cooldown:
                continue
            if "grid supplement" in decision.reason.lower():
                continue
            if appliance.id in self._reverse_deps:
                has_running_dep = any(
                    d.action == Action.ON
                    for d in decisions
                    if d.appliance_id in self._reverse_deps[appliance.id]
                )
                if has_running_dep:
                    continue
            candidates.append((decision.appliance_id, appliance))

        candidates.sort(key=lambda item: (-item[1].priority, item[0]))

        for app_id, appliance in candidates:
            if instant_budget >= self._off_threshold:
                break

            idx = decision_index[app_id]
            state = state_by_id.get(app_id)
            freed_power = (state.current_power if state and state.current_power > 0
                           else appliance.nominal_power)
            decisions[idx] = ControlDecision(
                appliance_id=app_id,
                action=Action.OFF,
                reason=f"Shed: insufficient excess (priority {appliance.priority})",
                overrides_plan=False,
            )
            instant_budget += freed_power
            _LOGGER.debug("  Shed %s: freed %.0fW, inst=%.0fW", appliance.name, freed_power, instant_budget)

        return instant_budget


def _format_staying_on(
    *,
    current_power: float,
    off_threshold: float,
    instant_budget: float,
) -> str:
    threshold_sign = "-" if off_threshold < 0 else "+"
    remaining_sign = "-" if instant_budget < 0 else "+"
    text = (
        f"Staying on ({current_power:.0f}W drawn) - "
        f"shed at {threshold_sign}{abs(off_threshold):.0f}W "
        f"(current: {remaining_sign}{abs(instant_budget):.0f}W)"
    )
    if instant_budget < off_threshold:
        text += " (shed imminent)"
    return text
