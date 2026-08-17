"""Deterministic enforcement of Macro Strategist capital allocation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from kairos_core.contracts import StrategicAllocation
from kairos_core.enums import MarketRegime, ReasonCode


@dataclass(frozen=True, slots=True)
class StrategicLimits:
    allowed: bool
    max_leverage: float
    available_equity_usd: float
    remaining_gross_notional_usd: float
    notes: tuple[str, ...] = ()


def allocation_error(allocation: StrategicAllocation) -> str | None:
    """Return why a strategic allocation is unsafe to consume, if anything."""
    if not math.isfinite(allocation.stable_reserve_pct) or not 0 <= allocation.stable_reserve_pct <= 1:
        return "strategic stable reserve must be a finite value in [0, 1]"
    if not math.isfinite(allocation.max_gross_leverage) or allocation.max_gross_leverage <= 0:
        return "strategic maximum leverage must be finite and positive"
    weights = tuple(allocation.strategy_weights.values())
    if any(not math.isfinite(weight) or not 0 <= weight <= 1 for weight in weights):
        return "strategic weights must be finite values in [0, 1]"
    if allocation.stable_reserve_pct + sum(weights) > 1.0001:
        return "strategic reserve and weights exceed total capital"
    return None


def is_fresh(allocation: StrategicAllocation, *, max_age_s: float, now: datetime | None = None) -> bool:
    if not math.isfinite(max_age_s) or max_age_s <= 0:
        return False
    current = now or datetime.now(UTC)
    produced = allocation.produced_at
    if current.utcoffset() is None or produced.utcoffset() is None:
        return False
    age_s = (current - produced).total_seconds()
    return 0 <= age_s <= max_age_s


def limits_for(
    allocation: StrategicAllocation,
    *,
    reason: ReasonCode,
    equity_usd: float,
    gross_exposure_usd: float,
) -> StrategicLimits:
    """Convert allocation into hard entry limits; exits are handled before this call."""
    notes: list[str] = []
    if (
        not math.isfinite(equity_usd)
        or equity_usd <= 0
        or not math.isfinite(gross_exposure_usd)
        or gross_exposure_usd < 0
    ):
        return StrategicLimits(
            allowed=False,
            max_leverage=0.0,
            available_equity_usd=0.0,
            remaining_gross_notional_usd=0.0,
            notes=("account inputs for strategic limits are invalid",),
        )
    invalid = allocation_error(allocation)
    if invalid is not None:
        return StrategicLimits(
            allowed=False,
            max_leverage=0.0,
            available_equity_usd=0.0,
            remaining_gross_notional_usd=0.0,
            notes=(invalid,),
        )
    allowed = True
    if allocation.regime is MarketRegime.BEAR and reason is ReasonCode.ENTER_LONG_TREND:
        allowed = False
        notes.append("strategic regime BEAR forbids new long trend entries")
    elif allocation.regime is MarketRegime.BULL and reason is ReasonCode.ENTER_SHORT_TREND:
        allowed = False
        notes.append("strategic regime BULL forbids new short trend entries")
    elif allocation.regime is MarketRegime.CHOP and reason in {
        ReasonCode.ENTER_LONG_TREND,
        ReasonCode.ENTER_SHORT_TREND,
    }:
        allowed = False
        notes.append("strategic regime CHOP forbids new trend entries")

    risk_budget = max(0.0, 1.0 - allocation.stable_reserve_pct)
    available_equity = equity_usd * risk_budget
    gross_cap = equity_usd * allocation.max_gross_leverage * risk_budget
    remaining = max(0.0, gross_cap - gross_exposure_usd)
    if remaining <= 0:
        allowed = False
        notes.append("strategic gross exposure cap exhausted")
    return StrategicLimits(
        allowed=allowed,
        max_leverage=allocation.max_gross_leverage,
        available_equity_usd=available_equity,
        remaining_gross_notional_usd=remaining,
        notes=tuple(notes),
    )
