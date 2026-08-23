"""Risk-side checks for the durable manually armed PAPER canary.

``kairos-persistence`` owns the transaction, row locks, single-use state and
CandidateReview outbox. These structural protocols keep preview and unit tests
network-free while this module independently verifies the exact record returned
to the Risk PAPER consumer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, cast

from kairos_core.contracts import CandidateReviewV1, StrategicAllocation, canonical_sha256
from kairos_core.enums import (
    CandidateReviewTier,
    MarketRegime,
    ReasoningEffort,
    ReviewDecision,
    Side,
    StrategicTrigger,
)
from kairos_persistence import PaperCanaryArmRepository as PersistencePaperCanaryArmRepository

from .canary_instrument import bound_canary_instrument
from .config import PAPER_CANARY_STRATEGY_ID, PAPER_DEV_SYMBOL_MAP

CANARY_STRATEGY_REVISION = "1"
CANARY_STRATEGY_REF = f"{PAPER_CANARY_STRATEGY_ID}@{CANARY_STRATEGY_REVISION}"
CANARY_SOURCE = "kairos-paper-canary"
CANARY_ALLOCATION_WEIGHT = 0.0025
CANARY_ALLOCATION_RATIONALE = "Manually armed EVEDEX DEV technical canary; no alpha or LLM claim."
CANARY_MAX_CLOCK_SKEW_MS = 2_000


class CanaryAuthorizationError(RuntimeError):
    """The durable record does not authorize this exact canary review."""


class PaperCanaryArmRecord(Protocol):
    """Structural view of ``kairos_persistence.PaperCanaryArm``."""

    arm_id: str
    account_id: str
    review: CandidateReviewV1
    allocation: StrategicAllocation
    status: str
    expires_at: datetime
    decided_at_ms: int | None


class PaperCanaryArmRepository(Protocol):
    """Persistence API used by the CLI and Risk PAPER consumer."""

    async def arm(
        self,
        *,
        account_id: str,
        review: CandidateReviewV1,
        allocation: StrategicAllocation,
    ) -> PaperCanaryArmRecord:
        """Atomically store the arm, audit the review and enqueue its outbox row."""

    async def consume(
        self,
        *,
        account_id: str,
        review: CandidateReviewV1,
    ) -> PaperCanaryArmRecord | None:
        """Atomically consume once or replay the same exact consumed record."""


class RejectAllPaperCanaryArmRepository:
    """Fail-closed placeholder until the durable repository is installed."""

    async def arm(
        self,
        *,
        account_id: str,
        review: CandidateReviewV1,
        allocation: StrategicAllocation,
    ) -> PaperCanaryArmRecord:
        del account_id, review, allocation
        raise CanaryAuthorizationError("durable PAPER canary repository is unavailable")

    async def consume(
        self,
        *,
        account_id: str,
        review: CandidateReviewV1,
    ) -> PaperCanaryArmRecord | None:
        del account_id, review
        return None


def build_persistence_canary_repository(pool: Any) -> PaperCanaryArmRepository:
    """Load the pinned persistence implementation without touching external state."""

    return cast(PaperCanaryArmRepository, PersistencePaperCanaryArmRepository(pool))


def canary_arm_identity(
    *,
    account_id: str,
    review: CandidateReviewV1,
    allocation: StrategicAllocation,
) -> str:
    """Match the persistence repository's canonical arm identity exactly."""

    return canonical_sha256(
        {
            "account_id": account_id,
            "allocation": allocation.model_dump(mode="json"),
            "domain": "kairos.paper-canary-arm.v1",
            "review": review.model_dump(mode="json"),
        }
    )


def allocation_identity(allocation: StrategicAllocation) -> str:
    """Recompute the identity of the exact deterministic canary allocation."""

    produced_at = allocation.produced_at
    if produced_at.utcoffset() is None:
        raise ValueError("canary allocation produced_at must be timezone-aware")
    produced_at_ms = int(produced_at.astimezone(UTC).timestamp() * 1_000)
    return canonical_sha256(
        {
            "causation_id": allocation.causation_id,
            "contract_version": "technical-canary-allocation.v1",
            "correlation_id": allocation.correlation_id,
            "max_gross_leverage": allocation.max_gross_leverage,
            "produced_at_ms": produced_at_ms,
            "regime": allocation.regime.value,
            "rationale": allocation.rationale,
            "schema_version": allocation.schema_version,
            "source": allocation.source,
            "stable_reserve_pct": allocation.stable_reserve_pct,
            "strategy_weights": allocation.strategy_weights,
            "triggered_by": allocation.triggered_by.value,
        }
    )


