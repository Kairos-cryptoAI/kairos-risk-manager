"""Independent PAPER operator authority; strategy/LLM confidence cannot arm it."""

from __future__ import annotations

import json
from typing import Any, Protocol

from kairos_core.contracts import RiskTradeDecisionV1
from kairos_persistence.canary_session import CanaryScope
from kairos_persistence.operator_control import (
    OperatorAdmissionV1,
    OperatorControlRefused,
    OperatorSnapshotV1,
)

from .config import RiskSettings


class PaperOperatorRepository(Protocol):
    async def snapshot(self, expected_scope: CanaryScope) -> OperatorSnapshotV1: ...

    async def bind_decision(
        self, *, decision: RiskTradeDecisionV1, expected_scope: CanaryScope, expected_version: int
    ) -> OperatorAdmissionV1: ...


class RejectAllPaperOperatorRepository:
    async def snapshot(self, expected_scope: CanaryScope) -> OperatorSnapshotV1:
        raise OperatorControlRefused("durable operator control is unavailable")

    async def bind_decision(
        self, *, decision: RiskTradeDecisionV1, expected_scope: CanaryScope, expected_version: int
    ) -> OperatorAdmissionV1:
        raise OperatorControlRefused("durable operator control is unavailable")


def load_operator_scope(settings: RiskSettings, supplied: CanaryScope | None = None) -> CanaryScope:
    if supplied is None:
        path = settings.paper_operator_scope_file
        if path is None or not path.is_absolute() or path.suffix != ".json" or path.stat().st_size > 16_384:
            raise ValueError("operator scope requires a bounded independent absolute JSON artifact")
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    else:
        value = supplied.model_dump(mode="json")
    scope = CanaryScope.model_validate(value)
    if scope.account_id != settings.paper_account_id or scope.environment != settings.environment:
        raise ValueError("operator scope differs from the configured PAPER account/environment")
    return scope
