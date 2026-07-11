"""Risk limits (env prefix ``KAIROS_``)."""
from __future__ import annotations

from kairos_core.config import CoreSettings


class RiskSettings(CoreSettings):
    service_name: str = "kairos-risk-manager"

    # Leverage: anything above the hard limit is treated as a model error and
    # forcibly reduced to the safe cap (spec example 1: >10x -> 5x).
    hard_leverage_limit: float = 10.0
    safe_leverage_cap: float = 5.0
    max_allowed_leverage: float = 10.0

    # Drawdown: above this daily loss, all *new* risk is refused (spec example 2).
    max_daily_drawdown_pct: float = 3.0

    # Position sizing.
    per_trade_risk_fraction: float = 0.02   # fraction of equity risked per entry
    max_position_notional_usd: float = 250_000.0
    min_notional_usd: float = 5.0           # EVEDEX minimum notional
    require_reconciled_account: bool = True
    require_strategic_allocation: bool = True
    strategic_allocation_max_age_s: float = 26 * 60 * 60

    # Circuit breaker.
    breaker_max_consecutive_failures: int = 2   # trips when exceeded (i.e. on the 3rd)
    breaker_cooldown_s: float = 300.0           # 5 minutes in LOCAL_QUANT_MODE
