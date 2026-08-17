from kairos_core.contracts import TacticalCommand
from kairos_core.enums import ReasonCode, Side, SystemMode, TacticalStatus

from kairos_risk.account import AccountState
from kairos_risk.config import RiskSettings
from kairos_risk.pipeline import RiskPipeline


def _cmd(reason=ReasonCode.ENTER_LONG_TREND, lev=20.0):
    return TacticalCommand(
        source="aggregator",
        symbol="BTCUSD",
        status=TacticalStatus.STABLE_TREND_ENTRY,
        reason_code=reason,
        target_side=Side.LONG,
        requested_leverage=lev,
    )


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


def test_close_long_uses_exact_position_quantity_and_reduce_only():
    account = AccountState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        open_position_qty=0.125,
    )
    out = RiskPipeline().validate(_cmd(reason=ReasonCode.CLOSE_POSITION), account, price=65_000)
    assert out.approved is True
    assert out.intent.side.value == "SELL"
    assert out.intent.quantity == 0.125
    assert out.intent.reduce_only is True


def test_close_short_uses_exact_position_quantity_and_reduce_only():
    account = AccountState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        open_position_qty=-0.2,
    )
    out = RiskPipeline().validate(_cmd(reason=ReasonCode.CLOSE_POSITION), account, price=65_000)
    assert out.approved is True
    assert out.intent.side.value == "BUY"
    assert out.intent.quantity == 0.2
    assert out.intent.reduce_only is True


def test_close_without_position_is_refused():
    account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)
    out = RiskPipeline().validate(_cmd(reason=ReasonCode.CLOSE_POSITION), account, price=65_000)
    assert out.approved is False
    assert out.reason_code is ReasonCode.NO_TRADE
    assert any("no open position" in adjustment for adjustment in out.adjustments)


def test_local_quant_mode_blocks_new_risk_but_not_protective_exit():
    pipeline = RiskPipeline()
    account = AccountState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        open_position_qty=0.125,
    )

    entry = pipeline.validate(
        _cmd(),
        account,
        price=65_000,
        system_mode=SystemMode.LOCAL_QUANT_MODE,
    )
    close = pipeline.validate(
        _cmd(reason=ReasonCode.CLOSE_POSITION),
        account,
        price=65_000,
        system_mode=SystemMode.LOCAL_QUANT_MODE,
    )

    assert entry.approved is False
    assert entry.reason_code is ReasonCode.NO_TRADE
    assert any("LOCAL_QUANT_MODE" in adjustment for adjustment in entry.adjustments)
    assert close.approved is True
    assert close.intent.reduce_only is True


def test_narrow_degradation_modes_do_not_blanket_block_entries():
    pipeline = RiskPipeline()
    account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)

    for mode in (SystemMode.TEXT_LOCAL_FILTER, SystemMode.CONFLICT_SAFE):
        result = pipeline.validate(_cmd(lev=2.0), account, price=65_000, system_mode=mode)
        assert result.approved is True


def test_rebalance_uses_explicit_target_side():
    pipeline = RiskPipeline()
    account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)

    long = pipeline.validate(_cmd(reason=ReasonCode.REBALANCE, lev=2.0), account, price=65_000)
    flat_command = _cmd(reason=ReasonCode.REBALANCE, lev=2.0).model_copy(update={"target_side": Side.FLAT})
    flat = pipeline.validate(flat_command, account, price=65_000)

    assert long.approved is True
    assert long.intent.side.value == "BUY"
    assert flat.approved is False
    assert flat.reason_code is ReasonCode.NO_TRADE


def test_entry_reason_must_match_target_side():
    pipeline = RiskPipeline()
    account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)
    inconsistent = _cmd(reason=ReasonCode.ENTER_SHORT_TREND, lev=2.0)

    result = pipeline.validate(inconsistent, account, price=65_000)

    assert result.approved is False
    assert result.reason_code is ReasonCode.NO_TRADE
    assert any("target_side=SHORT" in note for note in result.adjustments)


def test_unknown_gross_exposure_blocks_entry_but_not_exit():
    pipeline = RiskPipeline()
    account = AccountState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        gross_exposure_known=False,
        open_position_qty=0.1,
    )

    entry = pipeline.validate(_cmd(lev=2.0), account, price=65_000)
    close = pipeline.validate(_cmd(reason=ReasonCode.CLOSE_POSITION), account, price=65_000)

    assert entry.approved is False
    assert any("gross exposure" in note for note in entry.adjustments)
    assert close.approved is True
    assert close.intent.reduce_only is True
    assert close.intent.quantity == 0.1


def test_same_direction_addition_uses_remaining_position_capacity():
    pipeline = RiskPipeline(
        RiskSettings(
            max_position_notional_usd=5_000,
            per_trade_risk_fraction=0.1,
        )
    )
    account = AccountState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        gross_exposure_usd=4_800,
        open_position_qty=48,
        open_position_notional_usd=4_800,
        open_position_notional_known=True,
    )

    result = pipeline.validate(_cmd(lev=2.0), account, price=100)

    assert result.approved is True
    assert result.intent.quantity == 2
    assert account.open_position_notional_usd + result.intent.quantity * 100 == 5_000


def test_entry_cannot_implicitly_reverse_an_open_position():
    pipeline = RiskPipeline()
    account = AccountState(
        equity_usd=10_000,
        peak_equity_usd=10_000,
        gross_exposure_usd=1_000,
        open_position_qty=-0.1,
        open_position_notional_usd=1_000,
        open_position_notional_known=True,
    )

    result = pipeline.validate(_cmd(lev=2.0), account, price=10_000)

    assert result.approved is False
    assert any("close it before changing direction" in note for note in result.adjustments)


def test_redelivered_command_produces_stable_decision_and_intent_ids():
    pipeline = RiskPipeline()
    account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)
    command = _cmd(lev=2.0)

    first = pipeline.validate(command, account, price=65_000)
    second = pipeline.validate(command, account, price=65_000)

    assert first.message_id == second.message_id
    assert first.intent.message_id == second.intent.message_id
    assert first.causation_id == command.message_id
    assert first.intent.causation_id == command.message_id
