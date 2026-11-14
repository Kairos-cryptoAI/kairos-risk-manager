"""Learning ledger: what each strategy has actually earned, per instrument and market regime.

Inputs are closed-trade outcomes in R (the net result over the decision's worst-case
loss): live trades from the execution journal and, with less weight, historical
replays. The ledger answers one question for sizing: how much proven edge does this
strategy have on this instrument in this regime, and how sure are we?

The estimate is deliberately conservative. Its ``lower_r`` is a one-sided lower bound
of the mean R (mean minus z standard errors over the effective sample size), so a
short or noisy record cannot look like an edge. When the exact strategy x instrument x
regime cell has too few trades the ledger falls back to strategy x regime, then to the
strategy, then to "no evidence"; it never borrows evidence from another strategy.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

Level = Literal["strategy_symbol_regime", "strategy_regime", "strategy", "none"]
Source = Literal["LIVE", "REPLAY"]
UNKNOWN_REGIME = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    """One closed trade in R; ``regime`` is the Macro regime in force at the decision."""

    strategy_id: str
    symbol: str
    regime: str
    r_multiple: Decimal
    decided_at_ms: int
    source: Source

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.symbol or not self.regime:
            raise ValueError("an outcome needs its strategy, symbol and regime")
        r = Decimal(self.r_multiple)
        if not r.is_finite() or abs(r) > 100:
            raise ValueError("an outcome's R multiple must be finite and plausible")
        if self.source not in ("LIVE", "REPLAY") or self.decided_at_ms < 0:
            raise ValueError("an outcome needs a known source and a decision time")
        object.__setattr__(self, "r_multiple", r)


@dataclass(frozen=True, slots=True)
class EdgeEstimate:
    level: Level
    samples: int
    effective_samples: Decimal
    mean_r: Decimal
    lower_r: Decimal
    win_rate: Decimal

    @property
    def proven(self) -> bool:
        return self.level != "none"


NO_EVIDENCE = EdgeEstimate("none", 0, Decimal(0), Decimal(0), Decimal(0), Decimal(0))


@dataclass(frozen=True, slots=True)
class LedgerPolicy:
    """How much evidence a cell needs and how much a replayed trade counts."""

    minimum_effective_samples: Decimal = Decimal(20)
    replay_weight: Decimal = Decimal("0.5")
    z: Decimal = Decimal("1.645")

    def __post_init__(self) -> None:
        if not (self.minimum_effective_samples >= 5 and 0 < self.replay_weight <= 1 and self.z > 0):
            raise ValueError("ledger policy needs >= 5 samples, a replay weight in (0, 1] and z > 0")


@dataclass(slots=True)
class LearningLedger:
    outcomes: tuple[TradeOutcome, ...]
    policy: LedgerPolicy = field(default_factory=LedgerPolicy)

    def _estimate(self, rows: list[TradeOutcome], level: Level) -> EdgeEstimate | None:
        weights = [Decimal(1) if row.source == "LIVE" else self.policy.replay_weight for row in rows]
        total = sum(weights, Decimal(0))
        if total <= 0:
            return None
        # Evidence counts live trades fully and replays by their weight.
        effective = total
        if effective < self.policy.minimum_effective_samples:
            return None
        mean = sum((w * row.r_multiple for w, row in zip(weights, rows, strict=True)), Decimal(0)) / total
        variance = (
            sum((w * (row.r_multiple - mean) ** 2 for w, row in zip(weights, rows, strict=True)), Decimal(0))
            / total
        )
        standard_error = Decimal(math.sqrt(float(variance) / float(effective)))
        wins = sum((w for w, row in zip(weights, rows, strict=True) if row.r_multiple > 0), Decimal(0))
        return EdgeEstimate(
            level=level,
            samples=len(rows),
            effective_samples=effective.quantize(Decimal("0.01")),
            mean_r=mean.quantize(Decimal("0.0001")),
            lower_r=(mean - self.policy.z * standard_error).quantize(Decimal("0.0001")),
            win_rate=(wins / total).quantize(Decimal("0.0001")),
        )

    def estimate(self, strategy_id: str, symbol: str, regime: str) -> EdgeEstimate:
        """The most specific cell with enough evidence, else ``NO_EVIDENCE``."""
        own = [row for row in self.outcomes if row.strategy_id == strategy_id]
        for level, rows in (
            ("strategy_symbol_regime", [r for r in own if r.symbol == symbol and r.regime == regime]),
            ("strategy_regime", [r for r in own if r.regime == regime]),
            ("strategy", own),
        ):
            found = self._estimate(rows, level)  # type: ignore[arg-type]
            if found is not None:
                return found
        return NO_EVIDENCE

    def table(self) -> list[dict[str, Any]]:
        """Every strategy x symbol x regime cell with its own (possibly insufficient) evidence."""
        cells: dict[tuple[str, str, str], list[TradeOutcome]] = {}
        for row in self.outcomes:
            cells.setdefault((row.strategy_id, row.symbol, row.regime), []).append(row)
        result = []
        for (strategy, symbol, regime), rows in sorted(cells.items()):
            estimate = self._estimate(rows, "strategy_symbol_regime")
            result.append(
                {
                    "strategy_id": strategy,
                    "symbol": symbol,
                    "regime": regime,
                    "trades": len(rows),
                    "mean_r": None if estimate is None else str(estimate.mean_r),
                    "lower_r": None if estimate is None else str(estimate.lower_r),
                    "enough_evidence": estimate is not None,
                }
            )
        return result


def regime_at(allocations: Iterable[Mapping[str, Any]]) -> Callable[[int], str]:
    """The Macro regime in force at a time, from allocations with ``produced_at_ms`` and ``regime``."""
    timeline = sorted((int(item["produced_at_ms"]), str(item["regime"])) for item in allocations)

    def lookup(at_ms: int) -> str:
        current = UNKNOWN_REGIME
        for produced, regime in timeline:
            if produced > at_ms:
                break
            current = regime
        return current

    return lookup


def outcomes_from_execution_report(
    rows: Iterable[Mapping[str, Any]], *, regime_of: Callable[[int], str]
) -> tuple[TradeOutcome, ...]:
    """Live outcomes from ``kairos_execution.production_report`` rows."""
    result = []
    for row in rows:
        if row.get("version") != "kairos.execution-closed-trade.v1" or row.get("r_multiple") is None:
            continue
        result.append(
            TradeOutcome(
                strategy_id=str(row["strategy_id"]),
                symbol=str(row["symbol"]),
                regime=regime_of(int(row["decided_at_ms"])),
                r_multiple=Decimal(str(row["r_multiple"])),
                decided_at_ms=int(row["decided_at_ms"]),
                source="LIVE",
            )
        )
    return tuple(result)


__all__ = [
    "NO_EVIDENCE",
    "UNKNOWN_REGIME",
    "EdgeEstimate",
    "LearningLedger",
    "LedgerPolicy",
    "TradeOutcome",
    "outcomes_from_execution_report",
    "regime_at",
]
