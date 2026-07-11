"""Minimal view of account state needed for risk decisions."""
from __future__ import annotations

from pydantic import BaseModel, Field

from kairos_core.contracts import AccountSnapshot


class AccountState(BaseModel):
    equity_usd: float = Field(..., gt=0)
    peak_equity_usd: float = Field(..., gt=0)
    daily_pnl_pct: float = 0.0          # signed; negative means a loss today
    gross_exposure_usd: float = 0.0
    open_position_qty: float = 0.0       # signed position for the command symbol
    reconciled: bool = False

    @classmethod
    def from_snapshot(cls, snapshot: AccountSnapshot, *, symbol: str | None = None) -> "AccountState":
        positions = snapshot.positions
        selected = next((p for p in positions if p.symbol == symbol), None) if symbol else None
        gross_exposure = sum(
            abs(position.signed_quantity) * (position.mark_price or position.entry_price or 0.0)
            for position in positions
        )
        return cls(
            equity_usd=snapshot.equity_usd,
            peak_equity_usd=snapshot.peak_equity_usd,
            daily_pnl_pct=snapshot.daily_pnl_pct,
            gross_exposure_usd=gross_exposure,
            open_position_qty=selected.signed_quantity if selected else 0.0,
            reconciled=snapshot.reconciled,
        )

    @property
    def daily_drawdown_pct(self) -> float:
        """Positive number describing today's loss as a percentage (0 if in profit)."""
        return max(0.0, -self.daily_pnl_pct)
