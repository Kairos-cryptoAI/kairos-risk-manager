"""Async risk service: validates tactical commands and owns the circuit breaker."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from kairos_core.bus import BusEnvelope, build_bus
from kairos_core.contracts import (
    AccountSnapshot,
    AccountSnapshotV2,
    CandidateReviewV1,
    LLMHealthEvent,
    RiskTradeDecisionV1,
    StrategicAllocation,
    TacticalCommand,
    VenueQualityV1,
)
from kairos_core.contracts.base import KairosMessage
from kairos_core.enums import ReasonCode, SystemMode, TradingMode
from kairos_core.logging import configure_logging, get_logger
from kairos_core.topics import Topics
from kairos_persistence import DurableMessageBus

from .account import AccountState
from .canary_authorization import (
    CanaryAuthorizationError,
    PaperCanaryArmRepository,
    RejectAllPaperCanaryArmRepository,
    allocation_from_consumed_arm,
    build_persistence_canary_repository,
)
from .circuit_breaker import CircuitBreakerRegistry
from .config import PAPER_CANARY_STRATEGY_ID, RiskSettings
from .paper import PaperRiskPipeline
from .paper_runtime import (
    PaperInputDeadlineExceeded,
    PaperInputUnavailable,
    PaperRiskCoordinator,
)
from .pipeline import RiskPipeline
from .strategy import allocation_error, is_fresh

log = get_logger("risk")

_NON_ENTRY_REASONS = {
    ReasonCode.CLOSE_POSITION,
    ReasonCode.HOLD,
    ReasonCode.NO_TRADE,
    ReasonCode.REDUCE_LEVERAGE,
}

_MODEL_OUTAGE_KINDS = frozenset(("5xx", "timeout", "connection", "rate_limit"))
_PROVIDER_OUTAGE_KINDS = frozenset(("connection", "rate_limit", "provider_outage"))


class _Control(KairosMessage):
    mode: SystemMode
    detail: str = ""


class RiskService:
    def __init__(
        self,
        settings: RiskSettings | None = None,
        *,
        paper_canary_repository: PaperCanaryArmRepository | None = None,
    ) -> None:
        self.settings = settings or RiskSettings()
        transport = build_bus(self.settings)
        self.bus = (
            transport
            if self.settings.bus_backend == "memory"
            else DurableMessageBus(transport, service_name=self.settings.service_name)
        )
        self.pipeline = RiskPipeline(self.settings)
        self.paper_pipeline = PaperRiskPipeline(self.settings)
        self.paper = PaperRiskCoordinator(self.settings, self.paper_pipeline)
        self.paper_canary_repository = paper_canary_repository or RejectAllPaperCanaryArmRepository()
        self._paper_canary_repository_explicit = paper_canary_repository is not None
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
        self._latest_allocation_produced_at: datetime | None = None
        self._last_mode = SystemMode.NORMAL

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000

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
        """Feed an outage into one model breaker (legacy helper)."""
        self.breakers.record_failure(model)

    def record_llm_success(self, model: str, provider: str | None = None) -> None:
        """Recover a model and, when known, its aggregate provider breaker."""
        self.breakers.record_success(model)
        resolved_provider = provider or self.breakers.infer_provider(model)
        if resolved_provider:
            self.breakers.record_provider_success(resolved_provider)

    def apply_health_event(
        self,
        *,
        model: str,
        ok: bool,
        kind: str = "ok",
        provider: str | None = None,
    ) -> SystemMode:
        """Feed one LLM health signal into the per-model breakers; returns the mode.

        Model-level availability failures update the named model. Connection and
        rate-limit failures also update the aggregate OpenAI breaker because they
        normally affect the provider/account rather than one model. A healthy call
        recovers only its model plus its provider aggregate; bad output and permanent
        HTTP/client errors remain observable without being treated as outages.
        """
        resolved_provider = provider or self.breakers.infer_provider(model)
        if ok:
            self.record_llm_success(model, resolved_provider)
        else:
            if kind in _MODEL_OUTAGE_KINDS:
                self.breakers.record_failure(model)
            if (
                resolved_provider
                and self.breakers.normalize_provider(resolved_provider) == CircuitBreakerRegistry.OPENAI
                and kind in _PROVIDER_OUTAGE_KINDS
            ):
                self.breakers.record_provider_failure(resolved_provider)
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

    async def _handle_paper_review(self, env: BusEnvelope) -> None:
        review = CandidateReviewV1.model_validate(env.payload)
        is_canary = review.intent.strategy_id == PAPER_CANARY_STRATEGY_ID
        canary_allocation: StrategicAllocation | None = None
        admission_rejections: tuple[str, ...] = ()
        canary_arm_checked = False
        while True:
            await self.paper.wait_for_inputs(review, now_ms=self._now_ms)
            observed_now_ms = self._now_ms()
            decided_at_ms = observed_now_ms
            if is_canary and not canary_arm_checked:
                arm = await self.paper_canary_repository.consume(
                    account_id=self.settings.paper_account_id,
                    review=review,
                )
                canary_arm_checked = True
                if arm is None:
                    admission_rejections = ("technical_canary_arm_missing",)
                else:
                    try:
                        canary_allocation, decided_at_ms = allocation_from_consumed_arm(
                            arm,
                            review,
                            account_id=self.settings.paper_account_id,
                            now_ms=observed_now_ms,
                        )
                    except CanaryAuthorizationError:
                        admission_rejections = ("technical_canary_arm_invalid",)
                        log.exception(
                            "risk.technical_canary_arm_invalid",
                            review_id=review.review_id,
                        )
            try:
                decision = await self.paper.evaluate(
                    review,
                    allocation=canary_allocation if is_canary else self.strategic_allocation,
                    decided_at_ms=decided_at_ms,
                    system_mode=self.breakers.system_mode,
                    admission_rejection_reasons=admission_rejections,
                )
                break
            except PaperInputUnavailable:
                # A same-tick account/venue conflict can revoke authority after
                # wait_for_inputs returned. Re-enter the bounded condition wait.
                continue
        await self.bus.publish(Topics.RISK_TRADE_DECISION, decision)
        log.info(
            "risk.paper_decision",
            intent_id=review.intent.intent_id,
            symbol=review.intent.symbol,
            approved=decision.approved,
            rejection_reasons=decision.rejection_reasons,
            quantity=decision.quantity,
            worst_case_loss_usd=decision.worst_case_loss_usd,
        )

    async def _consume_paper_reviews(self) -> None:
        async for env in self.bus.subscribe(
            Topics.CANDIDATE_REVIEW,
            group="risk-paper",
            consumer="candidate-reviews",
        ):
            try:
                await self._handle_paper_review(env)
                await self.bus.ack(Topics.CANDIDATE_REVIEW, env, group="risk-paper")
            except PaperInputUnavailable:
                log.warning("risk.paper_input_unavailable", envelope_id=env.id)
            except PaperInputDeadlineExceeded:
                # VenueQuality is required by the strict output contract, so no
                # synthetic rejection may be invented when it never arrives.
                # Complete this terminal safe-drop before Redis's 3-minute reclaim.
                log.warning("risk.paper_input_deadline", envelope_id=env.id)
                await self.bus.ack(Topics.CANDIDATE_REVIEW, env, group="risk-paper")
            except Exception:
                log.exception("risk.paper_review_processing_failed", envelope_id=env.id)

    async def _handle_paper_venue(self, env: BusEnvelope) -> None:
        venue = VenueQualityV1.model_validate(env.payload)
        await self.paper.apply_venue(venue)

    async def _consume_paper_venue(self) -> None:
        async for env in self.bus.subscribe(
            Topics.VENUE_QUALITY,
            group="risk-paper",
            consumer="venue-quality",
        ):
            try:
                await self._handle_paper_venue(env)
                await self.bus.ack(Topics.VENUE_QUALITY, env, group="risk-paper")
            except Exception:
                log.exception("risk.paper_venue_processing_failed", envelope_id=env.id)

    async def _handle_paper_account(self, env: BusEnvelope) -> None:
        snapshot = AccountSnapshotV2.model_validate(env.payload)
        await self.paper.apply_account(snapshot)

    async def _consume_paper_account(self) -> None:
        async for env in self.bus.subscribe(
            Topics.ACCOUNT_SNAPSHOT_V2,
            group="risk-paper",
            consumer="account-v2",
        ):
            try:
                await self._handle_paper_account(env)
                await self.bus.ack(Topics.ACCOUNT_SNAPSHOT_V2, env, group="risk-paper")
            except Exception:
                log.exception("risk.paper_account_processing_failed", envelope_id=env.id)

    async def _handle_health(self, env: BusEnvelope) -> None:
        event = LLMHealthEvent.model_validate(env.payload)
        mode = self.apply_health_event(
            model=event.model,
            ok=event.ok,
            kind=event.kind,
            provider=event.provider,
        )
        log.debug(
            "risk.llm_health",
            model=event.model,
            provider=event.provider,
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
            elif snapshot.reconciled and self.account_snapshot is not None:
                current_payload = self.account_snapshot.model_dump(
                    exclude={
                        "message_id",
                        "produced_at",
                        "correlation_id",
                        "causation_id",
                        "reconciliation_detail",
                    }
                )
                incoming_payload = snapshot.model_dump(
                    exclude={
                        "message_id",
                        "produced_at",
                        "correlation_id",
                        "causation_id",
                        "reconciliation_detail",
                    }
                )
                if incoming_payload != current_payload:
                    self.account_snapshot = None
                    log.warning(
                        "risk.conflicting_account_snapshot_version",
                        exchange=snapshot.exchange,
                        captured_at=snapshot.captured_at.isoformat(),
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
            self._latest_account_captured_at = snapshot.captured_at
            self.account_snapshot = None
            raise ValueError(
                "account snapshot contains positions for a different exchange/account: "
                + ", ".join(mismatched_positions)
            )

        try:
            account = AccountState.from_snapshot(snapshot)
        except (ArithmeticError, ValueError):
            # Poison this event-time version so a delayed snapshot between the
            # previous good version and this invalid newer one cannot reopen risk.
            self._latest_account_captured_at = snapshot.captured_at
            self.account_snapshot = None
            raise
        self._latest_account_captured_at = snapshot.captured_at
        self.account_snapshot = snapshot
        self.account = account
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
            except Exception:
                # A malformed account event is itself loss of authoritative state.
                # Keep it pending for diagnosis/retry, but never keep trading from an
                # older snapshot after the boundary has observed invalid account data.
                self.account_snapshot = None
                log.exception("risk.account_processing_failed", envelope_id=env.id)
                continue
            try:
                await self.bus.ack(Topics.ACCOUNT_SNAPSHOT, env, group="risk")
            except Exception:
                # The applied snapshot remains authoritative. Its stable event-time
                # version makes the pending redelivery idempotent.
                log.exception("risk.account_ack_failed", envelope_id=env.id)

    def _handle_allocation(self, env: BusEnvelope) -> None:
        allocation = StrategicAllocation.model_validate(env.payload)
        produced_at = allocation.produced_at
        if produced_at.utcoffset() is None:
            self.strategic_allocation = None
            raise ValueError("strategic allocation produced_at must be timezone-aware")

        latest = self._latest_allocation_produced_at
        if latest is not None and produced_at < latest:
            log.warning(
                "risk.out_of_order_allocation",
                produced_at=produced_at.isoformat(),
            )
            return

        invalid = allocation_error(allocation)
        if invalid is not None:
            if latest is None or produced_at > latest:
                self._latest_allocation_produced_at = produced_at
            self.strategic_allocation = None
            raise ValueError(invalid)

        if latest is not None and produced_at == latest:
            current = self.strategic_allocation
            if current is None:
                return
            current_policy = (
                current.regime,
                current.stable_reserve_pct,
                tuple(sorted(current.strategy_weights.items())),
                current.max_gross_leverage,
            )
            incoming_policy = (
                allocation.regime,
                allocation.stable_reserve_pct,
                tuple(sorted(allocation.strategy_weights.items())),
                allocation.max_gross_leverage,
            )
            if incoming_policy != current_policy:
                self.strategic_allocation = None
                log.warning(
                    "risk.conflicting_allocation_version",
                    produced_at=produced_at.isoformat(),
                )
            return

        self._latest_allocation_produced_at = produced_at
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
            except Exception:
                self.strategic_allocation = None
                log.exception("risk.allocation_processing_failed", envelope_id=env.id)
                continue
            try:
                await self.bus.ack(Topics.STRATEGIC_ALLOCATION, env, group="risk")
            except Exception:
                log.exception("risk.allocation_ack_failed", envelope_id=env.id)

    async def close(self) -> None:
        await self.bus.close()

    async def _recover_paper_state(self) -> None:
        """Restore committed approvals before subscribing to any PAPER review."""

        if not isinstance(self.bus, DurableMessageBus):
            raise RuntimeError("PAPER recovery requires DurableMessageBus")
        await self.bus.start()
        if not self._paper_canary_repository_explicit:
            self.paper_canary_repository = build_persistence_canary_repository(self.bus.database.pool)
        recovered_at_ms = self._now_ms()
        rows = await self.bus.database.pool.fetch(
            """SELECT payload FROM event_audit
               WHERE topic=$1
                 AND payload->>'approved'='true'
                 AND payload->>'trading_mode'='PAPER'
                 AND payload->>'evedex_profile'='DEV'
                 AND payload->>'account_id'=$2
                 AND (payload->'intent'->>'entry_expires_ts_ms')::bigint >= $3
               ORDER BY produced_at, message_id""",
            Topics.RISK_TRADE_DECISION,
            self.settings.paper_account_id,
            recovered_at_ms,
        )
        decisions = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            decisions.append(RiskTradeDecisionV1.model_validate(payload))
        await self.paper.restore_reservations(decisions)
        log.info(
            "risk.paper_recovery_loaded",
            reservations=len(decisions),
            account_id=self.settings.paper_account_id,
        )

    async def run(self) -> None:
        try:
            configure_logging(
                self.settings.log_level,
                json_logs=self.settings.log_json,
                service=self.settings.service_name,
            )
            if self.settings.trading_mode is TradingMode.PAPER:
                await self._recover_paper_state()
            log.info("risk.start")
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._consume_health(), name="llm-health")
                tasks.create_task(self._consume_allocation(), name="strategic-allocation")
                if self.settings.trading_mode is TradingMode.DRY_RUN:
                    tasks.create_task(self._consume_commands(), name="tactical-commands")
                    tasks.create_task(self._consume_account(), name="account-snapshots")
                elif self.settings.trading_mode is TradingMode.PAPER:
                    tasks.create_task(self._consume_paper_reviews(), name="paper-reviews")
                    tasks.create_task(self._consume_paper_venue(), name="paper-venue-quality")
                    tasks.create_task(self._consume_paper_account(), name="paper-account-v2")
        finally:
            await self.close()


def main() -> None:
    asyncio.run(RiskService().run())


if __name__ == "__main__":
    main()
