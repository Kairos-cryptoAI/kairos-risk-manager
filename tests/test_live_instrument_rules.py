"""Venue-aware LIVE sizing uses synthetic authenticated-boundary fixtures only."""

from decimal import Decimal

import pytest
from kairos_core.contracts import ExitPlanV1
from kairos_core.enums import EvedexProfile, MarketRegime, Side

from kairos_risk.live import LiveRiskEvaluator, VerifiedLiveInstrumentRulesV1

from .test_live_risk import (
    NOW,
    SHA_B,
    SHA_C,
    SOURCE_ID,
    FixtureInputsVerifier,
    FixtureVerifier,
    _allocation,
    _evaluate,
    _intent,
    _limits,
    _review,
    _scope,
)


def _rules(**overrides):
    values = {
        "source_id": "fixture-upstream-inputs-v1",
        "scope_sha256": _scope().scope_sha256,
        "evidence_sha256": SHA_C,
        "venue_symbol": "BTCUSD",
        "profile": EvedexProfile.PROD,
        "observed_at_ms": NOW - 1_000,
        # A rule-change date is not the freshness clock of its capture.
        "rules_updated_at_ms": NOW - 86_400_000,
        "price_increment": Decimal("0.01"),
        "quantity_increment": Decimal("0.1"),
        "min_price": Decimal("1"),
        "max_price": Decimal("1000000"),
        "min_quantity": Decimal("0.1"),
        "max_quantity": Decimal("1000"),
        "min_notional_usd": Decimal("5"),
        "lot_size": Decimal("1"),
        "multiplier": Decimal("1"),
    }
    values.update(overrides)
    return VerifiedLiveInstrumentRulesV1(**values)


class InstrumentInputsVerifier(FixtureInputsVerifier):
    def __init__(self, rules):
        self.rules = rules

    def verify(self, raw_proof, **kwargs):
        verified = super().verify(raw_proof, **kwargs)
        return verified.model_copy(update={"instrument_rules": self.rules})


def _instrument_evaluator(rules=None, *, limits=None, verifier=None, required=True):
    limits = limits or _limits()
    return LiveRiskEvaluator(
        _scope(limits),
        limits,
        account_verifiers={SOURCE_ID: verifier or FixtureVerifier()},
        input_verifier=InstrumentInputsVerifier(rules if rules is not None else _rules()),
        require_instrument_rules=required,
    )


