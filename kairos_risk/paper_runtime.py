"""Race-safe in-process correlation state for PAPER risk inputs."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from kairos_core.contracts import (
    AccountSnapshotV2,
    CandidateReviewV1,
    OpenOrderSnapshotV2,
    PositionSnapshotV2,
    RiskTradeDecisionV1,
    StrategicAllocation,
    VenueQualityV1,
)
from kairos_core.enums import EvedexProfile, OrderRole, SystemMode, TradingMode

from .config import PAPER_DEV_SYMBOL_MAP, RiskSettings
from .paper import PaperReservations, PaperRiskPipeline, reservation_view


class PaperInputUnavailable(RuntimeError):
    """A retryable causal input has not reached the risk boundary yet."""


class PaperInputDeadlineExceeded(RuntimeError):
    """Required admission input did not arrive before intent expiry."""


@dataclass(frozen=True, slots=True)
class _Reservation:
    decision: RiskTradeDecisionV1
    reconciliation_seq: int


class PaperRiskCoordinator:
    """Correlate latest authoritative facts and serialize symbol reservations.

    Redis/PostgreSQL inbox/outbox supplies crash idempotency. This coordinator
    additionally closes races between account, venue and review subscriptions
    inside one process and prevents two simultaneous ideas for one symbol.
    """

    def __init__(self, settings: RiskSettings, pipeline: PaperRiskPipeline | None = None) -> None:
        self.settings = settings
        self.pipeline = pipeline or PaperRiskPipeline(settings)
        self._changed = asyncio.Condition()
        self._account: AccountSnapshotV2 | None = None
        self._latest_account_seq: int | None = None
        self._latest_account_captured_at_ms: int | None = None
        self._venue: dict[str, VenueQualityV1] = {}
        self._venue_versions: dict[str, tuple[int, str | None]] = {}
        self._decisions: OrderedDict[str, RiskTradeDecisionV1] = OrderedDict()
        self._reservations: dict[str, _Reservation] = {}
        self._durable_recovery_loaded = False
        self._account_recovery_ready = False

    @property
    def account(self) -> AccountSnapshotV2 | None:
        return self._account

    @property
    def reservations(self) -> PaperReservations:
        return reservation_view(item.decision for item in self._reservations.values())

    @property
    def recovery_complete(self) -> bool:
        return self._durable_recovery_loaded and self._account_recovery_ready

    async def restore_reservations(
        self,
        decisions: Iterable[RiskTradeDecisionV1],
    ) -> None:
        """Restore committed approvals before any new PAPER review is admitted."""

        async with self._changed:
            if self._durable_recovery_loaded:
                raise RuntimeError("PAPER reservations were already restored")
            for decision in decisions:
                self._validate_recovered_decision(decision)
                trade_id = decision.trade_id
                review_id = decision.review.review_id
                if trade_id is None or review_id is None:  # strict contract guarantees both
                    raise ValueError("recovered PAPER decision has incomplete canonical lineage")
                existing = self._reservations.get(trade_id)
                if existing is not None and existing.decision.decision_id != decision.decision_id:
                    raise ValueError("conflicting durable approvals reuse one trade_id")
                self._reservations[trade_id] = _Reservation(
                    decision=decision,
                    reconciliation_seq=0,
                )
                self._decisions[review_id] = decision
            self._trim_decision_cache()
            self._durable_recovery_loaded = True
            if self._account is not None:
                self._account_recovery_ready = self._account_is_authoritative(self._account)
                if self._account_recovery_ready:
                    try:
                        self._reconcile_reservations(self._account)
                    except ValueError:
                        self._account = None
                        self._account_recovery_ready = False
                        self._changed.notify_all()
                        raise
            self._changed.notify_all()

    async def apply_account(self, snapshot: AccountSnapshotV2) -> bool:
        """Apply a monotonic full reconciliation; return False for an old duplicate."""

        async with self._changed:
            latest_seq = self._latest_account_seq
            latest_captured = self._latest_account_captured_at_ms
            if latest_seq is not None and snapshot.reconciliation_seq < latest_seq:
                return False
            if latest_seq is not None and snapshot.reconciliation_seq == latest_seq:
                if self._account is not None and self._account.snapshot_id == snapshot.snapshot_id:
                    return False
                self._account = None
                self._account_recovery_ready = False
                self._changed.notify_all()
                raise ValueError("conflicting AccountSnapshotV2 reconciliation sequence")
            if latest_captured is not None and snapshot.captured_at_ms < latest_captured:
                self._latest_account_seq = snapshot.reconciliation_seq
                self._account = None
                self._account_recovery_ready = False
                self._changed.notify_all()
                raise ValueError("AccountSnapshotV2 capture time moved backwards")

            self._latest_account_seq = snapshot.reconciliation_seq
            self._latest_account_captured_at_ms = snapshot.captured_at_ms
            self._account = snapshot
            self._account_recovery_ready = self._account_is_authoritative(snapshot)
            if self._account_recovery_ready:
                try:
                    self._reconcile_reservations(snapshot)
                except ValueError:
                    # Preserve the observed sequence as poisoned. A newer exact
                    # reconciliation is required before PAPER admission resumes.
                    self._account = None
                    self._account_recovery_ready = False
                    self._changed.notify_all()
                    raise
            self._changed.notify_all()
            return True

    async def apply_venue(self, venue: VenueQualityV1) -> bool:
        """Apply the newest exact DEV measurement for one fixed instrument."""

        if venue.profile is not EvedexProfile.DEV:
            raise ValueError("PAPER venue quality must use the EVEDEX DEV profile")
        if venue.symbol not in PAPER_DEV_SYMBOL_MAP.values():
            raise ValueError("PAPER venue quality symbol is outside the fixed DEV universe")
        async with self._changed:
            current = self._venue_versions.get(venue.symbol)
            if current is not None and venue.observed_at_ms < current[0]:
                return False
            if current is not None and venue.observed_at_ms == current[0]:
                if current[1] == venue.measurement_id:
                    return False
                self._venue.pop(venue.symbol, None)
                self._venue_versions[venue.symbol] = (venue.observed_at_ms, None)
                self._changed.notify_all()
                raise ValueError("conflicting VenueQualityV1 observation timestamp")
            self._venue_versions[venue.symbol] = (venue.observed_at_ms, venue.measurement_id)
            self._venue[venue.symbol] = venue
            self._changed.notify_all()
            return True

    async def wait_for_inputs(
        self,
        review: CandidateReviewV1,
        *,
        now_ms: Callable[[], int],
    ) -> None:
        """Wake promptly on cross-stream venue/account/recovery progress."""

        expected_symbol = PAPER_DEV_SYMBOL_MAP.get(review.intent.symbol)
        while True:
            async with self._changed:
                if self.recovery_complete and expected_symbol in self._venue:
                    return
                remaining_ms = review.intent.entry_expires_ts_ms - now_ms()
                if remaining_ms <= 0:
                    raise PaperInputDeadlineExceeded(
                        "PAPER admission inputs did not arrive before intent expiry"
                    )
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=remaining_ms / 1_000)
                except TimeoutError as exc:
                    raise PaperInputDeadlineExceeded(
                        "PAPER admission inputs did not arrive before intent expiry"
                    ) from exc

    async def evaluate(
        self,
        review: CandidateReviewV1,
        *,
        allocation: StrategicAllocation | None,
        decided_at_ms: int,
        system_mode: SystemMode,
        admission_rejection_reasons: tuple[str, ...] = (),
    ) -> RiskTradeDecisionV1:
        """Serialize evaluation and reserve an approved symbol before publish."""

        review_id = review.review_id
        if review_id is None:  # impossible after strict contract validation
            raise ValueError("CandidateReviewV1 has no canonical review_id")
        async with self._changed:
            if not self.recovery_complete:
                raise PaperInputUnavailable("PAPER durable/account recovery is incomplete")
            cached = self._decisions.get(review_id)
            if cached is not None and not admission_rejection_reasons:
                self._decisions.move_to_end(review_id)
                return cached
            expected_symbol = PAPER_DEV_SYMBOL_MAP.get(review.intent.symbol)
            venue = self._venue.get(expected_symbol or "")
            if venue is None:
                raise PaperInputUnavailable("fresh fixed-symbol VenueQualityV1 is unavailable")

            reservation_snapshot = reservation_view(item.decision for item in self._reservations.values())
            decision = self.pipeline.evaluate(
                review,
                venue,
                account=self._account,
                allocation=allocation,
                decided_at_ms=decided_at_ms,
                system_mode=system_mode,
                reservations=reservation_snapshot,
                admission_rejection_reasons=admission_rejection_reasons,
            )
            if admission_rejection_reasons:
                # Admission authority is external to the decision cache.  A direct-bus
                # canary must never reuse an earlier approved cache entry after its
                # single-use durable arm is absent, and an unauthorized rejection must
                # not poison a later legitimate same-inbox recovery.
                return decision
            self._decisions[review_id] = decision
            self._decisions.move_to_end(review_id)
            if decision.approved:
                trade_id = decision.trade_id
                if trade_id is None:  # impossible after strict contract validation
                    raise ValueError("approved RiskTradeDecisionV1 has no trade_id")
                self._reservations[trade_id] = _Reservation(
                    decision=decision,
                    reconciliation_seq=self._latest_account_seq or 0,
                )
            self._trim_decision_cache()
            return decision

    def _account_is_authoritative(self, snapshot: AccountSnapshotV2) -> bool:
        return (
            snapshot.trading_mode is TradingMode.PAPER
            and snapshot.evedex_profile is EvedexProfile.DEV
            and snapshot.account_id == self.settings.paper_account_id
            and snapshot.reconciled
            and snapshot.reconciliation_seq > 0
        )

    def _validate_recovered_decision(self, decision: RiskTradeDecisionV1) -> None:
        if not decision.approved:
            raise ValueError("only approved decisions may become PAPER reservations")
        if (
            decision.trading_mode is not TradingMode.PAPER
            or decision.evedex_profile is not EvedexProfile.DEV
            or decision.account_id != self.settings.paper_account_id
            or decision.venue_symbol not in PAPER_DEV_SYMBOL_MAP.values()
        ):
            raise ValueError("durable PAPER decision belongs to a different environment/account")

    def _trim_decision_cache(self) -> None:
        """Never evict replay identity while its approval remains reserved."""

        reserved_review_ids = {
            reservation.decision.review.review_id for reservation in self._reservations.values()
        }
        while len(self._decisions) > self.settings.paper_decision_cache_size:
            evictable = next(
                (review_id for review_id in self._decisions if review_id not in reserved_review_ids),
                None,
            )
            if evictable is None:
                return
            self._decisions.pop(evictable)

    def _reconcile_reservations(self, snapshot: AccountSnapshotV2) -> None:
        positions_by_trade: dict[str, list[PositionSnapshotV2]] = {}
        orders_by_trade: dict[str, list[OpenOrderSnapshotV2]] = {}
        for position in snapshot.positions:
            positions_by_trade.setdefault(position.trade_id, []).append(position)
        for order in snapshot.open_orders:
            orders_by_trade.setdefault(order.trade_id, []).append(order)

        transferred: set[str] = set()
        for trade_id, reservation in tuple(self._reservations.items()):
            positions = positions_by_trade.get(trade_id, ())
            orders = orders_by_trade.get(trade_id, ())
            for representation in (*positions, *orders):
                self._validate_reconciled_lineage(reservation.decision, representation)
            if positions or any(order.order_role is OrderRole.ENTRY for order in orders):
                # A position or open entry now owns the idea. Protective orders
                # alone cannot prove that account risk owns the exposure.
                transferred.add(trade_id)
                continue
            if orders:
                raise ValueError(
                    "reconciled trade representation has protective orders without a position or entry"
                )
            if (
                snapshot.reconciliation_seq > reservation.reconciliation_seq
                and snapshot.captured_at_ms > reservation.decision.intent.entry_expires_ts_ms
            ):
                # Entry is no longer executable and a newer authoritative full
                # snapshot proves no venue position/order exists for the trade.
                transferred.add(trade_id)

        for trade_id in transferred:
            self._reservations.pop(trade_id, None)

    @staticmethod
    def _validate_reconciled_lineage(
        decision: RiskTradeDecisionV1,
        representation: PositionSnapshotV2 | OpenOrderSnapshotV2,
    ) -> None:
        mismatches: list[str] = []
        expected = {
            "venue_symbol": decision.venue_symbol,
            "strategy_id": decision.intent.strategy_id,
            "strategy_revision": decision.intent.strategy_revision,
            "intent_id": decision.intent.intent_id,
            "risk_decision_id": decision.decision_id,
            "trade_id": decision.trade_id,
        }
        for field, expected_value in expected.items():
            if getattr(representation, field) != expected_value:
                mismatches.append(field)
        if isinstance(representation, PositionSnapshotV2):
            if representation.side is not decision.intent.side:
                mismatches.append("side")
            if representation.exit_plan != decision.exit_plan:
                mismatches.append("exit_plan")
        if mismatches:
            raise ValueError(
                "reconciled trade representation conflicts with durable risk lineage: "
                + ", ".join(sorted(mismatches))
            )
