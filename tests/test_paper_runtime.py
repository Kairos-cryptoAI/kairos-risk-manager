"""Replay, race and service-boundary checks for PAPER correlation state."""

from __future__ import annotations

import asyncio

import pytest
from kairos_core.bus import BusEnvelope
from kairos_core.contracts import OpenOrderSnapshotV2
from kairos_core.enums import (
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    SystemMode,
    TradingMode,
)
from kairos_core.topics import Topics

from kairos_risk.config import RiskSettings
from kairos_risk.paper_runtime import PaperInputUnavailable, PaperRiskCoordinator
from kairos_risk.service import RiskService
from tests.test_paper import (
    NOW,
    T0,
    _account,
    _allocation,
    _intent,
    _position,
    _review,
    _settings,
    _venue,
)
from tests.test_service import FakeBus


def _envelope(topic: str, message: object, *, envelope_id: str = "paper-1") -> BusEnvelope:
    return BusEnvelope(id=envelope_id, topic=topic, payload=message.to_payload())


@pytest.mark.asyncio
async def test_duplicate_review_replays_the_exact_cached_decision() -> None:
    coordinator = PaperRiskCoordinator(_settings())
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    review = _review()

    first = await coordinator.evaluate(
        review,
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )
    second = await coordinator.evaluate(
        review,
        allocation=_allocation(),
        decided_at_ms=NOW + 1_000,
        system_mode=SystemMode.LOCAL_QUANT_MODE,
    )

    assert first.approved
    assert first.to_json() == second.to_json()
    assert coordinator.reservations.symbols == frozenset({"BTCUSD:DEV"})


