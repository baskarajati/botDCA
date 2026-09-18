from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Any

from botdca.backtest import Candle, IntrabarPath
from botdca.historical_profile import split_regimes
from botdca.historical_validation import evaluate_anchored_cycles
from botdca.strategy import DcaStrategy, DcaTriggerReference, StrategyConfig
from botdca.trader_export import TraderCycle, TraderExportData


def compare_dca_trigger_references(
    export: TraderExportData,
    candles: list[Candle],
    *,
    baseline_config: StrategyConfig | None = None,
    train_fraction: float = 0.70,
    minimum_regime_cycles: int = 30,
    maximum_close_time_error_seconds: float = 300.0,
    tp_change_threshold_percent: float = 0.04,
    taker_fee_rate: float = 0.00055,
) -> dict[str, Any]:
    """Compare fixed DCA ladder reference rules on chronological holdout cycles."""

    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    if minimum_regime_cycles < 2:
        raise ValueError("minimum_regime_cycles must be at least two")
    if maximum_close_time_error_seconds <= 0:
        raise ValueError("maximum_close_time_error_seconds must be positive")
    if taker_fee_rate < 0:
        raise ValueError("taker_fee_rate cannot be negative")
    if not export.cycles:
        raise ValueError("at least one trader cycle is required")
    if not candles:
        raise ValueError("at least one candle is required")

    baseline = baseline_config or StrategyConfig()
    regimes = split_regimes(
        list(export.cycles),
        threshold=tp_change_threshold_percent,
    )
    evaluated: list[dict[str, Any]] = []
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
        holdout = regime[split_index:]
        config = replace(
            baseline,
            leverage=int(regime[0].leverage),
            tp_percent=round(median(_tp_percent(cycle) for cycle in training), 6),
        )
        models: list[dict[str, Any]] = []
        for reference in DcaTriggerReference:
            models.append(
                {
                    "reference": reference.value,
                    "normalized_full_ladder": _normalized_full_ladder(
                        config,
                        reference=reference,
                    ),
                    "training": _evaluate_split(
                        training,
                        candles,
                        config,
                        reference=reference,
                        maximum_close_time_error_seconds=(
                            maximum_close_time_error_seconds
                        ),
                        taker_fee_rate=taker_fee_rate,
                    ),
                    "holdout": _evaluate_split(
                        holdout,
                        candles,
                        config,
                        reference=reference,
                        maximum_close_time_error_seconds=(
                            maximum_close_time_error_seconds
                        ),
                        taker_fee_rate=taker_fee_rate,
                    ),
                }
            )
        evaluated.append(
            {
                "regime_index": regime_index,
                "leverage": regime[0].leverage,
                "completed_cycles": len(regime),
                "training_cycles": len(training),
                "holdout_cycles": len(holdout),
                "training_tp_percent": config.tp_percent,
                "models": models,
            }
        )

    if not evaluated:
        raise ValueError("no regime has enough cycles for chronological holdout")

    aggregate = _aggregate_models(evaluated)
    eligible = [
        item
        for item in aggregate
        if item["normalized_full_ladder_executable_on_descent"]
    ]
    winner = max(
        eligible,
        key=lambda item: (
            tuple(item["holdout_objective"]),
            item["reference"] == DcaTriggerReference.WEIGHTED_AVERAGE.value,
        ),
    ) if eligible else None
    current = next(
        item
        for item in aggregate
        if item["reference"] == DcaTriggerReference.WEIGHTED_AVERAGE.value
    )
    defensible_alternative = False
    if winner is not None and winner["reference"] != current["reference"]:
        defensible_alternative = all(
            winner["holdout_by_path"][path]["matched_cycles"]
            > current["holdout_by_path"][path]["matched_cycles"]
            for path in (IntrabarPath.LOW_FIRST.value, IntrabarPath.HIGH_FIRST.value)
        ) and winner["maximum_normalized_margin_usdt"] <= (
            current["maximum_normalized_margin_usdt"] * 1.05
        )

    return {
        "methodology": {
            "strategy_engine": "runtime DcaStrategy via ReplayEngine",
            "question": (
                "whether the fixed current DCA percentages reference weighted average, "
                "previous fill, or initial entry"
            ),
            "split": (
                f"first {train_fraction * 100:g} percent for TP estimation, final "
                f"{(1 - train_fraction) * 100:g} percent chronological holdout"
            ),
            "fixed_parameters": [
                "all eight DCA trigger percentages",
                "all DCA quantity multipliers",
                "base margin",
                "fee rate",
            ],
            "trained_parameter": "regime take-profit percent only",
            "intrabar_paths": [path.value for path in IntrabarPath],
            "ranking": [
                "holdout matched cycles across both paths",
                "holdout exact DCA-depth matches",
                "fewer unmatched simulated cycles",
                "lower weighted-average-entry error",
                "lower exit-price error",
            ],
            "limitations": [
                "One-minute candles do not identify exact fill prices or intrabar ordering.",
                "This tests reinterpretation of the current percentages, not a refitted ladder.",
                (
                    "Initial-entry reference is ineligible if its fixed ladder becomes "
                    "marketable above the previous fill."
                ),
                "A historical winner would not prove profitability or live safety.",
            ],
        },
        "detected_regimes": len(regimes),
        "evaluated_regimes": len(evaluated),
        "skipped_regimes": skipped,
        "aggregate_models": aggregate,
        "regimes": evaluated,
        "conclusion": {
            "best_observed_reference": winner["reference"] if winner else None,
            "defensible_alternative_to_current": defensible_alternative,
            "status": (
                "alternative_candidate"
                if defensible_alternative
                else "current_reference_not_displaced"
            ),
            "all_holdout_targets_passed": bool(winner)
            and winner["all_holdout_targets_passed"],
            "runtime_defaults_changed": False,
        },
    }


