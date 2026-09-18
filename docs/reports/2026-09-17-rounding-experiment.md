# Training-only TP and quantity rounding experiment

## Result

The frozen training-selected TP construction recovered **0 of 7** prior failures
on each intrasecond path. All seven modeled close timestamps and DCA depths
remained identical to the control. Exit threshold prices changed, but no
close-and-depth match was recovered. This rejects TP construction alone as the
explanation for these failures under the fixed first-public-print entry rule.

Only the seven previously seen failures were replayed. The prior 73/80 score
has not been improved or revalidated with this TP rule; successful cycles could
regress. No new full-sample score, untouched validation, autonomous entry rule,
profitability or optimum is claimed.

## Training selection

Chronological training splits and regime boundaries were unchanged. A finite
14-candidate grid compared median realized TP and nominal TP rounded to 0.1
percentage points, with no price rounding or 0.001/0.01 floor/nearest/ceiling.
Minimum training mean absolute close-price error selected the rule. Neither
failure prices nor their close times selected a candidate. Regime segmentation
is inherited from earlier whole-export research, so this is not a fresh holdout.

| Regime | Training cycles | Selected construction | Mean absolute price error | Within 0.002 USDT |
| --- | ---: | --- | ---: | ---: |
| 3 | 103 | 1.1%, floor to 0.001 | 0.000325 | 103/103 |
| 4 | 80 | 1.0%, floor to 0.01 | 0.000544 | 79/80 |

The unrounded training-median controls had errors 0.000429 and 0.002432 USDT.
Those close-price fits support an explanatory TP hypothesis, not proof of the
trader's exact orders: exported final averages and closing prices are rounded
or aggregated. Final averages were not substituted into replay entries.

## Historical precision evidence

An additional official Aug 3 archive (about 15.8 MiB compressed) was downloaded
outside the repository; reading its gzip to EOF completed successfully.
All 407,753 public prints had an observed price grid of 0.001 and size grid
0.01. The existing Aug 19 file contained 1,045,945 prints on the same whole-day
grids. After regime 4's training start at 21:07:36 UTC, 205,543 prints still
included a 0.001 grid; a sub-cent print occurred at 21:45:00.470800 UTC.

Thus the selected 0.01 TP precision cannot be called an exchange requirement
throughout regime 4. It may be a trader calculation choice. Observed execution
grids do not recover official historical order increments. Bybit's
[instrument specification endpoint](https://bybit-exchange.github.io/docs/v5/market/instrument)
defines order-price `tickSize` and quantity `qtyStep`, but current specifications
would not independently prove historical values. No historical instrument
snapshot was recovered.

## Quantity diagnostic (not applied to replay)

Training export quantities have an observed grid of 0.01. Each addition was
predicted from the exported previous quantity and frozen ladder multiplier;
there was no per-cycle fitting. Only supported ladder levels were included.

| Regime | Pairs | Unrounded mean absolute qty error | Nearest-0.01 error | Nearest exact matches |
| --- | ---: | ---: | ---: | ---: |
| 3 | 175 | 0.004475 | 0.003714 | 110/175 |
| 4 | 99 | 0.003993 | 0.003535 | 64/99 |

Nearest rounding beat floor/ceiling in aggregate but leaves many unexplained
additions. This is not sufficient to identify the actual sizing rule. An
absolute 0.01 quantity increment also cannot be safely transferred to the
normalized one-USDT replay without a trader-scale sizing model. Replay quantities,
DCA thresholds, entry selection, funding assumptions and fees stayed unchanged.

## Replay evidence and recommendation

The diagnostic scanned nine archive dates and 3,315,020 public trades. Required
entry/order/close seconds were present; no timestamp order defects or retained
duplicate IDs were reported. These checks do not prove private fills or archive
completeness. The control reproduced every prior failure on both paths.
Detailed generated output remains outside the repository at
`Crypto_Quant/data/HYPEUSDT-zuya-rounding-experiment-2026-09-17.json`.

Request the trader's per-order execution export, including actual fill prices,
filled quantities, execution timestamps, order IDs and order type/limit prices.
The present CSV repeats the final position average instead of supplying each
addition's price. Its timestamps may also differ from execution timestamps.
Those missing observations prevent separating seed-price differences, trigger
rounding and delayed DCA admission confidently. Do not add an entry filter or
DCA9–10 rules merely to absorb these mismatches.

The brainstorming/debugging skills kept the experiment predeclared, training
selected and limited to one replay variable; verification checked Decimal
rounding, recomputation after DCA and unchanged execution mechanics.
88 tests passed; repository lint and diff checks passed. No runtime change,
commit, push or PR update was made.
