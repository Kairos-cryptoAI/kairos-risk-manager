# kairos-risk-manager

Kairos Layer 5 — deterministic risk validation and the authoritative per-model
circuit breaker. This service contains no LLM and never sends exchange requests.

Two authority paths are deliberately isolated:

- `TradingMode.DRY_RUN` retains the legacy `TacticalCommand -> ValidatedOrder`
  research/synthetic path unchanged.
- `TradingMode.PAPER` consumes only strict `CandidateReviewV1`, `VenueQualityV1`,
  `AccountSnapshotV2`, and current `StrategicAllocation`, then emits
  `RiskTradeDecisionV1`. It never accepts a legacy tactical mutation.
- `TradingMode.LIVE` is a startup error in this release.

## Safety properties

- `KAIROS_REQUIRE_RECONCILED_ACCOUNT=true` by default. Do not disable it in live
  environments.
- Every command that could produce an exchange order, including a reduce-only exit,
  requires a full, reconciled account snapshot no older than
  `KAIROS_ACCOUNT_SNAPSHOT_MAX_AGE_S` (60 seconds by default). An exit cannot be sized
  safely without the authoritative signed position.
- A newer `reconciled=false` snapshot immediately revokes the previously trusted
  account view. Older out-of-order snapshots cannot roll state back.
- The entry drawdown gate uses the worse of reported daily loss and the decline from
  authoritative peak equity. Equality at the configured 3% limit is fail-closed;
  reduce-only exits bypass the entry gate.
- Leverage settings have validated ordering, every numeric account/price/sizing input
  must be finite, and malformed sizing inputs produce `NO_TRADE` rather than an order.
- New risk is refused when gross exposure or an existing symbol position cannot be
  valued. Same-direction additions consume only the remaining per-position allowance;
  changing direction requires a reduce-only close first.
- Strategic allocation, gross exposure, and minimum-notional gates are deterministic.
- `LOCAL_QUANT_MODE` refuses `ENTER_LONG_TREND`, `ENTER_SHORT_TREND`, and `REBALANCE`.
  Reduce-only exits remain available. `TEXT_LOCAL_FILTER` and `CONFLICT_SAFE` retain
  their narrower upstream semantics and do not blanket-disable otherwise valid entries.
- A consumed event is acknowledged only after validation and every required publish
  succeeds. Transient failures therefore remain pending for Redis redelivery.
- Safety gates publish a deterministic rejected decision before acknowledging the
  command; missing reconciliation or allocation is observable instead of silently
  dropping the tactical event.

## Isolated market-data simulator admission

`kairos_risk.SimulationRiskPolicy` is a pure public API for the separate
`SIMULATED` market-data simulator. It takes one immutable `SimulationSessionV1`,
an existing `CandidateReviewV1`, a recorded Binance UM top-N book frame (or no
frame), a supplied decision timestamp, and a requested research quantity. It
returns only `SimulationRiskDecisionV1`; it never imports PAPER runtime code,
uses an account or credentials, calls a network/venue, or constructs
`RiskTradeDecisionV1`.

The policy fail-closes to a zero-sized rejected record for missing, stale,
future, discontinuous, wrong-tape/symbol, or non-SIMULATED book inputs. Its
only approval path requires an `ALLOW` review, an allowlisted strategy, the
fixed five-symbol Binance UM universe, a causal admitted book, valid exit
geometry, and a frozen-session participation cap. A manually forged
non-SIMULATED session or book is also rejected, so it cannot bridge into the
PAPER authority path. Simulator decisions explicitly remain ineligible for
PAPER qualification, Trial 15, or alpha claims.

## PAPER / EVEDEX DEV admission

PAPER is restricted to the exact EVEDEX DEV profile, a dedicated non-production
account, durable Redis/PostgreSQL delivery, and this fixed mapping:

| Binance signal | EVEDEX DEV |
| --- | --- |
| `BTCUSDT` | `BTCUSD:DEV` |
| `ETHUSDT` | `ETHUSD:DEV` |
| `SOLUSDT` | `SOLUSD:DEV` |
| `BNBUSDT` | `BNBUSD:DEV` |
| `XRPUSDT` | `XRPUSD:DEV` |

The exact `<strategy_id>@<revision>` PAPER allowlist is empty by default. The five
current Strategy Engine sleeves are hard-denied even if accidentally configured:
`trend_breakout_v1`, `trend_pullback_reclaim_v1`, `range_mean_reversion_v1`,
`orderflow_volatility_expansion_v1`, and `regime_veto_retest_reclaim_v1`. This keeps
`ALPHA_READY=false`; only an explicitly armed technical canary or a future promoted
strategy revision can pass the policy.

