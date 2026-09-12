"""Manually armed, deterministic technical canary for EVEDEX DEV PAPER.

The default command is a read-only preview.  The mutation path atomically stores
one single-use arm with its canary-scoped allocation and enqueues the exact
CandidateReviewV1; it never creates a RiskTradeDecision, talks to an exchange,
or calls an LLM.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from kairos_core.contracts import (
    AccountSnapshotV2,
    CandidateReviewV1,
    CandidateRouteV1,
    ClosedBarEventV1,
    EvidenceReferenceV1,
    ExitPlanV1,
    RiskTradeDecisionV1,
    StrategicAllocation,
    StrategyIntentV1,
    StrategyProvenanceV1,
    VenueQualityV1,
    canonical_json_bytes,
    canonical_sha256,
)
from kairos_core.contracts.base import datetime_from_unix_ms
from kairos_core.enums import (
    CandidateReviewTier,
    EvedexProfile,
    MarketRegime,
    ReasoningEffort,
    ReviewDecision,
    Side,
    StrategicTrigger,
    TradingMode,
)
from kairos_core.topics import Topics
from kairos_persistence.config import PersistenceSettings
from kairos_persistence.database import Database

from .canary_authorization import (
    CANARY_ALLOCATION_RATIONALE,
    CANARY_ALLOCATION_WEIGHT,
    CANARY_SOURCE,
    CANARY_STRATEGY_REF,
    CANARY_STRATEGY_REVISION,
    PaperCanaryArmRepository,
    build_persistence_canary_repository,
    validate_armed_record,
    validate_canary_binding,
)
from .canary_instrument import (
    EvedexDevInstrumentRule,
    fetch_evedex_dev_instrument,
)
from .config import PAPER_CANARY_STRATEGY_ID, PAPER_DEV_SYMBOL_MAP, RiskSettings
from .strategy import allocation_error, is_fresh

CANARY_ARM_PHRASE = "ARM EVEDEX DEV PAPER CANARY"

_MIN_STOP_BPS = 25.0
_MAX_STOP_BPS = 100.0
_MIN_TARGET_BPS = 25.0
_MAX_TARGET_BPS = 150.0
_MAX_TARGET_STOP_RATIO = 2.0
_MIN_HOLDING_MS = 60_000
_MAX_HOLDING_MS = 15 * 60_000
_MIN_ENTRY_WINDOW_MS = 5_000
_MAX_ENTRY_WINDOW_MS = 30_000
_MIN_VENUE_TTL_MS = 1_000
_MAX_INSTRUMENT_FETCH_AGE_MS = 5_000
_MAX_INSTRUMENT_FETCH_CLOCK_SKEW_MS = 2_000
_PRICE_QUANTUM = Decimal("0.00000001")


class CanaryError(RuntimeError):
    """Safe operator-facing canary failure."""


class CanaryPreflightError(CanaryError):
    """Required durable state is absent, stale, conflicting, or unsafe."""


class CanaryArmingError(CanaryError):
    """Mutation authority was not supplied exactly."""


class CanaryPublishError(CanaryError):
    """The atomic durable arm/enqueue call did not confirm completion."""


@dataclass(frozen=True, slots=True)
class CanaryPlan:
    """Explicit bounded lifecycle parameters for one technical candidate."""

    symbol: str
    side: Side
    stop_distance_bps: float = 50.0
    target_distance_bps: float = 75.0
    max_holding_ms: int = 5 * 60_000
    entry_window_ms: int = 30_000

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if self.symbol != symbol or symbol not in PAPER_DEV_SYMBOL_MAP:
            raise ValueError("symbol must be one of BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT")
        if self.side not in {Side.LONG, Side.SHORT}:
            raise ValueError("technical canary side must be LONG or SHORT")
        for name, value in (
            ("stop_distance_bps", self.stop_distance_bps),
            ("target_distance_bps", self.target_distance_bps),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if not _MIN_STOP_BPS <= self.stop_distance_bps <= _MAX_STOP_BPS:
            raise ValueError(f"stop distance must be in [{_MIN_STOP_BPS:g}, {_MAX_STOP_BPS:g}] bps")
        if not _MIN_TARGET_BPS <= self.target_distance_bps <= _MAX_TARGET_BPS:
            raise ValueError(f"target distance must be in [{_MIN_TARGET_BPS:g}, {_MAX_TARGET_BPS:g}] bps")
        if self.target_distance_bps > self.stop_distance_bps * _MAX_TARGET_STOP_RATIO:
            raise ValueError("target distance cannot exceed 2x the bounded stop distance")
        if (
            isinstance(self.max_holding_ms, bool)
            or not isinstance(self.max_holding_ms, int)
            or not _MIN_HOLDING_MS <= self.max_holding_ms <= _MAX_HOLDING_MS
        ):
            raise ValueError("holding timeout must be between 60 and 900 seconds")
        if (
            isinstance(self.entry_window_ms, bool)
            or not isinstance(self.entry_window_ms, int)
            or not _MIN_ENTRY_WINDOW_MS <= self.entry_window_ms <= _MAX_ENTRY_WINDOW_MS
        ):
            raise ValueError("entry window must be between 5 and 30 seconds")

    def identity_payload(self) -> dict[str, object]:
        return {
            "entry_window_ms": self.entry_window_ms,
            "max_holding_ms": self.max_holding_ms,
            "side": self.side.value,
            "stop_distance_bps": self.stop_distance_bps,
            "strategy_ref": CANARY_STRATEGY_REF,
            "symbol": self.symbol,
            "target_distance_bps": self.target_distance_bps,
        }


@dataclass(frozen=True, slots=True)
class CanaryInputs:
    """Latest durable facts selected at one preflight boundary."""

    bar: ClosedBarEventV1
    venue: VenueQualityV1
    account: AccountSnapshotV2
    instrument: EvedexDevInstrumentRule
    current_allocation: StrategicAllocation | None = None
    active_reservation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedCanary:
    """The two deterministic messages and read-only readiness metadata."""

    allocation: StrategicAllocation
    review: CandidateReviewV1
    current_allocation_ready: bool
    current_allocation_reason: str

    def allocation_bytes(self) -> bytes:
        return canonical_json_bytes(self.allocation.to_payload())

    def review_bytes(self) -> bytes:
        return canonical_json_bytes(self.review.to_payload())


@dataclass(frozen=True, slots=True)
class CanaryRunResult:
    prepared: PreparedCanary
    published: bool
    arm_id: str | None = None

    def summary(self) -> dict[str, object]:
        intent = self.prepared.review.intent
        metadata = dict(intent.metadata)
        return {
            "account_id": metadata.get("account_id"),
            "arm_id": self.arm_id,
            "allocation_message_id": self.prepared.allocation.message_id,
            "current_allocation_ready": self.prepared.current_allocation_ready,
            "current_allocation_reason": self.prepared.current_allocation_reason,
            "entry_eligible_ts_ms": intent.entry_eligible_ts_ms,
            "entry_expires_ts_ms": intent.entry_expires_ts_ms,
            "intent_id": intent.intent_id,
            "instrument_rules_sha256": metadata.get("instrument_rules_sha256"),
            "mode": "PUBLISHED" if self.published else "PREVIEW",
            "review_id": self.prepared.review.review_id,
            "route_id": self.prepared.review.route.route_id,
            "side": intent.side.value,
            "strategy": CANARY_STRATEGY_REF,
            "symbol": intent.symbol,
            "quantity": metadata.get("canary_quantity"),
            "target_price": intent.exit_plan.target_price,
            "timeout_ms": intent.exit_plan.max_holding_ms,
            "venue_symbol": metadata.get("venue_symbol"),
            "stop_price": intent.exit_plan.stop_price,
        }


class CanaryInputSource(Protocol):
    async def load(self, plan: CanaryPlan, *, account_id: str) -> CanaryInputs: ...


class CanarySession:
    """Prepare and optionally publish exactly one candidate per process session."""

    def __init__(
        self,
        source: CanaryInputSource,
        arm_repository: PaperCanaryArmRepository | None = None,
    ) -> None:
        self._source = source
        self._arm_repository = arm_repository
        self._used = False

    async def run(
        self,
        plan: CanaryPlan,
        *,
        account_id: str,
        now_ms: int,
        publish: bool = False,
        arm: str | None = None,
        account_max_age_ms: int = 30_000,
        allocation_max_age_s: float = 26 * 60 * 60,
        session_id: str | None = None,
        slot_id: str | None = None,
    ) -> CanaryRunResult:
        if self._used:
            raise CanaryError("this canary session already prepared one candidate")
        self._used = True
        if publish and arm != CANARY_ARM_PHRASE:
            raise CanaryArmingError("exact DEV PAPER canary arm phrase is required for publication")
        if publish and self._arm_repository is None:
            raise CanaryArmingError("publication requested without a durable canary-arm repository")
        if publish and (not session_id or not slot_id):
            raise CanaryArmingError("publication requires a verified bounded session_id and slot_id")

        inputs = await self._source.load(plan, account_id=account_id)
        prepared = prepare_canary(
            plan,
            inputs,
            account_id=account_id,
            now_ms=now_ms,
            account_max_age_ms=account_max_age_ms,
            allocation_max_age_s=allocation_max_age_s,
        )
        if not publish:
            return CanaryRunResult(prepared=prepared, published=False)

        arm_repository = self._arm_repository
        if arm_repository is None:  # Defensive narrowing after the authority check above.
            raise CanaryArmingError("publication requested without a durable canary-arm repository")
        validate_canary_binding(
            prepared.review,
            prepared.allocation,
            account_id=account_id,
        )
        try:
            armed_record = await arm_repository.arm(
                account_id=account_id,
                review=prepared.review,
                allocation=prepared.allocation,
                session_id=session_id,
                slot_id=slot_id,
            )
            arm_id = validate_armed_record(
                armed_record,
                prepared.review,
                prepared.allocation,
                account_id=account_id,
                now_ms=now_ms,
            )
        except Exception as exc:
            raise CanaryPublishError(
                "atomic canary arm/enqueue did not confirm completion; retrying the exact binding is "
                "idempotent"
            ) from exc
        return CanaryRunResult(prepared=prepared, published=True, arm_id=arm_id)


def prepare_canary(
    plan: CanaryPlan,
    inputs: CanaryInputs,
    *,
    account_id: str,
    now_ms: int,
    account_max_age_ms: int,
    allocation_max_age_s: float,
) -> PreparedCanary:
    """Validate durable facts and build byte-stable allocation/review messages."""

    _validate_now(now_ms)
    _validate_inputs(
        plan,
        inputs,
        account_id=account_id,
        now_ms=now_ms,
        account_max_age_ms=account_max_age_ms,
    )
    intent = _build_intent(
        plan,
        inputs.bar,
        inputs.venue,
        inputs.instrument,
        account_id=account_id,
    )
    _validate_executable_geometry(intent, inputs.venue)
    route = CandidateRouteV1(
        source=CANARY_SOURCE,
        correlation_id=intent.intent_id,
        causation_id=intent.message_id,
        intent=intent,
        review_tier=CandidateReviewTier.NORMAL,
        requested_reasoning_effort=ReasoningEffort.MEDIUM,
        routed_at_ms=inputs.bar.close_time_ms,
        review_deadline_ms=intent.entry_expires_ts_ms,
        evidence_ids=tuple(
            item.content_sha256 for item in intent.evidence if item.content_sha256 is not None
        ),
    )
    review = CandidateReviewV1(
        source=CANARY_SOURCE,
        correlation_id=intent.intent_id,
        causation_id=route.message_id,
        route=route,
        intent=intent,
        decision=ReviewDecision.ALLOW,
        priority=0,
        reviewed_at_ms=intent.entry_eligible_ts_ms,
        reviewer="DETERMINISTIC",
        reason_codes=("TECHNICAL_CANARY_MANUAL_POLICY",),
        evidence=intent.evidence,
    )
    allocation = _build_canary_allocation(intent, plan)
    ready, reason = _allocation_status(
        inputs.current_allocation,
        plan=plan,
        now_ms=now_ms,
        max_age_s=allocation_max_age_s,
    )
    return PreparedCanary(
        allocation=allocation,
        review=review,
        current_allocation_ready=ready,
        current_allocation_reason=reason,
    )


def _validate_now(now_ms: int) -> None:
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")


@lru_cache(maxsize=1)
def _strategy_code_sha256() -> str:
    """Fingerprint the normalized implementation source across Windows/Linux checkouts."""

    source = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _validate_inputs(
    plan: CanaryPlan,
    inputs: CanaryInputs,
    *,
    account_id: str,
    now_ms: int,
    account_max_age_ms: int,
) -> None:
    bar = inputs.bar
    if bar.symbol != plan.symbol:
        raise CanaryPreflightError("latest closed bar belongs to a different signal symbol")
    eligible_at_ms = bar.close_time_ms + 1
    expires_at_ms = eligible_at_ms + plan.entry_window_ms
    if now_ms <= bar.close_time_ms:
        raise CanaryPreflightError("current clock has not advanced past the selected closed bar")
    if now_ms < eligible_at_ms:
        raise CanaryPreflightError("NEXT_BAR_MARKET entry is not yet eligible")
    if now_ms >= expires_at_ms:
        raise CanaryPreflightError("latest closed bar is stale for the bounded canary entry window")

    expected_venue_symbol = PAPER_DEV_SYMBOL_MAP[plan.symbol]
    venue = inputs.venue
    if venue.profile is not EvedexProfile.DEV or venue.symbol != expected_venue_symbol:
        raise CanaryPreflightError("venue quality is not the exact mapped EVEDEX DEV instrument")
    if venue.observed_at_ms > now_ms:
        raise CanaryPreflightError("venue quality observation is from the future")
    if venue.expires_at_ms - now_ms < _MIN_VENUE_TTL_MS:
        raise CanaryPreflightError("venue quality is stale or has insufficient remaining TTL")
    if not venue.entry_allowed:
        raise CanaryPreflightError("venue quality gate does not allow entry")

    account = inputs.account
    if (
        account.trading_mode is not TradingMode.PAPER
        or account.evedex_profile is not EvedexProfile.DEV
        or account.account_id != account_id
    ):
        raise CanaryPreflightError(
            "account snapshot is not authoritative for the dedicated PAPER/DEV account"
        )
    if not account.reconciled or account.reconciliation_seq <= 0:
        raise CanaryPreflightError("account snapshot is not a completed authoritative reconciliation")
    account_age_ms = now_ms - account.captured_at_ms
    if account_age_ms < 0:
        raise CanaryPreflightError("account snapshot is from the future")
    if account_age_ms > account_max_age_ms:
        raise CanaryPreflightError("account snapshot is stale")
    if any(
        position.strategy_id == PAPER_CANARY_STRATEGY_ID or position.venue_symbol == expected_venue_symbol
        for position in account.positions
    ):
        raise CanaryPreflightError("an active canary or same-symbol position already exists")
    if any(
        order.strategy_id == PAPER_CANARY_STRATEGY_ID or order.venue_symbol == expected_venue_symbol
        for order in account.open_orders
    ):
        raise CanaryPreflightError("an active canary or same-symbol open order already exists")
    if inputs.active_reservation_ids:
        raise CanaryPreflightError("another durable technical-canary approval is still reserved")

    instrument = inputs.instrument
    if instrument.venue_symbol != expected_venue_symbol:
        raise CanaryPreflightError("instrument rule belongs to a different EVEDEX DEV instrument")
    fetch_age_ms = now_ms - instrument.fetched_at_ms
    if fetch_age_ms < -_MAX_INSTRUMENT_FETCH_CLOCK_SKEW_MS:
        raise CanaryPreflightError("instrument rule fetch time is from the future")
    if fetch_age_ms > _MAX_INSTRUMENT_FETCH_AGE_MS:
        raise CanaryPreflightError("instrument rule fetch is stale")
    if instrument.updated_at_ms > now_ms + _MAX_INSTRUMENT_FETCH_CLOCK_SKEW_MS:
        raise CanaryPreflightError("instrument rule update time is from the future")


def _build_intent(
    plan: CanaryPlan,
    bar: ClosedBarEventV1,
    venue: VenueQualityV1,
    instrument: EvedexDevInstrumentRule,
    *,
    account_id: str,
) -> StrategyIntentV1:
    reference = Decimal(str(bar.close))
    stop_fraction = Decimal(str(plan.stop_distance_bps)) / Decimal(10_000)
    target_fraction = Decimal(str(plan.target_distance_bps)) / Decimal(10_000)
    if plan.side is Side.LONG:
        stop = reference * (Decimal(1) - stop_fraction)
        target = reference * (Decimal(1) + target_fraction)
    else:
        stop = reference * (Decimal(1) + stop_fraction)
        target = reference * (Decimal(1) - target_fraction)
    stop_price = float(stop.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_EVEN))
    target_price = float(target.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_EVEN))
    reference_price = float(reference)
    gross_reward_bps = abs(target_price - reference_price) / reference_price * 10_000
    bar_hash = bar.bar_sha256
    if bar_hash is None:  # strict validation always assigns it
        raise CanaryPreflightError("closed bar has no canonical SHA-256")
    plan_payload = plan.identity_payload()
    worst_entry = venue.best_ask if plan.side is Side.LONG else venue.best_bid
    canary_quantity = instrument.effective_min_quantity(Decimal(str(worst_entry)))
    features = {
        "canary_quantity": str(canary_quantity),
        "gross_reward_bps": gross_reward_bps,
        "instrument_rules_sha256": instrument.rules_sha256,
        "reference_price": reference_price,
        "side": plan.side.value,
        "stop_price": stop_price,
        "target_price": target_price,
    }
    bar_evidence = EvidenceReferenceV1(
        kind="closed_bar",
        reference=f"BINANCE_UM:{bar.symbol}:{bar.open_time_ms}",
        content_sha256=bar_hash,
        observed_at_ms=bar.close_time_ms,
    )
    return StrategyIntentV1(
        source=CANARY_SOURCE,
        strategy_id=PAPER_CANARY_STRATEGY_ID,
        strategy_revision=CANARY_STRATEGY_REVISION,
        symbol=bar.symbol,
        side=plan.side,
        decision_ts_ms=bar.close_time_ms,
        entry_eligible_ts_ms=bar.close_time_ms + 1,
        entry_expires_ts_ms=bar.close_time_ms + 1 + plan.entry_window_ms,
        reference_price=reference_price,
        signal_strength=0.0,
        gross_reward_bps=gross_reward_bps,
        exit_plan=ExitPlanV1(
            stop_price=stop_price,
            target_price=target_price,
            max_holding_ms=plan.max_holding_ms,
        ),
        provenance=StrategyProvenanceV1(
            strategy_code_sha256=_strategy_code_sha256(),
            config_sha256=canonical_sha256(plan_payload),
            input_window_sha256=canonical_sha256({"bar_sha256s": [bar_hash]}),
            features_sha256=canonical_sha256(features),
            input_bar_sha256s=(bar_hash,),
        ),
        evidence=(bar_evidence, instrument.evidence()),
        metadata=(
            ("account_id", account_id),
            ("alpha_claim", "false"),
            ("entry_policy", "NEXT_BAR_MARKET"),
            ("purpose", "technical_execution_canary"),
            *instrument.metadata(quantity=canary_quantity),
        ),
    )


def _validate_executable_geometry(intent: StrategyIntentV1, venue: VenueQualityV1) -> None:
    exits = intent.exit_plan
    if intent.side is Side.LONG and not exits.stop_price < venue.best_ask < exits.target_price:
        raise CanaryPreflightError("LONG stop/target do not protect the current EVEDEX worst entry")
    if intent.side is Side.SHORT and not exits.target_price < venue.best_bid < exits.stop_price:
        raise CanaryPreflightError("SHORT stop/target do not protect the current EVEDEX worst entry")


def _build_canary_allocation(intent: StrategyIntentV1, plan: CanaryPlan) -> StrategicAllocation:
    regime = MarketRegime.BULL if plan.side is Side.LONG else MarketRegime.BEAR
    produced_at_ms = intent.entry_eligible_ts_ms
    rationale = CANARY_ALLOCATION_RATIONALE
    identity = {
        "causation_id": intent.message_id,
        "contract_version": "technical-canary-allocation.v1",
        "correlation_id": intent.intent_id,
        "max_gross_leverage": 1.0,
        "produced_at_ms": produced_at_ms,
        "regime": regime.value,
        "rationale": rationale,
        "schema_version": "1.0",
        "source": CANARY_SOURCE,
        "stable_reserve_pct": 1.0 - CANARY_ALLOCATION_WEIGHT,
        "strategy_weights": {PAPER_CANARY_STRATEGY_ID: CANARY_ALLOCATION_WEIGHT},
        "triggered_by": StrategicTrigger.SCHEDULE.value,
    }
    message_id = canonical_sha256(identity)
    return StrategicAllocation(
        source=CANARY_SOURCE,
        message_id=message_id,
        correlation_id=intent.intent_id,
        causation_id=intent.message_id,
        produced_at=datetime_from_unix_ms(produced_at_ms),
        regime=regime,
        stable_reserve_pct=1.0 - CANARY_ALLOCATION_WEIGHT,
        strategy_weights={PAPER_CANARY_STRATEGY_ID: CANARY_ALLOCATION_WEIGHT},
        max_gross_leverage=1.0,
        triggered_by=StrategicTrigger.SCHEDULE,
        rationale=rationale,
    )


def _allocation_status(
    allocation: StrategicAllocation | None,
    *,
    plan: CanaryPlan,
    now_ms: int,
    max_age_s: float,
) -> tuple[bool, str]:
    if allocation is None:
        return False, "missing; an armed review will carry a bound deterministic canary allocation"
    invalid = allocation_error(allocation)
    if invalid is not None:
        return False, invalid
    now = datetime.fromtimestamp(now_ms / 1_000, tz=UTC)
    if not is_fresh(allocation, max_age_s=max_age_s, now=now):
        return False, "stale"
    weight = allocation.strategy_weights.get(PAPER_CANARY_STRATEGY_ID, 0.0)
    if weight <= 0:
        return False, "technical-canary has no positive weight"
    if allocation.max_gross_leverage > 1.0:
        return False, "current allocation exceeds 1x canary leverage"
    if allocation.regime is MarketRegime.CHOP:
        return False, "CHOP blocks canary entry"
    if allocation.regime is MarketRegime.BEAR and plan.side is Side.LONG:
        return False, "BEAR blocks LONG"
    if allocation.regime is MarketRegime.BULL and plan.side is Side.SHORT:
        return False, "BULL blocks SHORT"
    return True, "current allocation already permits this canary"


def _json_payload(row: Any, *, label: str) -> dict[str, Any]:
    if row is None:
        raise CanaryPreflightError(f"latest durable {label} is unavailable")
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise CanaryPreflightError(f"latest durable {label} payload is not a JSON object")
    return payload


class InstrumentRuleLoader(Protocol):
    async def __call__(
        self,
        venue_symbol: str,
        *,
        fetched_at_ms: int,
    ) -> EvedexDevInstrumentRule: ...


class PostgresCanaryInputSource:
    """Read durable facts plus the current official public DEV instrument rule."""

    def __init__(
        self,
        pool: Any,
        instrument_loader: InstrumentRuleLoader | None = None,
    ) -> None:
        self._pool = pool
        self._instrument_loader = instrument_loader or fetch_evedex_dev_instrument

    async def load(self, plan: CanaryPlan, *, account_id: str) -> CanaryInputs:
        venue_symbol = PAPER_DEV_SYMBOL_MAP[plan.symbol]
        fetched_at_ms = time.time_ns() // 1_000_000
        instrument = await self._instrument_loader(
            venue_symbol,
            fetched_at_ms=fetched_at_ms,
        )
        async with self._pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                return await self._load_snapshot(
                    connection,
                    plan,
                    account_id=account_id,
                    instrument=instrument,
                )

    async def _load_snapshot(
        self,
        connection: Any,
        plan: CanaryPlan,
        *,
        account_id: str,
        instrument: EvedexDevInstrumentRule,
    ) -> CanaryInputs:
        venue_symbol = PAPER_DEV_SYMBOL_MAP[plan.symbol]
        bar_row = await connection.fetchrow(
            """SELECT payload FROM event_audit
               WHERE topic=$1 AND payload->>'symbol'=$2
               ORDER BY (payload->>'close_time_ms')::bigint DESC, persisted_at DESC
               LIMIT 1""",
            Topics.CLOSED_BAR,
            plan.symbol,
        )
        venue_row = await connection.fetchrow(
            """SELECT payload FROM event_audit
               WHERE topic=$1 AND payload->>'profile'='DEV' AND payload->>'symbol'=$2
               ORDER BY (payload->>'observed_at_ms')::bigint DESC, persisted_at DESC
               LIMIT 1""",
            Topics.VENUE_QUALITY,
            venue_symbol,
        )
        account_row = await connection.fetchrow(
            """SELECT payload FROM event_audit
               WHERE topic=$1 AND payload->>'trading_mode'='PAPER'
                 AND payload->>'evedex_profile'='DEV' AND payload->>'account_id'=$2
               ORDER BY (payload->>'reconciliation_seq')::bigint DESC,
                        (payload->>'captured_at_ms')::bigint DESC, persisted_at DESC
               LIMIT 1""",
            Topics.ACCOUNT_SNAPSHOT_V2,
            account_id,
        )
        allocation_row = await connection.fetchrow(
            """SELECT payload FROM event_audit
               WHERE topic=$1 ORDER BY produced_at DESC, persisted_at DESC LIMIT 1""",
            Topics.STRATEGIC_ALLOCATION,
        )
        account = AccountSnapshotV2.model_validate(_json_payload(account_row, label="account snapshot"))
        reservation_rows = await connection.fetch(
            """SELECT payload FROM event_audit
               WHERE topic=$1 AND payload->>'approved'='true'
                 AND payload->>'trading_mode'='PAPER' AND payload->>'evedex_profile'='DEV'
                 AND payload->>'account_id'=$2
                 AND payload->'intent'->>'strategy_id'=$3
                 AND (payload->'intent'->>'entry_expires_ts_ms')::bigint >= $4
               ORDER BY produced_at, message_id""",
            Topics.RISK_TRADE_DECISION,
            account_id,
            PAPER_CANARY_STRATEGY_ID,
            account.captured_at_ms,
        )
        reservation_ids: list[str] = []
        for row in reservation_rows:
            decision = RiskTradeDecisionV1.model_validate(_json_payload(row, label="canary reservation"))
            if decision.decision_id is not None:
                reservation_ids.append(decision.decision_id)
        allocation = (
            None
            if allocation_row is None
            else StrategicAllocation.model_validate(
                _json_payload(allocation_row, label="strategic allocation")
            )
        )
        return CanaryInputs(
            bar=ClosedBarEventV1.model_validate(_json_payload(bar_row, label="closed bar")),
            venue=VenueQualityV1.model_validate(_json_payload(venue_row, label="venue quality")),
            account=account,
            instrument=instrument,
            current_allocation=allocation,
            active_reservation_ids=tuple(sorted(set(reservation_ids))),
        )


def _validate_publish_settings(settings: RiskSettings) -> None:
    if settings.trading_mode is not TradingMode.PAPER:
        raise CanaryArmingError("publication requires KAIROS_TRADING_MODE=PAPER")
    if settings.evedex_profile is not EvedexProfile.DEV:
        raise CanaryArmingError("publication requires the exact EVEDEX DEV profile")
    if settings.bus_backend == "memory":
        raise CanaryArmingError("publication requires durable Redis/PostgreSQL transport")
    if not settings.paper_strategy_allowed(PAPER_CANARY_STRATEGY_ID, CANARY_STRATEGY_REVISION):
        raise CanaryArmingError(
            f"publication requires KAIROS_PAPER_STRATEGY_ALLOWLIST containing {CANARY_STRATEGY_REF}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairos-paper-canary",
        description="Preview or manually arm one bounded EVEDEX DEV PAPER technical canary.",
    )
    parser.add_argument("--symbol", required=True, choices=tuple(PAPER_DEV_SYMBOL_MAP))
    parser.add_argument("--side", required=True, choices=(Side.LONG.value, Side.SHORT.value))
    parser.add_argument("--stop-bps", type=float, default=50.0)
    parser.add_argument("--target-bps", type=float, default=75.0)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--entry-window-seconds", type=int, default=30)
    parser.add_argument("--publish", action="store_true", help="atomically arm and enqueue one review")
    parser.add_argument("--session-id", help="existing verified bounded session; required for publication")
    parser.add_argument("--slot-id", help="exact immutable session slot; required for publication")
    parser.add_argument(
        "--arm",
        metavar="PHRASE",
        help=f'exact publication phrase: "{CANARY_ARM_PHRASE}"',
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> CanaryRunResult:
    settings = RiskSettings()
    plan = CanaryPlan(
        symbol=args.symbol,
        side=Side(args.side),
        stop_distance_bps=args.stop_bps,
        target_distance_bps=args.target_bps,
        max_holding_ms=args.timeout_seconds * 1_000,
        entry_window_ms=args.entry_window_seconds * 1_000,
    )
    now_ms = time.time_ns() // 1_000_000
    if args.publish:
        if args.arm != CANARY_ARM_PHRASE:
            raise CanaryArmingError("exact DEV PAPER canary arm phrase is required for publication")
        if not args.session_id or not args.slot_id:
            raise CanaryArmingError("publication requires a verified bounded session_id and slot_id")
        _validate_publish_settings(settings)
        database = Database(PersistenceSettings())
        await database.connect()
        try:
            await database.migrate()
            source = PostgresCanaryInputSource(database.pool)
            arm_repository = _build_persistence_arm_repository(database)
            return await CanarySession(source, arm_repository).run(
                plan,
                account_id=settings.paper_account_id,
                now_ms=now_ms,
                publish=True,
                arm=args.arm,
                session_id=args.session_id,
                slot_id=args.slot_id,
                account_max_age_ms=int(settings.paper_account_snapshot_max_age_s * 1_000),
                allocation_max_age_s=settings.strategic_allocation_max_age_s,
            )
        finally:
            await database.close()

    database = Database(PersistenceSettings())
    await database.connect()
    try:
        source = PostgresCanaryInputSource(database.pool)
        return await CanarySession(source).run(
            plan,
            account_id=settings.paper_account_id,
            now_ms=now_ms,
            account_max_age_ms=int(settings.paper_account_snapshot_max_age_s * 1_000),
            allocation_max_age_s=settings.strategic_allocation_max_age_s,
        )
    finally:
        await database.close()


def _build_persistence_arm_repository(database: Database) -> PaperCanaryArmRepository:
    return build_persistence_canary_repository(database.pool)


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    try:
        result = asyncio.run(_run_cli(parser.parse_args(argv)))
    except (CanaryError, ValueError) as exc:
        print(f"canary refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        # Database/Redis exceptions can embed connection strings. Keep the
        # operator-visible failure useful without echoing credentials.
        print(
            f"canary refused: runtime dependency failure ({type(exc).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    print(json.dumps(result.summary(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
