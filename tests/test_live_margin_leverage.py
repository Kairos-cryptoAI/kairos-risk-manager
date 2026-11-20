from decimal import Decimal

import pytest

from tests.test_live_risk import FixtureVerifier, _allocation, _evaluate, _evaluator, _limits


@pytest.mark.parametrize(
    ("available", "reserved", "expected"),
    [(500.0, 0.0, 1), (250.0, 50.0, 3), (200.0, 100.0, 3)],
)
def test_margin_leverage_is_the_minimum_for_the_fully_bounded_position(available, reserved, expected):
    risk = _evaluator(
        verifier=FixtureVerifier(
            account_overrides={"available_balance_usd": available}, reserved_open_notional_usd=reserved
        ),
        limits=_limits(max_leverage=30.0),
    )
    decision = _evaluate(risk, adaptive_leverage_ceiling=3).decision
    ceiling_decision = _evaluate(risk, leverage=3.0).decision
    assert decision.approved and decision.leverage == expected
    assert decision.quantity == ceiling_decision.quantity
    assert decision.worst_case_loss_usd == ceiling_decision.worst_case_loss_usd <= decision.loss_budget_usd
    usable = Decimal(str(available - reserved))
    notional = Decimal(str(decision.notional_usd))
    assert usable * (expected - 1) < notional <= usable * expected


@pytest.mark.parametrize("reserved", [50.0, 60.0])
def test_no_available_margin_after_reservation_refuses_entry(reserved):
    risk = _evaluator(
        verifier=FixtureVerifier(
            account_overrides={"available_balance_usd": 50.0}, reserved_open_notional_usd=reserved
        ),
        limits=_limits(max_leverage=30.0),
    )
    decision = _evaluate(risk, adaptive_leverage_ceiling=3).decision
    assert not decision.approved and "position_capacity_exhausted" in decision.rejection_reasons
    assert decision.quantity == 0


def test_macro_reserve_and_strategy_budget_can_make_one_times_sufficient():
    risk = _evaluator(
        verifier=FixtureVerifier(account_overrides={"available_balance_usd": 60.0}),
        limits=_limits(max_leverage=30.0),
    )
    allocation = _allocation(stable_reserve_pct=0.995, strategy_weights={"paper-test-strategy": 0.005})
    decision = _evaluate(risk, allocation=allocation, adaptive_leverage_ceiling=30).decision
    assert decision.approved and decision.leverage == 1
    assert 0 < decision.notional_usd <= 50


def test_stop_limits_the_integer_margin_leverage_at_the_worst_entry():
    risk = _evaluator(
        verifier=FixtureVerifier(account_overrides={"available_balance_usd": 10.0}),
        limits=_limits(max_leverage=30.0),
    )
    decision = _evaluate(risk, adaptive_leverage_ceiling=30).decision
    assert decision.approved and decision.leverage == 9
    assert decision.notional_usd <= 90 and decision.worst_case_loss_usd <= decision.loss_budget_usd


def test_ceiling_cannot_bypass_account_limits_or_mix_with_a_fixed_leverage():
    decision = _evaluate(adaptive_leverage_ceiling=30).decision
    assert not decision.approved and "leverage_outside_constitution" in decision.rejection_reasons
    with pytest.raises(ValueError, match="no fixed leverage"):
        _evaluate(leverage=2, adaptive_leverage_ceiling=2)
