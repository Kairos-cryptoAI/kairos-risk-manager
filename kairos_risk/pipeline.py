"""Orchestrates the risk filters into a single ValidatedOrder decision."""
from __future__ import annotations

from kairos_core.contracts import OrderIntent, StrategicAllocation, TacticalCommand, ValidatedOrder
from kairos_core.enums import OrderSide, OrderType, ReasonCode

from .account import AccountState
from .config import RiskSettings
from .sizing import position_quantity
from .strategy import limits_for
from .validators import drawdown_gate, cap_leverage, enforce_min_notional

_SIDE_MAP = {
    ReasonCode.ENTER_LONG_TREND: OrderSide.BUY,
    ReasonCode.ENTER_SHORT_TREND: OrderSide.SELL,
    ReasonCode.CLOSE_POSITION: None,  # decided from current position
}


class RiskPipeline:
    def __init__(self, settings: RiskSettings | None = None) -> None:
        self.settings = settings or RiskSettings()

    def validate(
        self,
        command: TacticalCommand,
        account: AccountState,
        *,
        price: float,
        allocation: StrategicAllocation | None = None,
    ) -> ValidatedOrder:
        s = self.settings
        adjustments: list[str] = []
        reason = command.reason_code

        # 1) Hard veto: drawdown gate forces NO_TRADE on any new entry.
        veto = drawdown_gate(reason, account, s)
        if veto:
            adjustments.append(veto)
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        # 2) Non-actionable codes are passed through as refusals (no order).
        if reason in {ReasonCode.HOLD, ReasonCode.NO_TRADE, ReasonCode.REDUCE_LEVERAGE}:
            return self._refuse(command, reason, adjustments, account)

        # 3) Strategic allocation applies only to entries. Reduce-only exits must
        # remain available even when allocation is absent, stale or defensive.
        strategic = None
        if allocation is not None and reason != ReasonCode.CLOSE_POSITION:
            strategic = limits_for(
                allocation,
                reason=reason,
                equity_usd=account.equity_usd,
                gross_exposure_usd=account.gross_exposure_usd,
            )
            adjustments.extend(strategic.notes)
            if not strategic.allowed:
                return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        # 4) Leverage cap: deterministic settings and Macro allocation both apply.
        leverage, note = cap_leverage(command.requested_leverage, s)
        if strategic is not None and leverage > strategic.max_leverage:
            leverage = strategic.max_leverage
            adjustments.append(f"leverage capped by strategic allocation to {leverage:g}x")
        if note:
            adjustments.append(note)

        # 5) Direction + sizing. Exits must close the existing position exactly;
        # entry sizing formulas can otherwise over-close and flip the account.
        side = _SIDE_MAP.get(reason)
        if reason == ReasonCode.CLOSE_POSITION:
            if account.open_position_qty == 0:
                adjustments.append("no open position to close")
                return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)
            side = OrderSide.SELL if account.open_position_qty > 0 else OrderSide.BUY
            qty = abs(account.open_position_qty)
        else:
            sizing_equity = strategic.available_equity_usd if strategic is not None else account.equity_usd
            max_notional = s.max_position_notional_usd
            if strategic is not None:
                max_notional = min(max_notional, strategic.remaining_gross_notional_usd)
            qty = position_quantity(
                equity_usd=sizing_equity, price=price, leverage=leverage,
                risk_fraction=s.per_trade_risk_fraction, max_notional_usd=max_notional,
            )
        qty, note = enforce_min_notional(qty, price, s)
        if note:
            adjustments.append(note)
        if qty <= 0:
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        intent = OrderIntent(
            source=s.service_name, symbol=command.symbol, side=side,
            order_type=OrderType.LIMIT, quantity=qty, price=price,
            leverage=leverage, reduce_only=(reason == ReasonCode.CLOSE_POSITION),
            reason_code=reason,
        )
        return ValidatedOrder(
            source=s.service_name, intent=intent, approved=True, reason_code=reason,
            adjustments=adjustments,
            risk_notes=f"equity=${account.equity_usd:,.0f} dd={account.daily_drawdown_pct:.2f}%",
        )

    def _refuse(self, command, reason_code, adjustments, account) -> ValidatedOrder:
        s = self.settings
        intent = OrderIntent(
            source=s.service_name, symbol=command.symbol, side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=1e-9, reason_code=reason_code,
        )
        return ValidatedOrder(
            source=s.service_name, intent=intent, approved=False, reason_code=reason_code,
            adjustments=adjustments, risk_notes="refused by risk manager",
        )
