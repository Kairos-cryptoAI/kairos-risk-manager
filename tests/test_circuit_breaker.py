from kairos_risk.circuit_breaker import CircuitBreaker, BreakerState
from kairos_core.enums import SystemMode


def test_trips_after_more_than_two_consecutive_failures():
    cb = CircuitBreaker(max_consecutive_failures=2, cooldown_s=300)
    assert cb.record_failure() is BreakerState.CLOSED   # 1
    assert cb.record_failure() is BreakerState.CLOSED   # 2
    assert cb.record_failure() is BreakerState.OPEN     # 3 -> trip
    assert cb.system_mode is SystemMode.LOCAL_QUANT_MODE
    assert cb.llm_allowed is False


def test_success_resets_the_streak():
    cb = CircuitBreaker(max_consecutive_failures=2)
    cb.record_failure(); cb.record_failure()
    cb.record_success()
    assert cb.record_failure() is BreakerState.CLOSED
    assert cb.record_failure() is BreakerState.CLOSED


def test_half_opens_after_cooldown():
    cb = CircuitBreaker(max_consecutive_failures=2, cooldown_s=300)
    for _ in range(3):
        cb.record_failure(now=0.0)
    assert cb._state is BreakerState.OPEN
    cb._maybe_half_open(now=301.0)
    assert cb._state is BreakerState.HALF_OPEN
    cb.record_success()
    assert cb.system_mode is SystemMode.NORMAL