An approval requires all of the following at the same serialized decision boundary:

- the review embeds the exact unchanged intent/route and says `ALLOW`; `VETO` and
  `DEFER` are terminal rejections;
- the intent is eligible and unexpired, the venue measurement is fresh, executable,
  DEV-only, and for the exact mapped instrument;
- the circuit breaker is `NORMAL`;
- the dedicated PAPER/DEV account is fresh, fully reconciled, monotonic, within the
  drawdown gate, and has no position, order, or un-reconciled reservation for the
  symbol;
- aggregate reconciled plus reserved open risk remains below 1% of equity;
- a technical canary has no other active canary idea anywhere in the five-symbol
  universe (reconciled positions, unfinished entry orders, and recovered/in-flight
  approvals all count toward the global cap of one);
- Macro allocation is current, directionally compatible with the regime, and gives
  the exact strategy a positive allocation.

Conflicting account reconciliation sequences or venue observations poison the current
authority until a newer version arrives. Simultaneous reviews are serialized and the
first approval reserves its symbol and risk before publication. Once reconciliation
contains the trade, the authoritative snapshot owns that exposure; an unseen
reservation can be released only after entry expiry and a newer full reconciliation.
Ownership transfers only when every reconciled position/order field matches the durable
trade lineage; a reused `trade_id`, mutated strategy/intent/decision lineage, side, or
exit plan poisons that account snapshot and leaves the reservation intact.

Before PAPER subscriptions start, the service restores every committed, still-executable
approval for the dedicated account from the PostgreSQL audit/outbox transaction. New
entries remain blocked until that durable restore and the first authoritative
`AccountSnapshotV2` reconciliation are both complete. This prevents a restart between
approval and exchange reconciliation from admitting a second idea or exceeding the
portfolio-risk cap.

A review that arrives before its matching venue/account authority waits on the local
correlation condition and wakes immediately when the missing stream advances; it does
not depend on Redis's pending-message reclaim delay. If the immutable entry deadline is
reached without any `VenueQualityV1`, the review is terminally ACKed as a safe drop. A
synthetic `RiskTradeDecisionV1` is deliberately not invented because that strict
contract requires the exact venue measurement.

PAPER settings are shown in `.env.example`. `KAIROS_TRADING_MODE=PAPER` refuses an
in-memory bus, production environment/profile, a broad symbol universe, primary/live
account names, or `LIVE` authority. The legacy `KAIROS_DRY_RUN=false` switch is retired
and is a startup error; it can never grant PAPER or LIVE authority.

The Risk Manager owns `SystemMode`: it derives the mode from `kairos.llm.health` and
publishes changes on `kairos.system.control`. It deliberately does not subscribe to its
own control stream; command validation reads the same authoritative breaker registry
that produced the broadcast.

The health registry tracks `deepseek-v4-flash`, `gpt-5.6-luna`, `gpt-5.6-terra`, and
`gpt-5.6-sol` independently. A Flash outage selects `TEXT_LOCAL_FILTER`; a Luna outage
selects fail-closed `LOCAL_QUANT_MODE`; a Terra or Sol outage selects `CONFLICT_SAFE`.
Two or more unavailable models, an unknown unavailable model, or an aggregated OpenAI
connection/rate-limit outage selects `LOCAL_QUANT_MODE`. Successful calls recover only
the named model and its provider aggregate. Bad output and permanent HTTP/client errors
remain visible health failures but do not represent an availability outage.

## PAPER loss-at-stop sizing

PAPER uses executable EVEDEX top of book and the immutable strategy stop:

```text
risk_budget = min(0.25% * equity, 1% * equity - reconciled_open_risk - reserved_risk)
loss_per_unit = abs(worst_entry - stop) + round_trip_fees_per_unit + slippage_per_unit
quantity = risk_budget / loss_per_unit
```

The raw quantity is then reduced by 1x-by-default leverage/margin, per-position
notional, measured venue notional/depth, portfolio gross-notional, and Macro
strategy-allocation caps. Fees conservatively cover entry plus stop exit; slippage is
the side-specific value measured by `VenueQualityV1`. `NEXT_BAR_MARKET` and the entire
`ExitPlanV1` pass through unchanged. Aggregator priority, model provenance, LLM
confidence, and strategy `signal_strength` never increase size.

Defaults are at most 0.25% equity loss budget per trade and 1% total open risk. Pydantic
settings reject configuration above either ceiling. The current canary default is 1x;
leverage can only constrain notional and never multiplies risk budget.

