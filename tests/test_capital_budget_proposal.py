"""Pure capital-budget proposals are non-authority and use synthetic facts only."""

from __future__ import annotations

from decimal import Decimal

import pytest
from kairos_core.contracts import AccountSnapshotV2, canonical_sha256
from kairos_core.enums import EvedexProfile, TradingMode

from kairos_risk.capital_budget_proposal import (
    CapitalBudgetPolicyV1,
    VenueMinimumNotionalV1,
    propose_capital_budget,
)
from kairos_risk.live import LiveRiskLimitsV1, LiveRiskScopeV1, VerifiedLiveAccountV1

NOW_MS = 1_800_000_000_000
ACCOUNT_ID = "synthetic-test-account"


def _limits(**updates: object) -> LiveRiskLimitsV1:
    values: dict[str, object] = {
        "per_trade_risk_fraction": 0.0025,
        "aggregate_open_risk_fraction": 0.01,
        "max_leverage": 1.0,
        "per_trade_dollar_cap_usd": 500.0,
        "aggregate_dollar_cap_usd": 3_000.0,
        "daily_loss_cap_usd": 1_000.0,
        "max_account_equity_usd": 10_000.0,
        "max_position_notional_usd": 2_000.0,
        "min_notional_usd": 5.0,
    }
    values.update(updates)
    return LiveRiskLimitsV1(**values)


def _scope(limits: LiveRiskLimitsV1 | None = None) -> LiveRiskScopeV1:
    limits = limits or _limits()
    return LiveRiskScopeV1(
        context_sha256="a" * 64,
        source_set_sha256="b" * 64,
        policy_sha256="c" * 64,
        risk_limits_sha256=canonical_sha256(limits.model_dump(mode="json")),
        account_id=ACCOUNT_ID,
        strategy_id="synthetic-strategy",
        strategy_revision="fixture-1",
        approved_prod_symbols=(("BTCUSDT", "BTCUSD"),),
    )


def _verified(
    scope: LiveRiskScopeV1,
    *,
    equity: float = 8_000.0,
    as_of_ms: int = NOW_MS - 1_000,
    reconciled: bool = True,
) -> VerifiedLiveAccountV1:
    account = AccountSnapshotV2(
        source="synthetic-account-fixture",
        trading_mode=TradingMode.LIVE,
        evedex_profile=EvedexProfile.PROD,
        account_id=ACCOUNT_ID,
        equity_usd=equity,
        available_balance_usd=equity,
        margin_used_usd=0.0,
        durable_day_start_equity_usd=max(equity, 1.0),
        durable_peak_equity_usd=max(equity, 1.0),
        captured_at_ms=as_of_ms,
        reconciliation_seq=1,
        reconciled=reconciled,
        reconciliation_detail="synthetic offline fixture only",
    )
    return VerifiedLiveAccountV1(
        source_id="synthetic-verifier-fixture",
        scope_sha256=scope.scope_sha256,
        evidence_sha256="d" * 64,
        as_of_ms=as_of_ms,
        account=account,
        aggregate_open_risk_usd=0.0,
        aggregate_open_notional_usd=0.0,
        reserved_open_risk_usd=0.0,
        reserved_open_notional_usd=0.0,
        strategy_open_notional_usd=0.0,
        strategy_reserved_notional_usd=0.0,
        daily_adverse_loss_usd=0.0,
    )


def _policy() -> CapitalBudgetPolicyV1:
    return CapitalBudgetPolicyV1(
        per_trade_risk_fraction=Decimal("0.0025"),
        aggregate_open_risk_fraction=Decimal("0.01"),
        daily_loss_fraction=Decimal("0.01"),
    )


def _venue_minimum(*, min_notional: str = "5", observed_at_ms: int = NOW_MS - 500):
    return VenueMinimumNotionalV1(
        venue_symbol="BTCUSD",
        source_id="synthetic-venue-metadata-fixture",
        observed_at_ms=observed_at_ms,
        evidence_sha256="e" * 64,
        min_notional_usd=Decimal(min_notional),
    )


