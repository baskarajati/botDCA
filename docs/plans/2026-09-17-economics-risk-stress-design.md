# Historical economics, risk, and stress design

## Hypothesis

The completed-cycle export understates the principal risk of the reconstructed
strategy. Because DCA quantities grow geometrically, exposure and floating loss
should become nonlinear after the deepest fills. Trading fees should reduce
realized returns, but small TP calibration changes should matter less than the
tail exposure, time underwater, and inability of an autonomous replay to match
the trader's entry schedule.

The implementation must report contrary evidence if the replay does not show
this pattern.

## Chosen approach

Extend `ReplayEngine` telemetry instead of post-processing only completed
cycles. This keeps economics and risk on the same `DcaStrategy` execution path
used by runtime and captures open-cycle excursions that a closed-trade report
would omit. Record entry and exit fees separately, peak position notional,
worst floating P&L, per-cycle maximum adverse and favorable excursion, longest
open cycle, and minute-resolution underwater/recovery durations.

Build a separate historical-risk report over the regimes that had enough data
for walk-forward calibration. Compare baseline and training-selected parameters
under both low-first and high-first candle paths. These economics are simulated
strategy economics, not the private trader's actual account P&L. Funding is
reported as unavailable and excluded unless timestamped historical funding
rates are supplied by a future funding model.

## Tail stress model

Use `DcaStrategy` itself to exhaust the configured ladder from a normalized
100 USDT starting price. From the final DCA fill, shock price by an additional
5%, 10%, and 20%. Report deployed margin, mark notional, floating loss, entry
fees, estimated close fee, and a capital-reserve proxy:

`deployed margin + floating loss + fees + configured maintenance buffer`

The maintenance rate is configurable and defaults to 0.5% solely as a stress
assumption. This is not Bybit UTA cross-margin liquidation logic and must never
be labelled an exact liquidation price or minimum account balance.

## Validation and boundaries

Synthetic tests cover fee separation, excursion tracking, underwater recovery,
ladder exhaustion, stress monotonicity, and preservation of DCA sizing. The
private CSV remains outside Git. No runtime defaults, live settings, deployment,
or exchange orders are changed.
