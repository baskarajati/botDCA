from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from math import comb
from statistics import median
from typing import Any

from botdca.backtest import Candle
from botdca.historical_profile import MINUTE_MS, split_regimes
from botdca.trader_export import TraderCycle, TraderExportData

MIN_DEEP_LEVEL_SAMPLES_FOR_RUNTIME = 10


def identify_entry_and_deep_dca_behavior(
    export: TraderExportData,
    candles: list[Candle],
    *,
    train_fraction: float = 0.70,
    fixed_delay_seconds: float = 48.0,
    deep_dca_threshold: int = 8,
    long_cooldown_skipped_minutes: int = 5,
    price_tolerance_usdt: float = 0.0,
    tp_change_threshold_percent: float = 0.04,
) -> dict[str, Any]:
    """Identify entry timing and bound sparse DCA9-10 behavior."""

    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    if fixed_delay_seconds < 0:
        raise ValueError("fixed_delay_seconds cannot be negative")
    if deep_dca_threshold < 1:
        raise ValueError("deep_dca_threshold must be positive")
    if long_cooldown_skipped_minutes < 0:
        raise ValueError("long_cooldown_skipped_minutes cannot be negative")
    if price_tolerance_usdt < 0:
        raise ValueError("price_tolerance_usdt cannot be negative")
    if not export.cycles:
        raise ValueError("at least one trader cycle is required")
    if not candles:
        raise ValueError("at least one candle is required")

    candle_by_minute = {candle.start_ms: candle for candle in candles}
    regimes = split_regimes(list(export.cycles), threshold=tp_change_threshold_percent)
    regime_by_cycle = {
        id(cycle): index
        for index, regime in enumerate(regimes, start=1)
        for cycle in regime
    }
    transitions = _build_transitions(list(export.cycles))
    split_index = (
        max(1, min(len(transitions) - 1, int(len(transitions) * train_fraction)))
        if len(transitions) >= 2
        else len(transitions)
    )
    training = transitions[:split_index]
    holdout = transitions[split_index:]
    training_immediate = [item for item in training if item["skipped_minutes"] == 0]
    phase_source = training_immediate or training
    phase_second = (
        median(item["next_open_second"] for item in phase_source)
        if phase_source
        else 0.0
    )

    timing = {
        "training": _timing_split_summary(
            training,
            phase_second=phase_second,
            fixed_delay_seconds=fixed_delay_seconds,
        ),
        "holdout": _timing_split_summary(
            holdout,
            phase_second=phase_second,
            fixed_delay_seconds=fixed_delay_seconds,
        ),
        "all_transitions": _timing_split_summary(
            transitions,
            phase_second=phase_second,
            fixed_delay_seconds=fixed_delay_seconds,
        ),
        "trained_scheduler_phase_second": phase_second,
        "skipped_minute_distribution": _skip_distribution(transitions),
    }
    holdout_summary = timing["holdout"]
    scheduler_error = holdout_summary["scheduler_model_absolute_error_seconds"]
    fixed_error = holdout_summary["fixed_delay_model_absolute_error_seconds"]
    next_candle_proxy_supported = (
        scheduler_error is not None
        and fixed_error is not None
        and scheduler_error["median"] < fixed_error["median"]
        and holdout_summary["immediate_next_minute_percent"] >= 70.0
    )

    deep_cooldown = _deep_cooldown_summary(
        transitions,
        deep_dca_threshold=deep_dca_threshold,
        long_cooldown_skipped_minutes=long_cooldown_skipped_minutes,
    )
    first_entry = _first_entry_price_summary(export, candle_by_minute)
    deep_cycles, deep_summary = _deep_dca_evidence(
        export,
        candle_by_minute,
        regime_by_cycle=regime_by_cycle,
        price_tolerance_usdt=price_tolerance_usdt,
    )
    duplicate_open_cycles = sum(
        len({order.opened_ms for order in cycle.orders}) < len(cycle.orders)
        for cycle in export.cycles
    )
    fixed_delay_supported = (
        scheduler_error is not None
        and fixed_error is not None
        and fixed_error["median"] <= scheduler_error["median"]
    )
    cooldown_probability = deep_cooldown["one_sided_fisher_exact_probability"]
    cooldown_association = (
        deep_cooldown["deep_long_cooldown_percent"]
        > deep_cooldown["shallow_long_cooldown_percent"]
        and cooldown_probability is not None
        and cooldown_probability < 0.01
    )

    return {
        "methodology": {
            "timing_split": (
                f"first {train_fraction * 100:g} percent discovery, final "
                f"{(1 - train_fraction) * 100:g} percent chronological holdout"
            ),
            "fixed_delay_comparator_seconds": fixed_delay_seconds,
            "scheduler_model": (
                "next UTC minute boundary plus median second-of-minute from immediate "
                "training transitions"
            ),
            "deep_cooldown_definition": (
                f"prior DCA depth >= {deep_dca_threshold} and more than "
                f"{long_cooldown_skipped_minutes} skipped minute scans"
            ),
            "fill_bound_method": (
                "entry-minute candle ranges tightened by the exported final weighted-average "
                "linear constraint"
            ),
            "minimum_regime_samples_for_runtime_candidate": (
                MIN_DEEP_LEVEL_SAMPLES_FOR_RUNTIME
            ),
            "limitations": [
                "The export repeats final weighted-average entry on every row.",
                "Exact individual fill prices are identifiable only for DCA0 cycles.",
                "One-minute candles cannot reveal second-level execution prices.",
                "Cooldown association does not identify the hidden admission condition.",
            ],
        },
        "data_quality": {
            "source_rows": len(export.orders),
            "grouped_cycles": len(export.cycles),
            "close_fragment_groups_merged": export.grouping.close_fragment_groups_merged,
            "cycles_with_duplicate_open_timestamp": duplicate_open_cycles,
            "missing_entry_minute_candles": sum(
                order.opened_ms // MINUTE_MS * MINUTE_MS not in candle_by_minute
                for order in export.orders
            ),
            "findings": [
                {
                    "severity": "high",
                    "confidence": "high",
                    "issue": "individual layered fill prices are absent",
                    "impact": "DCA trigger values can be bounded but not exactly reconstructed",
                },
                {
                    "severity": "high",
                    "confidence": "high",
                    "issue": "DCA9 and DCA10 are sparse and regime-dependent",
                    "impact": "insufficient evidence to add production ladder levels",
                },
                {
                    "severity": "medium",
                    "confidence": "high",
                    "issue": "market evidence has one-minute grain",
                    "impact": "entry phase is identifiable, exact second-level price is not",
                },
            ],
        },
        "reentry_timing": timing,
        "deep_cycle_cooldown": deep_cooldown,
        "first_entry_price": first_entry,
        "deep_dca_cycles": deep_cycles,
        "deep_dca_summary": deep_summary,
        "conclusion": {
            "literal_fixed_delay_rule": (
                "supported" if fixed_delay_supported else "not_supported"
            ),
            "fixed_delay_seconds_tested": fixed_delay_seconds,
            "next_candle_open_replay_proxy": (
                "supported" if next_candle_proxy_supported else "not_supported"
            ),
            "best_supported_reentry_hypothesis": (
                "minute-scheduled attempt with unobserved admission or pause condition"
                if next_candle_proxy_supported
                else "inconclusive"
            ),
            "deep_cycle_cooldown": (
                "strong_association_low_sample"
                if cooldown_association
                else "inconclusive_low_sample"
            ),
            "mature_dca9": "capped_emergency_add_candidate_insufficient_samples",
            "older_20x_dca10": "distinct_regime_insufficient_samples",
            "runtime_defaults_changed": False,
        },
    }


