# botDCA

Long-only geometric DCA trading bot for up to three Bybit USDT perpetuals.

## Status

Live trading remains **disabled by default**. The repository contains a
versioned experimental strategy family, per-symbol allocation, account-level
portfolio risk authorization, minute-level historical replay, trader-export
comparison and GreenSynergy profiling, Bybit V5 REST/private-stream adapters,
durable event persistence, exchange reconciliation, strategy accounting, modular
alerting, live orchestration primitives, an operator console, Docker/PostgreSQL
setup and CI tests.

Nothing here is validated, optimal or guaranteed profitable. The GreenSynergy
strategy is an **EXPERIMENTAL reconstruction** from a public trader export.

## Strategy family

Traded parameters live in an **immutable, versioned strategy family**, not in
loose settings. A basket is pinned to the version it opened with, so editing
parameters can never silently alter an already-open basket. A new parameter set
is a new version that applies only to future baskets.

### `greensynergy-reconstructed-v1` (default) — **EXPERIMENTAL**

Reconstructed from a public Bybit copy-trader export. **Not validated, not
optimal and not guaranteed profitable.**

- Long only, Cross margin, 24x leverage
- Take profit: **+1.09%** from the **current** weighted-average entry
- DCA quantity: `next_qty = previous_qty x 1.42`, then Bybit instrument rounding
- Autonomous live maximum: **DCA8**

Live DCA triggers, as drops from the **current** weighted-average entry:

| Level | Trigger | Level | Trigger |
|---|---:|---|---:|
| DCA1 | 1.30% | DCA5 | 3.20% |
| DCA2 | 1.85% | DCA6 | 3.60% |
| DCA3 | 2.40% | DCA7 | 3.80% |
| DCA4 | 2.70% | DCA8 | 4.00% |

### Research-only ladder — never traded live

Historical GreenSynergy baskets reached deeper than DCA8 (HYPE and ONDO to
roughly DCA11, DOGE to roughly DCA13). A smooth continuation is carried in the
version as a **separate research ladder**:

| Level | Trigger | Level | Trigger |
|---|---:|---|---:|
| DCA9 | 4.20% | DCA12 | 4.80% |
| DCA10 | 4.40% | DCA13 | 5.00% |
| DCA11 | 4.60% | | |

These are used only for research, historical comparison, capital forecasting and
stress testing. They are not returned by the live ladder, so the runtime cannot
reach them. Extending autonomous live depth beyond DCA8 is a separate, explicit
decision.

### Re-entry

GreenSynergy re-enters a symbol almost immediately after a basket closes: the
observed median is about six seconds, with roughly 98–99% of re-entries within
five minutes. The exact scheduler and admission rules are **not** proven by the
trader export, so the production delay is an explicit, versioned, configurable
policy rather than a hidden constant.

### `zuya-reconstructed-v1` — legacy

The original PR #1–#3 reconstruction, derived mainly from Zuya HYPEUSDT, with
its non-uniform per-level multipliers (1.339, 1.539, 1.430, 1.442, 1.449, 1.450,
1.466, 1.467) and its own trigger ladder. Preserved so existing behaviour and
PR #3's historical validation remain reproducible.

**Zuya and GreenSynergy are different systems.** Their datasets are never mixed,
and the Zuya ladder is never presented as GreenSynergy.

## Per-symbol allocation

One strategy version is shared across up to three operator-selected coins. What
varies per symbol is the starting allocation, not the strategy logic. Every
enabled slot must reference the same version.

```
          greensynergy-reconstructed-v1
                       |
        +--------------+--------------+
     Slot 1          Slot 2         Slot 3
        |               |              |
  per-symbol starting allocation (mode + value)
        |               |              |
        +--------------+--------------+
                       |
            Portfolio Coordinator
                       |
            Bybit Unified Account
```

| Sizing mode | Quantity | Margin |
|---|---|---|
| `FIXED_MARGIN_USDT` | `margin x leverage / price` | the configured margin |
| `FIXED_BASE_QUANTITY` | the configured quantity | `qty x price / leverage` |

"HYPE initial quantity = 0.6 HYPE" is **not** "HYPE initial margin = 1 USDT".
Quantity, notional, margin, leverage and ROI% are kept separate everywhere. At a
fixed quantity, raising leverage does not change the dollar profit or loss from a
price move; it only reduces reserved initial margin. A higher ROI% is never a
stronger strategy edge.

Instrument tick size, quantity step, minimum quantity and minimum notional are
enforced; an allocation that rounds below a limit is refused, not rounded up.

