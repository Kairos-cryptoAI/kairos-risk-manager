"""Network-free, deterministic risk-policy evaluation harness."""

from __future__ import annotations

import math
from dataclasses import dataclass

from kairos_core.contracts import StrategicAllocation, TacticalCommand, ValidatedOrder
from kairos_core.enums import ReasonCode, SystemMode

from .account import AccountState
from .pipeline import RiskPipeline


@dataclass(frozen=True, slots=True)
class RiskCase:
    name: str
    command: TacticalCommand
    account: AccountState
    price: float
    allocation: StrategicAllocation | None = None
    system_mode: SystemMode = SystemMode.NORMAL


@dataclass(frozen=True, slots=True)
class RiskCaseResult:
    name: str
    decision: ValidatedOrder
    approved_notional_usd: float


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    cases: tuple[RiskCaseResult, ...]
    approved: int
    refused: int
    approved_notional_usd: float


def evaluate_policy(
    pipeline: RiskPipeline,
    cases: tuple[RiskCase, ...],
) -> RiskEvaluation:
    """Run an explicit policy matrix and verify core fail-closed invariants."""
    if not cases or len({case.name for case in cases}) != len(cases):
        raise ValueError("risk cases must be non-empty and uniquely named")
    output: list[RiskCaseResult] = []
    for case in cases:
        decision = pipeline.validate(
            case.command,
            case.account,
            price=case.price,
            allocation=case.allocation,
            system_mode=case.system_mode,
        )
        if not decision.approved:
            notional = 0.0
        else:
            intent_price = decision.intent.price or case.price
            notional = decision.intent.quantity * intent_price
            if not math.isfinite(notional) or notional <= 0:
                raise AssertionError(f"{case.name}: approved order has invalid notional")
            if decision.reason_code is ReasonCode.NO_TRADE:
                raise AssertionError(f"{case.name}: NO_TRADE decision cannot be approved")
            if case.system_mode is SystemMode.LOCAL_QUANT_MODE and not decision.intent.reduce_only:
                raise AssertionError(f"{case.name}: LOCAL_QUANT_MODE approved new risk")
        output.append(RiskCaseResult(case.name, decision, notional))
    approved = sum(item.decision.approved for item in output)
    return RiskEvaluation(
        cases=tuple(output),
        approved=approved,
        refused=len(output) - approved,
        approved_notional_usd=sum(item.approved_notional_usd for item in output),
    )
