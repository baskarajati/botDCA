from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import UTC, datetime
from statistics import median
from typing import Any

from botdca.backtest import Candle, CycleReplay, IntrabarPath, ReplayEngine
from botdca.historical_profile import MINUTE_MS, split_regimes
from botdca.strategy import StrategyConfig
from botdca.trader_export import TraderCycle, TraderExportData


def evaluate_anchored_cycles(
    cycles: list[TraderCycle],
    candles: list[Candle],
    config: StrategyConfig,
    *,
    intrabar_path: IntrabarPath,
    maximum_close_time_error_seconds: float = 300.0,
    taker_fee_rate: float = 0.00055,
) -> dict[str, Any]:
    """Evaluate one independently anchored replay per observed cycle."""

    ordered_candles = sorted(candles, key=lambda candle: candle.start_ms)
    candle_times = [candle.start_ms for candle in ordered_candles]
    actual: list[TraderCycle] = []
    simulated: list[CycleReplay] = []
    pairs: list[tuple[int, int]] = []
    diagnostics: list[dict[str, Any]] = []
    close_window_ms = int(maximum_close_time_error_seconds * 1000)
    for cycle in cycles:
        actual_index = len(actual)
        actual.append(cycle)
        start_ms = cycle.opened_ms // MINUTE_MS * MINUTE_MS
        end_ms = cycle.closed_ms // MINUTE_MS * MINUTE_MS + close_window_ms
        window = _candle_slice(ordered_candles, candle_times, start_ms, end_ms)
        if not window:
            diagnostics.append(_unmatched_diagnostic(cycle, 1, reason="missing_candles"))
            continue
        result = ReplayEngine(
            config,
            intrabar_path=intrabar_path,
            taker_fee_rate=taker_fee_rate,
            auto_reentry=False,
            maximum_completed_cycles=1,
        ).run(window)
        if not result.cycles:
            diagnostics.append(
                _unmatched_diagnostic(cycle, 1, reason="simulated_cycle_still_open")
            )
            continue
        candidate = result.cycles[0]
        simulated_index = len(simulated)
        simulated.append(candidate)
        close_error = abs(candidate.closed_ms - cycle.closed_ms) / 1000
        if close_error <= maximum_close_time_error_seconds:
            pairs.append((actual_index, simulated_index))
        else:
            diagnostic = _unmatched_diagnostic(
                cycle,
                1,
                reason="close_outside_match_window",
            )
            diagnostic["simulated_closed_utc"] = _iso_ms(candidate.closed_ms)
            diagnostic["close_time_error_seconds"] = close_error
            diagnostics.append(diagnostic)
    metrics = _comparison_metrics(actual, simulated, pairs)
    metrics["unmatched_actual_diagnostics"] = diagnostics[:25]
    return metrics


def evaluate_continuous_cycles(
    cycles: list[TraderCycle],
    candles: list[Candle],
    config: StrategyConfig,
    *,
    intrabar_path: IntrabarPath,
    maximum_close_time_error_seconds: float = 300.0,
    reentry_delay_seconds: float = 48.0,
    taker_fee_rate: float = 0.00055,
) -> dict[str, Any]:
    """Evaluate autonomous replay over one chronological set of cycles."""

    if not cycles:
        raise ValueError("at least one trader cycle is required")
    ordered_candles = sorted(candles, key=lambda candle: candle.start_ms)
    candle_times = [candle.start_ms for candle in ordered_candles]
    close_window_ms = int(maximum_close_time_error_seconds * 1000)
    start_ms = cycles[0].opened_ms // MINUTE_MS * MINUTE_MS
    end_ms = cycles[-1].closed_ms // MINUTE_MS * MINUTE_MS + close_window_ms
    window = _candle_slice(ordered_candles, candle_times, start_ms, end_ms)
    result = ReplayEngine(
        config,
        intrabar_path=intrabar_path,
        taker_fee_rate=taker_fee_rate,
        reentry_delay_seconds=reentry_delay_seconds,
    ).run(window)
    pairs = _ordered_close_matches(
        cycles,
        result.cycles,
        maximum_close_time_error_seconds=maximum_close_time_error_seconds,
    )
    return _comparison_metrics(cycles, result.cycles, pairs)


