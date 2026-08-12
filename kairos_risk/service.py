"""Async risk service: validates tactical commands and owns the circuit breaker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from kairos_core.bus import BusEnvelope, build_bus
from kairos_core.contracts import (
    AccountSnapshot,
    LLMHealthEvent,
    StrategicAllocation,
    TacticalCommand,
)
from kairos_core.contracts.base import KairosMessage
from kairos_core.enums import ReasonCode, SystemMode
from kairos_core.logging import configure_logging, get_logger
from kairos_core.topics import Topics

from .account import AccountState
from .circuit_breaker import CircuitBreakerRegistry
from .config import RiskSettings
from .pipeline import RiskPipeline
from .strategy import is_fresh

log = get_logger("risk")

_NON_ENTRY_REASONS = {
    ReasonCode.CLOSE_POSITION,
    ReasonCode.HOLD,
    ReasonCode.NO_TRADE,
    ReasonCode.REDUCE_LEVERAGE,
}


class _Control(KairosMessage):
    mode: SystemMode
    detail: str = ""


class RiskService:
    def __init__(self, settings: RiskSettings | None = None) -> None:
        self.settings = settings or RiskSettings()
        self.bus = build_bus(self.settings)
        self.pipeline = RiskPipeline(self.settings)
        self.breakers = CircuitBreakerRegistry(
            self.settings.breaker_max_consecutive_failures,
            self.settings.breaker_cooldown_s,
        )
        # This fallback is reachable only when the explicit dev/test escape hatch
        # require_reconciled_account=False is configured and Execution has not reported
        # a reconciliation failure. Production stays fail-closed.
        self.account = AccountState(equity_usd=10_000, peak_equity_usd=10_000, reconciled=False)
        self.account_snapshot: AccountSnapshot | None = None
        self._latest_account_captured_at: datetime | None = None
        self.strategic_allocation: StrategicAllocation | None = None
        self._last_mode = SystemMode.NORMAL

    async def _broadcast_mode(self) -> None:
        mode = self.breakers.system_mode
        if mode == self._last_mode:
            return
        await self.bus.publish(
            Topics.SYSTEM_CONTROL,
            _Control(
                source=self.settings.service_name,
                mode=mode,
                detail="per-model circuit breaker",
            ),
        )
        # Advance only after publish succeeds, so a transient bus failure remains retryable.
        self._last_mode = mode
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

    def _account_ready(self, *, symbol: str, now: datetime | None = None) -> bool:
        if self.account_snapshot is None and self._latest_account_captured_at is not None:
            # An explicit failure from Execution always wins, even if a developer
            # disabled the startup snapshot requirement for a network-free demo.
            log.warning("risk.account_reconciliation_lost", symbol=symbol)
            return False
        if not self.settings.require_reconciled_account:
            return True
        snapshot = self.account_snapshot
        if snapshot is None or not snapshot.reconciled:
            log.warning("risk.unreconciled_account", symbol=symbol)
            return False
        current_time = now or datetime.now(UTC)
        age_s = (current_time - snapshot.captured_at).total_seconds()
        if age_s < 0 or age_s > self.settings.account_snapshot_max_age_s:
            log.warning("risk.stale_account", symbol=symbol, age_s=round(age_s, 3))
            return False
        return True

    async def _handle_command(self, env: BusEnvelope) -> None:
        command = TacticalCommand.model_validate(env.payload)
        if not self.settings.symbol_allowed(command.symbol):
            log.warning("risk.symbol_rejected", symbol=command.symbol)
            await self._publish_refusal(command, "symbol is not allowed")
            return
        if not self._account_ready(symbol=command.symbol):
            await self._publish_refusal(command, "authoritative account reconciliation unavailable")
            return

        account_for_symbol = (
            AccountState.from_snapshot(self.account_snapshot, symbol=command.symbol)
            if self.account_snapshot is not None
            else self.account
        )
        system_mode = self.breakers.system_mode

        # Strategic allocation constrains new entries. It is unnecessary for a
        # LOCAL_QUANT_MODE refusal and must never prevent reduce-only exits.
        allocation = None
        is_new_risk = command.reason_code not in _NON_ENTRY_REASONS
        if (
            self.settings.require_strategic_allocation
            and is_new_risk
            and system_mode is not SystemMode.LOCAL_QUANT_MODE
        ):
            if self.strategic_allocation is None:
                log.warning("risk.no_allocation", symbol=command.symbol)
                await self._publish_refusal(
                    command,
                    "required strategic allocation unavailable",
                    account=account_for_symbol,
                )
                return
            if not is_fresh(
                self.strategic_allocation,
                max_age_s=self.settings.strategic_allocation_max_age_s,
            ):
                log.warning("risk.stale_allocation", symbol=command.symbol)
                await self._publish_refusal(
                    command,
                    "required strategic allocation is stale",
                    account=account_for_symbol,
                )
                return
            allocation = self.strategic_allocation

        price = command.reference_price
        if price <= 0:
            # Backward-compatible old commands are safe: refuse to size instead of guessing.
            log.debug("risk.skip_no_price", symbol=command.symbol)
            await self._publish_refusal(
                command,
                "positive reference price required for deterministic sizing",
                account=account_for_symbol,
            )
            return
        validated = self.pipeline.validate(
            command,
            account_for_symbol,
            price=price,
            allocation=allocation,
            system_mode=system_mode,
        )
        await self.bus.publish(Topics.VALIDATED_ORDER, validated)
        log.info(
            "risk.validated",
            symbol=command.symbol,
            approved=validated.approved,
            reason=validated.reason_code.value,
            adjustments=len(validated.adjustments),
        )

    async def _publish_refusal(
        self,
        command: TacticalCommand,
        adjustment: str,
        *,
        account: AccountState | None = None,
    ) -> None:
        refused = self.pipeline.refuse(command, adjustment=adjustment, account=account)
        await self.bus.publish(Topics.VALIDATED_ORDER, refused)
        log.info(
            "risk.refused",
            symbol=command.symbol,
            reason=adjustment,
        )

    async def _consume_commands(self) -> None:
        async for env in self.bus.subscribe(Topics.TACTICAL_COMMAND, group="risk", consumer="commands"):
            try:
                await self._handle_command(env)
                await self.bus.ack(Topics.TACTICAL_COMMAND, env, group="risk")
            except Exception:
                log.exception("risk.command_processing_failed", envelope_id=env.id)

    async def _handle_health(self, env: BusEnvelope) -> None:
        event = LLMHealthEvent.model_validate(env.payload)
        mode = self.apply_health_event(
            model=event.model,
            ok=event.ok,
            kind=event.kind,
        )
        log.debug(
            "risk.llm_health",
            model=event.model,
            ok=event.ok,
            kind=event.kind,
            mode=mode.value,
        )
        await self._broadcast_mode()

    async def _consume_health(self) -> None:
        """Drive the per-model breakers from LLM health signals on the bus."""
        async for env in self.bus.subscribe(Topics.LLM_HEALTH, group="risk", consumer="health"):
            try:
                await self._handle_health(env)
                await self.bus.ack(Topics.LLM_HEALTH, env, group="risk")
            except Exception:
                log.exception("risk.health_processing_failed", envelope_id=env.id)

    def _handle_account(self, env: BusEnvelope) -> None:
        """Apply one full exchange snapshot without weakening reconciliation."""
        snapshot = AccountSnapshot.model_validate(env.payload)
        if snapshot.captured_at.utcoffset() is None:
            raise ValueError("account snapshot captured_at must be timezone-aware")
        if any(position.captured_at.utcoffset() is None for position in snapshot.positions):
            raise ValueError("account snapshot position captured_at values must be timezone-aware")

        latest_captured_at = self._latest_account_captured_at
        if latest_captured_at is not None and snapshot.captured_at < latest_captured_at:
            log.warning(
                "risk.out_of_order_account_snapshot",
                exchange=snapshot.exchange,
                captured_at=snapshot.captured_at.isoformat(),
            )
            return

        if latest_captured_at is not None and snapshot.captured_at == latest_captured_at:
            # Reconciliation failure wins for a duplicated snapshot version. Once a
            # version is marked untrusted, a same-version success cannot reopen the gate.
            if not snapshot.reconciled and self.account_snapshot is not None:
                self.account_snapshot = None
                log.warning(
                    "risk.account_reconciliation_lost",
                    exchange=snapshot.exchange,
                    detail=snapshot.reconciliation_detail,
                )
            return

        if not snapshot.reconciled:
            # A newer reconciliation failure revokes the previously trusted view.
            self._latest_account_captured_at = snapshot.captured_at
            self.account_snapshot = None
            log.warning(
                "risk.account_reconciliation_lost",
                exchange=snapshot.exchange,
                detail=snapshot.reconciliation_detail,
            )
            return

        mismatched_positions = [
            position.symbol
            for position in snapshot.positions
            if position.exchange != snapshot.exchange or position.account_id != snapshot.account_id
        ]
        if mismatched_positions:
            raise ValueError(
                "account snapshot contains positions for a different exchange/account: "
                + ", ".join(mismatched_positions)
            )

        self._latest_account_captured_at = snapshot.captured_at
        self.account_snapshot = snapshot
        self.account = AccountState.from_snapshot(snapshot)
        log.info(
            "risk.account_reconciled",
            equity=snapshot.equity_usd,
            positions=len(snapshot.positions),
        )

    async def _consume_account(self) -> None:
        """Receive authoritative full-account snapshots from Execution."""
        async for env in self.bus.subscribe(Topics.ACCOUNT_SNAPSHOT, group="risk", consumer="account"):
            try:
                self._handle_account(env)
                await self.bus.ack(Topics.ACCOUNT_SNAPSHOT, env, group="risk")
            except Exception:
                log.exception("risk.account_processing_failed", envelope_id=env.id)

    def _handle_allocation(self, env: BusEnvelope) -> None:
        allocation = StrategicAllocation.model_validate(env.payload)
        self.strategic_allocation = allocation
        log.info(
            "risk.allocation_updated",
            regime=allocation.regime.value,
            max_leverage=allocation.max_gross_leverage,
            stable=allocation.stable_reserve_pct,
        )

    async def _consume_allocation(self) -> None:
        """Receive strategic allocations; update internal constraint state."""
        async for env in self.bus.subscribe(
            Topics.STRATEGIC_ALLOCATION,
            group="risk",
            consumer="allocation",
        ):
            try:
                self._handle_allocation(env)
                await self.bus.ack(Topics.STRATEGIC_ALLOCATION, env, group="risk")
            except Exception:
                log.exception("risk.allocation_processing_failed", envelope_id=env.id)

    async def close(self) -> None:
        await self.bus.close()

    async def run(self) -> None:
        try:
            configure_logging(
                self.settings.log_level,
                json_logs=self.settings.log_json,
                service=self.settings.service_name,
            )
            log.info("risk.start")
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._consume_commands(), name="tactical-commands")
                tasks.create_task(self._consume_health(), name="llm-health")
                tasks.create_task(self._consume_account(), name="account-snapshots")
                tasks.create_task(self._consume_allocation(), name="strategic-allocation")
        finally:
            await self.close()


def main() -> None:
    asyncio.run(RiskService().run())


if __name__ == "__main__":
    main()
