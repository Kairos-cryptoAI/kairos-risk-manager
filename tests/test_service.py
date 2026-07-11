"""Risk service health handling and cross-layer command-to-order wiring."""
import asyncio

from kairos_core.bus import BusEnvelope
from kairos_core.contracts import AccountSnapshot, PositionSnapshot, TacticalCommand
from kairos_core.enums import ReasonCode, Side, SystemMode, TacticalStatus
from kairos_core.topics import Topics

from kairos_risk.config import RiskSettings
from kairos_risk.service import RiskService


def _svc() -> RiskService:
    return RiskService(RiskSettings(bus_backend="memory", require_reconciled_account=False))


def test_gpt_outage_drives_conflict_safe():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="5xx")
    assert svc.breakers.system_mode is SystemMode.CONFLICT_SAFE


def test_flash_outage_drives_text_local_filter():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="deepseek-v4-flash", ok=False, kind="timeout")
    assert svc.breakers.system_mode is SystemMode.TEXT_LOCAL_FILTER


def test_two_outages_drive_local_quant_mode():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="deepseek-v4-flash", ok=False, kind="5xx")
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="5xx")
    assert svc.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE


def test_healthy_signal_recovers_to_normal():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="5xx")
    assert svc.breakers.system_mode is SystemMode.CONFLICT_SAFE
    svc.apply_health_event(model="gpt-5.5", ok=True)
    assert svc.breakers.system_mode is SystemMode.NORMAL


def test_bad_output_does_not_trip_breaker():
    svc = _svc()
    for _ in range(5):
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="error")  # API answered
    assert svc.breakers.system_mode is SystemMode.NORMAL


class _OneMessageBus:
    def __init__(self, envelope):
        self.envelope = envelope
        self.published = []
        self.acks = []

    async def subscribe(self, topic, **kwargs):
        yield self.envelope

    async def publish(self, topic, message):
        self.published.append((topic, message))
        return "published-1"

    async def ack(self, topic, envelope, **kwargs):
        self.acks.append((topic, envelope.id))


def test_tactical_reference_price_reaches_validated_order():
    command = TacticalCommand(
        source="aggregator", symbol="BTCUSDT", reference_price=65_000,
        status=TacticalStatus.STABLE_TREND_ENTRY,
        reason_code=ReasonCode.ENTER_LONG_TREND, target_side=Side.LONG,
        requested_leverage=2.0,
    )
    envelope = BusEnvelope(
        id="command-1", topic=Topics.TACTICAL_COMMAND, payload=command.to_payload(),
    )
    service = _svc()
    service.bus = _OneMessageBus(envelope)

    asyncio.run(service._consume_commands())

    assert service.bus.acks == [(Topics.TACTICAL_COMMAND, "command-1")]
    assert len(service.bus.published) == 1
    topic, validated = service.bus.published[0]
    assert topic == Topics.VALIDATED_ORDER
    assert validated.approved is True
    assert validated.intent.price == command.reference_price
    assert validated.intent.quantity > 0


def test_reconciled_snapshot_closes_only_command_symbol_position():
    command = TacticalCommand(
        source="aggregator", symbol="BTCUSDT", reference_price=65_000,
        status=TacticalStatus.EXIT, reason_code=ReasonCode.CLOSE_POSITION,
    )
    envelope = BusEnvelope(
        id="close-1", topic=Topics.TACTICAL_COMMAND, payload=command.to_payload(),
    )
    service = RiskService(RiskSettings(bus_backend="memory", require_reconciled_account=True))
    service.account_snapshot = AccountSnapshot(
        source="execution", exchange="evedex", account_id="primary",
        equity_usd=10_000, available_balance_usd=8_000, peak_equity_usd=10_000,
        positions=[
            PositionSnapshot(
                source="execution", exchange="evedex", account_id="primary",
                symbol="BTCUSDT", signed_quantity=-0.2, mark_price=65_000,
            ),
            PositionSnapshot(
                source="execution", exchange="evedex", account_id="primary",
                symbol="ETHUSDT", signed_quantity=3.0, mark_price=3_000,
            ),
        ],
        reconciled=True,
    )
    service.bus = _OneMessageBus(envelope)

    asyncio.run(service._consume_commands())

    _, validated = service.bus.published[0]
    assert validated.intent.side.value == "BUY"
    assert validated.intent.quantity == 0.2
    assert validated.intent.reduce_only is True


def test_legacy_zero_price_command_is_acked_but_never_sized():
    command = TacticalCommand(
        source="aggregator", symbol="BTCUSDT", status=TacticalStatus.WAIT_CONFIRMATION,
        reason_code=ReasonCode.NO_TRADE,
    )
    envelope = BusEnvelope(
        id="legacy-1", topic=Topics.TACTICAL_COMMAND, payload=command.to_payload(),
    )
    service = _svc()
    service.bus = _OneMessageBus(envelope)

    asyncio.run(service._consume_commands())

    assert service.bus.acks == [(Topics.TACTICAL_COMMAND, "legacy-1")]
    assert service.bus.published == []
