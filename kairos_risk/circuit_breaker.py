"""Circuit breaker that detaches the LLM when the model API is unstable.

Spec: if the LLM API returns 502 / times out more than twice in a row, cut the
LLM out for 5 minutes and fall back to LOCAL_QUANT_MODE (local stop-loss
scripts manage open positions in the meantime).
"""
from __future__ import annotations

import time
from enum import Enum

from kairos_core.enums import SystemMode


class BreakerState(str, Enum):
    CLOSED = "CLOSED"      # healthy, LLM attached
    OPEN = "OPEN"          # tripped, LLM detached (LOCAL_QUANT_MODE)
    HALF_OPEN = "HALF_OPEN"  # cooldown elapsed, probing recovery


class CircuitBreaker:
    def __init__(self, max_consecutive_failures: int = 2, cooldown_s: float = 300.0) -> None:
        self.max_consecutive_failures = max_consecutive_failures
        self.cooldown_s = cooldown_s
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> BreakerState:
        self._maybe_half_open()
        return self._state

    @property
    def system_mode(self) -> SystemMode:
        return SystemMode.NORMAL if self.state is BreakerState.CLOSED else SystemMode.LOCAL_QUANT_MODE

    def _maybe_half_open(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        if self._state is BreakerState.OPEN and self._opened_at is not None:
            if now - self._opened_at >= self.cooldown_s:
                self._state = BreakerState.HALF_OPEN

    def record_failure(self, *, now: float | None = None) -> BreakerState:
        now = now if now is not None else time.monotonic()
        self._consecutive_failures += 1
        if self._consecutive_failures > self.max_consecutive_failures:
            self._trip(now)
        return self._state

    def record_success(self) -> BreakerState:
        self._consecutive_failures = 0
        # A success in HALF_OPEN (or CLOSED) closes the breaker.
        self._state = BreakerState.CLOSED
        self._opened_at = None
        return self._state

    def _trip(self, now: float) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = now

    @property
    def llm_allowed(self) -> bool:
        return self.state is not BreakerState.OPEN
