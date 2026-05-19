"""Compose the user-visible status string and attributes for an appliance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import Action
from .models import (
    ApplianceConfig,
    ApplianceState,
    ControlDecision,
)

HA_STATE_MAX_LENGTH = 255


@dataclass(frozen=True)
class FormattedStatus:
    """Composed status for a single appliance."""
    text: str
    action: str
    overrides_plan: bool
    cooldown_seconds_remaining: int | None
    switch_deferred: bool
    headroom_watts: float | None
    plan_action: str | None
    plan_window_start: datetime | None
    plan_window_end: datetime | None


def format_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact human-readable string."""
    total = max(0, int(seconds))

    if total < 60:
        return f"{total}s"

    if total < 600:
        mins, secs = divmod(total, 60)
        if secs == 0:
            return f"{mins}min"
        return f"{mins}min {secs}s"

    if total < 3600:
        mins = total // 60
        return f"{mins}min"

    hours, rem = divmod(total, 3600)
    mins = rem // 60
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}min"


def format_status(
    decision: ControlDecision,
    state: ApplianceState,
    config: ApplianceConfig,
    *,
    switch_interval: int,
    now: datetime,
) -> FormattedStatus:
    """Compose the final state string and attributes for an appliance."""
    suffixes: list[str] = []

    cooldown_remaining, switch_deferred = _compute_cooldown(
        decision, state, switch_interval, now
    )
    if switch_deferred:
        suffixes.append(
            f" (switch deferred - "
            f"{format_duration(cooldown_remaining)} cooldown)"
        )

    text = _compose_with_truncation(decision.reason, suffixes)

    return FormattedStatus(
        text=text,
        action=decision.action.value,
        overrides_plan=decision.overrides_plan,
        cooldown_seconds_remaining=cooldown_remaining if switch_deferred else None,
        switch_deferred=switch_deferred,
        headroom_watts=None,
        plan_action=None,
        plan_window_start=None,
        plan_window_end=None,
    )


def _compose_with_truncation(reason: str, suffixes: list[str]) -> str:
    """Compose `reason` + concatenated `suffixes`, capped at 255 chars."""
    suffix_str = "".join(suffixes)
    suffix_len = len(suffix_str)

    if suffix_len > HA_STATE_MAX_LENGTH - 10:
        composed = reason + suffix_str
        if len(composed) <= HA_STATE_MAX_LENGTH:
            return composed
        return composed[: HA_STATE_MAX_LENGTH - 3] + "..."

    reason_budget = HA_STATE_MAX_LENGTH - suffix_len
    if len(reason) <= reason_budget:
        return reason + suffix_str
    return reason[: reason_budget - 3] + "..." + suffix_str


def _compute_cooldown(
    decision: ControlDecision,
    state: ApplianceState,
    switch_interval: int,
    now: datetime,
) -> tuple[int, bool]:
    """Return (seconds_remaining, switch_deferred) for the cooldown decoration."""
    if decision.bypasses_cooldown:
        return (0, False)
    if state.last_state_change is None:
        return (0, False)

    would_switch_on = decision.action == Action.ON and not state.is_on
    would_switch_off = decision.action == Action.OFF and state.is_on
    if not (would_switch_on or would_switch_off):
        return (0, False)

    elapsed = (now - state.last_state_change).total_seconds()
    remaining = int(switch_interval - elapsed)
    if remaining <= 0:
        return (0, False)
    return (remaining, True)
