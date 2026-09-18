# Training-supported entry uncertainty diagnostic

No private execution export was found in the local data directory. User approved
the fallback uncertainty experiment. No live/runtime changes, commit, push or
PR changes are permitted.

Use the last 20 depth-zero cycles within each eligible regime's chronological
70% training segment. Depth-zero final average is an observed initial-position
average; deeper final averages cannot be used to learn seed prices independently.
Download the missing daily public archives for these training dates. Selecting
the last 20 is a predeclared bounded research sample, not complete training data.

Measure the absolute relative discrepancy between exported average (with
plus/minus 0.0005 USDT display uncertainty) and first public print at/after the
exported second. Freeze its training-sample maximum as a symmetric price box.
This is an empirical conditional feasibility box, not an execution guarantee,
confidence interval, slippage estimate or inferred trading rule.

For timing, inspect plus/minus 60 seconds around each training entry. Find the
nearest second whose public price range intersects the exported average's
display interval. This is a public-price occurrence proxy, not an actual fill
timestamp. If any training case lacks a witness, do not infer a timing bound.
Otherwise freeze the largest absolute witness lag as a proxy sensitivity range.
The 60-second search window is a censoring limit, not a learned bound.

Alternatives are a private-fill reconstruction (preferred but unavailable), a
bounded feasibility diagnostic (chosen), and arbitrary per-failure fitting
(rejected). Keep median training TP, ladder, quantities and fees unchanged.
On each prior failure test price offsets at -1, -0.5, 0, 0.5, 1 times the learned
relative price bound, and separately first-print entries at -bound, zero,
+bound timing offsets where timing has an uncensored witness range. Do not
combine factors or select a single deployable winner. Test both intrasecond
paths and the original 300-second joint close/depth tolerance.

Recoverable means some predeclared grid candidate matches; unresolved means no
tested candidate matches, not that no continuous-range solution exists. Per-case
selection is hindsight feasibility only. Do not turn failure-only results into
a new 80-cycle score. Preserve sample/censoring, coverage and private-fill limits.
Tests must cover training-only sampling, bound calculation, censoring and
baseline mechanics. Private derived output stays outside the repository.
