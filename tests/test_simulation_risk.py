"""SIM-only admission policy tests; no external service is involved."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from kairos_core.contracts import (
    CandidateReviewV1,
    CandidateRouteV1,
    ExitPlanV1,
    RecordedBookLevelV1,
    RecordedTopNBookFrameV1,
    SimulationAssumptionsV1,
    SimulationSessionV1,
    SimulationStrategyRefV1,
    StrategyIntentV1,
    StrategyProvenanceV1,
    canonical_sha256,
)
from kairos_core.enums import CandidateReviewTier, ReasoningEffort, ReviewDecision, Side

from kairos_risk.simulation import SimulationRiskPolicy

T0 = 1_800_000_000_000
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _intent(**overrides: object) -> StrategyIntentV1:
    values: dict[str, object] = {
        "source": "strategy-engine",
        "strategy_id": "sim-test-strategy",
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
    reviewed_at_ms: int = T0 + 60_100,
) -> CandidateReviewV1:
    candidate = intent or _intent()
    route = CandidateRouteV1(
        source="router",
        intent=candidate,
        review_tier=CandidateReviewTier.NORMAL,
        requested_reasoning_effort=ReasoningEffort.MEDIUM,
        routed_at_ms=T0 + 59_999,
        review_deadline_ms=T0 + 119_000,
    )
    return CandidateReviewV1(
        source="aggregator",
        route=route,
        intent=candidate,
        decision=decision,
        priority=50,
        reviewed_at_ms=reviewed_at_ms,
        reviewer="DETERMINISTIC",
        reason_codes=(decision.value,),
    )


def _session(**overrides: object) -> SimulationSessionV1:
    values: dict[str, object] = {
        "source": "simulator-controller",
        "tape_id": "sim-tape",
        "tape_sha256": "e" * 64,
        "assumptions": SimulationAssumptionsV1(
            latency_ms=25,
            maximum_book_age_ms=5_000,
            maximum_frame_latency_ms=1_000,
            depth_participation_fraction=0.1,
            adverse_slippage_bps=2.0,
            taker_fee_bps=5.0,
            price_tick=0.1,
            quantity_step=0.001,
        ),
        "strategy_allowlist": (
            SimulationStrategyRefV1(strategy_id="sim-test-strategy", strategy_revision="1"),
        ),
        "started_at_ms": T0,
        "ends_at_ms": T0 + 360_000,
    }
    values.update(overrides)
    return SimulationSessionV1(**values)


def _frame(**overrides: object) -> RecordedTopNBookFrameV1:
    values: dict[str, object] = {
        "source": "quant-scouts",
        "tape_id": "sim-tape",
        "stream_epoch": "epoch-1",
        "symbol": "BTCUSDT",
        "tape_sequence": 1,
        "exchange_update_id": 1,
        "exchange_at_ms": T0 + 60_000,
        "received_at_ms": T0 + 60_010,
        "persisted_at_ms": T0 + 60_020,
        "raw_payload_sha256": "f" * 64,
        "continuity": "ADMITTED",
        "bids": (RecordedBookLevelV1(price=99.9, quantity=2.0),),
        "asks": (RecordedBookLevelV1(price=100.1, quantity=2.0),),
    }
    values.update(overrides)
    return RecordedTopNBookFrameV1(**values)


def _evaluate(**overrides: object):
    values: dict[str, object] = {
        "session": _session(),
        "review": _review(),
        "selected_book_frame": _frame(),
        "decided_at_ms": T0 + 60_100,
        "requested_quantity": 0.25,
    }
    values.update(overrides)
    return SimulationRiskPolicy().evaluate(**values)


def test_admission_is_byte_stable_and_caps_visible_participation() -> None:
    first = _evaluate()
    replay = _evaluate()

    assert first.approved
    assert first.quantity == pytest.approx(0.2)
    assert first.price_cap == pytest.approx(100.2)
    assert first.decision_id == replay.decision_id
    assert first.model_dump(mode="json") == replay.model_dump(mode="json")
    assert first.execution_environment == "SIMULATED"
    assert not first.paper_qualification_eligible
    assert not first.trial15_eligible
    assert not first.alpha_claim


def test_short_admission_uses_bid_side_and_conservative_downward_cap() -> None:
    intent = _intent(
        side=Side.SHORT,
        exit_plan=ExitPlanV1(stop_price=105.0, target_price=95.0, max_holding_ms=180_000),
    )
    decision = _evaluate(review=_review(intent=intent))

    assert decision.approved
    assert decision.quantity == pytest.approx(0.2)
    assert decision.price_cap == pytest.approx(99.8)


def test_price_cap_that_reaches_target_is_rejected() -> None:
    intent = _intent(
        gross_reward_bps=20.0,
        exit_plan=ExitPlanV1(stop_price=95.0, target_price=100.2, max_holding_ms=180_000),
    )
    decision = _evaluate(review=_review(intent=intent))

    assert not decision.approved
    assert "PRICE_CAP_EXIT_GEOMETRY_INVALID" in decision.rejection_reasons


@pytest.mark.parametrize(
    ("review_decision", "expected_reason"),
    [
        (ReviewDecision.VETO, "REVIEW_VETO"),
        (ReviewDecision.DEFER, "REVIEW_DEFER"),
    ],
)
def test_non_allow_review_is_durable_rejected_evidence(
    review_decision: ReviewDecision,
    expected_reason: str,
) -> None:
    decision = _evaluate(review=_review(decision=review_decision))

    assert not decision.approved
    assert decision.quantity == 0
    assert decision.price_cap is None
    assert expected_reason in decision.rejection_reasons


@pytest.mark.parametrize(
    ("frame", "expected_reason"),
    [
        (None, "NO_RECORDED_BOOK_FRAME"),
        (_frame(tape_id="other-tape"), "BOOK_FRAME_TAPE_MISMATCH"),
        (_frame(symbol="ETHUSDT"), "BOOK_FRAME_SYMBOL_MISMATCH"),
        (_frame(continuity="GAP"), "BOOK_FRAME_NOT_ADMITTED"),
        (_frame(persisted_at_ms=T0 + 60_200), "BOOK_FRAME_FROM_FUTURE"),
        (
            _frame(exchange_at_ms=T0 + 59_000, received_at_ms=T0 + 59_100, persisted_at_ms=T0 + 60_020),
            "BOOK_FRAME_LATENCY_EXCEEDED",
        ),
    ],
)
def test_unusable_book_frame_is_rejected_without_synthetic_fill(
    frame: RecordedTopNBookFrameV1 | None,
    expected_reason: str,
) -> None:
    decision = _evaluate(selected_book_frame=frame)

    assert not decision.approved
    assert decision.quantity == 0
    assert decision.price_cap is None
    assert expected_reason in decision.rejection_reasons


def test_environment_mismatches_cannot_become_simulator_approvals() -> None:
    """Even a manually forged model instance remains a non-authorizing record."""

    forged_session = _session().model_copy(update={"execution_environment": "PAPER"})
    forged_session = forged_session.model_copy(
        update={"session_id": canonical_sha256(forged_session.identity_payload())}
    )
    session_decision = _evaluate(session=forged_session)

    forged_frame = _frame().model_copy(update={"execution_environment": "PAPER"})
    forged_frame = forged_frame.model_copy(
        update={"frame_sha256": canonical_sha256(forged_frame.identity_payload())}
    )
    frame_decision = _evaluate(selected_book_frame=forged_frame)

    for decision, expected_reason in (
        (session_decision, "SESSION_ENVIRONMENT_MISMATCH"),
        (frame_decision, "BOOK_FRAME_ENVIRONMENT_MISMATCH"),
    ):
        assert not decision.approved
        assert decision.quantity == 0
        assert decision.price_cap is None
        assert expected_reason in decision.rejection_reasons


def test_stale_and_expired_inputs_remain_rejected_evidence() -> None:
    stale = _evaluate(decided_at_ms=T0 + 65_021)
    expired = _evaluate(
        selected_book_frame=_frame(
            exchange_at_ms=T0 + 120_000,
            received_at_ms=T0 + 120_010,
            persisted_at_ms=T0 + 120_020,
        ),
        decided_at_ms=T0 + 120_021,
    )

    assert not stale.approved
    assert "BOOK_FRAME_STALE" in stale.rejection_reasons
    assert not expired.approved
    assert "INTENT_EXPIRED" in expired.rejection_reasons


@pytest.mark.parametrize("requested_quantity", [0.0, -1.0, float("nan"), float("inf"), 0.0001])
def test_invalid_or_substep_requested_quantity_cannot_admit(requested_quantity: float) -> None:
    decision = _evaluate(requested_quantity=requested_quantity)

    assert not decision.approved
    assert decision.quantity == 0
    assert decision.price_cap is None
    assert set(decision.rejection_reasons) & {
        "REQUESTED_QUANTITY_INVALID",
        "REQUESTED_QUANTITY_NOT_EXECUTABLE",
    }


def test_non_allowlisted_strategy_is_preserved_as_rejected_evidence() -> None:
    decision = _evaluate(
        session=_session(
            strategy_allowlist=(
                SimulationStrategyRefV1(strategy_id="other-strategy", strategy_revision="1"),
            ),
        )
    )

    assert not decision.approved
    assert "STRATEGY_NOT_SESSION_ALLOWLISTED" in decision.rejection_reasons


def test_pre_eligibility_cannot_be_retimestamped_as_a_decision() -> None:
    review = _review(reviewed_at_ms=T0 + 59_999)

    with pytest.raises(ValueError, match="next-bar eligibility"):
        _evaluate(review=review, decided_at_ms=T0 + 59_999)


def test_policy_module_has_no_non_simulator_import_path() -> None:
    module = Path(__file__).parents[1] / "kairos_risk" / "simulation.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name.lower() for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported_modules.update(
        (node.module or "").lower() for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    imported_names = {
        alias.name.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert not any("paper" in module_name or "execution" in module_name for module_name in imported_modules)
    assert "tradingmode" not in imported_names
    assert "risktradedecisionv1" not in imported_names
