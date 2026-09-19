"""Pure admission policy for the sealed market-data simulator.

The policy receives immutable candidate review and recorded-book inputs and
returns a SIM-only decision.  It deliberately has no clock, storage, account,
or venue client: the caller supplies the decision timestamp and persists the
result through the isolated simulator journal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from kairos_core.contracts import (
    CandidateReviewV1,
    RecordedTopNBookFrameV1,
    SimulationRiskDecisionV1,
    SimulationSessionV1,
)
from kairos_core.enums import ReviewDecision, Side


def _as_decimal(value: float, *, name: str) -> Decimal:
    """Convert a finite public-contract number without binary-float drift."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return Decimal(str(value))


def _round_to_step(value: Decimal, step: Decimal, *, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


@dataclass(frozen=True, slots=True)
class SimulationRiskPolicy:
    """Deterministically admit only causal, usable recorded-book candidates.

    Requested quantity is a caller-provided research input, never an account
    sizing result.  The policy caps it to the frozen visible-book participation
    fraction without consuming book depth; the execution controller records
    actual modeled consumption atomically with its command receipt.
    """

    source: str = "simulator-risk"

    def __post_init__(self) -> None:
        if not self.source or self.source != self.source.strip():
            raise ValueError("source must be a normalized non-empty string")

    def evaluate(
        self,
        *,
        session: SimulationSessionV1,
        review: CandidateReviewV1,
        selected_book_frame: RecordedTopNBookFrameV1 | None,
        decided_at_ms: int,
        requested_quantity: float,
    ) -> SimulationRiskDecisionV1:
        """Return one byte-stable SIM decision for supplied immutable inputs.

        A caller that arrives before the review, session, or next-bar boundary
        cannot create a decision at all: fabricating a rejected decision at a
        later timestamp would misrepresent causal history.  Other failures are
        retained as explicit rejected evidence.
        """

        self._validate_decision_clock(session=session, review=review, decided_at_ms=decided_at_ms)
        intent = review.intent
        reasons: set[str] = set()

        self._environment_rejection_reasons(
            reasons=reasons,
            session=session,
            intent_venue=intent.venue,
            selected_book_frame=selected_book_frame,
        )
        if review.decision is not ReviewDecision.ALLOW:
            reasons.add(f"REVIEW_{review.decision.value}")
        if (intent.strategy_id, intent.strategy_revision) not in {
            (item.strategy_id, item.strategy_revision) for item in session.strategy_allowlist
        }:
            reasons.add("STRATEGY_NOT_SESSION_ALLOWLISTED")
        if intent.symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}:
            reasons.add("UNSUPPORTED_INTENT_SYMBOL")
        if decided_at_ms > intent.entry_expires_ts_ms:
            reasons.add("INTENT_EXPIRED")

        self._book_rejection_reasons(
            reasons=reasons,
            session=session,
            intent_symbol=intent.symbol,
            selected_book_frame=selected_book_frame,
            decided_at_ms=decided_at_ms,
        )

        quantity = Decimal(0)
        price_cap: Decimal | None = None
        if not reasons and selected_book_frame is not None:
            quantity, quantity_reason = self._admitted_quantity(
                requested_quantity=requested_quantity,
                selected_book_frame=selected_book_frame,
                side=intent.side,
                session=session,
            )
            if quantity_reason is not None:
                reasons.add(quantity_reason)
            else:
                price_cap, price_reason = self._price_cap(
                    selected_book_frame=selected_book_frame,
                    side=intent.side,
                    session=session,
                )
                if price_reason is not None:
                    reasons.add(price_reason)
                elif not self._exit_geometry_is_valid(
                    side=intent.side,
                    price_cap=price_cap,
                    stop_price=intent.exit_plan.stop_price,
                    target_price=intent.exit_plan.target_price,
                ):
                    reasons.add("PRICE_CAP_EXIT_GEOMETRY_INVALID")

        approved = not reasons
        return SimulationRiskDecisionV1(
            source=self.source,
            correlation_id=intent.intent_id,
            causation_id=review.message_id,
            session=session,
            intent=intent,
            review=review,
            selected_book_frame=selected_book_frame,
            approved=approved,
            rejection_reasons=tuple(sorted(reasons)),
            quantity=float(quantity) if approved else 0.0,
            price_cap=float(price_cap) if approved and price_cap is not None else None,
            decided_at_ms=decided_at_ms,
        )

    @staticmethod
    def _validate_decision_clock(
        *,
        session: SimulationSessionV1,
        review: CandidateReviewV1,
        decided_at_ms: int,
    ) -> None:
        if isinstance(decided_at_ms, bool) or not isinstance(decided_at_ms, int) or decided_at_ms < 0:
            raise ValueError("decided_at_ms must be a non-negative integer")
        if not session.started_at_ms <= decided_at_ms <= session.ends_at_ms:
            raise ValueError("decided_at_ms must lie inside the immutable simulation session")
        if decided_at_ms < review.reviewed_at_ms:
            raise ValueError("decided_at_ms cannot predate the immutable candidate review")
        if decided_at_ms < review.intent.entry_eligible_ts_ms:
            raise ValueError("decided_at_ms cannot predate immutable next-bar eligibility")

    @staticmethod
    def _environment_rejection_reasons(
        *,
        reasons: set[str],
        session: SimulationSessionV1,
        intent_venue: str,
        selected_book_frame: RecordedTopNBookFrameV1 | None,
    ) -> None:
        """Fail closed if a caller bypassed strict Pydantic contract validation.

        Normal deserialization cannot create these mismatches because the
        versioned contracts use ``Literal`` fields.  They are still checked at
        this public boundary so a manually constructed model instance cannot
        turn a PAPER/venue-shaped object into an approved SIM decision.
        """

        if session.execution_environment != "SIMULATED":
            reasons.add("SESSION_ENVIRONMENT_MISMATCH")
        if intent_venue != "BINANCE_UM":
            reasons.add("INTENT_VENUE_MISMATCH")
        if selected_book_frame is None:
            return
        if selected_book_frame.execution_environment != "SIMULATED":
            reasons.add("BOOK_FRAME_ENVIRONMENT_MISMATCH")
        if selected_book_frame.market_data_venue != "BINANCE_UM":
            reasons.add("BOOK_FRAME_VENUE_MISMATCH")

    @staticmethod
    def _book_rejection_reasons(
        *,
        reasons: set[str],
        session: SimulationSessionV1,
        intent_symbol: str,
        selected_book_frame: RecordedTopNBookFrameV1 | None,
        decided_at_ms: int,
    ) -> None:
        if selected_book_frame is None:
            reasons.add("NO_RECORDED_BOOK_FRAME")
            return
        frame = selected_book_frame
        if frame.tape_id != session.tape_id:
            reasons.add("BOOK_FRAME_TAPE_MISMATCH")
        if frame.symbol != intent_symbol:
            reasons.add("BOOK_FRAME_SYMBOL_MISMATCH")
        if frame.continuity != "ADMITTED":
            reasons.add("BOOK_FRAME_NOT_ADMITTED")
        if frame.persisted_at_ms > decided_at_ms:
            reasons.add("BOOK_FRAME_FROM_FUTURE")
        elif decided_at_ms - frame.persisted_at_ms > session.assumptions.maximum_book_age_ms:
            reasons.add("BOOK_FRAME_STALE")
        if frame.persisted_at_ms - frame.exchange_at_ms > session.assumptions.maximum_frame_latency_ms:
            reasons.add("BOOK_FRAME_LATENCY_EXCEEDED")

    @staticmethod
    def _admitted_quantity(
        *,
        requested_quantity: float,
        selected_book_frame: RecordedTopNBookFrameV1,
        side: Side,
        session: SimulationSessionV1,
    ) -> tuple[Decimal, str | None]:
        try:
            requested = _as_decimal(requested_quantity, name="requested_quantity")
        except ValueError:
            return Decimal(0), "REQUESTED_QUANTITY_INVALID"
        if requested <= 0:
            return Decimal(0), "REQUESTED_QUANTITY_INVALID"

        levels = selected_book_frame.asks if side is Side.LONG else selected_book_frame.bids
        visible_quantity = sum(
            (_as_decimal(level.quantity, name="book_quantity") for level in levels),
            Decimal(0),
        )
        participation = _as_decimal(
            session.assumptions.depth_participation_fraction,
            name="depth_participation_fraction",
        )
        quantity_step = _as_decimal(session.assumptions.quantity_step, name="quantity_step")
        admitted = _round_to_step(
            min(requested, visible_quantity * participation),
            quantity_step,
            rounding=ROUND_FLOOR,
        )
        if admitted <= 0:
            return Decimal(0), "REQUESTED_QUANTITY_NOT_EXECUTABLE"
        return admitted, None

    @staticmethod
    def _price_cap(
        *,
        selected_book_frame: RecordedTopNBookFrameV1,
        side: Side,
        session: SimulationSessionV1,
    ) -> tuple[Decimal, str | None]:
        top_price = (
            _as_decimal(selected_book_frame.asks[0].price, name="ask_price")
            if side is Side.LONG
            else _as_decimal(selected_book_frame.bids[0].price, name="bid_price")
        )
        slippage_fraction = _as_decimal(
            session.assumptions.adverse_slippage_bps,
            name="adverse_slippage_bps",
        ) / Decimal(10_000)
        price_tick = _as_decimal(session.assumptions.price_tick, name="price_tick")
        multiplier = Decimal(1) + slippage_fraction if side is Side.LONG else Decimal(1) - slippage_fraction
        unrounded = top_price * multiplier
        price_cap = _round_to_step(
            unrounded,
            price_tick,
            rounding=ROUND_CEILING if side is Side.LONG else ROUND_FLOOR,
        )
        if price_cap <= 0:
            return Decimal(0), "PRICE_CAP_NOT_POSITIVE"
        return price_cap, None

    @staticmethod
    def _exit_geometry_is_valid(
        *,
        side: Side,
        price_cap: Decimal | None,
        stop_price: float,
        target_price: float,
    ) -> bool:
        if price_cap is None:
            return False
        stop = _as_decimal(stop_price, name="stop_price")
        target = _as_decimal(target_price, name="target_price")
        if side is Side.LONG:
            return stop < price_cap < target
        return target < price_cap < stop
