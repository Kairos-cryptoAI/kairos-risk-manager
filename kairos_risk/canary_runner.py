"""Restartable, single-step session controller. No polling loop, venue or paid API.

``submit-next`` may enqueue one exact slot; it cannot re-arm a failed slot with
new input, extend a session, or claim actual scenario coverage. Real dispatch
must independently recheck session admission at the execution boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kairos_core.enums import Side
from kairos_persistence.canary_session import (
    MAX_RECEIPT_AGE_MS,
    BoundedCanaryPlan,
    CanaryAdmissionError,
    CanaryScope,
    CanarySessionRepository,
)
from kairos_persistence.config import PersistenceSettings
from kairos_persistence.database import Database

from .canary import (
    CANARY_ARM_PHRASE,
    CanaryArmingError,
    CanaryPlan,
    CanarySession,
    PostgresCanaryInputSource,
    _validate_publish_settings,
)
from .canary_authorization import build_persistence_canary_repository
from .config import RiskSettings

SESSION_ARM_PHRASE = "ARM ONE BOUNDED EVEDEX DEV CANARY SESSION"


def check_runtime_scope(scope: CanaryScope, settings: RiskSettings) -> None:
    _validate_publish_settings(settings)
    if scope.account_id != settings.paper_account_id or scope.environment != settings.environment:
        raise CanaryArmingError("session scope does not match the configured PAPER account/environment")


def next_slot(status: dict[str, Any]) -> tuple[str, CanaryPlan] | None:
    """Select, never reset or retry a used slot; the database rechecks admission."""
    plan = BoundedCanaryPlan.model_validate(status["plan"])
    if status["state"] not in {"ARMED", "RUNNING"}:
        raise CanaryAdmissionError("session is not admitting entries")
    attempts = status.get("attempts", [])
    if any(item["state"] != "TERMINAL" for item in attempts):
        raise CanaryAdmissionError("previous attempt awaits authoritative Risk/execution completion")
    count = status["attempts_reserved"]
    if len(attempts) != count:
        raise CanaryAdmissionError("session attempt counter differs from its ledger")
    if count >= plan.max_attempts or count >= len(plan.slots):
        return None
    slot = plan.slots[count]
    return slot.slot_id, CanaryPlan(
        symbol=slot.symbol,
        side=Side(slot.side),
        stop_distance_bps=slot.stop_distance_bps,
        target_distance_bps=slot.target_distance_bps,
        max_holding_ms=slot.max_holding_ms,
        entry_window_ms=slot.entry_window_ms,
    )


def safe_status(status: dict[str, Any]) -> dict[str, Any]:
    """Operational state, not balances/PnL/secret material or a qualification claim."""
    return {
        key: status[key]
        for key in (
            "session_id",
            "receipt_id",
            "state",
            "armed_at",
            "entry_deadline_at",
            "attempts_reserved",
            "max_attempts",
            "last_progress_at",
            "stop_reason",
        )
    } | {
        "attempts": [
            {
                key: item[key]
                for key in ("attempt_id", "ordinal", "slot_id", "symbol", "state", "terminal_reason")
            }
            for item in status.get("attempts", [])
        ],
        "paper_qualified": False,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview-plan")
    preview.add_argument("--plan", required=True, type=Path)
    arm = commands.add_parser("arm-session")
    arm.add_argument("--plan", required=True, type=Path)
    arm.add_argument("--scope", required=True, type=Path)
    arm.add_argument("--receipt-id", required=True)
    arm.add_argument("--operator-nonce", required=True)
    arm.add_argument("--arm", required=True)
    arm.add_argument("--receipt-max-age-ms", type=int, default=MAX_RECEIPT_AGE_MS)
    for name in ("status", "stop", "submit-next"):
        command = commands.add_parser(name)
        command.add_argument("--session-id", required=True)
        if name == "submit-next":
            command.add_argument("--arm", required=True)
    return root


async def run_cli(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "preview-plan":
        return BoundedCanaryPlan.model_validate_json(args.plan.read_text(encoding="utf-8")).model_dump(
            mode="json"
        )
    if args.command == "arm-session" and args.arm != SESSION_ARM_PHRASE:
        raise CanaryArmingError("exact bounded DEV session arm phrase is required")
    if args.command == "submit-next" and args.arm != SESSION_ARM_PHRASE:
        raise CanaryArmingError("exact bounded DEV session arm phrase is required")
    settings = RiskSettings()
    _validate_publish_settings(settings)
    database = Database(PersistenceSettings())
    await database.connect()
    try:
        repository = CanarySessionRepository(database.pool)
        if args.command == "arm-session":
            scope = CanaryScope.model_validate_json(args.scope.read_text(encoding="utf-8"))
            check_runtime_scope(scope, settings)
            bounded_plan = BoundedCanaryPlan.model_validate_json(args.plan.read_text(encoding="utf-8"))
            return safe_status(
                await repository.arm_session(
                    receipt_id=args.receipt_id,
                    scope=scope,
                    plan=bounded_plan,
                    operator_nonce=args.operator_nonce,
                    receipt_max_age_ms=args.receipt_max_age_ms,
                )
            )
        status = await repository.status(args.session_id)
        scope = CanaryScope.model_validate(status["scope"])
        check_runtime_scope(scope, settings)
        if args.command == "stop":
            return safe_status(await repository.stop(args.session_id))
        if args.command == "status":
            return safe_status(status)
        status = await repository.refresh(args.session_id)
        choice = next_slot(status)
        if choice is None:
            return safe_status(status)
        slot_id, plan = choice
        result = await CanarySession(
            PostgresCanaryInputSource(database.pool), build_persistence_canary_repository(database.pool)
        ).run(
            plan,
            account_id=settings.paper_account_id,
            now_ms=time.time_ns() // 1_000_000,
            publish=True,
            arm=CANARY_ARM_PHRASE,
            session_id=args.session_id,
            slot_id=slot_id,
            account_max_age_ms=int(settings.paper_account_snapshot_max_age_s * 1_000),
            allocation_max_age_s=settings.strategic_allocation_max_age_s,
        )
        return result.summary() | {
            "session_id": args.session_id,
            "slot_id": slot_id,
            "paper_qualified": False,
        }
    finally:
        await database.close()


def main(argv: Sequence[str] | None = None) -> None:
    try:
        result = asyncio.run(run_cli(parser().parse_args(argv)))
    except Exception as exc:
        # Never echo arbitrary file/DB errors, which can contain a DSN or secret.
        message = (
            str(exc) if isinstance(exc, (CanaryAdmissionError, CanaryArmingError)) else type(exc).__name__
        )
        print(f"bounded canary refused: {message}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
