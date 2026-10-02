"""Actual RiskService boundary with explicit synthetic operator test doubles."""

from unittest.mock import AsyncMock

import pytest
from kairos_core.topics import Topics
from kairos_persistence.operator_control import OperatorControlRefused

from kairos_risk.operator_control import load_operator_scope
from kairos_risk.service import RiskService
from tests.test_paper import NOW, _account, _settings, _venue
from tests.test_paper_runtime import (
    FakeCanaryArmRepository,
    LayeredOperatorAdmission,
    _armed_canary,
    _envelope,
    _layered_operator_service,
)
from tests.test_service import FakeBus


async def ready(service):
    await service.paper.restore_reservations(())
    await service.paper.apply_account(_account())
    await service.paper.apply_venue(_venue())
    service._now_ms = lambda: NOW
    service.bus = FakeBus()


@pytest.mark.asyncio
async def test_missing_default_operator_refuses_without_consuming_canary_or_publishing_approval():
    review, arm = _armed_canary()
    canary = FakeCanaryArmRepository(arm)
    service = RiskService(_settings(environment="paper-dev"), paper_canary_repository=canary)
    await ready(service)
    await service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review))
    result = service.bus.published[0][1]
    assert not result.approved and "operator_control_unavailable" in result.rejection_reasons
    assert canary.consumes == []


@pytest.mark.asyncio
async def test_current_operator_kill_revokes_approved_in_process_cache():
    review, arm = _armed_canary()
    canary = FakeCanaryArmRepository(arm)
    service = _layered_operator_service(_settings(), paper_canary_repository=canary)
    await ready(service)
    await service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review))
    assert service.bus.published[0][1].approved
    service.paper_operator_repository.snapshot = AsyncMock(side_effect=OperatorControlRefused("killed"))
    await service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review, envelope_id="replay"))
    assert not service.bus.published[1][1].approved
    assert "operator_control_unavailable" in service.bus.published[1][1].rejection_reasons
    assert len(canary.consumes) == 1
    # Never discard a previous risk reservation merely because authority changed.
    assert service.paper.reservations


@pytest.mark.asyncio
async def test_control_changed_during_risk_binding_is_refused_before_publish():
    review, arm = _armed_canary()
    service = _layered_operator_service(_settings(), paper_canary_repository=FakeCanaryArmRepository(arm))
    await ready(service)
    service.paper_operator_repository.bind_decision = AsyncMock(side_effect=OperatorControlRefused("stale"))
    await service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review))
    assert not service.bus.published[0][1].approved
    assert "operator_control_changed" in service.bus.published[0][1].rejection_reasons


@pytest.mark.asyncio
async def test_binding_commits_before_risk_outbox_publish():
    review, arm = _armed_canary()
    service = _layered_operator_service(_settings(), paper_canary_repository=FakeCanaryArmRepository(arm))
    await ready(service)
    steps = []
    real_bind = LayeredOperatorAdmission().bind_decision

    async def bind(**kwargs):
        steps.append("binding")
        return await real_bind(**kwargs)

    async def publish(topic, result):
        assert steps == ["binding"] and result.approved
        steps.append("publish")

    service.paper_operator_repository.bind_decision = bind
    service.bus.publish = publish
    await service._handle_paper_review(_envelope(Topics.CANDIDATE_REVIEW, review))
    assert steps == ["binding", "publish"]


def test_scope_missing_wrong_account_and_unbounded_artifact_fail_closed(tmp_path):
    settings = _settings(environment="paper-dev")
    with pytest.raises(ValueError):
        load_operator_scope(settings)
    service = _layered_operator_service(settings)
    wrong = service._paper_operator_scope.model_copy(update={"account_id": "kairos-paper-dev-other"})
    with pytest.raises(ValueError, match="differs"):
        load_operator_scope(settings, wrong)
    path = tmp_path / "scope.json"
    path.write_text(" " * 16_385, encoding="utf-8")
    with pytest.raises(ValueError, match="bounded"):
        load_operator_scope(settings.model_copy(update={"paper_operator_scope_file": path}))
