# Second-level entry uncertainty audit design

## Decision

Use Bybit's archived HYPEUSDT public trades to determine whether the remaining
weighted-average-reference holdout mismatches are caused by one-minute entry
uncertainty or by a missing strategy rule. This is a diagnostic after the
holdout was opened. It cannot restore untouched-holdout status and must not
change runtime defaults, push the branch, or update a pull request.

## Source and scope

The V5 historical kline API has a one-minute minimum interval. Bybit's public
archive provides individual HYPEUSDT trades with sub-second timestamps. Keep
the compressed archive outside the repository and download only UTC dates
covering unresolved cycles. Validate gzip integrity, schema, timestamp order,
trade-ID uniqueness in retained windows, and coverage around each exported
order and close timestamp.

## Method

1. Recreate the same chronological regime split and training-only TP estimate
   used by the trigger-reference experiment.
2. Identify holdout cycles unmatched by the weighted-average model on either
   one-minute candle path.
3. Stream archived trades and retain only each unresolved cycle's entry-through-
   close-plus-300-second window.
4. Aggregate retained trades to one-second OHLC candles without forward-filling
   seconds that had no trades.
5. Replay the unchanged weighted-average DCA ladder from predeclared initial
   price candidates: second open, second low, second high, and the exact
   exported average for DCA0 cycles. Test both one-second OHLC paths.
6. Require both the observed DCA depth and a close within 300 seconds of the
   exported close. Do not search arbitrary prices or widen the match window.

## Interpretation

A mismatch is `resolution_recoverable` when at least one legitimate candidate
matches depth and close time. It is `model_mismatch` when complete second-level
coverage exists but no candidate matches. It is `data_gap` when required raw
trade coverage is missing. Report candidate multiplicity so a recovered cycle
is not mistaken for identification of the trader's exact fill.

The aggregate report must distinguish recovered one-minute mismatches from a
new validation result. Even if enough cycles become recoverable to exceed 90%,
new later trades are required for an untouched final validation.
