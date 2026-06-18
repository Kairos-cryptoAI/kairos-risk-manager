from kairos_risk.pipeline import RiskPipeline
from kairos_risk.account import AccountState
from kairos_core.contracts import TacticalCommand
from kairos_core.enums import ReasonCode, TacticalStatus, Side


def _cmd(reason=ReasonCode.ENTER_LONG_TREND, lev=20.0):
    return TacticalCommand(source="aggregator", symbol="BTCUSD", status=TacticalStatus.STABLE_TREND_ENTRY,
                           reason_code=reason, target_side=Side.LONG, requested_leverage=lev)


def test_drawdown_forces_no_trade():
    p = RiskPipeline()
    acc = AccountState(equity_usd=9600, peak_equity_usd=10000, daily_pnl_pct=-4.0)
    out = p.validate(_cmd(), acc, price=65000)
    assert out.approved is False
    assert out.reason_code is ReasonCode.NO_TRADE
    assert any("drawdown" in a for a in out.adjustments)


def test_entry_caps_leverage_and_sizes_position():
    p = RiskPipeline()
    acc = AccountState(equity_usd=10000, peak_equity_usd=10000, daily_pnl_pct=0.5)
    out = p.validate(_cmd(lev=25.0), acc, price=65000)
    assert out.approved is True
    assert out.intent.leverage == 5.0  # 25x -> capped
    assert out.intent.quantity > 0
    assert any("capped" in a for a in out.adjustments)


def test_hold_is_passed_through_without_order():
    p = RiskPipeline()
    acc = AccountState(equity_usd=10000, peak_equity_usd=10000)
    out = p.validate(_cmd(reason=ReasonCode.HOLD), acc, price=65000)
    assert out.approved is False
    assert out.reason_code is ReasonCode.HOLD
