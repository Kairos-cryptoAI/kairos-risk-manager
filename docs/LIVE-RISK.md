# Isolated LIVE risk evaluator

`kairos_risk.live.LiveRiskEvaluator` is a pure, opt-in evaluation boundary. It
does not enable `RiskSettings(trading_mode=LIVE)`, start a service, publish a
decision, contact a venue, or call the Execution signer. The existing runtime
continues to refuse LIVE. There is currently no production `LiveAccountSourceVerifier`
registered by this repository, so this module cannot approve a real account.

## API

```python
evaluator = LiveRiskEvaluator(
    scope: LiveRiskScopeV1,
    limits: LiveRiskLimitsV1,
    account_verifiers: Mapping[str, LiveAccountSourceVerifier] | None = None,
    input_verifier: LiveInputsSourceVerifier | None = None,
)

result = evaluator.evaluate(
    raw_account_evidence,
    source_id="registered-source-id",
    raw_inputs_evidence=authenticated_upstream_receipts,
    review=review,
    venue=venue_quality,
    allocation=allocation,
    decided_at_ms=decision_time_ms,
    system_mode=SystemMode.NORMAL,
)
```

Both verifier boundaries fail closed. The account verifier registry defaults
to empty, and the separate upstream-input verifier defaults to `None`; both are
required for approval. A registered account entry implements
`verify(raw_evidence, *, source_id, expected_scope)` and returns
`VerifiedLiveAccountV1`. The input verifier implements
`verify(raw_proof, *, expected_scope, review, venue, allocation, system_mode,
decided_at_ms)` and returns `VerifiedLiveInputsV1`. The evaluator checks exact
scope/source identity and canonical hashes for review, venue, allocation, and
system mode, with a maximum five-second receipt age. The verifier itself must
independently authenticate producer receipts; hashes or DTOs alone are not
authentication. Missing, foreign, stale, malformed, or refusing sources cause
a rejected decision.

`LiveRiskScopeV1` binds `context_sha256`, `source_set_sha256`, `policy_sha256`,
the canonical hash of the exact `LiveRiskLimitsV1`, the exact account and
strategy revision, and the closed signal-to-PROD-symbol
mapping. Its `scope_sha256` is the Core `canonical_sha256` of the validated JSON
model. These hashes identify a frozen scope; they are not signatures, custody
proof, or proof that an upstream release file was reviewed. A deployment must
establish that authority out-of-band before registering a verifier.

The verifier must attest to complete account state and mandatory numeric
metrics. `aggregate_open_*` cover reconciled positions and open entry orders;
`reserved_open_*` cover only committed entries not represented by the snapshot.
`daily_adverse_loss_usd` must be at least the sum of realized loss and
unrealized loss, each floored independently at zero (profits cannot offset
losses). It excludes unrepresented reservations, which the evaluator charges
once alongside existing open stop-risk in daily headroom. Strategy-level open and
reserved notional are also separate so macro weight limits cannot be bypassed.
No exposure, reservation, or daily-loss field defaults to zero.

## Admission and sizing

The evaluator requires an immutable route/review/intent match, non-SIM intent,
exact frozen strategy revision, ALLOW review, NORMAL system mode, eligible and
unexpired candidate, mapped EVEDEX PROD venue symbol, unblocked quote no more
than two seconds old, and verified LIVE/PROD reconciled account no more than
five seconds old. The quote's book and reference timestamps must each also be
nonfuture and no more than two seconds old; a recently wrapped stale quote is
not fresh. Account context must be exact and below the frozen equity
ceiling. Allocation must be present, valid and fresh (26 hours maximum), give
the strategy positive capital, permit the signal's regime/direction, and allow
at least 1x leverage. Long/short stop and target geometry is rechecked against
the executable top of book.

Sizing recomputes stop-distance loss plus entry/stop fees and measured
side-specific slippage. It uses the minimum of the 0.25%-equity trade ceiling,
explicit per-trade loss-dollar cap, 1%-equity remaining aggregate risk, and
remaining daily-loss room after charging current adverse loss, existing open
stop-risk, and unrepresented reservations. Quantity is rounded downward to 1e-8
before economics are recomputed with decimal arithmetic, then represented
conservatively in the Core floating-point DTO. Sizing refuses if values cannot
be converted finitely or conservative rounding would breach a hard ceiling.
Notional is bounded by available balance, venue depth and assessed
size, position and aggregate dollar caps, allocation gross capital, and
strategy-weight capital, after subtracting only unrepresented reservations.
Leverage is exactly 1x. No quota or minimum trade count is used; below-minimum
capacity rejects rather than inflating quantity.

Approved outputs are Core `RiskTradeDecisionV1` with `trading_mode=LIVE` and
`evedex_profile=PROD`; rejected outputs remain truthful LIVE/PROD decisions
with zero quantity and zero loss economics. `LiveRiskEvaluationV1` carries the
scope, source/evidence/account digests and the exact verified metrics alongside
that decision for a separate producer to bind. It is not an Execution signing
record and cannot substitute for Execution's own order-geometry authorization.

## Current boundary

Tests use an explicitly synthetic verifier fixture only. Passing those tests
demonstrates evaluator mechanics, not account-source authentication, release
sign-off, deployed reconciliation, production acceptance, or LIVE readiness.
Do not register a test verifier in a deployed process or infer LIVE authority
from this module's existence.
