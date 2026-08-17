"""Network-free service lifecycle, reconciliation, and ACK tests."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from kairos_core.bus import BusEnvelope
from kairos_core.contracts import (
    AccountSnapshot,
    LLMHealthEvent,
    PositionSnapshot,
    StrategicAllocation,
    TacticalCommand,
)
from kairos_core.enums import (
    MarketRegime,
    ReasonCode,
    Side,
    StrategicTrigger,
    SystemMode,
    TacticalStatus,
)
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
        fail_ack_topic: str | None = None,
    ) -> None:
        self.envelopes = envelopes or {}
        self.fail_publish_topic = fail_publish_topic
        self.fail_ack_topic = fail_ack_topic
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
        if topic == self.fail_ack_topic:
            raise RuntimeError("bus ack failed")
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


def _allocation(*, produced_at: datetime, regime: MarketRegime = MarketRegime.BULL):
    return StrategicAllocation(
        source="macro",
        regime=regime,
        stable_reserve_pct=0.2,
        strategy_weights={"trend": 0.8},
        max_gross_leverage=2,
        triggered_by=StrategicTrigger.SCHEDULE,
        produced_at=produced_at,
    )


def _trip(registry: CircuitBreakerRegistry, model: str) -> None:
    for _ in range(3):
        registry.record_failure(model)


@pytest.mark.parametrize(
    ("model", "expected_mode"),
    [
        ("gpt-5.6-luna", SystemMode.LOCAL_QUANT_MODE),
        ("gpt-5.6-terra", SystemMode.CONFLICT_SAFE),
        ("gpt-5.6-sol", SystemMode.CONFLICT_SAFE),
    ],
)
def test_openai_model_outages_drive_expected_mode(model, expected_mode):
    service = _service()
    for _ in range(3):
        service.apply_health_event(
            model=model,
            provider="openai",
            ok=False,
            kind="5xx",
        )
    assert service.breakers.system_mode is expected_mode


def test_flash_outage_drives_text_local_filter():
    service = _service()
    for _ in range(3):
        service.apply_health_event(
            model="deepseek-v4-flash",
            provider="deepseek",
            ok=False,
            kind="timeout",
        )
    assert service.breakers.system_mode is SystemMode.TEXT_LOCAL_FILTER


def test_two_outages_drive_local_quant_mode():
    service = _service()
    _trip(service.breakers, CircuitBreakerRegistry.FLASH)
    _trip(service.breakers, CircuitBreakerRegistry.TERRA)
    assert service.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE


def test_healthy_signal_recovers_to_normal():
    service = _service()
    _trip(service.breakers, CircuitBreakerRegistry.SOL)
    assert service.breakers.system_mode is SystemMode.CONFLICT_SAFE
    service.apply_health_event(model="gpt-5.6-sol", ok=True)
    assert service.breakers.system_mode is SystemMode.NORMAL


@pytest.mark.parametrize("kind", ["connection", "rate_limit"])
def test_openai_provider_outage_drives_local_quant_mode(kind):
    service = _service()
    models = ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-terra")
    for model in models:
        service.apply_health_event(model=model, provider="openai", ok=False, kind=kind)

    assert service.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE


def test_healthy_openai_signal_resets_aggregate_provider_streak():
    service = _service()
    for model in ("gpt-5.6-terra", "gpt-5.6-sol"):
        service.apply_health_event(model=model, provider="openai", ok=False, kind="connection")
    service.apply_health_event(model="gpt-5.6-luna", provider="openai", ok=True)
    for model in ("gpt-5.6-terra", "gpt-5.6-sol"):
        service.apply_health_event(model=model, provider="openai", ok=False, kind="connection")

    assert service.breakers.is_provider_down("openai") is False


def test_openai_success_recovers_provider_but_not_sibling_model():
    service = _service()
    _trip(service.breakers, CircuitBreakerRegistry.TERRA)
    for _ in range(3):
        service.breakers.record_provider_failure("openai")
    assert service.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE

    service.apply_health_event(model="gpt-5.6-luna", provider="openai", ok=True)

    assert service.breakers.is_provider_down("openai") is False
    assert service.breakers.is_down(CircuitBreakerRegistry.TERRA) is True
    assert service.breakers.system_mode is SystemMode.CONFLICT_SAFE


def test_deepseek_success_does_not_reset_openai_provider_streak():
    service = _service()
    for model in ("gpt-5.6-terra", "gpt-5.6-sol"):
        service.apply_health_event(model=model, provider="openai", ok=False, kind="connection")
    service.apply_health_event(model="deepseek-v4-flash", provider="deepseek", ok=True)
    service.apply_health_event(
        model="gpt-5.6-terra",
        provider="openai",
        ok=False,
        kind="connection",
    )

    assert service.breakers.is_provider_down("openai") is True
    assert service.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE


def test_explicit_provider_is_normalized_and_wins_over_inference():
    normalized = _service()
    for model in ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-terra"):
        normalized.apply_health_event(
            model=model,
            provider=" OpenAI ",
            ok=False,
            kind="connection",
        )
    assert normalized.breakers.is_provider_down("openai") is True

    explicit = _service()
    for _ in range(3):
        explicit.apply_health_event(
            model="gpt-5.6-terra",
            provider="deepseek",
            ok=False,
            kind="connection",
        )
    assert explicit.breakers.is_provider_down("openai") is False
    assert explicit.breakers.system_mode is SystemMode.CONFLICT_SAFE


def test_provider_is_inferred_for_legacy_health_helper_calls():
    service = _service()
    for _ in range(3):
        service.apply_health_event(model="gpt-5.6-terra", ok=False, kind="connection")

    assert service.breakers.is_provider_down("openai") is True
    assert service.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE


@pytest.mark.asyncio
async def test_health_consumer_uses_event_provider_for_aggregate_outage():
    events = [
        LLMHealthEvent(
            source="aggregator",
            provider="openai",
            model=model,
            ok=False,
            kind="connection",
        )
        for model in ("provider-model-a", "provider-model-b", "provider-model-c")
    ]
    bus = FakeBus(
        {
            Topics.LLM_HEALTH: [
                _envelope(Topics.LLM_HEALTH, event, envelope_id=f"health-{index}")
                for index, event in enumerate(events)
            ]
        }
    )
    service = _service()
    service.bus = bus

    await service._consume_health()

    assert service.breakers.is_provider_down("openai") is True
    assert service.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE
    assert [ack[1] for ack in bus.acks] == ["health-0", "health-1", "health-2"]


@pytest.mark.parametrize("kind", ["error", "bad_output", "http_4xx", "conflict"])
def test_non_outage_failure_does_not_trip_breaker(kind):
    service = _service()
    for _ in range(5):
        service.apply_health_event(
            model="gpt-5.6-sol",
            provider="openai",
            ok=False,
            kind=kind,
        )
    assert service.breakers.system_mode is SystemMode.NORMAL
    assert service.breakers.is_provider_down("openai") is False


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
async def test_conflicting_reconciled_snapshot_version_revokes_authority():
    captured_at = datetime.now(UTC)
    first = _snapshot(captured_at=captured_at)
    conflict = first.model_copy(update={"equity_usd": first.equity_usd - 100})
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [
                _envelope(Topics.ACCOUNT_SNAPSHOT, first, envelope_id="first"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, conflict, envelope_id="conflict"),
            ]
        }
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot is None
    assert len(bus.acks) == 2


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
async def test_malformed_newer_snapshot_revokes_previous_authority():
    now = datetime.now(UTC)
    good = _snapshot(captured_at=now - timedelta(seconds=1))
    invalid = _snapshot(captured_at=now).model_copy(update={"peak_equity_usd": 10_000})
    delayed = _snapshot(captured_at=now - timedelta(milliseconds=500))
    bus = FakeBus(
        {
            Topics.ACCOUNT_SNAPSHOT: [
                _envelope(Topics.ACCOUNT_SNAPSHOT, good, envelope_id="good"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, invalid, envelope_id="invalid"),
                _envelope(Topics.ACCOUNT_SNAPSHOT, delayed, envelope_id="delayed"),
            ]
        }
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot is None
    assert service._latest_account_captured_at == now
    assert bus.acks == [
        (Topics.ACCOUNT_SNAPSHOT, "good"),
        (Topics.ACCOUNT_SNAPSHOT, "delayed"),
    ]


@pytest.mark.asyncio
async def test_account_ack_failure_keeps_applied_authority_for_redelivery():
    snapshot = _snapshot()
    bus = FakeBus(
        {Topics.ACCOUNT_SNAPSHOT: [_envelope(Topics.ACCOUNT_SNAPSHOT, snapshot)]},
        fail_ack_topic=Topics.ACCOUNT_SNAPSHOT,
    )
    service = _service(require_reconciled_account=True)
    service.bus = bus

    await service._consume_account()

    assert service.account_snapshot == snapshot
    assert bus.acks == []


@pytest.mark.asyncio
async def test_unpriced_position_blocks_entry_but_still_allows_exact_exit():
    snapshot = _snapshot()
    unpriced = snapshot.positions[0].model_copy(update={"entry_price": None, "mark_price": None})
    snapshot = snapshot.model_copy(update={"positions": [unpriced]})
    service = _service(require_reconciled_account=True)
    service._handle_account(_envelope(Topics.ACCOUNT_SNAPSHOT, snapshot))
    entry = _command()
    close = _command(reason=ReasonCode.CLOSE_POSITION)
    bus = FakeBus(
        {
            Topics.TACTICAL_COMMAND: [
                _envelope(Topics.TACTICAL_COMMAND, entry, envelope_id="entry"),
                _envelope(Topics.TACTICAL_COMMAND, close, envelope_id="close"),
            ]
        }
    )
    service.bus = bus

    await service._consume_commands()

    entry_result = bus.published[0][1]
    close_result = bus.published[1][1]
    assert entry_result.approved is False
    assert any("gross exposure" in note for note in entry_result.adjustments)
    assert close_result.approved is True
    assert close_result.intent.quantity == 0.2
    assert close_result.intent.reduce_only is True


def test_older_allocation_cannot_roll_back_current_policy():
    now = datetime.now(UTC)
    service = _service()
    current = _allocation(produced_at=now, regime=MarketRegime.BEAR)
    older = _allocation(produced_at=now - timedelta(seconds=1), regime=MarketRegime.BULL)

    service._handle_allocation(_envelope(Topics.STRATEGIC_ALLOCATION, current))
    service._handle_allocation(_envelope(Topics.STRATEGIC_ALLOCATION, older))

    assert service.strategic_allocation == current


def test_conflicting_allocation_at_same_event_time_revokes_policy():
    now = datetime.now(UTC)
    service = _service()
    first = _allocation(produced_at=now, regime=MarketRegime.BULL)
    conflict = _allocation(produced_at=now, regime=MarketRegime.BEAR)

    service._handle_allocation(_envelope(Topics.STRATEGIC_ALLOCATION, first))
    service._handle_allocation(_envelope(Topics.STRATEGIC_ALLOCATION, conflict))

    assert service.strategic_allocation is None


@pytest.mark.asyncio
async def test_allocation_ack_failure_keeps_applied_policy_for_redelivery():
    allocation = _allocation(produced_at=datetime.now(UTC))
    bus = FakeBus(
        {Topics.STRATEGIC_ALLOCATION: [_envelope(Topics.STRATEGIC_ALLOCATION, allocation)]},
        fail_ack_topic=Topics.STRATEGIC_ALLOCATION,
    )
    service = _service()
    service.bus = bus

    await service._consume_allocation()

    assert service.strategic_allocation == allocation
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
    service.record_llm_failure(CircuitBreakerRegistry.SOL)
    service.record_llm_failure(CircuitBreakerRegistry.SOL)
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
    _trip(service.breakers, CircuitBreakerRegistry.TERRA)
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
