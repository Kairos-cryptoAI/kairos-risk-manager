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
* **venue cost**: spread and slippage above ``cost_halving_bps`` shrink the share.

The result never exceeds the ceiling and is rounded down. ``adapt_limits`` applies it to
``LiveRiskLimitsV1``; a zero result means no entry for this candidate.

**Leverage** (``adaptive_leverage``) is decided the same way and is capped by the owner's
constitution (``LiveRiskLimitsV1.max_leverage``, today exactly 1x): 1x without a proven
positive edge or in a drawdown, more only as far as the open positions actually need it
and the proven lower bound allows. Leverage never raises the per-trade risk, which is
set by the stop distance and the fraction above; it only lets the account hold the
notional that this risk requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

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


def adaptive_leverage(
    policy: AdaptiveRiskPolicyV1,
    edge: EdgeEstimate,
    *,
    constitution_max: Decimal,
    required_gross: Decimal,
    equity_usd: Decimal,
    peak_equity_usd: Decimal,
) -> AdaptiveLeverageDecisionV1:
    """The account leverage the evidence allows, never above the constitution and never above need.

    ``required_gross`` is the open plus proposed notional over equity. Rounded down to 0.1x;
    never below 1x (the venue's unleveraged setting).
    """
    cap = _decimal(constitution_max, "constitution_max")
    need = _decimal(required_gross, "required_gross")
    equity, peak = _decimal(equity_usd, "equity_usd"), _decimal(peak_equity_usd, "peak_equity_usd")
    if cap < 1 or need < 0 or equity <= 0 or peak < equity:
        raise ValueError("leverage inputs: a cap of at least 1x, a non-negative need and valid equity")
    if cap == 1:
        return AdaptiveLeverageDecisionV1(Decimal(1), ("the constitution allows 1x only",))
    if not edge.proven or edge.lower_r <= 0:
        return AdaptiveLeverageDecisionV1(Decimal(1), (f"no proven positive edge ({edge.level}): 1x",))
    drawdown = (peak - equity) / peak
    if drawdown > 0:
        return AdaptiveLeverageDecisionV1(Decimal(1), (f"drawdown {drawdown:.4f} from peak: 1x",))
    share = min(Decimal(1), edge.lower_r / policy.full_edge_r)
    allowed = 1 + (cap - 1) * share
    leverage = max(Decimal(1), min(cap, allowed, need))
    leverage = leverage.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    return AdaptiveLeverageDecisionV1(
        leverage,
        (
            f"proven edge ({edge.level}: lower bound {edge.lower_r} R) allows {allowed:.2f}x",
            f"open and proposed notional need {need:.2f}x; constitution caps at {cap}x",
        ),
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
]