def validate_strategy_history(
    export: TraderExportData,
    candles: list[Candle],
    *,
    baseline_config: StrategyConfig | None = None,
    intrabar_paths: tuple[IntrabarPath, ...] = (
        IntrabarPath.LOW_FIRST,
        IntrabarPath.HIGH_FIRST,
    ),
    maximum_close_time_error_seconds: float = 300.0,
    reentry_delay_seconds: float = 48.0,
    tp_change_threshold_percent: float = 0.04,
    taker_fee_rate: float = 0.00055,
) -> dict[str, Any]:
    """Run anchored and continuous validation with the runtime DcaStrategy.

    The uniform scenario uses the current reconstructed defaults. The
    regime-aware scenario changes only leverage and TP to values directly
    observed in each chronological regime; it is descriptive, not holdout
    calibration.
    """

    if maximum_close_time_error_seconds <= 0:
        raise ValueError("maximum_close_time_error_seconds must be positive")
    if reentry_delay_seconds < 0:
        raise ValueError("reentry_delay_seconds cannot be negative")
    baseline = baseline_config or StrategyConfig()
    ordered_candles = sorted(candles, key=lambda candle: candle.start_ms)
    if not ordered_candles:
        raise ValueError("at least one candle is required")
    candle_times = [candle.start_ms for candle in ordered_candles]
    regimes = split_regimes(list(export.cycles), threshold=tp_change_threshold_percent)

    scenarios: list[dict[str, Any]] = []
    for config_mode in ("uniform_baseline", "observed_regime"):
        for path in intrabar_paths:
            anchored = _validate_anchored(
                regimes,
                ordered_candles,
                candle_times,
                baseline=baseline,
                config_mode=config_mode,
                path=path,
                maximum_close_time_error_seconds=maximum_close_time_error_seconds,
                taker_fee_rate=taker_fee_rate,
            )
            continuous = _validate_continuous(
                regimes,
                ordered_candles,
                candle_times,
                baseline=baseline,
                config_mode=config_mode,
                path=path,
                maximum_close_time_error_seconds=maximum_close_time_error_seconds,
                reentry_delay_seconds=reentry_delay_seconds,
                taker_fee_rate=taker_fee_rate,
            )
            scenarios.append(
                {
                    "configuration": config_mode,
                    "intrabar_path": path.value,
                    "anchored": anchored,
                    "continuous": continuous,
                }
            )

    any_anchored_pass = any(
        scenario["anchored"]["meets_all_suggested_targets"] for scenario in scenarios
    )
    any_continuous_pass = any(
        scenario["continuous"]["meets_all_suggested_targets"] for scenario in scenarios
    )
    uniform_pass = any(
        scenario["configuration"] == "uniform_baseline"
        and scenario["anchored"]["meets_all_suggested_targets"]
        and scenario["continuous"]["meets_all_suggested_targets"]
        for scenario in scenarios
    )
    return {
        "methodology": {
            "strategy_engine": "botdca.strategy.DcaStrategy via ReplayEngine",
            "anchored_entry": "open of the one-minute candle containing actual first entry",
            "anchored_scope": "one simulated basket per observed cycle; no automatic re-entry",
            "continuous_scope": (
                "autonomous replay within each detected regime; state resets at regime boundaries"
            ),
            "uniform_baseline": {
                "leverage": baseline.leverage,
                "tp_percent": baseline.tp_percent,
                "dca_steps": [
                    {
                        "drop_percent_from_average": step.drop_percent_from_average,
                        "size_multiplier_from_previous": step.size_multiplier_from_previous,
                    }
                    for step in baseline.dca_steps
                ],
            },
            "observed_regime": (
                "uses each regime's observed leverage and median TP; DCA ladder remains baseline"
            ),
            "maximum_close_time_error_seconds": maximum_close_time_error_seconds,
            "continuous_reentry_delay_seconds": reentry_delay_seconds,
            "limitations": [
                "One-minute candles do not reveal the actual second-level entry price.",
                "Close timestamps are quantized to the candle start in replay.",
                "Observed-regime results are in-sample descriptive validation, not holdout evidence.",
                "Error percentiles and exact-depth rates are conditional on matched cycles.",
            ],
        },
        "regime_count": len(regimes),
        "conclusion": {
            "reconstructed_strategy_status": "validated" if uniform_pass else "not_validated",
            "uniform_baseline_meets_all_suggested_targets": uniform_pass,
            "any_anchored_scenario_meets_all_suggested_targets": any_anchored_pass,
            "any_continuous_scenario_meets_all_suggested_targets": any_continuous_pass,
        },
        "scenarios": scenarios,
    }


