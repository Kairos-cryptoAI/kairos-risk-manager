"""Learning ledger and adaptive risk: risk follows proven edge and drawdown, under the ceiling."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kairos_risk.adaptive_risk import AdaptiveRiskPolicyV1, adapt_limits, adaptive_risk_fraction
from kairos_risk.learning import (
    NO_EVIDENCE,
    LearningLedger,
    LedgerPolicy,
    TradeOutcome,
    outcomes_from_execution_report,
    regime_at,
)


def _outcomes(rs, *, strategy="hourly_trend", symbol="SOLUSDT", regime="BULL", source="LIVE"):
    return [
        TradeOutcome(strategy, symbol, regime, Decimal(str(r)), 1_000 + index, source)
        for index, r in enumerate(rs)
    ]


def test_ledger_needs_enough_evidence_and_falls_back_only_within_the_strategy():
    ledger = LearningLedger(
        tuple(
            _outcomes([1, -1] * 5)  # 10 trades in the exact cell: not enough
            + _outcomes([1.2, -1] * 10, symbol="XRPUSDT")  # 20 more in BULL for the strategy
            + _outcomes([3] * 40, strategy="other")
        )
    )
    estimate = ledger.estimate("hourly_trend", "SOLUSDT", "BULL")
    assert estimate.level == "strategy_regime" and estimate.samples == 30
    assert ledger.estimate("unknown", "SOLUSDT", "BULL") is NO_EVIDENCE
    assert ledger.estimate("hourly_trend", "SOLUSDT", "BEAR").level == "strategy"


def test_lower_bound_is_conservative_and_replays_count_less():
    live = LearningLedger(tuple(_outcomes([0.5] * 10 + [-0.4] * 10)))
    estimate = live.estimate("hourly_trend", "SOLUSDT", "BULL")
    assert estimate.mean_r == Decimal("0.05") and estimate.lower_r < estimate.mean_r
    replay = LearningLedger(tuple(_outcomes([0.5] * 10 + [-0.4] * 10, source="REPLAY")))
    assert replay.estimate("hourly_trend", "SOLUSDT", "BULL") is NO_EVIDENCE, "20 replays weigh as 10"
    with pytest.raises(ValueError):
        LedgerPolicy(minimum_effective_samples=Decimal(2))


def test_execution_report_rows_become_outcomes_with_the_regime_in_force():
    regime_of = regime_at(
        [{"produced_at_ms": 0, "regime": "BEAR"}, {"produced_at_ms": 5_000, "regime": "BULL"}]
    )
    rows = [
        {
            "version": "kairos.execution-closed-trade.v1",
            "strategy_id": "hourly_trend",
            "symbol": "SOLUSDT",
            "decided_at_ms": 6_000,
            "r_multiple": "1.25",
        },
        {
            "version": "kairos.execution-closed-trade.v1",
            "strategy_id": "hourly_trend",
            "symbol": "SOLUSDT",
            "decided_at_ms": 4_000,
            "r_multiple": "-1",
        },
        {"version": "other", "r_multiple": "9"},
    ]
    outcomes = outcomes_from_execution_report(rows, regime_of=regime_of)
    assert [(o.regime, o.r_multiple) for o in outcomes] == [
        ("BULL", Decimal("1.25")),
        ("BEAR", Decimal("-1")),
    ]
    assert regime_at([])(10) == "UNKNOWN"


def _edge(rs):
    return LearningLedger(tuple(_outcomes(rs))).estimate("hourly_trend", "SOLUSDT", "BULL")


def test_unproven_edge_uses_half_and_proven_negative_edge_uses_nothing():
    policy = AdaptiveRiskPolicyV1()
    unproven = adaptive_risk_fraction(
        policy, NO_EVIDENCE, equity_usd=Decimal(100), peak_equity_usd=Decimal(100)
    )
    assert unproven.fraction == Decimal("0.00125") and unproven.allows_entry
    negative = adaptive_risk_fraction(
        policy, _edge([-0.3, 0.2] * 15), equity_usd=Decimal(100), peak_equity_usd=Decimal(100)
    )
    assert negative.fraction == 0 and not negative.allows_entry
    assert "no proven edge" in negative.reasons[0]


def test_risk_grows_only_with_proven_edge_and_never_above_the_ceiling():
    policy = AdaptiveRiskPolicyV1()
    strong = adaptive_risk_fraction(
        policy, _edge([1.5, 0.8] * 20), equity_usd=Decimal(100), peak_equity_usd=Decimal(100)
    )
    modest = adaptive_risk_fraction(
        policy, _edge([1.0, -0.4] * 20), equity_usd=Decimal(100), peak_equity_usd=Decimal(100)
    )
    assert strong.fraction == Decimal("0.0025") >= modest.fraction > Decimal("0.00125")
    with pytest.raises(ValueError, match="0.25 %"):
        AdaptiveRiskPolicyV1(ceiling_fraction=Decimal("0.01"))


def test_drawdown_priority_and_venue_cost_only_reduce_risk():
    policy = AdaptiveRiskPolicyV1()
    edge = _edge([1.5, 0.8] * 20)
    base = adaptive_risk_fraction(policy, edge, equity_usd=Decimal(100), peak_equity_usd=Decimal(100))
    drawdown = adaptive_risk_fraction(policy, edge, equity_usd=Decimal(98), peak_equity_usd=Decimal(100))
    assert drawdown.fraction == (base.fraction / 2).quantize(Decimal("0.000001"))
    low_priority = adaptive_risk_fraction(
        policy, edge, equity_usd=Decimal(100), peak_equity_usd=Decimal(100), review_priority=0
    )
    costly = adaptive_risk_fraction(
        policy, edge, equity_usd=Decimal(100), peak_equity_usd=Decimal(100), venue_cost_bps=Decimal(20)
    )
    assert low_priority.fraction == costly.fraction == base.fraction / 2
    with pytest.raises(ValueError):
        adaptive_risk_fraction(policy, edge, equity_usd=Decimal(101), peak_equity_usd=Decimal(100))


def test_adapted_limits_shrink_the_real_evaluators_size_without_changing_the_strategy():
    from tests.test_live_risk import FixtureVerifier, _evaluate, _evaluator, _limits

    limits = _limits()
    base = _evaluate(_evaluator(verifier=FixtureVerifier(), limits=limits))
    decision = adaptive_risk_fraction(
        AdaptiveRiskPolicyV1(ceiling_fraction=Decimal(str(limits.per_trade_risk_fraction))),
        NO_EVIDENCE,
        equity_usd=Decimal(10_000),
        peak_equity_usd=Decimal(10_000),
    )
    adapted = adapt_limits(limits, decision)
    assert adapted is not None and adapted.per_trade_risk_fraction < limits.per_trade_risk_fraction
    reduced = _evaluate(_evaluator(verifier=FixtureVerifier(), limits=adapted))
    assert base.decision.approved and reduced.decision.approved
    assert reduced.decision.worst_case_loss_usd < base.decision.worst_case_loss_usd
    assert reduced.decision.intent == base.decision.intent
    assert (
        adapt_limits(
            limits,
            adaptive_risk_fraction(
                AdaptiveRiskPolicyV1(),
                _edge([-1, 0.5] * 15),
                equity_usd=Decimal(1),
                peak_equity_usd=Decimal(1),
            ),
        )
        is None
    )
