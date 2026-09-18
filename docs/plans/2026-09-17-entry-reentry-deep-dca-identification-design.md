# Entry, re-entry, and deep-DCA identification

## Hypotheses

1. Normal re-entry is a minute-scheduled attempt near the start of the next
   candle, not a literal fixed 48-second wait.
2. Some scheduled attempts are skipped by an unobserved admission or pause
   condition. Very deep recovered cycles are more likely to receive a long
   cooldown.
3. Mature DCA9 behavior is an emergency capped add rather than another
   geometric size increase. The single older DCA10 cycle belongs to a distinct
   20x ladder and must not be pooled with mature 24x behavior.

These are historical identification hypotheses, not live strategy changes.

## Analysis design

Use the grouped private export and the exact cached Bybit one-minute candles.
Split cycle-to-cycle transitions chronologically: first 70% for discovery and
last 30% for holdout timing evaluation. Compare a fixed 48-second prediction
with a minute-scheduler prediction whose second-of-minute phase is estimated
from immediate training transitions. Report median and p90 absolute timing
error, immediate-next-minute rate, scan skips, and results by preceding DCA
depth.

For first-entry price, restrict exact comparisons to DCA0 cycles because their
exported final average equals their only entry. Compare it with the opening and
closing price of the containing one-minute candle. Layered cycles retain only
a final weighted average and cannot establish an exact initial fill.

For DCA9 and DCA10, use each order quantity and its entry-minute candle range.
Tighten every fill-price interval with the exported final weighted-average
constraint. Derive conservative trigger-drop bounds from the prior weighted
average interval. Keep older 20x and mature 24x evidence separate.

## Decision rules

- Recommend `next-candle open` only as the replay proxy if it beats fixed 48
  seconds on holdout median error and at least 70% of holdout transitions enter
  on the immediate next minute.
- Report deep-cycle cooldown association, but do not set a duration unless a
  supported deterministic rule is observed.
- Never add runtime DCA9 or DCA10 from fewer than 10 regime-consistent samples.
- Flag near-1.0 quantity multipliers as capped-size evidence, not geometric
  progression.

## Deliverables and safety

Add an aggregate JSON/terminal analysis command and synthetic tests for timing
classification, weighted-average interval tightening, and regime separation.
Do not expose raw CSV rows, change runtime defaults, arm live trading, or amend
the already-open PR conclusion that `strategy-v1` is not ready.
