"""Offline engineering fixtures for the opted-in real PAPER risk policy path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from kairos_core.bus import BusEnvelope
from kairos_core.contracts import ExitPlanV1, StrategyIntentV1
from kairos_core.contracts.regime_capability import (
    REGIME_BOUND_ALLOCATION_TOPIC,
    CapabilityRegime,
    RegimeBoundAllocationV1,
    RegimeCapabilityPolicyV1,
    RegimeCapitalBasisV1,
    RegimeObservationV1,
    StrategyRegimeCapabilityV1,
)
from kairos_core.enums import MarketRegime, ReviewDecision, Side, SystemMode
from pydantic import ValidationError

from kairos_risk.config import RiskSettings
from kairos_risk.paper import PaperRiskPipeline
from kairos_risk.paper_runtime import PaperInputDeadlineExceeded, PaperRiskCoordinator
from kairos_risk.service import RiskService
from tests.test_paper import (
    NOW,
    SHA_A,
    SHA_B,
    SHA_C,
    SHA_D,
    T0,
    _account,
    _allocation,
    _intent,
    _review,
    _settings,
    _venue,
)


def _policy() -> RegimeCapabilityPolicyV1:
    return RegimeCapabilityPolicyV1(
        source_set_sha256=SHA_C,
        observation_source="engineering-detector-fixture",
        detector_code_sha256=SHA_D,
        detector_config_sha256=SHA_A,
        capabilities=tuple(
            StrategyRegimeCapabilityV1(
                strategy_id="paper-test-strategy",
                strategy_revision="1",
                strategy_code_sha256=SHA_A,
                config_sha256=SHA_B,
                regime=regime,
                sides=sides,
            )
            for regime, sides in (
                (CapabilityRegime.RANGE, (Side.LONG, Side.SHORT)),
                (CapabilityRegime.BULL, (Side.LONG,)),
                (CapabilityRegime.BEAR, (Side.SHORT,)),
            )
        ),
    )


def _adaptive_settings(tmp_path, **changes) -> RiskSettings:
    policy = _policy()
    artifact = tmp_path / "engineering-regime-policy.json"
    artifact.write_text(policy.model_dump_json(), encoding="utf-8")
    values = dict(
        paper_regime_policy_profile="adaptive-research-v1",
        paper_regime_policy_file=artifact,
        paper_regime_policy_sha256=policy.policy_sha256,
        paper_regime_source_set_sha256=SHA_C,
    )
    values.update(changes)
    return _settings(**values)


def _bound(intent: StrategyIntentV1 | None = None, regime: CapabilityRegime = CapabilityRegime.RANGE):
    intent = intent or _intent()
    capital_account = _account(captured_at_ms=T0 + 58_000)
    capital = _allocation(
        regime=MarketRegime.CHOP, produced_at=datetime.fromtimestamp((T0 + 59_000) / 1000, UTC)
    )
    capital.source = "kairos-macro-strategist"
    capital.message_id = "macro:engineering-fixture"
    return RegimeBoundAllocationV1(
        source="kairos-macro-strategist",
        bound_at_ms=T0 + 60_200,
        capital_basis=RegimeCapitalBasisV1(
            allocation=capital,
            policy_sha256=_policy().policy_sha256,
            source_set_sha256=SHA_C,
            account_id=capital_account.account_id,
            account_snapshot_id=capital_account.snapshot_id,
            account_captured_at_ms=capital_account.captured_at_ms,
        ),
        observation=RegimeObservationV1(
            source="engineering-detector-fixture",
            source_set_sha256=SHA_C,
            detector_code_sha256=SHA_D,
            detector_config_sha256=SHA_A,
            intent=intent,
            regime=regime,
            event_as_of_ms=intent.decision_ts_ms,
            observed_at_ms=T0 + 60_000,
            expires_at_ms=intent.entry_expires_ts_ms,
        ),
    )


def _evaluate(pipeline, *, intent=None, bound=None, review=None, account=None, venue=None):
    return pipeline.evaluate(
        review or _review(intent=intent),
        venue or _venue(),
        account=account or _account(),
        allocation=_allocation(regime=MarketRegime.CHOP),
        decided_at_ms=NOW,
        regime_allocation=bound,
    )


def test_legacy_default_ignores_capability_artifacts_and_keeps_blanket_chop_veto():
    pipeline = PaperRiskPipeline(_settings())
    decision = _evaluate(pipeline, bound=_bound())
    assert not decision.approved and "macro_regime_chop" in decision.rejection_reasons
    assert pipeline.regime_policy is None


def test_default_empty_strategy_allowlist_still_rejects_even_a_valid_bound_range_fixture():
    decision = _evaluate(PaperRiskPipeline(_settings(paper_strategy_allowlist=[])), bound=_bound())
    assert "strategy_not_paper_approved" in decision.rejection_reasons


def test_opt_in_is_explicit_and_requires_independently_frozen_inputs(tmp_path):
    with pytest.raises(ValidationError):
        _settings(paper_regime_policy_profile="adaptive-research-v1")
    with pytest.raises(ValidationError):
        _settings(paper_regime_policy_file=tmp_path / "unused.json")
    with pytest.raises(ValueError):
        PaperRiskPipeline(_adaptive_settings(tmp_path, paper_regime_policy_sha256=SHA_A))
    with pytest.raises(ValueError):
        PaperRiskPipeline(_adaptive_settings(tmp_path, paper_strategy_allowlist=["paper-test-strategy@2"]))


def test_opted_in_range_fixture_has_same_loss_and_portfolio_ceilings(tmp_path):
    pipeline = PaperRiskPipeline(_adaptive_settings(tmp_path))
    decision = _evaluate(pipeline, bound=_bound())
    assert decision.approved
    assert decision.loss_budget_usd == 25
    assert decision.worst_case_loss_usd <= 10_000 * 0.0025
    assert decision.leverage == 1
    assert pipeline.settings.paper_max_total_open_risk_fraction == 0.01
    assert decision.causation_id == _bound().message_id


def test_missing_bound_evidence_cannot_fall_back_to_positive_llm_weight(tmp_path):
    decision = _evaluate(PaperRiskPipeline(_adaptive_settings(tmp_path)))
    assert "regime_bound_allocation_missing" in decision.rejection_reasons


@pytest.mark.parametrize(
    "field,value",
    [
        ("strategy_revision", "2"),
        ("strategy_id", "range_mean_reversion_v1"),
    ],
)
def test_unknown_or_rejected_revision_cannot_be_promoted_by_mapping_or_weight(tmp_path, field, value):
    intent = _intent(**{field: value})
    decision = _evaluate(PaperRiskPipeline(_adaptive_settings(tmp_path)), intent=intent, bound=_bound(intent))
    assert not decision.approved
    assert "strategy_not_paper_approved" in decision.rejection_reasons
    assert "regime_bound_allocation_invalid" in decision.rejection_reasons


@pytest.mark.parametrize(
    "regime,side,allowed",
    [
        (CapabilityRegime.BULL, Side.LONG, True),
        (CapabilityRegime.BULL, Side.SHORT, False),
        (CapabilityRegime.BEAR, Side.SHORT, True),
        (CapabilityRegime.BEAR, Side.LONG, False),
        (CapabilityRegime.CRASH, Side.LONG, False),
        (CapabilityRegime.UNCERTAIN, Side.LONG, False),
    ],
)
def test_adaptive_bull_bear_handling_requires_exact_regime_and_side(tmp_path, regime, side, allowed):
    intent = _intent(
        side=side,
        exit_plan=ExitPlanV1(
            stop_price=95 if side is Side.LONG else 105,
            target_price=105 if side is Side.LONG else 95,
            max_holding_ms=180_000,
        ),
    )
    decision = _evaluate(
        PaperRiskPipeline(_adaptive_settings(tmp_path)), intent=intent, bound=_bound(intent, regime)
    )
    assert decision.approved is allowed


def test_adaptive_policy_does_not_bypass_review_venue_or_reconciliation(tmp_path):
    pipeline = PaperRiskPipeline(_adaptive_settings(tmp_path))
    for changes, reason in (
        ({"review": _review(decision=ReviewDecision.VETO)}, "review_veto"),
        ({"account": _account(reconciled=False)}, "account_not_reconciled"),
        ({"venue": _venue(entry_allowed=False, reason_codes=("blocked",))}, "venue_entry_blocked"),
    ):
        decision = _evaluate(pipeline, bound=_bound(), **changes)
        assert not decision.approved and reason in decision.rejection_reasons


def test_nested_capital_mutation_is_rejected_at_real_risk_admission(tmp_path):
    bound = _bound()
    bound.capital_basis.allocation.strategy_weights["paper-test-strategy"] = 0.7
    decision = _evaluate(PaperRiskPipeline(_adaptive_settings(tmp_path)), bound=bound)
    assert "regime_bound_allocation_invalid" in decision.rejection_reasons


def test_future_or_foreign_policy_and_account_boundaries_fail_closed(tmp_path):
    pipeline = PaperRiskPipeline(_adaptive_settings(tmp_path))
    for bound in (
        _bound().model_copy(update={"bound_at_ms": NOW + 1}),
        _bound().model_copy(
            update={"capital_basis": _bound().capital_basis.model_copy(update={"account_id": "foreign"})}
        ),
        _bound().model_copy(
            update={"capital_basis": _bound().capital_basis.model_copy(update={"policy_sha256": SHA_A})}
        ),
    ):
        assert "regime_bound_allocation_invalid" in _evaluate(pipeline, bound=bound).rejection_reasons


@pytest.mark.asyncio
async def test_real_subscriber_handler_correlates_binding_and_wakes_review_without_legacy_allocation(
    tmp_path,
):
    settings = _adaptive_settings(tmp_path)
    coordinator = PaperRiskCoordinator(settings)
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    waiter = asyncio.create_task(coordinator.wait_for_inputs(_review(), now_ms=lambda: NOW))
    await asyncio.sleep(0)
    assert not waiter.done()
    service = object.__new__(RiskService)
    service.paper = coordinator
    service._now_ms = lambda: NOW
    bound = _bound()
    await service._handle_regime_allocation(
        BusEnvelope(id="engineering", topic=REGIME_BOUND_ALLOCATION_TOPIC, payload=bound.to_payload())
    )
    await asyncio.wait_for(waiter, timeout=1)
    decision = await coordinator.evaluate(
        _review(), allocation=None, decided_at_ms=NOW, system_mode=SystemMode.NORMAL
    )
    assert decision.approved
    assert decision.causation_id == bound.message_id


@pytest.mark.asyncio
async def test_conflicting_binding_revokes_cached_approval_and_future_receipt_rejects(tmp_path):
    coordinator = PaperRiskCoordinator(_adaptive_settings(tmp_path, paper_decision_cache_size=1))
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    with pytest.raises(ValueError, match="trusted local receipt"):
        await coordinator.apply_regime_allocation(_bound(), received_at_ms=T0 + 60_199)
    await coordinator.apply_regime_allocation(_bound(), received_at_ms=NOW)
    approved = await coordinator.evaluate(
        _review(), allocation=None, decided_at_ms=NOW, system_mode=SystemMode.NORMAL
    )
    assert approved.approved
    with pytest.raises(ValueError, match="conflicting"):
        await coordinator.apply_regime_allocation(_bound(regime=CapabilityRegime.BULL), received_at_ms=NOW)
    # Another unadmitted intent cannot evict the poison attached to a cached,
    # reserved approval even with the smallest configured replay cache.
    await coordinator.apply_regime_allocation(_bound(_intent(symbol="ETHUSDT")), received_at_ms=NOW)
    rejected = await coordinator.evaluate(
        _review(), allocation=None, decided_at_ms=NOW, system_mode=SystemMode.NORMAL
    )
    assert not rejected.approved and "regime_bound_allocation_conflict" in rejected.rejection_reasons


@pytest.mark.asyncio
async def test_missing_adaptive_evidence_has_existing_bounded_expiry_not_legacy_fallback(tmp_path):
    coordinator = PaperRiskCoordinator(_adaptive_settings(tmp_path))
    await coordinator.restore_reservations(())
    await coordinator.apply_account(_account())
    await coordinator.apply_venue(_venue())
    with pytest.raises(PaperInputDeadlineExceeded):
        await coordinator.wait_for_inputs(_review(), now_ms=lambda: T0 + 120_000)


@pytest.mark.asyncio
async def test_versioned_risk_handler_rejects_wrong_topic_without_granting_a_binding(tmp_path):
    service = object.__new__(RiskService)
    service.paper = PaperRiskCoordinator(_adaptive_settings(tmp_path))
    service._now_ms = lambda: NOW
    with pytest.raises(ValueError, match="wrong versioned topic"):
        await service._handle_regime_allocation(
            BusEnvelope(id="wrong", topic="kairos.macro.allocation", payload=_bound().to_payload()),
        )
    assert not service.paper._regime_allocations


def test_validly_hashed_foreign_account_cannot_pass_the_adaptive_scope_gate(tmp_path):
    payload = _bound().model_dump(mode="json")
    payload.pop("allocation_id")
    payload["capital_basis"].pop("capital_basis_id")
    payload["capital_basis"]["account_id"] = "foreign-paper-dev"
    bound = RegimeBoundAllocationV1.model_validate(payload)
    assert (
        "regime_bound_allocation_invalid"
        in _evaluate(
            PaperRiskPipeline(_adaptive_settings(tmp_path)),
            bound=bound,
        ).rejection_reasons
    )


def test_rejected_strategy_in_artifact_cannot_be_qualified_even_if_accidentally_allowlisted(tmp_path):
    settings = _adaptive_settings(tmp_path)
    payload = _policy().model_dump(mode="json")
    for capability in payload["capabilities"]:
        capability["strategy_id"] = "range_mean_reversion_v1"
    policy = RegimeCapabilityPolicyV1.model_validate(payload)
    settings.paper_regime_policy_file.write_text(policy.model_dump_json(), encoding="utf-8")
    rejected_settings = _settings(
        paper_regime_policy_profile="adaptive-research-v1",
        paper_regime_policy_file=settings.paper_regime_policy_file,
        paper_regime_policy_sha256=policy.policy_sha256,
        paper_regime_source_set_sha256=SHA_C,
        paper_strategy_allowlist=["range_mean_reversion_v1@1"],
    )
    with pytest.raises(ValueError, match="unknown/unapproved"):
        PaperRiskPipeline(rejected_settings)
