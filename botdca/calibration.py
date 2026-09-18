from __future__ import annotations

from collections import Counter
from dataclasses import replace
from statistics import median
from typing import Any

from botdca.backtest import Candle, IntrabarPath
from botdca.domain import DcaStep
from botdca.historical_profile import split_regimes
from botdca.historical_validation import (
    evaluate_anchored_cycles,
    evaluate_continuous_cycles,
)
from botdca.strategy import StrategyConfig
from botdca.trader_export import TraderCycle, TraderExportData


def calibrate_strategy_history(
    export: TraderExportData,
    candles: list[Candle],
    *,
    baseline_config: StrategyConfig | None = None,
    intrabar_paths: tuple[IntrabarPath, ...] = (
        IntrabarPath.LOW_FIRST,
        IntrabarPath.HIGH_FIRST,
    ),
    train_fraction: float = 0.70,
    minimum_regime_cycles: int = 30,
    minimum_level_samples: int = 10,
    trigger_search_radius_percent: float = 0.15,
    reentry_delay_candidates_seconds: tuple[float, ...] = (0, 30, 48, 60, 90, 120, 180),
    maximum_close_time_error_seconds: float = 300.0,
    tp_change_threshold_percent: float = 0.04,
    taker_fee_rate: float = 0.00055,
) -> dict[str, Any]:
    """Run bounded chronological calibration without changing runtime defaults.

    TP and DCA triggers are fitted only on the first part of each sufficiently
    large observed regime. Quantity multipliers remain fixed. The later part is
    evaluated once as a holdout after parameter selection.
    """

    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    if minimum_regime_cycles < 2:
        raise ValueError("minimum_regime_cycles must be at least two")
    if minimum_level_samples < 1:
        raise ValueError("minimum_level_samples must be positive")
    if trigger_search_radius_percent <= 0:
        raise ValueError("trigger_search_radius_percent must be positive")
    if not intrabar_paths:
        raise ValueError("at least one intrabar path is required")
    if not reentry_delay_candidates_seconds or any(
        value < 0 for value in reentry_delay_candidates_seconds
    ):
        raise ValueError("re-entry delay candidates must be nonnegative")
    if not candles:
        raise ValueError("at least one candle is required")

    baseline = baseline_config or StrategyConfig()
    regimes = split_regimes(
        list(export.cycles),
        threshold=tp_change_threshold_percent,
    )
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for regime_index, regime in enumerate(regimes, start=1):
        if len(regime) < minimum_regime_cycles:
            skipped.append(
                {
                    "regime_index": regime_index,
                    "completed_cycles": len(regime),
                    "reason": "insufficient_cycles_for_chronological_holdout",
                }
            )
            continue

        split_index = max(1, min(len(regime) - 1, int(len(regime) * train_fraction)))
        training = regime[:split_index]
        validation = regime[split_index:]
        regime_baseline = replace(
            baseline,
            leverage=int(regime[0].leverage),
        )
        calibrated = replace(
            regime_baseline,
            tp_percent=round(median(_tp_percent(cycle) for cycle in training), 6),
        )

        level_samples = [
            sum(cycle.dca_count >= level for cycle in training)
            for level in range(1, len(calibrated.dca_steps) + 1)
        ]
        eligible_levels = [
            index
            for index, samples in enumerate(level_samples)
            if samples >= minimum_level_samples
        ]
        search_trace: list[dict[str, Any]] = []
        for level_index in eligible_levels:
            current_drop = calibrated.dca_steps[level_index].drop_percent_from_average
            candidates = sorted(
                {
                    round(max(0.05, current_drop - trigger_search_radius_percent), 6),
                    round(current_drop, 6),
                    round(current_drop + trigger_search_radius_percent, 6),
                }
            )
            scored: list[tuple[tuple[float, ...], float, dict[str, Any]]] = []
            for candidate in candidates:
                candidate_config = _replace_trigger(calibrated, level_index, candidate)
                metrics_by_path = _anchored_by_path(
                    training,
                    candles,
                    candidate_config,
                    intrabar_paths=intrabar_paths,
                    maximum_close_time_error_seconds=maximum_close_time_error_seconds,
                    taker_fee_rate=taker_fee_rate,
                )
                scored.append(
                    (
                        _anchored_objective(metrics_by_path),
                        candidate,
                        metrics_by_path,
                    )
                )
            best_score, best_candidate, _ = max(
                scored,
                key=lambda item: (item[0], -abs(item[1] - current_drop)),
            )
            calibrated = _replace_trigger(calibrated, level_index, best_candidate)
            search_trace.append(
                {
                    "level": level_index + 1,
                    "training_cycles_reaching_level": level_samples[level_index],
                    "baseline_drop_percent_from_average": current_drop,
                    "candidates": candidates,
                    "selected_drop_percent_from_average": best_candidate,
                    "selected_objective": list(best_score),
                }
            )

        delay_scores: list[tuple[tuple[float, ...], float]] = []
        for delay in reentry_delay_candidates_seconds:
            metrics_by_path = _continuous_by_path(
                training,
                candles,
                calibrated,
                intrabar_paths=intrabar_paths,
                reentry_delay_seconds=delay,
                maximum_close_time_error_seconds=maximum_close_time_error_seconds,
                taker_fee_rate=taker_fee_rate,
            )
            delay_scores.append((_continuous_objective(metrics_by_path), delay))
        selected_delay = max(
            delay_scores,
            key=lambda item: (item[0], -abs(item[1] - 48.0)),
        )[1]

        evaluations: dict[str, Any] = {}
        for split_name, split_cycles in (("training", training), ("validation", validation)):
            evaluations[split_name] = {
                "baseline": _evaluate_configuration(
                    split_cycles,
                    candles,
                    regime_baseline,
                    intrabar_paths=intrabar_paths,
                    reentry_delay_seconds=48.0,
                    maximum_close_time_error_seconds=maximum_close_time_error_seconds,
                    taker_fee_rate=taker_fee_rate,
                ),
                "calibrated": _evaluate_configuration(
                    split_cycles,
                    candles,
                    calibrated,
                    intrabar_paths=intrabar_paths,
                    reentry_delay_seconds=selected_delay,
                    maximum_close_time_error_seconds=maximum_close_time_error_seconds,
                    taker_fee_rate=taker_fee_rate,
                ),
            }

        validation_pass = all(
            item["anchored"]["meets_all_suggested_targets"]
            and item["continuous"]["meets_all_suggested_targets"]
            for item in evaluations["validation"]["calibrated"].values()
        )
        results.append(
            {
                "regime_index": regime_index,
                "leverage": regime[0].leverage,
                "completed_cycles": len(regime),
                "training_cycles": len(training),
                "validation_cycles": len(validation),
                "training_opened_from_ms": training[0].opened_ms,
                "training_closed_through_ms": training[-1].closed_ms,
                "validation_opened_from_ms": validation[0].opened_ms,
                "validation_closed_through_ms": validation[-1].closed_ms,
                "parameter_evidence": {
                    "tp_training_samples": len(training),
                    "dca_training_cycles_reaching_level": {
                        str(index): samples
                        for index, samples in enumerate(level_samples, start=1)
                    },
                    "eligible_dca_levels": [index + 1 for index in eligible_levels],
                },
                "baseline_parameters": _parameter_summary(regime_baseline, 48.0),
                "calibrated_parameters": _parameter_summary(calibrated, selected_delay),
                "dca_trigger_search": search_trace,
                "reentry_delay_search": [
                    {"seconds": delay, "objective": list(score)}
                    for score, delay in delay_scores
                ],
                "evaluations": evaluations,
                "holdout_mismatch_summary": _holdout_mismatch_summary(
                    evaluations["validation"]
                ),
                "validation_meets_all_targets_on_both_intrabar_paths": validation_pass,
            }
        )

    all_validation_pass = bool(results) and all(
        result["validation_meets_all_targets_on_both_intrabar_paths"] for result in results
    )
    return {
        "methodology": {
            "strategy_engine": "botdca.strategy.DcaStrategy via ReplayEngine",
            "split": (
                f"first {train_fraction * 100:g} percent train, final "
                f"{(1 - train_fraction) * 100:g} percent untouched chronological holdout"
            ),
            "train_fraction": train_fraction,
            "minimum_regime_cycles": minimum_regime_cycles,
            "minimum_training_samples_per_dca_level": minimum_level_samples,
            "calibrated_parameters": [
                "take-profit percent",
                "DCA trigger drops with sufficient training support",
                "continuous re-entry delay",
            ],
            "fixed_parameters": [
                "all DCA quantity multipliers",
                "base margin",
                "symbol",
            ],
            "trigger_search_radius_percent": trigger_search_radius_percent,
            "reentry_delay_candidates_seconds": list(reentry_delay_candidates_seconds),
            "maximum_close_time_error_seconds": maximum_close_time_error_seconds,
            "holdout_rule": "validation cycles are not used for parameter selection",
            "limitations": [
                "One-minute candles cannot recover second-level fills or high/low ordering.",
                "Regime boundaries are detected from completed observed cycles.",
                "Passing a small regime holdout would not establish live profitability or safety.",
            ],
        },
        "detected_regimes": len(regimes),
        "calibrated_regimes": len(results),
        "skipped_regimes": skipped,
        "conclusion": {
            "holdout_status": "passed" if all_validation_pass else "not_validated",
            "all_calibrated_regimes_pass_both_intrabar_paths": all_validation_pass,
            "runtime_defaults_changed": False,
        },
        "regimes": results,
    }


