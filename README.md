# botDCA

Long-only geometric DCA trading bot for Bybit USDT perpetuals.

## Status

Foundation + historical replay. Live trading is **disabled by default**. The current branch provides the strategy engine, dry-run executor, API shell, PostgreSQL-ready configuration, Docker setup, tests, and a minute-candle backtester that uses the same `DcaStrategy` class as the runtime.

## Strategy v1

- Long only
- Default leverage: 24x
- Base margin unit: 1 USDT
- Take profit: +1.09% from weighted average entry
- Immediate re-entry after a completed cycle
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

These values remain configurable and should be treated as reconstructed estimates until replay against the trader's exact execution history is complete.

## Historical replay

The backtester fetches Bybit public linear-perpetual klines and traverses each candle as monotonic price segments. It executes DCA/TP thresholds inside the candle rather than only checking the close.

Because OHLC candles do not reveal whether the high or low happened first, both intrabar assumptions are supported:

- `low-first`: open -> low -> high -> close
- `high-first`: open -> high -> low -> close

Run both paths on one-minute HYPE data:

```bash
botdca-backtest \
  --symbol HYPEUSDT \
  --start 2026-08-03T00:00:00Z \
  --end 2026-09-16T23:59:00Z \
  --interval 1 \
  --path both
```

The JSON result includes completed cycles, gross and net realized P&L, taker fees, maximum DCA depth, maximum margin deployed, mark-to-market drawdown, and the final open-cycle P&L. Use `--include-cycles` to include every completed cycle.

The public Bybit kline endpoint returns at most 1,000 candles per request; the market-data client paginates the requested period automatically.

## Local development

```bash
cp .env.example .env
docker compose up --build
```

API: `http://localhost:8000`

Health check:

```bash
curl http://localhost:8000/health
```

Strategy status:

```bash
curl http://localhost:8000/api/v1/bot/status
```

Run tests locally:

```bash
pip install -e '.[dev]'
ruff check botdca tests
pytest -q
```

## Safety defaults

- `BOT_LIVE_TRADING=false`
- No withdrawal or transfer functionality
- Manual close always pauses the strategy
- Exchange adapter is dry-run until live execution is explicitly enabled
- Strategy state is designed so the exchange, not local memory, becomes the ultimate source of truth once the live adapter is added

## Planned next milestones

1. Validate minute-level replay against the exported HYPE trader cycles
2. Bybit V5 live adapter + private WebSocket reconciliation
3. PostgreSQL event persistence
4. Exchange-hosted TP/DCA order management
5. Dashboard with portfolio, DCA ladder, manual close, pause, resume, and emergency stop
