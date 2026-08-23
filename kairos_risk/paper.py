"""Pure, fail-closed risk policy for the Strategy Parity -> EVEDEX DEV route.

This module has no bus, clock, LLM or exchange dependency. Callers supply the
decision timestamp and complete immutable inputs, making replay byte-stable.
The legacy ``TacticalCommand -> ValidatedOrder`` path remains in ``pipeline``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC

from kairos_core.contracts import (
    AccountSnapshotV2,
    CandidateReviewV1,
    PositionSnapshotV2,
    RiskTradeDecisionV1,
    StrategicAllocation,
    VenueQualityV1,
)
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

from .config import PAPER_CANARY_STRATEGY_ID, PAPER_DEV_SYMBOL_MAP, RiskSettings
from .strategy import allocation_error


@dataclass(frozen=True, slots=True)
class PaperReservations:
    """Risk already approved but not yet represented by reconciliation."""

    symbols: frozenset[str] = frozenset()
    open_risk_usd: float = 0.0
    notional_usd: float = 0.0
    strategy_notional_usd: tuple[tuple[str, float], ...] = ()
    active_canary_ideas: int = 0

    def __post_init__(self) -> None:
        if any(symbol not in PAPER_DEV_SYMBOL_MAP.values() for symbol in self.symbols):
            raise ValueError("reserved PAPER symbols must belong to the fixed DEV universe")
        if not math.isfinite(self.open_risk_usd) or self.open_risk_usd < 0:
            raise ValueError("reserved open risk must be finite and non-negative")
        if not math.isfinite(self.notional_usd) or self.notional_usd < 0:
            raise ValueError("reserved notional must be finite and non-negative")
        if (
            isinstance(self.active_canary_ideas, bool)
            or not isinstance(self.active_canary_ideas, int)
            or self.active_canary_ideas < 0
        ):
            raise ValueError("reserved active canary ideas must be a non-negative integer")
        names = [name for name, _value in self.strategy_notional_usd]
        if len(names) != len(set(names)):
            raise ValueError("reserved strategy notional names must be unique")
        if any(not name or name != name.strip() for name in names):
            raise ValueError("reserved strategy names must be normalized")
        if any(not math.isfinite(value) or value < 0 for _name, value in self.strategy_notional_usd):
            raise ValueError("reserved strategy notional must be finite and non-negative")
        if sum(value for _name, value in self.strategy_notional_usd) > self.notional_usd + 1e-6:
            raise ValueError("reserved strategy notional cannot exceed total reserved notional")
        object.__setattr__(self, "strategy_notional_usd", tuple(sorted(self.strategy_notional_usd)))

    def notional_for_strategy(self, strategy_id: str) -> float:
        return next(
            (value for name, value in self.strategy_notional_usd if name == strategy_id),
            0.0,
        )


class PaperRiskPipeline:
    """Build a strict PAPER decision from immutable reviewed inputs."""

    def __init__(self, settings: RiskSettings | None = None) -> None:
        self.settings = settings or RiskSettings()

    def evaluate(
        self,
        review: CandidateReviewV1,
        venue: VenueQualityV1,
        *,
        account: AccountSnapshotV2 | None,
        allocation: StrategicAllocation | None,
        decided_at_ms: int,
        system_mode: SystemMode = SystemMode.NORMAL,
        reservations: PaperReservations | None = None,
    ) -> RiskTradeDecisionV1:
        """Return an approved or explicit rejected ``RiskTradeDecisionV1``.

        ``decided_at_ms`` is supplied by the runtime boundary. A replay using
        the same causal inputs and timestamp produces identical bytes and IDs.
        """

        if isinstance(decided_at_ms, bool) or not isinstance(decided_at_ms, int) or decided_at_ms < 0:
            raise ValueError("decided_at_ms must be a non-negative integer")
        if venue.profile is not EvedexProfile.DEV:
            # The core contract intentionally cannot embed DEMO/PROD venue data
            # in even a rejected PAPER decision. Reject it at the ingestion edge.
            raise ValueError("PAPER risk accepts only EVEDEX DEV venue measurements")

        reservations = reservations or PaperReservations()
        intent = review.intent
        output_time_ms = max(decided_at_ms, review.reviewed_at_ms)
        expected_symbol = PAPER_DEV_SYMBOL_MAP.get(intent.symbol)
        reasons: list[str] = []

        if review.route.intent.model_dump(mode="json") != intent.model_dump(mode="json"):
            reasons.append("immutable_intent_mismatch")
        if review.decision is not ReviewDecision.ALLOW:
            reasons.append(f"review_{review.decision.value.lower()}")
        if not self.settings.paper_strategy_allowed(intent.strategy_id, intent.strategy_revision):
            reasons.append("strategy_not_paper_approved")
        if system_mode is not SystemMode.NORMAL:
            reasons.append(f"system_mode_{system_mode.value.lower()}")
        if decided_at_ms < review.reviewed_at_ms:
            reasons.append("decision_clock_precedes_review")
        if decided_at_ms < intent.entry_eligible_ts_ms:
            reasons.append("candidate_not_yet_eligible")
        if decided_at_ms > intent.entry_expires_ts_ms:
            reasons.append("candidate_expired")

        if expected_symbol is None:
            reasons.append("unsupported_signal_symbol")
        elif venue.symbol != expected_symbol:
            reasons.append("venue_symbol_mismatch")
        if decided_at_ms < venue.observed_at_ms:
            reasons.append("venue_observation_from_future")
        if decided_at_ms > venue.expires_at_ms:
            reasons.append("venue_quality_stale")
        if not venue.entry_allowed:
            reasons.append("venue_entry_blocked")
            reasons.extend(f"venue_{code}" for code in venue.reason_codes)

        account_reasons, account_metrics = self._account_gate(
            account,
            venue_symbol=venue.symbol,
            strategy_id=intent.strategy_id,
            decided_at_ms=decided_at_ms,
            reservations=reservations,
        )
        reasons.extend(account_reasons)

        allocation_reasons, allocation_metrics = self._allocation_gate(
            allocation,
            strategy_id=intent.strategy_id,
            side=intent.side,
            decided_at_ms=decided_at_ms,
        )
        reasons.extend(allocation_reasons)

        worst_entry = venue.best_ask if intent.side is Side.LONG else venue.best_bid
        if intent.side is Side.LONG and not (
            intent.exit_plan.stop_price < worst_entry < intent.exit_plan.target_price
        ):
            reasons.append("long_entry_exit_geometry_invalid")
        elif intent.side is Side.SHORT and not (
            intent.exit_plan.target_price < worst_entry < intent.exit_plan.stop_price
        ):
            reasons.append("short_entry_exit_geometry_invalid")

        loss_budget = 0.0
        leverage = 1.0
        quantity = 0.0
        if account_metrics is not None and allocation_metrics is not None:
            remaining_open_risk = max(
                0.0,
                account_metrics.equity_usd * self.settings.paper_max_total_open_risk_fraction
                - account_metrics.total_open_risk_usd
                - reservations.open_risk_usd,
            )
            loss_budget = min(
                account_metrics.equity_usd * self.settings.paper_per_trade_risk_fraction,
                remaining_open_risk,
            )
            if loss_budget <= 0:
                reasons.append("portfolio_open_risk_limit_exhausted")
            leverage = min(self.settings.paper_max_leverage, allocation_metrics.max_gross_leverage)
            if leverage < 1:
                reasons.append("macro_leverage_below_one")
                leverage = 1.0

        if not reasons and account_metrics is not None and allocation_metrics is not None:
            quantity = self._size_quantity(
                intent_side=intent.side,
                stop_price=intent.exit_plan.stop_price,
                worst_entry=worst_entry,
                venue=venue,
                loss_budget=loss_budget,
                leverage=leverage,
                account=account_metrics,
                allocation=allocation_metrics,
                reservations=reservations,
            )
            notional = quantity * worst_entry
            if quantity <= 0:
                reasons.append("position_capacity_exhausted")
            elif notional + 1e-9 < self.settings.paper_min_notional_usd:
                reasons.append("position_below_minimum_notional")
                quantity = 0.0

        fees, slippage, worst_loss = self._economics(
            quantity=quantity,
            stop_price=intent.exit_plan.stop_price,
            worst_entry=worst_entry,
            intent_side=intent.side,
            venue=venue,
        )
        if worst_loss > loss_budget + 1e-6:
            # This can only be floating-point drift or a programming error. It
            # must become a rejection, never an over-budget order.
            reasons.append("computed_loss_exceeds_budget")
            quantity = 0.0
            fees = slippage = worst_loss = 0.0

        approved = not reasons
        if not approved and quantity:
            quantity = 0.0
            fees = slippage = worst_loss = 0.0

        return RiskTradeDecisionV1(
            source=self.settings.service_name,
            correlation_id=intent.intent_id,
            causation_id=review.message_id,
            intent=intent,
            review=review,
            venue_quality=venue,
            approved=approved,
            rejection_reasons=tuple(reasons),
            decided_at_ms=output_time_ms,
            entry_policy=EntryPolicy.NEXT_BAR_MARKET,
            trading_mode=TradingMode.PAPER,
            evedex_profile=EvedexProfile.DEV,
            account_id=self.settings.paper_account_id,
            venue_symbol=venue.symbol,
            quantity=quantity,
            leverage=leverage,
            notional_usd=quantity * worst_entry,
            loss_budget_usd=loss_budget,
            worst_case_loss_usd=worst_loss,
            worst_entry_price=worst_entry,
            estimated_fees_usd=fees,
            estimated_slippage_usd=slippage,
            exit_plan=intent.exit_plan,
        )

    def _account_gate(
        self,
        account: AccountSnapshotV2 | None,
        *,
        venue_symbol: str,
        strategy_id: str,
        decided_at_ms: int,
        reservations: PaperReservations,
    ) -> tuple[list[str], _AccountMetrics | None]:
        reasons: list[str] = []
        if account is None:
            return ["account_snapshot_missing"], None
        if account.trading_mode is not TradingMode.PAPER or account.evedex_profile is not EvedexProfile.DEV:
            reasons.append("account_environment_mismatch")
        if account.account_id != self.settings.paper_account_id:
            reasons.append("account_id_mismatch")
        if not account.reconciled:
            reasons.append("account_not_reconciled")
        if account.reconciliation_seq <= 0:
            reasons.append("account_reconciliation_sequence_invalid")
        age_ms = decided_at_ms - account.captured_at_ms
        if age_ms < 0:
            reasons.append("account_snapshot_from_future")
        elif age_ms > self.settings.paper_account_snapshot_max_age_s * 1_000:
            reasons.append("account_snapshot_stale")

        if venue_symbol in reservations.symbols:
            reasons.append("symbol_has_reserved_idea")
        if any(position.venue_symbol == venue_symbol for position in account.positions):
            reasons.append("symbol_has_active_position")
        if any(order.venue_symbol == venue_symbol for order in account.open_orders):
            reasons.append("symbol_has_open_order")

        active_canary_trade_ids = {
            position.trade_id
            for position in account.positions
            if position.strategy_id == PAPER_CANARY_STRATEGY_ID
        }
        active_canary_trade_ids.update(
            order.trade_id
            for order in account.open_orders
            if order.strategy_id == PAPER_CANARY_STRATEGY_ID and order.order_role is OrderRole.ENTRY
        )
        if (
            strategy_id == PAPER_CANARY_STRATEGY_ID
            and len(active_canary_trade_ids) + reservations.active_canary_ideas >= 1
        ):
            reasons.append("global_canary_idea_limit_reached")

        position_risk = sum(self._position_stop_risk(position) for position in account.positions)
        if account.total_open_risk_usd + 1e-6 < position_risk:
            reasons.append("account_open_risk_understated")
        if (
            account.total_open_risk_usd + reservations.open_risk_usd
            >= account.equity_usd * self.settings.paper_max_total_open_risk_fraction - 1e-9
        ):
            reasons.append("portfolio_open_risk_limit_exhausted")

        daily_drawdown = max(
            0.0,
            (account.durable_day_start_equity_usd - account.equity_usd)
            / account.durable_day_start_equity_usd,
            -account.daily_realized_pnl_usd / account.durable_day_start_equity_usd,
            (account.durable_peak_equity_usd - account.equity_usd) / account.durable_peak_equity_usd,
        )
        if daily_drawdown * 100 >= self.settings.max_daily_drawdown_pct:
            reasons.append("daily_drawdown_limit_reached")

        gross_notional = sum(
            abs(position.signed_quantity) * position.mark_price for position in account.positions
        )
        strategy_notional = sum(
            abs(position.signed_quantity) * position.mark_price
            for position in account.positions
            if position.strategy_id == strategy_id
        )
        for order in account.open_orders:
            if order.order_role is not OrderRole.ENTRY:
                continue
            reasons.append("pending_entry_order_present")
            if order.filled_quantity >= order.quantity:
                # Presence in the authoritative open-order set keeps admission
                # blocked even if a transient venue snapshot reports a full fill.
                continue
            if order.price is None:
                reasons.append("unvalued_pending_entry_order")
                continue
            pending_notional = (order.quantity - order.filled_quantity) * order.price
            gross_notional += pending_notional
            if order.strategy_id == strategy_id:
                strategy_notional += pending_notional

        return reasons, _AccountMetrics(
            equity_usd=account.equity_usd,
            available_balance_usd=account.available_balance_usd,
            total_open_risk_usd=account.total_open_risk_usd,
            gross_notional_usd=gross_notional,
            strategy_notional_usd=strategy_notional,
        )

    def _allocation_gate(
        self,
        allocation: StrategicAllocation | None,
        *,
        strategy_id: str,
        side: Side,
        decided_at_ms: int,
    ) -> tuple[list[str], _AllocationMetrics | None]:
        if allocation is None:
            return ["strategic_allocation_missing"], None
        reasons: list[str] = []
        invalid = allocation_error(allocation)
        if invalid is not None:
            reasons.append("strategic_allocation_invalid")
        produced_at = allocation.produced_at
        if produced_at.utcoffset() is None:
            reasons.append("strategic_allocation_naive_timestamp")
            produced_at_ms = decided_at_ms + 1
        else:
            produced_at_ms = int(produced_at.astimezone(UTC).timestamp() * 1_000)
        age_ms = decided_at_ms - produced_at_ms
        if age_ms < 0:
            reasons.append("strategic_allocation_from_future")
        elif age_ms > self.settings.strategic_allocation_max_age_s * 1_000:
            reasons.append("strategic_allocation_stale")

        strategy_weight = allocation.strategy_weights.get(strategy_id, 0.0)
        if strategy_weight <= 0:
            reasons.append("strategy_has_no_macro_allocation")
        if allocation.regime is MarketRegime.CHOP:
            reasons.append("macro_regime_chop")
        elif allocation.regime is MarketRegime.BEAR and side is Side.LONG:
            reasons.append("macro_regime_forbids_long")
        elif allocation.regime is MarketRegime.BULL and side is Side.SHORT:
            reasons.append("macro_regime_forbids_short")

        return reasons, _AllocationMetrics(
            strategy_id=strategy_id,
            stable_reserve_fraction=allocation.stable_reserve_pct,
            strategy_weight=strategy_weight,
            max_gross_leverage=allocation.max_gross_leverage,
        )

    def _size_quantity(
        self,
        *,
        intent_side: Side,
        stop_price: float,
        worst_entry: float,
        venue: VenueQualityV1,
        loss_budget: float,
        leverage: float,
        account: _AccountMetrics,
        allocation: _AllocationMetrics,
        reservations: PaperReservations,
    ) -> float:
        side_slippage_bps = venue.buy_slippage_bps if intent_side is Side.LONG else venue.sell_slippage_bps
        loss_per_unit = (
            abs(worst_entry - stop_price)
            + (worst_entry + stop_price) * venue.taker_fee_bps / 10_000
            + venue.venue_mid_price * side_slippage_bps / 10_000
        )
        if not math.isfinite(loss_per_unit) or loss_per_unit <= 0 or loss_budget <= 0:
            return 0.0
        risk_quantity = loss_budget / loss_per_unit

        deployable_equity = min(
            account.available_balance_usd,
            account.equity_usd * (1 - allocation.stable_reserve_fraction),
        )
        margin_notional_cap = max(0.0, deployable_equity * leverage - reservations.notional_usd)
        gross_cap = (
            account.equity_usd * (1 - allocation.stable_reserve_fraction) * allocation.max_gross_leverage
        )
        remaining_gross = max(
            0.0,
            gross_cap - account.gross_notional_usd - reservations.notional_usd,
        )
        strategy_cap = account.equity_usd * allocation.strategy_weight * allocation.max_gross_leverage
        remaining_strategy = max(
            0.0,
            strategy_cap
            - account.strategy_notional_usd
            - reservations.notional_for_strategy(allocation.strategy_id),
        )
        notional_cap = min(
            self.settings.paper_max_position_notional_usd,
            venue.assessed_notional_usd,
            venue.depth_usd,
            margin_notional_cap,
            remaining_gross,
            remaining_strategy,
        )
        if not math.isfinite(notional_cap) or notional_cap <= 0:
            return 0.0
        return max(0.0, min(risk_quantity, notional_cap / worst_entry))

    @staticmethod
    def _economics(
        *,
        quantity: float,
        stop_price: float,
        worst_entry: float,
        intent_side: Side,
        venue: VenueQualityV1,
    ) -> tuple[float, float, float]:
        side_slippage_bps = venue.buy_slippage_bps if intent_side is Side.LONG else venue.sell_slippage_bps
        fees = quantity * (worst_entry + stop_price) * venue.taker_fee_bps / 10_000
        slippage = quantity * venue.venue_mid_price * side_slippage_bps / 10_000
        loss = quantity * abs(worst_entry - stop_price) + fees + slippage
        return fees, slippage, loss

    @staticmethod
    def _position_stop_risk(position: PositionSnapshotV2) -> float:
        side = position.side
        quantity = abs(position.signed_quantity)
        entry = position.entry_price
        stop = position.exit_plan.stop_price
        if side is Side.LONG:
            return quantity * max(0.0, entry - stop)
        return quantity * max(0.0, stop - entry)


@dataclass(frozen=True, slots=True)
class _AccountMetrics:
    equity_usd: float
    available_balance_usd: float
    total_open_risk_usd: float
    gross_notional_usd: float
    strategy_notional_usd: float


@dataclass(frozen=True, slots=True)
class _AllocationMetrics:
    strategy_id: str
    stable_reserve_fraction: float
    strategy_weight: float
    max_gross_leverage: float


def reservation_view(decisions: Iterable[RiskTradeDecisionV1]) -> PaperReservations:
    """Aggregate un-reconciled approvals without reading LLM priority/confidence."""

    approved = tuple(decision for decision in decisions if decision.approved)
    strategy_totals: dict[str, float] = {}
    for decision in approved:
        strategy_id = decision.intent.strategy_id
        strategy_totals[strategy_id] = strategy_totals.get(strategy_id, 0.0) + decision.notional_usd
    return PaperReservations(
        symbols=frozenset(decision.venue_symbol for decision in approved),
        open_risk_usd=sum(decision.worst_case_loss_usd for decision in approved),
        notional_usd=sum(decision.notional_usd for decision in approved),
        strategy_notional_usd=tuple(sorted(strategy_totals.items())),
        active_canary_ideas=sum(
            decision.intent.strategy_id == PAPER_CANARY_STRATEGY_ID for decision in approved
        ),
    )
