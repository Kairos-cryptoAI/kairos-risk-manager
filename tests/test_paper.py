"""Pure PAPER loss-at-stop sizing and admission invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kairos_core.contracts import (
    AccountSnapshotV2,
    CandidateReviewV1,
    CandidateRouteV1,
    ExitPlanV1,
    OpenOrderSnapshotV2,
    PositionSnapshotV2,
    StrategicAllocation,
    StrategyIntentV1,
    StrategyProvenanceV1,
    VenueQualityV1,
)
from kairos_core.enums import (
    CandidateReviewTier,
    EvedexProfile,
    MarketRegime,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    ReasoningEffort,
    ReviewDecision,
    Side,
    StrategicTrigger,
    SystemMode,
    TradeLifecycleState,
    TradingMode,
)

from kairos_risk.config import PAPER_DEV_SYMBOL_MAP, RiskSettings
from kairos_risk.paper import PaperReservations, PaperRiskPipeline

T0 = 1_800_000_000_000
NOW = T0 + 60_400
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _settings(**overrides: object) -> RiskSettings:
    values: dict[str, object] = {
        "trading_mode": TradingMode.PAPER,
        "paper_strategy_allowlist": ["technical-canary@1"],
        "paper_account_id": "kairos-paper-dev-01",
    }
    values.update(overrides)
    return RiskSettings(**values)


def _intent(**overrides: object) -> StrategyIntentV1:
    values: dict[str, object] = {
        "source": "strategy-engine",
        "strategy_id": "technical-canary",
        "strategy_revision": "1",
        "symbol": "BTCUSDT",
        "side": Side.LONG,
        "decision_ts_ms": T0 + 59_999,
        "entry_eligible_ts_ms": T0 + 60_000,
        "entry_expires_ts_ms": T0 + 120_000,
        "reference_price": 100.0,
        "signal_strength": 0.7,
        "gross_reward_bps": 500.0,
        "exit_plan": ExitPlanV1(
            stop_price=95.0,
            target_price=105.0,
            max_holding_ms=180_000,
        ),
        "provenance": StrategyProvenanceV1(
            strategy_code_sha256=SHA_A,
            config_sha256=SHA_B,
            input_window_sha256=SHA_C,
            features_sha256=SHA_D,
            input_bar_sha256s=(SHA_A, SHA_B),
        ),
    }
    values.update(overrides)
    return StrategyIntentV1(**values)


def _review(
    *,
    intent: StrategyIntentV1 | None = None,
    decision: ReviewDecision = ReviewDecision.ALLOW,
    priority: int = 50,
) -> CandidateReviewV1:
    candidate = intent or _intent()
    route = CandidateRouteV1(
        source="router",
        intent=candidate,
        review_tier=CandidateReviewTier.NORMAL,
        requested_reasoning_effort=ReasoningEffort.MEDIUM,
        routed_at_ms=T0 + 60_000,
        review_deadline_ms=T0 + 119_000,
    )
    return CandidateReviewV1(
        source="aggregator",
        route=route,
        intent=candidate,
        decision=decision,
        priority=priority,
        reviewed_at_ms=T0 + 60_100,
        reviewer="DETERMINISTIC",
        reason_codes=(decision.value,),
    )


def _venue(**overrides: object) -> VenueQualityV1:
    best_bid, best_ask = 99.99, 100.01
    venue_mid = (best_bid + best_ask) / 2
    values: dict[str, object] = {
        "source": "quant-scouts",
        "profile": EvedexProfile.DEV,
        "symbol": "BTCUSD:DEV",
        "observed_at_ms": T0 + 60_300,
        "expires_at_ms": T0 + 65_300,
        "reference_timestamp_ms": T0 + 60_200,
        "book_timestamp_ms": T0 + 60_200,
        "reference_mid_price": 100.0,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "venue_mid_price": venue_mid,
        "basis_bps": 0.0,
        "spread_bps": (best_ask - best_bid) / venue_mid * 10_000,
        "assessed_notional_usd": 1_000.0,
        "depth_usd": 5_000.0,
        "buy_slippage_bps": 1.0,
        "sell_slippage_bps": 1.2,
        "taker_fee_bps": 5.0,
        "reference_age_ms": 100,
        "book_age_ms": 100,
        "latency_ms": 20,
        "timestamp_skew_ms": 0,
        "entry_allowed": True,
    }
    values.update(overrides)
    return VenueQualityV1(**values)


def _account(**overrides: object) -> AccountSnapshotV2:
    values: dict[str, object] = {
        "source": "execution-engine",
        "trading_mode": TradingMode.PAPER,
        "evedex_profile": EvedexProfile.DEV,
        "account_id": "kairos-paper-dev-01",
        "equity_usd": 10_000.0,
        "available_balance_usd": 9_000.0,
        "margin_used_usd": 0.0,
        "durable_day_start_equity_usd": 10_000.0,
        "durable_peak_equity_usd": 10_000.0,
        "total_open_risk_usd": 0.0,
        "captured_at_ms": T0 + 60_200,
        "reconciliation_seq": 1,
        "reconciled": True,
        "reconciliation_detail": "full DEV reconciliation",
    }
    values.update(overrides)
    return AccountSnapshotV2(**values)


def _position(**overrides: object) -> PositionSnapshotV2:
    plan = ExitPlanV1(stop_price=95.0, target_price=105.0, max_holding_ms=180_000)
    values: dict[str, object] = {
        "venue_symbol": "ETHUSD:DEV",
        "side": Side.LONG,
        "signed_quantity": 0.1,
        "entry_price": 100.0,
        "mark_price": 100.0,
        "leverage": 1.0,
        "strategy_id": "technical-canary",
        "strategy_revision": "1",
        "intent_id": SHA_A,
        "risk_decision_id": SHA_B,
        "trade_id": SHA_C,
        "lifecycle_state": TradeLifecycleState.ACTIVE,
        "entry_client_order_id": "entry-order-0001",
        "stop_client_order_id": "stop-order-00001",
        "target_client_order_id": "target-order-001",
        "first_fill_at_ms": T0,
        "timeout_at_ms": T0 + plan.max_holding_ms,
        "exit_plan": plan,
    }
    values.update(overrides)
    return PositionSnapshotV2(**values)


def _allocation(
    *,
    strategy_id: str = "technical-canary",
    produced_at: datetime | None = None,
    regime: MarketRegime = MarketRegime.BULL,
    strategy_weight: float = 0.8,
    leverage: float = 2.0,
) -> StrategicAllocation:
    return StrategicAllocation(
        source="macro-strategist",
        regime=regime,
        stable_reserve_pct=0.2,
        strategy_weights={strategy_id: strategy_weight},
        max_gross_leverage=leverage,
        triggered_by=StrategicTrigger.SCHEDULE,
        produced_at=produced_at or datetime.fromtimestamp((T0 + 60_000) / 1_000, tz=UTC),
    )


def _evaluate(
    *,
    review: CandidateReviewV1 | None = None,
    venue: VenueQualityV1 | None = None,
    account: AccountSnapshotV2 | None = None,
    allocation: StrategicAllocation | None = None,
    settings: RiskSettings | None = None,
    decided_at_ms: int = NOW,
    system_mode: SystemMode = SystemMode.NORMAL,
    reservations: PaperReservations | None = None,
):
    return PaperRiskPipeline(settings or _settings()).evaluate(
        review or _review(),
        venue or _venue(),
        account=account if account is not None else _account(),
        allocation=allocation if allocation is not None else _allocation(),
        decided_at_ms=decided_at_ms,
        system_mode=system_mode,
        reservations=reservations,
    )


def test_approved_quantity_is_exact_loss_at_stop_budget() -> None:
    decision = _evaluate()

    per_unit_loss = (
        abs(decision.worst_entry_price - decision.exit_plan.stop_price)
        + (decision.worst_entry_price + decision.exit_plan.stop_price)
        * decision.venue_quality.taker_fee_bps
        / 10_000
        + decision.venue_quality.venue_mid_price * decision.venue_quality.buy_slippage_bps / 10_000
    )
    assert decision.approved
    assert decision.entry_policy.value == "NEXT_BAR_MARKET"
    assert decision.trading_mode is TradingMode.PAPER
    assert decision.evedex_profile is EvedexProfile.DEV
    assert decision.loss_budget_usd == pytest.approx(25.0)
    assert decision.quantity == pytest.approx(decision.loss_budget_usd / per_unit_loss)
    assert decision.worst_case_loss_usd == pytest.approx(decision.loss_budget_usd)
    assert decision.exit_plan == decision.intent.exit_plan
    assert decision.intent == decision.review.intent
    assert decision.venue_quality == _venue()
    assert decision.account_id == "kairos-paper-dev-01"
    assert decision.trade_id is not None
    assert decision.decision_id == decision.message_id


def test_short_uses_bid_sell_slippage_and_identical_risk_formula() -> None:
    intent = _intent(
        side=Side.SHORT,
        exit_plan=ExitPlanV1(
            stop_price=105.0,
            target_price=95.0,
            max_holding_ms=180_000,
        ),
    )
    decision = _evaluate(
        review=_review(intent=intent),
        allocation=_allocation(regime=MarketRegime.BEAR),
    )

    per_unit_loss = (
        abs(decision.worst_entry_price - decision.exit_plan.stop_price)
        + (decision.worst_entry_price + decision.exit_plan.stop_price)
        * decision.venue_quality.taker_fee_bps
        / 10_000
        + decision.venue_quality.venue_mid_price * decision.venue_quality.sell_slippage_bps / 10_000
    )
    assert decision.approved
    assert decision.worst_entry_price == decision.venue_quality.best_bid
    assert decision.quantity == pytest.approx(25.0 / per_unit_loss)
    assert decision.worst_case_loss_usd == pytest.approx(25.0)


@pytest.mark.parametrize("decision", [ReviewDecision.VETO, ReviewDecision.DEFER])
def test_only_allow_can_be_approved(decision: ReviewDecision) -> None:
    result = _evaluate(review=_review(decision=decision))

    assert not result.approved
    assert result.quantity == 0
    assert f"review_{decision.value.lower()}" in result.rejection_reasons


def test_llm_priority_and_signal_strength_never_increase_size() -> None:
    low_priority = _evaluate(review=_review(priority=0))
    high_priority = _evaluate(review=_review(priority=100))
    weak_signal = _evaluate(review=_review(intent=_intent(signal_strength=0.01)))
    strong_signal = _evaluate(review=_review(intent=_intent(signal_strength=1.0)))

    assert low_priority.quantity == high_priority.quantity
    assert weak_signal.quantity == strong_signal.quantity


def test_portfolio_open_risk_reduces_budget_and_hard_stops_at_one_percent() -> None:
    near_limit = _evaluate(account=_account(total_open_risk_usd=99.0))
    at_limit = _evaluate(account=_account(total_open_risk_usd=100.0))

    assert near_limit.approved
    assert near_limit.loss_budget_usd == pytest.approx(1.0)
    assert near_limit.worst_case_loss_usd == pytest.approx(1.0)
    assert not at_limit.approved
    assert "portfolio_open_risk_limit_exhausted" in at_limit.rejection_reasons


def test_leverage_notional_macro_and_liquidity_are_caps_after_risk_sizing() -> None:
    uncapped = _evaluate()
    macro_leverage = _evaluate(settings=_settings(paper_max_leverage=5.0))
    small_macro = _evaluate(allocation=_allocation(strategy_weight=0.001))
    small_book = _evaluate(venue=_venue(assessed_notional_usd=10.0, depth_usd=10.0))

    assert uncapped.leverage == 1.0
    assert macro_leverage.leverage == 2.0
    assert macro_leverage.quantity == uncapped.quantity
    assert small_macro.notional_usd <= 20.0 + 1e-6
    assert small_book.notional_usd <= 10.0 + 1e-6
    assert small_macro.quantity < uncapped.quantity
    assert small_book.quantity < uncapped.quantity


def test_existing_order_and_in_process_reservation_each_block_second_idea() -> None:
    open_order = OpenOrderSnapshotV2(
        venue_symbol="BTCUSD:DEV",
        client_order_id="entry-0001",
        exchange_order_id="exchange-1",
        order_role=OrderRole.ENTRY,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.NEW,
        quantity=1.0,
        price=100.0,
        reduce_only=False,
        strategy_id="technical-canary",
        strategy_revision="1",
        intent_id=SHA_A,
        risk_decision_id=SHA_B,
        trade_id=SHA_C,
        created_at_ms=T0 + 60_000,
        updated_at_ms=T0 + 60_100,
    )
    from_snapshot = _evaluate(account=_account(open_orders=(open_order,)))
    from_reservation = _evaluate(
        reservations=PaperReservations(
            symbols=frozenset({"BTCUSD:DEV"}),
            open_risk_usd=10.0,
            notional_usd=100.0,
        )
    )

    assert not from_snapshot.approved
    assert "symbol_has_open_order" in from_snapshot.rejection_reasons
    assert not from_reservation.approved
    assert "symbol_has_reserved_idea" in from_reservation.rejection_reasons


def test_other_symbol_reservation_counts_toward_portfolio_risk_and_notional() -> None:
    decision = _evaluate(
        reservations=PaperReservations(
            symbols=frozenset({"ETHUSD:DEV"}),
            open_risk_usd=99.0,
            notional_usd=500.0,
        )
    )

    assert decision.approved
    assert decision.loss_budget_usd == pytest.approx(1.0)
    assert decision.worst_case_loss_usd == pytest.approx(1.0)


def test_technical_canary_has_one_global_idea_across_all_symbols_and_states() -> None:
    existing_position = _evaluate(
        account=_account(positions=(_position(),), total_open_risk_usd=0.5),
    )
    existing_entry_order = OpenOrderSnapshotV2(
        venue_symbol="ETHUSD:DEV",
        client_order_id="entry-order-0002",
        exchange_order_id="exchange-2",
        order_role=OrderRole.ENTRY,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.NEW,
        quantity=1.0,
        price=100.0,
        reduce_only=False,
        strategy_id="technical-canary",
        strategy_revision="1",
        intent_id=SHA_A,
        risk_decision_id=SHA_B,
        trade_id=SHA_D,
        created_at_ms=T0 + 60_000,
        updated_at_ms=T0 + 60_100,
    )
    pending_entry = _evaluate(account=_account(open_orders=(existing_entry_order,)))
    fully_filled_payload = existing_entry_order.model_dump()
    fully_filled_payload.update(
        filled_quantity=existing_entry_order.quantity,
        status=OrderStatus.PARTIALLY_FILLED,
    )
    still_open_after_reported_fill = _evaluate(
        account=_account(open_orders=(OpenOrderSnapshotV2(**fully_filled_payload),))
    )
    recovered_or_in_flight = _evaluate(
        reservations=PaperReservations(
            symbols=frozenset({"ETHUSD:DEV"}),
            open_risk_usd=1.0,
            notional_usd=100.0,
            strategy_notional_usd=(("technical-canary", 100.0),),
            active_canary_ideas=1,
        )
    )

    assert not existing_position.approved
    assert "global_canary_idea_limit_reached" in existing_position.rejection_reasons
    assert not pending_entry.approved
    assert "global_canary_idea_limit_reached" in pending_entry.rejection_reasons
    assert not still_open_after_reported_fill.approved
    assert "global_canary_idea_limit_reached" in still_open_after_reported_fill.rejection_reasons
    assert "pending_entry_order_present" in still_open_after_reported_fill.rejection_reasons
    assert not recovered_or_in_flight.approved
    assert "global_canary_idea_limit_reached" in recovered_or_in_flight.rejection_reasons


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"decided_at_ms": T0 + 59_999}, "candidate_not_yet_eligible"),
        ({"decided_at_ms": T0 + 120_001}, "candidate_expired"),
        ({"system_mode": SystemMode.CONFLICT_SAFE}, "system_mode_conflict_safe"),
        ({"decided_at_ms": T0 + 66_000}, "venue_quality_stale"),
        (
            {"account": _account(captured_at_ms=T0)},
            "account_snapshot_stale",
        ),
        (
            {
                "allocation": _allocation(
                    produced_at=datetime.fromtimestamp(T0 / 1_000, tz=UTC) - timedelta(days=2)
                )
            },
            "strategic_allocation_stale",
        ),
    ],
)
def test_time_and_circuit_gates_fail_closed(kwargs: dict[str, object], reason: str) -> None:
    decision = _evaluate(**kwargs)

    assert not decision.approved
    assert reason in decision.rejection_reasons


def test_blocked_venue_wrong_account_and_missing_macro_are_explicit_rejections() -> None:
    blocked = _evaluate(venue=_venue(entry_allowed=False, reason_codes=("basis_exceeds_limit",)))
    wrong_account = _evaluate(account=_account(account_id="another-paper-account"))
    missing_macro = PaperRiskPipeline(_settings()).evaluate(
        _review(),
        _venue(),
        account=_account(),
        allocation=None,
        decided_at_ms=NOW,
    )

    assert not blocked.approved
    assert "venue_entry_blocked" in blocked.rejection_reasons
    assert "venue_basis_exceeds_limit" in blocked.rejection_reasons
    assert not wrong_account.approved
    assert "account_id_mismatch" in wrong_account.rejection_reasons
    assert not missing_macro.approved
    assert "strategic_allocation_missing" in missing_macro.rejection_reasons


@pytest.mark.parametrize("equity", [1_000.0, 10_000.0, 1_000_000.0])
@pytest.mark.parametrize("stop_price", [99.0, 95.0, 75.0])
def test_loss_property_never_exceeds_quarter_percent_at_varied_scale(
    equity: float,
    stop_price: float,
) -> None:
    intent = _intent(
        exit_plan=ExitPlanV1(
            stop_price=stop_price,
            target_price=105.0,
            max_holding_ms=180_000,
        )
    )
    settings = _settings(
        paper_min_notional_usd=0.01,
        paper_max_position_notional_usd=10_000_000.0,
    )
    venue = _venue(assessed_notional_usd=10_000_000.0, depth_usd=10_000_000.0)
    account = _account(
        equity_usd=equity,
        available_balance_usd=equity,
        durable_day_start_equity_usd=equity,
        durable_peak_equity_usd=equity,
    )
    allocation = _allocation(strategy_weight=0.8, leverage=5.0)

    decision = _evaluate(
        review=_review(intent=intent),
        venue=venue,
        account=account,
        allocation=allocation,
        settings=settings,
    )

    assert decision.approved
    assert decision.loss_budget_usd <= equity * 0.0025 + 1e-9
    assert decision.worst_case_loss_usd <= decision.loss_budget_usd + 1e-6


def test_strategy_allowlist_is_empty_by_default_and_rejected_sleeves_stay_denied() -> None:
    defaults = RiskSettings()
    mistaken = _settings(
        paper_strategy_allowlist=["trend_breakout_v1@1"],
    )

    assert not defaults.paper_strategy_allowed("technical-canary", "1")
    assert not mistaken.paper_strategy_allowed("trend_breakout_v1", "1")


def test_replay_is_byte_for_byte_stable() -> None:
    pipeline = PaperRiskPipeline(_settings())
    review = _review()
    venue = _venue()
    account = _account()
    allocation = _allocation()

    first = pipeline.evaluate(
        review,
        venue,
        account=account,
        allocation=allocation,
        decided_at_ms=NOW,
    )
    second = pipeline.evaluate(
        CandidateReviewV1.from_json(review.to_json()),
        VenueQualityV1.from_json(venue.to_json()),
        account=AccountSnapshotV2.from_json(account.to_json()),
        allocation=StrategicAllocation.from_json(allocation.to_json()),
        decided_at_ms=NOW,
    )

    assert first.to_json().encode() == second.to_json().encode()
    assert first.decision_id == second.decision_id
    assert first.trade_id == second.trade_id


def test_fixed_symbol_map_matches_all_five_evedex_dev_instruments() -> None:
    assert PAPER_DEV_SYMBOL_MAP == {
        "BTCUSDT": "BTCUSD:DEV",
        "ETHUSDT": "ETHUSD:DEV",
        "SOLUSDT": "SOLUSD:DEV",
        "BNBUSDT": "BNBUSD:DEV",
        "XRPUSDT": "XRPUSD:DEV",
    }
