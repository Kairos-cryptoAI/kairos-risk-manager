"""Minimal view of account state needed for risk decisions."""
from __future__ import annotations

from pydantic import BaseModel, Field


class AccountState(BaseModel):
    equity_usd: float = Field(..., gt=0)
    peak_equity_usd: float = Field(..., gt=0)
    daily_pnl_pct: float = 0.0          # signed; negative means a loss today
    gross_exposure_usd: float = 0.0
    open_position_qty: float = 0.0       # signed net position in base units

    @property
    def daily_drawdown_pct(self) -> float:
        """Positive number describing today's loss as a percentage (0 if in profit)."""
        return max(0.0, -self.daily_pnl_pct)
