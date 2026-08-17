"""Minimal view of account state needed for risk decisions."""

from __future__ import annotations

import math

from kairos_core.contracts import AccountSnapshot
from pydantic import BaseModel, Field, model_validator


class AccountState(BaseModel):
    equity_usd: float = Field(..., gt=0, allow_inf_nan=False)
    peak_equity_usd: float = Field(..., gt=0, allow_inf_nan=False)
    daily_pnl_pct: float = Field(default=0.0, allow_inf_nan=False)  # signed; loss is negative
    gross_exposure_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    gross_exposure_known: bool = True
    open_position_qty: float = Field(default=0.0, allow_inf_nan=False)  # signed, command symbol
    open_position_notional_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    open_position_notional_known: bool = False
    reconciled: bool = False

    @model_validator(mode="after")
    def validate_account_invariants(self) -> AccountState:
        if self.peak_equity_usd < self.equity_usd:
            raise ValueError("peak_equity_usd cannot be below current equity_usd")
        if (
            self.gross_exposure_known
            and self.open_position_notional_known
            and self.gross_exposure_usd < self.open_position_notional_usd
        ):
            raise ValueError("gross exposure cannot be below the command-symbol exposure")
        return self

    @classmethod
    def from_snapshot(cls, snapshot: AccountSnapshot, *, symbol: str | None = None) -> AccountState:
        positions = snapshot.positions
        symbols = [position.symbol for position in positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("account snapshot contains duplicate position symbols")

        selected = next((position for position in positions if position.symbol == symbol), None)
        gross_exposure = 0.0
        gross_exposure_known = True
        for position in positions:
            quantity = position.signed_quantity
            price = position.mark_price or position.entry_price
            if not math.isfinite(quantity):
                gross_exposure_known = False
                continue
            if quantity == 0:
                continue
            if price is None or not math.isfinite(price):
                gross_exposure_known = False
                continue
            notional = abs(quantity) * price
            if not math.isfinite(notional):
                gross_exposure_known = False
                continue
            gross_exposure += notional

        selected_qty = selected.signed_quantity if selected else 0.0
        selected_price = (selected.mark_price or selected.entry_price) if selected else None
        selected_notional_known = (
            selected is None
            or selected_qty == 0
            or (math.isfinite(selected_qty) and selected_price is not None and math.isfinite(selected_price))
        )
        selected_notional = (
            abs(selected_qty) * selected_price
            if selected_notional_known and selected_price is not None
            else 0.0
        )
        return cls(
            equity_usd=snapshot.equity_usd,
            peak_equity_usd=snapshot.peak_equity_usd,
            daily_pnl_pct=snapshot.daily_pnl_pct,
            gross_exposure_usd=gross_exposure,
            gross_exposure_known=gross_exposure_known,
            open_position_qty=selected_qty,
            open_position_notional_usd=selected_notional,
            open_position_notional_known=selected_notional_known,
            reconciled=snapshot.reconciled,
        )

    @property
    def daily_drawdown_pct(self) -> float:
        """Conservative entry drawdown from reported daily PnL and peak equity.

        A stale or inconsistent ``daily_pnl_pct`` must not reopen the entry gate
        while equity is already materially below the authoritative peak.
        """
        peak_drawdown = max(0.0, (self.peak_equity_usd - self.equity_usd) / self.peak_equity_usd * 100)
        return max(0.0, -self.daily_pnl_pct, peak_drawdown)
