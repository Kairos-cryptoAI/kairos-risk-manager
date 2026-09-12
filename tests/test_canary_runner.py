from __future__ import annotations

import pytest
from kairos_persistence.canary_session import (
    BoundedCanaryPlan,
    CanaryAdmissionError,
    CanaryScope,
    CanarySlot,
    validate_slot_review,
)

from kairos_risk.canary import CANARY_ARM_PHRASE, CanaryArmingError, CanaryPlan, CanarySession, prepare_canary
from kairos_risk.canary_runner import check_runtime_scope, next_slot, parser, run_cli, safe_status
from kairos_risk.config import RiskSettings
from tests.test_canary import ACCOUNT_ID, NOW, FakeArmRepository, FakeSource, _inputs


def plan() -> BoundedCanaryPlan:
    return BoundedCanaryPlan(
        slots=tuple(
            CanarySlot(slot_id=f"slot-{index + 1}", symbol=symbol, side="LONG", scenario=scenario)
            for index, (symbol, scenario) in enumerate(
                zip(
                    ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"),
                    ("STOP", "TARGET", "TIMEOUT", "RESTART", "ENTRY_CANCEL"),
                    strict=True,
                )
            )
        )
    )


def test_runner_plan_matches_existing_immutable_generator_and_exit_plan() -> None:
    status = {
        "plan": plan().model_dump(mode="json"),
        "attempts_reserved": 0,
        "state": "ARMED",
        "attempts": [],
    }
    slot_id, chosen = next_slot(status)
    assert slot_id == "slot-1"
    prepared = prepare_canary(
        chosen,
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    validate_slot_review(plan().slots[0], prepared.review)
    wrong = plan().slots[0].model_copy(update={"max_holding_ms": 60_000})
    with pytest.raises(CanaryAdmissionError):
        validate_slot_review(wrong, prepared.review)


def test_restart_chooses_only_next_unreserved_slot_and_pending_blocks() -> None:
    status = {
        "plan": plan().model_dump(mode="json"),
        "attempts_reserved": 1,
        "state": "RUNNING",
        "attempts": [{"state": "TERMINAL"}],
    }
    assert next_slot(status)[0] == "slot-2"
    status["attempts"][0]["state"] = "CONSUMED"
    with pytest.raises(CanaryAdmissionError, match="awaits"):
        next_slot(status)
    status["state"] = "DRAINING"
    with pytest.raises(CanaryAdmissionError, match="not admitting"):
        next_slot(status)


def test_exhausted_slots_never_repeat_for_missing_coverage() -> None:
    status = {
        "plan": plan().model_dump(mode="json"),
        "attempts_reserved": 5,
        "state": "RUNNING",
        "attempts": [{"state": "TERMINAL"}] * 5,
    }
    assert next_slot(status) is None
    status["attempts_reserved"] = 4
    with pytest.raises(CanaryAdmissionError, match="counter"):
        next_slot(status)


@pytest.mark.asyncio
async def test_unbounded_publish_fails_before_loading_state_or_calls() -> None:
    source, arms = FakeSource(_inputs()), FakeArmRepository()
    with pytest.raises(CanaryArmingError, match="session_id"):
        await CanarySession(source, arms).run(
            CanaryPlan(symbol="BTCUSDT", side=plan().slots[0].side),
            account_id=ACCOUNT_ID,
            now_ms=NOW,
            publish=True,
            arm=CANARY_ARM_PHRASE,
        )
    assert source.calls == 0 and arms.calls == []


@pytest.mark.asyncio
async def test_wrong_session_arm_phrase_does_not_connect(monkeypatch) -> None:
    def no_connect(*args, **kwargs):
        raise AssertionError("no runtime connection allowed")

    monkeypatch.setattr("kairos_risk.canary_runner.Database", no_connect)
    args = parser().parse_args(["submit-next", "--session-id", "f" * 64, "--arm", "almost"])
    with pytest.raises(CanaryArmingError, match="exact bounded"):
        await run_cli(args)


def test_runner_scope_cannot_substitute_runtime_account() -> None:
    scope = CanaryScope(
        environment="paper-dev",
        account_id=ACCOUNT_ID,
        remote_account_id="synthetic",
        config_sha256="a" * 64,
        recorder_code_sha256="b" * 64,
    )
    settings = RiskSettings(
        environment="paper-dev",
        trading_mode="PAPER",
        bus_backend="redis",
        paper_strategy_allowlist=["technical-canary@1"],
    )
    check_runtime_scope(scope, settings)
    with pytest.raises(CanaryArmingError, match="scope"):
        check_runtime_scope(scope.model_copy(update={"account_id": "kairos-paper-dev-other"}), settings)


def test_status_is_not_a_performance_or_qualification_report() -> None:
    status = dict.fromkeys(
        (
            "session_id",
            "receipt_id",
            "state",
            "armed_at",
            "entry_deadline_at",
            "attempts_reserved",
            "max_attempts",
            "last_progress_at",
            "stop_reason",
        ),
        None,
    )
    status.update(pnl=100, private_key="synthetic-secret")
    result = safe_status(status)
    assert result["paper_qualified"] is False
    assert "pnl" not in result and "private_key" not in result
