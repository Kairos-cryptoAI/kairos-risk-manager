"""Strategic allocation limit tests (regime forbids, exposure cap, freshness)."""
from datetime import datetime, timedelta, timezone

from kairos_core.contracts import StrategicAllocation
from kairos_core.enums import MarketRegime, ReasonCode, StrategicTrigger

from kairos_risk.strategy import is_fresh, limits_for


def _alloc(regime=MarketRegime.BULL, stable=0.2, max_lev=2.0, age_s=0.0):
    return StrategicAllocation(
        source="macro", regime=regime, stable_reserve_pct=stable,
        max_gross_leverage=max_lev, triggered_by=StrategicTrigger.SCHEDULE,
        produced_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
    )


def test_bull_regime_forbids_new_short_trends():
    lim = limits_for(
        _alloc(regime=MarketRegime.BULL), reason=ReasonCode.ENTER_SHORT_TREND,
        equity_usd=10_000, gross_exposure_usd=0,
    )
    assert lim.allowed is False
    assert any("BULL forbids new short" in note for note in lim.notes)


def test_bear_regime_forbids_new_long_trends():
    lim = limits_for(
        _alloc(regime=MarketRegime.BEAR), reason=ReasonCode.ENTER_LONG_TREND,
        equity_usd=10_000, gross_exposure_usd=0,
    )
    assert lim.allowed is False
    assert any("BEAR forbids new long" in note for note in lim.notes)


def test_chop_regime_forbids_any_trend_entry():
    for reason in [ReasonCode.ENTER_LONG_TREND, ReasonCode.ENTER_SHORT_TREND]:
        lim = limits_for(
            _alloc(regime=MarketRegime.CHOP), reason=reason,
            equity_usd=10_000, gross_exposure_usd=0,
        )
        assert lim.allowed is False
        assert any("CHOP forbids new trend" in note for note in lim.notes)


def test_stable_reserve_reduces_available_equity():
    lim = limits_for(
        _alloc(stable=0.4), reason=ReasonCode.ENTER_LONG_TREND,
        equity_usd=10_000, gross_exposure_usd=0,
    )
    assert lim.available_equity_usd == 6_000  # 10k * (1 - 0.4)


def test_max_gross_leverage_cap_enforced():
    lim = limits_for(
        _alloc(max_lev=1.5, stable=0.3), reason=ReasonCode.ENTER_LONG_TREND,
        equity_usd=10_000, gross_exposure_usd=8_000,
    )
    # gross_cap = 10_000 * 1.5 * (1 - 0.3) = 10,500
    assert lim.remaining_gross_notional_usd == 2_500  # 10,500 - 8,000


def test_exposure_exhaustion_blocks_new_entry():
    lim = limits_for(
        _alloc(max_lev=2.0, stable=0.2), reason=ReasonCode.ENTER_LONG_TREND,
        equity_usd=10_000, gross_exposure_usd=16_000,
    )
    assert lim.allowed is False
    assert any("cap exhausted" in note for note in lim.notes)


def test_fresh_allocation_within_max_age():
    alloc = _alloc(age_s=60)
    assert is_fresh(alloc, max_age_s=120) is True


def test_stale_allocation_exceeds_max_age():
    alloc = _alloc(age_s=200)
    assert is_fresh(alloc, max_age_s=120) is False