@pytest.mark.asyncio
async def test_active_reservation_replay_survives_decision_cache_pressure() -> None:
    coordinator = PaperRiskCoordinator(_settings(paper_decision_cache_size=1))
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    first_review = _review()
    first = await coordinator.evaluate(
        first_review,
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )
    await coordinator.evaluate(
        _review(intent=_intent(metadata=(("cache", "pressure"),))),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    replay = await coordinator.evaluate(
        first_review,
        allocation=_allocation(),
        decided_at_ms=NOW + 1,
        system_mode=SystemMode.CONFLICT_SAFE,
    )

    assert first.approved
    assert replay.to_json() == first.to_json()


@pytest.mark.asyncio
async def test_simultaneous_candidates_cannot_reserve_the_same_symbol_twice() -> None:
    coordinator = PaperRiskCoordinator(_settings())
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    first_review = _review()
    second_review = _review(intent=_intent(metadata=(("candidate", "second"),)))

    decisions = await asyncio.gather(
        coordinator.evaluate(
            first_review,
            allocation=_allocation(),
            decided_at_ms=NOW,
            system_mode=SystemMode.NORMAL,
        ),
        coordinator.evaluate(
            second_review,
            allocation=_allocation(),
            decided_at_ms=NOW,
            system_mode=SystemMode.NORMAL,
        ),
    )

    assert sum(decision.approved for decision in decisions) == 1
    rejected = next(decision for decision in decisions if not decision.approved)
    assert "symbol_has_reserved_idea" in rejected.rejection_reasons
    assert len(coordinator.reservations.symbols) == 1


@pytest.mark.asyncio
async def test_technical_canary_reservation_blocks_another_symbol_globally() -> None:
    coordinator = PaperRiskCoordinator(_settings())
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    first = await coordinator.evaluate(
        _review(),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    await coordinator.apply_venue(_venue(symbol="ETHUSD:DEV"))
    second_intent = _intent(symbol="ETHUSDT", metadata=(("candidate", "eth"),))
    second = await coordinator.evaluate(
        _review(intent=second_intent),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    assert first.approved
    assert not second.approved
    assert "global_canary_idea_limit_reached" in second.rejection_reasons
    assert coordinator.reservations.active_canary_ideas == 1


@pytest.mark.asyncio
async def test_new_reconciliation_clears_unseen_reservation_only_after_entry_expiry() -> None:
    coordinator = PaperRiskCoordinator(_settings())
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    await coordinator.evaluate(
        _review(),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    await coordinator.apply_account(_account(captured_at_ms=T0 + 119_999, reconciliation_seq=2))
    assert coordinator.reservations.symbols == frozenset({"BTCUSD:DEV"})

    await coordinator.apply_account(_account(captured_at_ms=T0 + 120_001, reconciliation_seq=3))
    assert coordinator.reservations.symbols == frozenset()


@pytest.mark.asyncio
async def test_same_version_conflicts_poison_account_and_venue_authority() -> None:
    coordinator = PaperRiskCoordinator(_settings())
    await coordinator.restore_reservations(())
    account = _account()
    venue = _venue()
    await coordinator.apply_account(account)
    await coordinator.apply_venue(venue)

    with pytest.raises(ValueError, match="AccountSnapshotV2"):
        await coordinator.apply_account(_account(equity_usd=9_999.0))
    assert coordinator.account is None

    with pytest.raises(ValueError, match="VenueQualityV1"):
        await coordinator.apply_venue(_venue(latency_ms=21))
    with pytest.raises(PaperInputUnavailable):
        await coordinator.evaluate(
            _review(),
            allocation=_allocation(),
            decided_at_ms=NOW,
            system_mode=SystemMode.NORMAL,
        )


def test_paper_runtime_rejects_memory_bus_and_live_authority() -> None:
    with pytest.raises(ValueError, match="durable"):
        RiskSettings(trading_mode=TradingMode.PAPER, bus_backend="memory")
    with pytest.raises(ValueError, match="LIVE is disabled"):
        RiskSettings(trading_mode=TradingMode.LIVE)
    with pytest.raises(ValueError, match="KAIROS_DRY_RUN=false"):
        RiskSettings(dry_run=False)
    with pytest.raises(ValueError, match="conflicts"):
        RiskSettings(trading_mode=TradingMode.PAPER, dry_run=True)


def test_retired_false_dry_run_environment_flag_fails_startup(monkeypatch) -> None:
    monkeypatch.setenv("KAIROS_DRY_RUN", "false")

    with pytest.raises(ValueError, match="KAIROS_DRY_RUN=false"):
        RiskSettings(_env_file=None)


@pytest.mark.asyncio
async def test_service_publishes_decision_before_acknowledging_review() -> None:
    service = RiskService(_settings())
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    await service.paper.apply_venue(_venue())
    service.strategic_allocation = _allocation()
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
    review = _review()
    bus = FakeBus(
        {Topics.CANDIDATE_REVIEW: [_envelope(Topics.CANDIDATE_REVIEW, review, envelope_id="review-1")]}
    )
    service.bus = bus

    await service._consume_paper_reviews()

    assert bus.events == [
        ("publish", Topics.RISK_TRADE_DECISION),
        ("ack", Topics.CANDIDATE_REVIEW),
    ]
    assert bus.published[0][1].approved
    assert bus.acks == [(Topics.CANDIDATE_REVIEW, "review-1")]
    await service.close()


@pytest.mark.asyncio
async def test_service_review_wakes_immediately_when_venue_quality_arrives() -> None:
    service = RiskService(_settings())
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    service.strategic_allocation = _allocation()
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
    review = _review()
    bus = FakeBus({Topics.CANDIDATE_REVIEW: [_envelope(Topics.CANDIDATE_REVIEW, review)]})
    service.bus = bus

    consumer = asyncio.create_task(service._consume_paper_reviews())
    await asyncio.sleep(0)

    assert bus.published == []
    assert bus.acks == []
    await service.paper.apply_venue(_venue())
    await asyncio.wait_for(consumer, timeout=1)

    assert bus.events == [
        ("publish", Topics.RISK_TRADE_DECISION),
        ("ack", Topics.CANDIDATE_REVIEW),
    ]
    assert bus.published[0][1].approved
    await service.close()


@pytest.mark.asyncio
async def test_service_acknowledges_terminal_review_when_inputs_miss_entry_deadline() -> None:
    service = RiskService(_settings())
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    review = _review()
    service._now_ms = lambda: review.intent.entry_expires_ts_ms + 1  # type: ignore[method-assign]
    bus = FakeBus(
        {Topics.CANDIDATE_REVIEW: [_envelope(Topics.CANDIDATE_REVIEW, review, envelope_id="late-1")]}
    )
    service.bus = bus

    await asyncio.wait_for(service._consume_paper_reviews(), timeout=1)

    assert bus.published == []
    assert bus.acks == [(Topics.CANDIDATE_REVIEW, "late-1")]
    assert bus.events == [("ack", Topics.CANDIDATE_REVIEW)]
    await service.close()


@pytest.mark.asyncio
async def test_paper_run_starts_only_strict_route_consumers(monkeypatch) -> None:
    service = RiskService(_settings())
    bus = FakeBus()
    service.bus = bus
    started: set[str] = set()

    def consumer(name: str):
        async def run() -> None:
            started.add(name)

        return run

    async def legacy_forbidden() -> None:
        raise AssertionError("PAPER started a legacy mutation consumer")

    monkeypatch.setattr(service, "_consume_health", consumer("health"))
    monkeypatch.setattr(service, "_consume_allocation", consumer("allocation"))
    monkeypatch.setattr(service, "_consume_paper_reviews", consumer("reviews"))
    monkeypatch.setattr(service, "_consume_paper_venue", consumer("venue"))
    monkeypatch.setattr(service, "_consume_paper_account", consumer("account-v2"))
    monkeypatch.setattr(service, "_recover_paper_state", consumer("recovery"))
    monkeypatch.setattr(service, "_consume_commands", legacy_forbidden)
    monkeypatch.setattr(service, "_consume_account", legacy_forbidden)

    await service.run()

    assert started == {
        "recovery",
        "health",
        "allocation",
        "reviews",
        "venue",
        "account-v2",
    }
    assert bus.closed


@pytest.mark.asyncio
async def test_restart_restores_reservation_before_admitting_same_symbol() -> None:
    first_runtime = PaperRiskCoordinator(_settings())
    await first_runtime.restore_reservations(())
    await first_runtime.apply_account(_account())
    await first_runtime.apply_venue(_venue())
    first = await first_runtime.evaluate(
        _review(),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )
    assert first.approved

    restarted = PaperRiskCoordinator(_settings())
    await restarted.restore_reservations((first,))
    await restarted.apply_account(_account(reconciliation_seq=2))
    await restarted.apply_venue(_venue())
    second = await restarted.evaluate(
        _review(intent=_intent(metadata=(("after", "restart"),))),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    assert restarted.recovery_complete
    assert not second.approved
    assert "symbol_has_reserved_idea" in second.rejection_reasons
    assert restarted.reservations.open_risk_usd == pytest.approx(first.worst_case_loss_usd)


@pytest.mark.asyncio
async def test_restart_restored_canary_reservation_blocks_a_different_symbol() -> None:
    first_runtime = PaperRiskCoordinator(_settings())
    await first_runtime.restore_reservations(())
    await first_runtime.apply_account(_account())
    await first_runtime.apply_venue(_venue())
    first = await first_runtime.evaluate(
        _review(),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    restarted = PaperRiskCoordinator(_settings())
    await restarted.restore_reservations((first,))
    await restarted.apply_account(_account(reconciliation_seq=2))
    await restarted.apply_venue(_venue(symbol="ETHUSD:DEV"))
    second = await restarted.evaluate(
        _review(intent=_intent(symbol="ETHUSDT", metadata=(("after", "restart-eth"),))),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    assert not second.approved
    assert "global_canary_idea_limit_reached" in second.rejection_reasons
    assert restarted.reservations.active_canary_ideas == 1


@pytest.mark.asyncio
async def test_unreconciled_snapshot_cannot_clear_a_recovered_reservation() -> None:
    first_runtime = PaperRiskCoordinator(_settings())
    await first_runtime.restore_reservations(())
    await first_runtime.apply_account(_account())
    await first_runtime.apply_venue(_venue())
    first = await first_runtime.evaluate(
        _review(),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )
    entry_order = OpenOrderSnapshotV2(
        venue_symbol=first.venue_symbol,
        client_order_id="entry-order-recovery-1",
        exchange_order_id="exchange-recovery-1",
        order_role=OrderRole.ENTRY,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.NEW,
        quantity=first.quantity,
        price=first.worst_entry_price,
        reduce_only=False,
        strategy_id=first.intent.strategy_id,
        strategy_revision=first.intent.strategy_revision,
        intent_id=first.intent.intent_id,
        risk_decision_id=first.decision_id,
        trade_id=first.trade_id,
        created_at_ms=NOW,
        updated_at_ms=NOW,
    )

    restarted = PaperRiskCoordinator(_settings())
    await restarted.restore_reservations((first,))
    await restarted.apply_account(
        _account(
            reconciliation_seq=2,
            captured_at_ms=NOW,
            reconciled=False,
            reconciliation_detail="partial DEV read",
            open_orders=(entry_order,),
        )
    )
    assert restarted.reservations.active_canary_ideas == 1
    assert not restarted.recovery_complete

    await restarted.apply_account(
        _account(
            reconciliation_seq=3,
            captured_at_ms=NOW + 1,
        )
    )
    await restarted.apply_venue(_venue())
    second = await restarted.evaluate(
        _review(intent=_intent(metadata=(("after", "untrusted-reconciliation"),))),
        allocation=_allocation(),
        decided_at_ms=NOW + 1,
        system_mode=SystemMode.NORMAL,
    )

    assert not second.approved
    assert "symbol_has_reserved_idea" in second.rejection_reasons


@pytest.mark.asyncio
async def test_conflicting_reconciled_lineage_poisons_authority_and_keeps_reservation() -> None:
    first_runtime = PaperRiskCoordinator(_settings())
    await first_runtime.restore_reservations(())
    await first_runtime.apply_account(_account())
    await first_runtime.apply_venue(_venue())
    first = await first_runtime.evaluate(
        _review(),
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )
    conflicting_position = _position(
        venue_symbol=first.venue_symbol,
        strategy_id="mislabelled-strategy",
        strategy_revision=first.intent.strategy_revision,
        intent_id=first.intent.intent_id,
        risk_decision_id=first.decision_id,
        trade_id=first.trade_id,
        exit_plan=first.exit_plan,
        timeout_at_ms=T0 + first.exit_plan.max_holding_ms,
    )

    restarted = PaperRiskCoordinator(_settings())
    await restarted.restore_reservations((first,))
    with pytest.raises(ValueError, match="durable risk lineage"):
        await restarted.apply_account(
            _account(
                reconciliation_seq=2,
                captured_at_ms=NOW,
                positions=(conflicting_position,),
                total_open_risk_usd=0.5,
            )
        )

    assert restarted.account is None
    assert not restarted.recovery_complete
    assert restarted.reservations.active_canary_ideas == 1

    await restarted.apply_account(_account(reconciliation_seq=3, captured_at_ms=NOW + 1))
    await restarted.apply_venue(_venue(symbol="ETHUSD:DEV"))
    second = await restarted.evaluate(
        _review(intent=_intent(symbol="ETHUSDT", metadata=(("after", "lineage-conflict"),))),
        allocation=_allocation(),
        decided_at_ms=NOW + 1,
        system_mode=SystemMode.NORMAL,
    )

    assert not second.approved
    assert "global_canary_idea_limit_reached" in second.rejection_reasons


@pytest.mark.asyncio
async def test_admission_is_blocked_until_durable_and_account_recovery_complete() -> None:
    coordinator = PaperRiskCoordinator(_settings())
    await coordinator.apply_venue(_venue())
    review = _review()

    with pytest.raises(PaperInputUnavailable, match="recovery"):
        await coordinator.evaluate(
            review,
            allocation=_allocation(),
            decided_at_ms=NOW,
            system_mode=SystemMode.NORMAL,
        )

    await coordinator.restore_reservations(())
    with pytest.raises(PaperInputUnavailable, match="recovery"):
        await coordinator.evaluate(
            review,
            allocation=_allocation(),
            decided_at_ms=NOW,
            system_mode=SystemMode.NORMAL,
        )

    await coordinator.apply_account(_account())
    decision = await coordinator.evaluate(
        review,
        allocation=_allocation(),
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )
    assert decision.approved
