# kairos-risk-manager

Kairos Layer 5 — deterministic risk validation and the authoritative per-model
circuit breaker. This service contains no LLM and never sends exchange requests.

## Safety properties

- `KAIROS_REQUIRE_RECONCILED_ACCOUNT=true` by default. Do not disable it in live
  environments.
- Every command that could produce an exchange order, including a reduce-only exit,
  requires a full, reconciled account snapshot no older than
  `KAIROS_ACCOUNT_SNAPSHOT_MAX_AGE_S` (60 seconds by default). An exit cannot be sized
  safely without the authoritative signed position.
- A newer `reconciled=false` snapshot immediately revokes the previously trusted
  account view. Older out-of-order snapshots cannot roll state back.
- Daily drawdown, leverage, strategic allocation, exposure, and minimum-notional
  gates are deterministic.
- `LOCAL_QUANT_MODE` refuses `ENTER_LONG_TREND`, `ENTER_SHORT_TREND`, and `REBALANCE`.
  Reduce-only exits remain available. `TEXT_LOCAL_FILTER` and `CONFLICT_SAFE` retain
  their narrower upstream semantics and do not blanket-disable otherwise valid entries.
- A consumed event is acknowledged only after validation and every required publish
  succeeds. Transient failures therefore remain pending for Redis redelivery.
- Safety gates publish a deterministic rejected decision before acknowledging the
  command; missing reconciliation or allocation is observable instead of silently
  dropping the tactical event.

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

## Required Execution account snapshot

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

Part of the [Kairos](https://github.com/Kairos-cryptoAI/kairos) system. MIT licensed.
