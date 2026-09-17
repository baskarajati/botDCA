# Bounded walk-forward calibration

## Decision

Calibrate only the two detected TP/leverage regimes with at least 30 completed
cycles. Preserve the final 30% of each regime as an untouched chronological
holdout. Smaller regimes are reported but skipped because splitting them would
produce misleading evidence.

The calibration uses the production `DcaStrategy` through `ReplayEngine`. It
does not implement a second approximation of the strategy.

## Parameters and bounds

- Set TP to the median observed TP in the training split.
- Search a single bounded coordinate pass for supported DCA trigger levels.
- Test baseline trigger minus 0.15 percentage points, baseline, and baseline
  plus 0.15 percentage points.
- Search a DCA level only when at least 10 training cycles reached that level.
- Test re-entry delays of 0, 30, 48, 60, 90, 120, and 180 seconds.
- Keep every DCA quantity multiplier, symbol, and base margin fixed.

Candidate scoring uses both low-first and high-first one-minute intrabar paths.
Anchored scoring first maximizes completed-cycle matches, then exact DCA-depth
matches. Continuous delay scoring first maximizes completed-cycle matches, then
penalizes surplus simulated cycles. The holdout is evaluated only after all
choices have been made.

## Interpretation

This procedure is deliberately too narrow to discover an arbitrary strategy.
It tests whether small, evidence-supported changes to the reconstruction carry
forward. A training improvement followed by a holdout failure is overfit and
must not change runtime defaults. Even a holdout pass would remain historical
behavioral evidence, not proof of profitability, liquidation safety, or live
order feasibility.
