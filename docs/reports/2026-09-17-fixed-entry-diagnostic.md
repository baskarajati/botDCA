# Fixed public-trade entry diagnostic

This local run replayed all 80 previously opened holdout cycles, not just the
earlier failures. Entry was fixed to the first archived public trade at or
after the CSV timestamp. The training-only regime TP and weighted-average DCA
ladder were unchanged. Subsequent trades were aggregated to one-second OHLC;
both intrasecond paths and the 300-second close tolerance were retained.

| Metric | Low-first | High-first |
| --- | ---: | ---: |
| One-minute close-only baseline | 70/80 | 71/80 |
| Fixed-entry one-second close-only matches | 73/80 | 73/80 |
| Fixed-entry joint close-and-depth matches | 73/80 | 73/80 |
| Fixed-entry joint match rate | 91.25% | 91.25% |

Thirty archive files totaling about 385 MB compressed were integrity checked.
The audit scanned 9,935,007 public trades. No timestamp-order defects were
reported and retained-window trade IDs had no duplicates. Required entry,
order, and close seconds were present. These checks do not independently prove
exchange archive completeness or executable private fills.

Seven cycles did not match. Two had previously matched the minute-level
baseline, so the improvement is not uniform. Five previously unresolved cycles
matched under the fixed rule. The previously reported 96.25% figure remains a
hindsight feasibility ceiling, not the achieved fixed-rule result.

The generated JSON's `fixed_entry_metrics` is the authoritative summary for
this mode. Legacy envelope fields such as `unique_minute_level_unresolved_cycles`
refer to all selected cycles in this mode, not the actual number of minute-level
failures; do not interpret their names as the fixed-rule denominator.

This is a diagnostic on already opened data with CSV-supplied entry times. It
does not establish an autonomous entry/re-entry signal, profitability, safety,
or untouched validation. Runtime defaults were not changed. No commit, push,
or PR update was made. Later unseen trades are still needed for final validation.
