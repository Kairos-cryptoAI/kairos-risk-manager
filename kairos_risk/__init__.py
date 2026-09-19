"""Kairos Layer 5 — Risk Manager & Circuit Breaker.

Pure, deterministic safety logic (no LLM). It stands between the analytical
brain (Aggregator / Macro-Strategist) and the Execution Engine, and its single
job is to make sure a hallucinating or malfunctioning model can never blow up
the account.
"""

from __future__ import annotations

__version__ = "0.2.0"

from .account import AccountState
from .circuit_breaker import BreakerState, CircuitBreaker, CircuitBreakerRegistry
from .config import RiskSettings
from .evaluation import RiskCase, RiskEvaluation, evaluate_policy
from .paper import PaperReservations, PaperRiskPipeline
from .paper_runtime import PaperInputDeadlineExceeded, PaperInputUnavailable, PaperRiskCoordinator
from .pipeline import RiskPipeline
from .simulation import SimulationRiskPolicy

__all__ = [
    "AccountState",
    "CircuitBreaker",
    "BreakerState",
    "CircuitBreakerRegistry",
    "RiskPipeline",
    "SimulationRiskPolicy",
    "PaperRiskPipeline",
    "PaperReservations",
    "PaperRiskCoordinator",
    "PaperInputUnavailable",
    "PaperInputDeadlineExceeded",
    "RiskCase",
    "RiskEvaluation",
    "RiskSettings",
    "evaluate_policy",
    "__version__",
]
