"""Deterministic enforcement of Macro Strategist capital allocation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from kairos_core.contracts import StrategicAllocation
from kairos_core.enums import MarketRegime, ReasonCode


@dataclass(frozen=True, slots=True)
class StrategicLimits:
    allowed: bool
    max_leverage: float
    available_equity_usd: float
    remaining_gross_notional_usd: float
    notes: tuple[str, ...] = ()


def is_fresh(allocation: StrategicAllocation, *, max_age_s: float, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    produced = allocation.produced_at
    if produced.tzinfo is None:
        produced = produced.replace(tzinfo=timezone.utc)
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
    allowed = True
    if allocation.regime is MarketRegime.BEAR and reason is ReasonCode.ENTER_LONG_TREND:
        allowed = False
        notes.append("strategic regime BEAR forbids new long trend entries")
    elif allocation.regime is MarketRegime.BULL and reason is ReasonCode.ENTER_SHORT_TREND:
        allowed = False
        notes.append("strategic regime BULL forbids new short trend entries")
    elif allocation.regime is MarketRegime.CHOP and reason in {
        ReasonCode.ENTER_LONG_TREND, ReasonCode.ENTER_SHORT_TREND,
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
