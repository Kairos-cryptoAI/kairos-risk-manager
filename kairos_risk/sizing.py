"""Deterministic position sizing — never delegated to the LLM."""

from __future__ import annotations

import math


def position_quantity(
    *,
    equity_usd: float,
    price: float,
    leverage: float,
    risk_fraction: float,
    max_notional_usd: float,
) -> float:
    """Return a fail-closed equity-budget quantity.

    The contract has no stop-loss distance, so ``risk_fraction`` is an equity
    allocation budget rather than a claim about loss-at-stop.
    """
    values = (equity_usd, price, leverage, risk_fraction, max_notional_usd)
    if not all(math.isfinite(value) for value in values):
        return 0.0
    if equity_usd <= 0 or price <= 0 or leverage <= 0 or not 0 < risk_fraction <= 1:
        return 0.0
    if max_notional_usd <= 0:
        return 0.0
    notional = equity_usd * risk_fraction * leverage
    notional = min(notional, max_notional_usd)
    return max(0.0, notional / price)
