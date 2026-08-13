"""Circuit breaker that detaches the LLM when the model API is unstable.

Spec: if the LLM API returns 502 / times out more than twice in a row, cut the
LLM out for 5 minutes and fall back to LOCAL_QUANT_MODE (local stop-loss
scripts manage open positions in the meantime).
"""

from __future__ import annotations

import time
from enum import StrEnum

from kairos_core.enums import SystemMode


class BreakerState(StrEnum):
    CLOSED = "CLOSED"  # healthy, LLM attached
    OPEN = "OPEN"  # tripped, LLM detached (LOCAL_QUANT_MODE)
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
        self._maybe_half_open(now)
        # A failed probe while HALF_OPEN re-trips immediately (fresh cooldown).
        if self._state is BreakerState.HALF_OPEN:
            self._trip(now)
            return self._state
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
        # HALF_OPEN is still degraded until an explicit successful health event
        # closes the breaker. This prevents risk from reopening during a probe.
        return self.state is BreakerState.CLOSED


class CircuitBreakerRegistry:
    """Model and provider breakers collapsed into one fail-safe ``SystemMode``.

    The model mapping preserves the narrowest safe degradation while still failing
    closed for an unavailable hot path, multiple model outages, an unknown model, or
    an aggregated OpenAI provider outage.
    """

    FLASH = "deepseek-v4-flash"
    LUNA = "gpt-5.6-luna"
    TERRA = "gpt-5.6-terra"
    SOL = "gpt-5.6-sol"
    OPENAI = "openai"

    # Backwards-compatible name used by older callers and tests.
    GPT = SOL

    KNOWN_MODELS = frozenset((FLASH, LUNA, TERRA, SOL))
    OPENAI_MODELS = frozenset((LUNA, TERRA, SOL))

    def __init__(self, max_consecutive_failures: int = 2, cooldown_s: float = 300.0) -> None:
        self._max = max_consecutive_failures
        self._cooldown = cooldown_s
        self._breakers: dict[str, CircuitBreaker] = {}
        self._provider_breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, model: str) -> CircuitBreaker:
        return self._breakers.setdefault(model, CircuitBreaker(self._max, self._cooldown))

    def provider_breaker(self, provider: str) -> CircuitBreaker:
        provider = self.normalize_provider(provider)
        return self._provider_breakers.setdefault(
            provider,
            CircuitBreaker(self._max, self._cooldown),
        )

    @staticmethod
    def normalize_provider(provider: str) -> str:
        return provider.strip().lower()

    @classmethod
    def infer_provider(cls, model: str) -> str | None:
        if model in cls.OPENAI_MODELS or model.startswith("gpt-"):
            return cls.OPENAI
        if model == cls.FLASH or model.startswith("deepseek-"):
            return "deepseek"
        return None

    def record_failure(self, model: str, *, now: float | None = None) -> BreakerState:
        return self.breaker(model).record_failure(now=now)

    def record_success(self, model: str) -> BreakerState:
        return self.breaker(model).record_success()

    def record_provider_failure(
        self,
        provider: str,
        *,
        now: float | None = None,
    ) -> BreakerState:
        return self.provider_breaker(provider).record_failure(now=now)

    def record_provider_success(self, provider: str) -> BreakerState:
        return self.provider_breaker(provider).record_success()

    def is_down(self, model: str) -> bool:
        return not self.breaker(model).llm_allowed

    def is_provider_down(self, provider: str) -> bool:
        return not self.provider_breaker(provider).llm_allowed

    @property
    def system_mode(self) -> SystemMode:
        if self.is_provider_down(self.OPENAI):
            return SystemMode.LOCAL_QUANT_MODE

        down_models = {model for model, breaker in self._breakers.items() if not breaker.llm_allowed}
        if len(down_models) >= 2 or self.LUNA in down_models:
            return SystemMode.LOCAL_QUANT_MODE
        if down_models & {self.TERRA, self.SOL}:
            return SystemMode.CONFLICT_SAFE
        if self.FLASH in down_models:
            return SystemMode.TEXT_LOCAL_FILTER
        if down_models - self.KNOWN_MODELS:
            # A newly introduced or misspelled model must not silently bypass risk
            # degradation merely because this package does not know its role yet.
            return SystemMode.LOCAL_QUANT_MODE
        return SystemMode.NORMAL
