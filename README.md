# botDCA

Long-only geometric DCA trading bot for Bybit USDT perpetuals.

## Status

Live trading remains **disabled by default**. The current branch contains the reconstructed strategy engine, minute-level historical replay, trader-export comparison, Bybit V5 REST/private-stream adapters, durable event persistence, exchange reconciliation, risk controls, live orchestration primitives, API/dashboard, Docker/PostgreSQL setup, and CI tests.

## Strategy v1

- Long only
- Default leverage: 24x
- Base margin unit: 1 USDT
- Take profit: +1.09% from weighted average entry
- Re-entry after a completed cycle following the configured delay
- Progressive DCA ladder reconstructed from historical HYPEUSDT trader data

Default DCA steps are expressed as percentage drops from the current weighted average entry:

1. 1.05% / size x1.339
2. 1.41% / size x1.539
3. 1.48% / size x1.430
4. 1.91% / size x1.442
5. 3.12% / size x1.449
6. 3.56% / size x1.450
7. 5.53% / size x1.466
8. 5.11% / size x1.467

These remain configurable reconstructed estimates until validation against the full trader export is complete.

## Risk controls

Default limits:

- `BOT_MAX_DCA_LEVEL=8`
- `BOT_MAX_STRATEGY_MARGIN_USDT=80`
- `BOT_MIN_AVAILABLE_BALANCE_USDT=0`
- `BOT_MIN_AVAILABLE_EQUITY_RATIO=0.20`

Before a DCA is planned, botDCA can check both strategy-level margin and the Bybit Unified Account `totalAvailableBalance`. The effective reserve floor is the larger of the absolute free-balance floor and the configured percentage of account equity. If the next DCA would breach that floor, the DCA is suppressed while the TP remains active.

## Pause semantics

`Pause` disables **re-entry**, not protection of an already-open basket. If a position is open, TP/DCA calculations and resting-order reconstruction remain available. When that basket closes, the strategy remains paused rather than starting another cycle.

`Close position & pause` is stronger: the controller pauses first, cancels resting orders, re-reads the actual exchange position, submits a reduce-only market close, and remains paused. A REST acknowledgement is never treated as a confirmed fill; execution/position reconciliation must confirm the close.

## Historical replay

The backtester fetches Bybit public linear-perpetual klines and traverses each candle as monotonic price segments, triggering DCA/TP levels inside the candle instead of checking only closes.

Because OHLC candles do not reveal high/low ordering, both paths are supported:

- `low-first`: open -> low -> high -> close
- `high-first`: open -> high -> low -> close

```bash
botdca-backtest \
  --symbol HYPEUSDT \
  --start 2026-08-03T00:00:00Z \
  --end 2026-09-16T23:59:00Z \
  --interval 1 \
  --path both
```

The result includes completed cycles, gross/net realized P&L, fees, maximum DCA depth, maximum strategy margin, mark-to-market drawdown, and final open-cycle P&L.

## Compare against a Bybit trader export

Keep the CSV outside the repository:

```bash
botdca-compare-export \
  --csv /path/to/bybit-trader-export.csv \
  --symbol HYPEUSDT \
  --leverage 24 \
  --path both \
  --match-window-seconds 300
```

The comparator reports completed-cycle match rate, closing-time error, exit-price error, DCA-depth error, and exact DCA-depth match percentage. Use `--timezone` for the export's named timezone. `--timezone-offset-minutes` remains available for legacy fixed-offset files.

## Build the historical evidence baseline

Keep the private trader CSV outside the repository. Before calibration, profile its cycle
structure and align it with exact Bybit one-minute candles:

```bash
botdca-profile-export \
  --csv /path/to/bybit-trader-export.csv \
  --symbol HYPEUSDT \
  --timezone Europe/Rome \
  --output /private/path/HYPEUSDT.trader-profile.json \
  > /tmp/HYPEUSDT-profile-stdout.json
```

The command:

- converts CSV timestamps through the named timezone and reports UTC output
- merges only narrowly matching close fragments instead of relying on exact close strings
- reports leverage/TP regimes, DCA depths, sizing multipliers, re-entry gaps, and holding times
- caches public Bybit candles under `~/.cache/botdca/marketdata`
- checks final weighted-average entries and closing prices against their one-minute ranges

The aggregate report does not contain the private CSV rows. One-minute candles bound possible
fills but do not establish exact second-level execution prices or intrabar ordering. Trigger
midpoints are diagnostics, not calibrated strategy parameters. Historical master sizing is
reported only as an approximate proxy because the export does not contain individual fill
prices, wallet equity, available balance, funding payments, or cross-margin liquidation state.

