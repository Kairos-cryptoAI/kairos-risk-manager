import math

import pytest
from kairos_core.contracts import TacticalCommand
from kairos_core.enums import ReasonCode, Side, SystemMode, TacticalStatus
from pydantic import ValidationError

from kairos_risk.account import AccountState
from kairos_risk.config import RiskSettings
from kairos_risk.evaluation import RiskCase, evaluate_policy
from kairos_risk.pipeline import RiskPipeline
from kairos_risk.sizing import position_quantity


def _command(reason: ReasonCode = ReasonCode.ENTER_LONG_TREND) -> TacticalCommand:
    return TacticalCommand(
        source="fixture",
        symbol="BTCUSD",
        status=TacticalStatus.STABLE_TREND_ENTRY,
        reason_code=reason,
        target_side=Side.LONG,
        requested_leverage=2,
    )


def test_peak_drawdown_cannot_be_bypassed_by_inconsistent_daily_pnl():
    account = AccountState(
        equity_usd=9_600,
        peak_equity_usd=10_000,
        daily_pnl_pct=0.5,
    )

    result = RiskPipeline().validate(_command(), account, price=65_000)

    assert account.daily_drawdown_pct == pytest.approx(4.0)
    assert result.approved is False
    assert result.reason_code is ReasonCode.NO_TRADE


def test_account_rejects_peak_below_current_equity():
    with pytest.raises(ValidationError, match="peak_equity_usd"):
        AccountState(equity_usd=10_001, peak_equity_usd=10_000)


@pytest.mark.parametrize("price", [0.0, -1.0, math.nan, math.inf])
def test_invalid_reference_price_fails_closed(price: float):
    account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)

    result = RiskPipeline().validate(_command(), account, price=price)

    assert result.approved is False
    assert result.reason_code is ReasonCode.NO_TRADE
    assert any("finite positive" in note for note in result.adjustments)


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0])
def test_invalid_account_exposure_is_rejected(value: float):
    with pytest.raises(ValidationError):
        AccountState(
            equity_usd=10_000,
            peak_equity_usd=10_000,
            gross_exposure_usd=value,
        )


def test_sizing_is_fail_closed_for_non_finite_inputs():
    assert (
        position_quantity(
            equity_usd=10_000,
            price=65_000,
            leverage=math.nan,
            risk_fraction=0.02,
            max_notional_usd=250_000,
        )
        == 0
    )


def test_settings_reject_incoherent_limit_ordering():
    with pytest.raises(ValidationError, match="leverage limits"):
        RiskSettings(safe_leverage_cap=6, max_allowed_leverage=5)
    with pytest.raises(ValidationError, match="min_notional"):
        RiskSettings(min_notional_usd=10, max_position_notional_usd=5)
    with pytest.raises(ValidationError):
        RiskSettings(account_snapshot_max_age_s=math.inf)


def test_policy_matrix_preserves_exit_and_blocks_degraded_new_risk():
    account = AccountState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        gross_exposure_usd=6_500,
        open_position_qty=0.1,
        open_position_notional_usd=6_500,
        open_position_notional_known=True,
    )
    cases = (
        RiskCase(
            "degraded_entry",
            _command(),
            account,
            65_000,
            system_mode=SystemMode.LOCAL_QUANT_MODE,
        ),
        RiskCase(
            "protective_exit",
            _command(ReasonCode.CLOSE_POSITION),
            account,
            65_000,
            system_mode=SystemMode.LOCAL_QUANT_MODE,
        ),
        RiskCase("normal_entry", _command(), account, 65_000),
    )

    result = evaluate_policy(RiskPipeline(), cases)

    assert result.approved == 2
    assert result.refused == 1
    assert result.cases[0].decision.reason_code is ReasonCode.NO_TRADE
    assert result.cases[1].decision.intent.reduce_only is True
    assert result.approved_notional_usd > 0


def test_policy_cases_require_unique_names():
    account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)
    duplicate = RiskCase("same", _command(), account, 65_000)
    with pytest.raises(ValueError, match="uniquely named"):
        evaluate_policy(RiskPipeline(), (duplicate, duplicate))
