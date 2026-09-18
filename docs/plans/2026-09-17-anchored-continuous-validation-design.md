# Anchored and Continuous Validation Design

Historical validation uses the runtime `DcaStrategy` through `ReplayEngine`; it does not
reimplement DCA triggers, sizing, or TP decisions. Replay now supports an explicit re-entry
delay, a no-re-entry mode, and a completed-cycle limit while preserving zero-delay defaults.
Completed replay records retain weighted-average entry and fill details for comparison.

Anchored validation creates one replay per observed cycle. It starts at the opening price of
the one-minute candle containing the trader's first entry, stops after one simulated basket,
and compares completion time, weighted-average entry, exit price, and DCA depth. This isolates
strategy behavior from cascading re-entry errors. The one-minute start price remains a proxy
because the export lacks an exact individual fill.

Continuous validation runs autonomously within each detected leverage/TP regime. A regime
starts at its first observed entry and ends shortly after its final observed close. Re-entry
uses an explicit delay, and replay state resets at regime boundaries so parameters never
change inside an open basket. Cycle alignment uses an order-preserving dynamic program that
maximizes matches inside the close-time window and then minimizes total close-time error.

The validation matrix covers low-first and high-first intrabar paths. A uniform scenario uses
the current reconstructed defaults. A descriptive regime-aware scenario changes only leverage
and TP to each regime's observed values while retaining the baseline DCA ladder. Reports
include p50/p90 errors, match rates, unmatched counts, exact depth matching, depth
distributions, and bounded mismatch diagnostics. No calibration, production-default change,
or live-trading action occurs in this phase.