def validate_canary_binding(
    review: CandidateReviewV1,
    allocation: StrategicAllocation,
    *,
    account_id: str,
) -> None:
    """Validate the exact model-free policy before arm and after consume."""

    intent = review.intent
    if review.decision is not ReviewDecision.ALLOW:
        raise ValueError("technical canary arm requires an ALLOW review")
    if (
        review.source != CANARY_SOURCE
        or review.route.source != CANARY_SOURCE
        or intent.source != CANARY_SOURCE
    ):
        raise ValueError("technical canary arm requires the dedicated canary source lineage")
    if review.reviewer != "DETERMINISTIC" or review.model_provenance is not None:
        raise ValueError("technical canary arm must be deterministic and model-free")
    if review.reason_codes != ("TECHNICAL_CANARY_MANUAL_POLICY",):
        raise ValueError("technical canary arm requires the exact manual policy reason")
    if (
        review.priority != 0
        or review.route.review_tier is not CandidateReviewTier.NORMAL
        or review.route.requested_reasoning_effort is not ReasoningEffort.MEDIUM
    ):
        raise ValueError("technical canary review policy or tier is not exact")
    if review.route.intent.model_dump(mode="json") != intent.model_dump(mode="json"):
        raise ValueError("technical canary route changed its immutable intent")
    if intent.strategy_id != PAPER_CANARY_STRATEGY_ID or intent.strategy_revision != CANARY_STRATEGY_REVISION:
        raise ValueError("durable arm is restricted to technical-canary@1")
    if review.message_id != review.review_id or intent.message_id != intent.intent_id:
        raise ValueError("technical canary requires canonical review and intent identities")
    try:
        instrument, _quantity = bound_canary_instrument(intent, account_id=account_id)
    except ValueError as exc:
        raise ValueError("technical canary instrument binding is invalid") from exc
    if instrument.venue_symbol != PAPER_DEV_SYMBOL_MAP.get(intent.symbol):
        raise ValueError("technical canary signal/venue symbol binding is invalid")
    input_bars = intent.provenance.input_bar_sha256s
    bar_evidence = tuple(item for item in intent.evidence if item.kind == "closed_bar")
    if (
        intent.signal_strength != 0.0
        or len(input_bars) != 1
        or len(intent.evidence) != 2
        or len(bar_evidence) != 1
        or bar_evidence[0].content_sha256 != input_bars[0]
        or bar_evidence[0].observed_at_ms != intent.decision_ts_ms
        or not bar_evidence[0].reference.startswith(f"BINANCE_UM:{intent.symbol}:")
        or review.evidence != intent.evidence
        or review.route.evidence_ids != tuple(sorted((input_bars[0], instrument.rules_sha256)))
        or review.route.routed_at_ms != intent.decision_ts_ms
        or review.route.review_deadline_ms != intent.entry_expires_ts_ms
        or review.reviewed_at_ms != intent.entry_eligible_ts_ms
    ):
        raise ValueError("technical canary bar evidence or timing is not exact")
    _validate_canary_allocation(allocation, review)


def validate_armed_record(
    arm: PaperCanaryArmRecord,
    review: CandidateReviewV1,
    allocation: StrategicAllocation,
    *,
    account_id: str,
    now_ms: int,
) -> str:
    """Confirm that persistence stored the exact arm requested by the CLI."""

    if arm.status not in {"ARMED", "CONSUMED"} or arm.account_id != account_id:
        raise CanaryAuthorizationError("persistence returned no active arm for this account")
    if arm.review.model_dump(mode="json") != review.model_dump(mode="json") or arm.allocation.model_dump(
        mode="json"
    ) != allocation.model_dump(mode="json"):
        raise CanaryAuthorizationError("persistence returned a canary arm with conflicting payloads")
    expected_arm_id = canary_arm_identity(
        account_id=account_id,
        review=review,
        allocation=allocation,
    )
    if arm.arm_id != expected_arm_id:
        raise CanaryAuthorizationError("persistence returned a conflicting canary arm identity")
    expires_at = arm.expires_at
    if expires_at.utcoffset() is None:
        raise CanaryAuthorizationError("durable canary expiry must be timezone-aware")
    expires_at_ms = int(expires_at.astimezone(UTC).timestamp() * 1_000)
    if expires_at_ms != review.intent.entry_expires_ts_ms or now_ms >= expires_at_ms:
        raise CanaryAuthorizationError("persistence returned an expired or conflicting canary arm")
    validate_canary_binding(review, allocation, account_id=account_id)
    return arm.arm_id


