"""Actual RiskService boundary with explicit synthetic operator test doubles."""

from unittest.mock import AsyncMock

import pytest
from kairos_core.bus import InMemoryBus
from kairos_core.bus.redis_streams import RedisStreamsBus
from kairos_core.topics import Topics
from kairos_persistence import Database, DurableMessageBus, MigrationProfile, PersistenceSettings
from kairos_persistence.operator_control import OperatorControlRefused

from kairos_risk.config import RiskSettings
from kairos_risk.operator_control import RejectAllPaperOperatorRepository, load_operator_scope
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


def test_explicit_dry_run_bus_avoids_default_transport(monkeypatch):
    def forbidden(_settings):
        raise AssertionError("Default transport must not be constructed")

    monkeypatch.setattr("kairos_risk.service.build_bus", forbidden)
    bus = InMemoryBus()
    service = RiskService(RiskSettings(_env_file=None, bus_backend="memory"), bus=bus)
    assert service.bus is bus
    assert isinstance(service.paper_operator_repository, RejectAllPaperOperatorRepository)


def _injected_paper_bus(
    *,
    verify=True,
    required=MigrationProfile.CONTROLLED_RUNTIME,
    profile=MigrationProfile.CONTROLLED_RUNTIME,
    transport=None,
    service_name="fixture-risk",
):
    settings = PersistenceSettings(
        _env_file=None, database_url="postgresql://fixture:fixture@fixture.invalid:5432/kairos"
    )
    return DurableMessageBus(
        transport or RedisStreamsBus("redis://fixture.invalid:6379/0"),
        service_name=service_name,
        settings=settings,
        database=Database(settings, migration_profile=profile),
        verify_schema_only=verify,
        required_migration_profile=required,
    )


@pytest.mark.parametrize(
    "kind",
    ["memory", "memory-transport", "wrong-identity", "migrating", "no-required-profile", "wrong-profile"],
)
def test_injected_paper_bus_cannot_bypass_controlled_schema(kind):
    bus = {
        "memory": lambda: InMemoryBus(),
        "memory-transport": lambda: _injected_paper_bus(transport=InMemoryBus()),
        "wrong-identity": lambda: _injected_paper_bus(service_name="other-service"),
        "migrating": lambda: _injected_paper_bus(verify=False),
        "no-required-profile": lambda: _injected_paper_bus(required=None),
        "wrong-profile": lambda: _injected_paper_bus(profile=MigrationProfile.RUNTIME),
    }[kind]()
    with pytest.raises(ValueError, match="verify-only controlled-runtime"):
        RiskService(
            RiskSettings(
                _env_file=None, service_name="fixture-risk", trading_mode="PAPER", bus_backend="redis"
            ),
            bus=bus,
        )


@pytest.mark.asyncio
async def test_valid_explicit_paper_bus_preserves_runtime_start_and_default_operator_deny(monkeypatch):
    bus = _injected_paper_bus()
    service = RiskService(
        RiskSettings(_env_file=None, service_name="fixture-risk", trading_mode="PAPER", bus_backend="redis"),
        bus=bus,
    )
    assert service.bus is bus and not service.paper.recovery_complete
    assert isinstance(service.paper_operator_repository, RejectAllPaperOperatorRepository)
    assert service._paper_operator_scope is None
    assert service.settings.paper_strategy_allowlist == []
    start = AsyncMock(side_effect=RuntimeError("FIXTURE_START_REFUSED"))
    monkeypatch.setattr(bus, "start", start)
    with pytest.raises(RuntimeError, match="FIXTURE_START_REFUSED"):
        await service._recover_paper_state()
    start.assert_awaited_once()
    assert not service.paper.recovery_complete


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