def _validate_anchored(
    regimes: list[list[TraderCycle]],
    candles: list[Candle],
    candle_times: list[int],
    *,
    baseline: StrategyConfig,
    config_mode: str,
    path: IntrabarPath,
    maximum_close_time_error_seconds: float,
    taker_fee_rate: float,
) -> dict[str, Any]:
    actual: list[TraderCycle] = []
    simulated: list[CycleReplay] = []
    pairs: list[tuple[int, int]] = []
    candidate_diagnostics: list[dict[str, Any]] = []
    close_window_ms = int(maximum_close_time_error_seconds * 1000)

    for regime_index, regime in enumerate(regimes, start=1):
        config = _scenario_config(regime, baseline=baseline, config_mode=config_mode)
        for cycle in regime:
            actual_index = len(actual)
            actual.append(cycle)
            start_ms = cycle.opened_ms // MINUTE_MS * MINUTE_MS
            end_ms = cycle.closed_ms // MINUTE_MS * MINUTE_MS + close_window_ms
            window = _candle_slice(candles, candle_times, start_ms, end_ms)
            if not window:
                candidate_diagnostics.append(
                    _unmatched_diagnostic(cycle, regime_index, reason="missing_candles")
                )
                continue
            result = ReplayEngine(
                config,
                intrabar_path=path,
                taker_fee_rate=taker_fee_rate,
                auto_reentry=False,
                maximum_completed_cycles=1,
            ).run(window)
            if not result.cycles:
                candidate_diagnostics.append(
                    _unmatched_diagnostic(cycle, regime_index, reason="simulated_cycle_still_open")
                )
                continue
            candidate = result.cycles[0]
            simulated_index = len(simulated)
            simulated.append(candidate)
            close_error = abs(candidate.closed_ms - cycle.closed_ms) / 1000
            if close_error <= maximum_close_time_error_seconds:
                pairs.append((actual_index, simulated_index))
            else:
                diagnostic = _unmatched_diagnostic(
                    cycle,
                    regime_index,
                    reason="close_outside_match_window",
                )
                diagnostic["simulated_closed_utc"] = _iso_ms(candidate.closed_ms)
                diagnostic["close_time_error_seconds"] = close_error
                candidate_diagnostics.append(diagnostic)

    metrics = _comparison_metrics(actual, simulated, pairs)
    metrics["unmatched_actual_diagnostics"] = candidate_diagnostics[:25]
    return metrics