def _build_transitions(cycles: list[TraderCycle]) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    for previous, current in pairwise(cycles):
        next_minute_ms = (previous.closed_ms // MINUTE_MS + 1) * MINUTE_MS
        scheduler_residual_seconds = (current.opened_ms - next_minute_ms) / 1000
        transitions.append(
            {
                "previous_closed_ms": previous.closed_ms,
                "next_opened_ms": current.opened_ms,
                "gap_seconds": (current.opened_ms - previous.closed_ms) / 1000,
                "next_minute_ms": next_minute_ms,
                "scheduler_residual_seconds": scheduler_residual_seconds,
                "skipped_minutes": max(0, int(scheduler_residual_seconds // 60)),
                "next_open_second": int(current.opened_ms / 1000) % 60,
                "previous_dca_depth": previous.dca_count,
            }
        )
    return transitions


def _timing_split_summary(
    transitions: list[dict[str, Any]],
    *,
    phase_second: float,
    fixed_delay_seconds: float,
) -> dict[str, Any]:
    fixed_errors = [
        abs(
            item["next_opened_ms"]
            - (item["previous_closed_ms"] + fixed_delay_seconds * 1000)
        )
        / 1000
        for item in transitions
    ]
    scheduler_errors = [
        abs(
            item["next_opened_ms"]
            - (item["next_minute_ms"] + phase_second * 1000)
        )
        / 1000
        for item in transitions
    ]
    immediate = [item for item in transitions if item["skipped_minutes"] == 0]
    return {
        "transitions": len(transitions),
        "immediate_next_minute": len(immediate),
        "immediate_next_minute_percent": (
            len(immediate) / len(transitions) * 100 if transitions else 0.0
        ),
        "immediate_open_at_second_0_to_10_percent": (
            sum(item["next_open_second"] <= 10 for item in immediate)
            / len(immediate)
            * 100
            if immediate
            else 0.0
        ),
        "gap_seconds": _numeric_summary([item["gap_seconds"] for item in transitions]),
        "fixed_delay_model_absolute_error_seconds": _error_summary(fixed_errors),
        "scheduler_model_absolute_error_seconds": _error_summary(scheduler_errors),
    }


def _skip_distribution(transitions: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(item["skipped_minutes"] for item in transitions)
    return {
        "0": counts.get(0, 0),
        "1": counts.get(1, 0),
        "2": counts.get(2, 0),
        "3_to_5": sum(count for skips, count in counts.items() if 3 <= skips <= 5),
        "6_to_30": sum(count for skips, count in counts.items() if 6 <= skips <= 30),
        "over_30": sum(count for skips, count in counts.items() if skips > 30),
    }


def _deep_cooldown_summary(
    transitions: list[dict[str, Any]],
    *,
    deep_dca_threshold: int,
    long_cooldown_skipped_minutes: int,
) -> dict[str, Any]:
    deep = [item for item in transitions if item["previous_dca_depth"] >= deep_dca_threshold]
    shallow = [item for item in transitions if item["previous_dca_depth"] < deep_dca_threshold]
    deep_long = sum(
        item["skipped_minutes"] > long_cooldown_skipped_minutes for item in deep
    )
    shallow_long = sum(
        item["skipped_minutes"] > long_cooldown_skipped_minutes for item in shallow
    )
    probability = _one_sided_fisher_probability(
        population=len(transitions),
        successes=deep_long + shallow_long,
        draws=len(deep),
        observed_successes=deep_long,
    )
    return {
        "deep_threshold": deep_dca_threshold,
        "long_cooldown_skipped_minutes_threshold": long_cooldown_skipped_minutes,
        "deep_transitions": len(deep),
        "deep_long_cooldowns": deep_long,
        "deep_long_cooldown_percent": deep_long / len(deep) * 100 if deep else 0.0,
        "shallow_transitions": len(shallow),
        "shallow_long_cooldowns": shallow_long,
        "shallow_long_cooldown_percent": (
            shallow_long / len(shallow) * 100 if shallow else 0.0
        ),
        "one_sided_fisher_exact_probability": probability,
        "deep_skipped_minutes": [item["skipped_minutes"] for item in deep],
        "interpretation": (
            f"association estimate is based on {len(deep)} deep transitions and cannot "
            "establish a deterministic cooldown duration"
        ),
    }


def _one_sided_fisher_probability(
    *,
    population: int,
    successes: int,
    draws: int,
    observed_successes: int,
) -> float:
    denominator = comb(population, draws)
    return sum(
        comb(successes, selected) * comb(population - successes, draws - selected)
        / denominator
        for selected in range(observed_successes, min(draws, successes) + 1)
        if 0 <= draws - selected <= population - successes
    )


def _first_entry_price_summary(
    export: TraderExportData,
    candle_by_minute: dict[int, Candle],
) -> dict[str, Any]:
    exact_cycles = [cycle for cycle in export.cycles if cycle.dca_count == 0]
    open_errors: list[float] = []
    close_errors: list[float] = []
    in_range = 0
    for cycle in exact_cycles:
        candle = candle_by_minute.get(cycle.opened_ms // MINUTE_MS * MINUTE_MS)
        if candle is None:
            continue
        open_errors.append(abs(cycle.average_entry - candle.open) / cycle.average_entry * 100)
        close_errors.append(abs(cycle.average_entry - candle.close) / cycle.average_entry * 100)
        in_range += candle.low <= cycle.average_entry <= candle.high
    return {
        "identifiable_dca0_cycles": len(exact_cycles),
        "cycles_with_entry_candle": len(open_errors),
        "entry_price_inside_entry_minute_range": in_range,
        "entry_vs_candle_open_absolute_error_percent": _error_summary(open_errors),
        "entry_vs_candle_close_absolute_error_percent": _error_summary(close_errors),
        "layered_cycle_initial_price_status": "not_identifiable_from_export",
    }


def _deep_dca_evidence(
    export: TraderExportData,
    candle_by_minute: dict[int, Candle],
    *,
    regime_by_cycle: dict[int, int],
    price_tolerance_usdt: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cycles: list[dict[str, Any]] = []
    grouped: dict[tuple[int, float, int], list[dict[str, float]]] = defaultdict(list)
    for cycle in export.cycles:
        if cycle.dca_count < 9:
            continue
        evidence = _deep_cycle_bounds(
            cycle,
            candle_by_minute,
            price_tolerance_usdt=price_tolerance_usdt,
        )
        regime_index = regime_by_cycle[id(cycle)]
        levels = [item for item in evidence if item["level"] >= 9]
        for item in levels:
            grouped[(regime_index, cycle.leverage, item["level"])].append(item)
        cycles.append(
            {
                "regime_index": regime_index,
                "leverage": cycle.leverage,
                "dca_depth": cycle.dca_count,
                "opened_utc": _iso_ms(cycle.opened_ms),
                "close_fragment_count": cycle.close_fragment_count,
                "levels": levels,
            }
        )

    summary: list[dict[str, Any]] = []
    for (regime_index, leverage, level), items in sorted(grouped.items()):
        multipliers = [item["quantity_multiplier_from_previous"] for item in items]
        midpoint = [item["trigger_drop_midpoint_proxy_percent"] for item in items]
        summary.append(
            {
                "regime_index": regime_index,
                "leverage": leverage,
                "level": level,
                "samples": len(items),
                "eligible_for_runtime_configuration": (
                    len(items) >= MIN_DEEP_LEVEL_SAMPLES_FOR_RUNTIME
                ),
                "quantity_multiplier_from_previous": _numeric_summary(multipliers),
                "trigger_drop_midpoint_proxy_percent": _numeric_summary(midpoint),
                "trigger_drop_union_bound_percent": {
                    "minimum": min(item["trigger_drop_lower_bound_percent"] for item in items),
                    "maximum": max(item["trigger_drop_upper_bound_percent"] for item in items),
                },
                "interpretation": _deep_level_interpretation(
                    leverage=leverage,
                    level=level,
                    multipliers=multipliers,
                    samples=len(items),
                ),
            }
        )
    return cycles, summary


def _deep_cycle_bounds(
    cycle: TraderCycle,
    candle_by_minute: dict[int, Candle],
    *,
    price_tolerance_usdt: float,
) -> list[dict[str, float | int]]:
    quantities = [order.order_qty for order in cycle.orders]
    entry_candles = [
        candle_by_minute.get(order.opened_ms // MINUTE_MS * MINUTE_MS)
        for order in cycle.orders
    ]
    if any(candle is None for candle in entry_candles):
        raise ValueError("deep cycle is missing an entry-minute candle")
    candles = [candle for candle in entry_candles if candle is not None]
    lows = [candle.low - price_tolerance_usdt for candle in candles]
    highs = [candle.high + price_tolerance_usdt for candle in candles]
    target = cycle.average_entry * sum(quantities)
    tightened: list[tuple[float, float]] = []
    for index, quantity in enumerate(quantities):
        other_low = sum(
            quantities[item] * lows[item]
            for item in range(len(quantities))
            if item != index
        )
        other_high = sum(
            quantities[item] * highs[item]
            for item in range(len(quantities))
            if item != index
        )
        lower = max(lows[index], (target - other_high) / quantity)
        upper = min(highs[index], (target - other_low) / quantity)
        if lower > upper and lower - upper <= 1e-9:
            lower = upper = (lower + upper) / 2
        if lower > upper:
            raise ValueError("weighted-average constraint is infeasible for deep cycle")
        tightened.append((lower, upper))

    results: list[dict[str, float | int]] = []
    for index in range(1, len(quantities)):
        previous_quantity = sum(quantities[:index])
        previous_low = sum(
            quantities[item] * tightened[item][0] for item in range(index)
        )
        previous_high = sum(
            quantities[item] * tightened[item][1] for item in range(index)
        )
        remainder_low = sum(
            quantities[item] * tightened[item][0]
            for item in range(index, len(quantities))
        )
        remainder_high = sum(
            quantities[item] * tightened[item][1]
            for item in range(index, len(quantities))
        )
        previous_low = max(previous_low, target - remainder_high)
        previous_high = min(previous_high, target - remainder_low)
        average_low = previous_low / previous_quantity
        average_high = previous_high / previous_quantity
        fill_low, fill_high = tightened[index]
        drop_low = (1 - fill_high / average_low) * 100
        drop_high = (1 - fill_low / average_high) * 100
        midpoint = (
            1
            - ((fill_low + fill_high) / 2)
            / ((average_low + average_high) / 2)
        ) * 100
        results.append(
            {
                "level": index,
                "quantity_multiplier_from_previous": (
                    quantities[index] / quantities[index - 1]
                ),
                "fill_price_lower_bound": fill_low,
                "fill_price_upper_bound": fill_high,
                "prior_average_lower_bound": average_low,
                "prior_average_upper_bound": average_high,
                "trigger_drop_lower_bound_percent": drop_low,
                "trigger_drop_upper_bound_percent": drop_high,
                "trigger_drop_midpoint_proxy_percent": midpoint,
            }
        )
    return results


def _deep_level_interpretation(
    *,
    leverage: float,
    level: int,
    multipliers: list[float],
    samples: int,
) -> str:
    if leverage == 24 and level == 9 and max(abs(value - 1) for value in multipliers) < 0.02:
        return "near-flat quantity suggests a capped emergency add; evidence is insufficient"
    if leverage == 20:
        return "older 20x geometric ladder; do not pool with mature 24x behavior"
    return f"only {samples} regime-consistent sample(s); insufficient for runtime configuration"


def _numeric_summary(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "minimum": ordered[0],
        "median": median(ordered),
        "p90": _quantile(ordered, 0.90),
        "maximum": ordered[-1],
    }


def _error_summary(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median": median(ordered),
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