@pytest.mark.parametrize("step", ["0.1", "0.3", "1", "0.00000003"])
def test_actual_quantity_increment_is_applied_downward_before_core_decision(step):
    baseline = _evaluate().decision
    rules = _rules(quantity_increment=Decimal(step))
    result = _evaluate(_instrument_evaluator(rules))
    decision = result.decision
    assert decision.approved
    assert Decimal(str(decision.quantity)) % Decimal(step) == 0
    assert 0 < decision.quantity <= baseline.quantity
    assert decision.loss_budget_usd == baseline.loss_budget_usd
    assert decision.worst_case_loss_usd <= baseline.worst_case_loss_usd
    assert decision.notional_usd <= baseline.notional_usd
    assert result.instrument_rules == rules
    assert decision.intent == baseline.intent
    assert decision.exit_plan == baseline.exit_plan


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [({"max_quantity": Decimal("2.35")}, 2.3), ({"max_notional_usd": Decimal("120")}, 1.1)],
)
def test_instrument_maxima_add_only_downward_caps(overrides, expected):
    decision = _evaluate(_instrument_evaluator(_rules(**overrides))).decision
    assert decision.approved
    assert decision.quantity == expected
    assert decision.worst_case_loss_usd <= decision.loss_budget_usd <= 25


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"min_quantity": Decimal("5")}, "position_below_instrument_minimum_quantity"),
        ({"min_notional_usd": Decimal("500")}, "position_below_instrument_minimum_notional"),
    ],
)
def test_instrument_minima_reject_without_upsizing(overrides, reason):
    decision = _evaluate(_instrument_evaluator(_rules(**overrides))).decision
    assert not decision.approved
    assert reason in decision.rejection_reasons
    assert decision.quantity == decision.notional_usd == decision.worst_case_loss_usd == 0


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_actual_entry_and_immutable_intent_are_unchanged_for_both_sides(side):
    intent = _intent(
        side=side,
        exit_plan=ExitPlanV1(
            stop_price=95.0 if side is Side.LONG else 105.0,
            target_price=105.0 if side is Side.LONG else 95.0,
            max_holding_ms=180_000,
        ),
    )
    original = intent.model_dump(mode="json")
    decision = _evaluate(
        _instrument_evaluator(_rules()),
        review=_review(intent=intent),
        allocation=_allocation(regime=MarketRegime.BULL if side is Side.LONG else MarketRegime.BEAR),
    ).decision
    assert decision.approved
    assert decision.worst_entry_price == (100.01 if side is Side.LONG else 99.99)
    assert decision.intent.model_dump(mode="json") == original
    assert decision.exit_plan == intent.exit_plan
    assert Decimal(str(decision.quantity)) % Decimal("0.1") == 0
    assert decision.worst_case_loss_usd <= decision.loss_budget_usd <= 25
    actual_stop_loss = Decimal(str(decision.quantity)) * abs(
        Decimal(str(decision.worst_entry_price)) - Decimal(str(intent.exit_plan.stop_price))
    )
    assert Decimal(str(decision.worst_case_loss_usd)) >= (
        actual_stop_loss
        + Decimal(str(decision.estimated_fees_usd))
        + Decimal(str(decision.estimated_slippage_usd))
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_off_grid_book_rejects_without_rewriting_core_bound_executable_entry(side):
    intent = _intent(
        side=side,
        exit_plan=ExitPlanV1(
            stop_price=95.0 if side is Side.LONG else 105.0,
            target_price=105.0 if side is Side.LONG else 95.0,
            max_holding_ms=180_000,
        ),
    )
    decision = _evaluate(
        _instrument_evaluator(_rules(price_increment=Decimal("0.1"))),
        review=_review(intent=intent),
        allocation=_allocation(regime=MarketRegime.BULL if side is Side.LONG else MarketRegime.BEAR),
    ).decision
    assert not decision.approved
    assert "instrument_price_geometry_invalid" in decision.rejection_reasons
    assert decision.worst_entry_price == (100.01 if side is Side.LONG else 99.99)
    assert decision.intent == intent


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_off_grid_immutable_exit_is_rejected_without_rounding(side):
    intent = _intent(
        side=side,
        exit_plan=ExitPlanV1(
            stop_price=95.01 if side is Side.LONG else 105.01,
            target_price=105.0 if side is Side.LONG else 95.0,
            max_holding_ms=180_000,
        ),
    )
    decision = _evaluate(
        _instrument_evaluator(_rules(price_increment=Decimal("0.1"))),
        review=_review(intent=intent),
        allocation=_allocation(regime=MarketRegime.BULL if side is Side.LONG else MarketRegime.BEAR),
    ).decision
    assert not decision.approved
    assert "instrument_price_geometry_invalid" in decision.rejection_reasons
    assert decision.exit_plan == intent.exit_plan


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_id": "foreign-input-source"},
        {"scope_sha256": SHA_B},
        {"evidence_sha256": SHA_B},
        {"venue_symbol": "ETHUSD"},
        {"observed_at_ms": NOW - 5_001},
        {"observed_at_ms": NOW + 1},
        {"observed_at_ms": NOW - 499},
    ],
)
def test_rules_bind_original_fresh_capture_source_scope_and_symbol(overrides):
    result = _evaluate(_instrument_evaluator(_rules(**overrides)))
    assert not result.decision.approved
    assert "input_source_verification_failed" in result.decision.rejection_reasons
    assert result.instrument_rules is None
    assert result.inputs_observed_at_ms is None


