"""Safe manual technical-canary preparation and publication semantics."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from kairos_core.contracts import (
    AccountSnapshotV2,
    CandidateReviewV1,
    ClosedBarEventV1,
    StrategicAllocation,
    VenueQualityV1,
)
from kairos_core.enums import EvedexProfile, MarketRegime, Side, TradingMode
from kairos_core.topics import Topics
from kairos_persistence import Database, PersistenceSettings
from kairos_persistence import PaperCanaryArmRepository as DurablePaperCanaryArmRepository

import kairos_risk.canary as canary_module
from kairos_risk.canary import (
    CANARY_ARM_PHRASE,
    CANARY_STRATEGY_REF,
    CanaryArmingError,
    CanaryInputs,
    CanaryPlan,
    CanaryPreflightError,
    CanaryPublishError,
    CanarySession,
    PostgresCanaryInputSource,
    prepare_canary,
)
from kairos_risk.canary_authorization import (
    CanaryAuthorizationError,
    PaperCanaryArmRecord,
    allocation_from_consumed_arm,
    build_persistence_canary_repository,
    canary_arm_identity,
)
from kairos_risk.canary_instrument import EvedexDevInstrumentRule

T0 = 1_800_000_000_000
NOW = T0 + 60_500
ACCOUNT_ID = "kairos-paper-dev-01"


def _bar(**overrides: object) -> ClosedBarEventV1:
    values: dict[str, object] = {
        "source": "quant-scouts",
        "symbol": "BTCUSDT",
        "open_time_ms": T0,
        "close_time_ms": T0 + 59_999,
        "open": 99.8,
        "high": 100.2,
        "low": 99.7,
        "close": 100.0,
        "base_volume": 10.0,
        "quote_volume": 1_000.0,
        "taker_buy_base_volume": 5.0,
        "taker_buy_quote_volume": 500.0,
    }
    values.update(overrides)
    return ClosedBarEventV1(**values)


def _venue(*, best_bid: float = 99.99, best_ask: float = 100.01, **overrides: object) -> VenueQualityV1:
    observed = T0 + 60_200
    reference_mid = 100.0
    venue_mid = (best_bid + best_ask) / 2
    reference_ts = observed - 100
    book_ts = observed - 100
    values: dict[str, object] = {
        "source": "quant-scouts",
        "profile": EvedexProfile.DEV,
        "symbol": "BTCUSD:DEV",
        "observed_at_ms": observed,
        "expires_at_ms": NOW + 5_000,
        "reference_timestamp_ms": reference_ts,
        "book_timestamp_ms": book_ts,
        "reference_mid_price": reference_mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "venue_mid_price": venue_mid,
        "basis_bps": (venue_mid - reference_mid) / reference_mid * 10_000,
        "spread_bps": (best_ask - best_bid) / venue_mid * 10_000,
        "assessed_notional_usd": 1_000.0,
        "depth_usd": 5_000.0,
        "buy_slippage_bps": 1.0,
        "sell_slippage_bps": 1.0,
        "taker_fee_bps": 5.0,
        "reference_age_ms": observed - reference_ts,
        "book_age_ms": observed - book_ts,
        "latency_ms": 20,
        "timestamp_skew_ms": abs(reference_ts - book_ts),
        "entry_allowed": True,
    }
    values.update(overrides)
    return VenueQualityV1(**values)


def _account(**overrides: object) -> AccountSnapshotV2:
    values: dict[str, object] = {
        "source": "execution-engine",
        "trading_mode": TradingMode.PAPER,
        "evedex_profile": EvedexProfile.DEV,
        "account_id": ACCOUNT_ID,
        "equity_usd": 10_000.0,
        "available_balance_usd": 10_000.0,
        "margin_used_usd": 0.0,
        "durable_day_start_equity_usd": 10_000.0,
        "durable_peak_equity_usd": 10_000.0,
        "total_open_risk_usd": 0.0,
        "captured_at_ms": NOW - 100,
        "reconciliation_seq": 7,
        "reconciled": True,
        "reconciliation_detail": "full EVEDEX DEV reconciliation",
    }
    values.update(overrides)
    return AccountSnapshotV2(**values)


def _instrument(**overrides: object) -> EvedexDevInstrumentRule:
    values: dict[str, object] = {
        "venue_symbol": "BTCUSD:DEV",
        "trading": "all",
        "market_state": "OPEN",
        "updated_at_ms": T0 + 60_100,
        "lot_size": Decimal("1"),
        "price_increment": Decimal("0.01"),
        "quantity_increment": Decimal("0.001"),
        "multiplier": Decimal("1"),
        "min_volume_usd": Decimal("5"),
        "min_price": Decimal("10"),
        "max_price": Decimal("1000"),
        "min_quantity": Decimal("0.001"),
        "max_quantity": Decimal("100"),
        "fetched_at_ms": NOW,
    }
    values.update(overrides)
    return EvedexDevInstrumentRule(**values)  # type: ignore[arg-type]


def _inputs(**overrides: object) -> CanaryInputs:
    values: dict[str, object] = {
        "bar": _bar(),
        "venue": _venue(),
        "account": _account(),
        "instrument": _instrument(),
    }
    values.update(overrides)
    return CanaryInputs(**values)


@dataclass
class FakeSource:
    inputs: CanaryInputs | Exception
    calls: int = 0

    async def load(self, plan: CanaryPlan, *, account_id: str) -> CanaryInputs:
        self.calls += 1
        assert plan.symbol == "BTCUSDT"
        assert account_id == ACCOUNT_ID
        if isinstance(self.inputs, Exception):
            raise self.inputs
        return self.inputs


@dataclass(frozen=True)
class FakePaperCanaryArm:
    arm_id: str
    account_id: str
    review: CandidateReviewV1
    allocation: StrategicAllocation
    status: str
    expires_at: datetime
    decided_at_ms: int | None


@dataclass
class FakeArmRepository:
    fail: bool = False
    calls: list[str] = field(default_factory=list)
    arms: list[FakePaperCanaryArm] = field(default_factory=list)

    async def arm(
        self,
        *,
        account_id: str,
        review: CandidateReviewV1,
        allocation: StrategicAllocation,
    ) -> PaperCanaryArmRecord:
        self.calls.append("arm")
        if self.fail:
            raise OSError("simulated atomic durable arm/enqueue failure")
        arm = FakePaperCanaryArm(
            arm_id=canary_arm_identity(account_id=account_id, review=review, allocation=allocation),
            account_id=account_id,
            review=review,
            allocation=allocation,
            status="ARMED",
            expires_at=datetime.fromtimestamp(review.intent.entry_expires_ts_ms / 1_000, tz=UTC),
            decided_at_ms=None,
        )
        self.arms.append(arm)
        return arm

    async def consume(
        self,
        *,
        account_id: str,
        review: CandidateReviewV1,
    ) -> PaperCanaryArmRecord | None:
        del account_id, review
        raise AssertionError("CLI arm repository attempted to consume")


class FakeAsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value
        self.entered = False

    async def __aenter__(self) -> object:
        self.entered = True
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        self.entered = False


class FakeSnapshotConnection:
    def __init__(self) -> None:
        self.transaction_options: dict[str, object] | None = None
        self.transaction_context = FakeAsyncContext(None)
        self.calls: list[tuple[str, str]] = []

    def transaction(self, **options: object) -> FakeAsyncContext:
        self.transaction_options = options
        return self.transaction_context

    async def fetchrow(self, _query: str, topic: str, *_args: object) -> dict[str, object] | None:
        self.calls.append(("fetchrow", topic))
        payloads = {
            Topics.CLOSED_BAR: _bar().to_payload(),
            Topics.VENUE_QUALITY: _venue().to_payload(),
            Topics.ACCOUNT_SNAPSHOT_V2: _account().to_payload(),
        }
        payload = payloads.get(topic)
        return None if payload is None else {"payload": payload}

    async def fetch(self, _query: str, topic: str, *_args: object) -> list[dict[str, object]]:
        self.calls.append(("fetch", topic))
        return []


class FakeSnapshotPool:
    def __init__(self, connection: FakeSnapshotConnection) -> None:
        self.connection = connection
        self.acquire_context = FakeAsyncContext(connection)

    def acquire(self) -> FakeAsyncContext:
        return self.acquire_context


def _consumed_arm(
    review: CandidateReviewV1,
    allocation: StrategicAllocation,
    **overrides: object,
) -> FakePaperCanaryArm:
    values: dict[str, object] = {
        "arm_id": canary_arm_identity(
            account_id=ACCOUNT_ID,
            review=review,
            allocation=allocation,
        ),
        "account_id": ACCOUNT_ID,
        "review": review,
        "allocation": allocation,
        "status": "CONSUMED",
        "expires_at": datetime.fromtimestamp(review.intent.entry_expires_ts_ms / 1_000, tz=UTC),
        "decided_at_ms": NOW,
    }
    values.update(overrides)
    return FakePaperCanaryArm(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_preview_is_mutation_free_and_builds_only_canary_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("preview attempted to create an external runtime client")

    monkeypatch.setattr(canary_module, "Database", forbidden_network)
    source = FakeSource(_inputs())
    writer = FakeArmRepository()

    result = await CanarySession(source, writer).run(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
    )

    assert not result.published
    assert source.calls == 1
    assert writer.calls == []
    assert result.prepared.review.intent.strategy_id == "technical-canary"
    assert result.prepared.review.intent.strategy_revision == "1"
    assert result.prepared.review.model_provenance is None
    assert result.prepared.review.route.intent.model_dump() == result.prepared.review.intent.model_dump()
    assert result.summary()["strategy"] == CANARY_STRATEGY_REF
    metadata = dict(result.prepared.review.intent.metadata)
    assert metadata["canary_entry_order"] == "MARKETABLE_IOC_LIMIT"
    assert metadata["canary_quantity"] == "0.05"
    assert metadata["venue_min_volume_usd"] == "5"
    assert {item.kind for item in result.prepared.review.intent.evidence} == {
        "closed_bar",
        "venue_instrument",
    }


@pytest.mark.asyncio
async def test_durable_input_selection_uses_one_read_only_repeatable_read_snapshot() -> None:
    connection = FakeSnapshotConnection()

    async def load_instrument(
        venue_symbol: str,
        *,
        fetched_at_ms: int,
    ) -> EvedexDevInstrumentRule:
        assert venue_symbol == "BTCUSD:DEV"
        return _instrument(fetched_at_ms=fetched_at_ms)

    source = PostgresCanaryInputSource(FakeSnapshotPool(connection), load_instrument)

    inputs = await source.load(CanaryPlan(symbol="BTCUSDT", side=Side.LONG), account_id=ACCOUNT_ID)

    assert inputs.bar.bar_sha256 == _bar().bar_sha256
    assert inputs.venue.measurement_id == _venue().measurement_id
    assert inputs.account.snapshot_id == _account().snapshot_id
    assert inputs.instrument.rules_sha256 == _instrument().rules_sha256
    assert connection.transaction_options == {"isolation": "repeatable_read", "readonly": True}
    assert connection.calls == [
        ("fetchrow", Topics.CLOSED_BAR),
        ("fetchrow", Topics.VENUE_QUALITY),
        ("fetchrow", Topics.ACCOUNT_SNAPSHOT_V2),
        ("fetchrow", Topics.STRATEGIC_ALLOCATION),
        ("fetch", Topics.RISK_TRADE_DECISION),
    ]


def test_canary_quantity_tracks_min_volume_and_rejects_unsafe_instrument_rules() -> None:
    prepared = prepare_canary(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        _inputs(
            bar=_bar(open=1.49, high=1.51, low=1.48, close=1.5),
            venue=_venue(best_bid=1.49, best_ask=1.50),
            instrument=_instrument(
                price_increment=Decimal("0.01"),
                quantity_increment=Decimal("0.1"),
                min_price=Decimal("0.01"),
                max_price=Decimal("1000"),
                min_quantity=Decimal("0.1"),
            ),
        ),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )

    assert dict(prepared.review.intent.metadata)["canary_quantity"] == "3.4"

    with pytest.raises(ValueError, match="trading=all"):
        _instrument(trading="none")
    with pytest.raises(ValueError, match="quantityIncrement"):
        _instrument(min_quantity=Decimal("0.0015"))
    with pytest.raises(ValueError, match="priceIncrement"):
        _instrument(min_price=Decimal("10.005"))


@pytest.mark.asyncio
async def test_cli_preview_connects_read_only_without_migration_or_arm_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeSnapshotConnection()

    class PreviewDatabase:
        def __init__(self, _settings: object) -> None:
            self.pool = FakeSnapshotPool(connection)
            self.connected = False
            self.closed = False

        async def connect(self) -> None:
            self.connected = True

        async def migrate(self) -> None:
            raise AssertionError("preview attempted a schema mutation")

        async def close(self) -> None:
            self.closed = True

    database_holder: list[PreviewDatabase] = []

    def database_factory(settings: object) -> PreviewDatabase:
        database = PreviewDatabase(settings)
        database_holder.append(database)
        return database

    def forbidden_repository(_database: object) -> object:
        raise AssertionError("preview attempted to construct the arm repository")

    monkeypatch.setattr(canary_module, "Database", database_factory)
    monkeypatch.setattr(canary_module, "_build_persistence_arm_repository", forbidden_repository)
    monkeypatch.setattr(canary_module.time, "time_ns", lambda: NOW * 1_000_000)

    async def load_instrument(
        venue_symbol: str,
        *,
        fetched_at_ms: int,
    ) -> EvedexDevInstrumentRule:
        assert venue_symbol == "BTCUSD:DEV"
        return _instrument(fetched_at_ms=fetched_at_ms)

    monkeypatch.setattr(canary_module, "fetch_evedex_dev_instrument", load_instrument)

    result = await canary_module._run_cli(
        canary_module._parser().parse_args(["--symbol", "BTCUSDT", "--side", "LONG"])
    )

    assert not result.published
    assert len(database_holder) == 1
    assert database_holder[0].connected
    assert database_holder[0].closed


@pytest.mark.asyncio
async def test_publish_requires_exact_arm_phrase_before_reading_state() -> None:
    source = FakeSource(_inputs())
    writer = FakeArmRepository()

    with pytest.raises(CanaryArmingError, match="exact"):
        await CanarySession(source, writer).run(
            CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
            account_id=ACCOUNT_ID,
            now_ms=NOW,
            publish=True,
            arm="almost",
        )

    assert source.calls == 0
    assert writer.calls == []


@pytest.mark.asyncio
async def test_armed_publish_atomically_binds_allocation_and_review() -> None:
    source = FakeSource(_inputs())
    writer = FakeArmRepository()

    result = await CanarySession(source, writer).run(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        publish=True,
        arm=CANARY_ARM_PHRASE,
    )

    assert result.published
    assert writer.calls == ["arm"]
    arm = writer.arms[0]
    assert arm.arm_id == result.arm_id
    assert arm.account_id == ACCOUNT_ID
    assert arm.review.model_dump(mode="json") == result.prepared.review.model_dump(mode="json")
    assert arm.allocation.model_dump(mode="json") == result.prepared.allocation.model_dump(mode="json")
    assert arm.expires_at == datetime.fromtimestamp(
        result.prepared.review.intent.entry_expires_ts_ms / 1_000,
        tz=UTC,
    )
    allocation = result.prepared.allocation
    assert allocation.regime is MarketRegime.BULL
    assert allocation.max_gross_leverage == 1.0
    assert allocation.strategy_weights == {"technical-canary": 0.0025}
    assert allocation.stable_reserve_pct + allocation.strategy_weights["technical-canary"] == 1.0


def test_same_bar_and_plan_produce_identical_allocation_and_review_bytes() -> None:
    plan = CanaryPlan(symbol="BTCUSDT", side=Side.LONG)

    first = prepare_canary(
        plan,
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    second = prepare_canary(
        plan,
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW + 100,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )

    assert first.review_bytes() == second.review_bytes()
    assert first.allocation_bytes() == second.allocation_bytes()
    assert first.review.review_id == second.review.review_id
    assert first.allocation.message_id == second.allocation.message_id
    first_arm_id = canary_arm_identity(
        account_id=ACCOUNT_ID,
        review=first.review,
        allocation=first.allocation,
    )
    second_arm_id = canary_arm_identity(
        account_id=ACCOUNT_ID,
        review=second.review,
        allocation=second.allocation,
    )
    assert first_arm_id == second_arm_id


def test_pinned_persistence_boundary_accepts_the_exact_generated_policy() -> None:
    prepared = prepare_canary(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )

    DurablePaperCanaryArmRepository._validate_binding(
        prepared.review,
        prepared.allocation,
    )
    DurablePaperCanaryArmRepository._validate_account_binding(
        ACCOUNT_ID,
        prepared.review,
    )


def test_persistence_adapter_uses_the_pinned_repository_without_network_access() -> None:
    sentinel_pool = object()

    repository = build_persistence_canary_repository(sentinel_pool)

    assert isinstance(repository, DurablePaperCanaryArmRepository)
    assert repository.pool is sentinel_pool


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_database_arm_consume_outbox_and_risk_binding_replay() -> None:
    database_url = os.getenv("KAIROS_PERSISTENCE_DATABASE_URL")
    if not database_url:
        pytest.skip("KAIROS_PERSISTENCE_DATABASE_URL is required for integration tests")
    prepared = prepare_canary(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    database = Database(PersistenceSettings(database_url=database_url))
    await database.connect()
    await database.migrate()
    repository = build_persistence_canary_repository(database.pool)
    review_id = prepared.review.message_id
    try:
        await database.pool.execute("DELETE FROM paper_canary_arms WHERE account_id=$1", ACCOUNT_ID)
        await database.pool.execute("DELETE FROM message_outbox WHERE message_id=$1", review_id)
        await database.pool.execute("DELETE FROM event_audit WHERE message_id=$1", review_id)
        armed = await repository.arm(
            account_id=ACCOUNT_ID,
            review=prepared.review,
            allocation=prepared.allocation,
        )
        assert armed.status == "ARMED"
        assert (
            await database.pool.fetchval(
                "SELECT count(*) FROM message_outbox WHERE message_id=$1",
                review_id,
            )
            == 1
        )
        consumed = await repository.consume(account_id=ACCOUNT_ID, review=prepared.review)
        assert consumed is not None and consumed.status == "CONSUMED"
        allocation, decided_at_ms = allocation_from_consumed_arm(
            consumed,
            prepared.review,
            account_id=ACCOUNT_ID,
            now_ms=NOW,
        )
        assert allocation == prepared.allocation
        assert decided_at_ms == prepared.review.intent.entry_eligible_ts_ms
        assert await repository.consume(account_id=ACCOUNT_ID, review=prepared.review) == consumed
    finally:
        await database.pool.execute("DELETE FROM paper_canary_arms WHERE account_id=$1", ACCOUNT_ID)
        await database.pool.execute("DELETE FROM message_outbox WHERE message_id=$1", review_id)
        await database.pool.execute("DELETE FROM event_audit WHERE message_id=$1", review_id)
        await database.close()


def test_claim_uses_exact_bound_allocation_not_a_mutated_runtime_copy() -> None:
    prepared = prepare_canary(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    stored_allocation = prepared.allocation.model_copy(deep=True)
    arm = _consumed_arm(prepared.review, stored_allocation)
    prepared.allocation.strategy_weights["untrusted-runtime-copy"] = 0.5

    restored, decided_at_ms = allocation_from_consumed_arm(
        arm,
        prepared.review,
        account_id=ACCOUNT_ID,
        now_ms=NOW,
    )

    assert restored.strategy_weights == {"technical-canary": 0.0025}
    assert restored.model_dump(mode="json") == stored_allocation.model_dump(mode="json")
    assert decided_at_ms == NOW


@pytest.mark.parametrize(
    "arm_override",
    [
        {"account_id": "wrong-account"},
        {"status": "ARMED"},
        {"expires_at": datetime.fromtimestamp((T0 + 120_000) / 1_000, tz=UTC)},
        {"decided_at_ms": T0 + 59_999},
        {"decided_at_ms": NOW + 2_001},
        {"arm_id": "d" * 64},
    ],
)
def test_claim_rejects_any_record_binding_mutation(arm_override: dict[str, object]) -> None:
    prepared = prepare_canary(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    with pytest.raises(CanaryAuthorizationError):
        allocation_from_consumed_arm(
            _consumed_arm(prepared.review, prepared.allocation, **arm_override),
            prepared.review,
            account_id=ACCOUNT_ID,
            now_ms=NOW,
        )


def test_claim_rejects_expired_bound_allocation() -> None:
    prepared = prepare_canary(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    with pytest.raises(CanaryAuthorizationError, match="expired"):
        allocation_from_consumed_arm(
            _consumed_arm(prepared.review, prepared.allocation),
            prepared.review,
            account_id=ACCOUNT_ID,
            now_ms=prepared.review.intent.entry_expires_ts_ms,
        )


def test_claim_rejects_rehashed_policy_mutation_with_stale_message_identity() -> None:
    prepared = prepare_canary(
        CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
        _inputs(),
        account_id=ACCOUNT_ID,
        now_ms=NOW,
        account_max_age_ms=30_000,
        allocation_max_age_s=1_000,
    )
    mutated_allocation = prepared.allocation.model_copy(
        update={"rationale": "mutated after arm"},
    )
    mutated_arm_id = canary_arm_identity(
        account_id=ACCOUNT_ID,
        review=prepared.review,
        allocation=mutated_allocation,
    )

    with pytest.raises(CanaryAuthorizationError, match="exact bound policy"):
        allocation_from_consumed_arm(
            _consumed_arm(
                prepared.review,
                mutated_allocation,
                arm_id=mutated_arm_id,
            ),
            prepared.review,
            account_id=ACCOUNT_ID,
            now_ms=NOW,
        )


@pytest.mark.asyncio
async def test_missing_durable_input_fails_without_publication() -> None:
    source = FakeSource(CanaryPreflightError("latest durable venue quality is unavailable"))
    writer = FakeArmRepository()

    with pytest.raises(CanaryPreflightError, match="unavailable"):
        await CanarySession(source, writer).run(
            CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
            account_id=ACCOUNT_ID,
            now_ms=NOW,
        )

    assert writer.calls == []


@pytest.mark.parametrize(
    ("inputs", "now_ms", "message"),
    [
        (_inputs(), T0 + 90_000, "stale for the bounded"),
        (_inputs(venue=_venue(expires_at_ms=NOW + 999)), NOW, "insufficient remaining TTL"),
        (_inputs(account=_account(captured_at_ms=NOW - 30_001)), NOW, "account snapshot is stale"),
    ],
)
def test_stale_bar_venue_or_account_fails_closed(
    inputs: CanaryInputs,
    now_ms: int,
    message: str,
) -> None:
    with pytest.raises(CanaryPreflightError, match=message):
        prepare_canary(
            CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
            inputs,
            account_id=ACCOUNT_ID,
            now_ms=now_ms,
            account_max_age_ms=30_000,
            allocation_max_age_s=1_000,
        )


def test_invalid_worst_entry_geometry_fails_before_review() -> None:
    with pytest.raises(CanaryPreflightError, match="worst entry"):
        prepare_canary(
            CanaryPlan(symbol="BTCUSDT", side=Side.LONG, target_distance_bps=50.0),
            _inputs(venue=_venue(best_bid=100.79, best_ask=100.81)),
            account_id=ACCOUNT_ID,
            now_ms=NOW,
            account_max_age_ms=30_000,
            allocation_max_age_s=1_000,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"stop_distance_bps": 24.99},
        {"stop_distance_bps": 100.01},
        {"target_distance_bps": 24.99},
        {"target_distance_bps": 150.01},
        {"stop_distance_bps": 25.0, "target_distance_bps": 50.01},
        {"max_holding_ms": 59_999},
        {"max_holding_ms": 900_001},
        {"entry_window_ms": 4_999},
        {"entry_window_ms": 30_001},
    ],
)
def test_plan_rejects_unbounded_lifecycle_parameters(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {"symbol": "BTCUSDT", "side": Side.LONG}
    values.update(overrides)
    with pytest.raises(ValueError):
        CanaryPlan(**values)


def test_active_durable_reservation_blocks_new_canary() -> None:
    with pytest.raises(CanaryPreflightError, match="still reserved"):
        prepare_canary(
            CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
            _inputs(active_reservation_ids=("a" * 64,)),
            account_id=ACCOUNT_ID,
            now_ms=NOW,
            account_max_age_ms=30_000,
            allocation_max_age_s=1_000,
        )


@pytest.mark.asyncio
async def test_atomic_arm_enqueue_failure_has_no_separate_publish_stages() -> None:
    writer = FakeArmRepository(fail=True)

    with pytest.raises(CanaryPublishError, match="atomic"):
        await CanarySession(FakeSource(_inputs()), writer).run(
            CanaryPlan(symbol="BTCUSDT", side=Side.LONG),
            account_id=ACCOUNT_ID,
            now_ms=NOW,
            publish=True,
            arm=CANARY_ARM_PHRASE,
        )

    assert writer.calls == ["arm"]
    assert writer.arms == []


@pytest.mark.asyncio
async def test_session_cannot_prepare_a_second_candidate() -> None:
    session = CanarySession(FakeSource(_inputs()))
    plan = CanaryPlan(symbol="BTCUSDT", side=Side.LONG)
    await session.run(plan, account_id=ACCOUNT_ID, now_ms=NOW)

    with pytest.raises(canary_module.CanaryError, match="already prepared"):
        await session.run(replace(plan, side=Side.SHORT), account_id=ACCOUNT_ID, now_ms=NOW)


def test_cli_sanitizes_runtime_dependency_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail_without_exposing_secret(_args: object) -> object:
        raise RuntimeError("postgresql://user:do-not-print@database/kairos")

    monkeypatch.setattr(canary_module, "_run_cli", fail_without_exposing_secret)

    with pytest.raises(SystemExit) as raised:
        canary_module.main(["--symbol", "BTCUSDT", "--side", "LONG"])

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "RuntimeError" in stderr
    assert "do-not-print" not in stderr
