"""Kairos Layer 5 — Risk Manager & Circuit Breaker.

Pure, deterministic safety logic (no LLM). It stands between the analytical
brain (Aggregator / Macro-Strategist) and the Execution Engine, and its single
job is to make sure a hallucinating or malfunctioning model can never blow up
the account.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .account import AccountState
from .circuit_breaker import CircuitBreaker, BreakerState
from .pipeline import RiskPipeline
from .config import RiskSettings

__all__ = ["AccountState", "CircuitBreaker", "BreakerState", "RiskPipeline", "RiskSettings", "__version__"]
