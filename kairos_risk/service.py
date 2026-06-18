"""Async risk service: validates tactical commands and owns the circuit breaker."""
from __future__ import annotations

import asyncio

from kairos_core.bus import build_bus
from kairos_core.contracts import TacticalCommand
from kairos_core.contracts.base import KairosMessage
from kairos_core.enums import SystemMode
from kairos_core.logging import configure_logging, get_logger
from kairos_core.topics import Topics

from .account import AccountState
from .circuit_breaker import CircuitBreaker, CircuitBreakerRegistry
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
        self.account = AccountState(equity_usd=10_000, peak_equity_usd=10_000)
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

    async def run(self) -> None:
        configure_logging(self.settings.log_level, json_logs=self.settings.log_json,
                          service=self.settings.service_name)
        log.info("risk.start")
        async for env in self.bus.subscribe(Topics.TACTICAL_COMMAND, group="risk", consumer="c1"):
            try:
                cmd = TacticalCommand.model_validate(env.payload)
                # Placeholder mid-price; production reads it from the latest snapshot cache.
                price = env.payload.get("price") or 0.0
                if price <= 0:
                    log.debug("risk.skip_no_price", symbol=cmd.symbol)
                    continue
                validated = self.pipeline.validate(cmd, self.account, price=price)
                await self.bus.publish(Topics.VALIDATED_ORDER, validated)
                log.info("risk.validated", symbol=cmd.symbol, approved=validated.approved,
                        reason=validated.reason_code.value, adjustments=len(validated.adjustments))
                await self._broadcast_mode()
            finally:
                await self.bus.ack(Topics.TACTICAL_COMMAND, env, group="risk")


def main() -> None:
    asyncio.run(RiskService().run())


if __name__ == "__main__":
    main()
