"""Independent, composable risk filters.

Each filter takes the working values and returns ``(new_value, note | None)``.
They are deliberately tiny so they can be unit-tested in isolation and audited.
"""
from __future__ import annotations

from typing import Optional, Tuple

from kairos_core.enums import ReasonCode

from .account import AccountState
from .config import RiskSettings

ENTRY_CODES = {ReasonCode.ENTER_LONG_TREND, ReasonCode.ENTER_SHORT_TREND, ReasonCode.REBALANCE}


def cap_leverage(requested: float, settings: RiskSettings) -> Tuple[float, Optional[str]]:
    if requested > settings.hard_leverage_limit:
        # Treated as a model error -> forcibly reduced to the safe cap.
        return settings.safe_leverage_cap, (
            f"leverage {requested:g}x exceeds hard limit {settings.hard_leverage_limit:g}x "
            f"-> capped to {settings.safe_leverage_cap:g}x (suspected error)"
        )
    if requested > settings.max_allowed_leverage:
        return settings.max_allowed_leverage, (
            f"leverage {requested:g}x clamped to {settings.max_allowed_leverage:g}x"
        )
    return requested, None


def drawdown_gate(reason_code: ReasonCode, account: AccountState, settings: RiskSettings) -> Optional[str]:
    """Return a veto note if a NEW entry must be refused due to drawdown."""
    if reason_code in ENTRY_CODES and account.daily_drawdown_pct >= settings.max_daily_drawdown_pct:
        return (
            f"daily drawdown {account.daily_drawdown_pct:.2f}% >= "
            f"{settings.max_daily_drawdown_pct:.2f}% -> entry refused, forcing NO_TRADE"
        )
    return None


def enforce_min_notional(qty: float, price: float, settings: RiskSettings) -> Tuple[float, Optional[str]]:
    if qty * price < settings.min_notional_usd:
        return 0.0, f"notional below exchange minimum ${settings.min_notional_usd:g} -> zeroed"
    return qty, None