def _propose(*, equity: float = 8_000.0, as_of_ms: int = NOW_MS - 1_000, minimum=None):
    limits = _limits()
    scope = _scope(limits)
    return propose_capital_budget(
        verified_account=_verified(scope, equity=equity, as_of_ms=as_of_ms),
        expected_scope=scope,
        current_limits=limits,
        policy=_policy(),
        venue_minimums=(minimum or _venue_minimum(),),
        now_ms=NOW_MS,
    )


def test_smaller_verified_balance_shrinks_all_equity_derived_caps() -> None:
    larger = _propose(equity=4_000.0)
    smaller = _propose(equity=2_000.0)

    assert smaller.capital_basis_usd == Decimal("2000.0")
    assert smaller.proposed_limits.per_trade_dollar_cap_usd < larger.proposed_limits.per_trade_dollar_cap_usd
    assert smaller.proposed_limits.daily_loss_cap_usd < larger.proposed_limits.daily_loss_cap_usd
    assert smaller.proposed_limits.aggregate_dollar_cap_usd < larger.proposed_limits.aggregate_dollar_cap_usd
    assert smaller.proposed_max_total_notional_usd < larger.proposed_max_total_notional_usd
    assert larger.proposed_max_total_notional_usd == Decimal("3000.00")
    assert larger.proposed_limits.max_position_notional_usd == 2_000.0
    assert smaller.proposed_max_total_notional_usd <= Decimal("2000.0")


def test_higher_balance_never_grows_past_the_scoped_start_ceiling() -> None:
    at_ceiling = _propose(equity=10_000.0)
    above_ceiling = _propose(equity=12_000.0)

    assert above_ceiling.capital_basis_usd == at_ceiling.capital_basis_usd == Decimal("10000.0")
    assert (
        above_ceiling.proposed_limits.per_trade_dollar_cap_usd
        == at_ceiling.proposed_limits.per_trade_dollar_cap_usd
    )
    assert (
        above_ceiling.proposed_limits.aggregate_dollar_cap_usd
        == at_ceiling.proposed_limits.aggregate_dollar_cap_usd
    )
    assert above_ceiling.proposed_limits.daily_loss_cap_usd == at_ceiling.proposed_limits.daily_loss_cap_usd
    assert above_ceiling.proposed_max_total_notional_usd == at_ceiling.proposed_max_total_notional_usd
    assert "ACCOUNT_EQUITY_ABOVE_RECORDED_START_CEILING" in above_ceiling.warnings
    assert above_ceiling.proposed_max_total_notional_usd <= Decimal("10000.0")


def test_tiny_balance_below_actual_venue_minimum_is_refused() -> None:
    with pytest.raises(ValueError, match="below the supplied actual venue minimum"):
        _propose(equity=4.0, minimum=_venue_minimum(min_notional="5"))


def test_fifty_dollar_balance_keeps_total_notional_cap_above_actual_minimum() -> None:
    proposal = _propose(equity=50.0, minimum=_venue_minimum(min_notional="5"))

    assert proposal.proposed_limits.aggregate_dollar_cap_usd == 50.0
    assert proposal.proposed_limits.max_position_notional_usd == 50.0
    assert proposal.proposed_max_total_notional_usd == Decimal("50.00")
    assert proposal.venue_min_notional_usd == Decimal("5")


@pytest.mark.parametrize(
    ("as_of_ms", "now_ms", "message"),
    [
        (NOW_MS - 5_001, NOW_MS, "stale"),
        (NOW_MS + 1, NOW_MS, "future-dated"),
    ],
)
def test_stale_or_future_account_evidence_is_refused(as_of_ms: int, now_ms: int, message: str) -> None:
    limits = _limits()
    scope = _scope(limits)
    with pytest.raises(ValueError, match=message):
        propose_capital_budget(
            verified_account=_verified(scope, as_of_ms=as_of_ms),
            expected_scope=scope,
            current_limits=limits,
            policy=_policy(),
            venue_minimums=(_venue_minimum(),),
            now_ms=now_ms,
        )