def _evaluate_split(
    cycles: list[TraderCycle],
    candles: list[Candle],
    config: StrategyConfig,
    *,
    reference: DcaTriggerReference,
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
            trigger_reference=reference,
        )
        for path in IntrabarPath
    }


def _aggregate_models(regimes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregates: list[dict[str, Any]] = []
    for reference in DcaTriggerReference:
        model_rows = [
            next(item for item in regime["models"] if item["reference"] == reference.value)
            for regime in regimes
        ]
        holdout_by_path = {
            path.value: _aggregate_path_metrics(
                [row["holdout"][path.value] for row in model_rows]
            )
            for path in IntrabarPath
        }
        objective = _objective(
            [row["holdout"][path.value] for row in model_rows for path in IntrabarPath]
        )
        tail_rows = [row["normalized_full_ladder"] for row in model_rows]
        aggregates.append(
            {
                "reference": reference.value,
                "holdout_objective": list(objective),
                "holdout_by_path": holdout_by_path,
                "normalized_full_ladder_executable_on_descent": all(
                    row["executable_on_descent"] for row in tail_rows
                ),
                "maximum_normalized_margin_usdt": max(
                    row["margin_deployed_usdt"] for row in tail_rows
                ),
                "maximum_normalized_notional_usdt": max(
                    row["position_notional_usdt"] for row in tail_rows
                ),
                "all_holdout_targets_passed": all(
                    metrics["meets_all_suggested_targets"]
                    for row in model_rows
                    for metrics in row["holdout"].values()
                ),
            }
        )
    return aggregates


def _aggregate_path_metrics(metrics: list[dict[str, Any]]) -> dict[str, float]:
    matched = sum(item["matched_cycles"] for item in metrics)
    actual = sum(item["actual_completed_cycles"] for item in metrics)
    exact = sum(_exact_depth_count(item) for item in metrics)
    return {
        "actual_completed_cycles": actual,
        "matched_cycles": matched,
        "completed_cycle_match_rate_percent": matched / actual * 100 if actual else 0.0,
        "exact_dca_depth_matches": exact,
        "exact_dca_depth_match_percent": exact / matched * 100 if matched else 0.0,
        "unmatched_simulated_cycles": sum(
            item["unmatched_simulated_cycles"] for item in metrics
        ),
    }


def _objective(metrics: list[dict[str, Any]]) -> tuple[float, ...]:
    return (
        float(sum(item["matched_cycles"] for item in metrics)),
        sum(_exact_depth_count(item) for item in metrics),
        -float(sum(item["unmatched_simulated_cycles"] for item in metrics)),
        -sum(_summary_median(item["weighted_average_entry_error_percent"]) for item in metrics),
        -sum(_summary_median(item["exit_price_error_percent"]) for item in metrics),
    )


def _exact_depth_count(metrics: dict[str, Any]) -> float:
    percent = metrics["exact_dca_depth_match_percent"]
    return 0.0 if percent is None else metrics["matched_cycles"] * percent / 100


def _summary_median(summary: dict[str, Any] | None) -> float:
    return 1_000_000.0 if summary is None else float(summary["median"])


def _normalized_full_ladder(
    config: StrategyConfig,
    *,
    reference: DcaTriggerReference,
) -> dict[str, Any]:
    strategy = DcaStrategy(config, trigger_reference=reference)
    strategy.resume()
    cycle = strategy.begin_cycle(100.0)
    fill_prices = [100.0]
    executable = True
    while True:
        trigger = strategy.next_dca_trigger_price()
        if trigger is None:
            break
        if trigger >= fill_prices[-1]:
            executable = False
            break
        strategy.apply_dca_fill(trigger)
        fill_prices.append(trigger)
    position_notional = sum(fill.price * fill.qty for fill in cycle.fills)
    return {
        "executable_on_descent": executable,
        "levels_filled": cycle.dca_level,
        "fill_prices_from_100": fill_prices,
        "final_average_entry": cycle.average_entry,
        "position_notional_usdt": position_notional,
        "margin_deployed_usdt": position_notional / config.leverage,
    }


def _tp_percent(cycle: TraderCycle) -> float:
    return (cycle.closing_price / cycle.average_entry - 1) * 100
