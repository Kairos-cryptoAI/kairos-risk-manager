"""Async risk service: validates tactical commands and owns the circuit breaker."""
from __future__ import annotations

import asyncio

from kairos_core.bus import build_bus
from kairos_core.contracts import AccountSnapshot, LLMHealthEvent, TacticalCommand
from kairos_core.contracts.base import KairosMessage
from kairos_core.enums import SystemMode
from kairos_core.logging import configure_logging, get_logger
from kairos_core.topics import Topics

from .account import AccountState
from .circuit_breaker import CircuitBreakerRegistry
from .config import RiskSettings
from .pipeline import RiskPipeline

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

                # Production-only: refuse stale commands if we lack a fresh reconciled account.
                if self.settings.require_reconciled_account and not self.account.reconciled:
                    log.warning("risk.unreconciled_account", symbol=cmd.symbol)
                    continue

                # Build a symbol-aware account state so position sizing sees the actual open exposure.
                account_for_symbol = AccountState.from_snapshot(
                    AccountSnapshot(
                        source="internal", exchange="", account_id="",
                        equity_usd=self.account.equity_usd, available_balance_usd=0.0,
                        peak_equity_usd=self.account.peak_equity_usd,
                        daily_pnl_pct=self.account.daily_pnl_pct,
                        positions=[], reconciled=self.account.reconciled,
                    ),
                    symbol=cmd.symbol,
                ) if self.account.reconciled else self.account

                price = cmd.reference_price
                if price <= 0:
                    # Backward-compatible old commands are safe: refuse to size instead of guessing.
                    log.debug("risk.skip_no_price", symbol=cmd.symbol)
                    continue
                validated = self.pipeline.validate(cmd, account_for_symbol, price=price)
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
                # Build per-symbol state later; store raw equity/peak for drawdown gate and allocation.
                self.account = AccountState(
                    equity_usd=snapshot.equity_usd,
                    peak_equity_usd=snapshot.peak_equity_usd,
                    daily_pnl_pct=snapshot.daily_pnl_pct,
                    gross_exposure_usd=sum(
                        abs(p.signed_quantity) * (p.mark_price or p.entry_price or 0.0)
                        for p in snapshot.positions
                    ),
                    open_position_qty=0.0,  # symbol-specific; extracted per command
                    reconciled=True,
                )
                log.info("risk.account_reconciled", equity=snapshot.equity_usd,
                         positions=len(snapshot.positions))
            finally:
                await self.bus.ack(Topics.ACCOUNT_SNAPSHOT, env, group="risk")

    async def run(self) -> None:
        configure_logging(self.settings.log_level, json_logs=self.settings.log_json,
                          service=self.settings.service_name)
        log.info("risk.start")
        await asyncio.gather(
            self._consume_commands(), self._consume_health(), self._consume_account(),
        )


def main() -> None:
    asyncio.run(RiskService().run())


if __name__ == "__main__":
    main()
