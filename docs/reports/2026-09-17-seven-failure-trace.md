# Seven fixed-entry failures: frozen-rule trace

The observer leaves all replay fills unchanged. It records DCA/TP events and
between-candle open-gap threshold crossings. Entry remains the first public
trade at the CSV timestamp; training-only TP, ladder, sizing and both intrasecond
paths are frozen. This is diagnosis on already inspected data, not new validation.

Nine daily archive files and 3,315,020 public trades were scanned. Entry, each
exported order and close seconds were present; no timestamp ordering defects or
retained duplicate IDs were reported. Archive completeness and private execution
are not thereby proven. The private JSON trace stays outside the repository.

## Findings

There were **zero between-second open-gap threshold crossings** across all
14 traces. A synthetic test demonstrates that the replay can miss such a jump,
but that behavior does not explain these seven cases. Do not change the replay
or strategy on the strength of that synthetic example alone.

All times below are UTC; timing differences are model minus exported timestamp.

| Opened | Exported depth | First public price / exported final average | Divergence |
| --- | ---: | --- | --- |
| Aug 13 04:37:09 | 0 | 56.85 / 56.84 | No modeled close; entry differs by 0.01. |
| Aug 13 07:09:26 | 1 | 57.40 / 57.073 | No modeled DCA or close despite an exported addition at +4,756 seconds. |
| Aug 27 14:38:03 | 0 | 84.16 / 84.17 | Model closes 721 seconds early. |
| Aug 27 18:41:03 | 4 | 86.00 / 83.793 | DCA2 is 5,289 seconds late, DCA4 is 20,102 seconds early; closes 23,130 seconds early. |
| Aug 29 14:27:25 | 0 | 82.14 / 82.06 | No modeled close; entry differs by 0.08. |
| Sep 02 05:25:14 | 3 | 83.42 / 81.924 | First three additions differ by +23, +102, +1 seconds, then model adds an unobserved DCA4 and closes 37,572 seconds early. |
| Sep 03 23:42:04 | 4 | 87.84 / 85.591 | First three additions differ by +1, +30, +6 seconds; DCA4 is 5,544 seconds early; no modeled close. |

For depth-zero cycles the exported final average is a direct entry comparison,
not a DCA average. Public first prints are therefore not an exact substitute for
the trader's entry fills. Deeper cycles do not expose individual fill prices;
their final averages cannot identify an entry price independently.

Observed addition quantity ratios are close to, but not exactly, the frozen
ladder. For example, the Aug 27 deep cycle is 1.346667, 1.534653, 1.432258,
1.441441 versus model 1.339, 1.539, 1.430, 1.442. Quantity rounding and fill-price
differences can shift weighted averages and subsequent triggers. They are
plausible explanations, not established causes.

The three deep traces show earlier additions roughly matching some exported
timestamps while later additions diverge materially. An entry-price correction
alone is not yet demonstrated to explain them; delayed admission, trigger
rounding or other execution rules remain candidates.

## Next defensible experiment

Test exchange-price/quantity rounding and TP construction using training cycles
only, then freeze those rules before repeating this diagnostic. Distinguish
nominal TP (for example 1.1% or 1.0% with tick rounding) from the current median
realized price ratio. Do not select a different price or TP for each failure.
Exported averages/close prices have rounding uncertainty and are not private
order logs. Historical instrument increments must be evidenced, not inferred
from current metadata alone. If the execution hypotheses remain ambiguous,
request per-order execution prices and order timestamps from the trader rather
than inventing an entry/re-entry filter to absorb the errors.

No improvement in the 73/80 fixed-entry score is claimed. This trace does not
identify autonomous entry/re-entry, DCA9–10, profitability or an optimum.
No runtime change, commit, push or PR update was made.