def test_unreconciled_account_and_stale_venue_metadata_are_refused() -> None:
    limits = _limits()
    scope = _scope(limits)
    with pytest.raises(ValueError, match="reconciled"):
        propose_capital_budget(
            verified_account=_verified(scope, reconciled=False),
            expected_scope=scope,
            current_limits=limits,
            policy=_policy(),
            venue_minimums=(_venue_minimum(),),
            now_ms=NOW_MS,
        )

    with pytest.raises(ValueError, match="venue minimum-notional metadata is future-dated or stale"):
        _propose(minimum=_venue_minimum(observed_at_ms=NOW_MS - 5_001))


def test_proposal_is_explicitly_non_authority_and_requires_new_scope() -> None:
    proposal = _propose()

    assert proposal.status == "NON_AUTHORITY_PROPOSAL"
    assert proposal.authority is False
    assert proposal.requires_new_scope_and_fresh_reverification is True
    assert proposal.account_evidence_sha256 == "d" * 64
    assert proposal.account_as_of_ms == NOW_MS - 1_000
    assert proposal.valid_until_ms == NOW_MS + 4_000
    assert proposal.proposed_limits.max_leverage == 1.0
    assert proposal.proposed_max_total_notional_usd <= proposal.observed_equity_usd
    assert proposal.policy_sha256 != "0" * 64


def test_policy_cannot_exceed_existing_fraction_ceilings_or_scope() -> None:
    with pytest.raises(ValueError, match="hard policy ceiling"):
        CapitalBudgetPolicyV1(
            per_trade_risk_fraction=Decimal("0.0026"),
            aggregate_open_risk_fraction=Decimal("0.01"),
            daily_loss_fraction=Decimal("0.01"),
        )

    limits = _limits()
    scope = _scope(limits)
    wrong_scope = _scope(_limits(per_trade_risk_fraction=0.002))
    with pytest.raises(ValueError, match="exact expected scope"):
        propose_capital_budget(
            verified_account=_verified(wrong_scope),
            expected_scope=scope,
            current_limits=limits,
            policy=_policy(),
            venue_minimums=(_venue_minimum(),),
            now_ms=NOW_MS,
        )


@pytest.mark.parametrize("equity", [True, 0.0, float("nan")])
def test_pydantic_model_copy_equity_tampering_is_revalidated(equity: object) -> None:
    limits = _limits()
    scope = _scope(limits)
    verified = _verified(scope).model_copy(
        update={"account": _verified(scope).account.model_copy(update={"equity_usd": equity})}
    )
    with pytest.raises(ValueError, match="revalidation"):
        propose_capital_budget(
            verified_account=verified,
            expected_scope=scope,
            current_limits=limits,
            policy=_policy(),
            venue_minimums=(_venue_minimum(),),
            now_ms=NOW_MS,
        )


def test_all_supplied_pydantic_models_are_revalidated_after_model_copy() -> None:
    limits = _limits()
    scope = _scope(limits)
    verified = _verified(scope)
    with pytest.raises(ValueError, match="revalidation"):
        propose_capital_budget(
            verified_account=verified,
            expected_scope=scope.model_copy(update={"account_id": ""}),
            current_limits=limits,
            policy=_policy(),
            venue_minimums=(_venue_minimum(),),
            now_ms=NOW_MS,
        )
    with pytest.raises(ValueError, match="revalidation"):
        propose_capital_budget(
            verified_account=verified,
            expected_scope=scope,
            current_limits=limits.model_copy(update={"min_notional_usd": 0.0}),
            policy=_policy(),
            venue_minimums=(_venue_minimum(),),
            now_ms=NOW_MS,
        )
    with pytest.raises(ValueError, match="revalidation"):
        propose_capital_budget(
            verified_account=verified.model_copy(update={"daily_adverse_loss_usd": float("nan")}),
            expected_scope=scope,
            current_limits=limits,
            policy=_policy(),
            venue_minimums=(_venue_minimum(),),
            now_ms=NOW_MS,
        )
