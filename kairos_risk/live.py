"""Opt-in pure LIVE risk evaluation.

This module deliberately does not wire a bus, database, exchange, configuration
flag, or execution signer. A deployment must inject a registered account-source
verifier that binds a complete authoritative account proof to the exact frozen
scope. With no verifier, every evaluation is rejected.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from kairos_core.contracts import (
    AccountSnapshotV2,
    CandidateReviewV1,
    RiskTradeDecisionV1,
    StrategicAllocation,
    StrategyIntentV1,
    VenueQualityV1,
    canonical_sha256,
)
from kairos_core.contracts.base import StrictValueModel
from kairos_core.enums import (
    EntryPolicy,
    EvedexProfile,
    MarketRegime,
    OrderRole,
    ReviewDecision,
    Side,
    SystemMode,
    TradingMode,
)
from pydantic import Field, model_validator

from .strategy import allocation_error

_MAX_ACCOUNT_AGE_MS = 5_000
_MAX_VENUE_AGE_MS = 2_000
_MAX_ALLOCATION_AGE_MS = 26 * 60 * 60 * 1_000
_QUANTITY_QUANTUM = Decimal("0.00000001")


class LiveRiskScopeV1(StrictValueModel):
    """Frozen non-secret identity for one independently released LIVE policy."""

    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_set_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    risk_limits_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    strategy_id: str = Field(min_length=1, max_length=128)
    strategy_revision: str = Field(min_length=1, max_length=128)
    # Pairs are (immutable signal symbol, exact PROD venue symbol).
    approved_prod_symbols: tuple[tuple[str, str], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> LiveRiskScopeV1:
        for name in ("account_id", "strategy_id", "strategy_revision"):
            value = getattr(self, name)
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        pairs = tuple(sorted(self.approved_prod_symbols))
        if len({signal for signal, _venue in pairs}) != len(pairs):
            raise ValueError("approved PROD signal symbols must be unique")
        if len({venue for _signal, venue in pairs}) != len(pairs):
            raise ValueError("approved PROD venue symbols must be unique")
        for signal, venue in pairs:
            if not signal or signal != signal.strip().upper():
                raise ValueError("approved signal symbols must be normalized uppercase")
            if not venue or venue != venue.strip().upper() or not venue.endswith(":PROD"):
                raise ValueError("approved venue symbols must be normalized PROD symbols")
        object.__setattr__(self, "approved_prod_symbols", pairs)
        return self

    @property
    def scope_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def venue_symbol_for(self, signal_symbol: str) -> str | None:
        return next((venue for signal, venue in self.approved_prod_symbols if signal == signal_symbol), None)


class LiveRiskLimitsV1(StrictValueModel):
    """Monotone hard ceilings; construction cannot relax the LIVE policy."""

    per_trade_risk_fraction: float = Field(gt=0, le=0.0025, allow_inf_nan=False, strict=True)
    aggregate_open_risk_fraction: float = Field(gt=0, le=0.01, allow_inf_nan=False, strict=True)
    max_leverage: float = Field(default=1.0, ge=1.0, le=1.0, allow_inf_nan=False, strict=True)
    per_trade_dollar_cap_usd: float = Field(
        gt=0,
        allow_inf_nan=False,
        strict=True,
        description="Maximum stop-loss risk dollars for one trade; not an order-notional limit.",
    )
    aggregate_dollar_cap_usd: float = Field(gt=0, allow_inf_nan=False, strict=True)
    daily_loss_cap_usd: float = Field(gt=0, allow_inf_nan=False, strict=True)
    max_account_equity_usd: float = Field(gt=0, allow_inf_nan=False, strict=True)
    max_position_notional_usd: float = Field(gt=0, allow_inf_nan=False, strict=True)
    min_notional_usd: float = Field(gt=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_dollar_limits(self) -> LiveRiskLimitsV1:
        if self.min_notional_usd > min(self.aggregate_dollar_cap_usd, self.max_position_notional_usd):
            raise ValueError("min_notional_usd exceeds a notional ceiling")
        return self


class VerifiedLiveAccountV1(StrictValueModel):
    """Verifier output; numeric fields are mandatory and never default to zero.

    The verifier's accounting contract is that aggregate open metrics include
    all reconciled positions and open entry orders, while reservation metrics
    include only committed risk/notional not yet represented in that snapshot.
    Daily adverse loss includes realized and current adverse PnL but excludes
    those unrepresented reservations, which the evaluator charges once.
    """

    source_id: str = Field(min_length=1, max_length=160)
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    as_of_ms: int = Field(ge=0, strict=True)
    account: AccountSnapshotV2
    aggregate_open_risk_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)
    aggregate_open_notional_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)
    reserved_open_risk_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)
    reserved_open_notional_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)
    strategy_open_notional_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)
    strategy_reserved_notional_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)
    daily_adverse_loss_usd: float = Field(ge=0, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_verified_facts(self) -> VerifiedLiveAccountV1:
        if self.source_id != self.source_id.strip():
            raise ValueError("verified source_id must be normalized")
        if self.account.trading_mode is not TradingMode.LIVE:
            raise ValueError("verified account must be LIVE")
        if self.account.evedex_profile is not EvedexProfile.PROD:
            raise ValueError("verified account must use EVEDEX PROD")
        if not self.account.reconciled or self.account.reconciliation_seq <= 0:
            raise ValueError("verified account must be reconciled with a positive sequence")
        if self.account.captured_at_ms != self.as_of_ms:
            raise ValueError("verified account as-of must equal snapshot captured_at_ms")
        if self.aggregate_open_risk_usd < self.account.total_open_risk_usd:
            raise ValueError("aggregate open risk cannot understate the account snapshot")
        snapshot_notional = sum(
            abs(position.signed_quantity) * position.mark_price for position in self.account.positions
        )
        for order in self.account.open_orders:
            if order.order_role is OrderRole.ENTRY:
                if order.price is None:
                    raise ValueError("complete LIVE open-notional evidence cannot value an entry order")
                snapshot_notional += (order.quantity - order.filled_quantity) * order.price
        if self.aggregate_open_notional_usd < snapshot_notional:
            raise ValueError("aggregate open notional cannot understate the account snapshot")
        minimum_adverse_loss = max(Decimal(0), -_d(self.account.daily_realized_pnl_usd)) + max(
            Decimal(0), -_d(self.account.unrealized_pnl_usd)
        )
        if _d(self.daily_adverse_loss_usd) < minimum_adverse_loss:
            raise ValueError("daily adverse loss cannot understate realized plus unrealized losses")
        return self


class LiveAccountSourceVerifier(Protocol):
    """Trusted registered verifier for one real account-evidence source."""

    def verify(
        self,
        raw_evidence: object,
        *,
        source_id: str,
        expected_scope: LiveRiskScopeV1,
    ) -> VerifiedLiveAccountV1:
        """Validate proof, source identity, completeness, freshness and scope."""
        ...


class VerifiedLiveInputsV1(StrictValueModel):
    """Verifier output binding exact upstream inputs; hashes alone are not proof."""

    source_id: str = Field(min_length=1, max_length=160)
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    venue_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    allocation_sha256: str | None
    system_mode: SystemMode
    observed_at_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_source_id(self) -> VerifiedLiveInputsV1:
        if self.source_id != self.source_id.strip():
            raise ValueError("verified input source_id must be normalized")
        return self


class LiveInputsSourceVerifier(Protocol):
    """Trusted registered verifier for exact upstream review/quote/macro inputs."""

    def verify(
        self,
        raw_proof: object,
        *,
        expected_scope: LiveRiskScopeV1,
        review: CandidateReviewV1,
        venue: VenueQualityV1,
        allocation: StrategicAllocation | None,
        system_mode: SystemMode,
        decided_at_ms: int,
    ) -> VerifiedLiveInputsV1:
        """Authenticate upstream receipts and return hashes for the exact inputs."""
        ...


class LiveRiskEvaluationV1(StrictValueModel):
    """Decision plus provenance facts used by an upstream LIVE producer."""

    decision: RiskTradeDecisionV1
    scope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str | None
    evidence_sha256: str | None
    account_snapshot_sha256: str | None
    account_as_of_ms: int | None
    aggregate_open_risk_usd: float | None
    aggregate_open_notional_usd: float | None
    reserved_open_risk_usd: float | None
    reserved_open_notional_usd: float | None
    strategy_open_notional_usd: float | None
    strategy_reserved_notional_usd: float | None
    daily_adverse_loss_usd: float | None
    account_equity_usd: float | None
    inputs_source_id: str | None
    inputs_evidence_sha256: str | None


class LiveRiskEvaluator:
    """Pure fail-closed LIVE evaluator; no implicit source is trusted."""

    def __init__(
        self,
        scope: LiveRiskScopeV1,
        limits: LiveRiskLimitsV1,
        *,
        account_verifiers: Mapping[str, LiveAccountSourceVerifier] | None = None,
        input_verifier: LiveInputsSourceVerifier | None = None,
    ) -> None:
        self.scope = LiveRiskScopeV1.model_validate(scope.model_dump(mode="json"))
        self.limits = LiveRiskLimitsV1.model_validate(limits.model_dump(mode="json"))
        if self.scope.risk_limits_sha256 != canonical_sha256(self.limits.model_dump(mode="json")):
            raise ValueError("LIVE scope is not bound to the supplied immutable risk limits")
        self._account_verifiers = dict(account_verifiers or {})
        self._input_verifier = input_verifier
        if any(not key or key != key.strip() for key in self._account_verifiers):
            raise ValueError("registered account source IDs must be normalized")

    def evaluate(
        self,
        raw_account_evidence: object,
        *,
        source_id: str,
        review: CandidateReviewV1,
        venue: VenueQualityV1,
        allocation: StrategicAllocation | None,
        decided_at_ms: int,
        system_mode: SystemMode = SystemMode.NORMAL,
        raw_inputs_evidence: object | None = None,
    ) -> LiveRiskEvaluationV1:
        if isinstance(decided_at_ms, bool) or not isinstance(decided_at_ms, int) or decided_at_ms < 0:
            raise ValueError("decided_at_ms must be a non-negative integer")
        review = CandidateReviewV1.model_validate_json(review.model_dump_json())
        venue = VenueQualityV1.model_validate_json(venue.model_dump_json())
        if venue.profile is not EvedexProfile.PROD:
            raise ValueError("LIVE risk requires an EVEDEX PROD venue measurement")

        intent = review.intent
        expected_symbol = self.scope.venue_symbol_for(intent.symbol)
        if expected_symbol is None:
            raise ValueError("LIVE risk symbol is outside the frozen PROD scope")
        if venue.symbol != expected_symbol:
            raise ValueError("LIVE venue symbol differs from frozen PROD scope")

        reasons: list[str] = []
        output_time_ms = max(decided_at_ms, review.reviewed_at_ms)
        verified: VerifiedLiveAccountV1 | None = None
        verified_inputs: VerifiedLiveInputsV1 | None = None
        if self._input_verifier is None:
            reasons.append("input_source_unregistered")
        elif raw_inputs_evidence is None:
            reasons.append("input_source_proof_missing")
        else:
            try:
                verified_inputs = VerifiedLiveInputsV1.model_validate(
                    self._input_verifier.verify(
                        raw_inputs_evidence,
                        expected_scope=self.scope,
                        review=review,
                        venue=venue,
                        allocation=allocation,
                        system_mode=system_mode,
                        decided_at_ms=decided_at_ms,
                    ).model_dump(mode="json")
                )
                if verified_inputs.scope_sha256 != self.scope.scope_sha256:
                    raise ValueError("verified inputs belong to another LIVE scope")
                if verified_inputs.review_sha256 != canonical_sha256(review.model_dump(mode="json")):
                    raise ValueError("verified inputs bind another candidate review")
                if verified_inputs.venue_sha256 != canonical_sha256(venue.model_dump(mode="json")):
                    raise ValueError("verified inputs bind another venue measurement")
                allocation_sha256 = (
                    canonical_sha256(allocation.model_dump(mode="json")) if allocation is not None else None
                )
                if verified_inputs.allocation_sha256 != allocation_sha256:
                    raise ValueError("verified inputs bind another strategic allocation")
                if verified_inputs.system_mode is not system_mode:
                    raise ValueError("verified inputs bind another system mode")
                age_ms = decided_at_ms - verified_inputs.observed_at_ms
                if age_ms < 0 or age_ms > _MAX_ACCOUNT_AGE_MS:
                    raise ValueError("upstream input receipt is future-dated or stale")
            except Exception:
                verified_inputs = None
                reasons.append("input_source_verification_failed")
        verifier = self._account_verifiers.get(source_id)
        if verifier is None:
            reasons.append("account_source_unregistered")
        else:
            try:
                verified = VerifiedLiveAccountV1.model_validate(
                    verifier.verify(
                        raw_account_evidence,
                        source_id=source_id,
                        expected_scope=self.scope,
                    ).model_dump(mode="json")
                )
                if verified.source_id != source_id:
                    raise ValueError("verifier returned a different source identity")
                if verified.scope_sha256 != self.scope.scope_sha256:
                    raise ValueError("verified account is bound to another LIVE scope")
                if verified.account.account_id != self.scope.account_id:
                    raise ValueError("verified account ID differs from LIVE scope")
            except Exception:
                verified = None
                reasons.append("account_source_verification_failed")

        self._admission_gates(
            reasons,
            review=review,
            venue=venue,
            allocation=allocation,
            verified=verified,
            decided_at_ms=decided_at_ms,
            system_mode=system_mode,
        )

        account = verified.account if verified is not None else None
        equity = account.equity_usd if account is not None else 0.0
        open_risk = verified.aggregate_open_risk_usd if verified is not None else 0.0
        open_notional = verified.aggregate_open_notional_usd if verified is not None else 0.0
        reserved_risk = verified.reserved_open_risk_usd if verified is not None else 0.0
        reserved_notional = verified.reserved_open_notional_usd if verified is not None else 0.0
        strategy_notional = verified.strategy_open_notional_usd if verified is not None else 0.0
        strategy_reserved_notional = verified.strategy_reserved_notional_usd if verified is not None else 0.0
        daily_adverse = verified.daily_adverse_loss_usd if verified is not None else 0.0
        worst_entry = venue.best_ask if intent.side is Side.LONG else venue.best_bid
        loss_budget = 0.0
        quantity = 0.0
        fees = slippage = worst_loss = notional = 0.0

        if verified is not None:
            allocation_error_text = allocation_error(allocation) if allocation is not None else "missing"
            if allocation is not None and allocation_error_text is None:
                daily_room = max(
                    Decimal(0),
                    _d(self.limits.daily_loss_cap_usd)
                    - _d(daily_adverse)
                    - _d(open_risk)
                    - _d(reserved_risk),
                )
                aggregate_risk_room = max(
                    Decimal(0),
                    _d(equity) * _d(self.limits.aggregate_open_risk_fraction)
                    - _d(open_risk)
                    - _d(reserved_risk),
                )
                loss_budget_decimal = min(
                    _d(equity) * _d(self.limits.per_trade_risk_fraction),
                    _d(self.limits.per_trade_dollar_cap_usd),
                    aggregate_risk_room,
                    daily_room,
                )
                loss_budget = _lower_float(loss_budget_decimal)
                if loss_budget <= 0:
                    reasons.append("risk_budget_exhausted")
                if not reasons:
                    try:
                        quantity, notional, fees, slippage, worst_loss = self._size(
                            intent=intent,
                            venue=venue,
                            allocation=allocation,
                            account=verified.account,
                            open_notional=open_notional,
                            reserved_notional=reserved_notional,
                            strategy_notional=strategy_notional,
                            strategy_reserved_notional=strategy_reserved_notional,
                            loss_budget=loss_budget,
                            worst_entry=worst_entry,
                        )
                    except (ArithmeticError, ValueError):
                        quantity = notional = fees = slippage = worst_loss = 0.0
                        reasons.append("sizing_numeric_invalid")
                    if quantity <= 0:
                        reasons.append("position_capacity_exhausted")
                    elif _d(notional) < _d(self.limits.min_notional_usd):
                        reasons.append("position_below_minimum_notional")
                if worst_loss > loss_budget:
                    reasons.append("computed_loss_exceeds_budget")
            else:
                # The allocation gate has already added a precise rejection.
                loss_budget = 0.0

        approved = not reasons
        if not approved:
            quantity = notional = fees = slippage = worst_loss = 0.0
        account_id = self.scope.account_id
        decision = RiskTradeDecisionV1(
            source="kairos-live-risk-evaluator",
            correlation_id=intent.intent_id,
            causation_id=review.message_id,
            intent=intent,
            review=review,
            venue_quality=venue,
            approved=approved,
            rejection_reasons=tuple(reasons) if not approved else (),
            decided_at_ms=output_time_ms,
            entry_policy=EntryPolicy.NEXT_BAR_MARKET,
            trading_mode=TradingMode.LIVE,
            evedex_profile=EvedexProfile.PROD,
            account_id=account_id,
            venue_symbol=venue.symbol,
            quantity=quantity,
            leverage=1.0,
            notional_usd=notional,
            loss_budget_usd=loss_budget if approved else 0.0,
            worst_case_loss_usd=worst_loss,
            worst_entry_price=worst_entry,
            estimated_fees_usd=fees,
            estimated_slippage_usd=slippage,
            exit_plan=intent.exit_plan,
        )
        return LiveRiskEvaluationV1(
            decision=decision,
            scope_sha256=self.scope.scope_sha256,
            source_id=verified.source_id if verified else source_id or None,
            evidence_sha256=verified.evidence_sha256 if verified else None,
            account_snapshot_sha256=account.snapshot_id if account else None,
            account_as_of_ms=verified.as_of_ms if verified else None,
            aggregate_open_risk_usd=verified.aggregate_open_risk_usd if verified else None,
            aggregate_open_notional_usd=verified.aggregate_open_notional_usd if verified else None,
            reserved_open_risk_usd=verified.reserved_open_risk_usd if verified else None,
            reserved_open_notional_usd=verified.reserved_open_notional_usd if verified else None,
            strategy_open_notional_usd=verified.strategy_open_notional_usd if verified else None,
            strategy_reserved_notional_usd=verified.strategy_reserved_notional_usd if verified else None,
            daily_adverse_loss_usd=verified.daily_adverse_loss_usd if verified else None,
            account_equity_usd=verified.account.equity_usd if verified else None,
            inputs_source_id=verified_inputs.source_id if verified_inputs else None,
            inputs_evidence_sha256=verified_inputs.evidence_sha256 if verified_inputs else None,
        )

    def _admission_gates(
        self,
        reasons: list[str],
        *,
        review: CandidateReviewV1,
        venue: VenueQualityV1,
        allocation: StrategicAllocation | None,
        verified: VerifiedLiveAccountV1 | None,
        decided_at_ms: int,
        system_mode: SystemMode,
    ) -> None:
        intent = review.intent
        if review.route.intent.model_dump(mode="json") != intent.model_dump(mode="json"):
            reasons.append("immutable_intent_mismatch")
        if intent.contract_version == "strategy-intent.observed-sim.v2":
            reasons.append("SIM_ONLY_INTENT")
        if (
            intent.strategy_id != self.scope.strategy_id
            or intent.strategy_revision != self.scope.strategy_revision
        ):
            reasons.append("strategy_scope_mismatch")
        if review.decision is not ReviewDecision.ALLOW:
            reasons.append(f"review_{review.decision.value.lower()}")
        if system_mode is not SystemMode.NORMAL:
            reasons.append(f"system_mode_{system_mode.value.lower()}")
        if decided_at_ms < review.reviewed_at_ms or decided_at_ms < intent.entry_eligible_ts_ms:
            reasons.append("candidate_not_yet_eligible")
        if decided_at_ms > intent.entry_expires_ts_ms:
            reasons.append("candidate_expired")

        if not venue.entry_allowed:
            reasons.append("venue_entry_blocked")
            reasons.extend(f"venue_{code}" for code in venue.reason_codes)
        age_ms = decided_at_ms - venue.observed_at_ms
        if age_ms < 0:
            reasons.append("venue_observation_from_future")
        elif age_ms > _MAX_VENUE_AGE_MS:
            reasons.append("venue_quality_stale")
        for timestamp_name, timestamp_ms in (
            ("book", venue.book_timestamp_ms),
            ("reference", venue.reference_timestamp_ms),
        ):
            source_age_ms = decided_at_ms - timestamp_ms
            if source_age_ms < 0:
                reasons.append(f"venue_{timestamp_name}_from_future")
            elif source_age_ms > _MAX_VENUE_AGE_MS:
                reasons.append(f"venue_{timestamp_name}_stale")
        if decided_at_ms > venue.expires_at_ms:
            reasons.append("venue_quality_expired")

        worst_entry = venue.best_ask if intent.side is Side.LONG else venue.best_bid
        if intent.side is Side.LONG and not (
            intent.exit_plan.stop_price < worst_entry < intent.exit_plan.target_price
        ):
            reasons.append("long_entry_exit_geometry_invalid")
        elif intent.side is Side.SHORT and not (
            intent.exit_plan.target_price < worst_entry < intent.exit_plan.stop_price
        ):
            reasons.append("short_entry_exit_geometry_invalid")

        if verified is None:
            reasons.append("account_snapshot_missing")
        else:
            account = verified.account
            if (
                account.trading_mode is not TradingMode.LIVE
                or account.evedex_profile is not EvedexProfile.PROD
            ):
                reasons.append("account_environment_mismatch")
            if account.account_id != self.scope.account_id:
                reasons.append("account_id_mismatch")
            if account.equity_usd > self.limits.max_account_equity_usd:
                reasons.append("account_equity_above_release_ceiling")
            age_ms = decided_at_ms - verified.as_of_ms
            if age_ms < 0:
                reasons.append("account_snapshot_from_future")
            elif age_ms > _MAX_ACCOUNT_AGE_MS:
                reasons.append("account_snapshot_stale")
            if any(position.venue_symbol == venue.symbol for position in account.positions):
                reasons.append("symbol_has_active_position")
            if any(order.venue_symbol == venue.symbol for order in account.open_orders):
                reasons.append("symbol_has_open_order")
            strategy_snapshot_notional = sum(
                abs(position.signed_quantity) * position.mark_price
                for position in account.positions
                if position.strategy_id == self.scope.strategy_id
            )
            strategy_snapshot_notional += sum(
                (order.quantity - order.filled_quantity) * order.price
                for order in account.open_orders
                if order.order_role is OrderRole.ENTRY
                and order.strategy_id == self.scope.strategy_id
                and order.price is not None
            )
            if verified.strategy_open_notional_usd < strategy_snapshot_notional:
                reasons.append("strategy_open_notional_understated")
            if verified.aggregate_open_risk_usd + verified.reserved_open_risk_usd >= (
                account.equity_usd * self.limits.aggregate_open_risk_fraction
            ):
                reasons.append("aggregate_open_risk_limit_exhausted")
            if (
                verified.daily_adverse_loss_usd
                + verified.aggregate_open_risk_usd
                + verified.reserved_open_risk_usd
                >= self.limits.daily_loss_cap_usd
            ):
                reasons.append("daily_loss_limit_exhausted")

        if allocation is None:
            reasons.append("strategic_allocation_missing")
        else:
            invalid = allocation_error(allocation)
            if invalid is not None:
                reasons.append("strategic_allocation_invalid")
            produced_at = allocation.produced_at
            if produced_at.utcoffset() is None:
                reasons.append("strategic_allocation_naive_timestamp")
            else:
                produced_at_ms = int(produced_at.astimezone(UTC).timestamp() * 1_000)
                age_ms = decided_at_ms - produced_at_ms
                if age_ms < 0:
                    reasons.append("strategic_allocation_from_future")
                elif age_ms > _MAX_ALLOCATION_AGE_MS:
                    reasons.append("strategic_allocation_stale")
            if allocation.strategy_weights.get(self.scope.strategy_id, 0.0) <= 0:
                reasons.append("strategy_has_no_macro_allocation")
            if allocation.regime is MarketRegime.CHOP:
                reasons.append("macro_regime_chop")
            elif allocation.regime is MarketRegime.BEAR and intent.side is Side.LONG:
                reasons.append("macro_regime_forbids_long")
            elif allocation.regime is MarketRegime.BULL and intent.side is Side.SHORT:
                reasons.append("macro_regime_forbids_short")
            if allocation.max_gross_leverage < 1.0:
                reasons.append("macro_leverage_below_one")

    def _size(
        self,
        *,
        intent: StrategyIntentV1,
        venue: VenueQualityV1,
        allocation: StrategicAllocation,
        account: AccountSnapshotV2,
        open_notional: float,
        reserved_notional: float,
        strategy_notional: float,
        strategy_reserved_notional: float,
        loss_budget: float,
        worst_entry: float,
    ) -> tuple[float, float, float, float, float]:
        slippage_bps = venue.buy_slippage_bps if intent.side is Side.LONG else venue.sell_slippage_bps
        entry = _d(worst_entry)
        stop = _d(intent.exit_plan.stop_price)
        mid = _d(venue.venue_mid_price)
        fee_rate = _d(venue.taker_fee_bps) / Decimal(10_000)
        slippage_rate = _d(slippage_bps) / Decimal(10_000)
        loss_per_unit = abs(entry - stop) + (entry + stop) * fee_rate + mid * slippage_rate
        loss_budget_decimal = _d(loss_budget)
        if loss_per_unit <= 0 or loss_budget_decimal <= 0:
            return 0.0, 0.0, 0.0, 0.0, 0.0

        equity = _d(account.equity_usd)
        deployable_equity = equity * (Decimal(1) - _d(allocation.stable_reserve_pct))
        allocation_gross_cap = deployable_equity * min(Decimal(1), _d(allocation.max_gross_leverage))
        remaining_gross = max(Decimal(0), allocation_gross_cap - _d(open_notional) - _d(reserved_notional))
        remaining_strategy = max(
            Decimal(0),
            equity * _d(allocation.strategy_weights[intent.strategy_id])
            - _d(strategy_notional)
            - _d(strategy_reserved_notional),
        )
        available_balance = max(Decimal(0), _d(account.available_balance_usd) - _d(reserved_notional))
        notional_cap = min(
            _d(self.limits.max_position_notional_usd),
            max(
                Decimal(0),
                _d(self.limits.aggregate_dollar_cap_usd) - _d(open_notional) - _d(reserved_notional),
            ),
            _d(venue.assessed_notional_usd),
            _d(venue.depth_usd),
            available_balance,
            remaining_gross,
            remaining_strategy,
        )
        unrounded = min(loss_budget_decimal / loss_per_unit, notional_cap / entry)
        quantity_decimal = (unrounded / _QUANTITY_QUANTUM).to_integral_value(rounding=ROUND_DOWN)
        quantity_decimal *= _QUANTITY_QUANTUM

        def economics(quantity_value: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
            notional_value = quantity_value * entry
            fees_value = quantity_value * (entry + stop) * fee_rate
            slippage_value = quantity_value * mid * slippage_rate
            loss_value = quantity_value * abs(entry - stop) + fees_value + slippage_value
            return notional_value, fees_value, slippage_value, loss_value

        for correction in range(3):
            if quantity_decimal <= 0:
                return 0.0, 0.0, 0.0, 0.0, 0.0
            notional_decimal, fees_decimal, slippage_decimal, loss_decimal = economics(quantity_decimal)
            quantity = _lower_float(quantity_decimal)
            displayed_stop_loss = _d(quantity) * abs(entry - stop)
            notional = _upper_float(max(notional_decimal, _d(quantity) * entry))
            fees = _upper_float(fees_decimal)
            slippage = _upper_float(slippage_decimal)
            worst_loss = _upper_float(max(loss_decimal, displayed_stop_loss + _d(fees) + _d(slippage)))
            violates = (
                loss_decimal > loss_budget_decimal
                or _d(worst_loss) > loss_budget_decimal
                or notional_decimal > notional_cap
                or _d(notional) > notional_cap
                or _d(quantity) * entry > notional_cap
            )
            if not violates:
                return quantity, notional, fees, slippage, worst_loss
            if correction == 2:
                break
            quantity_decimal -= _QUANTITY_QUANTUM
        return 0.0, 0.0, 0.0, 0.0, 0.0


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def _lower_float(value: Decimal) -> float:
    """Convert a non-negative cap downward so its decimal spelling cannot inflate it."""
    converted = float(value)
    if not math.isfinite(converted):
        raise OverflowError("quantity is outside finite float range")
    if _d(converted) > value:
        converted = math.nextafter(converted, -math.inf)
    return max(0.0, converted)


def _upper_float(value: Decimal) -> float:
    """Convert positive costs/notionals upward for conservative downstream checks."""
    converted = float(value)
    if not math.isfinite(converted):
        raise OverflowError("risk economics are outside finite float range")
    if _d(converted) < value:
        converted = math.nextafter(converted, math.inf)
    return converted