def _replace_trigger(config: StrategyConfig, level_index: int, drop: float) -> StrategyConfig:
    steps = list(config.dca_steps)
    previous = steps[level_index]
    steps[level_index] = DcaStep(
        drop_percent_from_average=drop,
        size_multiplier_from_previous=previous.size_multiplier_from_previous,
    )
    return replace(config, dca_steps=tuple(steps))


def _anchored_by_path(
    cycles: list[TraderCycle],
    candles: list[Candle],
    config: StrategyConfig,
    *,
    intrabar_paths: tuple[IntrabarPath, ...],
    maximum_close_time_error_seconds: float,
    taker_fee_rate: float,
) -> dict[str, Any]:
    return {
        path.value: evaluate_anchored_cycles(
            cycles,
            candles,
            config,
            intrabar_path=path,
            maximum_close_time_error_seconds=maximum_close_time_error_seconds,
            taker_fee_rate=taker_fee_rate,
        )
        for path in intrabar_paths
    }


def _continuous_by_path(
    cycles: list[TraderCycle],
    candles: list[Candle],
    config: StrategyConfig,
    *,
    intrabar_paths: tuple[IntrabarPath, ...],
    reentry_delay_seconds: float,
    maximum_close_time_error_seconds: float,
    taker_fee_rate: float,
) -> dict[str, Any]:
    return {
        path.value: evaluate_continuous_cycles(
            cycles,
            candles,
            config,
            intrabar_path=path,
            reentry_delay_seconds=reentry_delay_seconds,
            maximum_close_time_error_seconds=maximum_close_time_error_seconds,
            taker_fee_rate=taker_fee_rate,
        )
        for path in intrabar_paths
    }


