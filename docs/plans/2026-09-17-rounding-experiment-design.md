# Training-only execution precision experiment

User approved testing price/quantity rounding and TP construction, not runtime
implementation or PR work. Use the same eligible regimes and chronological 70%
training splits. Existing research changes stay uncommitted.

Compare three approaches: retain the median realized TP (control); nominal TP
rounded to the nearest 0.1 percentage point plus a finite price-rounding grid
(recommended); or per-cycle fitted TP (rejected because it would use answers
from the failures). Candidate increments are unrounded, 0.001 and 0.01; rounding
directions floor, nearest-half-up and ceiling. Select minimum training mean
absolute exported close-price error, with stable simpler-first ties. Final
export averages are explanatory training inputs, not available entry signals.

Check historical public price/size grids on training dates. Observed grids are
supporting evidence, not a recovered official historical instrument snapshot.
Do not use current metadata as historical proof. Public execution grids can be
finer than order increments or aggregate execution sizes.

Freeze the selected TP construction per regime and replay only the seven
previously seen failures against unchanged first-print entries, DCA thresholds,
quantities and fees, using both paths and the same 300-second tolerance. Record
the baseline beside the experiment. No new 80-cycle score is inferred from this
failure-only run, since previously successful cycles may regress.

Analyze quantity rounding separately: predict each training addition from the
exported previous quantity and frozen multiplier, using the observed exported
quantity grid. Report errors for unrounded/floor/nearest/ceiling. Do not apply
this physical quantity increment to the normalized one-USDT replay without a
defensible trader-scale sizing model. Do not change DCA admission simultaneously.

Decimal arithmetic avoids binary rounding artifacts. Tests cover construction,
training selection, research-only replay and unchanged entry/ladder behavior.
Missing archive coverage or regime mappings fail loudly. Outputs derived from
the private CSV remain outside the repository; only aggregate findings go in a
report. Verify tests, lint and diff checks before reporting completion.
