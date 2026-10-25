"""Pure, opt-in capital-budget proposals from already verified LIVE facts.

This module does not verify raw account evidence, create or update a LIVE scope,
register a verifier, issue an arm, or authorize execution. The existing LIVE
evaluator and its verifier registration remain the authority boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal

from kairos_core.contracts import canonical_sha256

from .live import (
    LiveRiskLimitsV1,
    LiveRiskScopeV1,
    VerifiedLiveAccountV1,
    _lower_float,
    _upper_float,
)

_MAX_ACCOUNT_AGE_MS = 5_000
_USD_QUANTUM = Decimal("0.01")
_PER_TRADE_CEILING = Decimal("0.0025")
_AGGREGATE_CEILING = Decimal("0.01")
_DAILY_CEILING = Decimal("0.01")


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a finite numeric value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be a finite numeric value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite numeric value") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'non-negative'}")
    return result


def _usd_floor(value: Decimal) -> Decimal:
    return value.quantize(_USD_QUANTUM, rounding=ROUND_DOWN)


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True, slots=True)
class CapitalBudgetPolicyV1:
    """Explicit risk fractions; this policy can only produce a proposal."""

    per_trade_risk_fraction: Decimal
    aggregate_open_risk_fraction: Decimal
    daily_loss_fraction: Decimal

    def __post_init__(self) -> None:
        values = (
            ("per_trade_risk_fraction", self.per_trade_risk_fraction, _PER_TRADE_CEILING),
            ("aggregate_open_risk_fraction", self.aggregate_open_risk_fraction, _AGGREGATE_CEILING),
            ("daily_loss_fraction", self.daily_loss_fraction, _DAILY_CEILING),
        )
        for name, value, ceiling in values:
            parsed = _decimal(value, name, positive=True)
            if parsed > ceiling:
                raise ValueError(f"{name} exceeds its hard policy ceiling")
            object.__setattr__(self, name, parsed)

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(
            {
                "version": "kairos.capital-budget-policy.v1",
                "per_trade_risk_fraction": _canonical_decimal(self.per_trade_risk_fraction),
                "aggregate_open_risk_fraction": _canonical_decimal(self.aggregate_open_risk_fraction),
                "daily_loss_fraction": _canonical_decimal(self.daily_loss_fraction),
                "max_leverage": "1",
                "currency_quantum_usd": "0.01",
            }
        )


@dataclass(frozen=True, slots=True)
class VenueMinimumNotionalV1:
    """Caller-supplied actual venue metadata; this record is not authenticated here."""

    venue_symbol: str
    source_id: str
    observed_at_ms: int
    evidence_sha256: str
    min_notional_usd: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.venue_symbol, str) or not self.venue_symbol.strip():
            raise ValueError("venue metadata requires an exact venue symbol")
        if self.venue_symbol != self.venue_symbol.strip().upper():
            raise ValueError("venue metadata symbol must be normalized uppercase")
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("venue metadata requires an identified source")
        if self.source_id != self.source_id.strip():
            raise ValueError("venue metadata source must be normalized")
        if type(self.observed_at_ms) is not int or self.observed_at_ms < 0:
            raise ValueError("venue metadata requires its original non-negative observation time")
        if (
            not isinstance(self.evidence_sha256, str)
            or len(self.evidence_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.evidence_sha256)
        ):
            raise ValueError("venue metadata requires its original SHA-256 evidence digest")
        object.__setattr__(
            self,
            "min_notional_usd",
            _decimal(self.min_notional_usd, "venue min_notional_usd", positive=True),
        )


@dataclass(frozen=True, slots=True)
class CapitalBudgetProposalV1:
    """Auditable NON-AUTHORITY proposal, tied to original evidence and scope."""

    status: Literal["NON_AUTHORITY_PROPOSAL"]
    authority: Literal[False]
    account_id: str
    source_id: str
    account_evidence_sha256: str
    scope_sha256: str
    current_risk_limits_sha256: str
    proposed_risk_limits_sha256: str
    policy_sha256: str
    baseline_sha256: str
    account_as_of_ms: int
    valid_until_ms: int
    observed_equity_usd: Decimal
    initial_budget_ceiling_usd: Decimal
    capital_basis_usd: Decimal
    proposed_max_total_notional_usd: Decimal
    venue_min_notional_usd: Decimal
    proposed_limits: LiveRiskLimitsV1
    warnings: tuple[str, ...]
    requires_new_scope_and_fresh_reverification: Literal[True]


def propose_capital_budget(
    *,
    verified_account: VerifiedLiveAccountV1,
    expected_scope: LiveRiskScopeV1,
    current_limits: LiveRiskLimitsV1,
    policy: CapitalBudgetPolicyV1,
    venue_minimums: tuple[VenueMinimumNotionalV1, ...],
    now_ms: int,
) -> CapitalBudgetProposalV1:
    """Calculate a conservative proposal from already verified, fresh facts.

    ``current_limits.max_account_equity_usd`` is treated as the recorded
    starting-capital ceiling. Deposits or gains cannot raise it implicitly.
    Venue metadata must exactly cover every scoped PROD symbol and be fresh;
    this function checks its shape and digest, but does not authenticate it.
    ``aggregate_dollar_cap_usd`` is the evaluator's portfolio-wide open-notional
    ceiling; ``aggregate_open_risk_fraction`` independently remains the stop-risk
    ceiling. No authority is created, and changed limits require a new scope and
    account reverification.
    """
    if type(verified_account) is not VerifiedLiveAccountV1:
        raise TypeError("an exact already-verified LIVE account record is required")
    if type(expected_scope) is not LiveRiskScopeV1:
        raise TypeError("an exact expected LIVE scope is required")
    if type(current_limits) is not LiveRiskLimitsV1:
        raise TypeError("current scoped LIVE risk limits are required")
    if type(policy) is not CapitalBudgetPolicyV1:
        raise TypeError("an explicit capital-budget policy is required")
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be an explicit non-negative integer")

    # Frozen Pydantic models can still be bypassed with model_copy(update=...).
    # Validate their serialized values again before using any of them.
    try:
        raw_equity = _decimal(verified_account.account.equity_usd, "verified account equity", positive=True)
        verified_account = VerifiedLiveAccountV1.model_validate(verified_account.model_dump(mode="json"))
        expected_scope = LiveRiskScopeV1.model_validate(expected_scope.model_dump(mode="json"))
        current_limits = LiveRiskLimitsV1.model_validate(current_limits.model_dump(mode="json"))
        policy = CapitalBudgetPolicyV1(
            per_trade_risk_fraction=policy.per_trade_risk_fraction,
            aggregate_open_risk_fraction=policy.aggregate_open_risk_fraction,
            daily_loss_fraction=policy.daily_loss_fraction,
        )
        if not isinstance(venue_minimums, tuple) or not venue_minimums:
            raise ValueError("actual venue minimum-notional metadata is required")
        venue_minimums = tuple(
            VenueMinimumNotionalV1(
                venue_symbol=item.venue_symbol,
                source_id=item.source_id,
                observed_at_ms=item.observed_at_ms,
                evidence_sha256=item.evidence_sha256,
                min_notional_usd=item.min_notional_usd,
            )
            if type(item) is VenueMinimumNotionalV1
            else item
            for item in venue_minimums
        )
    except Exception:
        raise ValueError("supplied capital-proposal inputs failed independent revalidation") from None

    current_limits_sha256 = canonical_sha256(current_limits.model_dump(mode="json"))
    if expected_scope.risk_limits_sha256 != current_limits_sha256:
        raise ValueError("expected scope does not bind the supplied current risk limits")
    if verified_account.scope_sha256 != expected_scope.scope_sha256:
        raise ValueError("verified account evidence is not bound to the exact expected scope")
    account = verified_account.account
    if account.account_id != expected_scope.account_id:
        raise ValueError("verified account identity differs from the expected scope")
    if not account.reconciled or account.reconciliation_seq <= 0:
        raise ValueError("verified account must have a completed reconciliation")
    if account.trading_mode.value != "LIVE" or account.evedex_profile.value != "PROD":
        raise ValueError("capital proposal requires verified LIVE/PROD account evidence")
    if verified_account.as_of_ms != account.captured_at_ms:
        raise ValueError("account evidence time differs from its original snapshot time")
    if verified_account.as_of_ms > now_ms or now_ms - verified_account.as_of_ms > _MAX_ACCOUNT_AGE_MS:
        raise ValueError("verified account evidence is future-dated or stale")

    if not isinstance(venue_minimums, tuple) or not venue_minimums:
        raise ValueError("actual venue minimum-notional metadata is required")
    if any(type(item) is not VenueMinimumNotionalV1 for item in venue_minimums):
        raise TypeError("exact venue minimum-notional metadata records are required")
    by_symbol = {item.venue_symbol: item for item in venue_minimums}
    scoped_symbols = {venue for _signal, venue in expected_scope.approved_prod_symbols}
    if len(by_symbol) != len(venue_minimums) or set(by_symbol) != scoped_symbols:
        raise ValueError("venue metadata must exactly cover the approved PROD symbol scope")
    for item in venue_minimums:
        if item.observed_at_ms > now_ms or now_ms - item.observed_at_ms > _MAX_ACCOUNT_AGE_MS:
            raise ValueError("venue minimum-notional metadata is future-dated or stale")

    equity = raw_equity
    baseline_ceiling = _decimal(
        current_limits.max_account_equity_usd, "recorded initial budget ceiling", positive=True
    )
    capital_basis = min(equity, baseline_ceiling)
    venue_minimum = max(item.min_notional_usd for item in venue_minimums)
    proposal_minimum = max(
        venue_minimum,
        _decimal(current_limits.min_notional_usd, "current min_notional_usd", positive=True),
    )

    if policy.per_trade_risk_fraction > Decimal(str(current_limits.per_trade_risk_fraction)):
        raise ValueError("proposed per-trade fraction cannot exceed the current scoped fraction")
    if policy.aggregate_open_risk_fraction > Decimal(str(current_limits.aggregate_open_risk_fraction)):
        raise ValueError("proposed aggregate fraction cannot exceed the current scoped fraction")

    per_trade_cap = _usd_floor(
        min(
            _decimal(current_limits.per_trade_dollar_cap_usd, "current per-trade cap", positive=True),
            capital_basis * policy.per_trade_risk_fraction,
        )
    )
    daily_cap = _usd_floor(
        min(
            _decimal(current_limits.daily_loss_cap_usd, "current daily cap", positive=True),
            capital_basis * policy.daily_loss_fraction,
        )
    )
    aggregate_notional_cap = _usd_floor(
        min(
            capital_basis,
            _decimal(
                current_limits.aggregate_dollar_cap_usd,
                "current aggregate notional cap",
                positive=True,
            ),
        )
    )
    total_notional_cap = _usd_floor(
        min(
            aggregate_notional_cap,
            _decimal(current_limits.max_position_notional_usd, "current position cap", positive=True),
        )
    )
    if min(per_trade_cap, daily_cap, total_notional_cap) <= 0:
        raise ValueError("verified capital is too small for positive cent-quantized limits")
    if proposal_minimum > total_notional_cap:
        raise ValueError("verified capital is below the supplied actual venue minimum notional")

    proposed_limits = LiveRiskLimitsV1(
        per_trade_risk_fraction=_lower_float(policy.per_trade_risk_fraction),
        aggregate_open_risk_fraction=_lower_float(policy.aggregate_open_risk_fraction),
        max_leverage=1.0,
        per_trade_dollar_cap_usd=_lower_float(per_trade_cap),
        aggregate_dollar_cap_usd=_lower_float(aggregate_notional_cap),
        daily_loss_cap_usd=_lower_float(daily_cap),
        max_account_equity_usd=_lower_float(baseline_ceiling),
        max_position_notional_usd=_lower_float(total_notional_cap),
        min_notional_usd=_upper_float(proposal_minimum),
    )
    policy_sha256 = policy.policy_sha256
    baseline_sha256 = canonical_sha256(
        {
            "version": "kairos.capital-budget-baseline.v1",
            "account_id": expected_scope.account_id,
            "scope_sha256": expected_scope.scope_sha256,
            "recorded_start_ceiling_usd": _canonical_decimal(baseline_ceiling),
            "record_source": "current_scoped_max_account_equity_usd",
        }
    )
    venue_metadata_sha256 = canonical_sha256(
        {
            "items": [
                {
                    "venue_symbol": item.venue_symbol,
                    "source_id": item.source_id,
                    "observed_at_ms": item.observed_at_ms,
                    "evidence_sha256": item.evidence_sha256,
                    "min_notional_usd": _canonical_decimal(item.min_notional_usd),
                }
                for item in sorted(venue_minimums, key=lambda value: value.venue_symbol)
            ]
        }
    )
    # Bind venue floors into the policy digest used by this exact proposal.
    combined_policy_sha256 = canonical_sha256(
        {"budget_policy_sha256": policy_sha256, "venue_metadata_sha256": venue_metadata_sha256}
    )
    warnings = ("ACCOUNT_EQUITY_ABOVE_RECORDED_START_CEILING",) if equity > baseline_ceiling else ()
    return CapitalBudgetProposalV1(
        status="NON_AUTHORITY_PROPOSAL",
        authority=False,
        account_id=expected_scope.account_id,
        source_id=verified_account.source_id,
        account_evidence_sha256=verified_account.evidence_sha256,
        scope_sha256=expected_scope.scope_sha256,
        current_risk_limits_sha256=current_limits_sha256,
        proposed_risk_limits_sha256=canonical_sha256(proposed_limits.model_dump(mode="json")),
        policy_sha256=combined_policy_sha256,
        baseline_sha256=baseline_sha256,
        account_as_of_ms=verified_account.as_of_ms,
        valid_until_ms=verified_account.as_of_ms + _MAX_ACCOUNT_AGE_MS,
        observed_equity_usd=equity,
        initial_budget_ceiling_usd=baseline_ceiling,
        capital_basis_usd=capital_basis,
        proposed_max_total_notional_usd=aggregate_notional_cap,
        venue_min_notional_usd=venue_minimum,
        proposed_limits=proposed_limits,
        warnings=warnings,
        requires_new_scope_and_fresh_reverification=True,
    )


__all__ = [
    "CapitalBudgetPolicyV1",
    "VenueMinimumNotionalV1",
    "CapitalBudgetProposalV1",
    "propose_capital_budget",
]
