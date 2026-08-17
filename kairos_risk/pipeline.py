"""Orchestrates the risk filters into a single ValidatedOrder decision."""

from __future__ import annotations

import math
from uuid import NAMESPACE_URL, uuid5

from kairos_core.contracts import OrderIntent, StrategicAllocation, TacticalCommand, ValidatedOrder
from kairos_core.enums import OrderSide, OrderType, ReasonCode, Side, SystemMode

from .account import AccountState
from .config import RiskSettings
from .sizing import position_quantity
from .strategy import limits_for
from .validators import cap_leverage, drawdown_gate, enforce_min_notional

_SIDE_MAP = {
    ReasonCode.ENTER_LONG_TREND: OrderSide.BUY,
    ReasonCode.ENTER_SHORT_TREND: OrderSide.SELL,
    ReasonCode.CLOSE_POSITION: None,  # decided from current position
}
_NEW_RISK_REASONS = {
    ReasonCode.ENTER_LONG_TREND,
    ReasonCode.ENTER_SHORT_TREND,
    ReasonCode.REBALANCE,
}
_EXPECTED_ENTRY_SIDE = {
    ReasonCode.ENTER_LONG_TREND: Side.LONG,
    ReasonCode.ENTER_SHORT_TREND: Side.SHORT,
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
        system_mode: SystemMode = SystemMode.NORMAL,
    ) -> ValidatedOrder:
        s = self.settings
        adjustments: list[str] = []
        reason = command.reason_code

        if not math.isfinite(price) or price <= 0:
            adjustments.append("finite positive reference price required")
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        # 1) LOCAL_QUANT_MODE permits protective actions only. Less severe modes
        # keep their narrower architecture-specific behavior in upstream layers.
        if system_mode is SystemMode.LOCAL_QUANT_MODE and reason in _NEW_RISK_REASONS:
            adjustments.append("LOCAL_QUANT_MODE blocks new risk")
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        expected_side = _EXPECTED_ENTRY_SIDE.get(reason)
        if expected_side is not None and command.target_side is not expected_side:
            adjustments.append(
                f"{reason.value} requires target_side={expected_side.value}, "
                f"received {command.target_side.value}"
            )
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        if reason in _NEW_RISK_REASONS and not account.gross_exposure_known:
            adjustments.append("authoritative gross exposure is unavailable")
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        # 2) Hard veto: drawdown gate forces NO_TRADE on any new entry.
        veto = drawdown_gate(reason, account, s)
        if veto:
            adjustments.append(veto)
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        # 3) Non-actionable codes are passed through as refusals (no order).
        if reason in {ReasonCode.HOLD, ReasonCode.NO_TRADE, ReasonCode.REDUCE_LEVERAGE}:
            return self._refuse(command, reason, adjustments, account)

        # 4) Strategic allocation applies only to entries. Reduce-only exits must
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

        # 5) Leverage cap: deterministic settings and Macro allocation both apply.
        leverage, note = cap_leverage(command.requested_leverage, s)
        if strategic is not None and leverage > strategic.max_leverage:
            leverage = strategic.max_leverage
            adjustments.append(f"leverage capped by strategic allocation to {leverage:g}x")
        if note:
            adjustments.append(note)

        # 6) Direction + sizing. Exits must close the existing position exactly;
        # entry sizing formulas can otherwise over-close and flip the account.
        side = _SIDE_MAP.get(reason)
        if reason == ReasonCode.CLOSE_POSITION:
            if account.open_position_qty == 0:
                adjustments.append("no open position to close")
                return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)
            side = OrderSide.SELL if account.open_position_qty > 0 else OrderSide.BUY
            qty = abs(account.open_position_qty)
        else:
            if reason == ReasonCode.REBALANCE:
                side = {
                    Side.LONG: OrderSide.BUY,
                    Side.SHORT: OrderSide.SELL,
                }.get(command.target_side)
                if side is None:
                    adjustments.append("rebalance requires LONG or SHORT target_side")
                    return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)
            if account.open_position_qty != 0:
                position_side = Side.LONG if account.open_position_qty > 0 else Side.SHORT
                if command.target_side is not position_side:
                    adjustments.append(
                        "entry target opposes the open position; close it before changing direction"
                    )
                    return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)
                if not account.open_position_notional_known:
                    adjustments.append("command-symbol position notional is unavailable")
                    return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)
            sizing_equity = strategic.available_equity_usd if strategic is not None else account.equity_usd
            max_notional = max(
                0.0,
                s.max_position_notional_usd - account.open_position_notional_usd,
            )
            if strategic is not None:
                max_notional = min(max_notional, strategic.remaining_gross_notional_usd)
            if max_notional <= 0:
                adjustments.append("position or strategic gross-notional cap exhausted")
                return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)
            qty = position_quantity(
                equity_usd=sizing_equity,
                price=price,
                leverage=leverage,
                risk_fraction=s.per_trade_risk_fraction,
                max_notional_usd=max_notional,
            )
        qty, note = enforce_min_notional(qty, price, s)
        if note:
            adjustments.append(note)
        if qty <= 0:
            return self._refuse(command, ReasonCode.NO_TRADE, adjustments, account)

        intent = OrderIntent(
            source=s.service_name,
            message_id=self._derived_id(command, "intent"),
            correlation_id=command.correlation_id or command.message_id,
            causation_id=command.message_id,
            symbol=command.symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=price,
            leverage=leverage,
            reduce_only=(reason == ReasonCode.CLOSE_POSITION),
            reason_code=reason,
        )
        return ValidatedOrder(
            source=s.service_name,
            message_id=self._derived_id(command, "decision"),
            correlation_id=command.correlation_id or command.message_id,
            causation_id=command.message_id,
            intent=intent,
            approved=True,
            reason_code=reason,
            adjustments=adjustments,
            risk_notes=f"equity=${account.equity_usd:,.0f} dd={account.daily_drawdown_pct:.2f}%",
        )

    def refuse(
        self,
        command: TacticalCommand,
        *,
        adjustment: str,
        account: AccountState | None = None,
    ) -> ValidatedOrder:
        """Build a stable, observable refusal for a service-level safety gate."""
        fallback_account = account or AccountState(
            equity_usd=1.0,
            peak_equity_usd=1.0,
            reconciled=False,
        )
        return self._refuse(
            command,
            ReasonCode.NO_TRADE,
            [adjustment],
            fallback_account,
        )

    def _refuse(
        self,
        command: TacticalCommand,
        reason_code: ReasonCode,
        adjustments: list[str],
        account: AccountState,
    ) -> ValidatedOrder:
        s = self.settings
        intent = OrderIntent(
            source=s.service_name,
            message_id=self._derived_id(command, "intent"),
            correlation_id=command.correlation_id or command.message_id,
            causation_id=command.message_id,
            symbol=command.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=1e-9,
            reason_code=reason_code,
        )
        return ValidatedOrder(
            source=s.service_name,
            message_id=self._derived_id(command, "decision"),
            correlation_id=command.correlation_id or command.message_id,
            causation_id=command.message_id,
            intent=intent,
            approved=False,
            reason_code=reason_code,
            adjustments=adjustments,
            risk_notes="refused by risk manager",
        )

    @staticmethod
    def _derived_id(command: TacticalCommand, kind: str) -> str:
        """Stable output identity keeps redelivery idempotent downstream."""
        return str(uuid5(NAMESPACE_URL, f"kairos:risk:{kind}:{command.message_id}"))