def _validate_continuous(
    regimes: list[list[TraderCycle]],
    candles: list[Candle],
    candle_times: list[int],
    *,
    baseline: StrategyConfig,
    config_mode: str,
    path: IntrabarPath,
    maximum_close_time_error_seconds: float,
    reentry_delay_seconds: float,
    taker_fee_rate: float,
) -> dict[str, Any]:
    all_actual: list[TraderCycle] = []
    all_simulated: list[CycleReplay] = []
    all_pairs: list[tuple[int, int]] = []
    per_regime: list[dict[str, Any]] = []
    close_window_ms = int(maximum_close_time_error_seconds * 1000)

    for regime_index, regime in enumerate(regimes, start=1):
        config = _scenario_config(regime, baseline=baseline, config_mode=config_mode)
        start_ms = regime[0].opened_ms // MINUTE_MS * MINUTE_MS
        end_ms = regime[-1].closed_ms // MINUTE_MS * MINUTE_MS + close_window_ms
        window = _candle_slice(candles, candle_times, start_ms, end_ms)
        result = ReplayEngine(
            config,
            intrabar_path=path,
            taker_fee_rate=taker_fee_rate,
            reentry_delay_seconds=reentry_delay_seconds,
        ).run(window)
        matches = _ordered_close_matches(
            regime,
            result.cycles,
            maximum_close_time_error_seconds=maximum_close_time_error_seconds,
        )
        regime_metrics = _comparison_metrics(regime, result.cycles, matches)
        regime_metrics.update(
            {
                "regime_index": regime_index,
                "configured_leverage": config.leverage,
                "configured_tp_percent": config.tp_percent,
                "opened_from_utc": _iso_ms(regime[0].opened_ms),
                "closed_through_utc": _iso_ms(regime[-1].closed_ms),
            }
        )
        per_regime.append(regime_metrics)

        actual_offset = len(all_actual)
        simulated_offset = len(all_simulated)
        all_actual.extend(regime)
        all_simulated.extend(result.cycles)
        all_pairs.extend(
            (actual_offset + actual_index, simulated_offset + simulated_index)
            for actual_index, simulated_index in matches
        )

    overall = _comparison_metrics(all_actual, all_simulated, all_pairs)
    overall["per_regime"] = per_regime
    return overall


def _scenario_config(
    regime: list[TraderCycle],
    *,
    baseline: StrategyConfig,
    config_mode: str,
) -> StrategyConfig:
    if config_mode == "uniform_baseline":
        leverage = baseline.leverage
        tp_percent = baseline.tp_percent
    elif config_mode == "observed_regime":
        leverage = int(regime[0].leverage)
        tp_percent = round(median(_actual_tp_percent(cycle) for cycle in regime), 6)
    else:
        raise ValueError(f"unknown config_mode: {config_mode}")
    return StrategyConfig(
        symbol=baseline.symbol,
        leverage=leverage,
        base_margin_usdt=baseline.base_margin_usdt,
        tp_percent=tp_percent,
        dca_steps=baseline.dca_steps,
    )


def _ordered_close_matches(
    actual: list[TraderCycle],
    simulated: list[CycleReplay],
    *,
    maximum_close_time_error_seconds: float,
) -> list[tuple[int, int]]:
    """Maximize ordered matches, then minimize total absolute close error."""

    rows = len(actual) + 1
    columns = len(simulated) + 1
    scores = [[(0, 0.0) for _ in range(columns)] for _ in range(rows)]
    actions = [["" for _ in range(columns)] for _ in range(rows)]

    for actual_index in range(1, rows):
        actions[actual_index][0] = "skip_actual"
    for simulated_index in range(1, columns):
        actions[0][simulated_index] = "skip_simulated"

    def better(left: tuple[int, float], right: tuple[int, float]) -> bool:
        return left[0] > right[0] or (left[0] == right[0] and left[1] < right[1])

    for actual_index in range(1, rows):
        for simulated_index in range(1, columns):
            best = scores[actual_index - 1][simulated_index]
            action = "skip_actual"
            skip_simulated = scores[actual_index][simulated_index - 1]
            if better(skip_simulated, best):
                best = skip_simulated
                action = "skip_simulated"

            close_error = abs(
                actual[actual_index - 1].closed_ms
                - simulated[simulated_index - 1].closed_ms
            ) / 1000
            if close_error <= maximum_close_time_error_seconds:
                previous = scores[actual_index - 1][simulated_index - 1]
                matched = (previous[0] + 1, previous[1] + close_error)
                if better(matched, best):
                    best = matched
                    action = "match"
            scores[actual_index][simulated_index] = best
            actions[actual_index][simulated_index] = action

    pairs: list[tuple[int, int]] = []
    actual_index = len(actual)
    simulated_index = len(simulated)
    while actual_index > 0 or simulated_index > 0:
        action = actions[actual_index][simulated_index]
        if action == "match":
            pairs.append((actual_index - 1, simulated_index - 1))
            actual_index -= 1
            simulated_index -= 1
        elif action == "skip_actual":
            actual_index -= 1
        elif action == "skip_simulated":
            simulated_index -= 1
        else:
            break
    pairs.reverse()
    return pairs