def test_required_mode_refuses_missing_rules_while_legacy_mode_keeps_existing_behavior():
    limits = _limits()
    kwargs = {
        "account_verifiers": {SOURCE_ID: FixtureVerifier()},
        "input_verifier": FixtureInputsVerifier(),
    }
    required = LiveRiskEvaluator(_scope(limits), limits, require_instrument_rules=True, **kwargs)
    legacy = LiveRiskEvaluator(_scope(limits), limits, **kwargs)
    assert _evaluate(legacy).decision.approved
    result = _evaluate(required)
    assert not result.decision.approved
    assert "instrument_rules_missing" in result.decision.rejection_reasons
    assert result.instrument_rules is None


def test_caller_rules_dto_or_boolean_cannot_replace_registered_input_verification():
    evaluator = _instrument_evaluator()
    for unverified in (_rules(), True, {"instrument_rules": _rules().model_dump(mode="json")}):
        result = _evaluate(evaluator, raw_inputs_evidence=unverified)
        assert not result.decision.approved
        assert "input_source_verification_failed" in result.decision.rejection_reasons
        assert result.instrument_rules is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"lot_size": Decimal("2")},
        {"multiplier": Decimal("10")},
        {"quantity_increment": True},
        {"quantity_increment": Decimal("NaN")},
        {"profile": EvedexProfile.DEV},
        {"rules_updated_at_ms": NOW},
        {"min_price": Decimal("1000000")},
        {"min_quantity": Decimal("1001")},
        {"max_notional_usd": Decimal("4")},
    ],
)
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
def test_malformed_or_unsupported_instrument_rules_fail_closed(overrides):
    # Bypass fixture construction to prove evaluator revalidates verifier output.
    corrupted = _rules().model_copy(update=overrides)
    result = _evaluate(_instrument_evaluator(corrupted))
    assert not result.decision.approved
    assert "input_source_verification_failed" in result.decision.rejection_reasons


@pytest.mark.parametrize("overrides", [{"min_price": Decimal("96")}, {"max_price": Decimal("104")}])
def test_immutable_exit_prices_must_also_satisfy_instrument_price_bounds(overrides):
    decision = _evaluate(_instrument_evaluator(_rules(**overrides))).decision
    assert not decision.approved
    assert "instrument_price_geometry_invalid" in decision.rejection_reasons


def test_optional_legacy_mode_cannot_ignore_bad_rules_returned_by_verifier():
    result = _evaluate(_instrument_evaluator(_rules(observed_at_ms=NOW - 5_001), required=False))
    assert not result.decision.approved
    assert "input_source_verification_failed" in result.decision.rejection_reasons
    assert result.instrument_rules is None


def test_original_instrument_capture_at_exact_five_second_boundary_is_allowed():
    decision = _evaluate(_instrument_evaluator(_rules(observed_at_ms=NOW - 5_000))).decision
    assert decision.approved


def test_lot_rules_do_not_relax_account_reservations_or_dollar_caps():
    limits = _limits(per_trade_dollar_cap_usd=10.0, max_position_notional_usd=100.0)
    verifier = FixtureVerifier(
        aggregate_open_risk_usd=70.0, reserved_open_risk_usd=10.0, daily_adverse_loss_usd=900.0
    )
    decision = _evaluate(
        _instrument_evaluator(
            _rules(scope_sha256=_scope(limits).scope_sha256), limits=limits, verifier=verifier
        )
    ).decision
    assert decision.approved
    assert decision.quantity == 0.9
    assert decision.loss_budget_usd == 10
    assert decision.worst_case_loss_usd <= 10
    assert decision.notional_usd <= 100


def test_unrepresentable_lot_is_rejected_instead_of_returning_nextafter_off_grid():
    rules = _rules(quantity_increment=Decimal("0.1234567890123456789"))
    decision = _evaluate(_instrument_evaluator(rules)).decision
    assert not decision.approved
    assert "sizing_numeric_invalid" in decision.rejection_reasons
    assert decision.quantity == 0
