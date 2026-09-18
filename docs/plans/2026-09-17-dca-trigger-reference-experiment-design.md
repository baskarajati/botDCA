# DCA trigger-reference experiment design

## Decision

Test whether each DCA trigger is measured from the current weighted-average
entry, the immediately previous fill, or the initial cycle entry. This is a
research-only comparison. It must not change runtime defaults, enable live
trading, push the current branch, or update the existing pull request.

## Why this test

Anchored validation still misses a material fraction of observed cycles even
though it removes autonomous re-entry timing from the comparison. A wrong DCA
trigger reference is therefore a plausible remaining structural mismatch.
Reusing the existing numeric drops under all three reference rules would be an
invalid comparison because it would create three different absolute ladders.

## Method

1. Keep the private CSV outside the repository and use cached Bybit one-minute
   candles.
2. Keep the existing leverage/TP regime segmentation.
3. Analyze only regimes with at least 30 completed cycles, split
   chronologically into the first 70% for training and the final 30% for one
   holdout evaluation.
4. Keep the existing eight trigger percentages and quantity multipliers fixed.
   Change only whether those percentages reference the current weighted
   average, the previous fill, or the initial entry. Fitting a separate trigger
   at every level for every reference would be observationally equivalent to
   fitting the same absolute ladder three different ways.
5. Estimate only the regime TP from training cycles, then freeze it before
   holdout evaluation.
6. Keep base margin, fee assumptions, and both low-first/high-first candle
   paths fixed.
7. Replay each holdout cycle independently through the runtime `DcaStrategy`
   and `ReplayEngine`.

## Ranking and acceptance

Rank preregistered models lexicographically by total holdout matches across
both candle paths, exact DCA-depth matches, fewer unmatched simulations, lower
median weighted-average-entry error, and lower exit-price error. Report every
model, not only the winner.

A model is a defensible winner only when it improves over the current
weighted-average model on both paths and does not materially increase the
normalized full-ladder notional or margin. Otherwise report that no reference
model is validated. Do not tune again after opening the holdout.

## Limits

One-minute candles do not identify exact fill prices or intrabar ordering.
The export repeats the final weighted-average entry rather than individual fill
prices. Trigger bounds are therefore constrained estimates. A holdout winner
would improve historical reconstruction, not prove profitability or live
safety.