def _comparison_metrics(
    actual: list[TraderCycle],
    simulated: list[CycleReplay],
    pairs: list[tuple[int, int]],
) -> dict[str, Any]:
    close_errors: list[float] = []
    average_entry_errors: list[float] = []
    exit_price_errors: list[float] = []
    depth_errors: list[float] = []
    exact_depth = 0
    pair_details: list[dict[str, Any]] = []

    for actual_index, simulated_index in pairs:
        observed = actual[actual_index]
        replayed = simulated[simulated_index]
        close_error = abs(replayed.closed_ms - observed.closed_ms) / 1000
        exit_error = abs(replayed.exit_price - observed.closing_price) / observed.closing_price * 100
        depth_error = replayed.dca_level - observed.dca_count
        average_error = None
        if replayed.average_entry is not None:
            average_error = (
                abs(replayed.average_entry - observed.average_entry)
                / observed.average_entry
                * 100
            )
            average_entry_errors.append(average_error)
        close_errors.append(close_error)
        exit_price_errors.append(exit_error)
        depth_errors.append(abs(depth_error))
        exact_depth += depth_error == 0
        pair_details.append(
            {
                "actual_closed_utc": _iso_ms(observed.closed_ms),
                "simulated_closed_utc": _iso_ms(replayed.closed_ms),
                "close_time_error_seconds": close_error,
                "weighted_average_entry_error_percent": average_error,
                "exit_price_error_percent": exit_error,
                "actual_dca_depth": observed.dca_count,
                "simulated_dca_depth": replayed.dca_level,
            }
        )

    matched_actual = {actual_index for actual_index, _ in pairs}
    matched_simulated = {simulated_index for _, simulated_index in pairs}
    worst = sorted(pair_details, key=lambda item: item["close_time_error_seconds"], reverse=True)
    unmatched_actual = [
        _nearest_unmatched_actual_diagnostic(actual[index], simulated)
        for index in range(len(actual))
        if index not in matched_actual
    ]
    unmatched_simulated = [
        {
            "simulated_opened_utc": _iso_ms(simulated[index].opened_ms),
            "simulated_closed_utc": _iso_ms(simulated[index].closed_ms),
            "simulated_dca_depth": simulated[index].dca_level,
        }
        for index in range(len(simulated))
        if index not in matched_simulated
    ]
    metrics = {
        "actual_completed_cycles": len(actual),
        "simulated_completed_cycles": len(simulated),
        "matched_cycles": len(pairs),
        "completed_cycle_match_rate_percent": len(pairs) / len(actual) * 100 if actual else 0.0,
        "unmatched_actual_cycles": len(actual) - len(matched_actual),
        "unmatched_simulated_cycles": len(simulated) - len(matched_simulated),
        "close_time_error_seconds": _error_summary(close_errors),
        "weighted_average_entry_error_percent": _error_summary(average_entry_errors),
        "exit_price_error_percent": _error_summary(exit_price_errors),
        "exact_dca_depth_match_percent": exact_depth / len(pairs) * 100 if pairs else None,
        "median_absolute_dca_depth_error": median(depth_errors) if depth_errors else None,
        "actual_dca_depth_distribution": _depth_distribution(
            [cycle.dca_count for cycle in actual]
        ),
        "simulated_dca_depth_distribution": _depth_distribution(
            [cycle.dca_level for cycle in simulated]
        ),
        "worst_matched_close_diagnostics": worst[:10],
        "unmatched_actual_diagnostics": unmatched_actual[:25],
        "unmatched_simulated_diagnostics": unmatched_simulated[:25],
    }
    metrics["suggested_target_evaluation"] = _target_evaluation(metrics)
    metrics["meets_all_suggested_targets"] = all(
        item["passed"] for item in metrics["suggested_target_evaluation"].values()
    )
    return metrics


