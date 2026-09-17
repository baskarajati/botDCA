from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from botdca.backtest import Candle
from botdca.marketdata import BybitKlineClient
from botdca.trader_export import TraderCycle, TraderExportData

MINUTE_MS = 60_000


def load_or_fetch_candles(
    client: BybitKlineClient,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str = "1",
    cache_dir: str | Path,
    refresh: bool = False,
) -> tuple[list[Candle], Path, bool]:
    """Load an exact-window candle cache or fetch and atomically replace it."""

    cache_root = Path(cache_dir).expanduser()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = cache_root / f"{symbol.upper()}-{interval}-{start_ms}-{end_ms}.json.gz"
    if cache_path.exists() and not refresh:
        with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("schema_version") != 1:
            raise ValueError(f"unsupported candle cache schema: {cache_path}")
        if (
            payload.get("symbol") != symbol.upper()
            or payload.get("interval") != interval
            or payload.get("start_ms") != start_ms
            or payload.get("end_ms") != end_ms
        ):
            raise ValueError(f"candle cache metadata does not match request: {cache_path}")
        candles = [Candle(**row) for row in payload.get("candles", [])]
        return candles, cache_path, True

    candles = client.fetch_linear_candles(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=interval,
    )
    payload = {
        "schema_version": 1,
        "symbol": symbol.upper(),
        "interval": interval,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "candles": [asdict(candle) for candle in candles],
    }
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    temporary.replace(cache_path)
    return candles, cache_path, False


def build_historical_profile(
    export: TraderExportData,
    *,
    candles: list[Candle] | None = None,
    tp_change_threshold_percent: float = 0.04,
) -> dict[str, Any]:
    """Build aggregate observed-behavior evidence without exposing private rows."""

    cycles = list(export.cycles)
    if not cycles:
        raise ValueError("at least one trader cycle is required")
    if tp_change_threshold_percent <= 0:
        raise ValueError("tp_change_threshold_percent must be positive")

    depth_counts = Counter(cycle.dca_count for cycle in cycles)
    maximum_depth = max(depth_counts)
    multipliers: dict[int, list[float]] = defaultdict(list)
    for cycle in cycles:
        for level, (previous, current) in enumerate(zip(cycle.orders, cycle.orders[1:]), start=1):
            multipliers[level].append(current.order_qty / previous.order_qty)

    reentry_gaps = [
        (current.opened_ms - previous.closed_ms) / 1000
        for previous, current in pairwise(cycles)
    ]
    overlap_count = sum(gap < 0 for gap in reentry_gaps)
    nonnegative_gaps = [gap for gap in reentry_gaps if gap >= 0]

    base_margin_proxy: dict[str, list[float]] = defaultdict(list)
    source_tz = ZoneInfo(export.timezone_name)
    for cycle in cycles:
        first_qty = cycle.orders[0].order_qty
        month = datetime.fromtimestamp(cycle.closed_ms / 1000, tz=UTC).astimezone(source_tz)
        key = f"{month:%Y-%m}|{cycle.leverage:g}x"
        base_margin_proxy[key].append(first_qty * cycle.average_entry / cycle.leverage)

    profile: dict[str, Any] = {
        "methodology": {
            "source_timezone": export.timezone_name,
            "cycle_grouping": (
                "same symbol, side, leverage, and final average entry; close fragments "
                "within configured time and price tolerances"
            ),
            "tp_percent": "(closing_price / final_weighted_average_entry - 1) * 100",
            "base_margin_proxy": (
                "first_order_qty * final_weighted_average_entry / leverage; approximate "
                "because individual fill price is not exported"
            ),
        },
        "dataset": {
            "source_rows": len(export.orders),
            "completed_cycles": len(cycles),
            "symbols": dict(sorted(Counter(order.symbol for order in export.orders).items())),
            "sides": dict(sorted(Counter(order.side for order in export.orders).items())),
            "opened_from_utc": _iso_ms(min(order.opened_ms for order in export.orders)),
            "closed_through_utc": _iso_ms(max(order.closed_ms for order in export.orders)),
            "grouping": asdict(export.grouping),
        },
        "leverage_cycle_counts": {
            f"{leverage:g}x": count
            for leverage, count in sorted(Counter(cycle.leverage for cycle in cycles).items())
        },
        "tp_percent": _numeric_summary([_tp_percent(cycle) for cycle in cycles]),
        "regimes": _detect_regimes(cycles, threshold=tp_change_threshold_percent),
        "dca_depth": {
            "exact_counts": {str(level): depth_counts.get(level, 0) for level in range(maximum_depth + 1)},
            "at_least_counts": {
                str(level): sum(cycle.dca_count >= level for cycle in cycles)
                for level in range(maximum_depth + 1)
            },
            "maximum_observed": maximum_depth,
        },
        "quantity_multiplier_from_previous": {
            str(level): _numeric_summary(values) for level, values in sorted(multipliers.items())
        },
        "reentry_gap_seconds": {
            "nonnegative": _numeric_summary(nonnegative_gaps),
            "overlap_count": overlap_count,
        },
        "holding_period_hours": _numeric_summary(
            [(cycle.closed_ms - cycle.opened_ms) / 3_600_000 for cycle in cycles]
        ),
        "approximate_base_margin_usdt_by_month_and_leverage": {
            key: _numeric_summary(values) for key, values in sorted(base_margin_proxy.items())
        },
    }
    if candles is not None:
        profile["market_alignment"] = build_market_alignment(export, candles)
    return profile


