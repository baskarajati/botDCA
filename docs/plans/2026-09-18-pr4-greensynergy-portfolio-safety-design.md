# PR #4 design — GreenSynergy strategy family and portfolio safety

Status: experimental. Built on PR #3, which is merged into `main` (squashed to
`317ed1c`). Because the repository squash-merges, PR #3's head commit is not an
ancestor of `main`, so this branch was replanted with
`git rebase --onto origin/main c6978a3` rather than simply retargeted.

## Goal

Move from "run the reconstructed HYPE/Zuya bot on up to three symbols" to "run one
explicit, versioned, experimental GreenSynergy-style strategy family across up to three
operator-selected coins, each with its own starting allocation, under one enforceable
portfolio risk budget".

## Conflicts found against the current code

1. `trader_export._load_orders` rejects foreign symbols instead of filtering, and the
   overlap check is global. The GreenSynergy export holds three symbols, so it cannot be
   parsed today. Resolved with a separate symbol-partitioning profiler.
2. The close-fragment grouping compares average entry exactly and allows five seconds of
   close skew. On the GreenSynergy export that splits 93 HYPE and 6 ONDO baskets whose
   rows share an average entry to eight decimals. Resolved with a relative-tolerance
   grouping used only by the GreenSynergy profiler, so PR #3's Zuya numbers do not move.
3. `Settings.bot_max_dca_level` is bounded `le=8`. The research ladder therefore lives in
   the strategy version, never in runtime settings.
4. `StrategySlot` carries only `base_margin_usdt`. Sizing mode, sizing value, strategy
   version and activation status are new columns; `create_all` does not alter an existing
   table, so an explicit additive migration runs at schema creation.
5. Risk evaluation is per-strategy. Three workers can each pass `max_strategy_margin_usdt`
   while jointly exceeding account capital. A portfolio coordinator is required.
6. `order_plan.initial_market_qty` only implements fixed margin.

## Modules

| Module | Responsibility |
|---|---|
| `strategy_version.py` | Immutable versioned strategy family; live vs research ladder |
| `sizing.py` | `FIXED_MARGIN_USDT` / `FIXED_BASE_QUANTITY` with instrument rounding |
| `portfolio.py` | Atomic account-level authorization, pending reservations, guards |
| `protection.py` | Open position must hold a valid TP or be explicitly unhealthy |
| `alerts.py` | `Alert` / `AlertSink` / dispatcher with journal persistence and dedupe |
| `forecast.py` | Per-symbol ladder forecast, portfolio scenarios, approximate stress |
| `accounting.py` | Attributed realized P&L, fees, funding, `unattributed_adjustments` |
| `activation.py` | DRAFT / VALIDATED / APPROVED_FOR_TESTNET / APPROVED_FOR_MAINNET_TRIAL / RETIRED |
| `greensynergy_profile.py` | GreenSynergy-only research profiling, separate from Zuya |

## Invariants

- A basket is pinned to the strategy version it opened with.
- DCA9-DCA13 are research-only and never reachable by the live ladder.
- Entry and DCA submission happen inside one portfolio-authorization critical section.
- At max DCA the basket holds: no new DCA, TP and reconciliation stay active.
- An open bot position either has a valid exchange-hosted TP or is explicitly unhealthy.
- REST acknowledgement is not proof of fill.