def _target_evaluation(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    close = metrics["close_time_error_seconds"]
    average = metrics["weighted_average_entry_error_percent"]
    exit_price = metrics["exit_price_error_percent"]
    checks = {
        "completed_cycle_match_rate_percent": (
            metrics["completed_cycle_match_rate_percent"],
            ">=",
            90.0,
        ),
        "exact_dca_depth_match_percent": (
            metrics["exact_dca_depth_match_percent"],
            ">=",
            85.0,
        ),
        "median_close_time_error_seconds": (
            close["median"] if close else None,
            "<=",
            120.0,
        ),
        "median_weighted_average_entry_error_percent": (
            average["median"] if average else None,
            "<=",
            0.15,
        ),
        "median_exit_price_error_percent": (
            exit_price["median"] if exit_price else None,
            "<=",
            0.10,
        ),
    }
    return {
        name: {
            "value": value,
            "operator": operator,
            "target": target,
            "passed": (
                value is not None
                and ((operator == ">=" and value >= target) or (operator == "<=" and value <= target))
            ),
        }
        for name, (value, operator, target) in checks.items()
    }


def _nearest_unmatched_actual_diagnostic(
    cycle: TraderCycle,
    simulated: list[CycleReplay],
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "actual_opened_utc": _iso_ms(cycle.opened_ms),
        "actual_closed_utc": _iso_ms(cycle.closed_ms),
        "actual_dca_depth": cycle.dca_count,
        "actual_tp_percent": _actual_tp_percent(cycle),
    }
    if simulated:
        nearest = min(simulated, key=lambda item: abs(item.closed_ms - cycle.closed_ms))
        diagnostic.update(
            {
                "nearest_simulated_closed_utc": _iso_ms(nearest.closed_ms),
                "nearest_close_time_error_seconds": abs(nearest.closed_ms - cycle.closed_ms)
                / 1000,
                "nearest_simulated_dca_depth": nearest.dca_level,
            }
        )
    else:
        diagnostic["reason"] = "no_simulated_completed_cycles"
    return diagnostic


def _error_summary(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median": _quantile(ordered, 0.50),
        "p90": _quantile(ordered, 0.90),
        "maximum": ordered[-1],
    }


def _depth_distribution(depths: list[int]) -> dict[str, int]:
    counts = Counter(depths)
    return {str(depth): counts[depth] for depth in sorted(counts)}


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _candle_slice(
    candles: list[Candle],
    candle_times: list[int],
    start_ms: int,
    end_ms: int,
) -> list[Candle]:
    start_index = bisect_left(candle_times, start_ms)
    end_index = bisect_right(candle_times, end_ms)
    return candles[start_index:end_index]


def _actual_tp_percent(cycle: TraderCycle) -> float:
    return (cycle.closing_price / cycle.average_entry - 1) * 100


def _unmatched_diagnostic(
    cycle: TraderCycle,
    regime_index: int,
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "regime_index": regime_index,
        "reason": reason,
        "actual_opened_utc": _iso_ms(cycle.opened_ms),
        "actual_closed_utc": _iso_ms(cycle.closed_ms),
        "actual_dca_depth": cycle.dca_count,
        "actual_tp_percent": _actual_tp_percent(cycle),
    }


def _iso_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