def build_market_alignment(
    export: TraderExportData,
    candles: list[Candle],
    *,
    price_tolerance_usdt: float = 0.003,
) -> dict[str, Any]:
    """Check exported entries/closures against complete one-minute price ranges."""

    if price_tolerance_usdt < 0:
        raise ValueError("price_tolerance_usdt cannot be negative")
    candle_by_minute = {candle.start_ms: candle for candle in candles}
    cycles = list(export.cycles)
    start_ms = min(order.opened_ms for order in export.orders) // MINUTE_MS * MINUTE_MS
    end_ms = max(order.closed_ms for order in export.orders) // MINUTE_MS * MINUTE_MS
    expected_minutes = (end_ms - start_ms) // MINUTE_MS + 1
    returned_minutes = sum(start_ms <= timestamp <= end_ms for timestamp in candle_by_minute)

    weighted_average_feasible = 0
    closing_price_in_range = 0
    weighted_midpoint_errors: list[float] = []
    missing_entry_rows = 0
    missing_close_cycles = 0
    trigger_ranges: dict[int, list[tuple[float, float]]] = defaultdict(list)
    trigger_midpoints: dict[int, list[float]] = defaultdict(list)

    for cycle in cycles:
        entry_candles: list[tuple[float, Candle]] = []
        for order in cycle.orders:
            candle = candle_by_minute.get(order.opened_ms // MINUTE_MS * MINUTE_MS)
            if candle is None:
                missing_entry_rows += 1
                continue
            if entry_candles:
                prior_qty = sum(quantity for quantity, _ in entry_candles)
                prior_low = sum(quantity * item.low for quantity, item in entry_candles) / prior_qty
                prior_high = sum(quantity * item.high for quantity, item in entry_candles) / prior_qty
                lower_drop = (1 - candle.high / prior_low) * 100
                upper_drop = (1 - candle.low / prior_high) * 100
                midpoint_drop = (
                    1
                    - ((candle.low + candle.high) / 2)
                    / ((prior_low + prior_high) / 2)
                ) * 100
                level = len(entry_candles)
                trigger_ranges[level].append((lower_drop, upper_drop))
                trigger_midpoints[level].append(midpoint_drop)
            entry_candles.append((order.order_qty, candle))

        if len(entry_candles) == len(cycle.orders):
            total_qty = sum(quantity for quantity, _ in entry_candles)
            lower_average = sum(quantity * item.low for quantity, item in entry_candles) / total_qty
            upper_average = sum(quantity * item.high for quantity, item in entry_candles) / total_qty
            midpoint = (lower_average + upper_average) / 2
            if (
                lower_average - price_tolerance_usdt
                <= cycle.average_entry
                <= upper_average + price_tolerance_usdt
            ):
                weighted_average_feasible += 1
            weighted_midpoint_errors.append(
                abs(midpoint - cycle.average_entry) / cycle.average_entry * 100
            )

        close_candle = candle_by_minute.get(cycle.closed_ms // MINUTE_MS * MINUTE_MS)
        if close_candle is None:
            missing_close_cycles += 1
        elif (
            close_candle.low - price_tolerance_usdt
            <= cycle.closing_price
            <= close_candle.high + price_tolerance_usdt
        ):
            closing_price_in_range += 1

    return {
        "requested_start_ms": start_ms,
        "requested_end_ms": end_ms,
        "expected_minutes": expected_minutes,
        "returned_unique_minutes": returned_minutes,
        "missing_market_minutes": expected_minutes - returned_minutes,
        "missing_entry_rows": missing_entry_rows,
        "missing_close_cycles": missing_close_cycles,
        "weighted_average_feasible_cycles": weighted_average_feasible,
        "weighted_average_feasible_percent": weighted_average_feasible / len(cycles) * 100,
        "closing_price_in_range_cycles": closing_price_in_range,
        "closing_price_in_range_percent": closing_price_in_range / len(cycles) * 100,
        "weighted_average_midpoint_absolute_error_percent": _numeric_summary(
            weighted_midpoint_errors
        ),
        "dca_trigger_drop_evidence_percent": {
            str(level): {
                "samples": len(ranges),
                "lower_bound": _numeric_summary([lower for lower, _ in ranges]),
                "upper_bound": _numeric_summary([upper for _, upper in ranges]),
                "midpoint_proxy": _numeric_summary(trigger_midpoints[level]),
            }
            for level, ranges in sorted(trigger_ranges.items())
        },
        "limitations": [
            "One-minute candles bound fill prices but do not reveal exact second-level fills.",
            "Candle midpoint trigger estimates are diagnostics, not exact observed trigger prices.",
        ],
    }


def _detect_regimes(cycles: list[TraderCycle], *, threshold: float) -> list[dict[str, Any]]:
    runs = split_regimes(cycles, threshold=threshold)

    return [
        {
            "index": index,
            "leverage": run[0].leverage,
            "opened_from_utc": _iso_ms(run[0].opened_ms),
            "closed_through_utc": _iso_ms(run[-1].closed_ms),
            "completed_cycles": len(run),
            "tp_percent": _numeric_summary([_tp_percent(cycle) for cycle in run]),
            "maximum_dca_depth": max(cycle.dca_count for cycle in run),
        }
        for index, run in enumerate(runs, start=1)
    ]


def split_regimes(
    cycles: list[TraderCycle],
    *,
    threshold: float = 0.04,
) -> list[list[TraderCycle]]:
    """Split chronological cycles at leverage or material TP changes."""

    if threshold <= 0:
        raise ValueError("threshold must be positive")
    runs: list[list[TraderCycle]] = []
    for cycle in cycles:
        if not runs:
            runs.append([cycle])
            continue
        current = runs[-1]
        current_tp = median(_tp_percent(item) for item in current)
        if cycle.leverage != current[-1].leverage or abs(_tp_percent(cycle) - current_tp) > threshold:
            runs.append([cycle])
        else:
            current.append(cycle)
    return runs


def _tp_percent(cycle: TraderCycle) -> float:
    return (cycle.closing_price / cycle.average_entry - 1) * 100


def _numeric_summary(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "minimum": ordered[0],
        "p10": _quantile(ordered, 0.10),
        "median": _quantile(ordered, 0.50),
        "p90": _quantile(ordered, 0.90),
        "maximum": ordered[-1],
    }


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _iso_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
