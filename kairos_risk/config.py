"""Risk limits (env prefix ``KAIROS_``)."""

from __future__ import annotations

from kairos_core.config import CoreSettings
from pydantic import Field, model_validator


class RiskSettings(CoreSettings):
    service_name: str = "kairos-risk-manager"

    # Leverage: anything above the hard limit is treated as a model error and
    # forcibly reduced to the safe cap (spec example 1: >10x -> 5x).
    hard_leverage_limit: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    safe_leverage_cap: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    max_allowed_leverage: float = Field(default=10.0, gt=0, allow_inf_nan=False)

    # Drawdown: above this daily loss, all *new* risk is refused (spec example 2).
    max_daily_drawdown_pct: float = Field(default=3.0, gt=0, le=100, allow_inf_nan=False)

    # Position sizing.
    # This is an equity allocation budget, amplified by approved leverage. It is
    # not stop-loss/VAR risk because TacticalCommand currently carries no stop.
    per_trade_risk_fraction: float = Field(default=0.02, gt=0, le=1, allow_inf_nan=False)
    max_position_notional_usd: float = Field(default=250_000.0, gt=0, allow_inf_nan=False)
    min_notional_usd: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    require_reconciled_account: bool = True
    account_snapshot_max_age_s: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    require_strategic_allocation: bool = True
    strategic_allocation_max_age_s: float = Field(default=26 * 60 * 60, gt=0, allow_inf_nan=False)

    # Circuit breaker.
    breaker_max_consecutive_failures: int = Field(default=2, ge=0)
    breaker_cooldown_s: float = Field(default=300.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_limit_ordering(self) -> RiskSettings:
        if not self.safe_leverage_cap <= self.max_allowed_leverage <= self.hard_leverage_limit:
            raise ValueError(
                "leverage limits must satisfy safe_leverage_cap <= "
                "max_allowed_leverage <= hard_leverage_limit"
            )
        if self.min_notional_usd > self.max_position_notional_usd:
            raise ValueError("min_notional_usd cannot exceed max_position_notional_usd")
        return self
