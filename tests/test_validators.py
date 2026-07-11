from kairos_risk.config import RiskSettings
from kairos_risk.validators import cap_leverage, drawdown_gate, enforce_min_notional
from kairos_risk.account import AccountState
from kairos_core.enums import ReasonCode

S = RiskSettings()


def test_leverage_above_hard_limit_is_capped_to_safe():
    lev, note = cap_leverage(15.0, S)
    assert lev == S.safe_leverage_cap == 5.0
    assert "suspected error" in note


def test_leverage_within_limits_untouched():
    lev, note = cap_leverage(3.0, S)
    assert lev == 3.0 and note is None


def test_drawdown_gate_blocks_entries():
    acc = AccountState(equity_usd=9700, peak_equity_usd=10000, daily_pnl_pct=-3.5)
    note = drawdown_gate(ReasonCode.ENTER_LONG_TREND, acc, S)
    assert note and "NO_TRADE" in note


def test_drawdown_gate_allows_exits():
    acc = AccountState(equity_usd=9700, peak_equity_usd=10000, daily_pnl_pct=-3.5)
    assert drawdown_gate(ReasonCode.CLOSE_POSITION, acc, S) is None


def test_min_notional_zeroes_dust():
    qty, note = enforce_min_notional(0.00001, 1.0, S)
    assert qty == 0.0 and note
