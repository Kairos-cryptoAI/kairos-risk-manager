"""Risk limits (env prefix ``KAIROS_``)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from kairos_core.config import CoreSettings
from kairos_core.enums import EvedexProfile, TradingMode
from pydantic import Field, field_validator, model_validator

if TYPE_CHECKING:
    from kairos_core.contracts.regime_capability import RegimeCapabilityPolicyV1

PAPER_DEV_SYMBOL_MAP: Mapping[str, str] = MappingProxyType(
    {
        "BTCUSDT": "BTCUSD:DEV",
        "ETHUSDT": "ETHUSD:DEV",
        "SOLUSDT": "SOLUSD:DEV",
        "BNBUSDT": "BNBUSD:DEV",
        "XRPUSDT": "XRPUSD:DEV",
    }
)
PAPER_CANARY_STRATEGY_ID = "technical-canary"

REJECTED_PAPER_STRATEGY_IDS = frozenset(
    {
        "trend_breakout_v1",
        "trend_pullback_reclaim_v1",
        "range_mean_reversion_v1",
        "orderflow_volatility_expansion_v1",
        "regime_veto_retest_reclaim_v1",
    }
)


class RiskSettings(CoreSettings):
    service_name: str = "kairos-risk-manager"

    # Runtime authority. The legacy TacticalCommand path remains DRY_RUN-only;
    # PAPER has a separate strict CandidateReview -> RiskTradeDecision route.
    trading_mode: TradingMode = TradingMode.DRY_RUN
    # Deprecated compatibility input. False is never interpreted as authority.
    dry_run: bool | None = None
    evedex_profile: EvedexProfile = EvedexProfile.DEV

    # Leverage: anything above the hard limit is treated as a model error and
    # forcibly reduced to the safe cap (spec example 1: >10x -> 5x).
    hard_leverage_limit: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    safe_leverage_cap: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    max_allowed_leverage: float = Field(default=10.0, gt=0, allow_inf_nan=False)

    # Drawdown: above this daily loss, all *new* risk is refused (spec example 2).
    max_daily_drawdown_pct: float = Field(default=3.0, gt=0, le=100, allow_inf_nan=False)

    # Position sizing.
    # This is an equity allocation budget, amplified by approved leverage. It is
    # not stop-loss/VAR risk because TacticalCommand currently carries no stop.
    per_trade_risk_fraction: float = Field(default=0.02, gt=0, le=1, allow_inf_nan=False)
    max_position_notional_usd: float = Field(default=250_000.0, gt=0, allow_inf_nan=False)
    min_notional_usd: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    require_reconciled_account: bool = True
    account_snapshot_max_age_s: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    require_strategic_allocation: bool = True
    strategic_allocation_max_age_s: float = Field(default=26 * 60 * 60, gt=0, allow_inf_nan=False)

    # PAPER is fail-closed until a strategy revision is promoted explicitly.
    # The five current research sleeves remain hard-denied even if mistakenly
    # copied into this allowlist.
    paper_strategy_allowlist: list[str] = Field(default_factory=list)
    paper_account_id: str = "kairos-paper-dev-01"
    paper_operator_scope_file: Path | None = None
    paper_account_snapshot_max_age_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    paper_per_trade_risk_fraction: float = Field(
        default=0.0025,
        gt=0,
        le=0.0025,
        allow_inf_nan=False,
    )
    paper_max_total_open_risk_fraction: float = Field(
        default=0.01,
        gt=0,
        le=0.01,
        allow_inf_nan=False,
    )
    paper_max_leverage: float = Field(default=1.0, ge=1.0, le=5.0, allow_inf_nan=False)
    paper_max_position_notional_usd: float = Field(default=1_000.0, gt=0, allow_inf_nan=False)
    paper_min_notional_usd: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    paper_decision_cache_size: int = Field(default=10_000, ge=1)
    # No deployment opt-in is supplied by this release. Mapping never promotes
    # a strategy: the existing exact allowlist and hard-denied IDs still win.
    paper_regime_policy_profile: Literal["legacy-v1", "adaptive-research-v1"] = "legacy-v1"
    paper_regime_policy_file: Path | None = None
    paper_regime_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    paper_regime_source_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    paper_regime_macro_source: str = "kairos-macro-strategist"

    # Circuit breaker.
    breaker_max_consecutive_failures: int = Field(default=2, ge=0)
    breaker_cooldown_s: float = Field(default=300.0, gt=0, allow_inf_nan=False)

    @field_validator("paper_strategy_allowlist")
    @classmethod
    def validate_paper_strategy_allowlist(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or item != item.strip() or item.count("@") != 1:
                raise ValueError(
                    "paper strategy allowlist entries must be normalized '<strategy_id>@<revision>'"
                )
            strategy_id, revision = item.split("@", maxsplit=1)
            if not strategy_id or not revision:
                raise ValueError("paper strategy allowlist entries require strategy_id and revision")
            normalized.append(item)
        if len(normalized) != len(set(normalized)):
            raise ValueError("paper strategy allowlist entries must be unique")
        return sorted(normalized)

    @field_validator("paper_account_id")
    @classmethod
    def validate_paper_account_id(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("paper_account_id must be a normalized non-empty string")
        if value.casefold() in {"primary", "prod", "production", "live"}:
            raise ValueError("PAPER requires a dedicated non-production account_id")
        return value

    @model_validator(mode="after")
    def validate_limit_ordering(self) -> RiskSettings:
        if not self.safe_leverage_cap <= self.max_allowed_leverage <= self.hard_leverage_limit:
            raise ValueError(
                "leverage limits must satisfy safe_leverage_cap <= "
                "max_allowed_leverage <= hard_leverage_limit"
            )
        if self.min_notional_usd > self.max_position_notional_usd:
            raise ValueError("min_notional_usd cannot exceed max_position_notional_usd")
        if self.paper_min_notional_usd > self.paper_max_position_notional_usd:
            raise ValueError("paper_min_notional_usd cannot exceed paper_max_position_notional_usd")
        if self.dry_run is False:
            raise ValueError(
                "KAIROS_DRY_RUN=false is retired; set KAIROS_TRADING_MODE explicitly. "
                "It never enables PAPER or LIVE."
            )
        if self.dry_run is True and self.trading_mode is not TradingMode.DRY_RUN:
            raise ValueError("KAIROS_DRY_RUN=true conflicts with an explicit non-DRY_RUN mode")
        if self.trading_mode is TradingMode.LIVE:
            raise ValueError("LIVE is disabled: this release is not LIVE_READY")
        if self.trading_mode is TradingMode.PAPER:
            if self.evedex_profile is not EvedexProfile.DEV:
                raise ValueError("PAPER is restricted to the exact EVEDEX DEV profile")
            if self.environment.casefold() == "prod":
                raise ValueError("PAPER cannot run in a production Kairos environment")
            if self.bus_backend == "memory":
                raise ValueError("PAPER requires the durable Redis/PostgreSQL message path")
            if set(self.trading_symbols) != set(PAPER_DEV_SYMBOL_MAP):
                raise ValueError("PAPER requires the exact five-symbol DEV allowlist")
        regime_inputs = (
            self.paper_regime_policy_file,
            self.paper_regime_policy_sha256,
            self.paper_regime_source_set_sha256,
        )
        if self.paper_regime_policy_profile == "legacy-v1" and any(
            value is not None for value in regime_inputs
        ):
            raise ValueError("regime artifacts require an explicit adaptive-research-v1 opt-in")
        if self.paper_regime_policy_profile == "adaptive-research-v1":
            if self.trading_mode is not TradingMode.PAPER or any(value is None for value in regime_inputs):
                raise ValueError(
                    "adaptive regime policy requires PAPER and independently frozen file/hash/source set"
                )
            if (
                not self.paper_regime_macro_source
                or self.paper_regime_macro_source != self.paper_regime_macro_source.strip()
            ):
                raise ValueError("adaptive Macro source must be exact and normalized")
        return self

    def load_regime_policy(self) -> RegimeCapabilityPolicyV1 | None:
        if self.paper_regime_policy_profile == "legacy-v1":
            return None
        # Lazy import preserves the legacy profile with the previously pinned
        # Core release. Opt-in requires the new contract release explicitly.
        from kairos_core.contracts.regime_capability import RegimeCapabilityPolicyV1, validate_policy_binding

        if (
            self.paper_regime_policy_file is None
            or self.paper_regime_policy_sha256 is None
            or self.paper_regime_source_set_sha256 is None
        ):
            raise ValueError("adaptive regime policy requires independently frozen file/hash/source set")
        policy = RegimeCapabilityPolicyV1.model_validate_json(self.paper_regime_policy_file.read_bytes())
        allowed: set[tuple[str, str]] = set()
        for item in self.paper_strategy_allowlist:
            strategy_id, revision = item.split("@", maxsplit=1)
            if self.paper_strategy_allowed(strategy_id, revision) and strategy_id != PAPER_CANARY_STRATEGY_ID:
                allowed.add((strategy_id, revision))
        validate_policy_binding(
            policy,
            expected_sha256=self.paper_regime_policy_sha256,
            source_set_sha256=self.paper_regime_source_set_sha256,
            allowed_strategy_revisions=allowed,
        )
        return policy

    def paper_strategy_allowed(self, strategy_id: str, strategy_revision: str) -> bool:
        if strategy_id in REJECTED_PAPER_STRATEGY_IDS:
            return False
        return f"{strategy_id}@{strategy_revision}" in self.paper_strategy_allowlist