def _evaluate_configuration(
    cycles: list[TraderCycle],
    candles: list[Candle],
    config: StrategyConfig,
    *,
    intrabar_paths: tuple[IntrabarPath, ...],
    reentry_delay_seconds: float,
    maximum_close_time_error_seconds: float,
    taker_fee_rate: float,
) -> dict[str, Any]:
    anchored = _anchored_by_path(
        cycles,
        candles,
        config,
        intrabar_paths=intrabar_paths,
        maximum_close_time_error_seconds=maximum_close_time_error_seconds,
        taker_fee_rate=taker_fee_rate,
    )
    continuous = _continuous_by_path(
        cycles,
        candles,
        config,
        intrabar_paths=intrabar_paths,
        reentry_delay_seconds=reentry_delay_seconds,
        maximum_close_time_error_seconds=maximum_close_time_error_seconds,
        taker_fee_rate=taker_fee_rate,
    )
    return {
        path.value: {
            "anchored": anchored[path.value],
            "continuous": continuous[path.value],
        }
        for path in intrabar_paths
    }


def _anchored_objective(metrics_by_path: dict[str, Any]) -> tuple[float, ...]:
    metrics = list(metrics_by_path.values())
    return (
        float(sum(item["matched_cycles"] for item in metrics)),
        sum(_exact_depth_count(item) for item in metrics),
        -float(sum(item["unmatched_simulated_cycles"] for item in metrics)),
        -sum(_summary_median(item["weighted_average_entry_error_percent"]) for item in metrics),
        -sum(_summary_median(item["exit_price_error_percent"]) for item in metrics),
    )


