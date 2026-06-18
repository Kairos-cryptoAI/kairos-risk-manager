"""Deterministic position sizing — never delegated to the LLM."""
from __future__ import annotations


def position_quantity(
    *,
    equity_usd: float,
    price: float,
    leverage: float,
    risk_fraction: float,
    max_notional_usd: float,
) -> float:
    """Risk-based sizing: notional = equity * risk_fraction * leverage, capped."""
    if price <= 0:
        return 0.0
    notional = equity_usd * risk_fraction * leverage
    notional = min(notional, max_notional_usd)
    return max(0.0, notional / price)