## Manually armed technical canary

`kairos-paper-canary` is the only pre-alpha candidate source intended for the
technical EVEDEX DEV canary. It has no authenticated/mutation exchange client,
secret access, LLM, or news-feed dependency. It reads the latest closed Binance bar, fresh EVEDEX DEV
`VenueQualityV1`, authoritative `AccountSnapshotV2`, current Macro allocation when
present, and unfinished canary reservations from durable `event_audit`. It also
reads the exact instrument rule from the fixed public EVEDEX DEV endpoint; the
canonical rule subset and SHA-256 are bound into the immutable intent.

The command is read-only unless both `--publish` and the exact arm phrase are
present. A preview builds the canonical IDs and bounded lifecycle without publishing:

```powershell
uv run --locked kairos-paper-canary --symbol BTCUSDT --side LONG
```

The fixed strategy revision is `technical-canary@1`; supported symbols are only
BTC, ETH, SOL, BNB, and XRP through the fixed Binance/EVEDEX DEV map. Stop distance
is constrained to 25–100 bps, target distance to 25–150 bps and at most twice the
stop, entry expiry to 5–30 seconds after the next-bar boundary, and holding timeout
to 1–15 minutes. Current EVEDEX worst-entry geometry is checked before any publish.
Quantity is exactly `max(minQuantity, ceil(minVolume / worstEntry / quantityIncrement)
* quantityIncrement)` and must align with current price/quantity increments and bounds.
Risk rejects the canary when that exact venue minimum does not fit the normal loss,
notional, liquidity, allocation, or portfolio caps; it never rounds the decision up.
The account must be fresh/reconciled and cannot contain another canary, a same-symbol
position/order, or an unresolved canary risk reservation.

An armed session requires PAPER mode, the durable bus, DEV profile,
`technical-canary@1` in `KAIROS_PAPER_STRATEGY_ALLOWLIST`, and a persisted bounded
session derived from a verified, fresh 24-hour read-only receipt. An arm phrase
alone no longer grants publication authority:

```powershell
uv run --locked kairos-paper-canary --symbol BTCUSDT --side LONG `
  --session-id <verified-session-id> --slot-id <exact-plan-slot> `
  --publish --arm "ARM EVEDEX DEV PAPER CANARY"
```

Publication is one PostgreSQL transaction: the controller stores a single-use arm
bound to the exact review, intent, account, signal/venue symbols and expiry, stores a
canonical deterministic allocation (0.25% `technical-canary`, 99.75% stable reserve,
direction-compatible non-CHOP regime, 1x), and enqueues that exact
`CandidateReviewV1(ALLOW)` in the outbox. There is no separate allocation stream or
ACK race.

The Risk PAPER consumer claims that exact arm in its own serialized PostgreSQL
transaction while the durable review inbox transaction is active. A crash between
the two is safe because the consumed arm is immutable and replayable by review ID.
Risk sizes only with the allocation bytes stored on the arm;
an in-memory Macro value cannot replace them, and a direct-bus canary `ALLOW` without
the arm is rejected. Generic promoted strategies continue to use the normal Macro
allocation stream. Publishing the resulting `RiskTradeDecisionV1` and acknowledging
the review remain in the durable inbox/outbox transaction. Deterministic IDs make a
same-payload retry or crash replay idempotent. One process invocation can prepare at
most one candidate, while Risk independently enforces the global one-canary cap.

The restartable controller is `uv run --locked python -m kairos_risk.canary_runner`.
Its subcommands are `preview-plan`, `arm-session`, `status`, `stop` and `submit-next`;
use `--help` for their explicit inputs. It has no polling/retry loop or receipt
import shortcut. The database persists one session's maximum ten attempts/two-hour
deadline, exact ordered slots, consumed reviews and terminal outcomes. A Risk
refusal still consumes its reserved attempt. Changing accounts or restarting a
process cannot create a second concurrent session. Stop enters draining and
does not disable protection or recovery of existing exposure.

Execution must independently verify the scope and commit a one-use dispatch
claim immediately before entry. The controller cannot claim a completed
scenario, a passed qualification, a real venue fill or strategy alpha. The live
read-only recorder and end-to-end venue coverage remain separate acceptance
requirements. See the version-pinned persistence dependency's
`docs/BOUNDED_CANARY.md` for the evidence and dispatch contract.

## Legacy DRY_RUN sizing and offline policy evaluation

On the legacy DRY_RUN path, `KAIROS_PER_TRADE_RISK_FRACTION=0.02` is an equity
allocation budget which is multiplied by the approved leverage and capped by the
per-position and strategic gross limits. It is **not** loss-at-stop, VaR, or a claim
that only 2% can be lost: the legacy `TacticalCommand` contract has no stop distance.