def allocation_from_consumed_arm(
    arm: PaperCanaryArmRecord,
    review: CandidateReviewV1,
    *,
    account_id: str,
    now_ms: int,
) -> tuple[StrategicAllocation, int]:
    """Return the exact bound allocation and authoritative DB decision time."""

    if arm.status != "CONSUMED" or arm.account_id != account_id:
        raise CanaryAuthorizationError("durable canary arm is not consumed for this account")
    if arm.review.model_dump(mode="json") != review.model_dump(mode="json"):
        raise CanaryAuthorizationError("durable canary arm does not match the exact review")
    expected_arm_id = canary_arm_identity(
        account_id=account_id,
        review=arm.review,
        allocation=arm.allocation,
    )
    if arm.arm_id != expected_arm_id:
        raise CanaryAuthorizationError("durable canary arm identity is invalid")
    expires_at = arm.expires_at
    if expires_at.utcoffset() is None:
        raise CanaryAuthorizationError("durable canary expiry must be timezone-aware")
    expires_at_ms = int(expires_at.astimezone(UTC).timestamp() * 1_000)
    if expires_at_ms != review.intent.entry_expires_ts_ms or now_ms >= expires_at_ms:
        raise CanaryAuthorizationError("durable canary arm is expired or has a conflicting expiry")
    decided_at_ms = arm.decided_at_ms
    if (
        decided_at_ms is None
        or decided_at_ms < review.intent.entry_eligible_ts_ms
        or decided_at_ms < review.reviewed_at_ms
        or decided_at_ms >= expires_at_ms
        or decided_at_ms > now_ms + CANARY_MAX_CLOCK_SKEW_MS
    ):
        raise CanaryAuthorizationError("durable canary decision time is outside the entry window")
    try:
        validate_canary_binding(review, arm.allocation, account_id=account_id)
    except ValueError as exc:
        raise CanaryAuthorizationError("durable canary allocation is not the exact bound policy") from exc
    return arm.allocation, decided_at_ms


def _validate_canary_allocation(
    allocation: StrategicAllocation,
    review: CandidateReviewV1,
) -> None:
    intent = review.intent
    if (
        allocation.source != CANARY_SOURCE
        or allocation.correlation_id != intent.intent_id
        or allocation.causation_id != intent.message_id
    ):
        raise ValueError("canary allocation lineage does not match the intent")
    if allocation.message_id != allocation_identity(allocation):
        raise ValueError("canary allocation message identity does not match its full policy payload")
    if allocation.schema_version != "1.0":
        raise ValueError("canary allocation schema version is not exact")
    if allocation.max_gross_leverage != 1.0:
        raise ValueError("canary allocation must be exactly 1x")
    expected_weights = {PAPER_CANARY_STRATEGY_ID: CANARY_ALLOCATION_WEIGHT}
    if allocation.strategy_weights != expected_weights:
        raise ValueError("canary allocation must contain only the bounded technical-canary weight")
    if allocation.stable_reserve_pct != 1.0 - CANARY_ALLOCATION_WEIGHT:
        raise ValueError("canary allocation stable reserve is not exact")
    expected_regime = MarketRegime.BULL if intent.side is Side.LONG else MarketRegime.BEAR
    if allocation.regime is not expected_regime:
        raise ValueError("canary allocation regime is not direction-compatible")
    produced_at_ms = int(allocation.produced_at.astimezone(UTC).timestamp() * 1_000)
    if produced_at_ms != intent.entry_eligible_ts_ms:
        raise ValueError("canary allocation timestamp does not match NEXT_BAR_MARKET eligibility")
    if allocation.triggered_by is not StrategicTrigger.SCHEDULE:
        raise ValueError("canary allocation trigger must be deterministic")
    if allocation.rationale != CANARY_ALLOCATION_RATIONALE:
        raise ValueError("canary allocation rationale is not the fixed manual policy")
