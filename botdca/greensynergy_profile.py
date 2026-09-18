"""GreenSynergy-specific historical profiling.

Kept strictly separate from the Zuya tooling. The two traders are different
systems, so their datasets are never mixed and the Zuya grouping defaults used
by PR #3's validation are left untouched.

Two things this module does that the Zuya path cannot:

1. The GreenSynergy export holds several symbols. `trader_export._load_orders`
   raises on a foreign symbol rather than filtering, and the overlap check is
   global, so the export is partitioned per symbol before grouping.

2. Rows belonging to one Bybit position share a final weighted-average entry,
   but the export prints it with differing trailing digits, and close fragments
   can be minutes apart. The Zuya grouping compares average entry exactly and
   allows five seconds of close skew, which splits many GreenSynergy baskets.
   Grouping here uses relative tolerances instead.

Everything produced here is descriptive research output. None of it is an
execution rule, and none of it makes the reconstruction validated.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from botdca.strategy_version import StrategyVersion
from botdca.trader_export import TraderOrder, _load_orders, _named_timezone

#: Relative tolerance on the shared final weighted-average entry of one basket.
DEFAULT_AVERAGE_ENTRY_RELATIVE_TOLERANCE = 1e-6
#: Relative tolerance on the shared closing price of one basket.
DEFAULT_CLOSE_PRICE_RELATIVE_TOLERANCE = 1e-4
#: Close fragments of one basket may be reported minutes apart.
DEFAULT_CLOSE_TIME_TOLERANCE_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class GreenSynergyBasket:
    symbol: str
    opened_ms: int
    closed_ms: int
    average_entry: float
    closing_price: float
    leverage: float
    order_quantities: tuple[float, ...]

    @property
    def dca_depth(self) -> int:
        return len(self.order_quantities) - 1

    @property
    def total_qty(self) -> float:
        return sum(self.order_quantities)

    @property
    def take_profit_percent(self) -> float:
        """Close price as a percentage above the final weighted-average entry."""
        return (self.closing_price / self.average_entry - 1.0) * 100.0

    @property
    def holding_seconds(self) -> float:
        return (self.closed_ms - self.opened_ms) / 1000.0

    def initial_margin_usdt(self, leverage: float | None = None) -> float:
        used = leverage or self.leverage
        return self.order_quantities[0] * self.average_entry / used

    @property
    def size_multipliers(self) -> tuple[float, ...]:
        return tuple(
            later / earlier
            for earlier, later in zip(self.order_quantities, self.order_quantities[1:])
            if earlier > 0
        )


@dataclass
class SymbolProfile:
    symbol: str
    source_rows: int
    baskets: list[GreenSynergyBasket] = field(default_factory=list)
    overlapping_baskets: int = 0

    def describe(self) -> dict[str, Any]:
        if not self.baskets:
            return {
                "symbol": self.symbol,
                "source_rows": self.source_rows,
                "basket_count": 0,
            }
        depths = [basket.dca_depth for basket in self.baskets]
        tps = [basket.take_profit_percent for basket in self.baskets]
        holds = [basket.holding_seconds for basket in self.baskets]
        multipliers = [m for basket in self.baskets for m in basket.size_multipliers]
        initial_margins = [basket.initial_margin_usdt() for basket in self.baskets]
        initial_quantities = [basket.order_quantities[0] for basket in self.baskets]

        by_level: dict[int, list[float]] = defaultdict(list)
        for basket in self.baskets:
            for index, multiplier in enumerate(basket.size_multipliers, start=1):
                by_level[index].append(multiplier)

        ordered = sorted(self.baskets, key=lambda b: b.opened_ms)
        gaps = [
            (later.opened_ms - earlier.closed_ms) / 1000.0
            for earlier, later in pairwise(ordered)
            if later.opened_ms >= earlier.closed_ms
        ]

        total = len(self.baskets)
        return {
            "symbol": self.symbol,
            "source_rows": self.source_rows,
            "basket_count": total,
            "overlapping_baskets": self.overlapping_baskets,
            "max_observed_dca": max(depths),
            "dca_depth_distribution": {
                str(level): count for level, count in sorted(Counter(depths).items())
            },
            "probability_reaching_level": {
                str(level): sum(1 for d in depths if d >= level) / total
                for level in range(1, max(depths) + 1)
            },
            "take_profit_percent": _summary(tps),
            "size_multiplier": _summary(multipliers),
            "size_multiplier_by_level": {
                str(level): _summary(values)
                for level, values in sorted(by_level.items())
                if len(values) >= 3
            },
            "reentry_gap_seconds": {
                **_summary(gaps),
                "within_5_minutes_fraction": (
                    sum(1 for gap in gaps if gap <= 300) / len(gaps) if gaps else None
                ),
            },
            "holding_seconds": _summary(holds),
            "initial_margin_usdt": _summary(initial_margins),
            "initial_quantity": _summary(initial_quantities),
        }


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "p10": None, "p90": None, "min": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "median": median(ordered),
        "p10": ordered[len(ordered) // 10],
        "p90": ordered[min(len(ordered) - 1, len(ordered) * 9 // 10)],
        "min": ordered[0],
        "max": ordered[-1],
    }


def split_export_by_symbol(
    path: str | Path, destination: Path, *, leverage_filter: str | None = None
) -> dict[str, Path]:
    """Partition a multi-symbol trader export into one CSV per symbol.

    The private export must stay outside Git; `destination` is a caller-supplied
    working directory, never a repository path.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return {}
    fieldnames = list(rows[0].keys())
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if leverage_filter and row.get("margin_and_leverage") != leverage_filter:
            continue
        grouped[str(row.get("position_symbol", "")).upper()].append(row)

    written: dict[str, Path] = {}
    for symbol, symbol_rows in grouped.items():
        if not symbol:
            continue
        target = destination / f"{symbol}.csv"
        with open(target, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(symbol_rows)
        written[symbol] = target
    return written


def group_baskets(
    orders: list[TraderOrder],
    *,
    average_entry_relative_tolerance: float = DEFAULT_AVERAGE_ENTRY_RELATIVE_TOLERANCE,
    close_price_relative_tolerance: float = DEFAULT_CLOSE_PRICE_RELATIVE_TOLERANCE,
    close_time_tolerance_seconds: float = DEFAULT_CLOSE_TIME_TOLERANCE_SECONDS,
) -> list[GreenSynergyBasket]:
    """Group export rows into baskets using relative tolerances.

    Every row of one Bybit position reports that position's final weighted
    average entry and its closing price, so those two values plus a bounded
    close time identify the basket.
    """
    buckets: list[list[TraderOrder]] = []
    for order in sorted(orders, key=lambda o: (o.closed_ms, o.opened_ms)):
        for bucket in buckets:
            reference = bucket[0]
            if (
                abs(order.average_entry - reference.average_entry)
                <= average_entry_relative_tolerance * max(abs(reference.average_entry), 1e-12)
                and abs(order.closing_price - reference.closing_price)
                <= close_price_relative_tolerance * max(abs(reference.closing_price), 1e-12)
                and abs(order.closed_ms - reference.closed_ms)
                <= close_time_tolerance_seconds * 1000
                and order.symbol == reference.symbol
                and order.side == reference.side
            ):
                bucket.append(order)
                break
        else:
            buckets.append([order])

    baskets: list[GreenSynergyBasket] = []
    for bucket in buckets:
        ordered = sorted(bucket, key=lambda o: o.opened_ms)
        baskets.append(
            GreenSynergyBasket(
                symbol=ordered[0].symbol,
                opened_ms=ordered[0].opened_ms,
                closed_ms=max(order.closed_ms for order in ordered),
                average_entry=ordered[0].average_entry,
                closing_price=ordered[0].closing_price,
                leverage=ordered[0].leverage,
                order_quantities=tuple(order.order_qty for order in ordered),
            )
        )
    baskets.sort(key=lambda basket: (basket.opened_ms, basket.closed_ms))
    return baskets


def profile_symbol_export(
    path: str | Path,
    symbol: str,
    *,
    timezone_name: str = "UTC",
    side: str = "Long",
    **grouping: float,
) -> SymbolProfile:
    orders = _load_orders(
        path,
        tz=_named_timezone(timezone_name),
        expected_symbol=symbol.upper(),
        expected_side=side,
    )
    baskets = group_baskets(orders, **grouping)
    overlaps = sum(
        1
        for earlier, later in pairwise(baskets)
        if later.opened_ms < earlier.closed_ms
    )
    return SymbolProfile(
        symbol=symbol.upper(),
        source_rows=len(orders),
        baskets=baskets,
        overlapping_baskets=overlaps,
    )


def reconstruct_ladder_fit(
    profile: SymbolProfile, version: StrategyVersion
) -> dict[str, Any]:
    """Score a strategy version's ladder against what the export actually shows.

    IMPORTANT LIMITATION. The trader export does not contain per-fill prices. It
    reports each order's quantity and timestamp, and every row of one basket
    repeats that basket's FINAL weighted-average entry. A ladder replay is
    scale-invariant in price, so any first-fill price can be solved for exactly
    and a "weighted-average reconstruction error" computed this way is
    identically zero by construction. Trigger-ladder accuracy therefore cannot
    be measured from this file alone; it needs per-second market data, which is
    what the separate entry-identification tooling is for.

    What this export CAN score is the quantity rule, which is reported directly.
    """
    multiplier = version.dca_size_multiplier
    per_level: dict[int, list[float]] = defaultdict(list)
    relative_errors: list[float] = []
    for basket in profile.baskets:
        for index, observed in enumerate(basket.size_multipliers, start=1):
            per_level[index].append(observed)
            relative_errors.append(abs(observed / multiplier - 1.0) * 100.0)

    steps = version.research_dca_steps()
    implied: list[float] = []
    for basket in profile.baskets:
        if not 1 <= basket.dca_depth <= len(steps):
            continue
        # Total weighted-average drawdown the ladder implies for this basket's
        # own observed quantities, expressed relative to the first fill price.
        total_qty = basket.order_quantities[0]
        total_notional = 1.0 * basket.order_quantities[0]
        for index, qty in enumerate(basket.order_quantities[1:]):
            fill = (total_notional / total_qty) * (
                1 - steps[index].drop_percent_from_average / 100
            )
            total_qty += qty
            total_notional += fill * qty
        implied.append((total_notional / total_qty - 1.0) * 100.0)

    return {
        "strategy_version_id": version.version_id,
        "expected_size_multiplier": multiplier,
        "observed_size_multiplier": _summary(
            [m for values in per_level.values() for m in values]
        ),
        "size_multiplier_relative_error_percent": _summary(relative_errors),
        "size_multiplier_by_level": {
            str(level): _summary(values)
            for level, values in sorted(per_level.items())
            if len(values) >= 3
        },
        "implied_weighted_average_drawdown_percent": _summary(implied),
        "scored_baskets": len(implied),
        "trigger_ladder_measurable_from_export": False,
        "note": (
            "Per-fill prices are not exported, so trigger-ladder accuracy is NOT "
            "measurable from this file. Only the quantity rule is scored here. The "
            "implied drawdown is what the version's ladder would produce for each "
            "basket's own observed quantities, relative to its first fill."
        ),
    }


def simultaneous_deep_periods(
    profiles: list[SymbolProfile], *, deep_dca_level: int = 5
) -> dict[str, Any]:
    """When were several symbols simultaneously deep? The correlated tail risk."""
    intervals: list[tuple[int, int, str]] = [
        (basket.opened_ms, basket.closed_ms, profile.symbol)
        for profile in profiles
        for basket in profile.baskets
        if basket.dca_depth >= deep_dca_level
    ]
    overlaps: list[dict[str, Any]] = []
    for index, (start_a, end_a, symbol_a) in enumerate(intervals):
        for start_b, end_b, symbol_b in intervals[index + 1 :]:
            if symbol_a == symbol_b:
                continue
            start, end = max(start_a, start_b), min(end_a, end_b)
            if start < end:
                overlaps.append(
                    {
                        "symbols": sorted((symbol_a, symbol_b)),
                        "overlap_seconds": (end - start) / 1000.0,
                        "start_ms": start,
                        "end_ms": end,
                    }
                )
    overlaps.sort(key=lambda row: row["overlap_seconds"], reverse=True)
    return {
        "deep_dca_level": deep_dca_level,
        "deep_basket_count": len(intervals),
        "overlapping_pair_count": len(overlaps),
        "longest_overlaps": overlaps[:10],
    }


def portfolio_capital_expansion(profiles: list[SymbolProfile]) -> dict[str, Any]:
    """Observed committed capital growth from initial fill to deepest basket."""
    rows = {}
    for profile in profiles:
        if not profile.baskets:
            continue
        deepest = max(profile.baskets, key=lambda basket: basket.dca_depth)
        initial = deepest.order_quantities[0] * deepest.average_entry / deepest.leverage
        full = deepest.total_qty * deepest.average_entry / deepest.leverage
        rows[profile.symbol] = {
            "max_observed_dca": deepest.dca_depth,
            "initial_margin_usdt": initial,
            "deepest_basket_margin_usdt": full,
            "expansion_multiple": (full / initial) if initial > 0 else None,
        }
    return {
        "per_symbol": rows,
        "note": (
            "Approximate: computed at the basket's final weighted-average entry, not "
            "at each individual fill price."
        ),
    }