Use `--skip-market-data` for a CSV-only structural profile. Use
`--refresh-market-data` to replace the exact-window public candle cache.

## Validate strategy behavior

After the evidence baseline is clean, run both anchored-cycle and continuous validation with
the same `DcaStrategy` used by runtime:

```bash
botdca-validate-strategy \
  --csv /path/to/bybit-trader-export.csv \
  --symbol HYPEUSDT \
  --timezone Europe/Rome \
  --path both \
  --match-window-seconds 300 \
  --reentry-delay-seconds 48 \
  --output /private/path/HYPEUSDT.validation.json \
  > /tmp/HYPEUSDT-validation-stdout.json
```

Anchored validation starts one simulation at each observed first-entry minute and disables
automatic re-entry. This isolates DCA depth, weighted-average entry, TP price, and close-time
behavior. Continuous validation runs autonomously inside each detected regime and tests cycle
matching plus re-entry timing. Regime boundaries reset replay state rather than silently
changing parameters inside an open position.

Every run reports the current uniform baseline and an observed-regime scenario. The latter
changes only leverage and TP to the regime's observed values; it retains the baseline DCA
ladder. Observed-regime results are in-sample diagnostics, not calibrated holdout evidence.
Both low-first and high-first candle paths are reported because one-minute OHLC data does not
identify intrabar ordering or the exact second-level initial fill.

## Run bounded walk-forward calibration

Calibration is intentionally narrower than strategy discovery. It uses the first 70% of each
regime with at least 30 completed cycles for parameter selection and keeps the final 30% as an
untouched chronological holdout:

```bash
botdca-calibrate-strategy \
  --csv /path/to/bybit-trader-export.csv \
  --symbol HYPEUSDT \
  --timezone Europe/Rome \
  --path both \
  --output /private/path/HYPEUSDT.calibration.json \
  > /tmp/HYPEUSDT-calibration-stdout.json
```

The search can adjust TP, sufficiently supported DCA trigger drops, and re-entry delay within
documented bounds. DCA quantity multipliers remain fixed. Small regimes are skipped, both
intrabar paths are retained, and validation failures are reported rather than used for further
tuning. The command produces research evidence only; it does not update runtime defaults or
enable live trading.

## Live orchestration

`LiveStrategyService` follows a reconciliation-first workflow:

1. read the actual exchange position
2. if flat and re-entry is enabled, enforce reserve limits before submitting an initial entry
3. if open, reconcile the position against persisted executions
4. cancel stale resting orders only after reconciliation succeeds
5. place TP first
6. place the next DCA only if strategy and account reserve limits allow it

`LiveWorker` combines this service with the private Bybit execution/order/position stream and records every sync/error event. FastAPI constructs and starts the authenticated worker only when both `BOT_LIVE_TRADING=true` and `BOT_START_LIVE_WORKER=true`. Missing credentials fail startup. A PostgreSQL advisory lease prevents a second worker for the same symbol.

Orders use stable client identities. A restart reuses matching TP/DCA orders instead of cancelling and recreating them, and an uncertain REST response is recovered by querying Bybit with the same identity. Stale TP orders are replaced protection-first; stale DCA orders are removed before their replacement so two entry liabilities are not active together. A disconnected private stream is restarted before the next reconciliation pass.

## Dashboard

The FastAPI root (`/`) serves an operations dashboard showing:

- strategy state and live/dry-run mode
- average entry, position size, DCA level, next DCA, TP
- Unified Account equity and available balance when API credentials are configured
- perpetual unrealized P&L and maintenance margin
- strategy margin cap and account reserve floor
- account-aware next-DCA permission and reason

Controls include resume, pause, and close-position-and-pause.

## Local development

```bash
cp .env.example .env
docker compose up --build
```

Dashboard/API: `http://localhost:8000`

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/bot/status
curl http://localhost:8000/api/v1/account/status
```

Run tests:

```bash
pip install -e '.[dev]'
ruff check botdca tests
pytest -q
```

## Safety defaults

- `BOT_LIVE_TRADING=false`
- `BOT_START_LIVE_WORKER=false`
- no withdrawal or transfer functionality
- live mutations require explicit live mode and credentials
- REST acknowledgements are never assumed to be fills
- unexpected short positions are refused
- DCA planning is bounded by depth, strategy-margin, and account-reserve limits
- worker errors fail closed by pausing re-entry

## Remaining before production use

1. Validate minute-level replay directly against the complete exported HYPE trader history
2. Run operator-approved end-to-end reconciliation tests against Bybit testnet or demo
3. Validate partial-fill behavior with recorded exchange fixtures and testnet fault injection
4. Finalize VPS/Tailscale deployment and operational runbook
