from kairos_risk.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, BreakerState
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
    cb.record_failure()
    cb.record_failure()
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


def test_failed_half_open_probe_retrips_with_fresh_cooldown():
    cb = CircuitBreaker(max_consecutive_failures=2, cooldown_s=300)
    for _ in range(3):
        cb.record_failure(now=0.0)
    assert cb._state is BreakerState.OPEN
    # After the cooldown a failed probe must re-trip immediately...
    assert cb.record_failure(now=301.0) is BreakerState.OPEN
    # ...with a fresh cooldown: still OPEN well before 301 + 300.
    cb._maybe_half_open(now=500.0)
    assert cb._state is BreakerState.OPEN


def _trip(reg, model):
    # Trip with real (monotonic) time so the breaker stays OPEN within the cooldown.
    for _ in range(3):
        reg.record_failure(model)


def test_flash_down_enters_text_local_filter():
    reg = CircuitBreakerRegistry(max_consecutive_failures=2)
    _trip(reg, CircuitBreakerRegistry.FLASH)
    assert reg.system_mode is SystemMode.TEXT_LOCAL_FILTER


def test_gpt_down_enters_conflict_safe():
    reg = CircuitBreakerRegistry(max_consecutive_failures=2)
    _trip(reg, CircuitBreakerRegistry.GPT)
    assert reg.system_mode is SystemMode.CONFLICT_SAFE


def test_two_models_down_enters_local_quant_mode():
    reg = CircuitBreakerRegistry(max_consecutive_failures=2)
    _trip(reg, CircuitBreakerRegistry.FLASH)
    _trip(reg, CircuitBreakerRegistry.GPT)
    assert reg.system_mode is SystemMode.LOCAL_QUANT_MODE


def test_recovery_returns_to_normal():
    reg = CircuitBreakerRegistry(max_consecutive_failures=2)
    _trip(reg, CircuitBreakerRegistry.GPT)
    reg.record_success(CircuitBreakerRegistry.GPT)
    assert reg.system_mode is SystemMode.NORMAL
