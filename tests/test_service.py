"""LLM health events on the bus must drive the per-model circuit breakers."""
from kairos_risk.config import RiskSettings
from kairos_risk.service import RiskService
from kairos_core.enums import SystemMode


def _svc() -> RiskService:
    return RiskService(RiskSettings(bus_backend="memory"))


def test_gpt_outage_drives_conflict_safe():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="5xx")
    assert svc.breakers.system_mode is SystemMode.CONFLICT_SAFE


def test_flash_outage_drives_text_local_filter():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="deepseek-v4-flash", ok=False, kind="timeout")
    assert svc.breakers.system_mode is SystemMode.TEXT_LOCAL_FILTER


def test_two_outages_drive_local_quant_mode():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="deepseek-v4-flash", ok=False, kind="5xx")
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="5xx")
    assert svc.breakers.system_mode is SystemMode.LOCAL_QUANT_MODE


def test_healthy_signal_recovers_to_normal():
    svc = _svc()
    for _ in range(3):
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="5xx")
    assert svc.breakers.system_mode is SystemMode.CONFLICT_SAFE
    svc.apply_health_event(model="gpt-5.5", ok=True)
    assert svc.breakers.system_mode is SystemMode.NORMAL


def test_bad_output_does_not_trip_breaker():
    svc = _svc()
    for _ in range(5):
        svc.apply_health_event(model="gpt-5.5", ok=False, kind="error")  # API answered
    assert svc.breakers.system_mode is SystemMode.NORMAL
