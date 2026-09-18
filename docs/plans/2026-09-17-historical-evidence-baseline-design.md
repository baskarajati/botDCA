# Historical Evidence Baseline Design

PR #3 begins with an evidence layer rather than parameter optimization. The private Bybit
trader export remains outside Git. A parser converts its naive timestamps from an explicit
IANA timezone to UTC, validates required fields and domain values, and retains every source
row as a structured order. Completed cycles are grouped using final average entry, symbol,
side, and leverage. Close executions may be merged only when their timestamps and rounded
prices fall inside reviewable tolerances. Grouping diagnostics report the exact-key count,
corrected cycle count, merged fragments, and any remaining overlap.

An aggregate profiler reports chronological leverage/TP regimes, DCA-depth frequencies,
quantity multipliers with sample sizes, re-entry timing, holding periods, and an explicitly
approximate base-margin proxy. Regime boundaries are deterministic: leverage changes always
start a new regime, while TP changes use a configurable percentage-point threshold. No
calibration occurs in this layer.

Public Bybit one-minute linear-perpetual candles are fetched for the exact UTC coverage
window and cached as versioned gzip JSON outside the repository. The alignment check verifies
that each reported final weighted average is feasible within quantity-weighted entry-minute
ranges and that each closing price is inside its closing-minute range. It also records bounded
DCA-trigger evidence while labeling candle midpoints as diagnostic proxies, not exact fills.

The CLI emits aggregate JSON and a concise terminal summary. It never copies private rows
into the output. Synthetic tests cover timezone conversion, close-fragment grouping, symbol
validation, DCA metrics, market alignment, deterministic regime profiling, and cache reuse.
Live-trading settings and runtime strategy defaults remain unchanged.