`kairos_risk.evaluation.evaluate_policy` runs a named, deterministic, network-free
matrix of commands, account states, system modes and allocations. It rejects duplicate
case names and asserts that approved decisions have finite positive notional, that
`NO_TRADE` is never approved, and that `LOCAL_QUANT_MODE` never creates new risk. The
test suite covers the exact drawdown boundary, stale/inconsistent PnL, non-finite input,
leverage/position caps, degraded-mode entry refusal, and protective exits.

## Required legacy DRY_RUN Execution account snapshot

Live execution is intentionally fail-closed until the Execution service implements an
authoritative reconciler and publishes a **full snapshot**, not a balance or position
delta, to `Topics.ACCOUNT_SNAPSHOT` (`kairos.account.snapshot`). The required shape is:

```python
AccountSnapshot(
    source="kairos-execution-engine",
    exchange="evedex",
    account_id="primary",
    equity_usd=10_250.0,
    available_balance_usd=8_000.0,
    margin_used_usd=2_250.0,
    peak_equity_usd=10_500.0,
    daily_pnl_pct=-0.5,
    realized_pnl_usd=-25.0,
    unrealized_pnl_usd=15.0,
    positions=[
        PositionSnapshot(
            source="kairos-execution-engine",
            exchange="evedex",
            account_id="primary",
            symbol="BTCUSDT",
            signed_quantity=-0.2,
            entry_price=65_000.0,
            mark_price=64_500.0,
            leverage=2.0,
            liquidation_price=90_000.0,
            unrealized_pnl_usd=100.0,
            protective_stop_order_id="exchange-stop-id",
            captured_at=exchange_capture_time,
        ),
    ],
    open_order_ids=["exchange-order-id"],
    captured_at=exchange_capture_time,
    reconciled=True,
    reconciliation_detail="balances, positions, and open orders fetched successfully",
)
```

`captured_at` must be timezone-aware. Every `PositionSnapshot` must carry the same
`exchange` and `account_id` as its parent. `positions` and `open_order_ids` must describe
the entire account at that capture point, including signed quantities and protective
stop IDs. `reconciled=true` may be set only after balances, positions, and open orders
have all been fetched and cross-checked successfully.

The execution engine publishes the snapshot:

- once at startup, before any live command can be approved;
- periodically at a cadence comfortably below the configured 60-second maximum age;
- immediately after each handled execution action, in addition to the periodic refresh;
- with `reconciled=false` and a useful `reconciliation_detail` as soon as an exchange
  read or cross-check fails, so Risk revokes the old state.

The default production configuration refuses new orders until the first fresh,
fully-reconciled snapshot arrives, and revokes that permission when reconciliation fails
or the snapshot becomes stale. Tests may opt out explicitly with
`require_reconciled_account=False`; this is not a production setting. Exchange events
that happen outside the command path are observed on the next polling refresh; a private
exchange event stream remains future work.

PAPER does not consume this legacy shape. It requires strict `AccountSnapshotV2` on
`kairos.account.snapshot.v2`, including trading mode/profile/account lineage, durable
day-start and peak equity, total open risk, complete positions/orders, and monotonic
`reconciliation_seq`.

## Windows / PowerShell development

Install [uv 0.12.3](https://docs.astral.sh/uv/), then:

```powershell
Set-Location D:\Kairos\kairos-risk-manager
uv python install 3.11
uv sync --locked
uv run --locked pytest -q --tb=short
```

Run the complete blocking check set:

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy kairos_risk
uv run --locked bandit -q -r kairos_risk -x tests
uv run --locked pytest -q --tb=short
uv build --no-sources
```

Run locally without Redis:

```powershell
$env:KAIROS_BUS_BACKEND = "memory"
uv run --locked python -m kairos_risk
```

The committed lockfile pins `kairos-core` to a reviewed Git commit. To update it, first
review and replace `tool.uv.sources.kairos-core.rev` in `pyproject.toml`, then run:

```powershell
uv lock --upgrade-package kairos-core
uv sync --locked
```

## Runtime delivery durability

With Redis, consumed IDs, validated/refused orders and completion are committed
through `kairos-persistence`; Redis is ACKed only after PostgreSQL commits.
Configure `KAIROS_PERSISTENCE_DATABASE_URL` through the deployment secret
provider. The in-memory backend intentionally bypasses persistence for tests.

Part of the [Kairos](https://github.com/Kairos-cryptoAI/kairos) system. MIT licensed.
