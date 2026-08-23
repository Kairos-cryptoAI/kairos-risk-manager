"""Replay, race and service-boundary checks for PAPER correlation state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from kairos_core.bus import BusEnvelope
from kairos_core.contracts import CandidateReviewV1, OpenOrderSnapshotV2
from kairos_core.enums import (
    MarketRegime,
    OrderRole,
    OrderSide,
    OrderStatus,
    OrderType,
    Side,
    SystemMode,
    TradingMode,
)
from kairos_core.topics import Topics

from kairos_risk.canary import CanaryInputs, CanaryPlan, prepare_canary
from kairos_risk.canary_authorization import (
    PaperCanaryArmRecord,
    canary_arm_identity,
)
from kairos_risk.config import RiskSettings
from kairos_risk.paper_runtime import PaperInputUnavailable, PaperRiskCoordinator
from kairos_risk.service import RiskService
from tests.test_canary import FakePaperCanaryArm
from tests.test_canary import _bar as _canary_bar
from tests.test_canary import _instrument as _canary_instrument
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


@dataclass
class FakeCanaryArmRepository:
    result: PaperCanaryArmRecord | None
    consumes: list[tuple[str, str | None]] = field(default_factory=list)

    async def consume(
        self,
        *,
        account_id: str,
        review: CandidateReviewV1,
    ) -> PaperCanaryArmRecord | None:
        self.consumes.append((account_id, review.review_id))
        return self.result

    async def arm(self, **_kwargs: object) -> PaperCanaryArmRecord:
        raise AssertionError("Risk PAPER repository attempted to arm a canary")


def _armed_canary(
    *,
    target_distance_bps: float = 75.0,
) -> tuple[CandidateReviewV1, PaperCanaryArmRecord]:
    prepared = prepare_canary(
        CanaryPlan(
            symbol="BTCUSDT",
            side=Side.LONG,
            target_distance_bps=target_distance_bps,
        ),
        CanaryInputs(
            bar=_canary_bar(),
            venue=_venue(),
            account=_account(),
            instrument=_canary_instrument(),
        ),
        account_id="kairos-paper-dev-01",
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    return prepared.review, FakePaperCanaryArm(
        arm_id=canary_arm_identity(
            account_id="kairos-paper-dev-01",
            review=prepared.review,
            allocation=prepared.allocation,
        ),
        account_id="kairos-paper-dev-01",
        review=prepared.review,
        allocation=prepared.allocation,
        status="CONSUMED",
        expires_at=datetime.fromtimestamp(prepared.review.intent.entry_expires_ts_ms / 1_000, tz=UTC),
        decided_at_ms=NOW,
    )


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
    first_review, first_arm = _armed_canary()
    first = await coordinator.evaluate(
        first_review,
        allocation=first_arm.allocation,
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    second_review, second_arm = _armed_canary(target_distance_bps=80.0)
    second = await coordinator.evaluate(
        second_review,
        allocation=second_arm.allocation,
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
    review, arm = _armed_canary()
    repository = FakeCanaryArmRepository(arm)
    service = RiskService(_settings(), paper_canary_repository=repository)
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    await service.paper.apply_venue(_venue())
    service.strategic_allocation = _allocation(regime=MarketRegime.CHOP)
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
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
    assert repository.consumes == [("kairos-paper-dev-01", review.review_id)]
    assert bus.acks == [(Topics.CANDIDATE_REVIEW, "review-1")]
    await service.close()


@pytest.mark.asyncio
async def test_service_review_wakes_immediately_when_venue_quality_arrives() -> None:
    review, arm = _armed_canary()
    service = RiskService(
        _settings(),
        paper_canary_repository=FakeCanaryArmRepository(arm),
    )
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
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
async def test_direct_bus_canary_allow_without_durable_arm_is_explicitly_rejected() -> None:
    review, _arm = _armed_canary()
    repository = FakeCanaryArmRepository(None)
    service = RiskService(_settings(), paper_canary_repository=repository)
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    await service.paper.apply_venue(_venue())
    service.strategic_allocation = _allocation()
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
    bus = FakeBus({Topics.CANDIDATE_REVIEW: [_envelope(Topics.CANDIDATE_REVIEW, review)]})
    service.bus = bus

    await service._consume_paper_reviews()

    decision = bus.published[0][1]
    assert not decision.approved
    assert "technical_canary_arm_missing" in decision.rejection_reasons
    assert repository.consumes == [("kairos-paper-dev-01", review.review_id)]
    assert bus.events == [
        ("publish", Topics.RISK_TRADE_DECISION),
        ("ack", Topics.CANDIDATE_REVIEW),
    ]
    await service.close()


@pytest.mark.asyncio
async def test_invalid_durable_canary_arm_is_rejected_and_review_is_acknowledged() -> None:
    review, arm = _armed_canary()
    repository = FakeCanaryArmRepository(replace(arm, status="ARMED"))
    service = RiskService(_settings(), paper_canary_repository=repository)
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    await service.paper.apply_venue(_venue())
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
    bus = FakeBus({Topics.CANDIDATE_REVIEW: [_envelope(Topics.CANDIDATE_REVIEW, review)]})
    service.bus = bus

    await service._consume_paper_reviews()

    decision = bus.published[0][1]
    assert not decision.approved
    assert "technical_canary_arm_invalid" in decision.rejection_reasons
    assert bus.events == [
        ("publish", Topics.RISK_TRADE_DECISION),
        ("ack", Topics.CANDIDATE_REVIEW),
    ]
    await service.close()


@pytest.mark.asyncio
async def test_missing_arm_cannot_reuse_an_approved_in_process_cache_entry() -> None:
    review, arm = _armed_canary()
    repository = FakeCanaryArmRepository(arm)
    service = RiskService(_settings(), paper_canary_repository=repository)
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    await service.paper.apply_venue(_venue())
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
    bus = FakeBus()
    service.bus = bus

    await service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review))
    repository.result = None
    await service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review, envelope_id="replay"))

    assert bus.published[0][1].approved
    assert not bus.published[1][1].approved
    assert "technical_canary_arm_missing" in bus.published[1][1].rejection_reasons
    assert repository.consumes == [
        ("kairos-paper-dev-01", review.review_id),
        ("kairos-paper-dev-01", review.review_id),
    ]
    await service.close()


@pytest.mark.asyncio
async def test_consumed_arm_and_decision_replay_identically_after_risk_restart() -> None:
    review, arm = _armed_canary()
    first_repository = FakeCanaryArmRepository(arm)
    first_service = RiskService(_settings(), paper_canary_repository=first_repository)
    await first_service.paper.restore_reservations(())
    await first_service.paper.apply_account(_account())
    await first_service.paper.apply_venue(_venue())
    first_service._now_ms = lambda: NOW  # type: ignore[method-assign]
    first_bus = FakeBus()
    first_service.bus = first_bus
    await first_service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review))
    first_decision = first_bus.published[0][1]
    assert first_decision.approved

    replay_repository = FakeCanaryArmRepository(arm)
    restarted = RiskService(_settings(), paper_canary_repository=replay_repository)
    await restarted.paper.restore_reservations((first_decision,))
    await restarted.paper.apply_account(_account(reconciliation_seq=2))
    await restarted.paper.apply_venue(_venue())
    restarted._now_ms = lambda: NOW  # type: ignore[method-assign]
    replay_bus = FakeBus()
    restarted.bus = replay_bus
    await restarted._handle_paper_review(
        _envelope(Topics.CANDIDATE_REVIEW, review, envelope_id="after-restart")
    )

    assert replay_bus.published[0][1].to_json() == first_decision.to_json()
    assert replay_repository.consumes == [("kairos-paper-dev-01", review.review_id)]
    await first_service.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_generic_promoted_strategy_keeps_macro_allocation_path_and_never_claims_canary_arm() -> None:
    repository = FakeCanaryArmRepository(None)
    settings = _settings(paper_strategy_allowlist=["promoted-alpha@1"])
    service = RiskService(settings, paper_canary_repository=repository)
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    await service.paper.apply_venue(_venue())
    service.strategic_allocation = _allocation(strategy_id="promoted-alpha")
    service._now_ms = lambda: NOW  # type: ignore[method-assign]
    review = _review(intent=_intent(strategy_id="promoted-alpha", strategy_revision="1"))
    bus = FakeBus({Topics.CANDIDATE_REVIEW: [_envelope(Topics.CANDIDATE_REVIEW, review)]})
    service.bus = bus

    await service._consume_paper_reviews()

    assert bus.published[0][1].approved
    assert repository.consumes == []
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
    first_review, first_arm = _armed_canary()
    first = await first_runtime.evaluate(
        first_review,
        allocation=first_arm.allocation,
        decided_at_ms=NOW,
        system_mode=SystemMode.NORMAL,
    )

    restarted = PaperRiskCoordinator(_settings())
    await restarted.restore_reservations((first,))
    await restarted.apply_account(_account(reconciliation_seq=2))
    await restarted.apply_venue(_venue())
    second_review, second_arm = _armed_canary(target_distance_bps=80.0)
    second = await restarted.evaluate(
        second_review,
        allocation=second_arm.allocation,
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
    first_review, first_arm = _armed_canary()
    first = await first_runtime.evaluate(
        first_review,
        allocation=first_arm.allocation,
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
    first_review, first_arm = _armed_canary()
    first = await first_runtime.evaluate(
        first_review,
        allocation=first_arm.allocation,
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
    await restarted.apply_venue(_venue())
    second_review, second_arm = _armed_canary(target_distance_bps=80.0)
    second = await restarted.evaluate(
        second_review,
        allocation=second_arm.allocation,
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