The third coin is still being researched and is **not** selected automatically.

## Portfolio risk

The main GreenSynergy risk is correlated deep DCA across several coins drawing
on one Bybit Unified Account, not one coin reaching DCA8. HYPE at DCA7, ONDO at
DCA6 and a third coin at DCA2 is not three ordinary shallow baskets.

Every entry and DCA is authorised against a coherent portfolio snapshot inside
one critical section shared by all symbols:

```
read account -> read bot exposure -> size candidate -> project exposure
-> evaluate guards -> submit -> persist
```

An authorised candidate is held as a pending reservation until the owning
symbol's exposure catches up or the reservation expires, because exchange
balances do not update instantly. Without it, two workers milliseconds apart
would both see the same free equity and both spend it.

| Setting | Guard |
|---|---|
| `BOT_MAX_TOTAL_BOT_MARGIN_USDT` | total committed margin across all symbols |
| `BOT_MAX_TOTAL_BOT_NOTIONAL_USDT` | total bot notional |
| `BOT_MIN_AVAILABLE_BALANCE_USDT` | absolute free-balance floor |
| `BOT_MIN_AVAILABLE_EQUITY_RATIO` | free balance as a fraction of equity |
| `BOT_DEEP_DCA_LEVEL` | depth at which a basket counts as deep |
| `BOT_MAX_SIMULTANEOUS_DEEP_BASKETS` | deep baskets tolerated at once |
| `BOT_MAX_TOTAL_FLOATING_LOSS_USDT` | optional portfolio floating-loss ceiling |

Take-profit management, reconciliation and manual reduce-only close stay
available when new risk is blocked.

## Max-DCA behaviour

DCA8 does not mean "the bot ran out of code". A basket reaching its pinned
maximum enters an explicit `MAX_DCA_REACHED` state, default policy
`HOLD_AND_ALERT`: no further normal DCA and no invented DCA9, take profit kept
active, reconciliation continuing, manual reduce-only close available, a
critical alert raised, manual intervention flagged, and the state reconstructed
correctly after a restart.

## Position protection

An open bot position must either hold a valid exchange-hosted take profit or be
explicitly unhealthy. A missing take profit is journaled, alerted, surfaced in
readiness and repaired when that is unambiguously safe. Duplicate reduce-only
exits are never created: several resting exits are reported as
`protection_ambiguous` and held for a human.

## Accounting

`GET /api/v1/accounting` attributes results from the bot's own deterministic
order identities, **never** from a wallet-balance delta, because deposits,
withdrawals, transfers and unrelated positions move the balance without being
strategy results. Realized P&L, entry fees, DCA fees, close fees, funding and
rebates are reported per symbol and for the portfolio. Anything that cannot be
attributed is preserved in an explicit `unattributed_adjustments_usdt` bucket,
and every limitation is stated in the response.

## Alerts

Alerts are raised by the runtime and delivered by sinks, so no vendor appears in
strategy code. The journal sink always persists them; an optional generic webhook
is configured with `BOT_ALERT_WEBHOOK_URL`. A failing notification channel never
interrupts reconciliation. Persistent states are deduplicated, so a condition
re-checked every two seconds produces one message rather than thousands.

Covered: worker crash, worker stale, stream disconnected, reconciliation failure,
order rejected, risk-blocked entry, risk-blocked DCA, deep DCA reached, max DCA
reached, low available equity, large portfolio floating loss, missing take profit,
protection repaired, manual intervention required.

## Activation lifecycle and trial mode

```
DRAFT -> VALIDATED -> APPROVED_FOR_TESTNET -> APPROVED_FOR_MAINNET_TRIAL
```

A configuration advances one step at a time, and any edit to the traded
parameters returns it to `DRAFT`. Saving can never make a freshly edited
experimental strategy mainnet-live. Mainnet additionally requires
`BOT_MAINNET_PREFLIGHT_APPROVED=true`.

`BOT_TRIAL_MODE=true` constrains a first live run via
`BOT_TRIAL_MAX_ACTIVE_SYMBOLS`, `BOT_TRIAL_MAX_DCA_LEVEL`,
`BOT_TRIAL_MAX_PORTFOLIO_MARGIN_USDT` and
`BOT_TRIAL_MANUAL_RESUME_AFTER_RESTART`. Trial mode can only ever tighten
limits, never loosen them, and the values are operator configuration rather than
recommendations.

## Profile GreenSynergy separately from Zuya

The private CSV stays outside Git; `--workdir` is a caller-supplied directory,
never a repository path:

