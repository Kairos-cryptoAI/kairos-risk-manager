"""Network-free service lifecycle, reconciliation, and ACK tests."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from kairos_core.bus import BusEnvelope
from kairos_core.contracts import (
    AccountSnapshot,
    LLMHealthEvent,
    PositionSnapshot,
    TacticalCommand,
)
from kairos_core.enums import ReasonCode, Side, SystemMode, TacticalStatus
from kairos_core.topics import Topics

from kairos_risk.circuit_breaker import CircuitBreakerRegistry
from kairos_risk.config import RiskSettings
from kairos_risk.service import RiskService


class FakeBus:
    def __init__(
        self,
        envelopes: dict[str, list[BusEnvelope]] | None = None,
        *,
        fail_publish_topic: str | None = None,
    ) -> None:
        self.envelopes = envelopes or {}
        self.fail_publish_topic = fail_publish_topic
        self.published: list[tuple[str, object]] = []
        self.acks: list[tuple[str, str]] = []
        self.events: list[tuple[str, str]] = []
        self.closed = False

    async def subscribe(self, topic, **kwargs):
        for envelope in self.envelopes.get(topic, []):
            yield envelope

    async def publish(self, topic, message) -> str:
        self.events.append(("publish", topic))
        if topic == self.fail_publish_topic:
            raise RuntimeError("bus publish failed")
        self.published.append((topic, message))
        return "published-1"

    async def ack(self, topic, envelope, **kwargs) -> None:
        self.events.append(("ack", topic))
        self.acks.append((topic, envelope.id))

    async def close(self) -> None:
        self.closed = True


def _service(
    *,
    require_reconciled_account: bool = False,
    require_strategic_allocation: bool = False,
    max_account_age_s: float = 60.0,
) -> RiskService:
    return RiskService(
        RiskSettings(
            bus_backend="memory",
            require_reconciled_account=require_reconciled_account,
            require_strategic_allocation=require_strategic_allocation,
            account_snapshot_max_age_s=max_account_age_s,
        )
    )


def _command(
    *,
    reason: ReasonCode = ReasonCode.ENTER_LONG_TREND,
    price: float = 65_000,
) -> TacticalCommand:
    return TacticalCommand(
        source="aggregator",
        message_id="command-1",
        symbol="BTCUSDT",
        reference_price=price,
        status=(
            TacticalStatus.EXIT if reason is ReasonCode.CLOSE_POSITION else TacticalStatus.STABLE_TREND_ENTRY
        ),
        reason_code=reason,
        target_side=Side.LONG,
        requested_leverage=2.0,
    )


def _envelope(topic: str, message, *, envelope_id: str = "message-1") -> BusEnvelope:
    return BusEnvelope(id=envelope_id, topic=topic, payload=message.to_payload())


def _snapshot(
    *,
    captured_at: datetime | None = None,
    reconciled: bool = True,
    reconciliation_detail: str = "balances, positions, and open orders reconciled",
) -> AccountSnapshot:
    capture_time = captured_at or datetime.now(UTC)
    return AccountSnapshot(
        source="kairos-execution-engine",
        exchange="evedex",
        account_id="primary",
        equity_usd=10_250,
        available_balance_usd=8_000,
        margin_used_usd=2_250,
        peak_equity_usd=10_500,
        daily_pnl_pct=-0.5,
        realized_pnl_usd=-25,
        unrealized_pnl_usd=15,
        positions=[
            PositionSnapshot(
                source="kairos-execution-engine",
                exchange="evedex",
                account_id="primary",
                symbol="BTCUSDT",
                signed_quantity=-0.2,
                entry_price=65_000,
                mark_price=64_500,
                leverage=2,
                liquidation_price=90_000,
                unrealized_pnl_usd=100,
                protective_stop_order_id="stop-1",
                captured_at=capture_time,
            ),
        ],
        open_order_ids=["order-1"],
        captured_at=capture_time,
        reconciled=reconciled,
        reconciliation_detail=reconciliation_detail,
    )


def _trip(registry: CircuitBreakerRegistry, model: str) -> None:
    for _ in range(3):
        registry.record_failure(model)


def test_gpt_56_outage_drives_conflict_safe():
    service = _service()
    for _ in range(3):
        service.apply_health_event(model="gpt-5.6-sol", ok=False, kind="5xx")
    assert service.breakers.system_mode is SystemMode.CONFLICT_SAFE


def test_flash_outage_drives_text_local_filter():
    service = _service()
    for _ in range(3):
        service.apply_health_event(model="deepseek-v4-flash", ok=False, kind="timeout")
    assert service.breakers.system_mode is SystemMode.TEXT_LOCAL_FILTER


def test_two_outages_drive_local_quant_mode():
    service = _service()
    _trip(service.breakers, CircuitBreakerRegistry.FLASH)
    _trip(service.breakers, CircuitBreakerRegistry.GPT)
    assert service.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE


def test_healthy_signal_recovers_to_normal():
    service = _service()
    _trip(service.breakers, CircuitBreakerRegistry.GPT)
    assert service.breakers.system_mode is SystemMode.CONFLICT_SAFE
    service.apply_health_event(model="gpt-5.6-sol", ok=True)
    assert service.breakers.system_mode is SystemMode.NORMAL


def test_bad_output_does_not_trip_breaker():
    service = _service()
    for _ in range(5):
        service.apply_health_event(model="gpt-5.6-sol", ok=False, kind="error")
    assert service.breakers.system_mode is SystemMode.NORMAL


@pytest.mark.asyncio
async def test_command_is_acked_only_after_validated_order_publish():
    command = _command()
    bus = FakeBus({Topics.TACTICAL_COMMAND: [_envelope(Topics.TACTICAL_COMMAND, command)]})
    service = _service()
    service.bus = bus

    await service._consume_commands()

    assert bus.events == [
        ("publish", Topics.VALIDATED_ORDER),
        ("ack", Topics.TACTICAL_COMMAND),
    ]
    assert bus.published[0][1].intent.price == command.reference_price


@pytest.mark.asyncio
async def test_publish_failure_leaves_command_pending():
    command = _command()
    bus = FakeBus(
        {Topics.TACTICAL_COMMAND: [_envelope(Topics.TACTICAL_COMMAND, command)]},
        fail_publish_topic=Topics.VALIDATED_ORDER,
    )
    service = _service()
    service.bus = bus

    await service._consume_commands()

    assert bus.events == [("publish", Topics.VALIDATED_ORDER)]
    assert bus.acks == []


@pytest.mark.asyncio
async def test_missing_required_reconciliation_publishes_refusal_and_acks():
    command = _command()
    bus = FakeBus({Topics.TACTICAL_COMMAND: [_envelope(Topics.TACTICAL_COMMAND, command)]})
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_commands()

    assert len(bus.published) == 1
    topic, refusal = bus.published[0]
    assert topic == Topics.VALIDATED_ORDER
    assert refusal.approved is False
    assert refusal.reason_code is ReasonCode.NO_TRADE
    assert refusal.adjustments == ["authoritative account reconciliation unavailable"]
    assert bus.acks == [(Topics.TACTICAL_COMMAND, "message-1")]


@pytest.mark.asyncio
async def test_full_execution_snapshot_is_accepted_and_used_for_close():
    snapshot = _snapshot()
    account_env = _envelope(Topics.ACCOUNT_SNAPSHOT, snapshot, envelope_id="account-1")
    command = _command(reason=ReasonCode.CLOSE_POSITION)
    command_env = _envelope(Topics.TACTICAL_COMMAND, command, envelope_id="close-1")
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [account_env],
            Topics.TACTICAL_COMMAND: [command_env],
        }
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()
    await service._consume_commands()

    assert service.account_snapshot == snapshot
    assert bus.acks == [
        (Topics.ACCOUNT_SNAPSHOT, "account-1"),
        (Topics.TACTICAL_COMMAND, "close-1"),
    ]
    _, validated = bus.published[0]
    assert validated.intent.side.value == "BUY"
    assert validated.intent.quantity == 0.2
    assert validated.intent.reduce_only is True


@pytest.mark.asyncio
async def test_newer_unreconciled_snapshot_revokes_previous_state():
    reconciled = _snapshot(captured_at=datetime.now(UTC) - timedelta(seconds=2))
    lost = _snapshot(
        captured_at=datetime.now(UTC),
        reconciled=False,
        reconciliation_detail="exchange position read failed",
    )
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [
                _envelope(Topics.ACCOUNT_SNAPSHOT, reconciled, envelope_id="account-ok"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, lost, envelope_id="account-lost"),
            ]
        }
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot is None
    assert bus.acks == [
        (Topics.ACCOUNT_SNAPSHOT, "account-ok"),
        (Topics.ACCOUNT_SNAPSHOT, "account-lost"),
    ]


@pytest.mark.asyncio
async def test_explicit_reconciliation_failure_blocks_even_with_dev_escape_hatch():
    failure = _snapshot(
        reconciled=False,
        reconciliation_detail="exchange account read failed",
    )
    command = _command()
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [_envelope(Topics.ACCOUNT_SNAPSHOT, failure)],
            Topics.TACTICAL_COMMAND: [_envelope(Topics.TACTICAL_COMMAND, command)],
        }
    )
    service = _service(require_reconciled_account=False)
    service.bus = bus

    await service._consume_account()
    await service._consume_commands()

    assert len(bus.published) == 1
    assert bus.published[0][1].approved is False
    assert bus.published[0][1].adjustments == ["authoritative account reconciliation unavailable"]
    assert len(bus.acks) == 2


@pytest.mark.asyncio
async def test_old_success_cannot_reopen_gate_after_newer_reconciliation_failure():
    now = datetime.now(UTC)
    first = _snapshot(captured_at=now - timedelta(seconds=10))
    lost = _snapshot(
        captured_at=now,
        reconciled=False,
        reconciliation_detail="open-order reconciliation failed",
    )
    delayed_success = _snapshot(captured_at=now - timedelta(seconds=5))
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [
                _envelope(Topics.ACCOUNT_SNAPSHOT, first, envelope_id="first"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, lost, envelope_id="lost"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, delayed_success, envelope_id="delayed"),
            ]
        }
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot is None
    assert service._latest_account_captured_at == now
    assert len(bus.acks) == 3


@pytest.mark.asyncio
async def test_same_version_failure_dominates_reconciled_snapshot():
    captured_at = datetime.now(UTC)
    reconciled = _snapshot(captured_at=captured_at)
    lost = _snapshot(
        captured_at=captured_at,
        reconciled=False,
        reconciliation_detail="same-version reconciliation failed",
    )
    duplicate_success = _snapshot(captured_at=captured_at)
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [
                _envelope(Topics.ACCOUNT_SNAPSHOT, reconciled, envelope_id="success"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, lost, envelope_id="failure"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, duplicate_success, envelope_id="duplicate"),
            ]
        }
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot is None
    assert len(bus.acks) == 3


@pytest.mark.asyncio
async def test_older_unreconciled_snapshot_cannot_revoke_newer_state():
    now = datetime.now(UTC)
    current = _snapshot(captured_at=now)
    older_failure = _snapshot(
        captured_at=now - timedelta(seconds=5),
        reconciled=False,
        reconciliation_detail="delayed failure event",
    )
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [
                _envelope(Topics.ACCOUNT_SNAPSHOT, current, envelope_id="current"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, older_failure, envelope_id="older"),
            ]
        }
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot == current
    assert len(bus.acks) == 2


@pytest.mark.asyncio
async def test_stale_snapshot_blocks_new_risk_but_command_is_handled():
    snapshot = _snapshot(captured_at=datetime.now(UTC) - timedelta(seconds=61))
    command = _command()
    bus = FakeBus({Topics.TACTICAL_COMMAND: [_envelope(Topics.TACTICAL_COMMAND, command)]})
    service = _service(require_reconciled_account=True, max_account_age_s=60)
    service.account_snapshot = snapshot
    service.account = service.account.from_snapshot(snapshot)
    service.bus = bus

    await service._consume_commands()

    assert len(bus.published) == 1
    assert bus.published[0][1].approved is False
    assert bus.published[0][1].adjustments == ["authoritative account reconciliation unavailable"]
    assert bus.acks == [(Topics.TACTICAL_COMMAND, "message-1")]


@pytest.mark.asyncio
async def test_inconsistent_position_identity_leaves_snapshot_pending():
    snapshot = _snapshot()
    bad_position = snapshot.positions[0].model_copy(update={"account_id": "other"})
    snapshot = snapshot.model_copy(update={"positions": [bad_position]})
    bus = FakeBus({Topics.ACCOUNT_SNAPSHOT: [_envelope(Topics.ACCOUNT_SNAPSHOT, snapshot)]})
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot is None
    assert bus.acks == []


@pytest.mark.asyncio
async def test_naive_snapshot_timestamp_leaves_snapshot_pending():
    snapshot = _snapshot(captured_at=datetime.now())
    bus = FakeBus({Topics.ACCOUNT_SNAPSHOT: [_envelope(Topics.ACCOUNT_SNAPSHOT, snapshot)]})
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot is None
    assert bus.acks == []


@pytest.mark.asyncio
async def test_mode_publish_failure_is_retryable_and_health_stays_pending():
    service = _service()
    service.record_llm_failure(CircuitBreakerRegistry.GPT)
    service.record_llm_failure(CircuitBreakerRegistry.GPT)
    event = LLMHealthEvent(
        source="aggregator",
        provider="openai",
        model="gpt-5.6-sol",
        ok=False,
        kind="5xx",
    )
    bus = FakeBus(
        {Topics.LLM_HEALTH: [_envelope(Topics.LLM_HEALTH, event)]},
        fail_publish_topic=Topics.SYSTEM_CONTROL,
    )
    service.bus = bus

    await service._consume_health()

    assert service.breakers.system_mode is SystemMode.CONFLICT_SAFE
    assert service._last_mode is SystemMode.NORMAL
    assert bus.acks == []


@pytest.mark.asyncio
async def test_local_quant_mode_publishes_refusal_for_new_entry():
    service = _service()
    _trip(service.breakers, CircuitBreakerRegistry.FLASH)
    _trip(service.breakers, CircuitBreakerRegistry.GPT)
    command = _command()
    bus = FakeBus({Topics.TACTICAL_COMMAND: [_envelope(Topics.TACTICAL_COMMAND, command)]})
    service.bus = bus

    await service._consume_commands()

    _, validated = next(item for item in bus.published if item[0] == Topics.VALIDATED_ORDER)
    assert validated.approved is False
    assert validated.reason_code is ReasonCode.NO_TRADE
    assert any("LOCAL_QUANT_MODE" in item for item in validated.adjustments)
    assert bus.acks == [(Topics.TACTICAL_COMMAND, "message-1")]


@pytest.mark.asyncio
async def test_zero_price_command_is_acked_but_never_sized():
    command = _command(reason=ReasonCode.NO_TRADE, price=0)
    bus = FakeBus({Topics.TACTICAL_COMMAND: [_envelope(Topics.TACTICAL_COMMAND, command)]})
    service = _service()
    service.bus = bus

    await service._consume_commands()

    assert bus.acks == [(Topics.TACTICAL_COMMAND, "message-1")]
    assert len(bus.published) == 1
    assert bus.published[0][1].approved is False
    assert bus.published[0][1].adjustments == ["positive reference price required for deterministic sizing"]


@pytest.mark.asyncio
async def test_task_group_cancels_peers_and_closes_bus(monkeypatch):
    service = _service()
    bus = FakeBus()
    service.bus = bus
    started = 0
    cancelled = 0
    all_started = asyncio.Event()

    async def fail_commands() -> None:
        await all_started.wait()
        raise RuntimeError("subscription failed")

    async def wait_for_cancellation() -> None:
        nonlocal started, cancelled
        started += 1
        if started == 3:
            all_started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled += 1

    monkeypatch.setattr(service, "_consume_commands", fail_commands)
    monkeypatch.setattr(service, "_consume_health", wait_for_cancellation)
    monkeypatch.setattr(service, "_consume_account", wait_for_cancellation)
    monkeypatch.setattr(service, "_consume_allocation", wait_for_cancellation)

    with pytest.raises(ExceptionGroup) as exc_info:
        await service.run()

    assert len(exc_info.value.exceptions) == 1
    assert str(exc_info.value.exceptions[0]) == "subscription failed"
    assert cancelled == 3
    assert bus.closed is True
