"""LIVE evaluator checks use only synthetic verifier fixtures, never real sources."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from kairos_core.contracts import (
    AccountSnapshotV2,
    CandidateReviewV1,
    CandidateRouteV1,
    ExitPlanV1,
    StrategicAllocation,
    StrategyIntentV1,
    StrategyProvenanceV1,
    VenueQualityV1,
    canonical_sha256,
)
from kairos_core.enums import (
    CandidateReviewTier,
    EvedexProfile,
    MarketRegime,
    ReasoningEffort,
    ReviewDecision,
    Side,
    StrategicTrigger,
    SystemMode,
    TradingMode,
)

from kairos_risk.live import (
    LiveRiskEvaluator,
    LiveRiskLimitsV1,
    LiveRiskScopeV1,
    VerifiedLiveAccountV1,
    VerifiedLiveInputsV1,
)

SOURCE_ID = "fixture-live-account-v1"
T0 = 1_800_000_000_000
NOW = T0 + 60_400
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
ACCOUNT_ID = "fixture-live-account"


def _scope(limits: LiveRiskLimitsV1 | None = None) -> LiveRiskScopeV1:
    limits = limits or _limits()
    return LiveRiskScopeV1(
        context_sha256=SHA_A,
        source_set_sha256=SHA_B,
        policy_sha256=SHA_C,
        risk_limits_sha256=canonical_sha256(limits.model_dump(mode="json")),
        account_id=ACCOUNT_ID,
        strategy_id="paper-test-strategy",
        strategy_revision="1",
        approved_prod_symbols=(("BTCUSDT", "BTCUSD:PROD"),),
    )


def _intent(**overrides: object) -> StrategyIntentV1:
    values: dict[str, object] = {
        "source": "strategy-fixture",
        "strategy_id": "paper-test-strategy",
        "strategy_revision": "1",
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "decision_ts_ms": T0 + 59_999,
        "entry_eligible_ts_ms": T0 + 60_000,
        "entry_expires_ts_ms": T0 + 120_000,
        "reference_price": 100.0,
        "signal_strength": 0.7,
        "gross_reward_bps": 500.0,
        "exit_plan": ExitPlanV1(stop_price=95.0, target_price=105.0, max_holding_ms=180_000),
        "provenance": StrategyProvenanceV1(
            strategy_code_sha256=SHA_A,
            config_sha256=SHA_B,
            input_window_sha256=SHA_C,
            features_sha256="d" * 64,
            input_bar_sha256s=(SHA_A, SHA_B),
        ),
    }
    values.update(overrides)
    return StrategyIntentV1(**values)


def _review(*, decision=ReviewDecision.ALLOW, intent=None) -> CandidateReviewV1:
    intent = intent or _intent()
    route = CandidateRouteV1(
        source="router-fixture",
        intent=intent,
        review_tier=CandidateReviewTier.NORMAL,
        requested_reasoning_effort=ReasoningEffort.MEDIUM,
        routed_at_ms=T0 + 60_000,
        review_deadline_ms=T0 + 119_000,
    )
    return CandidateReviewV1(
        source="aggregator-fixture",
        route=route,
        intent=intent,
        decision=decision,
        reviewed_at_ms=T0 + 60_100,
        reviewer="DETERMINISTIC",
        reason_codes=(decision.value,),
    )


def _limits(**overrides: object) -> LiveRiskLimitsV1:
    values: dict[str, object] = {
        "per_trade_risk_fraction": 0.0025,
        "aggregate_open_risk_fraction": 0.01,
        "max_leverage": 1.0,
        "per_trade_dollar_cap_usd": 500.0,
        "aggregate_dollar_cap_usd": 3_000.0,
        "daily_loss_cap_usd": 1_000.0,
        "max_account_equity_usd": 20_000.0,
        "max_position_notional_usd": 2_000.0,
        "min_notional_usd": 5.0,
    }
    values.update(overrides)
    return LiveRiskLimitsV1(**values)


def _live_account(**overrides: object) -> AccountSnapshotV2:
    values: dict[str, object] = {
        "source": "execution-fixture",
        "trading_mode": TradingMode.LIVE,
        "evedex_profile": EvedexProfile.PROD,
        "account_id": ACCOUNT_ID,
        "equity_usd": 10_000.0,
        "available_balance_usd": 9_000.0,
        "margin_used_usd": 0.0,
        "durable_day_start_equity_usd": 10_000.0,
        "durable_peak_equity_usd": 10_000.0,
        "total_open_risk_usd": 0.0,
        "captured_at_ms": NOW - 1_000,
        "reconciliation_seq": 1,
        "reconciled": True,
        "reconciliation_detail": "synthetic test fixture only",
    }
    values.update(overrides)
    return AccountSnapshotV2(**values)


def _prod_venue(**overrides: object) -> VenueQualityV1:
    best_bid, best_ask = 99.99, 100.01
    mid = (best_bid + best_ask) / 2
    values: dict[str, object] = {
        "source": "venue-fixture",
        "profile": EvedexProfile.PROD,
        "symbol": "BTCUSD:PROD",
        "observed_at_ms": NOW - 500,
        "expires_at_ms": NOW + 1_500,
        "reference_timestamp_ms": NOW - 550,
        "book_timestamp_ms": NOW - 550,
        "reference_mid_price": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "venue_mid_price": mid,
        "basis_bps": 0.0,
        "spread_bps": (best_ask - best_bid) / mid * 10_000,
        "assessed_notional_usd": 1_000.0,
        "depth_usd": 5_000.0,
        "buy_slippage_bps": 1.0,
        "sell_slippage_bps": 1.2,
        "taker_fee_bps": 5.0,
        "reference_age_ms": 50,
        "book_age_ms": 50,
        "latency_ms": 20,
        "timestamp_skew_ms": 0,
        "entry_allowed": True,
    }
    values.update(overrides)
    return VenueQualityV1(**values)


def _allocation(**overrides: object) -> StrategicAllocation:
    values: dict[str, object] = {
        "source": "macro-strategist-fixture",
        "regime": MarketRegime.BULL,
        "stable_reserve_pct": 0.2,
        "strategy_weights": {"paper-test-strategy": 0.8},
        "max_gross_leverage": 1.0,
        "triggered_by": StrategicTrigger.SCHEDULE,
        "produced_at": datetime.fromtimestamp((NOW - 20_000) / 1_000, tz=UTC),
    }
    values.update(overrides)
    return StrategicAllocation(**values)


class FixtureVerifier:
    """Test-only trusted registry entry; evidence is synthetic and unauthenticated."""

    def __init__(self, *, account_overrides=None, **metrics: float) -> None:
        self.metrics = metrics
        self.account_overrides = account_overrides or {}

    def verify(self, raw_evidence, *, source_id, expected_scope):
        assert raw_evidence == "fixture-proof"
        account = _live_account(**self.account_overrides)
        facts: dict[str, float] = {
            "aggregate_open_risk_usd": 0.0,
            "aggregate_open_notional_usd": 0.0,
            "reserved_open_risk_usd": 0.0,
            "reserved_open_notional_usd": 0.0,
            "strategy_open_notional_usd": 0.0,
            "strategy_reserved_notional_usd": 0.0,
            "daily_adverse_loss_usd": 0.0,
        }
        facts.update(self.metrics)
        return VerifiedLiveAccountV1(
            source_id=source_id,
            scope_sha256=expected_scope.scope_sha256,
            evidence_sha256=SHA_A,
            as_of_ms=account.captured_at_ms,
            account=account,
            **facts,
        )


class FixtureInputsVerifier:
    """Synthetic test-only source; this does not authenticate real producer receipts."""

    def verify(
        self,
        raw_proof,
        *,
        expected_scope,
        review,
        venue,
        allocation,
        system_mode,
        decided_at_ms,
    ):
        assert raw_proof == "fixture-input-proof"
        return VerifiedLiveInputsV1(
            source_id="fixture-upstream-inputs-v1",
            scope_sha256=expected_scope.scope_sha256,
            evidence_sha256=SHA_C,
            review_sha256=canonical_sha256(review.model_dump(mode="json")),
            venue_sha256=canonical_sha256(venue.model_dump(mode="json")),
            allocation_sha256=(
                canonical_sha256(allocation.model_dump(mode="json")) if allocation is not None else None
            ),
            system_mode=system_mode,
            observed_at_ms=decided_at_ms - 500,
        )


def _evaluator(*, verifier=None, input_verifier=True, limits=None) -> LiveRiskEvaluator:
    limits = limits or _limits()
    return LiveRiskEvaluator(
        _scope(limits),
        limits,
        account_verifiers={SOURCE_ID: verifier} if verifier else {},
        input_verifier=FixtureInputsVerifier() if input_verifier is True else input_verifier or None,
    )


def _evaluate(evaluator=None, **overrides):
    args: dict[str, object] = {
        "raw_account_evidence": "fixture-proof",
        "raw_inputs_evidence": "fixture-input-proof",
        "source_id": SOURCE_ID,
        "review": _review(),
        "venue": _prod_venue(),
        "allocation": _allocation(),
        "decided_at_ms": NOW,
        "system_mode": SystemMode.NORMAL,
    }
    args.update(overrides)
    return (evaluator or _evaluator(verifier=FixtureVerifier())).evaluate(**args)


def test_registered_synthetic_source_can_approve_with_true_live_prod_context() -> None:
    result = _evaluate()
    decision = result.decision

    assert decision.approved
    assert decision.trading_mode is TradingMode.LIVE
    assert decision.evedex_profile is EvedexProfile.PROD
    assert decision.account_id == ACCOUNT_ID
    assert decision.venue_symbol == "BTCUSD:PROD"
    assert decision.leverage == 1.0
    assert 0 < decision.quantity
    assert decision.quantity == pytest.approx(round(decision.quantity, 8))
    assert decision.worst_case_loss_usd <= decision.loss_budget_usd <= 25.0
    assert result.source_id == SOURCE_ID
    assert result.evidence_sha256 == SHA_A
    assert result.account_snapshot_sha256 is not None
    assert result.account_equity_usd == 10_000.0
    assert Decimal(str(decision.worst_case_loss_usd)) <= Decimal(str(decision.loss_budget_usd))
    assert Decimal(str(decision.notional_usd)) <= Decimal("3000")


def test_default_missing_verifier_fails_closed_without_claiming_approval() -> None:
    result = _evaluate(_evaluator())

    assert not result.decision.approved
    assert result.decision.quantity == 0
    assert result.decision.worst_case_loss_usd == 0
    assert "account_source_unregistered" in result.decision.rejection_reasons
    assert result.account_snapshot_sha256 is None
    assert result.account_equity_usd is None


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"per_trade_risk_fraction": 0.0025001}, "per_trade_risk_fraction"),
        ({"aggregate_open_risk_fraction": 0.010001}, "aggregate_open_risk_fraction"),
        ({"max_leverage": 1.01}, "max_leverage"),
        ({"daily_loss_cap_usd": 0.0}, "daily_loss_cap_usd"),
    ],
)
def test_limits_cannot_inflate_policy(limits, message) -> None:
    with pytest.raises(ValueError, match=message):
        _limits(**limits)


def test_scope_binds_exact_risk_limits() -> None:
    with pytest.raises(ValueError, match="not bound"):
        LiveRiskEvaluator(_scope(), _limits(per_trade_dollar_cap_usd=501.0))


def test_loss_dollar_cap_is_not_misinterpreted_as_an_order_notional_cap() -> None:
    limits = _limits(per_trade_dollar_cap_usd=0.25, min_notional_usd=5.0)
    assert limits.per_trade_dollar_cap_usd == 0.25
    assert limits.min_notional_usd == 5.0


@pytest.mark.parametrize(
    "field",
    [
        "aggregate_open_risk_usd",
        "aggregate_open_notional_usd",
        "reserved_open_risk_usd",
        "reserved_open_notional_usd",
        "strategy_open_notional_usd",
        "strategy_reserved_notional_usd",
        "daily_adverse_loss_usd",
    ],
)
def test_verified_financial_facts_reject_boolean_numbers(field) -> None:
    result = _evaluate(_evaluator(verifier=FixtureVerifier(**{field: True})))
    assert not result.decision.approved
    assert "account_source_verification_failed" in result.decision.rejection_reasons


def test_verified_account_as_of_rejects_boolean_timestamp() -> None:
    verifier = FixtureVerifier()
    original_verify = verifier.verify

    def bad_time(raw_evidence, *, source_id, expected_scope):
        verified = original_verify(raw_evidence, source_id=source_id, expected_scope=expected_scope)
        return verified.model_copy(update={"as_of_ms": True})

    verifier.verify = bad_time
    result = _evaluate(_evaluator(verifier=verifier))
    assert not result.decision.approved
    assert "account_source_verification_failed" in result.decision.rejection_reasons


def test_verifier_failure_and_foreign_scope_fail_closed() -> None:
    class RefusingVerifier:
        def verify(self, *_args, **_kwargs):
            raise RuntimeError("not independently verified")

    failed = _evaluate(_evaluator(verifier=RefusingVerifier()))
    assert not failed.decision.approved
    assert "account_source_verification_failed" in failed.decision.rejection_reasons

    foreign = FixtureVerifier()
    original_verify = foreign.verify

    def foreign_scope(raw_evidence, *, source_id, expected_scope):
        verified = original_verify(raw_evidence, source_id=source_id, expected_scope=expected_scope)
        return verified.model_copy(update={"scope_sha256": SHA_B})

    foreign.verify = foreign_scope
    result = _evaluate(_evaluator(verifier=foreign))
    assert not result.decision.approved
    assert "account_source_verification_failed" in result.decision.rejection_reasons


def test_missing_input_verifier_and_proof_fail_closed() -> None:
    no_verifier = _evaluate(_evaluator(input_verifier=False))
    assert not no_verifier.decision.approved
    assert "input_source_unregistered" in no_verifier.decision.rejection_reasons
    assert no_verifier.inputs_source_id is None

    missing_proof = _evaluate(_evaluator(), raw_inputs_evidence=None)
    assert not missing_proof.decision.approved
    assert "input_source_proof_missing" in missing_proof.decision.rejection_reasons


def test_daily_adverse_loss_cannot_offset_losses_with_profitable_pnl() -> None:
    underreported = _evaluate(
        _evaluator(
            verifier=FixtureVerifier(
                account_overrides={"daily_realized_pnl_usd": -2.0, "unrealized_pnl_usd": -3.0},
                daily_adverse_loss_usd=4.999,
            )
        )
    )
    assert not underreported.decision.approved
    assert "account_source_verification_failed" in underreported.decision.rejection_reasons

    exact_floor = _evaluate(
        _evaluator(
            verifier=FixtureVerifier(
                account_overrides={"daily_realized_pnl_usd": -2.0, "unrealized_pnl_usd": -3.0},
                daily_adverse_loss_usd=5.0,
            )
        )
    )
    assert exact_floor.decision.approved
    assert exact_floor.daily_adverse_loss_usd == 5.0


def test_fresh_venue_wrapper_does_not_refresh_old_book_or_reference() -> None:
    venue = _prod_venue(
        observed_at_ms=NOW - 500,
        expires_at_ms=NOW + 1_500,
        reference_timestamp_ms=NOW - 500,
        book_timestamp_ms=NOW - 2_001,
        reference_age_ms=0,
        book_age_ms=1_501,
        timestamp_skew_ms=1_501,
    )
    result = _evaluate(venue=venue)
    assert not result.decision.approved
    assert "venue_book_stale" in result.decision.rejection_reasons


@pytest.mark.parametrize("mutate", ["scope", "review", "future", "stale"])
def test_input_verification_must_bind_fresh_exact_inputs(mutate) -> None:
    class AlteredInputsVerifier(FixtureInputsVerifier):
        def verify(self, raw_proof, **kwargs):
            verified = super().verify(raw_proof, **kwargs)
            if mutate == "scope":
                return verified.model_copy(update={"scope_sha256": SHA_B})
            if mutate == "review":
                return verified.model_copy(update={"review_sha256": SHA_B})
            if mutate == "future":
                return verified.model_copy(update={"observed_at_ms": NOW + 1})
            return verified.model_copy(update={"observed_at_ms": NOW - 5_001})

    result = _evaluate(_evaluator(input_verifier=AlteredInputsVerifier()))
    assert not result.decision.approved
    assert "input_source_verification_failed" in result.decision.rejection_reasons
    assert result.inputs_source_id is None


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"system_mode": SystemMode.LOCAL_QUANT_MODE}, "system_mode_local_quant_mode"),
        ({"review": _review(decision=ReviewDecision.VETO)}, "review_veto"),
        ({"allocation": _allocation(regime=MarketRegime.BEAR)}, "macro_regime_forbids_long"),
        ({"allocation": None}, "strategic_allocation_missing"),
        ({"decided_at_ms": NOW + 2_001}, "venue_quality_stale"),
    ],
)
def test_admission_gates_refuse_without_quotas(kwargs, reason) -> None:
    result = _evaluate(**kwargs)
    assert not result.decision.approved
    assert reason in result.decision.rejection_reasons
    assert result.decision.quantity == 0


def test_daily_and_aggregate_reservations_are_each_counted_once() -> None:
    bounded = _evaluate(
        _evaluator(
            verifier=FixtureVerifier(
                aggregate_open_risk_usd=70.0,
                reserved_open_risk_usd=10.0,
                daily_adverse_loss_usd=900.0,
            )
        )
    )
    assert bounded.decision.approved
    assert bounded.decision.loss_budget_usd == pytest.approx(20.0)

    daily_block = _evaluate(
        _evaluator(verifier=FixtureVerifier(daily_adverse_loss_usd=995.0, reserved_open_risk_usd=5.0))
    )
    assert not daily_block.decision.approved
    assert "daily_loss_limit_exhausted" in daily_block.decision.rejection_reasons

    aggregate_block = _evaluate(
        _evaluator(verifier=FixtureVerifier(aggregate_open_risk_usd=95.0, reserved_open_risk_usd=5.0))
    )
    assert not aggregate_block.decision.approved
    assert "risk_budget_exhausted" in aggregate_block.decision.rejection_reasons


def test_daily_loss_and_dollar_caps_bind_without_changing_strategy() -> None:
    tiny_day = _evaluate(
        _evaluator(
            verifier=FixtureVerifier(daily_adverse_loss_usd=990.0),
            limits=_limits(daily_loss_cap_usd=1_000.0, per_trade_dollar_cap_usd=500.0),
        )
    )
    assert tiny_day.decision.approved
    assert tiny_day.decision.worst_case_loss_usd <= 10.0

    small_dollars = _evaluate(
        _evaluator(
            verifier=FixtureVerifier(),
            limits=_limits(per_trade_dollar_cap_usd=10.0, max_position_notional_usd=100.0),
        )
    )
    assert small_dollars.decision.approved
    assert small_dollars.decision.worst_case_loss_usd <= 10.0
    assert small_dollars.decision.notional_usd <= 100.0


def test_tiny_loss_budget_does_not_round_up_to_the_minimum_order() -> None:
    result = _evaluate(
        _evaluator(verifier=FixtureVerifier(), limits=_limits(per_trade_dollar_cap_usd=0.000001))
    )
    assert not result.decision.approved
    assert result.decision.quantity == 0
    assert result.decision.notional_usd == 0
    assert result.decision.worst_case_loss_usd == 0
    assert "position_below_minimum_notional" in result.decision.rejection_reasons


def test_huge_finite_sizing_input_fails_bounded_and_closed() -> None:
    tiny = 1e-300
    intent = _intent(
        reference_price=tiny,
        gross_reward_bps=5_000.0,
        exit_plan=ExitPlanV1(stop_price=0.5e-300, target_price=1.5e-300, max_holding_ms=180_000),
    )
    venue = _prod_venue(
        reference_timestamp_ms=NOW - 500,
        book_timestamp_ms=NOW - 500,
        reference_mid_price=tiny,
        best_bid=0.9e-300,
        best_ask=1.1e-300,
        venue_mid_price=tiny,
        basis_bps=0.0,
        spread_bps=2_000.0,
        assessed_notional_usd=1e308,
        depth_usd=1e308,
        reference_age_ms=0,
        book_age_ms=0,
        timestamp_skew_ms=0,
    )
    limits = _limits(
        per_trade_dollar_cap_usd=1e308,
        aggregate_dollar_cap_usd=1e308,
        daily_loss_cap_usd=1e308,
        max_account_equity_usd=1e308,
        max_position_notional_usd=1e308,
        min_notional_usd=1.0,
    )
    huge_verifier = FixtureVerifier(
        account_overrides={
            "equity_usd": 1e308,
            "available_balance_usd": 1e308,
            "durable_day_start_equity_usd": 1e308,
            "durable_peak_equity_usd": 1e308,
        }
    )
    result = _evaluate(
        _evaluator(verifier=huge_verifier, limits=limits),
        review=_review(intent=intent),
        venue=venue,
    )
    assert not result.decision.approved
    assert "sizing_numeric_invalid" in result.decision.rejection_reasons
    assert result.decision.quantity == 0
    assert result.decision.worst_case_loss_usd == 0


def test_existing_stop_risk_also_consumes_daily_loss_headroom() -> None:
    result = _evaluate(
        _evaluator(
            verifier=FixtureVerifier(aggregate_open_risk_usd=30.0, daily_adverse_loss_usd=980.0),
            limits=_limits(daily_loss_cap_usd=1_000.0),
        )
    )
    assert not result.decision.approved
    assert "daily_loss_limit_exhausted" in result.decision.rejection_reasons


def test_repeated_identical_inputs_yield_identical_core_decision_ids() -> None:
    evaluator = _evaluator(verifier=FixtureVerifier())
    first = _evaluate(evaluator)
    second = _evaluate(evaluator)
    assert first.decision.decision_id == second.decision.decision_id
    assert first.decision.trade_id == second.decision.trade_id


def test_paper_venue_cannot_be_relabelled_live() -> None:
    values = _prod_venue().model_dump(mode="python")
    values.update(profile=EvedexProfile.DEV, measurement_id=None)
    dev_venue = VenueQualityV1(**values)
    with pytest.raises(ValueError, match="EVEDEX PROD"):
        _evaluate(venue=dev_venue)
