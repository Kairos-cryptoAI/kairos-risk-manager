"""Async risk service: validates tactical commands and owns the circuit breaker."""
from __future__ import annotations

import asyncio

from kairos_core.bus import build_bus
from kairos_core.contracts import (
    AccountSnapshot,
    LLMHealthEvent,
    StrategicAllocation,
    TacticalCommand,
)
from kairos_core.contracts.base import KairosMessage
from kairos_core.enums import SystemMode
from kairos_core.logging import configure_logging, get_logger
from kairos_core.topics import Topics

from .account import AccountState
from .circuit_breaker import CircuitBreakerRegistry
from .config import RiskSettings
from .pipeline import RiskPipeline
from .strategy import is_fresh

log = get_logger("risk")


class _Control(KairosMessage):
    mode: SystemMode
    detail: str = ""


class RiskService:
    def __init__(self, settings: RiskSettings | None = None) -> None:
        self.settings = settings or RiskSettings()
        self.bus = build_bus(self.settings)
        self.pipeline = RiskPipeline(self.settings)
        self.breakers = CircuitBreakerRegistry(
            self.settings.breaker_max_consecutive_failures, self.settings.breaker_cooldown_s
        )
        # In a real deployment this is hydrated from the Execution Engine / exchange.
        self.account = AccountState(equity_usd=10_000, peak_equity_usd=10_000, reconciled=False)
        self.account_snapshot: AccountSnapshot | None = None
        self.strategic_allocation: StrategicAllocation | None = None
        self._last_account_ts = 0.0
        self._last_mode = SystemMode.NORMAL

    async def _broadcast_mode(self) -> None:
        mode = self.breakers.system_mode
        if mode != self._last_mode:
            self._last_mode = mode
            await self.bus.publish(Topics.SYSTEM_CONTROL,
                                  _Control(source=self.settings.service_name, mode=mode,
                                           detail="per-model circuit breaker"))
            log.warning("risk.mode_change", mode=mode.value)

    def record_llm_failure(self, model: str) -> None:
        """Feed an LLM health signal (5xx/timeout) into the per-model breaker."""
        self.breakers.record_failure(model)

    def record_llm_success(self, model: str) -> None:
        self.breakers.record_success(model)

    def apply_health_event(self, *, model: str, ok: bool, kind: str = "ok") -> SystemMode:
        """Feed one LLM health signal into the per-model breakers; returns the mode.

        Only API-level instability (5xx / timeout) trips a breaker; a healthy call
        resets it. Bad-output / 4xx signals are ignored (the API answered).
        """
        if ok:
            self.breakers.record_success(model)
        elif kind in ("5xx", "timeout"):
            self.breakers.record_failure(model)
        return self.breakers.system_mode

    async def _consume_commands(self) -> None:
        async for env in self.bus.subscribe(Topics.TACTICAL_COMMAND, group="risk", consumer="commands"):
            try:
                cmd = TacticalCommand.model_validate(env.payload)
                if not self.settings.symbol_allowed(cmd.symbol):
                    log.warning("risk.symbol_rejected", symbol=cmd.symbol)
                    continue

                # Production-only: refuse commands until an authoritative snapshot is available.
                if self.settings.require_reconciled_account and self.account_snapshot is None:
                    log.warning("risk.unreconciled_account", symbol=cmd.symbol)
                    continue

                # Extract the signed position for this command's symbol from the full snapshot.
                account_for_symbol = (
                    AccountState.from_snapshot(self.account_snapshot, symbol=cmd.symbol)
                    if self.account_snapshot is not None else self.account
                )

                # Strategic allocation applies only to new entries; reduce-only exits remain allowed.
                allocation = None
                if self.settings.require_strategic_allocation and cmd.reason_code.value not in {
                    "CLOSE_POSITION", "HOLD", "NO_TRADE", "REDUCE_LEVERAGE",
                }:
                    if self.strategic_allocation is None:
                        log.warning("risk.no_allocation", symbol=cmd.symbol)
                        continue
                    if not is_fresh(
                        self.strategic_allocation, max_age_s=self.settings.strategic_allocation_max_age_s,
                    ):
                        log.warning("risk.stale_allocation", symbol=cmd.symbol)
                        continue
                    allocation = self.strategic_allocation

                price = cmd.reference_price
                if price <= 0:
                    # Backward-compatible old commands are safe: refuse to size instead of guessing.
                    log.debug("risk.skip_no_price", symbol=cmd.symbol)
                    continue
                validated = self.pipeline.validate(
                    cmd, account_for_symbol, price=price, allocation=allocation,
                )
                await self.bus.publish(Topics.VALIDATED_ORDER, validated)
                log.info("risk.validated", symbol=cmd.symbol, approved=validated.approved,
                        reason=validated.reason_code.value, adjustments=len(validated.adjustments))
                await self._broadcast_mode()
            finally:
                await self.bus.ack(Topics.TACTICAL_COMMAND, env, group="risk")

    async def _consume_health(self) -> None:
        """Drive the per-model breakers from LLM health signals on the bus."""
        async for env in self.bus.subscribe(Topics.LLM_HEALTH, group="risk", consumer="health"):
            try:
                ev = LLMHealthEvent.model_validate(env.payload)
                mode = self.apply_health_event(model=ev.model, ok=ev.ok, kind=ev.kind)
                log.debug("risk.llm_health", model=ev.model, ok=ev.ok, kind=ev.kind, mode=mode.value)
                await self._broadcast_mode()
            finally:
                await self.bus.ack(Topics.LLM_HEALTH, env, group="risk")

    async def _consume_account(self) -> None:
        """Receive reconciled account snapshots; update internal state atomically."""
        async for env in self.bus.subscribe(Topics.ACCOUNT_SNAPSHOT, group="risk", consumer="account"):
            try:
                snapshot = AccountSnapshot.model_validate(env.payload)
                if not snapshot.reconciled:
                    log.debug("risk.skip_unreconciled_snapshot", exchange=snapshot.exchange)
                    continue
                import time
                self._last_account_ts = time.time()
                self.account_snapshot = snapshot
                self.account = AccountState.from_snapshot(snapshot)
                log.info("risk.account_reconciled", equity=snapshot.equity_usd,
                         positions=len(snapshot.positions))
            finally:
                await self.bus.ack(Topics.ACCOUNT_SNAPSHOT, env, group="risk")

    async def _consume_allocation(self) -> None:
        """Receive strategic allocations; update internal constraint state."""
        async for env in self.bus.subscribe(Topics.STRATEGIC_ALLOCATION, group="risk", consumer="allocation"):
            try:
                allocation = StrategicAllocation.model_validate(env.payload)
                self.strategic_allocation = allocation
                log.info("risk.allocation_updated", regime=allocation.regime.value,
                         max_leverage=allocation.max_gross_leverage, stable=allocation.stable_reserve_pct)
            finally:
                await self.bus.ack(Topics.STRATEGIC_ALLOCATION, env, group="risk")

    async def run(self) -> None:
        configure_logging(self.settings.log_level, json_logs=self.settings.log_json,
                          service=self.settings.service_name)
        log.info("risk.start")
        await asyncio.gather(
            self._consume_commands(), self._consume_health(), self._consume_account(),
            self._consume_allocation(),
        )


def main() -> None:
    asyncio.run(RiskService().run())


if __name__ == "__main__":
    main()
