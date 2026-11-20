"""Adaptive risk: how much of the owner's per-trade ceiling one entry may use.

The owner's constitution sets the ceiling (at most 0.25 % of equity per trade). Inside it
the system decides, from evidence only:

* **edge** (``learning.LearningLedger``): no proven edge yet uses a reduced share; a proven
  negative or zero lower bound uses nothing; a proven positive lower bound grows toward the
  full ceiling as the lower bound approaches ``full_edge_r``;
* **drawdown**: the share halves as equity falls ``drawdown_halving_fraction`` below its
  durable peak, and keeps shrinking with deeper drawdown, so losses reduce risk and nothing
  ever tries to win them back;
* **review priority** (0-100 from the Aggregator): a low-priority ALLOW uses less;
* **venue cost**: spread and slippage above ``cost_halving_bps`` shrink the share;
* **minimum lot** (``minimum_lot_fraction``): a share too small for the venue's minimum
  lot may rise to it, within the ceiling and its drawdown reduction only.

The result never exceeds the ceiling and is rounded down. ``adapt_limits`` applies it to
``LiveRiskLimitsV1``; a zero result means no entry for this candidate.

**Leverage** (``adaptive_leverage``) is decided the same way and is capped by the owner's
constitution (``LiveRiskLimitsV1.max_leverage``, at most 30x): only as much as the open
positions and this trade need for margin, up to 3x without a proven edge, 1x for a proven
non-positive edge, more as the proven lower bound grows, less in a drawdown, and never
so much that the stop sits past half the way to liquidation. Leverage never raises the
per-trade risk, which is set by the stop distance and the fraction above; it only lets the
account hold the notional that this risk requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from .learning import EdgeEstimate
from .live import LiveRiskLimitsV1

_CEILING = Decimal("0.0025")
_QUANTUM = Decimal("0.000001")


def _decimal(value: object, name: str) -> Decimal:
    parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


@dataclass(frozen=True, slots=True)
class AdaptiveRiskPolicyV1:
    ceiling_fraction: Decimal = _CEILING
    unproven_multiplier: Decimal = Decimal("0.5")
    full_edge_r: Decimal = Decimal("0.25")
    drawdown_halving_fraction: Decimal = Decimal("0.02")
    cost_halving_bps: Decimal = Decimal("20")
    minimum_priority_multiplier: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if not 0 < self.ceiling_fraction <= _CEILING:
            raise ValueError("the adaptive ceiling cannot exceed the constitution's 0.25 % per trade")
        if not (
            0 < self.unproven_multiplier <= 1
            and self.full_edge_r > 0
            and self.drawdown_halving_fraction > 0
            and self.cost_halving_bps > 0
            and 0 <= self.minimum_priority_multiplier <= 1
        ):
            raise ValueError("adaptive risk multipliers must be fractions and the scales positive")


@dataclass(frozen=True, slots=True)
class AdaptiveRiskDecisionV1:
    fraction: Decimal
    multiplier: Decimal
    reasons: tuple[str, ...]

    @property
    def allows_entry(self) -> bool:
        return self.fraction > 0


def adaptive_risk_fraction(
    policy: AdaptiveRiskPolicyV1,
    edge: EdgeEstimate,
    *,
    equity_usd: Decimal,
    peak_equity_usd: Decimal,
    review_priority: int | None = None,
    venue_cost_bps: Decimal | None = None,
) -> AdaptiveRiskDecisionV1:
    equity, peak = _decimal(equity_usd, "equity_usd"), _decimal(peak_equity_usd, "peak_equity_usd")
    if equity <= 0 or peak < equity:
        raise ValueError("equity must be positive and at most its durable peak")
    reasons: list[str] = []
    if not edge.proven:
        multiplier = policy.unproven_multiplier
        reasons.append(f"edge not proven yet: {policy.unproven_multiplier} of the ceiling")
    elif edge.lower_r <= 0:
        return AdaptiveRiskDecisionV1(
            Decimal(0),
            Decimal(0),
            (f"no proven edge ({edge.level}: lower bound {edge.lower_r} R over {edge.samples} trades)",),
        )
    else:
        share = min(Decimal(1), edge.lower_r / policy.full_edge_r)
        multiplier = policy.unproven_multiplier + (1 - policy.unproven_multiplier) * share
        reasons.append(f"proven edge ({edge.level}: lower bound {edge.lower_r} R): {multiplier:.3f}")
    drawdown = (peak - equity) / peak
    if drawdown > 0:
        factor = 1 / (1 + drawdown / policy.drawdown_halving_fraction)
        multiplier *= factor
        reasons.append(f"drawdown {drawdown:.4f} from peak: x{factor:.3f}")
    if review_priority is not None:
        if not 0 <= review_priority <= 100:
            raise ValueError("review priority is 0-100")
        factor = (
            policy.minimum_priority_multiplier
            + (1 - policy.minimum_priority_multiplier) * Decimal(review_priority) / 100
        )
        multiplier *= factor
        reasons.append(f"review priority {review_priority}: x{factor:.3f}")
    if venue_cost_bps is not None:
        cost = max(Decimal(0), _decimal(venue_cost_bps, "venue_cost_bps"))
        factor = 1 / (1 + cost / policy.cost_halving_bps)
        multiplier *= factor
        reasons.append(f"venue cost {cost} bps: x{factor:.3f}")
    multiplier = min(Decimal(1), max(Decimal(0), multiplier))
    fraction = (policy.ceiling_fraction * multiplier).quantize(_QUANTUM, rounding=ROUND_DOWN)
    return AdaptiveRiskDecisionV1(fraction, multiplier.quantize(Decimal("0.0001")), tuple(reasons))


@dataclass(frozen=True, slots=True)
class AdaptiveLeverageDecisionV1:
    leverage: Decimal
    reasons: tuple[str, ...]


UNPROVEN_LEVERAGE = Decimal(3)
STOP_SHARE_OF_LIQUIDATION = Decimal("0.5")


def adaptive_leverage(
    policy: AdaptiveRiskPolicyV1,
    edge: EdgeEstimate,
    *,
    constitution_max: Decimal,
    required_gross: Decimal | None = None,
    equity_usd: Decimal,
    peak_equity_usd: Decimal,
    stop_fraction: Decimal | None = None,
    venue_max: Decimal | None = None,
) -> AdaptiveLeverageDecisionV1:
    """This trade's account leverage: as much as the book needs, as little as possible.

    When supplied, ``required_gross`` is a caller's estimate of the leverage required.
    Without an estimate, return the ceiling for the LIVE evaluator to fit to the
    approved position and verified free margin. The ceiling follows the evidence (3x without a
    proven edge, up to the constitution as the proven lower bound grows, nothing beyond 1x
    for a proven non-positive edge), by drawdown (the evidence cap shrinks like the risk
    share), by the stop (it must sit at most half way to liquidation, about 1/leverage),
    by the venue's own maximum and by the constitution. Whole numbers, at least 1x.
    Leverage never raises a trade's stop-loss risk; the stop distance and the adaptive
    fraction set that.
    """
    cap = _decimal(constitution_max, "constitution_max")
    need = None if required_gross is None else _decimal(required_gross, "required_gross")
    equity, peak = _decimal(equity_usd, "equity_usd"), _decimal(peak_equity_usd, "peak_equity_usd")
    if cap < 1 or (need is not None and need < 0) or equity <= 0 or peak < equity:
        raise ValueError("leverage inputs: a cap of at least 1x, a non-negative need and valid equity")
    if cap == 1:
        return AdaptiveLeverageDecisionV1(Decimal(1), ("the constitution allows 1x only",))
    reasons: list[str] = []
    if not edge.proven:
        evidence_cap = min(cap, UNPROVEN_LEVERAGE)
        reasons.append(f"edge not proven yet: up to {evidence_cap}x for margin only")
    elif edge.lower_r <= 0:
        evidence_cap = Decimal(1)
        reasons.append(f"no proven positive edge ({edge.level}): 1x")
    else:
        share = min(Decimal(1), edge.lower_r / policy.full_edge_r)
        evidence_cap = min(cap, UNPROVEN_LEVERAGE + (cap - UNPROVEN_LEVERAGE) * share)
        reasons.append(f"proven edge ({edge.level}: lower bound {edge.lower_r} R): up to {evidence_cap:.1f}x")
    drawdown = (peak - equity) / peak
    if drawdown > 0:
        evidence_cap = max(Decimal(1), evidence_cap / (1 + drawdown / policy.drawdown_halving_fraction))
        reasons.append(f"drawdown {drawdown:.4f}: up to {evidence_cap:.1f}x")
    caps = [cap, evidence_cap]
    if stop_fraction is not None:
        stop = _decimal(stop_fraction, "stop_fraction")
        if stop > 0:
            caps.append(STOP_SHARE_OF_LIQUIDATION / stop)
            reasons.append(
                f"stop {stop:.4%} away: liquidation-safe up to {STOP_SHARE_OF_LIQUIDATION / stop:.1f}x"
            )
    if venue_max is not None and _decimal(venue_max, "venue_max") >= 1:
        caps.append(_decimal(venue_max, "venue_max"))
    ceiling = max(Decimal(1), min(caps).to_integral_value(rounding=ROUND_DOWN))
    if need is None:
        reasons.append(f"permitted margin leverage ceiling {ceiling}x")
        return AdaptiveLeverageDecisionV1(ceiling, tuple(reasons))
    wanted = max(Decimal(1), need.to_integral_value(rounding=ROUND_CEILING))
    leverage = min(ceiling, wanted)
    reasons.append(f"the book needs {need:.2f}x of margin; ceiling {ceiling}x")
    return AdaptiveLeverageDecisionV1(leverage, tuple(reasons))


def minimum_lot_fraction(
    policy: AdaptiveRiskPolicyV1,
    decision: AdaptiveRiskDecisionV1,
    *,
    needed_fraction: Decimal,
    equity_usd: Decimal,
    peak_equity_usd: Decimal,
) -> AdaptiveRiskDecisionV1 | None:
    """The adaptive share raised just enough for the venue's minimum lot, or None.

    A small account sized by the adaptive share alone can fall below the instrument's
    minimum lot and never trade. The share may then rise to ``needed_fraction``, but never
    above the constitution's ceiling, shrunk by drawdown exactly as the share is, and never
    for a candidate the evidence refuses (a zero share).
    """
    needed = _decimal(needed_fraction, "needed_fraction").quantize(_QUANTUM, rounding=ROUND_CEILING)
    equity, peak = _decimal(equity_usd, "equity_usd"), _decimal(peak_equity_usd, "peak_equity_usd")
    if equity <= 0 or peak < equity or needed <= 0:
        raise ValueError("a positive need, positive equity at most its durable peak")
    if not decision.allows_entry:
        return None
    if needed <= decision.fraction:
        return decision
    cap = policy.ceiling_fraction / (1 + (peak - equity) / peak / policy.drawdown_halving_fraction)
    if needed > cap:
        return None
    return AdaptiveRiskDecisionV1(
        needed,
        (needed / policy.ceiling_fraction).quantize(Decimal("0.0001")),
        (*decision.reasons, f"raised to {needed} of equity for the venue's minimum lot (cap {cap:.6f})"),
    )


def adapt_limits(limits: LiveRiskLimitsV1, decision: AdaptiveRiskDecisionV1) -> LiveRiskLimitsV1 | None:
    """The same limits with the adaptive per-trade fraction, never above the original; None = no entry."""
    if not decision.allows_entry:
        return None
    fraction = min(Decimal(str(limits.per_trade_risk_fraction)), decision.fraction)
    return LiveRiskLimitsV1.model_validate(
        {**limits.model_dump(mode="json"), "per_trade_risk_fraction": float(fraction)}
    )


__all__ = [
    "AdaptiveLeverageDecisionV1",
    "AdaptiveRiskDecisionV1",
    "AdaptiveRiskPolicyV1",
    "adapt_limits",
    "adaptive_leverage",
    "adaptive_risk_fraction",
    "minimum_lot_fraction",
]