def _continuous_objective(metrics_by_path: dict[str, Any]) -> tuple[float, ...]:
    metrics = list(metrics_by_path.values())
    return (
        float(sum(item["matched_cycles"] for item in metrics)),
        -float(sum(item["unmatched_simulated_cycles"] for item in metrics)),
        sum(_exact_depth_count(item) for item in metrics),
        -sum(_summary_median(item["close_time_error_seconds"]) for item in metrics),
    )


def _exact_depth_count(metrics: dict[str, Any]) -> float:
    percent = metrics["exact_dca_depth_match_percent"]
    return 0.0 if percent is None else metrics["matched_cycles"] * percent / 100


def _summary_median(summary: dict[str, Any] | None) -> float:
    return 1_000_000.0 if summary is None else float(summary["median"])


def _parameter_summary(config: StrategyConfig, reentry_delay_seconds: float) -> dict[str, Any]:
    return {
        "leverage": config.leverage,
        "tp_percent": config.tp_percent,
        "reentry_delay_seconds": reentry_delay_seconds,
        "dca_steps": [
            {
                "drop_percent_from_average": step.drop_percent_from_average,
                "size_multiplier_from_previous": step.size_multiplier_from_previous,
            }
            for step in config.dca_steps
        ],
    }


def _holdout_mismatch_summary(validation: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for configuration in ("baseline", "calibrated"):
        summary[configuration] = {}
        for path, modes in validation[configuration].items():
            summary[configuration][path] = {}
            for mode in ("anchored", "continuous"):
                metrics = modes[mode]
                diagnostics = metrics["unmatched_actual_diagnostics"]
                nearest_errors = [
                    item["nearest_close_time_error_seconds"]
                    for item in diagnostics
                    if "nearest_close_time_error_seconds" in item
                ]
                reasons = Counter(
                    item.get("reason", "outside_ordered_match_window")
                    for item in diagnostics
                )
                summary[configuration][path][mode] = {
                    "matched_cycles": metrics["matched_cycles"],
                    "match_rate_percent": metrics["completed_cycle_match_rate_percent"],
                    "unmatched_actual_cycles": metrics["unmatched_actual_cycles"],
                    "unmatched_simulated_cycles": metrics["unmatched_simulated_cycles"],
                    "failed_targets": [
                        name
                        for name, result in metrics["suggested_target_evaluation"].items()
                        if not result["passed"]
                    ],
                    "unmatched_actual_reason_counts": dict(sorted(reasons.items())),
                    "median_nearest_close_error_seconds": (
                        median(nearest_errors) if nearest_errors else None
                    ),
                }
    return summary


def _tp_percent(cycle: TraderCycle) -> float:
    return (cycle.closing_price / cycle.average_entry - 1) * 100
