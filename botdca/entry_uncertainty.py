"""Empirical entry sensitivity bounds, not inferred private executions."""

from botdca.backtest import Candle


def training_sample(regime, *, fraction=0.7, count=20):
    split = int(len(regime) * fraction)
    sample = [c for c in regime[:split] if c.dca_count == 0][-count:]
    if len(sample) < count:
        raise ValueError("insufficient depth-zero training cycles")
    return sample


def learn_entry_bounds(training, candles_by_open, *, display_half_width=0.0005):
    if not training:
        raise ValueError("training observations required")
    rows = []
    for cycle in training:
        candles: list[Candle] = candles_by_open[cycle.opened_ms]
        later = [c for c in candles if c.start_ms >= cycle.opened_ms]
        if not later:
            raise ValueError("missing training entry prices")
        first = later[0].open
        low = cycle.average_entry - display_half_width
        high = cycle.average_entry + display_half_width
        witnesses = [
            c.start_ms - cycle.opened_ms for c in candles if c.low <= high and c.high >= low
        ]
        lag = min(witnesses, key=lambda v: (abs(v), v)) if witnesses else None
        rows.append(
            {
                "opened_ms": cycle.opened_ms,
                "first_public_price": first,
                "exported_average": cycle.average_entry,
                "absolute_relative_price_bound": max(abs(low / first - 1), abs(high / first - 1)),
                "nearest_price_range_witness_lag_ms": lag,
            }
        )
    censored = sum(r["nearest_price_range_witness_lag_ms"] is None for r in rows)
    return {
        "training_cycles": len(rows),
        "relative_price_bound": max(r["absolute_relative_price_bound"] for r in rows),
        "timing_proxy_bound_ms": None
        if censored
        else max(abs(r["nearest_price_range_witness_lag_ms"]) for r in rows),
        "timing_censored_cycles": censored,
        "actual_fill_timing_identified": False,
        "rows": rows,
    }