```bash
botdca-profile-greensynergy \
  --csv /private/path/bybit-greensynergy-past-trader-initiated-trades.csv \
  --workdir /private/path/greensynergy-work
```

Reports per symbol: basket count, DCA-depth distribution and probabilities,
maximum observed depth, quantity-multiplier distribution by level, take-profit
distribution, re-entry timing, holding times, initial-allocation distribution,
simultaneous deep-basket periods and portfolio capital expansion.

Two things differ from the Zuya path, both necessary:

- The export holds three symbols. The Zuya parser **rejects** a foreign symbol
  rather than filtering it, and its overlap check is global, so the export is
  partitioned per symbol first.
- Basket grouping uses relative tolerances. The Zuya defaults compare average
  entry exactly and allow five seconds of close skew, which splits GreenSynergy
  baskets whose rows share an average entry to eight decimals. The Zuya defaults
  are left untouched so PR #3's validated numbers do not move.

Trigger-ladder accuracy is deliberately **not** claimed from this export:
per-fill prices are not present, and a ladder replay is scale-invariant in price,
so any first-fill price can be solved for exactly. Only the quantity rule is
scored.

## Per-symbol risk controls

These bound one symbol. The account-level guards are in
[Portfolio risk](#portfolio-risk) above, and they are what matters when several
coins escalate together.

- `BOT_MAX_DCA_LEVEL=8`
- `BOT_MAX_STRATEGY_MARGIN_USDT=80`
- `BOT_MIN_AVAILABLE_BALANCE_USDT=0`
- `BOT_MIN_AVAILABLE_EQUITY_RATIO=0.20`

Before a DCA is planned, botDCA checks strategy-level margin and ladder depth.
The account reserve floor is the larger of the absolute free-balance floor and
the configured percentage of account equity. When a portfolio coordinator is
configured it evaluates that floor **once, across every symbol**, rather than
per symbol, so the same free capital is never counted twice. If the next DCA
would breach a guard, the DCA is suppressed while the TP remains active.

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

## Identify entry timing and sparse deep DCA behavior

Use the aggregate export and cached one-minute candles to test entry/re-entry hypotheses and
to keep the few observed DCA9-DCA10 cycles separated by leverage/TP regime:

```bash
botdca-identify-entry \
  --csv /path/to/bybit-trader-export.csv \
  --symbol HYPEUSDT \
  --timezone Europe/Rome \
  --output /private/path/HYPEUSDT.entry-identification.json \
  > /tmp/HYPEUSDT-entry-identification-stdout.json
```

The timing comparison learns a scheduler phase from the first 70% of transitions and evaluates
it on the final 30%. It compares a literal fixed-delay model with an attempt near the next
one-minute candle boundary and reports skipped-minute behavior separately. DCA trigger evidence
is bounded by each entry-minute candle and the exported final weighted-average entry; it does
not invent unavailable individual fill prices.

Sparse deep levels are reported by regime and never merged into the runtime ladder. The command
is an identification tool only: it does not update runtime defaults or enable live trading.

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

## Analyze economics and tail risk

Use the saved calibration report to compare baseline and calibrated replay economics on the
regimes with enough evidence:

```bash
botdca-analyze-risk \
  --csv /path/to/bybit-trader-export.csv \
  --calibration-report /private/path/HYPEUSDT.calibration.json \
  --symbol HYPEUSDT \
  --timezone Europe/Rome \
  --path both \
  --fee-rate 0.00055 \
  --maintenance-margin-rate 0.005 \
  --output /private/path/HYPEUSDT.risk.json \
  > /tmp/HYPEUSDT-risk-stdout.json
```

The report separates entry fees, exit fees, gross realized P&L, and net realized P&L. It also
reports peak strategy margin, peak position notional, worst floating P&L, drawdown, cycle
duration, minute-resolution underwater/recovery time, MAE/MFE, and DCA-depth frequency.
Funding is excluded unless a timestamped `FundingModel` is supplied; the report never inserts
invented historical rates.

Tail stress exhausts the runtime DCA ladder and then shocks price another 5%, 10%, and 20% from
the final DCA fill. Its account-equity figure is a configurable reserve proxy, not an exact Bybit
UTA liquidation value. See
[`docs/reports/2026-09-17-hypeusdt-validation-summary.md`](docs/reports/2026-09-17-hypeusdt-validation-summary.md)
for the current research result and limitations.

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

Controls include guarded resume, pause, and confirmed close-position-and-pause requests.
The console is split into Overview (`/`), Configuration (`/configuration`) and Journal
(`/journal`). It shows worker/private-stream freshness, readiness checks, recent actual
fills, CSV export and a read-only configuration fingerprint. Operator authentication
is required for controls and credentialed account data. Client-side page navigation
keeps the token in memory; reload requires reconnecting from Configuration.

Configuration persists three strategy slots in the database. Each enabled slot
selects a unique Bybit linear USDT perpetual and its own initial (DCA0) margin.
The UI forecasts added and cumulative margin through DCA8 for each coin and the
combined portfolio. Forecasts are informational: they never reserve funds,
close a position or define a maximum loss. All enabled coins share the same
Unified Account equity and reserve floor. Live workers make account/order
decisions under one shared portfolio lock, while controls and worker leases
remain symbol-specific.
Stale data disables resume/close; locking the console does not pause trading.

On a VPS, the Configuration page can validate a Bybit mainnet key and store its
authenticated ciphertext in a dedicated persistent volume. The master encryption
key, operator token and database URL are mounted as Docker secrets. The browser
does not retain or redisplay the Bybit values, and successful credential storage
does not start the worker or enable entries. Use only a private HTTPS path; the
VPS Compose profile keeps the API port on loopback.

`BOT_TRIAL_EQUITY_USDT=100` is the agreed trial reference, not an account balance
or maximum-loss limit. Preview places no orders and is not a paper-trading simulator.
See [the trial runbook](docs/TRIAL_RUNBOOK.md) for setup, data limitations and activation gates.

## Deploy to a VPS

```bash
scripts/deploy-vps.sh        # deploy main; any ref, tag or commit also works
```

Release-directory deployment using the hardened Compose profile. Deploys in
preview and refuses to run while any activation flag is enabled. See
[docs/TRIAL_RUNBOOK.md](docs/TRIAL_RUNBOOK.md).

## Local development

```bash
# Only copy if .env does not already exist; preserve existing configuration.
cp -n .env.example .env
docker compose up --build
```

Dashboard/API: `http://localhost:8000`

For the hardened VPS layout, follow [the trial runbook](docs/TRIAL_RUNBOOK.md)
and layer `docker-compose.vps.yml` over the base Compose file. Do not put Bybit
credentials in `.env`.

```bash
curl http://localhost:8000/health
# Account/API routes require X-Operator-Token when configured.
```

Run tests:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check botdca tests
.venv/bin/python -m pytest -q
```

## Safety defaults

- `BOT_LIVE_TRADING=false`
- `BOT_START_LIVE_WORKER=false`
- `BOT_MAINNET_PREFLIGHT_APPROVED=false`
- no withdrawal or transfer functionality
- no automatic change to the Bybit position mode
- live mutations require explicit live mode and credentials
- REST acknowledgements are never assumed to be fills
- unexpected short positions are refused
- DCA planning is bounded by depth, strategy-margin, and account-reserve limits
- worker errors fail closed by pausing re-entry
- strong operator token, PostgreSQL and trial-reference checks gate live startup
- mainnet additionally requires explicit operator preflight acknowledgement
- hedge-mode exposure is refused rather than incorrectly treated as flat
- execution history recovery precedes reconciliation on startup and reconnect
- entries and DCA are authorised against the whole portfolio, atomically
- research-only DCA levels are unreachable from the live ladder
- reaching the live maximum holds and alerts; DCA9 is never invented
- an open position without a valid take profit is explicitly unhealthy
- saving a configuration returns it to DRAFT; Save never arms mainnet
- the third coin is never selected automatically

## Remaining before production use

1. Run the optional Bybit testnet/demo lifecycle harness (`pytest -m bybit_integration`)
   so the first order-path proof does not have to use real funds
2. Verify Docker/PostgreSQL runtime, the additive schema migration and durable
   single-worker supervision on the target host
3. Complete private HTTPS/VPS deployment checks and validate the restricted mainnet key
4. Rehearse interruption/restart recovery without enabling entries, including a
   basket already at the live maximum
5. Select and research the third coin; PR #4 deliberately does not choose it
6. Advance the activation lifecycle deliberately, one step at a time
7. Obtain explicit approval for the exact funded account and first mainnet activation
8. Validate partial fills, recovery and close confirmation with the smallest approved live trial

No additional strategy optimization is planned before this evidence-collection
trial. Local tests and a usable UI do not establish exchange readiness or
profitability, and the GreenSynergy reconstruction remains experimental: a
martingale-like strategy can show a very high win rate and still be net
loss-making after one tail event.
